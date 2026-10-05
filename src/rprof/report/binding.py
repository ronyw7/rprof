"""Per-segment, per-knob binding decisions (enforce) and violation metrics (measure)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import knobs as K
from ..units import cpuset_size
from .data import RunData, stats

DISK_FULL_BYTES = 1 << 20

# Knobs that describe the environment rather than a budget: reported as context only.
CONTEXT_KNOBS = {"net.delay", "net.jitter", "cpu.period", "net.allow"}


@dataclass
class Thresholds:
    throttle_frac: float = 0.05
    pressure_frac: float = 0.05
    throughput_frac: float = 0.8

    @classmethod
    def from_dict(cls, d: dict | None) -> "Thresholds":
        t = cls()
        for k, v in (d or {}).items():
            if not hasattr(t, k):
                raise ValueError(f"unknown threshold {k!r} (have: throttle_frac, pressure_frac, throughput_frac)")
            setattr(t, k, float(v))
        return t


Window = list[tuple[float, float]]


def _sum_delta(rd: RunData, path, win: Window) -> float | None:
    vals = [rd.delta(path, a, b) for a, b in win]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def _frac(rd: RunData, path, win: Window) -> float | None:
    d = _sum_delta(rd, path, win)
    dur = sum(b - a for a, b in win)
    if d is None or dur <= 0:
        return None
    return d / (dur * 1e6)


def _in(rd: RunData, ys: list, win: Window) -> list[float]:
    out = []
    for a, b in win:
        out.extend(ys[i] for i in rd.window_idx(a, b) if ys[i] is not None)
    return out


MEMORY_BASES = ("non_reclaimable", "total")


def usage_series(rd: RunData, knob: str, memory_basis: str = "non_reclaimable"
                 ) -> tuple[list[float | None], str] | None:
    """Per-tick usage comparable with the knob's limit, and its unit."""
    if knob in ("cpu.cores", "cpu.cpus"):
        r = rd.tick_rates(rd.series(("cpu", "usage_usec")))
        return [None if x is None else x / 1e6 for x in r], "cores"
    if knob in ("mem.max", "mem.high"):
        ys, _ = rd.mem_series(memory_basis, with_peak=knob == "mem.max")
        return ys, "bytes"
    if knob == "mem.swap_max":
        return rd.series(("mem", "swap_current")), "bytes"
    if knob in ("io.rbps", "io.wbps", "io.riops", "io.wiops"):
        field = {"io.rbps": "rbytes", "io.wbps": "wbytes", "io.riops": "rios", "io.wiops": "wios"}[knob]
        unit = "bytes/s" if knob.endswith("bps") else "ops/s"
        return rd.tick_rates(rd.io_series(field)), unit
    if knob == "pids.max":
        return rd.series(("pids", "current")), "tasks"
    if knob in ("net.rate", "net.partition"):
        rx, tx = rd.tick_rates(rd.net_series("rx_bytes")), rd.tick_rates(rd.net_series("tx_bytes"))
        return [None if a is None and b is None else max(a or 0, b or 0) * 8 for a, b in zip(rx, tx)], "bit/s"
    if knob == "disk.capacity":
        return rd.disk_workload(), "bytes"
    return None


def smoothed_throughput(rd: RunData, knob: str) -> list[float | None]:
    if knob.startswith("io."):
        field = {"io.rbps": "rbytes", "io.wbps": "wbytes", "io.riops": "rios", "io.wiops": "wios"}[knob]
        return rd.smoothed(rd.io_series(field))
    rx, tx = rd.smoothed(rd.net_series("rx_bytes")), rd.smoothed(rd.net_series("tx_bytes"))
    return [None if a is None and b is None else max(a or 0, b or 0) * 8 for a, b in zip(rx, tx)]


def limit_number(knob: str, value: Any) -> float | None:
    if value is None:
        return None
    if knob == "cpu.cpus":
        return float(cpuset_size(value))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _rounded(st: dict | None) -> dict | None:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in st.items()} if st else None


