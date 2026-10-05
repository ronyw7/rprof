"""Loading a run directory and deriving rates from raw counters (design Appendix B)."""

from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ..profile import Profile, load_profile, unlimited_profile
from ..util import read_jsonl

Path_ = tuple[str, ...]


def get(d: Any, path: Iterable[str]) -> float | None:
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    if isinstance(d, bool) or not isinstance(d, (int, float)):
        return None
    return float(d)


@dataclass
class CallRec:
    call_id: str
    cmd: str
    step: int | None
    t0: float
    t1: float | None
    exit_code: int | None = None
    timed_out: bool = False
    cause: str | None = None
    finished: bool = False
    evidence: dict | None = None
    mem_peak: float | None = None
    mem_peak_nonreclaimable: float | None = None

    @property
    def failed(self) -> bool:
        return self.finished and (self.timed_out or self.exit_code is None or self.exit_code != 0)

    def end(self, t_end: float) -> float:
        return self.t1 if self.t1 is not None else t_end


class RunData:
    def __init__(self, run_dir: str | Path):
        self.dir = Path(run_dir)
        if not (self.dir / "samples.jsonl").exists() and not (self.dir / "events.jsonl").exists():
            raise FileNotFoundError(f"{self.dir} is not an rprof run directory")
        try:
            self.meta = json.loads((self.dir / "meta.json").read_text())
        except (OSError, ValueError):
            self.meta = {}
        try:
            self.profile: Profile = load_profile(self.dir / "profile.yaml")
        except Exception:  # noqa: BLE001
            self.profile = unlimited_profile()
        self.samples = read_jsonl(self.dir / "samples.jsonl")
        self.samples.sort(key=lambda s: s.get("t", 0))
        self.events = read_jsonl(self.dir / "events.jsonl")
        self.t = [float(s["t"]) for s in self.samples]
        self.mode = self.meta.get("mode", "enforce")
        self.run_id = self.meta.get("run_id", self.dir.name)
        end = [e["t"] for e in self.events if e.get("type") == "run_end"]
        self.t_end = float(end[-1]) if end else (self.t[-1] if self.t else 0.0)
        if self.t:
            self.t_end = max(self.t_end, self.t[-1]) if not end else self.t_end
        self.io_dev = (self.meta.get("target") or {}).get("io_device")
        self.calls = self._calls()
        self._cache: dict = {}

    # ------------------------------------------------------------ calls
    def _calls(self) -> list[CallRec]:
        calls: dict[str, CallRec] = {}
        for e in self.events:
            if e.get("type") == "tool_start":
                calls[e["call_id"]] = CallRec(e["call_id"], e.get("cmd", ""), e.get("step"), float(e["t"]), None)
            elif e.get("type") == "tool_end" and e.get("call_id") in calls:
                c = calls[e["call_id"]]
                c.t1 = float(e["t"])
                c.exit_code = e.get("exit_code")
                c.timed_out = bool(e.get("timed_out"))
                c.cause = e.get("cause")
                c.finished = True
                c.evidence = e.get("evidence") or {}
                c.mem_peak = e.get("mem_peak_bytes")
                c.mem_peak_nonreclaimable = e.get("mem_peak_nonreclaimable_bytes")
        return sorted(calls.values(), key=lambda c: c.t0)

    # ------------------------------------------------------------ series
    def values(self, fn: Callable[[dict], float | None]) -> list[float | None]:
        return [fn(s) for s in self.samples]

    def series(self, path: Path_) -> list[float | None]:
        key = ("series",) + path
        if key not in self._cache:
            self._cache[key] = [get(s, path) for s in self.samples]
        return self._cache[key]

    def has(self, path: Path_) -> bool:
        return any(v is not None for v in self.series(path))

    def at(self, path: Path_, t: float) -> float | None:
        """Linearly interpolated counter value at time t."""
        return self._interp(self.series(path), t)

    def _interp(self, ys: list[float | None], t: float) -> float | None:
        ts = self.t
        if not ts:
            return None
        i = bisect.bisect_left(ts, t)
        lo = i - 1
        while lo >= 0 and ys[lo] is None:
            lo -= 1
        hi = i
        while hi < len(ts) and ys[hi] is None:
            hi += 1
        if hi < len(ts) and ts[hi] == t:
            return ys[hi]
        if lo < 0 and hi >= len(ts):
            return None
        if lo < 0:
            return ys[hi]
        if hi >= len(ts):
            return ys[lo]
        y0, y1 = ys[lo], ys[hi]
        assert y0 is not None and y1 is not None
        if ts[hi] == ts[lo]:
            return y1
        return y0 + (y1 - y0) * (t - ts[lo]) / (ts[hi] - ts[lo])

    def delta(self, path: Path_, t0: float, t1: float) -> float | None:
        a, b = self.at(path, t0), self.at(path, t1)
        if a is None or b is None:
            return None
        return max(0.0, b - a)

    def rate(self, path: Path_, t0: float, t1: float) -> float | None:
        d = self.delta(path, t0, t1)
        if d is None or t1 <= t0:
            return None
        return d / (t1 - t0)

    def tick_rates(self, ys: list[float | None]) -> list[float | None]:
        """Per-tick rate ending at each sample (None for the first)."""
        out: list[float | None] = [None]
        for i in range(1, len(ys)):
            a, b = ys[i - 1], ys[i]
            dt = self.t[i] - self.t[i - 1]
            out.append(None if a is None or b is None or dt <= 0 else max(0.0, (b - a) / dt))
        return out

    def window_idx(self, t0: float, t1: float) -> range:
        i0 = bisect.bisect_right(self.t, t0)   # ticks ending after t0
        i1 = bisect.bisect_right(self.t, t1)
        return range(i0, i1)

    def smoothed(self, ys: list[float | None], width_s: float = 1.0) -> list[float | None]:
        """Rate over a trailing window of width_s (for 'throughput >= 80% of the limit')."""
        out: list[float | None] = []
        j = 0
        for i in range(len(ys)):
            while j < i and self.t[i] - self.t[j] > width_s:
                j += 1
            a, b = ys[j], ys[i]
            dt = self.t[i] - self.t[j]
            out.append(None if a is None or b is None or dt < width_s * 0.5 else max(0.0, (b - a) / dt))
        return out

    # ------------------------------------------------------------ derived
    def io_path(self, field: str) -> Callable[[dict], float | None]:
        dev = self.io_dev

        def fn(s: dict) -> float | None:
            io = s.get("io")
            if not isinstance(io, dict):
                return None
            if dev:
                return get(io, (dev, field))
            vals = [get(v, (field,)) for v in io.values()]
            vals = [v for v in vals if v is not None]
            return sum(vals) if vals else None
        return fn

    def io_series(self, field: str) -> list[float | None]:
        key = ("io", field)
        if key not in self._cache:
            self._cache[key] = self.values(self.io_path(field))
        return self._cache[key]

    def net_series(self, field: str) -> list[float | None]:
        key = ("net", field)
        if key not in self._cache:
            def fn(s: dict):
                net = s.get("net")
                if not isinstance(net, dict):
                    return None
                vals = [get(v, (field,)) for v in net.values() if isinstance(v, dict)]
                vals = [v for v in vals if v is not None]
                return sum(vals) if vals else None
            self._cache[key] = self.values(fn)
        return self._cache[key]

    def mem_series(self, basis: str = "non_reclaimable", with_peak: bool = False
                   ) -> tuple[list[float | None], str]:
        """Memory usage per sample, and the basis actually used.

        ``non_reclaimable`` is current − (file − shmem): page cache the kernel would drop
        under a limit doesn't count, tmpfs/shared memory does. Runs recorded before rprof
        sampled shmem fall back to current − file (``non_reclaimable_without_shmem``), and runs
        without memory.stat to ``total`` (memory.current, page cache included).
        ``with_peak`` folds in memory.peak, which only exists on the total basis.
        """
        key = ("mem", basis, with_peak)
        if key not in self._cache:
            self._cache[key] = self._mem_series(basis, with_peak)
        return self._cache[key]

    def _mem_series(self, basis: str, with_peak: bool) -> tuple[list[float | None], str]:
        cur = self.series(("mem", "current"))
        if basis == "non_reclaimable":
            file, shmem = self.series(("mem", "file")), self.series(("mem", "shmem"))
            if any(f is not None for f in file):
                used = ("non_reclaimable" if any(x is not None for x in shmem)
                        else "non_reclaimable_without_shmem")
                out = [None if c is None or f is None else max(0.0, c - (f - (m or 0.0)))
                       for c, f, m in zip(cur, file, shmem)]
                return out, used
        if with_peak:
            peak = self.series(("mem", "peak"))
            return [max(x for x in (c, p) if x is not None) if (c is not None or p is not None) else None
                    for c, p in zip(cur, peak)], "total"
        return cur, "total"

    def disk_workload(self) -> list[float | None]:
        def fn(s):
            u, b = get(s, ("disk", "used_bytes")), get(s, ("disk", "ballast_bytes")) or 0.0
            return None if u is None else u - b
        return self.values(fn)


def pct(xs: list[float], q: float) -> float | None:
    xs = sorted(x for x in xs if x is not None and not math.isnan(x))
    if not xs:
        return None
    k = (len(xs) - 1) * q
    f = math.floor(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def stats(xs: list[float | None]) -> dict | None:
    v = [x for x in xs if x is not None]
    if not v:
        return None
    return {"mean": sum(v) / len(v), "p95": pct(v, 0.95), "max": max(v)}
