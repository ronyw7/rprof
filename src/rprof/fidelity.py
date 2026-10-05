"""Fidelity checks for ``rprof selftest``: does rprof record what a known workload does?

Each check runs a workload with a known footprint (2 busy cores, 1 GiB of memory, a 512 MiB
write, a 10 MiB transfer, 50 processes) as a tool call in a short measure-mode run against a
scratch container. The result is then read back from that run's samples.jsonl and
events.jsonl with the same code reports use. A check passes only if sampling, parsing, rate
calculation, the memory basis and per-call attribution all work on this host.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .client import Client
from .profile import unlimited_profile
from .report.data import RunData
from .runner import RunOptions, RunSession
from .selftest import Check, Reporter
from .snapshot import state_dir
from .util import run_cmd

MiB = 1 << 20
HZ = 20
# Pass bands: the same as tests/tolerances.yaml's fidelity section.
CPU_REL = 0.10            # recorded cores within ±10% of 2
MEM_REL = 0.10            # recorded memory rise within ±10% of 1 GiB
MEM_RELEASE = 32 * MiB    # back within 32 MiB of the baseline 2 s after the process exits
CACHE_NR_MAX = 64 * MiB   # reading a file may raise non-reclaimable memory by at most this
IO_REL = 0.02             # bytes written within ±2%
NET_REL = 0.03            # bytes sent within ±3% of what iperf3 reports
PIDS_ABS = 3              # 50 processes ±3 (the shell running them counts too)
ALIGN_SAMPLES = 2         # the memory rise shows up within 2 samples of tool_start...
EXEC_SLACK_S = 0.25       # ...plus docker exec start-up


def idle_cores(window_s: float = 0.5) -> float:
    """How many CPUs are idle right now, from /proc/stat."""
    def read():
        f = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        return f[3] + f[4], sum(f[:8])
    i0, t0 = read()
    time.sleep(window_s)
    i1, t1 = read()
    return (os.cpu_count() or 1) * (i1 - i0) / max(1, t1 - t0)


class _Run:
    """A measure-mode rprof run, in-process, against one container."""

    def __init__(self, container: str, runs_dir: Path):
        opts = RunOptions(target=f"docker:{container}", mode="measure", hz=HZ, runs_dir=str(runs_dir),
                          name="selftest", self_cgroup=False, view_dir=str(runs_dir / "view"), quiet=True,
                          duration=1e6, report=False)
        self.sess = RunSession(opts, unlimited_profile("selftest"))
        self.sess.setup()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._main, name="rprof-selftest-run", daemon=True)
        self.thread.start()
        for _ in range(500):
            if getattr(self.sess, "loop", None) is not None and self.sess.sampler.count > 0:
                break
            time.sleep(0.01)
        self.client = Client(run_dir=str(self.sess.run_dir), timeout_s=10)
        self.container = container

    def _main(self):
        try:
            asyncio.run(self.sess.main())
        except BaseException as e:  # noqa: BLE001
            self.error = e

    def call(self, call_id: str, cmd: str, timeout: float = 120) -> tuple[int, str]:
        self.client.tool_start(call_id, cmd)
        t0 = time.monotonic()
        r = run_cmd(["docker", "exec", self.container, "sh", "-c", cmd], timeout=timeout)
        out = r.out + r.err
        self.client.tool_end(call_id, r.rc, time.monotonic() - t0, output=out)
        return r.rc, out

    def stop(self) -> RunData:
        self.sess.request_stop("profile_end")
        self.thread.join(30)
        self.sess.teardown()
        self.client.close()
        return RunData(self.sess.run_dir)


def _at(rd: RunData, ys: list, t: float) -> float | None:
    return rd._interp(ys, t)


def _bracket_delta(rd: RunData, ys: list, t0: float, t1: float) -> float | None:
    """Counter change from the last sample before t0 to the first sample after t1.

    The container is idle outside the call, so this is the call's whole total. Interpolating
    at t0 and t1 instead would split a burst shorter than a sample interval across both sides.
    """
    before = [ys[i] for i in range(len(rd.t)) if rd.t[i] <= t0 and ys[i] is not None]
    after = [ys[i] for i in range(len(rd.t)) if rd.t[i] >= t1 and ys[i] is not None]
    if not before or not after:
        return None
    return after[0] - before[-1]


def _max_in(rd: RunData, ys: list, t0: float, t1: float) -> float | None:
    vals = [ys[i] for i in rd.window_idx(t0, t1) if ys[i] is not None]
    return max(vals) if vals else None


def _rise(peak: float | None, base: float | None) -> float | None:
    return None if peak is None or base is None else peak - base


def _mib(x: float | None, digits: int = 0, sign: bool = False) -> str:
    return "-" if x is None else f"{x / MiB:{'+' if sign else ''}.{digits}f} MiB"


def analyse(rd: RunData, ran: dict[str, Any]) -> list[Check]:
    """Compare what the run recorded with what each workload did."""
    calls = {c.call_id: c for c in rd.calls}
    nr, basis = rd.mem_series("non_reclaimable")
    total, _ = rd.mem_series("total")
    out: list[Check] = []

    cpu_expect = f"{2 * (1 - CPU_REL):.2f}–{2 * (1 + CPU_REL):.2f} cores"
    if "cpu" in ran:
        c = calls["cpu"]
        r = rd.rate(("cpu", "usage_usec"), c.t0 + 1.0, c.end(rd.t_end) - 0.5)
        cores = None if r is None else r / 1e6
        ok = cores is not None and abs(cores - 2.0) <= CPU_REL * 2.0
        out.append(Check("cpu", ok, cpu_expect, "no samples" if cores is None else f"{cores:.2f} cores",
                         "stress-ng --cpu 2 for 5 s", label="CPU usage", measured=cores, unit="cores"))
    elif "cpu_skipped" in ran:
        out.append(Check("cpu", None, cpu_expect, "not tested", "stress-ng --cpu 2 for 5 s", label="CPU usage",
                         note=ran["cpu_skipped"]))

    c = calls["memory"]
    base = _at(rd, nr, c.t0)
    peak = c.mem_peak_nonreclaimable
    rise = None if peak is None or base is None else peak - base
    ok = rise is not None and abs(rise - 1024 * MiB) <= MEM_REL * 1024 * MiB
    out.append(Check("memory_peak", ok, f"{1024 * (1 - MEM_REL):.0f}–{1024 * (1 + MEM_REL):.0f} MiB", _mib(rise),
                     "hog-mem 1G holds 1 GiB for 3 s; the call's peak above the memory before it",
                     label="memory peak", note=None if basis == "non_reclaimable" else f"memory basis: {basis}",
                     measured=rise, unit="bytes"))
    after = _at(rd, nr, c.end(rd.t_end) + 2.0)
    diff = None if after is None or base is None else after - base
    ok = diff is not None and abs(diff) <= MEM_RELEASE
    out.append(Check("memory_after_exit", ok, f"within ±{_mib(MEM_RELEASE)}", _mib(diff, sign=True),
                     "memory 2 s after hog-mem exits, minus the memory before it started",
                     label="memory after exit", measured=diff, unit="bytes"))
    rise_t = next((rd.t[i] for i in range(len(rd.t)) if rd.t[i] > c.t0 and nr[i] is not None
                   and base is not None and nr[i] - base > 128 * MiB), None)
    lag = None if rise_t is None else rise_t - c.t0
    limit = ALIGN_SAMPLES / HZ + EXEC_SLACK_S
    ok = lag is not None and lag <= limit
    out.append(Check("call_timing", ok, f"≤{limit:.2f} s", "never" if lag is None else f"{lag:.2f} s",
                     "time from tool_start until hog-mem's memory shows in the samples",
                     label="call timing", measured=lag, unit="s"))

    c = calls["io"]
    wrote = _bracket_delta(rd, rd.io_series("wbytes"), c.t0, c.end(rd.t_end))
    ok = wrote is not None and abs(wrote - 512 * MiB) <= IO_REL * 512 * MiB
    out.append(Check("disk_writes", ok, f"{512 * (1 - IO_REL):.0f}–{512 * (1 + IO_REL):.0f} MiB", _mib(wrote),
                     f"dd writes 512 MiB to {rd.io_dev} with O_DIRECT", label="disk writes",
                     measured=wrote, unit="bytes"))

    c = calls["page_cache"]
    t1 = c.end(rd.t_end)
    tot_rise = _rise(_max_in(rd, total, c.t0, t1), _at(rd, total, c.t0))
    nr_rise = _rise(_max_in(rd, nr, c.t0, t1), _at(rd, nr, c.t0))
    cached = tot_rise is not None and tot_rise >= 400 * MiB
    ok = cached and nr_rise is not None and nr_rise <= CACHE_NR_MAX
    out.append(Check("page_cache", ok, f"≤{_mib(CACHE_NR_MAX)}",
                     _mib(nr_rise, sign=True) + ("" if cached else f"; total only {_mib(tot_rise, sign=True)}"),
                     "write, sync and read a 512 MiB file; memory may rise by the page cache only",
                     label="page cache excluded",
                     note=f"total memory, page cache included, rose {_mib(tot_rise)} (must be ≥ 400 MiB)",
                     measured={"memory": nr_rise, "total": tot_rise}, unit="bytes"))

    if "network" in ran:
        c = calls["network"]
        sent_rec = _bracket_delta(rd, rd.net_series("tx_bytes"), c.t0, c.end(rd.t_end))
        sent = ran["network"]
        ok = sent_rec is not None and sent is not None and abs(sent_rec - sent) <= NET_REL * sent
        out.append(Check("network_sent", ok, f"{_mib(sent, 1)} ±{NET_REL:.0%}", _mib(sent_rec, 1),
                         "iperf3 sends 10 MiB to a peer container; expected is what iperf3 reports",
                         label="network sent", measured=sent_rec, unit="bytes"))
    else:
        out.append(Check("network_sent", None, "10.0 MiB ±3%", "not tested", "iperf3 sends 10 MiB to a peer",
                         label="network sent", note=ran.get("network_skipped")))

    c = calls["pids"]
    pids = rd.series(("pids", "current"))
    p0, pk = _at(rd, pids, c.t0), _max_in(rd, pids, c.t0, c.end(rd.t_end))
    rise = None if p0 is None or pk is None else pk - p0
    ok = rise is not None and abs(rise - 50) <= PIDS_ABS
    out.append(Check("processes", ok, f"+{50 - PIDS_ABS}–{50 + PIDS_ABS}", "-" if rise is None else f"{rise:+.0f}",
                     "start 50 background sleeps", label="processes", measured=rise, unit="processes"))
    return out


def run_fidelity(image: str = "rprof-testbox", rep: Reporter | None = None) -> dict[str, dict]:
    """Run the fidelity checks; returns {check: result}. Keeps the run directory if a check fails."""
    tag = uuid.uuid4().hex[:6]
    net, box, peer = f"rprof-selftest-fid-{tag}", f"rprof-selftest-fid-{tag}", f"rprof-selftest-fidpeer-{tag}"
    ran: dict[str, Any] = {}
    runs_dir = state_dir() / "selftest" / tag
    rep = rep or Reporter()
    rep.section("Fidelity", f"  Workloads of known size run as tool calls in a measure-mode recording at {HZ} Hz;\n"
                            "  the samples are read back with the report code.")
    rep.line()
    have_peer = run_cmd(["docker", "image", "inspect", "rprof-netpeer"], timeout=20).ok
    run = None
    try:
        run_cmd(["docker", "network", "create", net], timeout=30, check=True)
        if have_peer:
            run_cmd(["docker", "run", "-d", "--name", peer, "--network", net, "rprof-netpeer"], timeout=60, check=True)
        run_cmd(["docker", "run", "-d", "--name", box, "--network", net, "--cgroup-parent=rprof-selftest.slice",
                 image, "sleep", "infinity"], timeout=60, check=True)
        idle = idle_cores()
        run = _Run(box, runs_dir)
        time.sleep(1.0)                                   # baseline samples
        if idle >= 2.5:
            run.call("cpu", "stress-ng --cpu 2 --timeout 5s")
            ran["cpu"] = True
        else:
            ran["cpu_skipped"] = f"only {idle:.1f} CPUs are idle; the test needs 2.5"
        run.call("memory", "hog-mem 1G 3")
        time.sleep(2.5)                                   # the release check looks 2 s after exit
        run.call("io", "dd if=/dev/zero of=/var/tmp/rprof-io bs=1M count=512 oflag=direct status=none; "
                       "rm -f /var/tmp/rprof-io")
        run.call("page_cache", "dd if=/dev/zero of=/var/tmp/rprof-cache bs=1M count=512 status=none && sync && "
                               "cat /var/tmp/rprof-cache > /dev/null; rm -f /var/tmp/rprof-cache")
        if have_peer:
            for _ in range(50):                           # the peer's iperf3 server takes a moment to start
                if run_cmd(["docker", "exec", peer, "sh", "-c", "grep -q ':1451 ' /proc/net/tcp*"]).ok:
                    break
                time.sleep(0.1)
            _, out = run.call("network", f"iperf3 -c {peer} -n 10M -J")
            try:
                ran["network"] = json.loads(out[out.index("{"):])["end"]["sum_sent"]["bytes"]
            except (ValueError, KeyError):
                ran["network"] = None
        else:
            ran["network_skipped"] = "the rprof-netpeer image is missing: docker build -t rprof-netpeer images/netpeer"
        run.call("pids", "for i in $(seq 50); do sleep 4 & done; sleep 1")
        time.sleep(0.5)
        rd = run.stop()
        run = None
        checks = analyse(rd, ran)
    finally:
        if run is not None:
            try:
                run.stop()
            except Exception:  # noqa: BLE001
                pass
        run_cmd(["docker", "rm", "-f", box, peer], timeout=30)
        run_cmd(["docker", "network", "rm", net], timeout=30)

    for c in checks:
        rep.check(c)
    if any(c.ok is False for c in checks):
        rep.line(f"  The recording is kept for inspection: {rd.dir}")
    else:
        shutil.rmtree(runs_dir, ignore_errors=True)
    return {c.name: c.as_dict() for c in checks}
