"""Fixtures for running rprof against real Harbor trials (imported by test_harbor.py).

Not a conftest.py: the integration tests import theirs by name (``from conftest import ...``),
which only works while it is the only conftest.py in the tree.

Harbor (https://github.com/harbor-framework/harbor) starts each trial's container itself, from a
task directory, with the CPU and memory limits in its ``task.toml``. These tests run a tiny local
task with Harbor's ``oracle`` agent (it runs the task's ``solution/solve.sh``; no model or API
key), find the container Harbor started, and attach rprof to it the way the experiment runbook does.

Needs root, Docker, cgroup v2, the ``rprof-testbox`` image and the ``harbor`` CLI: on PATH, or
given as ``RPROF_HARBOR`` (useful under sudo, which resets PATH)::

    sudo env RPROF_HARBOR=$(which harbor) .venv/bin/python -m pytest tests/harbor

As in the runbook, Harbor runs as the user who ran sudo (their docker CLI has the compose
plugin Harbor needs), and only rprof runs as root.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RPROF = [sys.executable, "-m", "rprof.cli"]
IMAGE = os.environ.get("RPROF_TEST_IMAGE", "rprof-testbox")
# Harbor uses the docker CLI's default daemon; rprof only sees the rootful one.
os.environ.setdefault("DOCKER_HOST", "unix:///var/run/docker.sock")
USER = os.environ.get("SUDO_USER") if os.geteuid() == 0 else None


def as_user(argv: list[str]) -> list[str]:
    """Run ``argv`` as the user who ran sudo, if any, with the rootful Docker daemon."""
    env = ["env", f"DOCKER_HOST={os.environ['DOCKER_HOST']}"]
    return ["sudo", "-u", USER, "-H", *env, *argv] if USER else argv


def give_to_user(path: Path) -> None:
    if USER:
        subprocess.run(["chown", "-R", f"{os.environ['SUDO_UID']}:{os.environ['SUDO_GID']}", str(path)], check=True)


def _harbor() -> str | None:
    exe = os.environ.get("RPROF_HARBOR") or shutil.which("harbor")
    if not exe and os.environ.get("SUDO_USER"):
        guess = Path("/home") / os.environ["SUDO_USER"] / ".local/bin/harbor"
        exe = str(guess) if guess.exists() else None
    return exe


@pytest.fixture(scope="session")
def harbor_bin() -> str:
    exe = _harbor()
    if not exe:
        msg = "harbor CLI not found: `uv tool install harbor`, or set RPROF_HARBOR"
        if os.environ.get("RPROF_REQUIRE_HARBOR"):            # CI: fail rather than skip quietly
            pytest.fail(msg)
        pytest.skip(msg)
    if os.geteuid() != 0:
        pytest.skip("needs root")
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        pytest.skip(f"image {IMAGE} missing: docker build -t {IMAGE} images/testbox")
    return exe


def make_task(parent: Path, name: str, solve: str, cpus: int = 2, memory_mb: int = 1536) -> Path:
    """A Harbor task whose solution runs ``solve`` (bash) and then writes /app/out.txt."""
    d = parent / name
    for sub in ("environment", "solution", "tests"):
        (d / sub).mkdir(parents=True)
    (d / "environment" / "Dockerfile").write_text(f"FROM {IMAGE}\nWORKDIR /app\n")
    (d / "solution" / "solve.sh").write_text(f"#!/bin/bash\nset -e\n{solve}\necho done > /app/out.txt\n")
    (d / "tests" / "test.sh").write_text("#!/bin/bash\nif grep -q done /app/out.txt; then echo 1; else echo 0; fi"
                                         " > /logs/verifier/reward.txt\n")
    for f in ("solution/solve.sh", "tests/test.sh"):
        (d / f).chmod(0o755)
    (d / "instruction.md").write_text("Write `done` to /app/out.txt.\n")
    (d / "task.toml").write_text(f'''schema_version = "1.1"

[task]
name = "rprof/{name}"
description = "rprof attach test"

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 300.0

[environment]
build_timeout_sec = 600.0
cpus = {cpus}
memory_mb = {memory_mb}
storage_mb = 10240
''')
    return d


@pytest.fixture
def workspace():
    """Task and job directories Harbor can use when it runs as another user (pytest's tmp_path
    is private to root)."""
    d = Path(tempfile.mkdtemp(prefix="rprof-harbor-"))
    d.chmod(0o755)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def harbor_task(workspace):
    def make(name: str, solve: str, cpus: int = 2, memory_mb: int = 1536) -> Path:
        task = make_task(workspace / "tasks", name, solve, cpus, memory_mb)
        give_to_user(workspace / "tasks")
        return task
    return make


@dataclass
class Trial:
    """One ``harbor run`` of a local task with the oracle agent, in the background."""
    proc: subprocess.Popen
    task: Path
    jobs: Path

    def container(self, timeout: float = 180) -> str:
        """The trial's main container: named ``<task dir>__<id>__env-main-1`` by Harbor."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = subprocess.run(["docker", "ps", "--filter", "label=com.docker.compose.service=main",
                                  "--format", "{{.ID}} {{.Names}}"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                cid, name = line.split(maxsplit=1)
                if name.startswith(f"{self.task.name}__"):
                    return cid
            if self.proc.poll() is not None:
                raise AssertionError(f"harbor exited ({self.proc.returncode}) before its container appeared:\n"
                                     + self.log()[-3000:])
            time.sleep(0.25)
        raise AssertionError("no Harbor container appeared")

    def wait(self, timeout: float = 300) -> int:
        return self.proc.wait(timeout=timeout)

    def log(self) -> str:
        return (self.jobs / "harbor.log").read_text(errors="replace")

    def reward(self) -> str | None:
        found = list(self.jobs.rglob("verifier/reward.txt"))
        return found[0].read_text().strip() if found else None


