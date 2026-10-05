"""Scheduler: applies the profile at segment boundaries on a background timer.

Limits change on time alone; tool-call events never move a boundary. Only knobs
whose value changed are written, and every apply is logged with its latency.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from typing import Any

from . import knobs as K
from .controllers import Controller
from .profile import Profile


class Scheduler:
    def __init__(self, profile: Profile, controllers: list[Controller], managed: set[str], mode: str,
                 clock, events, on_change=None):
        self.profile = profile
        self.controllers = controllers
        self.managed = managed
        self.enforce = mode == "enforce"
        self.clock = clock
        self.events = events
        self.on_change = on_change
        self.applied: dict[str, Any] = {}
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="rprof-apply")
        self.apply_ms: list[float] = []

    def desired(self, t: float) -> dict[str, Any]:
        lim = self.profile.limits_at(t)
        return {k: v for k, v in lim.items() if k in self.managed}

    def apply_at(self, boundary: float) -> dict:
        """Apply the limits in force at ``boundary`` (blocking; runs in the apply thread)."""
        want = self.desired(boundary)
        changed = {k for k in set(want) | set(self.applied) if want.get(k, None) != self.applied.get(k, None)
                   or (k in want) != (k in self.applied)}
        errors: list[str] = []
        per: dict[str, float] = {}
        t_start = self.clock.now()
        p0 = time.perf_counter()
        if self.enforce and changed:
            full = dict(self.profile.limits_at(boundary))
            for c in self.controllers:
                mine = {k for k in changed if k in c.knobs}
                if not mine:
                    continue
                c0 = time.perf_counter()
                try:
                    errors.extend(f"{c.name}: {e}" for e in c.apply(full, mine))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{c.name}: {type(e).__name__}: {e}")
                per[c.name] = round((time.perf_counter() - c0) * 1e3, 2)
        apply_ms = round((time.perf_counter() - p0) * 1e3, 2)
        self.applied = want
        if changed and self.enforce:
            self.apply_ms.append(apply_ms)
        seg, active = self.profile.segment_at(boundary)
        ev = self.events.emit(
            "segment_applied", t=t_start, boundary=boundary, segment=seg, active_segments=active,
            limits=K.limits_json(self.profile.limits_at(boundary)),
            changed=sorted(changed), enforced=self.enforce, apply_ms=apply_ms, apply_ms_by_controller=per,
            late_ms=round((t_start - boundary) * 1e3, 2), errors=errors)
        if errors:
            self.events.emit("error", code="apply_failed", message="; ".join(errors)[:2000])
        if self.on_change:
            self.on_change(boundary)
        return ev

    def apply_knobs(self, values: dict[str, Any]) -> list[str]:
        """One-shot apply for ``rprof apply`` (no profile timing)."""
        errors: list[str] = []
        full = K.defaults()
        full.update(self.applied)
        full.update(values)
        for c in self.controllers:
            mine = {k for k in values if k in c.knobs}
            if mine:
                errors.extend(f"{c.name}: {e}" for e in c.apply(full, mine))
        self.applied.update(values)
        return errors

    async def run(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        for b in self.profile.boundaries():
            if b <= 0:
                continue
            delay = b - self.clock.now()
            if delay > 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    pass
            if stop.is_set():
                return
            await loop.run_in_executor(self.executor, self.apply_at, b)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True)

