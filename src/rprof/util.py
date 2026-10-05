"""Small shared helpers: subprocesses with timeouts, atomic writes, clocks, JSON lines."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger("rprof")

SUBPROCESS_TIMEOUT_S = 2.0


class RprofError(Exception):
    """An error with a design exit code (Appendix A.5)."""

    exit_code = 70

    def __init__(self, message: str, exit_code: int | None = None):
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class TargetNotFound(RprofError):
    exit_code = 71


class PermissionDenied(RprofError):
    exit_code = 72


class MissingCapability(RprofError):
    exit_code = 73


class TargetLocked(RprofError):
    exit_code = 74


class RestoreFailed(RprofError):
    exit_code = 75


class CmdResult:
    __slots__ = ("cmd", "rc", "out", "err", "ms")

    def __init__(self, cmd, rc, out, err, ms):
        self.cmd, self.rc, self.out, self.err, self.ms = cmd, rc, out, err, ms

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def __repr__(self):
        return f"CmdResult({' '.join(self.cmd)!r}, rc={self.rc}, {self.ms:.1f} ms)"


def run_cmd(cmd: Sequence[str], timeout: float = SUBPROCESS_TIMEOUT_S, check: bool = False,
            input: str | None = None, quiet: bool = False) -> CmdResult:
    """Run a subprocess with a timeout; never raises unless check=True."""
    t0 = time.perf_counter()
    try:
        p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, input=input)
        res = CmdResult(list(cmd), p.returncode, p.stdout, p.stderr.strip(), (time.perf_counter() - t0) * 1e3)
    except subprocess.TimeoutExpired:
        res = CmdResult(list(cmd), -1, "", f"timed out after {timeout:g} s", (time.perf_counter() - t0) * 1e3)
    except FileNotFoundError:
        res = CmdResult(list(cmd), 127, "", f"{cmd[0]}: command not found", (time.perf_counter() - t0) * 1e3)
    if not quiet:
        log.debug("exec %s -> rc=%s %.1fms %s", " ".join(cmd), res.rc, res.ms, res.err[:200])
    if check and not res.ok:
        raise RprofError(f"{' '.join(cmd)} failed: {res.err or res.out}")
    return res


def atomic_write(path: str | Path, data: str | bytes, mode: int = 0o644, fsync: bool = True) -> None:
    """Write via temp file + rename so readers never see a partial file.

    ``fsync=False`` keeps the atomic rename but skips durability (for files rewritten every second).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: str | Path, obj: Any, mode: int = 0o644) -> None:
    atomic_write(path, json.dumps(obj, indent=2, default=_json_default) + "\n", mode)


def _json_default(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=_json_default)


def iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def read_jsonl(path: str | Path) -> list[dict]:
    out = []
    p = Path(path)
    if not p.exists():
        return out
    with p.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # torn last line after a crash
    return out


_STOP = object()


class JsonlWriter:
    """Append-only JSON-lines file, written by a background thread.

    ``write`` only queues the object, so the caller never blocks on the file system. That
    matters on the event loop: when the kernel throttles writers during heavy writeback, a
    blocking write would stall the loop and, with it, the control socket. The writer thread
    encodes, writes, and flushes every ``flush_every`` seconds, or at once for ``flush=True``.
    """

    def __init__(self, path: str | Path, flush_every: float = 1.0):
        self.path = Path(path)
        self.f = self.path.open("a", buffering=1 << 16)
        self.flush_every = flush_every
        self.q: queue.SimpleQueue = queue.SimpleQueue()
        self.closed = False
        self.error: BaseException | None = None   # the first write error
        self.errors = 0
        self.thread = threading.Thread(target=self._loop, name=f"rprof-writer-{self.path.name}", daemon=True)
        self.thread.start()

    def write(self, obj: dict, flush: bool = False) -> None:
        if not self.closed:
            self.q.put((obj, flush))

    def _loop(self) -> None:
        last = time.monotonic()
        while True:
            try:
                item = self.q.get(timeout=self.flush_every)
            except queue.Empty:
                item = None
            try:
                if item is _STOP:
                    self.f.flush()
                    return
                if item is not None:
                    obj, flush = item
                    self.f.write(dumps(obj) + "\n")
                    if flush:
                        self.f.flush()
                        last = time.monotonic()
                if time.monotonic() - last >= self.flush_every:
                    self.f.flush()
                    last = time.monotonic()
            except Exception as e:  # noqa: BLE001  keep draining; reported now and by close()
                self.errors += 1
                if self.error is None:
                    self.error = e
                    log.error("writing %s failed: %s", self.path, e)

    def close(self) -> str | None:
        """Flush and close; returns a description of any write error, else None."""
        if self.closed:
            return self.error_text()
        self.closed = True
        self.q.put(_STOP)
        self.thread.join()
        try:
            self.f.close()
        except OSError as e:
            self.errors += 1
            self.error = self.error or e
        return self.error_text()

    def error_text(self) -> str | None:
        if self.error is None:
            return None
        return f"{self.errors} write(s) to {self.path.name} failed; first: {type(self.error).__name__}: {self.error}"


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def sudo_owner() -> tuple[int, int] | None:
    """(uid, gid) of the user who ran sudo, if any."""
    try:
        return int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    except (KeyError, ValueError):
        return None


def chown_tree(root: Path, uid: int, gid: int) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [dirpath] + [os.path.join(dirpath, n) for n in dirnames + filenames]:
            try:
                os.lchown(name, uid, gid)
            except OSError:
                pass
