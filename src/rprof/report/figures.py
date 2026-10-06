"""Paper figures: one or more runs overlaid, one panel per metric, the limit as a step line.

``plot_row`` puts the metrics side by side in one row, ``PANEL_IN`` wide each; ``plot_paper``
writes each metric to its own single-column figure. ``width`` overrides either width. Two styles, both with Times and embedded
TrueType fonts (camera-ready checks reject Type 3 fonts): ``classic`` (thin lines, hollow
markers, dotted grid, as in gnuplot figures) and ``bold`` (bold labels, filled markers, a boxed
legend). Samples are averaged into ``BIN_S`` bins: raw 10 Hz rates alias.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import ticker  # noqa: E402

from .. import knobs as K  # noqa: E402
from .data import RunData  # noqa: E402
from .plot import _limit_steps  # noqa: E402

MiB = 1 << 20
BIN_S = 0.5
SINGLE_COL_IN, DOUBLE_COL_IN = 3.33, 7.0
PANEL_IN = 2.4          # each panel of a row

SERIF = ["Times New Roman", "Times", "Nimbus Roman", "Nimbus Roman No9 L", "STIXGeneral", "DejaVu Serif"]
_COMMON = {"font.family": "serif", "font.serif": SERIF, "mathtext.fontset": "stix",
           "pdf.fonttype": 42, "ps.fonttype": 42, "savefig.dpi": 300}
STYLES = {
    "classic": {
        "rc": {**_COMMON, "font.size": 8, "axes.labelsize": 8, "xtick.labelsize": 7, "ytick.labelsize": 7,
               "legend.fontsize": 7, "axes.linewidth": 0.6, "xtick.direction": "in", "ytick.direction": "in",
               "xtick.top": True, "ytick.right": True, "xtick.major.width": 0.6, "ytick.major.width": 0.6,
               "axes.grid": True, "axes.grid.axis": "y", "grid.linestyle": ":", "grid.color": "0.25",
               "grid.linewidth": 0.6, "legend.frameon": False, "lines.linewidth": 0.8, "lines.markersize": 3.6},
        "series": [dict(color=c, marker=m, mfc="white", mew=0.7) for c, m in
                   (("black", "s"), ("#D62728", "o"), ("#1F77B4", "^"), ("#2CA02C", "D"), ("#9467BD", "v"))],
        "limit": dict(color="#1F3FBF", ls="--", lw=0.8),
        "legend_one": dict(loc="upper right", markerfirst=False, handlelength=2.6),
        "legend_row": dict(loc="lower center", bbox_to_anchor=(0.5, 1.0), frameon=False, markerfirst=False,
                           handlelength=2.6),
        "height": 1.75,
    },
    "bold": {
        "rc": {**_COMMON, "font.size": 9, "font.weight": "bold", "axes.labelweight": "bold", "axes.labelsize": 10,
               "xtick.labelsize": 9, "ytick.labelsize": 9, "legend.fontsize": 8.5, "axes.linewidth": 0.8,
               "axes.spines.top": False, "axes.spines.right": False, "lines.linewidth": 1.4,
               "lines.markersize": 4},
        "series": [dict(color=c, ls=ls, marker=m) for c, ls, m in
                   (("black", "--", "o"), ("#E8833A", "-.", "^"), ("#2B3990", "-", "s"), ("#3A9E57", ":", "D"),
                    ("#8E44AD", (0, (5, 1, 1, 1)), "v"))],
        "limit": dict(color="#2B3990", ls="-", lw=1.1),
        "legend_one": dict(loc="upper center", bbox_to_anchor=(0.5, -0.38), ncol=3, frameon=True,
                           fancybox=False, edgecolor="0.8", handlelength=2.4, columnspacing=1.2),
        "legend_row": dict(loc="upper center", bbox_to_anchor=(0.5, 0.0), frameon=True, fancybox=False,
                           edgecolor="0.8", handlelength=2.4),
        "height": 1.95,
    },
}


@dataclass(frozen=True)
class Metric:
    name: str
    ylabel: str
    values: Callable[[RunData], list]       # one value per sample
    rate: bool                              # values are a counter: plot its per-second rate
    scale: float                            # divide by this for the axis unit
    knobs: tuple[tuple[str, float], ...] = ()   # limits drawn as step lines: (knob, scale)
    log: bool = False                       # may span decades: log axis when it does
    pct: bool = False                       # a share of time, 0–100


def _path(*p: str) -> Callable[[RunData], list]:
    return lambda rd: rd.series(p)


METRICS: dict[str, Metric] = {m.name: m for m in (
    Metric("cpu", "CPU (cores)", _path("cpu", "usage_usec"), True, 1e6, (("cpu.cores", 1.0), ("cpu.cpus", 1.0))),
    Metric("cpu-throttled", "CPU throttled (%)", _path("cpu", "throttled_usec"), True, 1e4, pct=True),
    Metric("memory", "Memory (MiB)", lambda rd: rd.mem_series("non_reclaimable")[0], False, MiB,
           (("mem.max", MiB), ("mem.high", MiB)), log=True),     # runs under 1 GiB and 48 GiB side by side
    Metric("swap", "Swap (MiB)", _path("mem", "swap_current"), False, MiB, (("mem.swap_max", MiB),)),
    Metric("disk-read", "Disk read (MiB/s)", lambda rd: rd.io_series("rbytes"), True, MiB, (("io.rbps", MiB),),
           log=True),
    Metric("disk-write", "Disk write (MiB/s)", lambda rd: rd.io_series("wbytes"), True, MiB, (("io.wbps", MiB),),
           log=True),
    Metric("read-iops", "Read IOPS", lambda rd: rd.io_series("rios"), True, 1.0, (("io.riops", 1.0),), log=True),
    Metric("write-iops", "Write IOPS", lambda rd: rd.io_series("wios"), True, 1.0, (("io.wiops", 1.0),), log=True),
    Metric("disk-used", "Disk used (MiB)", lambda rd: rd.disk_workload(), False, MiB, (("disk.capacity", MiB),)),
    Metric("net-in", "Net in (Mbit/s)", lambda rd: rd.net_series("rx_bytes"), True, 1e6 / 8,
           (("net.rate", 1e6),), log=True),
    Metric("net-out", "Net out (Mbit/s)", lambda rd: rd.net_series("tx_bytes"), True, 1e6 / 8,
           (("net.rate", 1e6),), log=True),
    Metric("retransmits", "TCP retransmits (/s)", _path("net", "tcp_retrans_segs"), True, 1.0),
    Metric("processes", "Processes", _path("pids", "current"), False, 1.0, (("pids.max", 1.0),)),
    Metric("cpu-stall", "CPU stall (%)", _path("psi", "cpu", "some_us"), True, 1e4, pct=True),
    Metric("memory-stall", "Memory stall (%)", _path("psi", "memory", "some_us"), True, 1e4, pct=True),
    Metric("io-stall", "I/O stall (%)", _path("psi", "io", "some_us"), True, 1e4, pct=True),
)}
CORE = ("cpu", "memory", "disk-read", "disk-write", "net-in", "net-out")


def _limited(rd: RunData, knob: str) -> bool:
    """Whether an enforce-mode run's profile sets ``knob`` to a non-default value at any time."""
    if rd.mode != "enforce":
        return False
    default = K.KNOBS[knob].default
    pts = [0.0] + [b for b in rd.profile.boundaries() if b < rd.t_end]
    return any(rd.profile.limits_at(t).get(knob) not in (None, default) for t in pts)


