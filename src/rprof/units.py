"""Value grammar for profile knobs (design Appendix A.1).

Every parser takes the raw YAML value and returns the canonical JSON value used in
limits objects (bytes as int, durations in the unit named by the caller, ``None``
for unlimited). Parsers raise ``UnitError`` with a message that reads well after
``<path>: `` in ``rprof validate`` output.
"""

from __future__ import annotations

import ipaddress
import math
import re

MAX = "max"
NONE = "none"


class UnitError(ValueError):
    pass


_BYTE_SUFFIX = {
    "": 1,
    "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
}
_BYTES_RE = re.compile(r"^(\d+)\s*(Ki|Mi|Gi|Ti|K|M|G|T)?$")
_DUR_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(ms|s|m)$")
_PCT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*%$")
_CPUSET_RE = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")
# tc(8) rate units: *bit are bits/s, *bps are bytes/s; SI (k=1000) and IEC (ki=1024).
_RATE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgt]i?)?(bit|bps)$", re.IGNORECASE)
_RATE_MULT = {"": 1, "k": 1000, "m": 1000**2, "g": 1000**3, "t": 1000**4,
              "ki": 1024, "mi": 1024**2, "gi": 1024**3, "ti": 1024**4}


def _fmt(v) -> str:
    return str(v)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_bytes(v, *, allow_zero: bool = False, allow_max: bool = True) -> int | None:
    """``512Mi`` -> 536870912; ``max`` -> None."""
    if allow_max and v == MAX:
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        n = v
    elif isinstance(v, str):
        m = _BYTES_RE.match(v.strip())
        if not m:
            raise UnitError(f"{v} is not a byte size")
        n = int(m.group(1)) * _BYTE_SUFFIX[m.group(2) or ""]
    else:
        raise UnitError(f"{_fmt(v)} is not a byte size")
    if n < 0 or (n == 0 and not allow_zero):
        raise UnitError(f"{_fmt(v)} must be {'>= 0' if allow_zero else '> 0'}")
    return n


def parse_duration(v, *, unit: str = "s") -> float:
    """``100ms`` -> 0.1 (unit='s') or 100.0 (unit='ms')."""
    if not isinstance(v, str):
        raise UnitError(f"{_fmt(v)} is not a duration (use ms, s or m)")
    m = _DUR_RE.match(v.strip())
    if not m:
        raise UnitError(f"{v} is not a duration (use ms, s or m)")
    x = float(m.group(1))
    secs = {"ms": x / 1000, "s": x, "m": x * 60}[m.group(2)]
    return secs * 1000 if unit == "ms" else secs


def parse_percent(v) -> float:
    if not isinstance(v, str):
        raise UnitError(f"{_fmt(v)} is not a percentage (write e.g. 30%)")
    m = _PCT_RE.match(v.strip())
    if not m:
        raise UnitError(f"{v} is not a percentage (write e.g. 30%)")
    x = float(m.group(1))
    if x > 100:
        raise UnitError(f"{v} must be between 0% and 100%")
    return x


def parse_cores(v) -> float | None:
    if v == MAX:
        return None
    if not _is_num(v):
        raise UnitError(f"{_fmt(v)} is not a core count (a number > 0, or max)")
    if v <= 0 or not math.isfinite(v):
        raise UnitError(f"{_fmt(v)} must be > 0")
    return float(v)


def parse_cpuset(v) -> str | None:
    if v == "all":
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str) or not _CPUSET_RE.match(v.replace(" ", "")):
        raise UnitError(f"{_fmt(v)} is not a cpuset list (e.g. 0-3,6, or all)")
    s = v.replace(" ", "")
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            if int(a) > int(b):
                raise UnitError(f"{v}: range {part} is reversed")
    return s


def cpuset_size(s: str) -> int:
    n = 0
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            n += int(b) - int(a) + 1
        else:
            n += 1
    return n


def parse_count(v, *, minimum: int = 1) -> int | None:
    if v == MAX:
        return None
    if not isinstance(v, int) or isinstance(v, bool):
        raise UnitError(f"{_fmt(v)} is not an integer (or max)")
    if v < minimum:
        raise UnitError(f"{v} must be >= {minimum}")
    return v


def parse_rate(v) -> int | None:
    """tc rate -> bits per second; ``max`` -> None."""
    if v == MAX:
        return None
    if not isinstance(v, str):
        raise UnitError(f"{_fmt(v)} is not a tc rate (e.g. 10mbit, 500kbit, or max)")
    m = _RATE_RE.match(v.strip())
    if not m:
        raise UnitError(f"{v} is not a tc rate (e.g. 10mbit, 500kbit, or max)")
    x = float(m.group(1)) * _RATE_MULT[(m.group(2) or "").lower()]
    if m.group(3).lower() == "bps":
        x *= 8
    if x <= 0:
        raise UnitError(f"{v} must be > 0")
    return int(round(x))


def parse_deadline(v) -> float | None:
    if v == NONE:
        return None
    d = parse_duration(v)
    if d <= 0:
        raise UnitError(f"{v} must be > 0")
    return d


def parse_enum(v, allowed: tuple[str, ...]) -> str:
    if v not in allowed:
        raise UnitError(f"{_fmt(v)} is not one of {', '.join(allowed)}")
    return v


def parse_cidrs(v) -> list[str]:
    if not isinstance(v, list):
        raise UnitError("must be a list of CIDRs")
    out = []
    for i, c in enumerate(v):
        try:
            out.append(str(ipaddress.ip_network(str(c), strict=False)))
        except ValueError:
            raise UnitError(f"{c} is not a CIDR") from None
    return out


# ---------------------------------------------------------------- formatting

def fmt_bytes(n: int | None) -> str:
    """Human IEC size: 1073741824 -> '1 GiB', 838860800 -> '800 MiB'."""
    if n is None:
        return "unlimited"
    for unit, mult in (("TiB", 1024**4), ("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if n >= mult:
            x = n / mult
            if x >= 100 or abs(x - round(x)) < 0.05:
                return f"{round(x):.0f} {unit}"
            return f"{x:.1f} {unit}"
    return f"{n} B"


def fmt_rate_bits(bps: int | None) -> str:
    if bps is None:
        return "unlimited"
    for unit, mult in (("Gbit/s", 1e9), ("Mbit/s", 1e6), ("kbit/s", 1e3)):
        if bps >= mult:
            x = bps / mult
            return f"{x:.0f} {unit}" if abs(x - round(x)) < 0.05 else f"{x:.1f} {unit}"
    return f"{bps} bit/s"


def fmt_num(x: float) -> str:
    return f"{x:g}"


def fmt_seconds(x: float) -> str:
    return f"{x:g} s"
