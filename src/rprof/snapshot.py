"""Snapshot of every knob rprof touches, persisted before the first write, and restore.

``snapshot.json`` (Appendix A.4): ``files`` {absolute path: original contents},
``tc`` (qdiscs added), ``iptables`` (chains added, tagged ``rprof:<run-id>``),
``ballast`` (path or null). ``restore`` works from the JSON alone, so
``rprof reset --run DIR`` can undo a run that was SIGKILLed.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Any

from .target import docker
from .target.cgroup import parse_nested, read_text, write_text
from .util import RprofError, TargetLocked, iso_now, log, run_cmd, write_json

IO_RESET = "rbps=max wbps=max riops=max wiops=max"


def state_dir() -> Path:
    return Path(os.environ.get("RPROF_STATE_DIR", "/var/lib/rprof"))


def lock_dir() -> Path:
    return Path(os.environ.get("RPROF_LOCK_DIR", "/run/rprof/locks"))


class Snapshot:
    def __init__(self, path: Path | None, run_id: str, target: dict | None = None):
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {
            "run_id": run_id, "created_at": iso_now(), "target": target or {},
            "files": {}, "tc": [], "iptables": [], "ballast": None, "masks": [],
        }

    @classmethod
    def load(cls, path: Path) -> "Snapshot":
        s = cls(path, "")
        s.data.update(json.loads(Path(path).read_text()))
        return s

    def save(self) -> None:
        if self.path:
            write_json(self.path, self.data)

    def record_file(self, path: str | Path) -> bool:
        """Remember a file's original contents before rprof first writes it."""
        path = str(path)
        with self.lock:
            if path in self.data["files"]:
                return True
            try:
                self.data["files"][path] = read_text(Path(path))
            except OSError as e:
                log.debug("snapshot: cannot read %s: %s", path, e)
                return False
            self.save()
            return True

    def has_file(self, path: str | Path) -> bool:
        return str(path) in self.data["files"]

    def original(self, path: str | Path) -> str | None:
        return self.data["files"].get(str(path))

    def add_tc(self, entry: dict) -> None:
        with self.lock:
            if entry not in self.data["tc"]:
                self.data["tc"].append(entry)
                self.save()

    def remove_tc(self, entry: dict) -> None:
        with self.lock:
            if entry in self.data["tc"]:
                self.data["tc"].remove(entry)
                self.save()

    def add_iptables(self, entry: dict) -> None:
        with self.lock:
            if entry not in self.data["iptables"]:
                self.data["iptables"].append(entry)
                self.save()

    def remove_iptables(self, entry: dict) -> None:
        with self.lock:
            if entry in self.data["iptables"]:
                self.data["iptables"].remove(entry)
                self.save()

    def add_mask(self, entry: dict) -> None:
        with self.lock:
            masks = self.data.setdefault("masks", [])
            if entry not in masks:
                masks.append(entry)
                self.save()

    def remove_mask(self, entry: dict) -> None:
        with self.lock:
            if entry in self.data.get("masks", []):
                self.data["masks"].remove(entry)
                self.save()

    def set_ballast(self, path: str | None) -> None:
        with self.lock:
            self.data["ballast"] = path
            self.save()

    def merge(self, other: "Snapshot") -> None:
        """Fold another snapshot in, keeping the older originals (used by live apply state)."""
        with self.lock:
            for k, v in other.data["files"].items():
                self.data["files"].setdefault(k, v)
            for e in other.data["tc"]:
                if e not in self.data["tc"]:
                    self.data["tc"].append(e)
            for e in other.data["iptables"]:
                if e not in self.data["iptables"]:
                    self.data["iptables"].append(e)
            if other.data["ballast"]:
                self.data["ballast"] = other.data["ballast"]
            self.save()


# ---------------------------------------------------------------- restore

def _live_pid(entry: dict) -> int | None:
    """The netns PID for a recorded container, re-resolved if it restarted."""
    cid = entry.get("container_id")
    if cid:
        try:
            _, pid = docker.running_pid(cid)
            return pid
        except RprofError:
            return None
    pid = entry.get("pid")
    return pid if pid and Path(f"/proc/{pid}").exists() else None


def remove_iptables(entry: dict) -> str | None:
    pid = _live_pid(entry)
    if pid is None:
        return None  # netns is gone, and its rules with it
    ipt = entry.get("binary", "iptables")
    ns = ["nsenter", "-t", str(pid), "-n", "--", ipt, "-w", "5"]
    chain, parent, comment = entry["chain"], entry["parent"], entry["comment"]
    err = None
    for _ in range(4):  # delete every jump we may have inserted
        r = run_cmd(ns + ["-D", parent, "-m", "comment", "--comment", comment, "-j", chain])
        if not r.ok:
            break
    r1 = run_cmd(ns + ["-F", chain])
    r2 = run_cmd(ns + ["-X", chain])
    if not r2.ok and "No chain" not in r2.err and "does not exist" not in r2.err:
        err = f"{ipt} -X {chain} in netns of pid {pid}: {r2.err or r1.err}"
    return err


