"""``rprof run --target harbor -- harbor run ...``: follow the container a Harbor trial starts.

Harbor (https://github.com/harbor-framework/harbor) starts each trial's container itself, so the
target doesn't exist when rprof starts. rprof launches the ``harbor`` command, waits for the
trial's container (Docker Compose service ``main`` of a project named ``<trial>__env``), then for
the agent's first process in it, and only then attaches and starts the profile clock: time 0 is
when the agent starts, not when Harbor builds or sets up the environment.

Under sudo, ``harbor`` runs as the user who ran sudo: their Docker CLI has the Compose plugin
Harbor needs, and Harbor's job directory stays theirs. It talks to the same Docker daemon as
rprof. One rprof run follows one trial: pass one task and ``-k 1``.
"""

from __future__ import annotations

import os
import pwd
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .util import RprofError, run_cmd

DEFAULT_DOCKER_HOST = "unix:///var/run/docker.sock"

# A command-line fragment that marks each Harbor agent starting inside the container.
AGENT_START = {
    "terminus-2": "tmux new-session", "terminus-1": "tmux new-session", "terminus": "tmux new-session",
    "claude-code": "claude --verbose",
    "oracle": "solve.sh",
}
# Agents Harbor installs into the container: their own processes share its limits, so protect
# them from memory kills (rprof then resets the commands they start back to killable).
PROTECT = {"claude-code": "claude --verbose"}


def agent_of(argv: list[str]) -> str | None:
    """The ``-a``/``--agent`` value of a ``harbor run`` command line."""
    for i, a in enumerate(argv):
        if a in ("-a", "--agent") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--agent="):
            return a.split("=", 1)[1]
    return None


def _as_invoking_user(env: dict[str, str]) -> dict:
    """Popen arguments that run a command as the user who ran sudo (nothing if not under sudo)."""
    user = os.environ.get("SUDO_USER")
    if os.geteuid() != 0 or not user or user == "root":
        return {"env": env}
    pw = pwd.getpwnam(user)
    home = pw.pw_dir
    env = {**env, "HOME": home, "USER": user, "LOGNAME": user,
           # sudo's secure_path drops the user's own bin directories, where `uv tool` puts harbor.
           "PATH": f"{home}/.local/bin:{env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"}
    return {"env": env, "user": pw.pw_uid, "group": pw.pw_gid,
            "extra_groups": [g for g in os.getgrouplist(user, pw.pw_gid) if g != pw.pw_gid]}


def _main_containers() -> dict[str, tuple[str, str]]:
    """Running Compose ``main`` services: id -> (name, project)."""
    r = run_cmd(["docker", "ps", "--filter", "label=com.docker.compose.service=main", "--format",
                 '{{.ID}} {{.Names}} {{.Label "com.docker.compose.project"}}'], timeout=20, quiet=True)
    out = {}
    for line in r.out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            out[parts[0]] = (parts[1], parts[2])
    return out


def _cmdlines(pid: int) -> list[str]:
    """Command lines of every process in the cgroup of ``pid`` (the container)."""
    from .target.cgroup import Cgroup, cgroup_of_pid
    try:
        cg = Cgroup(cgroup_of_pid(pid))
    except OSError:
        return []
    out = []
    for p in cg.procs():
        try:
            out.append(Path(f"/proc/{p}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace"))
        except OSError:
            pass
    return out


def _trial_names(jobs: Path | None) -> set[str]:
    """Trial directories in a Harbor job directory, ``<jobs>/<job>/<trial>``, lowercased as Compose does."""
    if jobs is None or not jobs.is_dir():
        return set()
    return {d.name.lower() for d in jobs.glob("*/*__*") if d.is_dir()}


