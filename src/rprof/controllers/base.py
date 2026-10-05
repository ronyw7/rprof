"""Controller interface: capabilities(), snapshot(), apply(), restore() and sample().

Each controller owns a few knobs. ``apply`` receives the full desired limits and
the set of knobs that changed, so a controller whose kernel file combines
several knobs (``cpu.max``, ``io.max``, netem) can rewrite it whole.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Iterable

from ..snapshot import Snapshot
from ..target import Target
from ..target.cgroup import write_text


class FileCache:
    """Keeps files open and ``pread``s them from offset 0 each tick.

    Every read goes through one reused buffer, so a cache belongs to one thread: give each
    reader its own cache. The lock is a safety net that turns accidental sharing into waiting
    instead of reads that return another file's contents.
    """

    def __init__(self):
        self.fds: dict[str, int] = {}
        self.dead: set[str] = set()
        self._buf = bytearray(16384)  # reused for every read: no per-read allocation
        self._lock = threading.Lock()

    def read(self, path: str | Path) -> str | None:
        with self._lock:
            return self._read(path)

    def _read(self, path: str | Path) -> str | None:
        if not isinstance(path, str):
            path = str(path)
        if path in self.dead:
            return None
        fd = self.fds.get(path)
        try:
            if fd is None:
                fd = os.open(path, os.O_RDONLY)
                self.fds[path] = fd
            n = os.preadv(fd, [self._buf], 0)
            if n < len(self._buf):
                return self._buf[:n].decode()
            chunks = [bytes(self._buf)]
            off = n
            while True:
                b = os.pread(fd, 65536, off)
                if not b:
                    break
                chunks.append(b)
                off += len(b)
            return b"".join(chunks).decode()
        except OSError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                self.fds.pop(path, None)
            if not os.path.exists(path):
                self.dead.add(path)
            return None

    def forget(self, path: str | Path) -> None:
        with self._lock:
            fd = self.fds.pop(str(path), None)
            if fd is not None:
                os.close(fd)
            self.dead.discard(str(path))

    def close(self) -> None:
        with self._lock:
            for fd in self.fds.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            self.fds.clear()


class Controller:
    name: str = ""
    knobs: tuple[str, ...] = ()

    def __init__(self, target: Target, snap: Snapshot | None = None, run_id: str = "", events=None):
        self.target = target
        self.snap = snap
        self.run_id = run_id
        self.events = events  # EventLog or None

    # --- capabilities ------------------------------------------------
    def capabilities(self) -> dict[str, str | None]:
        """knob -> None when supported, else a reason."""
        return {k: None for k in self.knobs}

    def files_for(self, knobs: Iterable[str]) -> list[Path]:
        """Kernel files this controller writes for these knobs (snapshotted up front)."""
        return []

    # --- control -----------------------------------------------------
    def snapshot(self, knobs: Iterable[str]) -> None:
        if self.snap is None:
            return
        for f in self.files_for(knobs):
            self.snap.record_file(f)

    def apply(self, limits: dict[str, Any], changed: set[str]) -> list[str]:
        return []

    def restore(self) -> list[str]:
        """Controller-specific undo beyond snapshot files (qdiscs, rules, ballast)."""
        return []

    # --- sampling ----------------------------------------------------
    def sample(self, out: dict, fc: FileCache) -> None:
        pass

    def slow_poll(self) -> None:
        """Expensive counters (subprocesses), called at 1 Hz from a worker thread."""

    # --- helpers -----------------------------------------------------
    def write(self, path: Path, value: str, errors: list[str]) -> bool:
        if self.snap is not None:
            self.snap.record_file(path)
        try:
            write_text(path, value)
            return True
        except OSError as e:
            errors.append(f"{path.name}={value!r}: {e.strerror or e}")
            return False

    def warn(self, code: str, message: str) -> None:
        if self.events is not None:
            self.events.emit("warning", code=code, message=message)
