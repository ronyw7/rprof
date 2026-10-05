"""Failure explanations for ``tool_end`` (design: Harness integration)."""

from __future__ import annotations

import re
from typing import Any

from .knobs import describe
from .profile import Profile
from .units import fmt_bytes, fmt_num, fmt_rate_bits

DISK_FULL_BYTES = 1 << 20  # "free bytes reached 0", allowing for the last partial write
OUTPUT_SCAN_CHARS = 65536  # only the end of a call's output is searched

# Out-of-memory errors that programs report themselves, without the kernel killing anything:
# DuckDB ("Out of Memory Error"), Python (MemoryError), C/ENOMEM, C++ (std::bad_alloc), Java.
OOM_OUTPUT = re.compile(r"out of memory|MemoryError|Cannot allocate memory|std::bad_alloc|OutOfMemoryError",
                        re.IGNORECASE)


def match_oom_output(output: str | None) -> str | None:
    """The output line that reports running out of memory, or None."""
    if not output:
        return None
    tail = output[-OUTPUT_SCAN_CHARS:]
    matches = list(OOM_OUTPUT.finditer(tail))
    if not matches:
        return None
    m = matches[-1]
    start = tail.rfind("\n", 0, m.start()) + 1
    end = tail.find("\n", m.end())
    line = tail[start:end if end >= 0 else None].strip()
    return line[:200]


def counters(sample: dict) -> dict[str, float]:
    """The counters failure attribution compares at a call's start and end."""
    g = lambda *ks: _get(sample, ks)  # noqa: E731
    return {
        "oom_kill": g("mem", "events", "oom_kill"),
        "mem_max": g("mem", "events", "max"),
        "pids_max": g("pids", "events_max"),
        "qdisc_drops": g("net", "qdisc_drops"),
        "partition_hits": g("net", "partition_hits"),
        "disk_free": g("disk", "free_bytes"),
        "throttled_usec": g("cpu", "throttled_usec"),
        "psi_cpu": g("psi", "cpu", "some_us"),
        "psi_mem": g("psi", "memory", "some_us"),
        "psi_io": g("psi", "io", "some_us"),
    }


def _get(d: Any, ks: tuple) -> float:
    for k in ks:
        if not isinstance(d, dict) or k not in d:
            return float("nan")
        d = d[k]
    return float(d) if isinstance(d, (int, float)) else float("nan")


def _rose(a: dict, b: dict, k: str) -> bool:
    x, y = a.get(k, float("nan")), b.get(k, float("nan"))
    return x == x and y == y and y > x


def attribute(start: dict, end: dict, min_free: float | None, duration_s: float,
              exit_code: int | None, timed_out: bool, output: str | None = None,
              memory_limited: bool = False) -> tuple[str | None, dict]:
    """(cause, evidence). cause is memory|pids|disk|network|deadline or None.

    An OOM kill is reported even when the call exited 0 (evidence ``exited_ok``): in
    ``a; b`` the killed step's status is hidden by the last command's. ``output`` is matched
    for out-of-memory errors that programs report themselves, but only for failed calls and
    only when ``memory_limited`` (a memory limit was enforced during the call).
    """
    failed = timed_out or exit_code is None or exit_code != 0
    if _rose(start, end, "oom_kill"):
        ev: dict[str, Any] = {"oom_kill": end["oom_kill"] - start["oom_kill"]}
        if not failed:
            ev["exited_ok"] = True
        return "memory", ev
    if not failed:
        return None, {}
    if _rose(start, end, "pids_max"):
        return "pids", {"pids_max_events": end["pids_max"] - start["pids_max"]}
    if memory_limited:
        line = match_oom_output(output)
        if line is not None:
            return "memory", {"output_match": line}
    if min_free is not None and min_free <= DISK_FULL_BYTES:
        return "disk", {"min_free_bytes": min_free}
    if _rose(start, end, "partition_hits") or _rose(start, end, "qdisc_drops"):
        ev = {}
        for k in ("partition_hits", "qdisc_drops"):
            if _rose(start, end, k):
                ev[k] = end[k] - start[k]
        return "network", ev
    if timed_out:
        return "deadline", throttle_fractions(start, end, duration_s)
    return None, {}


