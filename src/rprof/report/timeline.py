"""Timeline: one interval per stretch with constant running calls and limits.

A new interval starts whenever a tool call starts or ends or a segment boundary
passes. Usage in an interval is shared by the calls running in it; a call that ran
alone gets exact numbers.
"""

from __future__ import annotations

from .. import knobs as K
from ..units import fmt_bytes
from .data import RunData

EPS = 1e-6


def interval_points(rd: RunData) -> list[float]:
    pts = {0.0, rd.t_end}
    for c in rd.calls:
        pts.add(c.t0)
        pts.add(c.end(rd.t_end))
    for b in rd.profile.boundaries():
        if 0 < b < rd.t_end:
            pts.add(b)
    pts = sorted(p for p in pts if 0 <= p <= rd.t_end)
    out = [pts[0]] if pts else []
    for p in pts[1:]:
        if p - out[-1] > EPS:
            out.append(p)
    return out


def _r(x, n=4):
    return None if x is None else round(x, n)


def interval_usage(rd: RunData, t0: float, t1: float) -> tuple[dict, dict, dict]:
    dur = max(t1 - t0, EPS)
    idx = rd.window_idx(t0, t1)
    usage: dict = {}
    cores = rd.rate(("cpu", "usage_usec"), t0, t1)
    tick = rd.tick_rates(rd.series(("cpu", "usage_usec")))
    in_ticks = [tick[i] for i in idx if tick[i] is not None]
    usage["cpu_cores_mean"] = _r(cores / 1e6 if cores is not None else None)
    usage["cpu_cores_max"] = _r(max(in_ticks) / 1e6 if in_ticks else usage["cpu_cores_mean"])
    cur = rd.series(("mem", "current"))
    peak = rd.series(("mem", "peak"))
    mems = [cur[i] for i in idx if cur[i] is not None]
    if not mems:
        m = rd._interp(cur, (t0 + t1) / 2)
        mems = [m] if m is not None else []
    usage["mem_mean"] = int(sum(mems) / len(mems)) if mems else None
    # memory.peak covers the tick that ends at sample i, so include the first tick after t1 too.
    pk_idx = list(idx) + ([idx.stop] if idx.stop < len(rd.t) else [])
    peaks = [p for i in pk_idx for p in (peak[i], cur[i]) if p is not None]
    usage["mem_peak"] = int(max(peaks)) if peaks else usage["mem_mean"]
    nr, basis = rd.mem_series("non_reclaimable")
    if basis != "total":
        vals = [nr[i] for i in pk_idx if nr[i] is not None]
        usage["mem_nonreclaimable_peak"] = int(max(vals)) if vals else None
    for name, field in (("io_rbps", "rbytes"), ("io_wbps", "wbytes")):
        ys = rd.io_series(field)
        a, b = rd._interp(ys, t0), rd._interp(ys, t1)
        usage[name] = _r((b - a) / dur, 1) if a is not None and b is not None else None
    for name, field in (("net_rx_bps", "rx_bytes"), ("net_tx_bps", "tx_bytes")):
        ys = rd.net_series(field)
        a, b = rd._interp(ys, t0), rd._interp(ys, t1)
        usage[name] = _r(max(0.0, b - a) * 8 / dur, 1) if a is not None and b is not None else None
    pids = rd.series(("pids", "current"))
    pv = [pids[i] for i in idx if pids[i] is not None]
    usage["pids_max"] = int(max(pv)) if pv else None

    pressure = {}
    for name, path in (("cpu_some", ("psi", "cpu", "some_us")), ("mem_some", ("psi", "memory", "some_us")),
                       ("mem_full", ("psi", "memory", "full_us")), ("io_some", ("psi", "io", "some_us")),
                       ("io_full", ("psi", "io", "full_us"))):
        r = rd.rate(path, t0, t1)
        pressure[name] = _r(min(1.0, r / 1e6)) if r is not None else None

    events = {}
    for name, path in (("oom_kill", ("mem", "events", "oom_kill")), ("mem_high", ("mem", "events", "high")),
                       ("mem_max", ("mem", "events", "max")), ("pids_max", ("pids", "events_max")),
                       ("net_drops", ("net", "qdisc_drops")), ("partition_hits", ("net", "partition_hits"))):
        d = rd.delta(path, t0, t1)
        events[name] = int(round(d)) if d is not None else None
    return usage, pressure, events


def build_timeline(rd: RunData) -> list[dict]:
    pts = interval_points(rd)
    out = []
    for t0, t1 in zip(pts[:-1], pts[1:]):
        mid = (t0 + t1) / 2
        seg, active = rd.profile.segment_at(t0)
        running = [c for c in rd.calls if c.t0 <= mid < c.end(rd.t_end)]
        failed = [c.call_id for c in rd.calls if c.failed and c.t1 is not None and abs(c.t1 - t1) < EPS]
        usage, pressure, events = interval_usage(rd, t0, t1)
        iv = {"t0": round(t0, 4), "t1": round(t1, 4), "segment": seg,
              "limits": K.limits_json(rd.profile.limits_at(t0)),
              "running_calls": [{"call_id": c.call_id, "cmd": c.cmd} for c in running],
              "usage": usage, "pressure": pressure, "events": events, "failed_calls": failed}
        if len(active) > 1:
            iv["active_segments"] = active
        out.append(iv)
    return out


def _limits_short(rd: RunData, t: float) -> str:
    lim = rd.profile.limits_at(t)
    raw = rd.profile.raw_at(t)
    parts = []
    for k, v in lim.items():
        if K.is_unified(k) or k.startswith("harness.") or k in ("cpu.period", "net.allow"):
            continue
        if not K.is_default(k, v) and v != rd.profile.defaults.get(k):
            parts.append(f"{k} {raw[k]}")
    return ", ".join(parts) if parts else "defaults"


def render_timeline(rd: RunData, rows: list[dict]) -> str:
    hdr = ["t (s)", "segment", "limits", "running calls", "cpu", "mem peak", "io write", "events"]
    table = []
    for iv in rows:
        # Slivers (e.g. between the harness exiting and the run ending) only add noise to the table;
        # timeline.jsonl and --json keep every interval.
        if iv["t1"] - iv["t0"] < 0.05 and not iv["running_calls"] and not iv["failed_calls"] \
                and not any(iv["events"].values()):
            continue
        calls = ", ".join(f"{c['call_id']} {c['cmd'].split()[0] if c['cmd'].split() else ''}".strip()
                          for c in iv["running_calls"]) or "-"
        u = iv["usage"]
        ev = []
        for k, v in iv["events"].items():
            if v:
                ev.append(f"{k}: {v}")
        for c in iv["failed_calls"]:
            ev.append(f"failed: {c}")
        cpu = f"{u['cpu_cores_mean']:.1f}" if u.get("cpu_cores_mean") is not None else "-"
        mem = fmt_bytes(u["mem_peak"]) if u.get("mem_peak") is not None else "-"
        iow = f"{fmt_bytes(int(u['io_wbps']))}/s" if u.get("io_wbps") else "0"
        table.append([f"{iv['t0']:.1f}–{iv['t1']:.1f}", str(iv["segment"]), _limits_short(rd, iv["t0"]),
                      calls, cpu, mem, iow, "; ".join(ev)])
    widths = [max(len(r[i]) for r in [hdr] + table) for i in range(len(hdr))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(hdr, widths)).rstrip()]
    for r in table:
        lines.append("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return "\n".join(lines) + "\n"
