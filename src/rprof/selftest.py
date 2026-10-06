"""``rprof selftest``: host enforcement and fidelity checks -> ``capabilities.json``.

Enforcement: each knob is set on a scratch ``rprof-testbox`` container (with an
``rprof-netpeer`` for network knobs) through rprof's own controllers, and a workload that
needs more than the limit checks that the kernel holds it. Fidelity (``rprof.fidelity``):
workloads with a known footprint run as tool calls in a short measure-mode run, and what rprof
recorded is compared with it.

Every run copies capabilities.json into its meta.json. ``run`` refuses knobs that failed
enforcement unless given ``--allow-degraded``, and warns if a fidelity check failed.
"""

from __future__ import annotations

import datetime as _dt
import json
import platform
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import controllers as C
from . import knobs as K
from .snapshot import Snapshot, restore, state_dir
from .target import resolve
from .target.cgroup import parse_flat, read_text
from .util import run_cmd, write_json


def _round(x: Any) -> Any:
    if isinstance(x, float):
        return round(x, 3) if abs(x) < 1e4 else int(round(x))
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    return x


@dataclass
class Check:
    """One check's result: a line of output, and its entry in capabilities.json."""
    name: str                 # the key in capabilities.json: a knob, or a fidelity measurement
    ok: bool | None           # None: not tested on this host
    expected: str             # the pass condition
    observed: str             # what happened
    workload: str             # what ran in the container
    label: str = ""           # what the line shows; defaults to name
    note: str | None = None   # more about the result, e.g. why it wasn't tested
    measured: Any = None      # the number behind ``observed``
    unit: str | None = None
    info: bool = False        # reports host behaviour; no pass condition

    @property
    def status(self) -> str:
        return "INFO" if self.info else {True: "PASS", False: "FAIL", None: "SKIP"}[self.ok]

    @property
    def detail(self) -> str:
        return f"observed {self.observed}, expected {self.expected} ({self.workload})" + (
            f"; {self.note}" if self.note else "")

    def as_dict(self) -> dict:
        d: dict[str, Any] = {"ok": self.ok, "label": self.label or self.name, "expected": self.expected,
                             "observed": self.observed, "workload": self.workload, "detail": self.detail}
        if self.note:
            d["note"] = self.note
        if self.measured is not None:
            d["measured"] = _round(self.measured)
            d["unit"] = self.unit
        return d


COLORS = {"PASS": "32", "FAIL": "1;31", "WARN": "33", "SKIP": "33", "INFO": "2", "bold": "1", "dim": "2"}
W_LABEL, W_EXPECTED = 20, 22


class Reporter:
    """Prints results as they come in: one row per check by default; ``verbose`` also shows
    the workload and method; ``quiet`` prints nothing (the caller prints one summary line).
    Failed and skipped checks are always expanded."""

    def __init__(self, echo: Callable[[str], None] = print, verbose: bool = False, quiet: bool = False,
                 color: bool = False):
        self.echo, self.verbose, self.quiet, self.color = echo, verbose, quiet, color
        self.group: str | None = None

    def paint(self, text: str, style: str) -> str:
        return f"\033[{COLORS[style]}m{text}\033[0m" if self.color and style in COLORS else text

    def line(self, text: str = "") -> None:
        if not self.quiet:
            self.echo(text)

    def section(self, title: str, method: str = "") -> None:
        self.group = None
        self.line()
        self.line(self.paint(title, "bold"))
        if method and self.verbose:
            self.line(self.paint(method, "dim"))

    def subgroup(self, name: str) -> None:
        if name != self.group:
            self.group = name
            self.line()
            self.line(f"  {self.paint(name, 'bold')}")

    def check(self, c: Check) -> None:
        status = self.paint(f"{c.status:<4}", c.status)
        label = c.label or c.name
        expand = self.verbose or c.ok is False or (c.ok is None and not c.info)
        if expand:
            self.line(f"  {status}  {label}")
            for k, v in (("expected" if not c.info else "finding", c.expected), ("observed", c.observed),
                         ("workload", c.workload), ("note", c.note)):
                if v:
                    self.line(f"        {k:<10} {v}")
            return
        mid = c.expected if c.info else f"expected {c.expected}"
        self.line(f"  {status}  {label:<{W_LABEL}} {mid:<{W_EXPECTED + 9}} observed {c.observed}")


