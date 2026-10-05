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


# Column widths of the result tables.
W_LABEL, W_WORKLOAD, W_RESULT = 22, 34, 29


def row(mark: str, label: str, workload: str, result: str, pass_if: str = "") -> str:
    return f"  {mark:<5} {label:<{W_LABEL}} {workload:<{W_WORKLOAD}} {result:<{W_RESULT}} {pass_if}".rstrip()


def header(first: str, result: str) -> str:
    return row("", first, "workload", result, "pass if")


def _round(x: Any) -> Any:
    if isinstance(x, float):
        return round(x, 3) if abs(x) < 1e4 else int(round(x))
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    return x


@dataclass
class Check:
    """One selftest line, and its entry in capabilities.json."""
    name: str                 # the key in capabilities.json: a knob, or a fidelity measurement
    ok: bool | None           # None: not tested
    workload: str             # what ran in the container
    result: str               # what happened
    pass_if: str = ""         # the pass condition
    label: str = ""           # the line's first column; defaults to name
    expected: Any = None
    measured: Any = None
    unit: str | None = None

    @property
    def detail(self) -> str:
        return f"{self.workload}: {self.result}" + (f" (pass if {self.pass_if})" if self.pass_if else "")

    def as_dict(self) -> dict:
        d = {"ok": self.ok, "workload": self.workload, "result": self.result, "pass_if": self.pass_if,
             "detail": self.detail}
        for k in ("expected", "measured", "unit"):
            if getattr(self, k) is not None:
                d[k] = _round(getattr(self, k))
        return d

    def line(self) -> str:
        mark = {True: "ok", False: "FAIL", None: "skip"}[self.ok]
        return row(mark, self.label or self.name, self.workload, self.result, self.pass_if)


def _counts(results: dict) -> tuple[int, list[str], list[str]]:
    """(passed, failed names, untested names)."""
    ok = [k for k, v in results.items() if v.get("ok") is True]
    bad = [k for k, v in results.items() if v.get("ok") is False]
    return len(ok), bad, [k for k, v in results.items() if v.get("ok") is None]


def _summary(what: str, noun: str, results: dict, kept_from: str | None) -> str:
    n, bad, untested = _counts(results)
    s = f"{n} of {len(results)} {noun}"
    if bad:
        s += f"; failed: {', '.join(bad)}"
    if untested:
        s += f"; not tested: {', '.join(untested)}"
    if kept_from:
        s += f" (kept from the selftest at {kept_from})"
    return f"  {what:<12} {s}"


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
             echo: Callable[[str], None] = print, only: str | None = None) -> tuple[dict, bool]:
    """Run the checks and write capabilities.json; returns (capabilities, all passed).

    ``only`` runs one part ("enforcement" or "fidelity") and keeps the other part's results
    from an existing capabilities.json.
    """
    if only is not None and only not in PARTS:
        raise ValueError(f"--only must be one of {', '.join(PARTS)}")
    if not run_cmd(["docker", "image", "inspect", image], timeout=20).ok:
        echo(f"image {image} not found: build it with `docker build -t {image} images/testbox`")
        return {}, False
    path = Path(out) if out else state_dir() / "capabilities.json"
    try:
        previous = json.loads(path.read_text()) if only else {}
    except (OSError, ValueError):
        previous = {}
    knobs, features = previous.get("knobs", {}), previous.get("features", {})
    fid = previous.get("fidelity", {})
    echo(f"rprof selftest · rprof {__version__} · {socket.gethostname()} · kernel {platform.release()}")
    if only in (None, "enforcement"):
        knobs, features = _enforcement(quick, image, echo)
    if only in (None, "fidelity"):
        from .fidelity import run_fidelity
        fid = run_fidelity(image, echo)
    caps = {"rprof_version": __version__, "at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "host": {"hostname": socket.gethostname(), "kernel": platform.release()},
            "quick": quick, "knobs": knobs, "features": features, "fidelity": fid}
    write_json(path, caps)
    kept = previous.get("at")
    echo("")
    echo("Summary")
    echo(_summary("enforcement", "limits hold", knobs, kept if only == "fidelity" else None))
    echo(_summary("fidelity", "measurements correct", fid, kept if only == "enforcement" else None))
    echo(f"  Saved to {path}. `rprof run` refuses limits that failed here (unless")
    echo("  --allow-degraded) and warns if a measurement failed.")
    ok = all(v["ok"] is not False for v in knobs.values()) and all(v["ok"] is not False for v in fid.values())
    return caps, ok


