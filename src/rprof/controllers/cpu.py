"""CPU: cpu.cores and cpu.period -> cpu.max; cpu.cpus -> cpuset.cpus."""

from __future__ import annotations

from typing import Any

import errno
from pathlib import Path

from ..target.cgroup import pick_flat, read_text, write_text
from .base import Controller, FileCache

CPU_STAT = ("usage_usec", "user_usec", "system_usec", "nr_periods", "nr_throttled", "throttled_usec")


def cpu_max_value(cores: float | None, period_ms: float) -> str:
    period_us = int(round(period_ms * 1000))
    if cores is None:
        return f"max {period_us}"
    return f"{max(1000, int(round(cores * period_us)))} {period_us}"


def write_cpuset_all(cg: Path, errs: list[str] | None = None, ctrl: Controller | None = None) -> str:
    """Make cpuset.cpus unrestricted: empty (inherit) if the kernel allows it, else the parent's CPUs.

    A populated cgroup cannot go back to an empty cpuset.cpus on current kernels (ENOSPC), so the
    fallback writes the parent's effective set: the same CPUs are allowed, the file just is not empty.
    Returns the value written.
    """
    f = cg / "cpuset.cpus"
    if ctrl is not None and ctrl.snap is not None:
        ctrl.snap.record_file(f)
    try:
        write_text(f, "")
        return ""
    except OSError as e:
        if e.errno != errno.ENOSPC:
            if errs is not None:
                errs.append(f"cpuset.cpus='': {e.strerror}")
            return ""
    parent = read_text(cg.parent / "cpuset.cpus.effective").strip()
    try:
        write_text(f, parent)
    except OSError as e:
        if errs is not None:
            errs.append(f"cpuset.cpus={parent!r}: {e.strerror}")
    return parent


class CpuController(Controller):
    name = "cpu"
    knobs = ("cpu.cores", "cpu.period", "cpu.cpus")

    def capabilities(self):
        cg = self.target.cgroup
        out = {}
        for k in ("cpu.cores", "cpu.period"):
            out[k] = None if cg.has("cpu.max") else "cpu controller not enabled for the target (no cpu.max)"
        out["cpu.cpus"] = None if cg.has("cpuset.cpus") else "cpuset controller not enabled for the target"
        return out

    def files_for(self, knobs):
        knobs = set(knobs)
        cg = self.target.cgroup
        out = []
        if knobs & {"cpu.cores", "cpu.period"} and cg.has("cpu.max"):
            out.append(cg.file("cpu.max"))
        if "cpu.cpus" in knobs and cg.has("cpuset.cpus"):
            out.append(cg.file("cpuset.cpus"))
        return out

    def apply(self, limits: dict[str, Any], changed: set[str]) -> list[str]:
        errs: list[str] = []
        cg = self.target.cgroup
        if changed & {"cpu.cores", "cpu.period"}:
            self.write(cg.file("cpu.max"), cpu_max_value(limits["cpu.cores"], limits["cpu.period"]), errs)
        if "cpu.cpus" in changed:
            want = limits["cpu.cpus"]
            if want is None:
                write_cpuset_all(cg.path, errs, self)
            else:
                self.write(cg.file("cpuset.cpus"), want, errs)
        return errs

    def sample(self, out: dict, fc: FileCache) -> None:
        txt = fc.read(self.target.cgroup.fstr("cpu.stat"))
        if txt is None:
            return
        out["cpu"] = pick_flat(txt, CPU_STAT)