def _tally(results: dict) -> tuple[int, list[str], list[str]]:
    """(passed, failed names, skipped names)."""
    passed = sum(1 for v in results.values() if v.get("ok") is True)
    return (passed, [k for k, v in results.items() if v.get("ok") is False],
            [k for k, v in results.items() if v.get("ok") is None])


def _part_status(results: dict) -> str:
    passed, failed, _ = _tally(results)
    return "FAIL" if failed and not passed else "WARN" if failed else "PASS"


def summarize(out: Reporter, knobs: dict, fid: dict, path: Path, kept: dict[str, str]) -> None:
    """The summary: a status per part, then what it means for ``rprof run``."""
    out.section("Summary")
    out.line()
    for title, results in (("Enforcement", knobs), ("Fidelity", fid)):
        passed, failed, skipped = _tally(results)
        st = _part_status(results)
        extra = (f", {len(skipped)} skipped" if skipped else "") + (
            f"  (from {kept[title]})" if title in kept else "")
        out.line(f"  {out.paint(f'{st:<4}', st)}  {title:<13} {passed:>2} / {len(results)}{extra}")
    out.line()
    _, bad_knobs, skipped_knobs = _tally(knobs)
    _, bad_fid, skipped_fid = _tally(fid)
    if bad_knobs or bad_fid:
        out.line("  System is partially supported.")
    elif skipped_knobs or skipped_fid:
        out.line("  System is supported; some checks could not run here.")
    else:
        out.line("  System is fully supported.")
    if bad_knobs:
        out.line(f"  `rprof run` refuses profiles that set {', '.join(bad_knobs)}; "
                 "use --allow-degraded to run without them.")
    if bad_fid:
        labels = ", ".join(fid[k].get("label") or k.replace("_", " ") for k in bad_fid)
        out.line(f"  Recorded {labels} may be wrong; `rprof run` warns about this.")
    for k in skipped_knobs + skipped_fid:
        r = knobs.get(k) or fid.get(k) or {}
        out.line(f"  Not tested: {k}" + (f" ({r['note']})" if r.get("note") else ""))
    out.line(f"  Capabilities saved to {path}")


def quiet_line(knobs: dict, fid: dict) -> str:
    sts = {_part_status(knobs), _part_status(fid)}
    st = "FAIL" if "FAIL" in sts else "WARN" if "WARN" in sts else "PASS"
    (pk, bk, _), (pf, bf, _) = _tally(knobs), _tally(fid)
    s = f"rprof selftest: {st} ({pk}/{len(knobs)} enforcement, {pf}/{len(fid)} fidelity)"
    return s + (f"; failed: {', '.join(bk + bf)}" if bk or bf else "")


class Box:
    """A scratch sandbox container plus helpers."""

    def __init__(self, image: str, name: str, network: str | None = None, data_mb: int | None = None):
        self.name = name
        args = ["docker", "run", "-d", "--name", name, "--cgroup-parent=rprof-selftest.slice"]
        if network:
            args += ["--network", network]
        if data_mb:
            args += ["--mount", f"type=tmpfs,dst=/data,tmpfs-size={data_mb}m"]
        r = run_cmd(args + [image, "sleep", "infinity"], timeout=60)
        if not r.ok:
            raise RuntimeError(f"cannot start {image}: {r.err}")
        self.target = resolve(f"docker:{name}")
        self.snap = Snapshot(None, f"selftest-{name}", self.target.to_meta())
        self.ctrls = C.build(self.target, self.snap, f"selftest-{name}")
        self.applied: dict[str, Any] = {}

    def apply(self, **kv) -> list[str]:
        vals = {k.replace("_", ".", 1): v for k, v in kv.items()}
        parsed = {k: K.KNOBS[k].parse(v) for k, v in vals.items()}
        full = K.defaults()
        full.update(self.applied)
        full.update(parsed)
        errs: list[str] = []
        for c in self.ctrls:
            mine = {k for k in parsed if k in c.knobs}
            if mine:
                errs += c.apply(full, mine)
        self.applied.update(parsed)
        return errs

    def exec(self, cmd: str, timeout: float = 60) -> tuple[int, str, float]:
        t0 = time.monotonic()
        r = run_cmd(["docker", "exec", self.name, "sh", "-c", cmd], timeout=timeout)
        return r.rc, (r.out + r.err), time.monotonic() - t0

    def stat(self, f: str) -> dict[str, int]:
        return parse_flat(read_text(self.target.cgroup.file(f)))

    def close(self):
        try:
            restore(self.snap)
        except Exception:  # noqa: BLE001
            pass
        run_cmd(["docker", "rm", "-f", self.name], timeout=30)


