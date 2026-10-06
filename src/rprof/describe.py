"""What to tell an agent about its resources under a profile, in sentences.

``rprof describe PROFILE`` prints it, and ``rprof run --target harbor --tell-agent`` appends it to
the task's instruction, so every trial is told the same things in the same words, straight from
the profile that is enforced. The profile's ``visibility`` decides how much is said: ``full``
gives the whole schedule, ``current`` only the limits at the start, ``none`` nothing.

Disk bandwidth the profile doesn't limit is described as not throttled, with the native speed
``rprof selftest`` measured on this host (``features.disk_bandwidth`` in capabilities.json). Swap is
the smaller of what the profile allows and what the host has (``features.swap_bytes``), and is left
out if the host's swap isn't known.
"""

from __future__ import annotations

from typing import Any

from . import knobs as K
from . import units as u
from .profile import Profile

# Closes every description: what the agent should do with it, as in the agentic-host-os CompCert
# experiment, with its list of steps made examples so it fits any task.
CLOSING = ("Be aware of these resource limitations, and feel free to optimize how you carry out the task under "
           "these circumstances. Optimize for the total end-to-end duration of the entire task (such as installing "
           "dependencies, building, and verifying), given these resource limits.")

IO_KNOBS = ("io.rbps", "io.wbps", "io.riops", "io.wiops")


def _speed(bps: float) -> str:
    return f"{bps / 1e9:.1f} GB/s" if bps >= 1e9 else f"{bps / 1e6:.0f} MB/s"


def _disk(lim: dict[str, Any], measured: dict | None) -> str:
    parts = []
    for knob, what in (("io.wbps", "writes"), ("io.rbps", "reads")):
        if lim.get(knob) is not None:
            parts.append(f"disk {what} limited to {u.fmt_bytes(lim[knob])}/s")
    for knob, what in (("io.wiops", "writes"), ("io.riops", "reads")):
        if lim.get(knob) is not None:
            parts.append(f"disk {what} limited to {lim[knob]} operations/s")
    if parts:
        return ", ".join(parts)
    if measured and measured.get("write_bps") and measured.get("read_bps"):
        return (f"disk bandwidth that is not throttled (about {_speed(measured['write_bps'])} write, "
                f"{_speed(measured['read_bps'])} read)")
    return "disk bandwidth that is not throttled"


def resources(lim: dict[str, Any], features: dict | None = None) -> str:
    """The resources in force under ``lim`` (canonical knob values), as one phrase. ``features`` are
    the host's, from capabilities.json."""
    features = features or {}
    full = K.defaults()
    full.update(lim)
    parts = []
    if full.get("cpu.cpus") is not None:
        n = u.cpuset_size(full["cpu.cpus"])
        parts.append(f"{n} CPU{'s' if n != 1 else ''}")
    if full.get("cpu.cores") is not None:
        c = full["cpu.cores"]
        parts.append(f"{'CPU time limited to ' if parts else ''}{u.fmt_num(c)} CPU core{'s' if c != 1 else ''}")
    hi, mx = full.get("mem.high"), full.get("mem.max")
    if mx is not None:
        mem = f"{u.fmt_bytes(mx)} of memory (processes that go above it are killed"
        mem += f"; above {u.fmt_bytes(hi)} they are slowed down)" if hi is not None and hi < mx else ")"
        parts.append(mem)
    elif hi is not None:
        parts.append(f"{u.fmt_bytes(hi)} of memory before processes are slowed down")
    if full.get("mem.swap_max") and features.get("swap_bytes"):
        # An allowance beyond the host's swap isn't swap the agent can use.
        swap = min(full["mem.swap_max"], features["swap_bytes"])
        parts.append(f"{u.fmt_bytes(swap)} of swap")
    if full.get("pids.max") is not None:
        parts.append(f"at most {full['pids.max']} processes")
    if full.get("disk.capacity") is not None:
        parts.append(f"{u.fmt_bytes(full['disk.capacity'])} of disk space")
    parts.append(_disk(full, features.get("disk_bandwidth")))
    for knob in ("net.rate", "net.delay", "net.jitter", "net.loss", "net.partition"):
        d = K.describe(knob, full.get(knob))
        if d:
            parts.append(d)
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _span(t0: float, t1: float | None) -> str:
    return f"after {u.fmt_num(t0)} s" if t1 is None else f"{u.fmt_num(t0)}–{u.fmt_num(t1)} s"


def _cli_size(b: int) -> str:
    return f"{b >> 30}G" if b % (1 << 30) == 0 else f"{b >> 20}M"


def _span_words(s: float) -> str:
    if s % 3600 == 0:
        return f"{u.fmt_num(s / 3600)} h"
    if s % 60 == 0:
        return f"{u.fmt_num(s / 60)} min"
    return f"{u.fmt_num(s)} s"


def leases(profile: Profile) -> str:
    """How the agent leases more than its baseline, with the profile's ceiling in the example."""
    from .lease import phrase
    ceiling = profile.lease_max
    example = ["lease request"]
    if "cpu.cores" in ceiling:
        example.append(f"--cpus {u.fmt_num(ceiling['cpu.cores'])}")
    if "mem.max" in ceiling:
        example.append(f"--mem {_cli_size(ceiling['mem.max'])}")
    example.append("--for 10m")
    return (f"You can lease more while you need it, up to {phrase(ceiling)} in total, with the `lease` command "
            f"(`lease --help` explains it): `{' '.join(example)}` raises your limits to those totals until the "
            "lease ends, `lease release` gives them back, and `lease status` shows what you hold. A lease is "
            f"granted at once and lasts at most {_span_words(profile.lease_max_duration)}; when it ends, your "
            "limits return to the baseline, and processes then using more memory than the baseline are killed. "
            "Leased resources come from a pool shared with other jobs on this machine: lease only what you need, "
            "for as long as you need it.")


def describe(profile: Profile, capabilities: dict | None = None) -> str | None:
    """The text for ``profile``; None if its visibility is ``none``."""
    if profile.visibility == "none":
        return None
    features = (capabilities or {}).get("features") or {}
    if profile.lease_max:
        return (f"Resource environment: your container has {resources(profile.limits_at(0.0), features)}. "
                f"{leases(profile)} {CLOSING}")
    intervals = profile.intervals()
    changes = profile.visibility == "full" and len(intervals) > 1
    if not changes:
        text = f"Resource environment: your container has {resources(profile.limits_at(0.0), features)}."
        if profile.visibility == "current" and len(intervals) > 1:
            text += " These limits may change while you work."
        return text + " " + CLOSING
    lines = ["Resource environment: your container's resources change over time. Time 0 is when you "
             "receive this task: run `date` now and keep track of the time."]
    for t0, t1 in intervals:
        lines.append(f"- {_span(t0, t1)}: {resources(profile.limits_at(t0), features)}.")
    lines.append(CLOSING)
    return "\n".join(lines)


__all__ = ["describe", "resources"]
