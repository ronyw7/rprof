"""Run clock: seconds since ``run`` started, on the monotonic clock."""

from __future__ import annotations

import datetime as _dt
import time


class RunClock:
    def __init__(self):
        self.start()

    def start(self) -> None:
        self.t0_mono = time.monotonic()
        self.t0_wall = time.time()
        self.started = True

    def now(self) -> float:
        if not self.started:
            return 0.0
        return time.monotonic() - self.t0_mono

    def mono_at(self, t: float) -> float:
        return self.t0_mono + t

    def wall_iso(self, t: float | None = None) -> str:
        w = self.t0_wall + (self.now() if t is None else t)
        d = _dt.datetime.fromtimestamp(w, _dt.timezone.utc)
        return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SimClock(RunClock):
    """A clock tests can move by hand."""

    def __init__(self, t: float = 0.0):
        super().__init__()
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt
