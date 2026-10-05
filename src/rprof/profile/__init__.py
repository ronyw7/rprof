"""Profile loading and the resolved schedule.

``load_profile`` parses and validates a YAML profile and returns a ``Profile``
whose segments carry both raw and canonical values. All time lookups go through
``Profile.limits_at`` so the scheduler, agent view and reports agree.
"""

from __future__ import annotations

import bisect
import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .. import knobs as K
from .schema import NAME_PATTERN, NAME_RULE, ProfileModel


class ProfileError(Exception):
    """Invalid profile; ``problems`` is a list of (path, message)."""

    def __init__(self, problems: list[tuple[str, str]]):
        self.problems = problems
        super().__init__("\n".join(f"{p}: {m}" if p else m for p, m in problems))


@dataclass
class Segment:
    index: int                    # 1-based, profile order
    t0: float
    t1: float
    label: str | None
    raw: dict[str, Any]           # knobs this segment sets -> raw YAML value
    values: dict[str, Any]        # same knobs -> canonical value

    def contains(self, t: float) -> bool:
        return self.t0 <= t < self.t1


@dataclass
class Profile:
    name: str
    version: int = 1
    clock: str = "wall"
    visibility: str = "none"
    source: dict = field(default_factory=dict)
    defaults_raw: dict[str, Any] = field(default_factory=dict)   # full: every knob
    defaults: dict[str, Any] = field(default_factory=dict)       # full: every knob, canonical
    explicit: set[str] = field(default_factory=set)              # knobs named anywhere in the file
    segments: list[Segment] = field(default_factory=list)
    path: str | None = None

    # ------------------------------------------------------------ lookup
    def active(self, t: float) -> list[Segment]:
        return [s for s in self.segments if s.contains(t)]

    def segment_at(self, t: float) -> tuple[int, list[int]]:
        """(segment number, all active numbers); 0 means defaults only."""
        act = [s.index for s in self.active(t)]
        return (min(act) if act else 0), act

    def limits_at(self, t: float) -> dict[str, Any]:
        out = dict(self.defaults)
        for s in self.active(t):
            out.update(s.values)
        return out

    def raw_at(self, t: float) -> dict[str, Any]:
        out = dict(self.defaults_raw)
        for s in self.active(t):
            out.update(s.raw)
        return out

    def boundaries(self) -> list[float]:
        b = sorted({x for s in self.segments for x in (s.t0, s.t1)})
        return b

    def end(self) -> float | None:
        return max((s.t1 for s in self.segments), default=None)

    def next_boundary(self, t: float) -> float | None:
        b = self.boundaries()
        i = bisect.bisect_right(b, t)
        return b[i] if i < len(b) else None

    def intervals(self, t_end: float | None = None) -> list[tuple[float, float | None]]:
        """Piecewise-constant intervals [t0, t1) from 0; the last one is open (None)."""
        pts = [0.0] + [x for x in self.boundaries() if x > 0]
        if t_end is not None:
            pts = [x for x in pts if x < t_end] + [t_end]
            return list(zip(pts[:-1], pts[1:]))
        return list(zip(pts, pts[1:] + [None]))  # type: ignore[arg-type]

    def changes_from_defaults(self, t: float) -> dict[str, Any]:
        """Raw values in force at t that differ from the defaults (agent view 'changes')."""
        lim, raw = self.limits_at(t), self.raw_at(t)
        return {k: raw[k] for k in raw if lim.get(k) != self.defaults.get(k)}

    def managed_knobs(self) -> set[str]:
        """Knobs rprof writes: those named explicitly anywhere in the profile.

        Knobs a profile never mentions are left exactly as the container had them.
        """
        return {k for k in self.explicit if K.is_unified(k) or K.KNOBS[k].enforced}

    # ------------------------------------------------------------ output
    def resolved_dict(self) -> dict:
        """The profile as applied, defaults filled in (written to runs/<id>/profile.yaml)."""
        def nest(flat: dict[str, Any]) -> dict:
            d: dict[str, Any] = {}
            for name, v in flat.items():
                if K.is_unified(name):
                    d.setdefault("unified", {})[name[len(K.UNIFIED_PREFIX):]] = v
                else:
                    g, k = name.split(".")
                    d.setdefault(g, {})[k] = v
            return d

        segs = []
        for s in self.segments:
            sd: dict[str, Any] = {"from": s.t0, "to": s.t1}
            if s.label:
                sd["label"] = s.label
            sd.update(nest(s.raw))
            segs.append(sd)
        return {"version": self.version, "name": self.name, "clock": self.clock,
                "visibility": self.visibility, "source": copy.deepcopy(self.source),
                "defaults": nest(self.defaults_raw), "segments": segs}

    def dump_yaml(self) -> str:
        return yaml.safe_dump(self.resolved_dict(), sort_keys=False, default_flow_style=None, width=100)