def _enforcement(quick: bool, image: str, echo: Callable[[str], None]) -> tuple[dict, dict]:
    tag = uuid.uuid4().hex[:6]
    secs = 3 if quick else 8
    knobs: dict[str, dict] = {}
    features: dict[str, Any] = {}
    swap_total = 0

    def record(c: Check) -> None:
        knobs[c.name] = c.as_dict()
        echo(c.line())

    echo("")
    echo("Enforcement: does the kernel hold each limit?")
    echo(f"  rprof sets each limit on a scratch container (image {image}) with the same code")
    echo("  `rprof run` uses, then runs a workload that needs more than the limit and checks the")
    echo("  container's cgroup counters, or times the workload. Nothing is recorded in this part.")
    echo("")
    echo(header("limit", "result"))

    box = Box(image, f"rprof-selftest-{tag}", data_mb=256)
    net = f"rprof-selftest-{tag}"
    peer = None
    try:
        # ---- CPU
        stress2 = f"stress-ng --cpu 2 --timeout {secs}s"
        box.apply(cpu_cores=0.5)
        c, _ = _cpu(box, stress2, secs)
        record(Check("cpu.cores", abs(c - 0.5) <= 0.1, f"stress-ng --cpu 2 for {secs} s", f"{c:.2f} cores",
                     "0.40–0.60 cores", label="cpu.cores=0.5", expected=0.5, measured=c, unit="cores"))
        box.apply(cpu_period="20ms")
        c, per = _cpu(box, stress2, secs)
        record(Check("cpu.period", 40 <= per <= 60 and abs(c - 0.5) <= 0.1, "stress-ng --cpu 2, cpu.cores=0.5",
                     f"{per:.0f} periods/s, {c:.2f} cores", "40–60 periods/s", label="cpu.period=20ms",
                     expected=50, measured=per, unit="periods/s"))
        box.apply(cpu_cores="max", cpu_period="100ms")
        if box.target.cgroup.has("cpuset.cpus"):
            first = read_text(box.target.cgroup.file("cpuset.cpus.effective")).strip().split(",")[0].split("-")[0]
            box.apply(cpu_cpus=first)
            c, _ = _cpu(box, f"stress-ng --cpu 4 --timeout {secs}s", secs)
            record(Check("cpu.cpus", c <= 1.1, f"stress-ng --cpu 4 for {secs} s", f"{c:.2f} cores",
                         "at most 1.10 cores", label=f"cpu.cpus={first}", expected=1.0, measured=c, unit="cores"))
            box.apply(cpu_cpus="all")
        else:
            record(Check("cpu.cpus", False, "-", "cpuset controller not enabled for containers"))

        # ---- memory
        ev0 = box.stat("memory.events")
        box.apply(mem_max="128Mi", mem_swap_max=0)
        rc, _, _ = box.exec("hog-mem 256M 2")
        ev1 = box.stat("memory.events")
        kills = ev1.get("oom_kill", 0) - ev0.get("oom_kill", 0)
        alive = run_cmd(["docker", "inspect", "-f", "{{.State.Running}}", box.name]).out.strip() == "true"
        record(Check("mem.max", rc == 137 and kills > 0 and alive, "hog-mem 256M",
                     f"exit {rc}, {kills} OOM kill{'s' if kills != 1 else ''}" + ("" if alive else ", container died"),
                     "OOM-killed; the container survives", label="mem.max=128Mi", measured=kills, unit="oom_kills"))
        box.apply(mem_max="max")
        box.apply(mem_high="128Mi")
        ev0 = box.stat("memory.events")
        rc, _, dt = box.exec("timeout 4 hog-mem 160M 2; echo rc=$?")
        ev1 = box.stat("memory.events")
        hi = ev1.get("high", 0) - ev0.get("high", 0)
        record(Check("mem.high", hi > 0, "hog-mem 160M (stopped after 4 s)", f"throttled {hi}× in {dt:.1f} s",
                     "throttled (memory.events high)", label="mem.high=128Mi", measured=hi, unit="high_events"))
        box.apply(mem_high="max")
        try:
            swap_total = int([ln for ln in Path("/proc/meminfo").read_text().splitlines()
                              if ln.startswith("SwapTotal")][0].split()[1]) * 1024
        except (OSError, IndexError, ValueError):
            pass
        if not box.target.cgroup.has("memory.swap.max"):
            record(Check("mem.swap_max", False, "-", "no memory.swap.max (swap accounting off)",
                         label="mem.swap_max=128Mi"))
        elif swap_total < (256 << 20):
            record(Check("mem.swap_max", None, "-", f"host swap {swap_total >> 20} MiB, need 256",
                         label="mem.swap_max=128Mi"))
        else:
            box.apply(mem_max="96Mi", mem_swap_max="128Mi")
            rc, _, _ = box.exec("hog-mem 160M 2")
            record(Check("mem.swap_max", rc == 0, "hog-mem 160M, mem.max=96Mi",
                         "finished (exit 0)" if rc == 0 else f"exit {rc}", "finishes, using swap",
                         label="mem.swap_max=128Mi"))
            box.apply(mem_max="max", mem_swap_max=0)

        # ---- pids
        box.apply(pids_max=20)
        p0 = box.stat("pids.events").get("max", 0)
        box.exec("for i in $(seq 50); do sleep 5 & done 2>/dev/null; sleep 0.5", timeout=30)
        n = box.stat("pids.events").get("max", 0) - p0
        record(Check("pids.max", n > 0, "start 50 background sleeps", f"{n} fork{'s' if n != 1 else ''} refused",
                     "a fork is refused", label="pids.max=20", measured=n, unit="refused_forks"))
        box.exec("pkill sleep || true")
        box.apply(pids_max="max")

        # ---- io: each limit in turn, timing direct I/O that needs longer than the limit allows
        mb, n = (40, 400) if quick else (100, 1000)
        dev = box.target.io_device
        io_tests = [
            ("io.wbps", "20Mi", f"write {mb} MiB to {dev}, direct",
             f"dd if=/dev/zero of=/var/tmp/rprof-bw bs=1M count={mb} oflag=direct", mb / 20),
            ("io.rbps", "20Mi", f"read {mb} MiB from {dev}, direct",
             "dd if=/var/tmp/rprof-bw of=/dev/null bs=1M iflag=direct", mb / 20),
            ("io.wiops", 200, f"{n} 4 KiB writes, direct",
             f"dd if=/dev/zero of=/var/tmp/rprof-iops bs=4k count={n} oflag=direct", n / 200),
            ("io.riops", 200, f"{n} 4 KiB reads, direct",
             "dd if=/var/tmp/rprof-iops of=/dev/null bs=4k iflag=direct", n / 200),
        ]
        if not dev:
            for k, v, *_ in io_tests:
                record(Check(k, False, "-", "no block device found for the container", label=f"{k}={v}"))
        else:
            for k, v, workload, cmd, want in io_tests:
                errs = box.apply(**{k.replace(".", "_"): v})
                rc, outp, dt = box.exec(f"{cmd} 2>&1", timeout=120)
                box.apply(**{k.replace(".", "_"): "max"})
                ok = not errs and rc == 0 and abs(dt - want) <= 0.3 * want
                result = f"{dt:.1f} s" if rc == 0 else f"dd failed: {outp.strip()[-60:]}"
                record(Check(k, ok, workload, result + (f"; {'; '.join(errs)}" if errs else ""),
                             f"{0.7 * want:.1f}–{1.3 * want:.1f} s", label=f"{k}={v}", expected=want, measured=dt,
                             unit="s"))
            box.exec("rm -f /var/tmp/rprof-bw /var/tmp/rprof-iops")
            # Not a knob: whether io.max also slows buffered writes, via cgroup writeback.
            box.apply(io_wbps="20Mi")
            rc, _, dt = box.exec(f"dd if=/dev/zero of=/var/tmp/rprof-dd bs=1M count={mb} 2>/dev/null; sync; "
                                 "rm -f /var/tmp/rprof-dd", timeout=120)
            box.apply(io_wbps="max")
            features["io_buffered_writes_throttled"] = dt >= 0.6 * mb / 20
            echo(row("info", "buffered writes", f"write {mb} MiB + sync, buffered", f"{dt:.1f} s",
                     "io.wbps=20Mi slows buffered writes too" if features["io_buffered_writes_throttled"]
                     else "io limits slow direct I/O only on this host"))

        # ---- disk
        cap = 100
        errs = box.apply(disk_capacity=f"{cap}Mi")
        rc, outp, _ = box.exec("dd if=/dev/zero of=/data/fill bs=1M count=200 2>&1; df -m /data | tail -1; "
                               "ls -l /data/fill | awk '{print $5}'")
        last = outp.strip().split()[-1] if outp.strip() else ""
        written = (int(last) if last.isdigit() else 0) / 2**20
        enospc = "No space" in outp
        result = f"ENOSPC after {written:.0f} MiB" if enospc else f"no ENOSPC; wrote {written:.0f} MiB"
        record(Check("disk.capacity", enospc and abs(written - cap) <= 0.15 * cap and not errs,
                     "write 200 MiB to /data", result + (f"; {errs}" if errs else ""),
                     f"ENOSPC after {0.85 * cap:.0f}–{1.15 * cap:.0f} MiB", label=f"disk.capacity={cap}Mi",
                     expected=cap, measured=written, unit="MiB"))
        box.exec("rm -f /data/fill")
        box.apply(disk_capacity="max")
    finally:
        box.close()

    # ---- network: a second container and a peer on their own Docker network
    net_labels = {"net.rate": "net.rate=10mbit", "net.delay": "net.delay=50ms", "net.jitter": "net.jitter=20ms",
                  "net.loss": "net.loss=30%", "net.partition": "net.partition=reject", "net.allow": "net.allow=<peer>"}
    try:
        run_cmd(["docker", "network", "create", net], timeout=30)
        peer_name = f"rprof-selftest-peer-{tag}"
        if not run_cmd(["docker", "image", "inspect", "rprof-netpeer"], timeout=20).ok:
            for k, label in net_labels.items():
                record(Check(k, None, "-", "rprof-netpeer image missing", "build images/netpeer", label=label))
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