def default_metrics(runs: list[RunData]) -> list[str]:
    """The core metrics, then one for each limit an enforce-mode run sets that they don't show,
    if that metric has any data."""
    shown = {k for n in CORE for k, _ in METRICS[n].knobs}
    extra = {m.name for m in METRICS.values() if m.name not in CORE
             and any(k not in shown and _limited(rd, k) for rd in runs for k, _ in m.knobs)
             and _active(runs, m)}               # e.g. no swap panel if nothing swapped
    return list(CORE) + [n for n in METRICS if n in extra]


def run_label(rd: RunData) -> str:
    """The run's --name: run ids are <YYYY-MM-DDTHHMM>-<name>."""
    parts = rd.run_id.split("-", 3)
    return parts[3] if len(parts) == 4 else rd.run_id


def binned(rd: RunData, m: Metric) -> tuple[list[float], list[float]]:
    vals = m.values(rd)
    if m.rate:
        vals = rd.tick_rates(vals)
    n = int(rd.t_end // BIN_S) + 1
    acc: list[list[float]] = [[] for _ in range(n)]
    for t, v in zip(rd.t, vals):
        if v is not None and 0 <= t <= rd.t_end:
            acc[min(n - 1, int(t // BIN_S))].append(v / m.scale)
    xs = [(i + 0.5) * BIN_S for i in range(n) if acc[i]]
    ys = [sum(a) / len(a) for a in acc if a]
    if m.pct:
        ys = [min(100.0, y) for y in ys]
    return xs, ys


def _limits(runs: list[RunData], m: Metric) -> list[tuple[int, str, tuple[list, list]]]:
    """(run index, knob, step points) for each limit an enforce-mode run sets; one per distinct schedule."""
    out, seen = [], []
    for i, rd in enumerate(runs):
        for knob, scale in m.knobs:
            if not _limited(rd, knob):
                continue
            st = _limit_steps(rd, knob, scale)
            key = st and (knob, tuple(st[0]), tuple(None if y != y else y for y in st[1]))
            if st and key not in seen:
                seen.append(key)
                out.append((i, knob, st))
    return out


def _si(v: float, _pos=None) -> str:
    for div, suf in ((1e9, "G"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"{v / div:g}{suf}"
    return f"{v:g}"


def _log_floor(series: list[list[float]], limits: list[float]) -> float | None:
    """A power of ten to start a log axis at, or None for a linear axis.

    Log only when the levels that matter, each run's peak and each limit, span more than ~1.5
    decades, as when an unconstrained run reaches 30 Gbit/s next to a 10 Mbit/s limit. Background
    noise far below them doesn't count, and sits on the floor.
    """
    levels = [max(ys) for ys in series if ys and max(ys) > 0] + [x for x in limits if x > 0]
    if len(levels) < 2:
        return None
    top = max(levels)
    low = max(min(levels), top / 1e4)
    if top / low < 30:
        return None
    return 10.0 ** (math.floor(math.log10(low)) - 1)


# Panels that read as a pair share their y scale: both log if either needs it.
PAIRS = (("disk-read", "disk-write"), ("read-iops", "write-iops"), ("net-in", "net-out"))


def _own_floor(runs: list[RunData], m: Metric) -> float | None:
    if not m.log:
        return None
    limits = [y for *_, (_, ys) in _limits(runs, m) for y in ys if y == y]
    return _log_floor([binned(rd, m)[1] for rd in runs], limits)


def _active(runs: list[RunData], m: Metric) -> bool:
    return any(y > 0 for rd in runs for y in binned(rd, m)[1])


def log_floors(runs: list[RunData], metrics: list[str]) -> dict[str, float | None]:
    """Each metric's log-axis floor, or None for a linear axis. A pair shares a log axis when
    either needs one and both have data; a panel with nothing in it stays linear."""
    floors = {n: _own_floor(runs, METRICS[n]) for n in metrics}
    for pair in PAIRS:
        live = [n for n in pair if n in floors and _active(runs, METRICS[n])]
        fs = [floors[n] for n in live if floors[n]]
        for n in live:
            if fs:
                floors[n] = min(fs)
    return floors


def draw(ax, style: str, runs: list[RunData], labels: list[str], m: Metric, markers: int = 7,
         floor: float | None = None) -> bool:
    """One metric on ``ax``, on a log axis from ``floor`` if given; returns whether it's log."""
    st = STYLES[style]
    data = [binned(rd, m) for rd in runs]
    limits = _limits(runs, m)
    for i, ((xs, ys), label) in enumerate(zip(data, labels)):
        if floor:
            ys = [max(y, floor) for y in ys]            # idle stretches run along the bottom
        every = max(1, len(xs) // markers)
        ax.plot(xs, ys, label=label, markevery=(i * every // max(1, len(runs)), every),
                **st["series"][i % len(st["series"])])
    distinct = {k for _, k, _ in limits}
    for i, knob, (xs, ys) in limits:
        name = "Limit" if len(distinct) == 1 and len(limits) == 1 else f"{labels[i]} {knob}" if len(runs) > 1 \
            else knob
        kw = dict(st["limit"]) if len(limits) == 1 else {**st["limit"], "color": st["series"][i]["color"]}
        ax.step(xs, ys, where="post", label=name, **kw)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(m.ylabel)
    ax.set_xlim(0, max((rd.t_end for rd in runs), default=1.0) or 1.0)
    if floor:
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(ticker.LogLocator(base=10, numticks=6))
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(_si))
        ax.yaxis.set_minor_formatter(ticker.NullFormatter())
        ax.set_ylim(bottom=floor)
    else:
        ax.set_ylim(bottom=0)
        if m.pct:
            ax.set_ylim(top=100)
        elif not any(y > 0 for _, ys in data for y in ys) and not limits:
            ax.set_ylim(top=1)                          # nothing happened: a plain 0–1 axis
    return bool(floor)


def _check(style: str, metrics: list[str]) -> None:
    if style not in STYLES:
        raise ValueError(f"unknown style {style!r}; choose {' or '.join(STYLES)}")
    bad = [n for n in metrics if n not in METRICS]
    if bad:
        raise ValueError(f"unknown metric {', '.join(bad)}; choose from {', '.join(METRICS)}")


def _load(run_dirs: list[Path], labels: list[str] | None, metrics: list[str] | None
          ) -> tuple[list[RunData], list[str], list[str]]:
    runs = [RunData(d) for d in run_dirs]
    if labels and len(labels) != len(runs):
        raise ValueError(f"{len(labels)} labels for {len(runs)} runs")
    return runs, labels or [run_label(rd) for rd in runs], metrics or default_metrics(runs)


def _save(fig, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)
    return out


def plot_row(run_dirs: list[Path], out: Path, style: str = "classic", metrics: list[str] | None = None,
             labels: list[str] | None = None, width: float | None = None) -> Path:
    """All metrics side by side in one row, ``PANEL_IN`` wide each unless ``width`` sets the total."""
    runs, labels, metrics = _load(run_dirs, labels, metrics)
    _check(style, metrics)
    st = STYLES[style]
    n = len(metrics)
    with plt.rc_context(st["rc"]):
        width = width or (SINGLE_COL_IN if n == 1 else n * PANEL_IN)
        fig, axes = plt.subplots(1, n, figsize=(width, st["height"]), squeeze=False)
        flat = list(axes[0])
        handles: dict[str, object] = {}
        floors = log_floors(runs, metrics)
        for ax, name in zip(flat, metrics):
            log = draw(ax, style, runs, labels, METRICS[name], markers=max(3, round(width / n * 3)),
                       floor=floors[name])
            lo, top = ax.get_ylim()
            if log:
                ax.set_ylim(lo, 10.0 ** math.ceil(math.log10(top * 1.2)))    # end on a labelled decade
            elif not METRICS[name].pct:
                ax.set_ylim(lo, top * 1.08)
            for h, lab in zip(*ax.get_legend_handles_labels()):
                handles.setdefault(lab, h)
        fig.tight_layout(pad=0.3, w_pad=1.0)
        fig.legend(list(handles.values()), list(handles), ncol=min(len(handles), 6), **st["legend_row"])
        return _save(fig, out)


def plot_paper(run_dirs: list[Path], out_dir: Path, style: str = "classic", metrics: list[str] | None = None,
               labels: list[str] | None = None, fmt: str = "pdf", width: float | None = None) -> list[Path]:
    """Each metric in its own figure, ``<out_dir>/<metric>.<fmt>``: one column wide unless ``width`` says."""
    runs, labels, metrics = _load(run_dirs, labels, metrics)
    _check(style, metrics)
    st = STYLES[style]
    paths = []
    floors = log_floors(runs, metrics)
    with plt.rc_context(st["rc"]):
        for name in metrics:
            fig, ax = plt.subplots(figsize=(width or SINGLE_COL_IN, st["height"]))
            log = draw(ax, style, runs, labels, METRICS[name], floor=floors[name])
            lo, top = ax.get_ylim()
            if not METRICS[name].pct:
                ax.set_ylim(lo, top * (8 if log else 1.25))      # room for the legend inside
            if style == "classic" and METRICS[name].pct:
                ax.legend(**{**st["legend_one"], "loc": "best"})
            else:
                ax.legend(**st["legend_one"])
            fig.tight_layout(pad=0.3)
            paths.append(_save(fig, out_dir / f"{name}.{fmt}"))
    return paths


__all__ = ["METRICS", "CORE", "STYLES", "default_metrics", "plot_row", "plot_paper"]
