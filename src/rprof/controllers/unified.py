"""Raw cgroup v2 files from a segment's ``unified`` map."""

from __future__ import annotations

from typing import Any

from ..knobs import UNIFIED_PREFIX, is_unified
from .base import Controller


class UnifiedController(Controller):
    name = "unified"

    def __init__(self, *a, files: set[str] | None = None, **kw):
        super().__init__(*a, **kw)
        self.files = files or set()
        self.knobs = tuple(UNIFIED_PREFIX + f for f in sorted(self.files))

    def capabilities(self):
        cg = self.target.cgroup
        return {UNIFIED_PREFIX + f: (None if cg.has(f) else f"{f} does not exist in the target cgroup")
                for f in self.files}

    def files_for(self, knobs):
        cg = self.target.cgroup
        return [cg.file(k[len(UNIFIED_PREFIX):]) for k in knobs if is_unified(k)
                and cg.has(k[len(UNIFIED_PREFIX):])]

    def apply(self, limits: dict[str, Any], changed: set[str]) -> list[str]:
        errs: list[str] = []
        cg = self.target.cgroup
        for k in sorted(changed):
            if not is_unified(k):
                continue
            f = k[len(UNIFIED_PREFIX):]
            if k in limits:
                self.write(cg.file(f), str(limits[k]), errs)
            elif self.snap is not None and self.snap.original(cg.file(f)) is not None:
                # The segment that set it ended: put the original value back.
                self.write(cg.file(f), self.snap.original(cg.file(f)).strip(), errs)  # type: ignore[union-attr]
        return errs