def _cpu(box: Box, cmd: str, secs: float) -> tuple[float, float]:
    """(cores used, CFS periods per second) while ``cmd`` runs."""
    a = box.stat("cpu.stat")
    t0 = time.monotonic()
    box.exec(cmd, timeout=secs + 30)
    b, dt = box.stat("cpu.stat"), time.monotonic() - t0
    return (b["usage_usec"] - a["usage_usec"]) / 1e6 / dt, (b.get("nr_periods", 0) - a.get("nr_periods", 0)) / dt


PARTS = ("enforcement", "fidelity")


def selftest(quick: bool = True, out: Path | None = None, image: str = "rprof-testbox",
             echo: Callable[[str], None] = print, only: str | None = None, verbose: bool = False,
             quiet: bool = False, color: bool = False) -> tuple[dict, bool]:
    """Run the checks and write capabilities.json; returns (capabilities, all passed).

    ``only`` runs one part ("enforcement" or "fidelity") and keeps the other part's results
    from an existing capabilities.json.
    """
    if only is not None and only not in PARTS:
        raise ValueError(f"--only must be one of {', '.join(PARTS)}")
    if not run_cmd(["docker", "image", "inspect", image], timeout=20).ok:
        echo(f"rprof selftest: image {image} not found; build it with `docker build -t {image} images/testbox`")
        return {}, False
    rep = Reporter(echo, verbose=verbose, quiet=quiet, color=color)
    path = Path(out) if out else state_dir() / "capabilities.json"
    try:
        previous = json.loads(path.read_text()) if only else {}
    except (OSError, ValueError):
        previous = {}
    knobs, features = previous.get("knobs", {}), previous.get("features", {})
    fid = previous.get("fidelity", {})
    rep.line(f"rprof {__version__}")
    rep.line(f"host {socket.gethostname()} · Linux {platform.release()}")
    rep.line()
    rep.line(rep.paint("Self-test", "bold"))
    rep.line("Verifying resource enforcement and measurement fidelity.")
    if only in (None, "enforcement"):
        knobs, features = _enforcement(quick, image, rep)
    if only in (None, "fidelity"):
        from .fidelity import run_fidelity
        fid = run_fidelity(image, rep)
    caps = {"rprof_version": __version__, "at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "host": {"hostname": socket.gethostname(), "kernel": platform.release()},
            "quick": quick, "knobs": knobs, "features": features, "fidelity": fid}
    write_json(path, caps)
    at = previous.get("at", "an earlier selftest")
    kept = {"Fidelity": at} if only == "enforcement" else {"Enforcement": at} if only == "fidelity" else {}
    summarize(rep, knobs, fid, path, kept)
    if quiet:
        echo(quiet_line(knobs, fid))
    ok = all(v["ok"] is not False for v in knobs.values()) and all(v["ok"] is not False for v in fid.values())
    return caps, ok


GROUPS = {"cpu": "CPU", "mem": "Memory", "pids": "Processes", "io": "I/O", "disk": "I/O", "net": "Network"}