def remove_tc(entry: dict) -> str | None:
    if entry["side"] == "host":
        if not Path(f"/sys/class/net/{entry['dev']}").exists():
            return None
        r = run_cmd(["tc", "qdisc", "del", "dev", entry["dev"], "root"])
    else:
        pid = _live_pid(entry)
        if pid is None:
            return None
        r = run_cmd(["nsenter", "-t", str(pid), "-n", "--", "tc", "qdisc", "del", "dev", entry["dev"], "root"])
    if not r.ok and "No such file" not in r.err and "Cannot find device" not in r.err \
            and "Cannot delete qdisc with handle of zero" not in r.err:
        return f"tc qdisc del dev {entry['dev']} root ({entry['side']}): {r.err}"
    return None


def restore_file(path: str, original: str) -> None:
    p = Path(path)
    name = p.name
    if name == "io.max":
        cur = parse_nested(read_text(p))
        orig_lines = {ln.split()[0]: ln for ln in original.splitlines() if ln.strip()}
        for dev in cur:
            if dev not in orig_lines:
                write_text(p, f"{dev} {IO_RESET}")
        for dev, ln in orig_lines.items():
            write_text(p, ln)
        return
    if name == "cpuset.cpus" and not original.strip():
        from .controllers.cpu import write_cpuset_all
        errs: list[str] = []
        write_cpuset_all(p.parent, errs)
        if errs:
            raise OSError(errs[0])
        return
    write_text(p, original.strip())


def restore(snap: Snapshot) -> list[str]:
    """Undo everything in the snapshot; returns error strings (empty on success)."""
    errors: list[str] = []
    data = snap.data
    for e in list(data.get("masks", [])):
        from . import mask
        if mask.mnt_ns(e["pid"]) == e["ns"]:   # same process, same namespace: still ours to undo
            err = mask.unmask(e["pid"])
            if err:
                errors.append(f"unmask /sys/fs/cgroup in pid {e['pid']}: {err}")
                continue
        snap.remove_mask(e)
    for e in list(data.get("iptables", [])):
        err = remove_iptables(e)
        if err:
            errors.append(err)
        else:
            snap.remove_iptables(e)
    for e in list(data.get("tc", [])):
        err = remove_tc(e)
        if err:
            errors.append(err)
        else:
            snap.remove_tc(e)
    if data.get("ballast"):
        try:
            os.unlink(data["ballast"])
        except FileNotFoundError:
            pass
        except OSError as e:
            errors.append(f"remove ballast {data['ballast']}: {e.strerror}")
        if not any("ballast" in x for x in errors):
            snap.set_ballast(None)
    # memory.max before memory.high etc. does not matter on v2; restore in reverse order of recording.
    for path, original in reversed(list(data.get("files", {}).items())):
        if not Path(path).exists():
            continue  # cgroup or process gone
        try:
            restore_file(path, original)
        except OSError as e:
            if e.errno in (3, 19):  # ESRCH / ENODEV: process or cgroup vanished
                continue
            errors.append(f"restore {path} = {original.strip()!r}: {e.strerror}")
    return errors


# ---------------------------------------------------------------- locks

class TargetLock:
    """Per-target lock so two runs never control one container."""

    def __init__(self, key: str, owner: str):
        self.path = lock_dir() / f"{key}.lock"
        self.owner = owner
        self.fd: int | None = None

    def acquire(self) -> "TargetLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                try:
                    holder = os.pread(fd, 4096, 0).decode().strip()
                except OSError:
                    holder = "?"
                os.close(fd)
                raise TargetLocked(f"target is locked by another rprof process ({holder or 'unknown'}); "
                                   f"lock file {self.path}") from None
            # The previous holder deletes the file when it releases. If it did so after we opened
            # it, we hold a lock on a deleted file: start over with a fresh one.
            try:
                same = os.fstat(fd).st_ino == os.stat(self.path).st_ino
            except FileNotFoundError:
                same = False
            if same:
                break
            os.close(fd)
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid={os.getpid()} {self.owner}\n".encode(), 0)
        self.fd = fd
        return self

    def release(self) -> None:
        if self.fd is not None:
            try:
                try:
                    os.unlink(self.path)    # while still locked, so no one else can hold this file
                except FileNotFoundError:
                    pass
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *a):
        self.release()
