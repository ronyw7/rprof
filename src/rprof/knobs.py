"""The knob registry: one entry per profile knob (design Appendix A.1).

Internally a set of limits is a flat ``{knob: canonical value}`` dict, e.g.
``{"mem.max": 1073741824, "cpu.cores": 0.5}``. ``limits_json`` turns it into the
nested limits object of the control protocol (Appendix A.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import units as u


@dataclass(frozen=True)
class Knob:
    name: str            # "mem.max"
    group: str           # "mem"
    key: str             # key in the YAML group: "max"
    json_key: str        # key in the limits JSON group: "max" / "period_ms" / "rate_bps"
    parse: Callable[[Any], Any]
    default_raw: Any     # YAML default
    controller: str      # cpu | memory | io | pids | net | disk | harness | unified
    # True when the default value means "rprof installs nothing" (no limit).
    enforced: bool = True

    @property
    def default(self):
        return self.parse(self.default_raw)


def _period(v):
    ms = u.parse_duration(v, unit="ms")
    if not 1 <= ms <= 1000:
        raise u.UnitError(f"{v} must be between 1ms and 1s")
    return ms


def _delay(v):
    ms = u.parse_duration(v, unit="ms")
    if ms < 0:
        raise u.UnitError(f"{v} must be >= 0")
    return ms


KNOBS: dict[str, Knob] = {}


def _k(name, json_key, parse, default_raw, controller, enforced=True):
    group, key = name.split(".")
    KNOBS[name] = Knob(name, group, key, json_key, parse, default_raw, controller, enforced)


_k("cpu.cores", "cores", u.parse_cores, "max", "cpu")
_k("cpu.cpus", "cpus", u.parse_cpuset, "all", "cpu")
_k("cpu.period", "period_ms", _period, "100ms", "cpu")
_k("mem.high", "high", u.parse_bytes, "max", "memory")
_k("mem.max", "max", u.parse_bytes, "max", "memory")
_k("mem.swap_max", "swap_max", lambda v: u.parse_bytes(v, allow_zero=True), 0, "memory")
_k("io.rbps", "rbps", u.parse_bytes, "max", "io")
_k("io.wbps", "wbps", u.parse_bytes, "max", "io")
_k("io.riops", "riops", u.parse_count, "max", "io")
_k("io.wiops", "wiops", u.parse_count, "max", "io")
_k("pids.max", "max", u.parse_count, "max", "pids")
_k("net.rate", "rate_bps", u.parse_rate, "max", "net")
_k("net.delay", "delay_ms", _delay, "0ms", "net")
_k("net.jitter", "jitter_ms", _delay, "0ms", "net")
_k("net.loss", "loss_pct", u.parse_percent, "0%", "net")
_k("net.partition", "partition", lambda v: u.parse_enum(v, ("none", "reject", "drop")), "none", "net")
_k("net.allow", "allow", u.parse_cidrs, [], "net")
_k("disk.capacity", "capacity", u.parse_bytes, "max", "disk")
_k("harness.deadline", "deadline_s", u.parse_deadline, "none", "harness", enforced=False)
_k("harness.feedback", "feedback", lambda v: u.parse_enum(v, ("none", "errno", "explain")),
   "errno", "harness", enforced=False)

GROUPS = ("cpu", "mem", "io", "pids", "net", "disk", "harness")
GROUP_KEYS = {g: [k.key for k in KNOBS.values() if k.group == g] for g in GROUPS}
UNIFIED_PREFIX = "unified."


def knob(name: str) -> Knob | None:
    return KNOBS.get(name)


def is_unified(name: str) -> bool:
    return name.startswith(UNIFIED_PREFIX)


def defaults() -> dict[str, Any]:
    return {k.name: k.default for k in KNOBS.values()}


def default_raw() -> dict[str, Any]:
    return {k.name: k.default_raw for k in KNOBS.values()}


def is_default(name: str, value) -> bool:
    if is_unified(name):
        return False
    return KNOBS[name].default == value


def limits_json(flat: dict[str, Any]) -> dict:
    """Flat canonical limits -> nested limits object (Appendix A.2)."""
    out: dict[str, dict] = {g: {} for g in GROUPS}
    full = defaults()
    full.update(flat)
    for name, val in full.items():
        if is_unified(name):
            out.setdefault("unified", {})[name[len(UNIFIED_PREFIX):]] = val
            continue
        k = KNOBS[name]
        out[k.group][k.json_key] = val
    return out


# ---------------------------------------------------------------- display

def describe(name: str, value) -> str | None:
    """One knob value as agent-facing text; None when it imposes no limit."""
    if is_unified(name):
        return f"cgroup {name[len(UNIFIED_PREFIX):]}={value}"
    if is_default(name, value) and name not in ("cpu.period",):
        return None
    if name == "cpu.cores":
        return f"cpu {u.fmt_num(value)} core{'s' if value != 1 else ''}"
    if name == "cpu.cpus":
        n = u.cpuset_size(value)
        return f"{n} CPU{'s' if n != 1 else ''}"
    if name == "cpu.period":
        return None if value == 100 else f"cpu period {u.fmt_num(value)} ms"
    if name == "mem.swap_max":
        return f"swap {u.fmt_bytes(value)}" if value is not None else None
    if name in ("io.rbps", "io.wbps"):
        return f"disk {'read' if name == 'io.rbps' else 'write'} {u.fmt_bytes(value)}/s"
    if name in ("io.riops", "io.wiops"):
        return f"disk {'read' if name == 'io.riops' else 'write'} {value} IOPS"
    if name == "pids.max":
        return f"max {value} processes"
    if name == "net.rate":
        return f"network {u.fmt_rate_bits(value)}"
    if name == "net.delay":
        return f"network delay {u.fmt_num(value)} ms"
    if name == "net.jitter":
        return f"network jitter {u.fmt_num(value)} ms"
    if name == "net.loss":
        return f"network loss {u.fmt_num(value)}%"
    if name == "net.partition":
        return f"network blocked ({value})"
    if name == "net.allow":
        return None
    if name == "disk.capacity":
        return f"disk space {u.fmt_bytes(value)}"
    if name == "harness.deadline":
        return f"deadline {u.fmt_num(value)} s per call"
    if name == "harness.feedback":
        return None
    return f"{name} {value}"


def describe_limits(flat: dict[str, Any]) -> list[str]:
    """All limiting knobs as short phrases, memory merged as in the design's now.txt."""
    full = defaults()
    full.update(flat)
    parts: list[str] = []
    hi, mx = full.get("mem.high"), full.get("mem.max")
    if mx is not None and hi is not None:
        parts.append(f"memory {u.fmt_bytes(mx)} hard ({u.fmt_bytes(hi)} soft)")
    elif mx is not None:
        parts.append(f"memory {u.fmt_bytes(mx)} hard")
    elif hi is not None:
        parts.append(f"memory {u.fmt_bytes(hi)} soft")
    order = ["pids.max", "cpu.cores", "cpu.cpus", "cpu.period", "mem.swap_max", "io.rbps", "io.wbps",
             "io.riops", "io.wiops", "net.partition", "net.rate", "net.delay", "net.jitter", "net.loss",
             "disk.capacity", "harness.deadline"]
    for name in order:
        d = describe(name, full[name])
        if d:
            parts.append(d)
    for name in sorted(n for n in full if is_unified(n)):
        parts.append(describe(name, full[name]) or name)
    return parts