def _enforcement(quick: bool, image: str, out: Reporter) -> tuple[dict, dict]:
    tag = uuid.uuid4().hex[:6]
    secs = 3 if quick else 8
    knobs: dict[str, dict] = {}
    features: dict[str, Any] = {}
    swap_total = 0

    def record(c: Check) -> None:
        if not c.info:
            knobs[c.name] = c.as_dict()
        out.subgroup(GROUPS[c.name.split(".")[0]] if "." in c.name else out.group or "")
        out.check(c)

    out.section("Enforcement", f"  Each limit is set on a scratch {image} container through the same code as "
                               "`rprof run`,\n  then a workload that needs more than the limit runs in it. "
                               "Nothing is recorded.")

    box = Box(image, f"rprof-selftest-{tag}", data_mb=256)
    net = f"rprof-selftest-{tag}"
    peer = None
    try:
        # ---- CPU
        stress2 = f"stress-ng --cpu 2 --timeout {secs}s"
        box.apply(cpu_cores=0.5)
        c, _ = _cpu(box, stress2, secs)
        record(Check("cpu.cores", abs(c - 0.5) <= 0.1, "0.40–0.60 cores", f"{c:.2f} cores",
                     f"stress-ng --cpu 2 for {secs} s", label="cpu.cores=0.5", measured=c, unit="cores"))
        box.apply(cpu_period="20ms")
        c, per = _cpu(box, stress2, secs)
        cores_ok = abs(c - 0.5) <= 0.1
        record(Check("cpu.period", 40 <= per <= 60 and cores_ok, "40–60 periods/s", f"{per:.0f} periods/s",
                     f"stress-ng --cpu 2 for {secs} s under cpu.cores=0.5", label="cpu.period=20ms",
                     note=None if cores_ok else f"used {c:.2f} cores; expected 0.40–0.60",
                     measured=per, unit="periods/s"))
        box.apply(cpu_cores="max", cpu_period="100ms")
        if box.target.cgroup.has("cpuset.cpus"):
            first = read_text(box.target.cgroup.file("cpuset.cpus.effective")).strip().split(",")[0].split("-")[0]
            box.apply(cpu_cpus=first)
            c, _ = _cpu(box, f"stress-ng --cpu 4 --timeout {secs}s", secs)
            record(Check("cpu.cpus", c <= 1.1, "≤1.10 cores", f"{c:.2f} cores", f"stress-ng --cpu 4 for {secs} s",
                         label=f"cpu.cpus={first}", measured=c, unit="cores"))
            box.apply(cpu_cpus="all")
        else:
            record(Check("cpu.cpus", False, "≤1.10 cores", "cannot set cpuset.cpus", "-", label="cpu.cpus",
                         note="the cpuset controller is not enabled for containers"))

        # ---- memory
        ev0 = box.stat("memory.events")
        box.apply(mem_max="128Mi", mem_swap_max=0)
        rc, _, _ = box.exec("hog-mem 256M 2")
        ev1 = box.stat("memory.events")
        kills = ev1.get("oom_kill", 0) - ev0.get("oom_kill", 0)
        alive = run_cmd(["docker", "inspect", "-f", "{{.State.Running}}", box.name]).out.strip() == "true"
        record(Check("mem.max", rc == 137 and kills > 0 and alive, "OOM kill",
                     f"exit {rc}, {kills} OOM kill{'s' if kills != 1 else ''}" + ("" if alive else ", container died"),
                     "hog-mem 256M (allocates 256 MiB)", label="mem.max=128Mi",
                     note=None if alive else "the container must survive the kill", measured=kills, unit="oom_kills"))
        box.apply(mem_max="max")
        box.apply(mem_high="128Mi")
        ev0 = box.stat("memory.events")
        rc, _, dt = box.exec("timeout 4 hog-mem 160M 2; echo rc=$?")
        ev1 = box.stat("memory.events")
        hi = ev1.get("high", 0) - ev0.get("high", 0)
        record(Check("mem.high", hi > 0, "throttling", f"throttled {hi}× in {dt:.1f} s",
                     "hog-mem 160M, stopped after 4 s; counts memory.events high", label="mem.high=128Mi",
                     measured=hi, unit="high_events"))
        box.apply(mem_high="max")
        try:
            swap_total = int([ln for ln in Path("/proc/meminfo").read_text().splitlines()
                              if ln.startswith("SwapTotal")][0].split()[1]) * 1024
        except (OSError, IndexError, ValueError):
            pass
        if not box.target.cgroup.has("memory.swap.max"):
            record(Check("mem.swap_max", False, "workload completes", "cannot set memory.swap.max", "-",
                         label="mem.swap_max=128Mi", note="swap accounting is off"))
        elif swap_total < (256 << 20):
            record(Check("mem.swap_max", None, "workload completes", "not tested", "hog-mem 160M under mem.max=96Mi",
                         label="mem.swap_max=128Mi", note=f"host swap is {swap_total >> 20} MiB; the test needs 256 MiB"))
        else:
            box.apply(mem_max="96Mi", mem_swap_max="128Mi")
            rc, _, _ = box.exec("hog-mem 160M 2")
            record(Check("mem.swap_max", rc == 0, "workload completes", f"exit {rc}",
                         "hog-mem 160M under mem.max=96Mi", label="mem.swap_max=128Mi"))
            box.apply(mem_max="max", mem_swap_max=0)

        # ---- pids
        box.apply(pids_max=20)
        p0 = box.stat("pids.events").get("max", 0)
        box.exec("for i in $(seq 50); do sleep 5 & done 2>/dev/null; sleep 0.5", timeout=30)
        n = box.stat("pids.events").get("max", 0) - p0
        record(Check("pids.max", n > 0, "fork refused", f"{n} fork{'s' if n != 1 else ''} refused",
                     "start 50 background sleeps", label="pids.max=20", measured=n, unit="refused_forks"))
        box.exec("pkill sleep || true")
        box.apply(pids_max="max")

        # ---- io: each limit in turn, timing direct I/O that needs longer than the limit allows
        mb, n = (40, 400) if quick else (100, 1000)
        dev = box.target.io_device
        io_tests = [
            ("io.wbps", "20Mi", f"write {mb} MiB to {dev} with O_DIRECT at 20 MiB/s",
             f"dd if=/dev/zero of=/var/tmp/rprof-bw bs=1M count={mb} oflag=direct", mb / 20),
            ("io.rbps", "20Mi", f"read {mb} MiB from {dev} with O_DIRECT at 20 MiB/s",
             "dd if=/var/tmp/rprof-bw of=/dev/null bs=1M iflag=direct", mb / 20),
            ("io.wiops", 200, f"{n} 4 KiB writes with O_DIRECT at 200/s",
             f"dd if=/dev/zero of=/var/tmp/rprof-iops bs=4k count={n} oflag=direct", n / 200),
            ("io.riops", 200, f"{n} 4 KiB reads with O_DIRECT at 200/s",
             "dd if=/var/tmp/rprof-iops of=/dev/null bs=4k iflag=direct", n / 200),
        ]
        if not dev:
            for k, v, *_ in io_tests:
                record(Check(k, False, "a limited device", "no block device", "-", label=f"{k}={v}",
                             note="no block device found for the container's filesystem"))
        else:
            for k, v, workload, cmd, want in io_tests:
                errs = box.apply(**{k.replace(".", "_"): v})
                rc, outp, dt = box.exec(f"{cmd} 2>&1", timeout=120)
                box.apply(**{k.replace(".", "_"): "max"})
                ok = not errs and rc == 0 and abs(dt - want) <= 0.3 * want
                note = "; ".join(errs + ([f"dd: {outp.strip()[-120:]}"] if rc else [])) or None
                record(Check(k, ok, f"{0.7 * want:.1f}–{1.3 * want:.1f} s", f"{dt:.1f} s" if rc == 0 else "dd failed",
                             workload, label=f"{k}={v}", note=note, measured=dt, unit="s"))
            box.exec("rm -f /var/tmp/rprof-bw /var/tmp/rprof-iops")
            # Not a knob: whether io.max also slows buffered writes, via cgroup writeback.
            box.apply(io_wbps="20Mi")
            rc, _, dt = box.exec(f"dd if=/dev/zero of=/var/tmp/rprof-dd bs=1M count={mb} 2>/dev/null; sync; "
                                 "rm -f /var/tmp/rprof-dd", timeout=120)
            box.apply(io_wbps="max")
            features["io_buffered_writes_throttled"] = dt >= 0.6 * mb / 20
            record(Check("buffered writes", None, "limit applies to buffered I/O"
                         if features["io_buffered_writes_throttled"] else "limit skips buffered I/O",
                         f"{dt:.1f} s", f"write {mb} MiB without O_DIRECT, then sync, under io.wbps=20Mi", info=True))
            # Not a knob either: the disk's own speed, which `rprof describe` reports when a profile
            # doesn't throttle it.
            bw = disk_bandwidth(1024 if quick else 4096)
            if bw:
                features["disk_bandwidth"] = bw
                record(Check("disk bandwidth", None, "native speed, not throttled",
                             f"{bw['write_bps'] / 1e9:.1f} GB/s write, {bw['read_bps'] / 1e9:.1f} GB/s read",
                             f"median of {bw['repeats']} passes of {bw['bytes'] >> 20} MiB with O_DIRECT in "
                             f"{bw['path']}", info=True))

        # ---- disk
        cap = 100
        errs = box.apply(disk_capacity=f"{cap}Mi")
        rc, outp, _ = box.exec("dd if=/dev/zero of=/data/fill bs=1M count=200 2>&1; df -m /data | tail -1; "
                               "ls -l /data/fill | awk '{print $5}'")
        last = outp.strip().split()[-1] if outp.strip() else ""
        written = (int(last) if last.isdigit() else 0) / 2**20
        enospc = "No space" in outp
        record(Check("disk.capacity", enospc and abs(written - cap) <= 0.15 * cap and not errs,
                     f"ENOSPC at {0.85 * cap:.0f}–{1.15 * cap:.0f} MiB",
                     f"ENOSPC at {written:.0f} MiB" if enospc else f"no ENOSPC ({written:.0f} MiB)",
                     "write 200 MiB to /data", label=f"disk.capacity={cap}Mi", note="; ".join(errs) or None,
                     measured=written, unit="MiB"))
        box.exec("rm -f /data/fill")
        box.apply(disk_capacity="max")
    finally:
        box.close()

    # ---- network: a second container and a peer on their own Docker network
    net_labels = {"net.rate": "net.rate=10mbit", "net.delay": "net.delay=50ms", "net.jitter": "net.jitter=20ms",
                  "net.loss": "net.loss=30%", "net.partition": "net.partition=reject", "net.allow": "net.allow"}
    try:
        run_cmd(["docker", "network", "create", net], timeout=30)
        peer_name = f"rprof-selftest-peer-{tag}"
        if not run_cmd(["docker", "image", "inspect", "rprof-netpeer"], timeout=20).ok:
            for k, label in net_labels.items():
                record(Check(k, None, "-", "not tested", "-", label=label,
                             note="the rprof-netpeer image is missing: docker build -t rprof-netpeer images/netpeer"))
        else:
            run_cmd(["docker", "run", "-d", "--name", peer_name, "--network", net, "rprof-netpeer"], timeout=60)
            peer = peer_name
            nb = Box(image, f"rprof-selftest-net-{tag}", network=net)
            try:
                _network(nb, peer, net, secs, record)
            finally:
                nb.close()
    finally:
        if peer:
            run_cmd(["docker", "rm", "-f", peer], timeout=30)
        run_cmd(["docker", "network", "rm", net], timeout=30)

    features["psi"] = Path("/proc/pressure/cpu").exists()
    features["swap_bytes"] = swap_total
    return knobs, features


