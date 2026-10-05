"""cgroup v2 directory access.

``RPROF_CGROUP_ROOT`` and ``RPROF_PROC`` override ``/sys/fs/cgroup`` and ``/proc``
so unit tests can run controllers against a fake cgroupfs.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from ..util import RprofError

REQUIRED_CONTROLLERS = ("cpu", "cpuset", "memory", "io", "pids")


def cgroup_root() -> Path:
    return Path(os.environ.get("RPROF_CGROUP_ROOT", "/sys/fs/cgroup"))


def proc_root() -> Path:
    return Path(os.environ.get("RPROF_PROC", "/proc"))


def is_cgroup2(root: Path | None = None) -> bool:
    root = root or cgroup_root()
    if "RPROF_CGROUP_ROOT" in os.environ:
        return (root / "cgroup.controllers").exists()
    try:
        with open("/proc/self/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == str(root):
                    return parts[2] == "cgroup2"
    except OSError:
        pass
    return False


def read_text(path: Path) -> str:
    with open(path) as f:
        return f.read()


def write_text(path: Path, value: str) -> None:
    """Write a cgroup file with one write(2), as the kernel expects."""
    data = value.encode() if value else b"\n"
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def parse_flat(text: str) -> dict[str, int]:
    """'key value' lines (cpu.stat, memory.stat, memory.events, pids.events)."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def pick_flat(text: str, keys: tuple[str, ...]) -> dict[str, int]:
    """Only ``keys`` from a 'key value' file (hot path: no full split of memory.stat)."""
    out = {}
    t = "\n" + text
    for k in keys:
        i = t.find("\n" + k + " ")
        if i < 0:
            continue
        i += len(k) + 2
        j = t.find("\n", i)
        try:
            out[k] = int(t[i:j] if j >= 0 else t[i:])
        except ValueError:
            pass
    return out


def parse_psi(text: str) -> dict[str, int]:
    """PSI file -> {'some_us': total, 'full_us': total}."""
    out = {}
    i = text.find("total=")
    if i >= 0:
        j = text.find("\n", i)
        out["some_us"] = int(text[i + 6:j if j >= 0 else None])
        k = text.find("total=", i + 6)
        if k >= 0 and text.startswith("full", j + 1):
            e = text.find("\n", k)
            out["full_us"] = int(text[k + 6:e if e >= 0 else None])
    return out


def parse_nested(text: str) -> dict[str, dict[str, int | str]]:
    """'MAJ:MIN k=v k=v' lines (io.stat, io.max)."""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        d: dict[str, int | str] = {}
        for kv in parts[1:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                d[k] = int(v) if v.isdigit() else v
        out[parts[0]] = d
    return out


class Cgroup:
    """One cgroup v2 directory."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._s = str(self.path)
        self._dirs: list[str] = [self._s]
        self._dirs_at = -1e9

    def fstr(self, name: str) -> str:
        """Path of a file in this cgroup as a plain string (hot path: no pathlib)."""
        return self._s + "/" + name

    def __repr__(self):
        return f"Cgroup({str(self.path)!r})"

    @property
    def rel(self) -> str:
        try:
            return "/" + str(self.path.relative_to(cgroup_root()))
        except ValueError:
            return str(self.path)

    def exists(self) -> bool:
        return (self.path / "cgroup.procs").exists()

    def file(self, name: str) -> Path:
        return self.path / name

    def has(self, name: str) -> bool:
        return (self.path / name).exists()

    def read(self, name: str) -> str:
        return read_text(self.path / name)

    def write(self, name: str, value: str) -> None:
        write_text(self.path / name, value)

    def controllers(self) -> set[str]:
        try:
            return set(self.read("cgroup.controllers").split())
        except OSError:
            return set()

    def parent(self) -> "Cgroup":
        return Cgroup(self.path.parent)

    def subtree(self) -> list[str]:
        """This cgroup and its descendants (string paths), rescanned at most once a second."""
        now = time.monotonic()
        if now - self._dirs_at > 1.0:  # sub-cgroups come and go rarely
            self._dirs = [d for d, _, _ in os.walk(self._s)] or [self._s]
            self._dirs_at = now
        return self._dirs

    def procs(self, recursive: bool = True) -> list[int]:
        pids: list[int] = []
        dirs = self.subtree() if recursive else [self._s]
        for d in dirs:
            try:
                with open(d + "/cgroup.procs") as f:
                    pids.extend(int(x) for x in f.read().split())
            except (OSError, ValueError):
                pass
        return sorted(set(pids))

    def missing_controllers(self, wanted=REQUIRED_CONTROLLERS) -> list[str]:
        """Controllers not enabled for this cgroup (i.e. absent from the parent's subtree_control)."""
        have = self.controllers()
        return [c for c in wanted if c not in have]

    def enable_controllers(self, wanted=REQUIRED_CONTROLLERS) -> list[str]:
        """Enable controllers down the ancestry so this cgroup gets them; returns what changed."""
        root = cgroup_root()
        chain = []
        p = self.path.parent
        while True:
            chain.append(p)
            if p == root or p == p.parent:
                break
            p = p.parent
        changed = []
        for anc in reversed(chain):
            try:
                avail = set(read_text(anc / "cgroup.controllers").split())
                enabled = set(read_text(anc / "cgroup.subtree_control").split())
            except OSError:
                continue
            for c in wanted:
                if c in avail and c not in enabled:
                    try:
                        write_text(anc / "cgroup.subtree_control", f"+{c}")
                        changed.append(f"{anc}:+{c}")
                    except OSError as e:
                        raise RprofError(f"cannot enable {c} in {anc}/cgroup.subtree_control: {e.strerror}")
        return changed


def cgroup_of_pid(pid: int) -> Path:
    """The cgroup v2 directory of a process, as seen from our cgroup namespace."""
    text = read_text(proc_root() / str(pid) / "cgroup")
    for line in text.splitlines():
        if line.startswith("0::"):
            rel = line[3:].strip()
            return cgroup_root() / rel.lstrip("/")
    raise RprofError(f"pid {pid} has no cgroup v2 entry (is the host on cgroup v2?)")


def move_pid(cg: Path, pid: int) -> None:
    write_text(cg / "cgroup.procs", str(pid))
