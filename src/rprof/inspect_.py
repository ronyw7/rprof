"""``rprof inspect``: resolved paths, PIDs, devices and current limits."""

from __future__ import annotations

from pathlib import Path

from .protect import cmdline
from .target import Target
from .target.cgroup import read_text
from .util import run_cmd

LIMIT_FILES = ("cpu.max", "cpuset.cpus", "cpuset.cpus.effective", "memory.high", "memory.max",
               "memory.swap.max", "io.max", "pids.max", "memory.oom.group")


def current_limits(t: Target) -> dict[str, str | None]:
    out = {}
    for f in LIMIT_FILES:
        try:
            out[f] = read_text(t.cgroup.file(f)).strip()
        except OSError:
            out[f] = None
    return out


def qdiscs(t: Target) -> dict[str, str]:
    out = {}
    for nt in t.net:
        r = run_cmd(nt.nsenter("tc", "qdisc", "show", "dev", nt.ifname), quiet=True)
        out[f"{nt.spec} {nt.ifname} (container)"] = r.out.strip() if r.ok else r.err
        if nt.host_veth:
            r = run_cmd(["tc", "qdisc", "show", "dev", nt.host_veth], quiet=True)
            out[f"{nt.spec} {nt.host_veth} (host)"] = r.out.strip() if r.ok else r.err
    return out


def inspect(t: Target) -> dict:
    pids = t.pids()
    procs = []
    for pid in pids[:200]:
        try:
            adj = Path(f"/proc/{pid}/oom_score_adj").read_text().strip()
        except OSError:
            adj = None
        procs.append({"pid": pid, "oom_score_adj": adj, "cmd": cmdline(pid)[:120]})
    d = t.to_meta()
    d["controllers"] = sorted(t.cgroup.controllers())
    d["missing_controllers"] = t.cgroup.missing_controllers()
    d["limits"] = current_limits(t)
    d["processes"] = procs
    d["process_count"] = len(pids)
    d["qdiscs"] = qdiscs(t)
    return d