# ---------------------------------------------------------------- loading

def _loc(loc: tuple) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out


def _pydantic_problems(err: ValidationError) -> list[tuple[str, str]]:
    probs = []
    for e in err.errors():
        loc = tuple(p for p in e["loc"] if not (isinstance(p, str) and p.startswith("function-")))
        msg = e["msg"]
        if e["type"] == "extra_forbidden":
            msg = "unknown key"
        elif e["type"] == "missing":
            msg = "required"
        elif msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        elif e["type"] == "literal_error":
            msg = f"{e.get('input')!r} is not allowed; {msg[0].lower()}{msg[1:]}"
        elif e["type"] == "string_pattern_mismatch" and loc and loc[-1] == "name":
            msg = f"{e.get('input')!r} {NAME_RULE}"
        probs.append((_loc(loc), msg))
    return probs


def profile_from_dict(data: Any, path: str | None = None) -> Profile:
    if not isinstance(data, dict):
        raise ProfileError([("", "profile must be a YAML mapping")])
    try:
        m = ProfileModel.model_validate(data)
    except ValidationError as e:
        raise ProfileError(_pydantic_problems(e)) from None

    problems: list[tuple[str, str]] = []
    explicit: set[str] = set()

    d_raw = K.default_raw()
    d_flat = m.defaults.flat_raw()
    explicit.update(d_flat)
    d_raw.update(d_flat)
    defaults = {k: (K.KNOBS[k].parse(v) if not K.is_unified(k) else v) for k, v in d_raw.items()}

    segs: list[Segment] = []
    for i, sm in enumerate(m.segments):
        raw = sm.flat_raw()
        explicit.update(raw)
        if sm.to <= sm.from_:
            problems.append((f"segments[{i}].to", f"{sm.to:g} must be greater than from ({sm.from_:g})"))
        vals = {k: (K.KNOBS[k].parse(v) if not K.is_unified(k) else v) for k, v in raw.items()}
        segs.append(Segment(i + 1, float(sm.from_), float(sm.to), sm.label, raw, vals))

    # Same-knob overlap check.
    for a in range(len(segs)):
        for b in range(a + 1, len(segs)):
            sa, sb = segs[a], segs[b]
            if sa.t0 < sb.t1 and sb.t0 < sa.t1:
                common = sorted(set(sa.raw) & set(sb.raw))
                if common:
                    problems.append((f"segments[{b}]",
                                     f"overlaps segments[{a}] ({sa.t0:g}–{sa.t1:g} s) and both set "
                                     + ", ".join(common)))

    # CPU quota sanity: the kernel needs quota >= 1 ms.
    def _check_cpu(path: str, lim: dict):
        cores, period = lim.get("cpu.cores"), lim.get("cpu.period", 100)
        if cores is not None and cores * period < 1:
            problems.append((path, f"cpu.cores {cores:g} × period {period:g} ms is below the kernel's 1 ms "
                                   "minimum quota"))
    for s in segs:
        lim = dict(defaults)
        lim.update(s.values)
        _check_cpu(f"segments[{s.index - 1}].cpu.cores", lim)
    _check_cpu("defaults.cpu.cores", defaults)

    if problems:
        raise ProfileError(problems)
    return Profile(name=m.name, version=m.version, clock=m.clock, visibility=m.visibility,
                   source=m.source, defaults_raw=d_raw, defaults=defaults, explicit=explicit,
                   segments=segs, path=path)


def load_profile(path: str | Path) -> Profile:
    p = Path(path)
    try:
        text = p.read_text()
    except OSError as e:
        raise ProfileError([(str(p), f"cannot read: {e.strerror}")]) from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ProfileError([(str(p), f"YAML error: {e}")]) from None
    return profile_from_dict(data, str(p))


def check_name(name: str) -> str | None:
    """None if ``name`` is a valid profile name or run label, else the problem."""
    import re
    return None if re.match(NAME_PATTERN, name) else f"{name!r} {NAME_RULE}"


def unlimited_profile(name: str = "unlimited") -> Profile:
    return profile_from_dict({"version": 1, "name": name})
