"""Integration fixtures: a fresh rprof-testbox per test, rprof running in-process at 20 Hz.

Needs root, Docker and cgroup v2 (run on the experiment host):
    sudo .venv/bin/pytest tests/integration -m "not slow"
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from rprof import knobs as K
from rprof.client import Client
from rprof.profile import Profile, profile_from_dict, unlimited_profile
from rprof.runner import RunOptions, RunSession
from rprof.target.cgroup import is_cgroup2

MiB = 1 << 20
ROOT = Path(__file__).resolve().parents[2]
TOL = yaml.safe_load((ROOT / "tests" / "tolerances.yaml").read_text())
IMAGE = os.environ.get("RPROF_TEST_IMAGE", "rprof-testbox")
PEER_IMAGE = os.environ.get("RPROF_PEER_IMAGE", "rprof-netpeer")


def _have_image(img: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", img], capture_output=True).returncode == 0


def pytest_collection_modifyitems(config, items):
    # Timing-based tests get one retry (design: "Keeping timing tests reliable").
    if config.pluginmanager.hasplugin("rerunfailures"):
        for it in items:
            if it.get_closest_marker("timing") and not it.get_closest_marker("flaky"):
                it.add_marker(pytest.mark.flaky(reruns=1))
    reason = None
    if os.geteuid() != 0:
        reason = "integration tests need root"
    elif not is_cgroup2():
        reason = "integration tests need cgroup v2"
    elif shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode:
        reason = "integration tests need a Docker daemon"
    elif not _have_image(IMAGE):
        reason = f"build the test image first: docker build -t {IMAGE} images/testbox"
    if reason:
        for it in items:
            if "integration" in str(it.fspath) or "e2e" in str(it.fspath):
                it.add_marker(pytest.mark.skip(reason=reason))


def sh(*args, check=True, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, check=check, timeout=timeout)


class Sandbox:
    def __init__(self, name: str, image: str = IMAGE, network: str | None = None, extra: list[str] | None = None,
                 cmd: list[str] | None = None, slice_: str | None = None):
        self.name = name
        self.slice = slice_ or f"rprof-it{uuid.uuid4().hex[:6]}.slice"
        args = ["docker", "run", "-d", "--name", name, f"--cgroup-parent={self.slice}"]
        if network:
            args += ["--network", network]
        args += extra or []
        sh(*args, image, *(cmd if cmd is not None else ["sleep", "infinity"]))

    def exec(self, cmd: str, timeout: float | None = 120, user: str | None = None):
        t0 = time.monotonic()
        argv = ["docker", "exec"] + (["-u", user] if user else []) + [self.name, "sh", "-c", cmd]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout + p.stderr, time.monotonic() - t0, False
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return None, out, time.monotonic() - t0, True

    def is_running(self) -> bool:
        p = sh("docker", "inspect", "-f", "{{.State.Running}}", self.name, check=False)
        return p.stdout.strip() == "true"

    @property
    def pid(self) -> int:
        return int(sh("docker", "inspect", "-f", "{{.State.Pid}}", self.name).stdout.strip())

    def rm(self):
        sh("docker", "rm", "-f", self.name, check=False)


@dataclass
class Result:
    call_id: str
    exit_code: int | None
    output: str
    duration: float
    timed_out: bool
    t0: float
    t1: float
    cause: str | None
    explain: str | None
    segment: int | None


def get(d, path: str):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


class Window:
    def __init__(self, samples: list[dict], t0: float, t1: float):
        inside = [s for s in samples if t0 <= s["t"] <= t1]
        after = [s for s in samples if s["t"] > t1][:1]
        before = [s for s in samples if s["t"] < t0][-1:]
        self.samples = before + inside + after
        self.t0, self.t1 = t0, t1

    def vals(self, path: str) -> list[float]:
        return [v for v in (get(s, path) for s in self.samples) if v is not None]

    def delta(self, path: str) -> float:
        v = self.vals(path)
        return (v[-1] - v[0]) if len(v) >= 2 else 0.0

    def max(self, path: str) -> float:
        return max(self.vals(path))

    def min(self, path: str) -> float:
        return min(self.vals(path))

    def rate(self, path: str) -> float:
        s = [x for x in self.samples if get(x, path) is not None]
        dt = s[-1]["t"] - s[0]["t"]
        return (get(s[-1], path) - get(s[0], path)) / dt if dt > 0 else 0.0

    def cores(self) -> float:
        return self.rate("cpu.usage_usec") / 1e6


class Rprof:
    """rprof running in-process against one sandbox."""

    def __init__(self, sandbox: Sandbox, tmp: Path, profile: Profile | dict | None = None, hz: float = 20,
                 mode: str = "enforce", **opts):
        if isinstance(profile, dict):
            profile = profile_from_dict(profile)
        self.sandbox = sandbox
        self.samples: list[dict] = []
        opts.setdefault("duration", 1e6)  # in-process runs end when the test stops them, not at profile end
        o = RunOptions(target=f"docker:{sandbox.name}", runs_dir=str(tmp / "runs"), hz=hz, mode=mode,
                       self_cgroup=False, view_dir=str(tmp / "view"), quiet=True, **opts)
        self.sess = RunSession(o, profile or unlimited_profile("it"))
        self.sess.setup()
        self.sess.sampler.listeners.append(self.samples.append)
        self.code: int | None = None
        self._err: BaseException | None = None
        self.thread = threading.Thread(target=self._main, daemon=True)
        self.thread.start()
        for _ in range(200):
            if getattr(self.sess, "loop", None) is not None and self.samples:
                break
            time.sleep(0.02)
        self.client = Client(run_dir=str(self.sess.run_dir), timeout_s=5)
        self._n = 0
        self.stopped = False

    def _main(self):
        import asyncio
        try:
            asyncio.run(self.sess.main())
        except BaseException as e:  # noqa: BLE001
            self._err = e

    @property
    def run_dir(self) -> Path:
        return self.sess.run_dir

    def now(self) -> float:
        return self.sess.clock.now()

    def apply(self, raw: dict) -> float:
        vals = {k: (K.KNOBS[k].parse(v) if not K.is_unified(k) else v) for k, v in raw.items()}
        errs, ms = self.sess.apply_now(vals)
        assert not errs, errs
        return ms

    def run(self, cmd: str, timeout: float | None = 120, call_id: str | None = None) -> Result:
        self._n += 1
        cid = call_id or f"c{self._n}"
        info = self.client.tool_start(cid, cmd, step=self._n)
        t0 = self.now()
        rc, out, dur, to = self.sandbox.exec(cmd, timeout=timeout)
        fb = self.client.tool_end(cid, rc, dur, to, output=out)
        return Result(cid, rc, out, dur, to, t0, self.now(), fb.cause, fb.explain, info.segment)

    def window(self, r: Result) -> Window:
        # Counters for the end of the call land in the first tick after it: wait for that tick.
        deadline = time.monotonic() + 2.0
        while not (self.samples and self.samples[-1]["t"] > r.t1) and time.monotonic() < deadline:
            time.sleep(0.01)
        return Window(self.samples, r.t0, r.t1)

    def events(self, type_: str | None = None) -> list[dict]:
        import json
        p = self.run_dir / "events.jsonl"
        evs = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        return [e for e in evs if type_ is None or e["type"] == type_]

    def stop(self) -> int:
        if self.stopped:
            return self.code or 0
        self.stopped = True
        self.sess.request_stop("profile_end")
        self.thread.join(30)
        self.code = self.sess.teardown()
        if self._err:
            raise self._err
        return self.code


@pytest.fixture
def sandbox():
    sb = Sandbox(f"rprof-it-{uuid.uuid4().hex[:8]}")
    yield sb
    sb.rm()


@pytest.fixture
def rprof_factory(tmp_path):
    made: list[Rprof] = []

    def make(sb: Sandbox, profile=None, **kw) -> Rprof:
        r = Rprof(sb, tmp_path, profile, **kw)
        made.append(r)
        return r
    yield make
    for r in made:
        try:
            r.stop()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def rprof(sandbox, rprof_factory):
    return rprof_factory(sandbox)


@pytest.fixture
def netpeer():
    if not _have_image(PEER_IMAGE):
        pytest.skip(f"build {PEER_IMAGE}: docker build -t {PEER_IMAGE} images/netpeer")
    tag = uuid.uuid4().hex[:6]
    net = f"rprof-it-net-{tag}"
    sh("docker", "network", "create", net)
    peer = Sandbox(f"rprof-it-peer-{tag}", image=PEER_IMAGE, network=net, cmd=[])
    # Wait until iperf3 (5201 = 0x1451) and HTTP (80 = 0x0050) are in LISTEN (0A) state; no probe connects.
    for _ in range(100):
        rc, out, _, _ = peer.exec("cat /proc/net/tcp /proc/net/tcp6", timeout=10)
        listening = {ln.split()[1].rsplit(":", 1)[1] for ln in out.splitlines()[1:]
                     if len(ln.split()) > 3 and ln.split()[3] == "0A"}
        if {"1451", "0050"} <= listening:
            break
        time.sleep(0.1)
    yield net, peer.name
    peer.rm()
    sh("docker", "network", "rm", net, check=False)


@pytest.fixture
def net_sandbox(netpeer):
    net, _ = netpeer
    sb = Sandbox(f"rprof-it-{uuid.uuid4().hex[:8]}", network=net)
    yield sb
    sb.rm()


@pytest.fixture
def data_fs(tmp_path):
    """A 512 MiB loop-mounted ext4 (mkfs -m 0), as the design recommends for disk.capacity."""
    img = tmp_path / "data.img"
    mnt = tmp_path / "data"
    mnt.mkdir()
    sh("fallocate", "-l", "512M", str(img))
    sh("mkfs.ext4", "-q", "-m", "0", "-F", str(img))
    sh("mount", "-o", "loop", str(img), str(mnt))
    yield mnt
    sh("umount", str(mnt), check=False)


def tol(section: str, key: str):
    return TOL[section][key]