def _ms(x: float | None) -> str:
    return "no reply" if x is None else f"{x:.1f} ms"


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
        mbit = -1
    record(Check("net.rate", abs(mbit - 10) <= 2.0, f"iperf3 to the peer for {secs} s",
                 f"{mbit:.2f} Mbit/s" if mbit >= 0 else "iperf3 failed", "8–12 Mbit/s", label="net.rate=10mbit",
                 expected=10, measured=mbit, unit="Mbit/s"))
    nb.apply(net_rate="max", net_delay="50ms")
    avg, _, _ = _ping(nb, peer, 5)
    record(Check("net.delay", avg is not None and 45 <= avg <= 80, "5 pings to the peer", f"{_ms(avg)} average",
                 "45–80 ms", label="net.delay=50ms", expected=50, measured=avg, unit="ms"))
    nb.apply(net_jitter="20ms")
    avg, mdev, _ = _ping(nb, peer, 20)
    record(Check("net.jitter", mdev is not None and 5 <= mdev <= 25, "20 pings, net.delay=50ms",
                 "no reply" if mdev is None else f"varies by ±{mdev:.1f} ms (mdev)", "±5–25 ms",
                 label="net.jitter=20ms", measured=mdev, unit="ms"))
    nb.apply(net_delay="0ms", net_jitter="0ms", net_loss="30%")
    _, _, loss = _ping(nb, peer, 100, 0.02)
    record(Check("net.loss", abs(loss - 30) <= 12, "100 pings to the peer", f"{loss:.0f}% lost", "18–42% lost",
                 label="net.loss=30%", expected=30, measured=loss, unit="%"))
    nb.apply(net_loss="0%", net_partition="reject")
    rc, _, dt = nb.exec(f"curl -s -m 5 http://{peer}/ -o /dev/null")
    record(Check("net.partition", rc == 7 and dt < 1.5, "curl to the peer",
                 f"refused in {dt:.2f} s" if rc == 7 else f"curl exit {rc} after {dt:.1f} s",
                 "refused within 1.5 s", label="net.partition=reject", measured=dt, unit="s"))
    # Allowed destinations bypass both the partition and the netem delay; others stay blocked.
    nb.apply(net_delay="50ms", net_allow=[f"{peer_ip}/32"])
    rc, _, _ = nb.exec(f"curl -s -m 5 http://{peer}/ -o /dev/null")
    avg, _, _ = _ping(nb, peer, 3)
    _, _, gw_loss = _ping(nb, gateway, 2) if gateway else (None, None, 100.0)
    ok = rc == 0 and avg is not None and avg < 10 and gw_loss == 100
    result = (f"peer {_ms(avg)}" + ("" if rc == 0 else f", curl exit {rc}")
              + ("; gateway blocked" if gw_loss == 100 else "; gateway reachable"))
    record(Check("net.allow", ok, "pings, reject + 50ms delay", result,
                 "peer undelayed; gateway blocked", label=f"net.allow={peer_ip}", measured=avg, unit="ms"))
    nb.apply(net_partition="none", net_allow=[], net_delay="0ms")


__all__ = ["selftest"]
