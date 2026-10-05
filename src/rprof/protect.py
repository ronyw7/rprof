"""``--protect``: oom_score_adj=-1000 on matching processes (default: container PID 1).

When PID 1 is an init shim (``docker run --init`` runs docker-init; tini and dumb-init are
common too), the container lives exactly as long as the shim's child, so the children the
shim has when the run starts are protected as well. Children that appear later stay
unprotected: orphaned background jobs are reparented to the shim, and they are ordinary
workload.

Children inherit oom_score_adj at fork, so a protected harness would make every tool it
launches unkillable too, and a memory limit would stall instead of killing the tool.
The protector therefore resets descendants that merely inherited -1000 (they do not
match a pattern themselves) back to 0, within one sampler tick of their start.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from .snapshot import Snapshot
from .target import Target
from .target.cgroup import write_text


INIT_SHIMS = {"docker-init", "tini", "tini-static", "dumb-init", "catatonit"}


def ppid_of(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def is_init_shim(pid: int) -> bool:
    try:
        argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
    except OSError:
        return False
    return Path(argv0).name in INIT_SHIMS


def cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


class Protector:
    def __init__(self, target: Target, patterns: list[str], snap: Snapshot | None, events=None):
        self.target = target
        self.patterns = [re.compile(p) for p in patterns]
        self.snap = snap
        self.events = events
        self.done: set[int] = set()
        self.checked: dict[int, float] = {}
        self.reset: set[int] = set()
        self.lock = threading.Lock()
        # Captured once, at run start (see the module docstring).
        self.init_children: set[int] = set()
        init = target.init_pid
        if init and is_init_shim(init):
            self.init_children = {p for p in target.pids() if p != init and ppid_of(p) == init}

    def _wanted(self, pid: int) -> bool:
        if pid == self.target.init_pid or pid in self.init_children:
            return True
        cl = cmdline(pid)
        return bool(cl) and any(p.search(cl) for p in self.patterns)

    def scan(self, new_only: bool = False) -> list[int]:
        """Protect matching processes not yet protected; returns newly protected PIDs.

        ``new_only`` (every sampler tick) checks PIDs not seen in the last second, so a
        harness is protected within one tick of starting; a full rescan runs at 1 Hz and
        catches processes whose command line changed after exec.
        """
        # The sampler tick must never wait on the 1 Hz full scan.
        if not self.lock.acquire(blocking=not new_only):
            return []
        try:
            return self._scan(new_only)
        finally:
            self.lock.release()

    def _scan(self, new_only: bool) -> list[int]:
        new = []
        now = time.monotonic()
        for pid in self.target.pids():
            if pid in self.done:
                continue
            if new_only and now - self.checked.get(pid, -1e9) < 1.0:
                continue
            self.checked[pid] = now
            if not self._wanted(pid):
                self._unprotect_inherited(pid)
                continue
            path = Path(f"/proc/{pid}/oom_score_adj")
            if self.snap is not None and not self.snap.record_file(path):
                continue
            try:
                write_text(path, "-1000")
            except OSError as e:
                if self.events:
                    self.events.emit("warning", code="protect_failed", message=f"pid {pid}: {e.strerror}")
                continue
            self.done.add(pid)
            new.append(pid)
            if self.events:
                self.events.emit("protect", pid=pid, cmd=cmdline(pid)[:200])
        return new

    def _unprotect_inherited(self, pid: int) -> None:
        try:
            adj = Path(f"/proc/{pid}/oom_score_adj").read_text().strip()
        except OSError:
            return
        ppid = ppid_of(pid)
        if ppid is None:
            return
        if adj != "-1000" or ppid not in self.done | self.reset:
            return
        try:
            write_text(Path(f"/proc/{pid}/oom_score_adj"), "0")
        except OSError:
            return
        self.reset.add(pid)
        if self.events:
            self.events.emit("unprotect_child", pid=pid, ppid=ppid, cmd=cmdline(pid)[:200])