def disk_bandwidth(mib: int = 4096, repeats: int = 5) -> dict | None:
    """The disk's native write and read speed where Docker keeps containers: the median of
    ``repeats`` passes of ``mib`` MiB of direct I/O (a shared disk varies from pass to pass)."""
    root = run_cmd(["docker", "info", "-f", "{{.DockerRootDir}}"], timeout=20, quiet=True).out.strip() or "/var/lib/docker"
    f = Path(root) / f".rprof-bandwidth-{uuid.uuid4().hex[:6]}"
    speeds: dict[str, list[int]] = {"write_bps": [], "read_bps": []}
    try:
        for _ in range(repeats):
            for key, args in (("write_bps", ["if=/dev/zero", f"of={f}", "oflag=direct"]),
                              ("read_bps", [f"if={f}", "of=/dev/null", "iflag=direct"])):
                t0 = time.monotonic()
                r = run_cmd(["dd", *args, "bs=1M", f"count={mib}"], timeout=600, quiet=True)
                if not r.ok:
                    return None
                speeds[key].append(int(mib * (1 << 20) / (time.monotonic() - t0)))
    finally:
        f.unlink(missing_ok=True)
    med = {k: sorted(v)[len(v) // 2] for k, v in speeds.items()}
    return {"path": root, "bytes": mib << 20, "repeats": repeats, **med, "samples": speeds}


def _ping(nb: Box, dest: str, count: int, interval: float = 0.2) -> tuple[float | None, float | None, float]:
    """(average RTT ms, RTT spread (mdev) ms, % lost) for ``count`` pings."""
    rc, outp, _ = nb.exec(f"ping -c {count} -i {interval} -W 1 -q {dest}", timeout=count * interval + 30)
    avg = mdev = None
    loss = 100.0
    for ln in outp.splitlines():
        if "packet loss" in ln:
            try:
                loss = float(ln.split("%")[0].split()[-1])
            except (ValueError, IndexError):
                pass
        if ln.startswith(("rtt", "round-trip")):
            try:
                vals = ln.split("=")[1].split()[0].split("/")
                avg, mdev = float(vals[1]), float(vals[3])
            except (IndexError, ValueError):
                pass
    return avg, mdev, loss


def _network(nb: Box, peer: str, net: str, secs: int, record: Callable[[Check], None]) -> None:
    peer_ip = run_cmd(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                       peer]).out.strip()
    gateway = run_cmd(["docker", "network", "inspect", "-f", "{{(index .IPAM.Config 0).Gateway}}",
                       net]).out.strip()
    time.sleep(1.0)                                     # the peer's servers start

    nb.apply(net_rate="10mbit")
    rc, outp, _ = nb.exec(f"iperf3 -c {peer} -t {secs} -f m | grep receiver", timeout=60)
    try:
        mbit = float(outp.split("Mbits/sec")[0].split()[-1])
    except (ValueError, IndexError):
        mbit = None
    record(Check("net.rate", mbit is not None and abs(mbit - 10) <= 2.0, "8–12 Mbit/s",
                 "iperf3 failed" if mbit is None else f"{mbit:.2f} Mbit/s", f"iperf3 to a peer container for {secs} s",
                 label="net.rate=10mbit", measured=mbit, unit="Mbit/s"))
    nb.apply(net_rate="max", net_delay="50ms")
    avg, _, _ = _ping(nb, peer, 5)
    record(Check("net.delay", avg is not None and 45 <= avg <= 80, "45–80 ms",
                 "no reply" if avg is None else f"{avg:.1f} ms", "5 pings to the peer (average round trip)",
                 label="net.delay=50ms", measured=avg, unit="ms"))
    nb.apply(net_jitter="20ms")
    _, mdev, _ = _ping(nb, peer, 20)
    record(Check("net.jitter", mdev is not None and 5 <= mdev <= 25, "±5–25 ms",
                 "no reply" if mdev is None else f"±{mdev:.1f} ms", "20 pings under net.delay=50ms (ping's mdev)",
                 label="net.jitter=20ms", measured=mdev, unit="ms"))
    nb.apply(net_delay="0ms", net_jitter="0ms", net_loss="30%")
    _, _, loss = _ping(nb, peer, 100, 0.02)
    record(Check("net.loss", abs(loss - 30) <= 12, "18–42%", f"{loss:.0f}%", "100 pings to the peer",
                 label="net.loss=30%", measured=loss, unit="%"))
    nb.apply(net_loss="0%", net_partition="reject")
    rc, _, dt = nb.exec(f"curl -s -m 5 http://{peer}/ -o /dev/null")
    record(Check("net.partition", rc == 7 and dt < 1.5, "rejected within 1.5 s",
                 f"rejected in {dt:.2f} s" if rc == 7 else f"curl exit {rc} after {dt:.1f} s", "curl to the peer",
                 label="net.partition=reject", measured=dt, unit="s"))
    # Allowed destinations bypass both the partition and the netem delay; others stay blocked.
    nb.apply(net_delay="50ms", net_allow=[f"{peer_ip}/32"])
    rc, _, _ = nb.exec(f"curl -s -m 5 http://{peer}/ -o /dev/null")
    avg, _, _ = _ping(nb, peer, 3)
    _, _, gw_loss = _ping(nb, gateway, 2) if gateway else (None, None, 100.0)
    problems = (["peer blocked"] if rc != 0 or avg is None else [f"peer delayed {avg:.0f} ms"] if avg >= 10 else []) + (
        [] if gw_loss == 100 else ["other traffic allowed"])
    record(Check("net.allow", not problems, "only peer, undelayed",
                 "; ".join(problems) or f"only peer, {avg:.1f} ms",
                 "curl and ping the peer, ping the gateway; net.partition=reject and net.delay=50ms",
                 label=f"net.allow={peer_ip}", measured=avg, unit="ms"))
    nb.apply(net_partition="none", net_allow=[], net_delay="0ms")


__all__ = ["selftest"]