@pytest.fixture
def run_trial(harbor_bin, workspace):
    started: list[Trial] = []

    def start(task: Path) -> Trial:
        jobs = workspace / "jobs"
        jobs.mkdir(exist_ok=True)
        give_to_user(jobs)
        log = open(jobs / "harbor.log", "w")
        proc = subprocess.Popen(as_user([harbor_bin, "run", "-p", str(task), "-a", "oracle", "-o", str(jobs), "-y"]),
                                cwd=jobs, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        t = Trial(proc, task, jobs)
        started.append(t)
        return t

    yield start
    for t in started:
        if t.proc.poll() is None:
            t.proc.terminate()
            try:
                t.proc.wait(30)
            except subprocess.TimeoutExpired:
                t.proc.kill()
        out = subprocess.run(["docker", "ps", "-aq", "--filter", f"name={t.task.name}__"], capture_output=True,
                             text=True).stdout.split()
        if out:
            subprocess.run(["docker", "rm", "-f", *out], capture_output=True)


def cgroup_limits(cid: str) -> dict[str, str]:
    """The container's memory.max, memory.swap.max and cpu.max, read from the host."""
    pid = subprocess.run(["docker", "inspect", "-f", "{{.State.Pid}}", cid], capture_output=True,
                         text=True).stdout.strip()
    rel = Path(f"/proc/{pid}/cgroup").read_text().strip().split(":", 2)[2]
    base = Path("/sys/fs/cgroup") / rel.lstrip("/")
    return {f: (base / f).read_text().strip() for f in ("memory.max", "memory.swap.max", "cpu.max")}


@pytest.fixture
def limits():
    return cgroup_limits


def start_rprof(cid: str, runs: Path, *args: str) -> subprocess.Popen:
    """``rprof run`` attached to a container, with no command: it ends when the container exits."""
    return subprocess.Popen(RPROF + ["run", "--target", f"docker:{cid}", "--runs-dir", str(runs),
                                     "--view-dir", str(runs.parent / "view"), *args],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)


@pytest.fixture
def rprof():
    return start_rprof


def events(run_dir: Path) -> list[dict]:
    f = run_dir / "events.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines() if x.strip()] if f.exists() else []


def wait_event(runs: Path, pred, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for d in runs.glob("*"):
            for e in events(d):
                if pred(e):
                    return e
        time.sleep(0.1)
    raise AssertionError("event not seen")


@pytest.fixture
def wait_for_event():
    return wait_event
