"""``--hide-limits``: stop the sandbox from reading its limits from /sys/fs/cgroup.

A container sees its own cgroup at ``/sys/fs/cgroup``, so even with ``visibility: none``
an agent can ``cat /sys/fs/cgroup/memory.max``. With ``--hide-limits``, rprof mounts a small
read-only tmpfs over ``/sys/fs/cgroup`` inside each mount namespace of the target. It
holds files that report no limits (``memory.max`` reads ``max``, and so on). The kernel
still enforces the real limits, and rprof still reads them from the host's cgroupfs.

The mount is made by a short-lived helper process that joins the container's mount
namespace with setns(2): a fresh single-threaded process, because rprof itself is
multi-threaded. It is removed when the run ends, or by ``rprof reset --run`` after a crash.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .util import run_cmd

MASK_SOURCE = "rprof-mask"
MASK_POINT = "/sys/fs/cgroup"

# What the sandbox sees instead of its cgroup: a cgroup with no limits.
MASK_FILES = {
    "cgroup.controllers": "cpuset cpu io memory pids\n",
    "cgroup.procs": "",
    "cpu.max": "max 100000\n",
    "cpu.weight": "100\n",
    "cpuset.cpus": "\n",
    "io.max": "",
    "memory.high": "max\n",
    "memory.low": "0\n",
    "memory.max": "max\n",
    "memory.min": "0\n",
    "memory.oom.group": "0\n",
    "memory.swap.max": "max\n",
    "pids.max": "max\n",
}

_HELPER = r'''
import ctypes, json, os, sys
CLONE_NEWNS, MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_REMOUNT, MNT_DETACH = 0x20000, 1, 2, 4, 8, 32, 2
libc = ctypes.CDLL(None, use_errno=True)
action, pid, point, source = sys.argv[1], int(sys.argv[2]), sys.argv[3].encode(), sys.argv[4].encode()

def fail(what):
    e = ctypes.get_errno()
    sys.stderr.write(f"{what}: {os.strerror(e)}\n")
    sys.exit(1)

fd = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY)
if libc.setns(fd, CLONE_NEWNS) != 0:
    fail("setns")
os.chdir("/")
flags = MS_NOSUID | MS_NODEV | MS_NOEXEC
if action == "mask":
    if libc.mount(source, point, b"tmpfs", flags, b"size=64k,mode=0755") != 0:
        fail("mount tmpfs")
    for name, content in json.loads(sys.stdin.read()).items():
        with open(os.path.join(point.decode(), name), "w") as f:
            f.write(content)
    if libc.mount(source, point, b"tmpfs", MS_REMOUNT | MS_RDONLY | flags, b"size=64k,mode=0755") != 0:
        e = ctypes.get_errno()
        libc.umount2(point, MNT_DETACH)
        ctypes.set_errno(e)
        fail("remount read-only")
elif action == "unmask":
    if libc.umount2(point, MNT_DETACH) != 0:
        fail("umount")
'''


def mnt_ns(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{pid}/ns/mnt").st_ino
    except OSError:
        return None


def is_masked(pid: int) -> bool:
    """True if our tmpfs is the top mount at /sys/fs/cgroup in this process's mount namespace."""
    try:
        lines = Path(f"/proc/{pid}/mountinfo").read_text().splitlines()
    except OSError:
        return False
    top = None
    for ln in lines:
        pre, _, post = ln.partition(" - ")
        a, b = pre.split(), post.split()
        if len(a) >= 5 and a[4] == MASK_POINT and len(b) >= 2:
            top = (b[0], b[1])  # later lines are mounted on top of earlier ones
    return top == ("tmpfs", MASK_SOURCE)


def _helper(action: str, pid: int) -> str | None:
    r = run_cmd([sys.executable, "-c", _HELPER, action, str(pid), MASK_POINT, MASK_SOURCE], timeout=10,
                input=json.dumps(MASK_FILES) if action == "mask" else "")
    return None if r.ok else (r.err or f"exit {r.rc}")


def mask(pid: int) -> str | None:
    """Mask /sys/fs/cgroup in pid's mount namespace; returns an error message or None."""
    if is_masked(pid):
        return None
    return _helper("mask", pid)


def unmask(pid: int) -> str | None:
    if not is_masked(pid):
        return None
    return _helper("unmask", pid)


def namespaces(pids: list[int]) -> dict[int, int]:
    """One PID per mount namespace among ``pids``, skipping the host's and rprof's own."""
    skip = {mnt_ns(1), mnt_ns(os.getpid())}
    out: dict[int, int] = {}
    for pid in pids:
        ns = mnt_ns(pid)
        if ns is not None and ns not in skip and ns not in out:
            out[ns] = pid
    return out