@dataclass
class Trial:
    container_id: str
    container: str
    trial: str                      # Harbor's trial name, lowercased by Docker Compose: the job's trial directory
    agent: str | None
    agent_start: str | None
    waited_container_s: float
    waited_agent_s: float | None = None

    def to_meta(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Launch:
    """The ``harbor`` command, started before its trial's container exists."""
    command: list[str]
    agent_start: str | None = None          # override for the agent's start marker
    say: Callable[[str], None] = print
    proc: subprocess.Popen | None = None
    before: set[str] = field(default_factory=set)
    jobs: Path | None = None                    # Harbor's job directory (-o), where its trial directory appears
    trials_before: set[str] = field(default_factory=set)

    def start(self) -> None:
        if not self.command:
            raise RprofError("--target harbor needs the harbor command after `--`", 2)
        env = dict(os.environ)
        env.setdefault("DOCKER_HOST", DEFAULT_DOCKER_HOST)    # the daemon rprof sees
        self.before = set(_main_containers())
        self.jobs = jobs_dir(self.command, Path.cwd())
        self.trials_before = _trial_names(self.jobs)
        try:
            self.proc = subprocess.Popen(self.command, **_as_invoking_user(env))
        except OSError as e:
            raise RprofError(f"cannot start {self.command[0]}: {e.strerror}", 127) from e

    def returncode(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def find_trial(self, poll_s: float = 0.25) -> Trial | None:
        """Wait for the trial's container, then for its agent to start. None if Harbor exits first."""
        t0 = time.monotonic()
        self.say("rprof: waiting for the Harbor trial's container")
        found = None
        while found is None:
            if self.returncode() is not None:
                return None
            found = self._my_container(time.monotonic() - t0)
            if found is None:
                time.sleep(poll_s)
        cid, name, project = found
        agent = agent_of(self.command)
        marker = self.agent_start or AGENT_START.get(agent or "")
        trial = Trial(cid, name, project[: -len("__env")], agent, marker, round(time.monotonic() - t0, 2))
        if not marker:
            self.say(f"rprof: no start marker for agent {agent!r}; starting the profile now "
                     "(give one with --agent-start)")
            return trial
        self.say(f"rprof: found {name}; waiting for the agent to start ({marker!r})")
        pid = run_cmd(["docker", "inspect", "-f", "{{.State.Pid}}", cid], timeout=20, quiet=True).out.strip()
        t1 = time.monotonic()
        while True:
            if self.returncode() is not None or not pid.isdigit() or int(pid) == 0:
                return None
            if any(marker in c for c in _cmdlines(int(pid))):
                trial.waited_agent_s = round(time.monotonic() - t1, 2)
                return trial
            if not Path(f"/proc/{pid}").exists():
                return None                                   # the container stopped first
            time.sleep(poll_s)

    def _my_container(self, waited_s: float) -> tuple[str, str, str] | None:
        """The new trial container that belongs to this Harbor run, if it has appeared.

        Several Harbor runs may start trials at once, so a new container is ours only if its
        Compose project (``<trial>__env``) names a trial directory that appeared in our job
        directory after we started. Parallel runs need their own ``-o`` directories.
        """
        new = {cid: v for cid, v in _main_containers().items() if cid not in self.before and v[1].endswith("__env")}
        mine = _trial_names(self.jobs) - self.trials_before
        if len(mine) > 1:
            raise RprofError(f"{len(mine)} Harbor trials started in {self.jobs}; rprof follows one: give harbor "
                             "one task and -k 1, and each parallel rprof run its own -o directory", 2)
        for cid, (name, project) in new.items():
            if project[: -len("__env")] in mine:
                return cid, name, project
        if not mine and self.jobs is not None and not self.jobs.exists() and len(new) == 1 and waited_s > 30:
            # No job directory where we expected one (an unusual -o?): fall back to the only new trial.
            self.say(f"rprof: Harbor's job directory {self.jobs} not found; following the only new trial")
            cid, (name, project) = next(iter(new.items()))
            return cid, name, project
        return None

    def wait(self, timeout: float | None = None) -> int:
        assert self.proc is not None
        return self.proc.wait(timeout=timeout)

    def stop(self, grace_s: float = 60.0) -> int | None:
        """Interrupt Harbor as Ctrl-C would (it then removes its containers); terminate it, then kill
        it, if it doesn't stop."""
        if self.proc is None or self.proc.poll() is not None:
            return self.returncode()
        for sig, wait in ((signal.SIGINT, grace_s), (signal.SIGTERM, 15.0), (signal.SIGKILL, 15.0)):
            try:
                self.proc.send_signal(sig)
                return self.proc.wait(wait)
            except subprocess.TimeoutExpired:
                continue
            except ProcessLookupError:
                break
        return self.returncode()


def jobs_dir(argv: list[str], cwd: Path) -> Path:
    """Where ``harbor run`` writes its job: ``-o``/``--jobs-dir``, else ``jobs`` in its working directory."""
    for i, a in enumerate(argv):
        if a in ("-o", "--jobs-dir") and i + 1 < len(argv):
            return (cwd / argv[i + 1]).resolve()
        if a.startswith("--jobs-dir="):
            return (cwd / a.split("=", 1)[1]).resolve()
    return (cwd / "jobs").resolve()


def find_trial_dir(jobs: Path, trial: str) -> Path | None:
    """The trial's directory, ``<jobs>/<job>/<trial>``; Compose lowercased the name rprof saw."""
    found = [d for d in jobs.glob("*/*") if d.is_dir() and d.name.lower() == trial.lower()]
    return max(found, key=lambda d: d.stat().st_mtime) if found else None


def keep_trial(launch: Launch, trial: Trial, run_dir: Path, cwd: Path) -> dict:
    """Copy the trial's directory (agent logs and trajectory, verifier output, result) into the run
    directory, so each rprof run keeps what its agent did. Returns what to record in meta.json."""
    import shutil
    jobs = jobs_dir(launch.command, cwd)
    src = find_trial_dir(jobs, trial.trial)
    if src is None:
        return {"trial_dir": None, "kept": None, "jobs_dir": str(jobs)}
    dest = run_dir / "harbor-trial"
    shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
    return {"trial_dir": str(src), "kept": dest.name, "jobs_dir": str(jobs)}


__all__ = ["AGENT_START", "PROTECT", "Launch", "Trial", "agent_of", "jobs_dir", "find_trial_dir", "keep_trial"]
