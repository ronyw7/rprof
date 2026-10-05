"""``rprof selftest``: host fidelity and enforcement checks -> ``capabilities.json``.

Checks run against a scratch ``rprof-testbox`` container (and an ``rprof-netpeer``
for network knobs) through rprof's own controllers, so they exercise the same code
path as ``run``. Every run copies capabilities.json into its meta.json, and ``run``
refuses knobs that failed here unless given ``--allow-degraded``.
"""

from __future__ import annotations

import datetime as _dt
import platform
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import controllers as C
from . import knobs as K
from .snapshot import Snapshot, restore, state_dir
from .target import resolve
from .target.cgroup import parse_flat, read_text
from .util import run_cmd, write_json


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


def _cores(box: Box, cmd: str, secs: float) -> float:
    a = box.stat("cpu.stat")["usage_usec"]
    t0 = time.monotonic()
    box.exec(cmd, timeout=secs + 30)
    return (box.stat("cpu.stat")["usage_usec"] - a) / 1e6 / (time.monotonic() - t0)


def selftest(quick: bool = True, out: Path | None = None, image: str = "rprof-testbox",
             echo: Callable[[str], None] = print) -> tuple[dict, bool]:
    tag = uuid.uuid4().hex[:6]
    secs = 3 if quick else 8
    knobs: dict[str, dict] = {}
    features: dict[str, Any] = {}
    swap_total = 0

    def record(names, ok, detail):
        for n in names if isinstance(names, (list, tuple)) else [names]:
            knobs[n] = {"ok": ok, "detail": detail}
        echo(f"{'ok  ' if ok else ('FAIL' if ok is False else 'skip')}  {', '.join(names) if isinstance(names, (list, tuple)) else names:<36} {detail}")

    if not run_cmd(["docker", "image", "inspect", image], timeout=20).ok:
        echo(f"image {image} not found: build it with `docker build -t {image} images/testbox`")
        return {}, False

    box = Box(image, f"rprof-selftest-{tag}", data_mb=256)
    net = f"rprof-selftest-{tag}"
    peer = None
    try:
        # ---- CPU
        box.apply(cpu_cores=0.5)
        c = _cores(box, f"stress-ng --cpu 2 --timeout {secs}s", secs)
        record(["cpu.cores", "cpu.period"], abs(c - 0.5) <= 0.1, f"stress-ng --cpu 2 under 0.5 cores: {c:.2f} cores")
        box.apply(cpu_cores="max")
        if box.target.cgroup.has("cpuset.cpus"):
            first = read_text(box.target.cgroup.file("cpuset.cpus.effective")).strip().split(",")[0].split("-")[0]
            box.apply(cpu_cpus=first)
            c = _cores(box, f"stress-ng --cpu 4 --timeout {secs}s", secs)
            record("cpu.cpus", c <= 1.1, f"stress-ng --cpu 4 on cpu {first}: {c:.2f} cores")
            box.apply(cpu_cpus="all")
        else:
            record("cpu.cpus", False, "cpuset controller not enabled for containers")

        # ---- memory
        ev0 = box.stat("memory.events")
        box.apply(mem_max="128Mi", mem_swap_max=0)
        rc, _, _ = box.exec("hog-mem 256M 2")
        ev1 = box.stat("memory.events")
        alive = run_cmd(["docker", "inspect", "-f", "{{.State.Running}}", box.name]).out.strip() == "true"
        record("mem.max", rc == 137 and ev1.get("oom_kill", 0) > ev0.get("oom_kill", 0) and alive,
               f"hog-mem 256M under 128Mi: exit {rc}, oom_kill +{ev1.get('oom_kill', 0) - ev0.get('oom_kill', 0)}, "
               f"container {'alive' if alive else 'dead'}")
        box.apply(mem_max="max")
        box.apply(mem_high="128Mi")
        ev0 = box.stat("memory.events")
        rc, _, dt = box.exec("timeout 4 hog-mem 160M 2; echo rc=$?")
        ev1 = box.stat("memory.events")
        hi = ev1.get("high", 0) - ev0.get("high", 0)
        record("mem.high", hi > 0, f"hog-mem 160M over 128Mi soft: high events +{hi}, took {dt:.1f} s")
        box.apply(mem_high="max")
        try:
            swap_total = int([ln for ln in Path("/proc/meminfo").read_text().splitlines()
                              if ln.startswith("SwapTotal")][0].split()[1]) * 1024
        except (OSError, IndexError, ValueError):
            pass
        if not box.target.cgroup.has("memory.swap.max"):
            record("mem.swap_max", False, "no memory.swap.max (swap accounting off)")
        elif swap_total < (256 << 20):
            record("mem.swap_max", True, f"memory.swap.max writable; host swap {swap_total >> 20} MiB is too small to test")
        else:
            box.apply(mem_max="96Mi", mem_swap_max="128Mi")
            rc, _, _ = box.exec("hog-mem 160M 2")
            record("mem.swap_max", rc == 0, f"hog-mem 160M with 96Mi + 128Mi swap: exit {rc}")
            box.apply(mem_max="max", mem_swap_max=0)

        # ---- pids
        box.apply(pids_max=20)
        p0 = box.stat("pids.events").get("max", 0)
        box.exec("for i in $(seq 50); do sleep 5 & done 2>/dev/null; sleep 0.5", timeout=30)
        p1 = box.stat("pids.events").get("max", 0)
        record("pids.max", p1 > p0, f"50 background sleeps under pids.max=20: max events +{p1 - p0}")
        box.exec("pkill sleep || true")
        box.apply(pids_max="max")

        # ---- io
        mb = 40 if quick else 100
        if not box.target.io_device:
            record(["io.rbps", "io.wbps", "io.riops", "io.wiops"], False, "no block device resolved")
        else:
            errs = box.apply(io_wbps="20Mi")
            if errs:
                record(["io.rbps", "io.wbps", "io.riops", "io.wiops"], False, "; ".join(errs))
            else:
                rc, outp, dt = box.exec(f"dd if=/dev/zero of=/var/tmp/rprof-dd bs=1M count={mb} oflag=direct 2>&1; "
                                        "rm -f /var/tmp/rprof-dd", timeout=120)
                want = mb / 20
                ok = rc == 0 and abs(dt - want) <= 0.3 * want
                record(["io.wbps", "io.rbps", "io.riops", "io.wiops"], ok,
                       f"dd {mb} MiB direct at 20Mi/s on {box.target.io_device}: {dt:.1f} s (expect {want:.1f})"
                       + ("" if rc == 0 else f"; dd failed: {outp[-200:]}"))
                rc, _, dt = box.exec(f"dd if=/dev/zero of=/var/tmp/rprof-dd bs=1M count={mb} 2>/dev/null; sync; "
                                     "rm -f /var/tmp/rprof-dd", timeout=120)
                features["io_buffered_writes_throttled"] = dt >= 0.6 * want
                echo(f"info  buffered writes throttled            {dt:.1f} s for {mb} MiB "
                     f"({'yes' if features['io_buffered_writes_throttled'] else 'no'}: cgroup writeback)")
            box.apply(io_wbps="max")

        # ---- disk
        cap = 100
        errs = box.apply(disk_capacity=f"{cap}Mi")
        rc, outp, _ = box.exec("dd if=/dev/zero of=/data/fill bs=1M count=200 2>&1; df -m /data | tail -1; "
                               "ls -l /data/fill | awk '{print $5}'")
        written = int(outp.strip().split()[-1]) if outp.strip().split()[-1].isdigit() else 0
        ok = "No space" in outp and abs(written / 2**20 - cap) <= 0.15 * cap
        record("disk.capacity", ok and not errs, f"200 MiB into /data with capacity {cap}Mi: "
               f"wrote {written / 2**20:.0f} MiB, {'ENOSPC' if 'No space' in outp else 'no ENOSPC'}"
               + (f"; {errs}" if errs else ""))
        box.exec("rm -f /data/fill")
        box.apply(disk_capacity="max")
    finally:
        box.close()

    # ---- network
    try:
        run_cmd(["docker", "network", "create", net], timeout=30)
        peer_name = f"rprof-selftest-peer-{tag}"
        if not run_cmd(["docker", "image", "inspect", "rprof-netpeer"], timeout=20).ok:
            record(["net.rate", "net.delay", "net.jitter", "net.loss", "net.partition", "net.allow"], None,
                   "rprof-netpeer image missing: build images/netpeer")
        else:
            run_cmd(["docker", "run", "-d", "--name", peer_name, "--network", net, "rprof-netpeer"], timeout=60)
            peer = peer_name
            nb = Box(image, f"rprof-selftest-net-{tag}", network=net)
            try:
                time.sleep(1.0)
                nb.apply(net_rate="10mbit")
                rc, outp, _ = nb.exec(f"iperf3 -c {peer} -t {secs} -f m | grep receiver", timeout=60)
                try:
                    mbit = float(outp.split("Mbits/sec")[0].split()[-1])
                except (ValueError, IndexError):
                    mbit = -1
                record("net.rate", abs(mbit - 10) <= 2.0, f"iperf3 under 10mbit: {mbit:.2f} Mbit/s")
                nb.apply(net_rate="max", net_delay="50ms")
                rc, outp, _ = nb.exec(f"ping -c 5 -i 0.2 -q {peer} | tail -1")
                try:
                    avg = float(outp.split("=")[1].split("/")[1])
                except (IndexError, ValueError):
                    avg = -1
                record(["net.delay", "net.jitter"], 45 <= avg <= 80, f"ping avg rtt with 50ms delay: {avg:.1f} ms")
                nb.apply(net_delay="0ms", net_loss="30%")
                rc, outp, _ = nb.exec(f"ping -c 100 -i 0.02 -q {peer} | grep -o '[0-9.]*% packet loss'")
                try:
                    loss = float(outp.split("%")[0])
                except ValueError:
                    loss = -1
                record("net.loss", abs(loss - 30) <= 12, f"ping x100 under 30% loss: {loss:.0f}% lost")
                nb.apply(net_loss="0%", net_partition="reject")
                rc, outp, dt = nb.exec(f"curl -s -m 5 http://{peer}/ -o /dev/null")
                record("net.partition", rc == 7 and dt < 1.5, f"curl under reject: exit {rc} in {dt:.2f} s")
                nb.apply(net_partition="none")
                rc, _, _ = nb.exec(f"curl -s -m 5 http://{peer}/ -o /dev/null")
                record("net.allow", rc == 0, f"curl after partition removed: exit {rc}")
            finally:
                nb.close()
    finally:
        if peer:
            run_cmd(["docker", "rm", "-f", peer], timeout=30)
        run_cmd(["docker", "network", "rm", net], timeout=30)

    features["psi"] = Path("/proc/pressure/cpu").exists()
    features["swap_bytes"] = swap_total
    caps = {"rprof_version": __version__, "at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "host": {"hostname": socket.gethostname(), "kernel": platform.release()},
            "quick": quick, "knobs": knobs, "features": features}
    path = Path(out) if out else state_dir() / "capabilities.json"
    write_json(path, caps)
    echo(f"wrote {path}")
    ok = all(v["ok"] is not False for v in knobs.values())
    return caps, ok


__all__ = ["selftest"]
