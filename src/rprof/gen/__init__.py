"""Optional profile generators (milestone M5).

Each generator returns a plain profile dict; ``dump`` renders it as YAML with
one flow-style line per segment so the output reads like a hand-written profile.
Output is deterministic: the same arguments, seed and rprof version give a
byte-identical file.
"""

from __future__ import annotations

import copy
import csv
import math
import random
import re
from pathlib import Path
from typing import Any

import yaml

from .. import __version__
from .. import knobs as K


def _coerce(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    s = v.strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d*", s):
        return float(s)
    return s


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")
    return s or "x"


def _check_knob(knob: str) -> K.Knob:
    k = K.knob(knob)
    if k is None:
        raise ValueError(f"unknown knob {knob!r}")
    return k


def _nest(knob: str, level: Any) -> dict:
    k = _check_knob(knob)
    return {k.group: {k.key: _coerce(level)}}


def _merge(dst: dict, src: dict) -> dict:
    for g, kv in src.items():
        dst.setdefault(g, {}).update(kv)
    return dst


def _t(x: float) -> float | int:
    x = round(x, 1)
    return int(x) if x == int(x) else x


def _header(name: str, visibility: str, source: dict) -> dict:
    return {"version": 1, "name": _slug(name), "clock": "wall", "visibility": visibility,
            "source": {**source, "rprof_version": __version__}}


def dump(d: dict) -> str:
    head = {k: v for k, v in d.items() if k != "segments"}
    text = yaml.safe_dump(head, sort_keys=False, default_flow_style=None, width=120)
    segs = d.get("segments") or []
    if segs:
        text += "segments:\n"
        for s in segs:
            text += "  - " + yaml.safe_dump(s, sort_keys=False, default_flow_style=True, width=10000).strip() + "\n"
    else:
        text += "segments: []\n"
    return text


def _valid(knob: str, level: Any) -> bool:
    try:
        _check_knob(knob).parse(_coerce(level))
        return True
    except Exception:  # noqa: BLE001
        return False


def _is_default(knob: str, level: Any) -> bool:
    k = _check_knob(knob)
    try:
        return k.parse(_coerce(level)) == k.default
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- simple shapes

def const(knob: str, level: str, duration: float, name: str | None = None, visibility: str = "none") -> dict:
    d = _header(name or f"const-{knob}-{level}", visibility,
                {"generator": "const", "knob": knob, "level": level, "duration": duration})
    seg = {"from": 0, "to": _t(duration)}
    seg.update(_nest(knob, level))
    d["segments"] = [seg]
    return d


def step(knob: str, level: str, start: float, end: float, name: str | None = None, visibility: str = "none") -> dict:
    if end <= start:
        raise ValueError("--to must be greater than --from")
    d = _header(name or f"step-{knob}-{level}", visibility,
                {"generator": "step", "knob": knob, "level": level, "from": start, "to": end})
    seg = {"from": _t(start), "to": _t(end)}
    seg.update(_nest(knob, level))
    d["segments"] = [seg]
    return d


def square(knob: str, low: str, high: str, period: float, duty: float, duration: float,
           name: str | None = None, visibility: str = "none") -> dict:
    if not 0 < duty < 1:
        raise ValueError("--duty must be between 0 and 1")
    if period <= 0:
        raise ValueError("--period must be > 0")
    d = _header(name or f"square-{knob}-{low}-{high}", visibility,
                {"generator": "square", "knob": knob, "low": low, "high": high, "period": period, "duty": duty,
                 "duration": duration})
    d["defaults"] = _nest(knob, high)
    segs = []
    t = 0.0
    while t < duration - 1e-9:
        a, b = t, min(t + duty * period, duration)
        if b > a:
            seg = {"from": _t(a), "to": _t(b)}
            seg.update(_nest(knob, low))
            segs.append(seg)
        t += period
    d["segments"] = segs
    return d


# ---------------------------------------------------------------- random

def random_profile(knobs: list[str], levels: dict[str, list[str]], duration: float, mean: float,
                   min_len: float, max_len: float, seed: int, name: str | None = None,
                   visibility: str = "none") -> dict:
    if min_len <= 0 or max_len < min_len:
        raise ValueError("need 0 < --min <= --max")
    rng = random.Random(seed)
    levels = {k: [lv for lv in levels.get(k, []) if _valid(k, lv)] for k in knobs}
    for k in knobs:
        _check_knob(k)
        if not levels.get(k):
            raise ValueError(f"no valid levels for {k}")
    d = _header(name or f"random-{'-'.join(knobs)}-s{seed}", visibility,
                {"generator": "random", "seed": seed, "knobs": list(knobs),
                 "levels": {k: list(levels[k]) for k in knobs}, "mean": mean, "min": min_len, "max": max_len,
                 "duration": duration})
    defaults: dict = {}
    for k in knobs:
        kn = _check_knob(k)
        _merge(defaults, {kn.group: {kn.key: kn.default_raw}})
    d["defaults"] = defaults
    segs = []
    for k in knobs:
        t = 0.0
        last: dict | None = None
        while t < duration - 1e-9:
            length = min(max(rng.expovariate(1.0 / mean), min_len), max_len)
            a, b = _t(t), _t(min(t + length, duration))
            level = rng.choice(levels[k])
            t += length
            if b <= a or _is_default(k, level):
                last = None
                continue
            nest = _nest(k, level)
            if last is not None and last["to"] == a and all(last.get(g) == v for g, v in nest.items()):
                last["to"] = b  # same level continues: one longer segment
                continue
            seg = {"from": a, "to": b}
            seg.update(nest)
            segs.append(seg)
            last = seg
    segs.sort(key=lambda s: (s["from"], s["to"], next(iter(s.keys() - {"from", "to"}))))
    d["segments"] = segs
    return d


# ---------------------------------------------------------------- trace

def _fmt_level(knob: str, x: float) -> Any:
    kn = _check_knob(knob)
    if knob == "cpu.cores":
        return max(0.01, round(x, 2))
    if knob == "net.rate":
        return f"{max(1000, int(round(x)))}bit"
    if knob in ("io.riops", "io.wiops", "pids.max"):
        return max(1, int(round(x)))
    if kn.parse is not None and knob in ("mem.high", "mem.max", "io.rbps", "io.wbps", "disk.capacity"):
        return max(1, int(round(x)))
    raise ValueError(f"trace generator does not support {knob}")


def _read_csv(path: Path) -> list[tuple[float, float]]:
    rows = []
    with open(path, newline="") as f:
        for r in csv.reader(f):
            if len(r) < 2:
                continue
            try:
                rows.append((float(r[0]), float(r[1])))
            except ValueError:
                continue  # header
    rows.sort()
    return rows


def _read_mahimahi(path: Path) -> list[tuple[float, float]]:
    """Mahimahi link trace: one ms timestamp per 1500-byte delivery opportunity -> bits/s per second."""
    counts: dict[int, int] = {}
    last = 0
    for line in Path(path).read_text().split():
        try:
            ms = int(line)
        except ValueError:
            continue
        counts[ms // 1000] = counts.get(ms // 1000, 0) + 1
        last = max(last, ms // 1000)
    return [(float(s), counts.get(s, 0) * 1500 * 8.0) for s in range(last + 1)]


def trace(path: Path, knob: str, capacity: str, time_scale: float = 1.0, merge: float = 0.05,
          min_len: float = 1.0, fmt: str = "csv", name: str | None = None, visibility: str = "none") -> dict:
    kn = _check_knob(knob)
    if fmt == "mahimahi":
        pts = _read_mahimahi(path)
        avail = [(t, v) for t, v in pts]          # the trace is the capacity itself
    else:
        cap = kn.parse(_coerce(capacity))
        if cap is None:
            raise ValueError("--capacity must be a finite value")
        pts = _read_csv(path)
        avail = [(t, max(cap * 0.01, cap - u)) for t, u in pts]
    if not avail:
        raise ValueError(f"{path}: no data rows")
    t0 = avail[0][0]
    steps = [((t - t0) * time_scale, v) for t, v in avail]
    end = steps[-1][0] + (steps[-1][0] - steps[-2][0] if len(steps) > 1 else time_scale)
    # Merge steps whose relative change is below `merge`, and enforce min_len.
    merged: list[list[float]] = []
    for (t, v), nxt in zip(steps, [s[0] for s in steps[1:]] + [end]):
        if merged and (abs(v - merged[-1][2]) <= merge * max(abs(merged[-1][2]), 1e-9)
                       or merged[-1][1] - merged[-1][0] < min_len):
            a, b, mv = merged[-1]
            w0, w1 = b - a, nxt - t
            merged[-1] = [a, nxt, (mv * w0 + v * w1) / (w0 + w1) if w0 + w1 > 0 else v]
        else:
            merged.append([t, nxt, v])
    d = _header(name or f"trace-{Path(path).stem}-{knob}", visibility,
                {"generator": "trace", "file": Path(path).name, "format": fmt, "knob": knob, "capacity": capacity,
                 "time_scale": time_scale, "merge": merge, "min": min_len})
    segs = []
    for a, b, v in merged:
        if b - a <= 0:
            continue
        seg = {"from": _t(a), "to": _t(b)}
        seg.update({kn.group: {kn.key: _fmt_level(knob, v)}})
        if segs and seg["from"] == segs[-1]["to"] and seg[kn.group] == segs[-1][kn.group]:
            segs[-1]["to"] = seg["to"]
            continue
        if seg["to"] > seg["from"]:
            segs.append(seg)
    d["segments"] = segs
    return d


# ---------------------------------------------------------------- sweep

def sweep(base: Path, knob: str, levels: list[str], out_dir: Path, start: float | None = None,
          end: float | None = None) -> list[Path]:
    kn = _check_knob(knob)
    src = yaml.safe_load(Path(base).read_text())
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for lv in levels:
        d = copy.deepcopy(src)
        d["name"] = _slug(f"{src.get('name', 'profile')}-{knob}-{lv}")
        d.setdefault("source", {})
        d["source"] = {**(d["source"] or {}), "generator": "sweep", "base": Path(base).name, "knob": knob,
                       "level": lv, "rprof_version": __version__}
        if start is None:
            d["defaults"] = _merge(d.get("defaults") or {}, _nest(knob, lv))
        else:
            if end is None or end <= start:
                raise ValueError("--to must be greater than --from")
            segs = []
            for s in d.get("segments") or []:
                if s.get("from", 0) < end and start < s.get("to", math.inf) and kn.key in (s.get(kn.group) or {}):
                    s = copy.deepcopy(s)
                    del s[kn.group][kn.key]
                    if not s[kn.group]:
                        del s[kn.group]
                    if not set(s) - {"from", "to", "label"}:
                        continue  # nothing left in this segment
                segs.append(s)
            seg = {"from": _t(start), "to": _t(end)}
            seg.update(_nest(knob, lv))
            segs.append(seg)
            segs.sort(key=lambda s: (s.get("from", 0), s.get("to", 0)))
            d["segments"] = segs
        p = out_dir / f"{d['name']}.yaml"
        p.write_text(dump(d))
        paths.append(p)
    return paths
