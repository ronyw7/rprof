"""Docker lookups through the docker CLI (honours DOCKER_HOST / contexts)."""

from __future__ import annotations

import json
from functools import lru_cache

from ..util import TargetNotFound, run_cmd


def inspect(name: str) -> dict:
    r = run_cmd(["docker", "inspect", "--type", "container", name], timeout=10)
    if not r.ok:
        msg = r.err or r.out
        if "No such" in msg or "no such" in msg:
            raise TargetNotFound(f"docker container {name!r} not found")
        raise TargetNotFound(f"docker inspect {name} failed: {msg}")
    data = json.loads(r.out)
    if not data:
        raise TargetNotFound(f"docker container {name!r} not found")
    return data[0]


def running_pid(name: str) -> tuple[dict, int]:
    info = inspect(name)
    st = info.get("State", {})
    if not st.get("Running") or not st.get("Pid"):
        raise TargetNotFound(f"docker container {name!r} is not running")
    return info, int(st["Pid"])


@lru_cache(maxsize=1)
def info() -> dict:
    r = run_cmd(["docker", "info", "--format", "{{json .}}"], timeout=10)
    if not r.ok:
        return {}
    try:
        return json.loads(r.out)
    except json.JSONDecodeError:
        return {}


def version() -> str | None:
    return info().get("ServerVersion")


def cgroup_driver() -> str | None:
    return info().get("CgroupDriver")


def root_dir() -> str:
    return info().get("DockerRootDir") or "/var/lib/docker"