def throttle_fractions(start: dict, end: dict, duration_s: float) -> dict[str, float]:
    dur_us = max(duration_s, 1e-3) * 1e6
    out = {}
    for name, keys in (("cpu", ("throttled_usec", "psi_cpu")), ("memory", ("psi_mem",)), ("io", ("psi_io",))):
        best = 0.0
        for k in keys:
            a, b = start.get(k, float("nan")), end.get(k, float("nan"))
            if a == a and b == b:
                best = max(best, (b - a) / dur_us)
        out[name] = round(min(best, 1.0), 4)
    return out


def explain_text(cause: str | None, evidence: dict, limits: dict, profile: Profile, t: float,
                 deadline_s: float | None) -> str | None:
    if cause is None:
        return None
    if cause == "memory":
        mx, hi = limits.get("mem.max"), limits.get("mem.high")
        if mx is not None:
            lim = f"memory limit {fmt_bytes(mx)}"
        elif hi is not None:
            lim = f"soft memory limit {fmt_bytes(hi)}"
        else:
            lim = None
        if "output_match" in evidence:
            quoted = evidence["output_match"]
            quoted = quoted if len(quoted) <= 80 else quoted[:77] + "..."
            msg = (f"Failed: out of memory under the {lim}" if lim else "Failed: out of memory")
            msg += f' (the program reported "{quoted}")'
        elif evidence.get("exited_ok"):
            msg = (f"A process was killed: {lim} reached, but the call exited 0" if lim
                   else "A process was killed for lack of memory, but the call exited 0")
        else:
            msg = f"Killed: {lim} reached" if lim else "Killed: out of memory"
    elif cause == "pids":
        n = limits.get("pids.max")
        msg = f"Failed: process limit of {n} reached (fork failed)" if n else "Failed: process limit reached"
    elif cause == "disk":
        c = limits.get("disk.capacity")
        msg = (f"Failed: disk capacity {fmt_bytes(c)} reached (no space left on device)" if c is not None
               else "Failed: no space left on device")
    elif cause == "network":
        part, loss, rate = limits.get("net.partition"), limits.get("net.loss"), limits.get("net.rate")
        if part and part != "none":
            msg = f"Failed: network blocked ({part})"
        elif loss:
            msg = f"Failed: network loss {fmt_num(loss)}% in effect"
        elif rate:
            msg = f"Failed: network limited to {fmt_rate_bits(rate)}"
        else:
            msg = "Failed: network packets dropped"
    elif cause == "deadline":
        worst = max(evidence.items(), key=lambda kv: kv[1]) if evidence else ("", 0.0)
        head = f"Timed out after {fmt_num(deadline_s)} s" if deadline_s else "Timed out"
        if worst[1] >= 0.01:
            name = worst[0]
            lim = {"cpu": describe("cpu.cores", limits.get("cpu.cores")),
                   "memory": describe("mem.high", limits.get("mem.high")) or describe("mem.max", limits.get("mem.max")),
                   "io": next((d for k in ("io.wbps", "io.rbps", "io.wiops", "io.riops")
                               if (d := describe(k, limits.get(k)))), None)}.get(name)
            msg = f"{head}: {name} was the most throttled resource ({worst[1]:.0%} of the call"
            msg += f"; limit: {lim})" if lim else ")"
        else:
            msg = f"{head}: no resource limit was throttling the call"
    else:
        msg = f"Failed: {cause}"
    if profile.visibility == "full":
        seg, _ = profile.segment_at(t)
        if seg:
            s = profile.segments[seg - 1]
            msg += f" (segment {seg}, {fmt_num(s.t0)}–{fmt_num(s.t1)} s)"
    return msg + "."