def evaluate(rd: RunData, knob: str, limit: Any, win: Window, calls_failed: list, th: Thresholds,
             mode: str, memory_basis: str = "non_reclaimable") -> dict:
    row: dict[str, Any] = {"knob": knob, "limit": limit, "usage": None, "unit": None, "bound": None,
                           "evidence": {}, "violation": None}
    us = usage_series(rd, knob, memory_basis)
    if us is not None:
        ys, unit = us
        row["usage"] = _rounded(stats(_in(rd, ys, win)))
        row["unit"] = unit
    if knob in ("mem.max", "mem.high"):
        # Which memory the usage and violation figures count; the total is kept alongside.
        _, row["basis"] = rd.mem_series(memory_basis, with_peak=knob == "mem.max")
        if row["basis"] != "total":
            tot, _ = rd.mem_series("total", with_peak=knob == "mem.max")
            row["usage_total"] = _rounded(stats(_in(rd, tot, win)))
    if K.is_unified(knob) or knob in CONTEXT_KNOBS:
        row["context"] = True
        return row
    lim = limit_number(knob, limit)
    ev: dict[str, Any] = {}

    if mode == "measure":
        row["violation"] = violation(rd, knob, limit, lim, win, us)
        return row

    bound = False
    if knob in ("cpu.cores", "cpu.cpus"):
        # throttled_usec sums over per-CPU runqueues, so it can exceed wall time: clamp to 1.
        thr = _frac(rd, ("cpu", "throttled_usec"), win)
        thr = None if thr is None else min(1.0, thr)
        per = _sum_delta(rd, ("cpu", "nr_periods"), win)
        nthr = _sum_delta(rd, ("cpu", "nr_throttled"), win)
        pfrac = (nthr / per) if per and nthr is not None else None
        psi = _frac(rd, ("psi", "cpu", "some_us"), win)
        if knob == "cpu.cores":
            if thr is not None:
                ev["throttled_frac"] = round(thr, 4)
            if pfrac is not None:
                ev["throttled_periods_frac"] = round(pfrac, 4)
        if psi is not None:
            ev["cpu_pressure"] = round(min(1.0, psi), 4)
        bound = (knob == "cpu.cores" and (thr or 0) >= th.throttle_frac) or (psi or 0) >= th.pressure_frac
    elif knob == "mem.high":
        hi = _sum_delta(rd, ("mem", "events", "high"), win)
        psi = _frac(rd, ("psi", "memory", "some_us"), win)
        ev["mem_high_events"] = int(hi) if hi is not None else None
        if psi is not None:
            ev["mem_pressure"] = round(psi, 4)
        bound = (hi or 0) > 0 or (psi or 0) >= th.pressure_frac
    elif knob == "mem.max":
        mx = _sum_delta(rd, ("mem", "events", "max"), win)
        ok = _sum_delta(rd, ("mem", "events", "oom_kill"), win)
        ev["mem_max_events"] = int(mx) if mx is not None else None
        ev["oom_kill"] = int(ok) if ok is not None else None
        bound = (mx or 0) > 0 or (ok or 0) > 0
    elif knob == "mem.swap_max":
        st = row["usage"]
        bound = bool(lim and st and st["max"] >= 0.95 * lim)
    elif knob.startswith("io."):
        psi = _frac(rd, ("psi", "io", "some_us"), win)
        thr = max(_in(rd, smoothed_throughput(rd, knob), win), default=None)
        if psi is not None:
            ev["io_pressure"] = round(psi, 4)
        if thr is not None and lim:
            ev["throughput_frac"] = round(thr / lim, 4)
        bound = (psi or 0) >= th.pressure_frac and bool(lim) and (thr or 0) >= th.throughput_frac * lim
    elif knob == "pids.max":
        pm = _sum_delta(rd, ("pids", "events_max"), win)
        ev["pids_max_events"] = int(pm) if pm is not None else None
        bound = (pm or 0) > 0
    elif knob == "net.rate":
        thr = max(_in(rd, smoothed_throughput(rd, knob), win), default=None)
        if thr is not None and lim:
            ev["throughput_frac"] = round(thr / lim, 4)
        bound = bool(lim) and (thr or 0) >= th.throughput_frac * lim
    elif knob == "net.loss":
        d = _sum_delta(rd, ("net", "qdisc_drops"), win)
        ev["qdisc_drops"] = int(d) if d is not None else None
        bound = (d or 0) > 0
    elif knob == "net.partition":
        d = _sum_delta(rd, ("net", "partition_hits"), win)
        ev["partition_hits"] = int(d) if d is not None else None
        bound = (d or 0) > 0
    elif knob == "disk.capacity":
        free = _in(rd, rd.series(("disk", "free_bytes")), win)
        mn = min(free) if free else None
        ev["min_free_bytes"] = int(mn) if mn is not None else None
        enospc = [c for c in calls_failed if c.cause == "disk"]
        ev["enospc_calls"] = len(enospc)
        bound = (mn is not None and mn <= DISK_FULL_BYTES) or bool(enospc)
    row["bound"] = bool(bound)
    row["evidence"] = ev
    return row


def violation(rd: RunData, knob: str, limit: Any, lim: float | None, win: Window, us) -> dict | None:
    if us is None:
        return None
    ys, _ = us
    vals = _in(rd, ys, win)
    if not vals:
        return {"time_frac": None, "peak_over": None}
    if knob == "net.partition":
        if limit in (None, "none"):
            return None
        over = [v for v in vals if v > 0]
        nbytes = 0.0
        for f in ("rx_bytes", "tx_bytes"):
            ys2 = rd.net_series(f)
            for a, b in win:
                x0, x1 = rd._interp(ys2, a), rd._interp(ys2, b)
                if x0 is not None and x1 is not None:
                    nbytes += max(0.0, x1 - x0)
        return {"time_frac": round(len(over) / len(vals), 4), "peak_over": int(nbytes)}
    if lim is None:
        return {"time_frac": 0.0, "peak_over": 0}
    over = [v - lim for v in vals if v > lim]
    return {"time_frac": round(len(over) / len(vals), 4),
            "peak_over": round(max(over), 4) if over else 0}
