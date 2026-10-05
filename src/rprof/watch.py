"""``rprof watch``: a live usage table, optionally recorded to a JSON-lines file."""

from __future__ import annotations

import time
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table

from . import controllers as C
from .clock import RunClock
from .inspect_ import current_limits
from .sampler import Sampler
from .target import Target
from .units import fmt_bytes, fmt_rate_bits
from .util import JsonlWriter


def _g(d, *ks):
    for k in ks:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _rate(a, b, dt, *ks):
    x, y = _g(a, *ks), _g(b, *ks)
    if x is None or y is None or dt <= 0:
        return None
    return max(0.0, (y - x) / dt)


def _sum_dev(s, field):
    io = s.get("io") or {}
    return sum(v.get(field, 0) for v in io.values())


def _sum_net(s, field):
    net = s.get("net") or {}
    return sum(v.get(field, 0) for v in net.values() if isinstance(v, dict))


def row(prev: dict, cur: dict, lim: dict) -> dict:
    dt = cur["t"] - prev["t"]
    r = {}
    cores = _rate(prev, cur, dt, "cpu", "usage_usec")
    thr = _rate(prev, cur, dt, "cpu", "throttled_usec")
    cm = (lim.get("cpu.max") or "max").split()
    r["cpu"] = (f"{cores / 1e6:.2f}" if cores is not None else "-") + (
        f" / {int(cm[0]) / int(cm[1]):.2f}" if cm[0] != "max" else " / max")
    r["thr"] = f"{thr / 1e4:.0f}%" if thr is not None else "-"
    m = cur.get("mem") or {}
    r["mem"] = fmt_bytes(m.get("current")) if m.get("current") is not None else "-"
    r["high/max"] = f"{_lim_b(lim.get('memory.high'))} / {_lim_b(lim.get('memory.max'))}"
    r["oom"] = str(_g(cur, "mem", "events", "oom_kill") or 0)
    dtt = dt if dt > 0 else 1
    r["io r/w"] = (f"{(_sum_dev(cur, 'rbytes') - _sum_dev(prev, 'rbytes')) / dtt / 2**20:.1f} / "
                   f"{(_sum_dev(cur, 'wbytes') - _sum_dev(prev, 'wbytes')) / dtt / 2**20:.1f} MiB/s")
    p = cur.get("pids") or {}
    r["pids"] = f"{p.get('current', '-')} / {lim.get('pids.max') or '-'}"
    rx = (_sum_net(cur, "rx_bytes") - _sum_net(prev, "rx_bytes")) * 8 / dtt
    tx = (_sum_net(cur, "tx_bytes") - _sum_net(prev, "tx_bytes")) * 8 / dtt
    r["net rx/tx"] = f"{fmt_rate_bits(int(rx))} / {fmt_rate_bits(int(tx))}" if cur.get("net") else "-"
    psi = []
    for k in ("cpu", "memory", "io"):
        v = _rate(prev, cur, dt, "psi", k, "some_us")
        psi.append(f"{v / 1e4:.0f}%" if v is not None else "-")
    r["psi c/m/io"] = "/".join(psi)
    return r


def _lim_b(v):
    if v is None:
        return "-"
    return "max" if v == "max" else fmt_bytes(int(v))


def watch(t: Target, hz: float = 10.0, out: str | None = None, duration: float | None = None,
          refresh_hz: float = 2.0, once: bool = False) -> None:
    clock = RunClock()
    ctrls = C.build(t)
    s = Sampler(t, ctrls, clock, hz)
    w = JsonlWriter(Path(out)) if out else None
    console = Console()
    cols = ["t", "cpu", "thr", "mem", "high/max", "oom", "io r/w", "pids", "net rx/tx", "psi c/m/io"]

    def table(r: dict, t_now: float) -> Table:
        tb = Table(title=f"rprof watch {t.spec} ({t.cgroup.rel})", expand=False)
        for c in cols:
            tb.add_column(c)
        tb.add_row(f"{t_now:.1f}", *[r.get(c, "-") for c in cols[1:]])
        return tb

    prev = s.tick()
    if w:
        w.write(prev)
    if once:
        time.sleep(1.0)
        cur = s.tick()
        console.print(table(row(prev, cur, current_limits(t)), cur["t"]))
        return
    last_draw = 0.0
    window = [prev]
    try:
        with Live(console=console, auto_refresh=False) as live:
            nxt = time.monotonic()
            while duration is None or clock.now() < duration:
                nxt += 1.0 / hz
                time.sleep(max(0.0, nxt - time.monotonic()))
                cur = s.tick()
                if w:
                    w.write(cur)
                window.append(cur)
                while len(window) > 2 and cur["t"] - window[0]["t"] > 1.0:
                    window.pop(0)
                if cur["t"] - last_draw >= 1.0 / refresh_hz:
                    live.update(table(row(window[0], cur, current_limits(t)), cur["t"]), refresh=True)
                    last_draw = cur["t"]
                if not t.cgroup.exists():
                    break
    except KeyboardInterrupt:
        pass
    finally:
        if w:
            w.close()
        s.close()
