"""Memory: mem.high -> memory.high, mem.max -> memory.max, mem.swap_max -> memory.swap.max."""

from __future__ import annotations

import os
import threading
from typing import Any

from ..target.cgroup import pick_flat, read_text
from ..units import fmt_bytes
from .base import Controller, FileCache

FILES = {"mem.high": "memory.high", "mem.max": "memory.max", "mem.swap_max": "memory.swap.max"}
EVENTS = ("high", "max", "oom", "oom_kill")
# file includes shmem (tmpfs, shared memory), which can't be reclaimed without swap.
STAT_KEYS = ("anon", "file", "shmem", "pgmajfault")


def non_reclaimable(current: int, file: int | None, shmem: int | None) -> int:
    """Memory the kernel can't drop under pressure: current − (file − shmem).

    Page cache (``file``) is reclaimable, except shmem (tmpfs, shared memory), which is
    counted in ``file`` but needs swap to be freed.
    """
    if file is None:
        return current
    return max(0, current - (file - (shmem or 0)))


def _v(x: int | None) -> str:
    return "max" if x is None else str(int(x))


class MemoryController(Controller):
    name = "memory"
    knobs = ("mem.high", "mem.max", "mem.swap_max")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._peak_fd: int | None = None
        self._peak_lock = threading.Lock()   # the sampler resets the fd; reads between samples peek it
        self.peak_resettable: bool | None = None

    def capabilities(self):
        cg = self.target.cgroup
        out = {}
        for k, f in FILES.items():
            if not cg.has(f):
                out[k] = ("swap accounting unavailable (no memory.swap.max)" if k == "mem.swap_max"
                          else "memory controller not enabled for the target")
            else:
                out[k] = None
        return out

    def files_for(self, knobs):
        cg = self.target.cgroup
        return [cg.file(FILES[k]) for k in FILES if k in set(knobs) and cg.has(FILES[k])]

    def apply(self, limits: dict[str, Any], changed: set[str]) -> list[str]:
        errs: list[str] = []
        cg = self.target.cgroup
        # memory.max last: lowering it below usage blocks while the kernel reclaims or OOM-kills.
        for k in ("mem.swap_max", "mem.high", "mem.max"):
            if k in changed and cg.has(FILES[k]):
                if k == "mem.max" and limits[k] is not None:
                    self._warn_if_below_usage(limits[k])
                self.write(cg.file(FILES[k]), _v(limits[k]), errs)
        return errs

    def _warn_if_below_usage(self, new_max: int) -> None:
        """Lowering memory.max below usage makes the kernel reclaim and, failing that, OOM-kill now."""
        try:
            cur = int(read_text(self.target.cgroup.file("memory.current")))
            st = pick_flat(read_text(self.target.cgroup.file("memory.stat")), ("file", "shmem"))
        except (OSError, ValueError):
            return
        if new_max >= cur:
            return
        nr = non_reclaimable(cur, st.get("file"), st.get("shmem"))
        self.warn("mem_max_below_usage",
                  f"memory.max set to {fmt_bytes(new_max)}, below current usage {fmt_bytes(cur)} "
                  f"({fmt_bytes(nr)} of it not reclaimable): the kernel reclaims, then OOM-kills "
                  f"if usage still doesn't fit")

    # memory.peak is reset through our own fd each tick (Linux 6.12+); older kernels only
    # offer a lifetime maximum, which we do not report as mem.peak.
    def _peak(self) -> int | None:
        if self.peak_resettable is False:
            return None
        path = self.target.cgroup.file("memory.peak")
        try:
            with self._peak_lock:
                if self._peak_fd is None:
                    self._peak_fd = os.open(path, os.O_RDWR)
                val = int(os.pread(self._peak_fd, 64, 0).split()[0])
                os.write(self._peak_fd, b"reset\n")
            self.peak_resettable = True
            return val
        except (OSError, ValueError, IndexError):
            if self._peak_fd is not None:
                try:
                    os.close(self._peak_fd)
                except OSError:
                    pass
                self._peak_fd = None
            if self.peak_resettable is None:
                self.peak_resettable = False
            return None

    def peek_peak(self) -> int | None:
        """The highest memory since the last sample, without resetting it (Linux 6.12+)."""
        if not self.peak_resettable or self._peak_fd is None:
            return None
        with self._peak_lock:
            try:
                return int(os.pread(self._peak_fd, 64, 0).split()[0]) if self._peak_fd is not None else None
            except (OSError, ValueError, IndexError):
                return None

    def sample(self, out: dict, fc: FileCache) -> None:
        cg = self.target.cgroup
        cur = fc.read(cg.fstr("memory.current"))
        if cur is None:
            return
        m: dict[str, Any] = {"current": int(cur)}
        if getattr(fc, "primary", True):
            pk = self._peak()
        else:
            # A read between samples, at a tool call's start or end: a spike since the last sample
            # must count, or a call that allocates, is killed and returns within one tick shows none.
            pk = self.peek_peak()
            life = fc.read(cg.fstr("memory.peak"))      # this fd is never reset: the lifetime maximum
            if life:
                m["lifetime_peak"] = int(life)
        if pk is not None:
            m["peak"] = max(pk, m["current"])
        st = fc.read(cg.fstr("memory.stat"))
        if st:
            m.update(pick_flat(st, STAT_KEYS))
        sw = fc.read(cg.fstr("memory.swap.current"))
        if sw:
            m["swap_current"] = int(sw)
        ev = fc.read(cg.fstr("memory.events"))
        if ev:
            m["events"] = pick_flat(ev, EVENTS)
        out["mem"] = m

    def close(self):
        with self._peak_lock:
            if self._peak_fd is not None:
                os.close(self._peak_fd)
                self._peak_fd = None
