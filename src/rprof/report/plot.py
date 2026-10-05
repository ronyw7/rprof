"""``rprof plot``: usage over the run, limits as step lines, tool calls as spans, failures as markers."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from .data import RunData  # noqa: E402

MiB = 1024 ** 2
SPAN_COLORS = ("#4C78A8", "#72B7B2", "#54A24B", "#EECA3B", "#B279A2", "#FF9DA6")


def _limit_steps(rd: RunData, knob: str, scale: float = 1.0) -> tuple[list[float], list[float]] | None:
    """Step-line points for a knob over [0, t_end]; None if never limited."""
    pts = [0.0] + [b for b in rd.profile.boundaries() if 0 < b < rd.t_end] + [rd.t_end]
    xs, ys, any_set = [], [], False
    for t in pts:
        v = rd.profile.limits_at(t).get(knob)
        if knob == "cpu.cpus" and v is not None:
            from ..units import cpuset_size
            v = cpuset_size(v)
        ys.append(float("nan") if v is None else float(v) / scale)
        xs.append(t)
        any_set = any_set or v is not None
    return (xs, ys) if any_set else None


def _rates(rd: RunData, ys: list[float | None], scale: float = 1.0) -> list[float]:
    return [float("nan") if v is None else v / scale for v in rd.tick_rates(ys)]


def _vals(ys: list[float | None], scale: float = 1.0) -> list[float]:
    return [float("nan") if v is None else v / scale for v in ys]


def _has(ys: list[float]) -> bool:
    return any(v == v for v in ys)


def plot_run(run_dir: str | Path, out: str | Path) -> Path:
    rd = RunData(run_dir)
    out = Path(out)
    t = rd.t

    # Each panel: (title, unit label, [(label, series, style)], [(knob, scale, label)])
    panels: list[tuple[str, str, list, list]] = []

    cpu = _rates(rd, rd.series(("cpu", "usage_usec")), 1e6)
    panels.append(("CPU", "cores", [("used", cpu, "-")],
                   [("cpu.cores", 1.0, "cpu.cores"), ("cpu.cpus", 1.0, "cpu.cpus (count)")]))

    mem = [("current", _vals(rd.series(("mem", "current")), MiB), "-")]
    if rd.has(("mem", "peak")):
        mem.append(("peak", _vals(rd.series(("mem", "peak")), MiB), ":"))
    if any((v or 0) > 0 for v in rd.series(("mem", "swap_current"))):
        mem.append(("swap", _vals(rd.series(("mem", "swap_current")), MiB), "--"))
    panels.append(("Memory", "MiB", mem, [("mem.high", MiB, "mem.high"), ("mem.max", MiB, "mem.max")]))

    io = [("read", _rates(rd, rd.io_series("rbytes"), MiB), "-"),
          ("write", _rates(rd, rd.io_series("wbytes"), MiB), "-")]
    dev = f" ({rd.io_dev})" if rd.io_dev else ""
    panels.append((f"Disk I/O{dev}", "MiB/s", io, [("io.rbps", MiB, "io.rbps"), ("io.wbps", MiB, "io.wbps")]))

    panels.append(("Processes", "tasks", [("pids", _vals(rd.series(("pids", "current"))), "-")],
                   [("pids.max", 1.0, "pids.max")]))

    net = [("rx", _rates(rd, rd.net_series("rx_bytes"), 1e6 / 8), "-"),
           ("tx", _rates(rd, rd.net_series("tx_bytes"), 1e6 / 8), "-")]
    panels.append(("Network", "Mbit/s", net, [("net.rate", 1e6, "net.rate")]))

    disk = rd.disk_workload()
    if any(v is not None for v in disk) and _limit_steps(rd, "disk.capacity"):
        panels.append(("Data filesystem", "MiB used", [("workload", _vals(disk, MiB), "-")],
                       [("disk.capacity", MiB, "disk.capacity")]))

    psi = []
    for name, path in (("cpu some", ("psi", "cpu", "some_us")), ("mem some", ("psi", "memory", "some_us")),
                       ("mem full", ("psi", "memory", "full_us")), ("io some", ("psi", "io", "some_us"))):
        psi.append((name, [min(1.0, v) if v == v else v for v in _rates(rd, rd.series(path), 1e6)], "-"))
    panels.append(("Pressure (PSI)", "stall fraction", psi, []))

    def keep(p) -> bool:
        return any(_has(s) for _, s, _ in p[2])
    panels = [p for p in panels if keep(p)]
    if not panels:
        panels = [("CPU", "cores", [("used", cpu, "-")], [])]

    fig, axes = plt.subplots(len(panels), 1, sharex=True, figsize=(13, 2.1 * len(panels) + 0.8), squeeze=False)
    axes = [a[0] for a in axes]

    boundaries = [b for b in rd.profile.boundaries() if 0 < b < rd.t_end]
    calls = rd.calls
    for ax, (title, unit, series, limits) in zip(axes, panels):
        for label, ys, style in series:
            if _has(ys):
                ax.plot(t, ys, style, lw=1.1, label=label)
        for knob, scale, label in limits:
            st = _limit_steps(rd, knob, scale)
            if st:
                ax.step(st[0], st[1], where="post", color="#D62728", lw=1.2, ls="--", alpha=0.85, label=label)
        for i, c in enumerate(calls):
            ax.axvspan(c.t0, c.end(rd.t_end), color=SPAN_COLORS[i % len(SPAN_COLORS)], alpha=0.12, lw=0)
        for b in boundaries:
            ax.axvline(b, color="0.5", lw=0.6, alpha=0.5)
        for c in calls:
            if c.failed and c.t1 is not None:
                ax.axvline(c.t1, color="#D62728", lw=0.8, alpha=0.6)
        ax.set_ylabel(unit, fontsize=8)
        ax.set_title(title, fontsize=9, loc="left")
        ax.tick_params(labelsize=8)
        ax.grid(alpha=0.25, lw=0.5)
        ax.set_ylim(bottom=0)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=7, loc="upper left", bbox_to_anchor=(1.005, 1.0), framealpha=0.7)

    # Segment numbers and call labels on the top panel; failure markers on every panel.
    top = axes[0]
    ytop = top.get_ylim()[1]
    edges = [0.0] + boundaries + [rd.t_end]
    for a, b in zip(edges[:-1], edges[1:]):
        seg, _ = rd.profile.segment_at(a)
        top.text((a + b) / 2, ytop * 0.97, f"seg {seg}", ha="center", va="top", fontsize=7, color="0.35")
    for i, c in enumerate(calls):
        top.text(c.t0, ytop * 0.80, c.call_id, fontsize=6.5, color=SPAN_COLORS[i % len(SPAN_COLORS)],
                 ha="left", va="top", clip_on=True)
    for ax in axes:
        y0, y1 = ax.get_ylim()
        for c in calls:
            if c.failed and c.t1 is not None:
                ax.plot([c.t1], [y1 * 0.92], marker="x", color="#D62728", ms=6, mew=1.5, clip_on=False)
        ax.set_ylim(y0, y1)
    axes[-1].set_xlabel("t (s since run start)", fontsize=8)
    axes[-1].set_xlim(0, max(rd.t_end, 1e-3))
    fails = sum(c.failed for c in calls)
    fig.suptitle(f"{rd.run_id} · {rd.mode} · profile {rd.profile.name} · {len(calls)} calls, {fails} failed",
                 fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


__all__ = ["plot_run", "Callable"]
