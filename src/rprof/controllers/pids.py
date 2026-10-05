"""Process count: pids.max."""

from __future__ import annotations

from typing import Any

from ..target.cgroup import pick_flat
from .base import Controller, FileCache


class PidsController(Controller):
    name = "pids"
    knobs = ("pids.max",)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.hierarchical_events = self.target.cgroup.has("pids.events.local")

    def capabilities(self):
        ok = self.target.cgroup.has("pids.max")
        return {"pids.max": None if ok else "pids controller not enabled for the target"}

    def files_for(self, knobs):
        if "pids.max" in set(knobs) and self.target.cgroup.has("pids.max"):
            return [self.target.cgroup.file("pids.max")]
        return []

    def apply(self, limits: dict[str, Any], changed: set[str]) -> list[str]:
        errs: list[str] = []
        if "pids.max" in changed:
            v = limits["pids.max"]
            self.write(self.target.cgroup.file("pids.max"), "max" if v is None else str(v), errs)
        return errs

    def sample(self, out: dict, fc: FileCache) -> None:
        cg = self.target.cgroup
        cur = fc.read(cg.fstr("pids.current"))
        if cur is None:
            return
        p = {"current": int(cur)}
        if self.hierarchical_events:
            ev = fc.read(cg.fstr("pids.events"))
            if ev:
                p["events_max"] = pick_flat(ev, ("max",)).get("max", 0)
        else:
            # Before pids.events.local existed (Linux < 6.12), a fork refused by an ancestor's
            # pids.max is counted in the forking task's own cgroup: sum the whole subtree.
            total, seen = 0, False
            for d in cg.subtree():
                ev = fc.read(d + "/pids.events")
                if ev:
                    total += pick_flat(ev, ("max",)).get("max", 0)
                    seen = True
            if seen:
                p["events_max"] = total
        out["pids"] = p
