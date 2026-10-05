"""``rprof doctor``: cgroup v2, controllers, tools and kernel features; ``--deep`` adds probes."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .target import docker
from .target.cgroup import (REQUIRED_CONTROLLERS, Cgroup, cgroup_root, is_cgroup2, parse_flat, read_text,
                            write_text)
from .util import is_root, run_cmd


@dataclass
class Check:
    name: str
    ok: bool | None          # None = warning / informational
    detail: str = ""


def _kernel_ge(major: int, minor: int) -> bool:
    try:
        rel = os.uname().release.split("-")[0].split(".")
        return (int(rel[0]), int(rel[1])) >= (major, minor)
    except (ValueError, IndexError):
        return False


def checks(target=None, deep: bool = False) -> list[Check]:
    out: list[Check] = []
    root = cgroup_root()
    v2 = is_cgroup2()
    out.append(Check("cgroup v2 mounted at " + str(root), v2,
                     "unified hierarchy" if v2 else "not cgroup2 (v1/hybrid hosts are not supported)"))
    out.append(Check("running as root", is_root() or None, "" if is_root() else
                     "not root: apply/run/selftest need sudo"))
    if v2:
        avail = set(read_text(root / "cgroup.controllers").split())
        enabled = set(read_text(root / "cgroup.subtree_control").split())
        for c in REQUIRED_CONTROLLERS:
            out.append(Check(f"controller {c}", c in avail and (c in enabled or None),
                             "enabled at root" if c in enabled else
                             ("available, not enabled in root subtree_control (rprof enables it on demand)"
                              if c in avail else "not available in this kernel")))
    for tool in ("tc", "iptables", "ip6tables", "nsenter", "ip", "docker"):
        p = shutil.which(tool)
        out.append(Check(f"tool {tool}", bool(p) if tool not in ("ip6tables",) else (bool(p) or None),
                         p or "not found"))
    psi = Path("/proc/pressure/cpu").exists()
    out.append(Check("PSI (pressure stall information)", psi or None,
                     "available" if psi else "missing: pressure-based binding evidence unavailable"))
    out.append(Check("memory.peak", _kernel_ge(5, 19) or None,
                     "kernel >= 5.19" if _kernel_ge(5, 19) else "kernel < 5.19"))
    out.append(Check("memory.peak per-fd reset", _kernel_ge(6, 12) or None,
                     "kernel >= 6.12: per-tick peaks recorded" if _kernel_ge(6, 12) else
                     f"kernel {os.uname().release} < 6.12: mem.peak omitted, spikes between samples can be missed"))
    try:
        swap = [ln for ln in Path("/proc/meminfo").read_text().splitlines() if ln.startswith("SwapTotal")]
        swap_kb = int(swap[0].split()[1]) if swap else 0
    except OSError:
        swap_kb = 0
    out.append(Check("swap", None, f"{swap_kb // 1024} MiB configured" + (
        "" if swap_kb else "; mem.swap_max > 0 has no effect")))
    dv = docker.version()
    out.append(Check("docker daemon", bool(dv) or None,
                     f"{dv}, cgroup driver {docker.cgroup_driver()}" if dv else "unreachable (docker: targets unavailable)"))
    if target is not None:
        t = target
        out.append(Check(f"target {t.spec}", t.cgroup.exists(), str(t.cgroup.path)))
        miss = t.cgroup.missing_controllers()
        out.append(Check("target controllers", not miss or None,
                         "all enabled" if not miss else f"missing {', '.join(miss)} (rprof run enables them)"))
        out.append(Check("io device", bool(t.io_device) or None,
                         f"{t.io_device} ({t.io_device_name}) from {t.io_device_source}" if t.io_device else
                         "none resolved: io.* knobs unavailable (use --io-device)"))
        out.append(Check("network", bool(t.net) or None,
                         ", ".join(f"{n.ifname}<->{n.host_veth}" for n in t.net) or "no netns: net.* unavailable"))
        out.append(Check("data filesystem", bool(t.data) or None,
                         f"{t.data.container_path or ''} -> {t.data.host_path} ({t.data.fstype})" if t.data else
                         "none at /data: disk.capacity unavailable"))
    if deep:
        out.extend(deep_probes(target))
    return out


def deep_probes(target=None) -> list[Check]:
    """Quick enforcement probes in a scratch cgroup (needs root)."""
    if not is_root() or not is_cgroup2():
        return [Check("deep probes", None, "skipped: need root and cgroup v2")]
    out: list[Check] = []
    cg = Cgroup(cgroup_root() / "rprof.slice" / f"doctor-{os.getpid()}")
    try:
        cg.path.mkdir(parents=True, exist_ok=True)
        cg.enable_controllers(REQUIRED_CONTROLLERS)
        # pids.max
        write_text(cg.file("pids.max"), "8")
        code = ("import os,sys\nn=0\nfor i in range(50):\n  try:\n    p=os.fork()\n  except OSError:\n    break\n"
                "  if p==0:\n    import time; time.sleep(3); os._exit(0)\n  n+=1\nprint(n)\n")
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                             preexec_fn=lambda: write_text(cg.file("cgroup.procs"), str(os.getpid())))
        n = int((p.communicate(timeout=20)[0] or "0").strip() or 0)
        ev = parse_flat(read_text(cg.file("pids.events"))).get("max", 0)
        out.append(Check("probe pids.max=8", n < 8 and ev > 0, f"forked {n} children, pids.events max={ev}"))
        time.sleep(3.5)
        write_text(cg.file("pids.max"), "max")
        # memory.max
        write_text(cg.file("memory.max"), str(64 << 20))
        write_text(cg.file("memory.swap.max"), "0") if cg.has("memory.swap.max") else None
        p = subprocess.run([sys.executable, "-c", "b=bytearray(256<<20)\nfor i in range(0,len(b),4096): b[i]=1"],
                           preexec_fn=lambda: write_text(cg.file("cgroup.procs"), str(os.getpid())), timeout=30)
        ev = parse_flat(read_text(cg.file("memory.events")))
        out.append(Check("probe memory.max=64Mi", p.returncode == -9 and ev.get("oom_kill", 0) > 0,
                         f"exit {p.returncode}, oom_kill={ev.get('oom_kill')}"))
        write_text(cg.file("memory.max"), "max")
        # cpu.max
        write_text(cg.file("cpu.max"), "20000 100000")
        t0 = parse_flat(read_text(cg.file("cpu.stat")))
        w0 = time.monotonic()
        subprocess.run([sys.executable, "-c", "import time\nt=time.time()\nwhile time.time()-t<1.5: pass"],
                       preexec_fn=lambda: write_text(cg.file("cgroup.procs"), str(os.getpid())), timeout=30)
        t1 = parse_flat(read_text(cg.file("cpu.stat")))
        cores = (t1["usage_usec"] - t0["usage_usec"]) / 1e6 / (time.monotonic() - w0)
        out.append(Check("probe cpu.max=0.2 cores", 0.1 <= cores <= 0.3 and t1["nr_throttled"] > t0["nr_throttled"],
                         f"{cores:.2f} cores, throttled {t1['nr_throttled'] - t0['nr_throttled']} periods"))
        # io.max accepted for the target device
        if target is not None and target.io_device:
            try:
                write_text(cg.file("io.max"), f"{target.io_device} wbps=10485760")
                write_text(cg.file("io.max"), f"{target.io_device} wbps=max")
                out.append(Check(f"probe io.max on {target.io_device}", True, "accepted"))
            except OSError as e:
                out.append(Check(f"probe io.max on {target.io_device}", False, e.strerror or str(e)))
        r = run_cmd(["tc", "qdisc", "show", "dev", "lo"])
        out.append(Check("probe tc", r.ok, r.err or "ok"))
    except Exception as e:  # noqa: BLE001
        out.append(Check("deep probes", False, f"{type(e).__name__}: {e}"))
    finally:
        for _ in range(20):
            try:
                os.rmdir(cg.path)
                break
            except OSError:
                time.sleep(0.25)
    return out
