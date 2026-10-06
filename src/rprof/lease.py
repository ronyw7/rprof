"""Leases: the agent asks for more CPU and memory while it needs them, and gives them back.

A profile with a ``leases`` section sets a baseline (its ``defaults``) and the most the agent may
hold (``leases.max``, as totals). In the sandbox, the ``lease`` command asks rprof for limits up to
that ceiling for a while::

    lease request --cpus 4 --mem 8G --for 10m     # these totals until the lease ends
    lease release                                  # back to the baseline now
    lease status

One lease at a time: a new request replaces the current lease, larger or smaller. A knob the
request leaves out stays at the baseline. Requests are granted at once (there is one agent and the
ceiling is its own), capped at ``leases.max`` and ``leases.max_duration``.

When a lease ends (release, expiry, or a smaller lease replacing it), CPU returns at once. Memory
returns in two steps: ``memory.high`` drops to the new limit, so the kernel reclaims and throttles
what is above it, and ``grace`` seconds later ``memory.max`` drops too, when processes still above
it are OOM-killed.

The command reaches rprof through files in the sandbox, under ``/run/rprof-lease``: it writes a
request into ``req/`` and waits for the answer in ``resp/``. rprof reads and writes them through
``/proc/<pid>/root``, so nothing has to be mounted when the sandbox starts; rprof installs the
command (``/usr/local/bin/lease``, POSIX sh) when its run starts. Every request, grant and end is an
event (``lease_*``) in ``events.jsonl``.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import units as u

LEASABLE = ("cpu.cores", "mem.max")      # what a lease can raise; mem.high follows mem.max
DIR = "run/rprof-lease"                   # in the sandbox, relative to its root
BIN = "usr/local/bin/lease"
DEFAULT_DURATION = 600.0
POLL_S = 0.2

SCRIPT = r'''#!/bin/sh
# lease: hold more CPU and memory for a while, or give it back. Installed by rprof.
D="${RPROF_LEASE_DIR:-/run/rprof-lease}"
usage() {
  cat <<'EOF'
Usage:
  lease request [--cpus N] [--mem SIZE] [--for DURATION]
      Hold these totals (not increments) until the lease ends. What you leave out stays at
      the baseline. SIZE like 512M or 8G (binary units); DURATION like 90s, 10m or 1h
      (default 10m). A new request replaces your current lease, larger or smaller.
  lease release      Give the lease back now: limits return to the baseline.
  lease status       Your baseline, the most you can lease, and the lease you hold.

When a lease ends, CPU returns to the baseline at once. Memory above the baseline is
reclaimed, and processes still using more than the baseline shortly after are killed.
EOF
}
op=; cpus=; mem=; dur=
case "$1" in
  request) op=request; shift
    while [ $# -gt 0 ]; do
      case "$1" in
        --cpus) cpus="$2"; shift 2 ;;
        --cpus=*) cpus="${1#*=}"; shift ;;
        --mem|--memory) mem="$2"; shift 2 ;;
        --mem=*|--memory=*) mem="${1#*=}"; shift ;;
        --for|--duration) dur="$2"; shift 2 ;;
        --for=*|--duration=*) dur="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "lease: unknown option $1" >&2; usage >&2; exit 2 ;;
      esac
    done ;;
  release|status) op="$1" ;;
  -h|--help|help|"") usage; exit 0 ;;
  *) echo "lease: unknown command $1" >&2; usage >&2; exit 2 ;;
esac
id="$$.$(date +%s).$(od -An -N4 -tu4 /dev/urandom 2>/dev/null | tr -d ' ')"
if ! { printf 'op=%s\ncpus=%s\nmem=%s\nfor=%s\n' "$op" "$cpus" "$mem" "$dur" > "$D/req/.$id" &&
       mv "$D/req/.$id" "$D/req/$id"; }; then
  echo "lease: cannot reach the resource controller ($D)" >&2; exit 1
fi
i=0
while [ ! -f "$D/resp/$id" ]; do
  i=$((i + 1))
  if [ "$i" -gt 300 ]; then echo "lease: no answer from the resource controller" >&2; exit 1; fi
  sleep 0.1
done
status=$(head -n 1 "$D/resp/$id")
tail -n +2 "$D/resp/$id"
[ "$status" = ok ]
'''


class LeaseError(ValueError):
    pass


_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([kmgt])(?:i?b?)\s*$", re.I)
_DUR_PART = re.compile(r"([0-9]*\.?[0-9]+)\s*([hms]?)", re.I)


def parse_size(v: str) -> int:
    """``512M``, ``8G``, ``8GiB``, ``1.5g``: binary units. A bare number is refused (which unit?)."""
    m = _SIZE_RE.match(v)
    if not m:
        raise LeaseError(f"{v!r} is not a size: write it like 512M or 8G")
    return int(float(m.group(1)) * (1 << (10 * "kmgt".index(m.group(2).lower()) + 10)))


def parse_seconds(v: str) -> float:
    """``600``, ``90s``, ``10m``, ``1h``, ``1h30m``."""
    s = v.strip().lower().replace(" ", "")
    parts = _DUR_PART.findall(s)
    if not s or "".join(a + b for a, b in parts) != s:
        raise LeaseError(f"{v!r} is not a duration: write it like 90s, 10m or 1h")
    return sum(float(x) * {"h": 3600, "m": 60, "s": 1, "": 1}[unit] for x, unit in parts)


def phrase(values: dict[str, Any]) -> str:
    """``4 CPU cores and 8 GiB of memory``."""
    parts = []
    if values.get("cpu.cores") is not None:
        c = values["cpu.cores"]
        parts.append(f"{u.fmt_num(c)} CPU core{'s' if c != 1 else ''}")
    if values.get("mem.max") is not None:
        parts.append(f"{u.fmt_bytes(values['mem.max'])} of memory")
    return " and ".join(parts) or "nothing"


@dataclass
class Lease:
    id: str
    values: dict[str, Any]         # totals granted, per leasable knob
    asked: dict[str, Any]
    t_start: float
    t_end: float                   # when it expires
    ended: float | None = None
    reason: str | None = None      # released, expired, replaced

    def record(self) -> dict:
        return {"id": self.id, "values": self.values, "asked": self.asked, "t_start": round(self.t_start, 3),
                "t_expires": round(self.t_end, 3), "t_ended": None if self.ended is None else round(self.ended, 3),
                "reason": self.reason}


@dataclass
class LeaseManager:
    """Grants, replaces and ends leases on one sandbox. ``apply`` sets knob values on its cgroup."""
    base: dict[str, Any]                       # leasable knob -> baseline value
    ceiling: dict[str, Any]                    # leasable knob -> most a lease may hold
    max_duration: float
    grace: float
    root: Path                                 # the sandbox's / as the host sees it
    apply: Callable[[dict[str, Any]], list[str]]
    now: Callable[[], float]
    emit: Callable[..., Any]
    base_high: Any = None                      # the profile's own memory.high, restored at the end
    lease: Lease | None = None
    history: list[Lease] = field(default_factory=list)
    mem_now: Any = None                        # memory.max as written now
    drop_at: float | None = None               # when memory.max follows memory.high down...
    drop_to: Any = None                        # ...to this
    _n: int = 0

    def __post_init__(self):
        if self.mem_now is None:
            self.mem_now = self.base.get("mem.max")

    @classmethod
    def for_profile(cls, profile, root: Path, apply, now, emit) -> "LeaseManager":
        return cls(base={k: profile.defaults.get(k) for k in profile.lease_max},
                   ceiling=dict(profile.lease_max), max_duration=profile.lease_max_duration,
                   grace=profile.lease_grace, root=Path(root), apply=apply, now=now, emit=emit,
                   base_high=profile.defaults.get("mem.high"))

    # ------------------------------------------------------------ files in the sandbox
    @property
    def dir(self) -> Path:
        return self.root / DIR

    def install(self) -> None:
        """The ``lease`` command and its request/answer directories, in the sandbox."""
        for sub in ("req", "resp"):
            p = self.dir / sub
            p.mkdir(parents=True, exist_ok=True)
            os.chmod(p, 0o1777)                 # whoever the agent runs as can ask
        b = self.root / BIN
        b.parent.mkdir(parents=True, exist_ok=True)
        tmp = b.with_name(".lease.rprof-tmp")
        tmp.write_text(SCRIPT)
        os.chmod(tmp, 0o755)
        os.replace(tmp, b)
        self.emit("lease_installed", path="/" + BIN, dir="/" + DIR, base=self.base, ceiling=self.ceiling,
                  max_duration=self.max_duration, grace=self.grace)

    def _answer(self, rid: str, ok: bool, text: str) -> None:
        p = self.dir / "resp" / rid
        tmp = p.with_name("." + rid)
        tmp.write_text(("ok" if ok else "refused") + "\n" + text.rstrip("\n") + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)

    def poll(self) -> None:
        """Expire, finish memory drops, and answer every waiting request."""
        t = self.now()
        if self.lease is not None and t >= self.lease.t_end:
            self._end("expired", t)
        if self.drop_at is not None and t >= self.drop_at:
            self._drop()
        req = self.dir / "req"
        try:
            names = sorted(n for n in os.listdir(req) if not n.startswith("."))
        except OSError:
            return
        for rid in names:
            p = req / rid
            try:
                text = p.read_text()
                p.unlink()
            except OSError:
                continue
            fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            try:
                ok, answer = self.handle(fields)
            except LeaseError as e:
                ok, answer = False, f"lease: {e}"
                self.emit("lease_refused", request=fields, reason=str(e))
            self._answer(rid, ok, answer)
        self._sweep()

    def _sweep(self) -> None:
        """Answers nobody collected (the command gives up after 30 s)."""
        resp = self.dir / "resp"
        cutoff = time.time() - 120
        try:
            for n in os.listdir(resp):
                p = resp / n
                if p.stat().st_mtime < cutoff:
                    p.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------ requests
    def handle(self, f: dict[str, str]) -> tuple[bool, str]:
        op = f.get("op", "")
        if op == "status":
            return True, self.status()
        if op == "release":
            if self.lease is None:
                return True, "You hold no lease; your limits are the baseline: " + phrase(self.base) + "."
            lid = self.lease.id
            self._end("released", self.now())
            return True, f"{lid} released: back to the baseline, {phrase(self.base)}." + self._memory_note()
        if op != "request":
            raise LeaseError(f"unknown operation {op!r}")
        return True, self.request(f)

    def request(self, f: dict[str, str]) -> str:
        asked: dict[str, Any] = {}
        if f.get("cpus"):
            try:
                asked["cpu.cores"] = float(f["cpus"])
            except ValueError:
                raise LeaseError(f"{f['cpus']!r} is not a number of CPUs") from None
            if asked["cpu.cores"] <= 0:
                raise LeaseError("--cpus must be more than 0")
        if f.get("mem"):
            asked["mem.max"] = parse_size(f["mem"])
        if not asked:
            raise LeaseError("ask for something: --cpus N and/or --mem SIZE")
        for k in asked:
            if k not in self.ceiling:
                raise LeaseError(f"{'CPUs' if k == 'cpu.cores' else 'memory'} can't be leased here")
        duration = parse_seconds(f["for"]) if f.get("for") else DEFAULT_DURATION
        if duration <= 0:
            raise LeaseError("--for must be more than 0")
        notes = []
        granted = dict(self.base)
        for k, v in asked.items():
            if v > self.ceiling[k]:
                notes.append(f"You asked for {phrase({k: v})}; the most you can hold is {phrase({k: self.ceiling[k]})}.")
                v = self.ceiling[k]
            if self.base.get(k) is not None and v < self.base[k]:
                v = self.base[k]                 # never below the baseline
            granted[k] = v
        if duration > self.max_duration:
            notes.append(f"Leases last at most {u.fmt_num(self.max_duration)} s.")
            duration = self.max_duration
        t = self.now()
        previous = self.lease
        self._n += 1
        lease = Lease(f"L{self._n}", granted, asked, t, t + duration)
        if previous is not None:
            previous.ended, previous.reason = t, "replaced"
            self.emit("lease_ended", id=previous.id, reason="replaced", by=lease.id)
        self.lease = lease
        self.history.append(lease)
        self._set(granted, t)
        self.emit("lease_granted", id=lease.id, asked=asked, granted=granted, duration_s=duration,
                  t_expires=round(lease.t_end, 3), replaced=previous.id if previous else None)
        out = (f"{lease.id} granted: {phrase(granted)} until t = {u.fmt_num(round(lease.t_end))} s "
               f"({u.fmt_num(round(duration))} s from now).")
        if previous is not None:
            out += f" It replaces {previous.id}."
        return " ".join([out] + notes) + self._memory_note()

    def status(self) -> str:
        t = self.now()
        lines = [f"Baseline: {phrase(self.base)}. Most you can lease: {phrase(self.ceiling)}, "
                 f"for at most {u.fmt_num(self.max_duration)} s at a time. Now: t = {u.fmt_num(round(t))} s."]
        if self.lease is None:
            lines.append("You hold no lease.")
        else:
            lines.append(f"{self.lease.id}: {phrase(self.lease.values)}, {u.fmt_num(round(self.lease.t_end - t))} s "
                         f"left (until t = {u.fmt_num(round(self.lease.t_end))} s).")
        return "\n".join(lines) + self._memory_note()

    def _memory_note(self) -> str:
        if self.drop_at is None:
            return ""
        return (f"\nMemory above {phrase({'mem.max': self.drop_to})} is being reclaimed; processes "
                f"still above it in {u.fmt_num(round(max(0.0, self.drop_at - self.now())))} s will be killed.")

    # ------------------------------------------------------------ limits
    def _high_for(self, mem_max: Any) -> Any:
        """memory.high to go with memory.max: none above the baseline, the profile's own at it."""
        base = self.base.get("mem.max")
        return None if base is None or mem_max > base else self.base_high

    def _set(self, values: dict[str, Any], t: float) -> None:
        """Make ``values`` the limits: CPU at once; memory up at once, down in two steps."""
        now_vals: dict[str, Any] = {}
        if "cpu.cores" in values:
            now_vals["cpu.cores"] = values["cpu.cores"]
        if "mem.max" in values:
            new = values["mem.max"]
            if self.mem_now is not None and new < self.mem_now and self.grace > 0:
                now_vals["mem.high"] = new                     # reclaim and throttle above it now...
                self.drop_at, self.drop_to = t + self.grace, new   # ...and kill above it later
            else:
                now_vals["mem.max"] = new
                now_vals["mem.high"] = self._high_for(new)
                self.mem_now, self.drop_at, self.drop_to = new, None, None
        errors = self.apply(now_vals) if now_vals else []
        self.emit("lease_limits", values=now_vals, errors=errors,
                  memory_max_at=None if self.drop_at is None else round(self.drop_at, 3))

    def _drop(self) -> None:
        new = self.drop_to
        vals = {"mem.max": new, "mem.high": self._high_for(new)}
        self.mem_now, self.drop_at, self.drop_to = new, None, None
        errors = self.apply(vals)
        self.emit("lease_limits", values=vals, errors=errors, memory_max_at=None)

    def _end(self, reason: str, t: float) -> None:
        lease = self.lease
        assert lease is not None
        lease.ended, lease.reason = t, reason
        self.lease = None
        self.emit("lease_ended", id=lease.id, reason=reason, held_s=round(t - lease.t_start, 3))
        self._set(dict(self.base), t)

    def close(self) -> list[dict]:
        """At the end of the run: the leases, for the record (limits are restored by the run)."""
        if self.lease is not None:
            self.lease.ended, self.lease.reason = self.now(), "run_end"
        return [x.record() for x in self.history]


class LeaseLoop:
    """Polls a LeaseManager on its own thread until stopped."""

    def __init__(self, mgr: LeaseManager, poll_s: float = POLL_S):
        self.mgr = mgr
        self.poll_s = poll_s
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="rprof-lease", daemon=True)
        self.errors = 0

    def _run(self) -> None:
        while not self.stop.wait(self.poll_s):
            try:
                self.mgr.poll()
            except Exception as e:  # noqa: BLE001  (keep serving; record it)
                self.errors += 1
                if self.errors <= 5:
                    self.mgr.emit("error", code="lease_poll_failed", message=f"{type(e).__name__}: {e}")

    def start(self) -> None:
        self.thread.start()

    def join(self) -> None:
        self.stop.set()
        self.thread.join(5)
