"""Restore and safety (design: Testing plan)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from conftest import ROOT, Sandbox, sh

pytestmark = pytest.mark.integration
RPROF = [sys.executable, "-m", "rprof.cli"]

FULL_PROFILE = {
    "version": 1, "name": "restore", "visibility": "full",
    "defaults": {"cpu": {"cores": 1.5, "cpus": "0-3"}, "mem": {"high": "900Mi", "max": "1Gi", "swap_max": 0},
                 "io": {"wbps": "50Mi", "riops": 1000}, "pids": {"max": 100},
                 "net": {"loss": "5%", "delay": "10ms", "partition": "reject", "allow": ["10.0.0.0/8"]},
                 "disk": {"capacity": "64Mi"}},
}


def _state(sb: Sandbox, data: Path | None):
    pid = sb.pid
    cg = Path("/sys/fs/cgroup") / open(f"/proc/{pid}/cgroup").read().strip().split("::")[1].lstrip("/")
    files = {f: (cg / f).read_text() for f in ("cpu.max", "cpuset.cpus.effective", "memory.high", "memory.max",
                                              "memory.swap.max", "io.max", "pids.max")}
    files["oom_score_adj"] = Path(f"/proc/{pid}/oom_score_adj").read_text()
    q = sh("nsenter", "-t", str(pid), "-n", "tc", "qdisc", "show").stdout
    ipt = sh("nsenter", "-t", str(pid), "-n", "iptables", "-S").stdout
    veth = sh("tc", "qdisc", "show").stdout
    return files, q, ipt, veth, (data / ".rprof-ballast").exists() if data else False


def _start(sb: Sandbox, tmp: Path, profile: dict, *extra, cmd=("sleep", "120")):
    p = tmp / "p.yaml"
    p.write_text(yaml.safe_dump(profile))
    return subprocess.Popen(RPROF + ["run", "--target", f"docker:{sb.name}", "--profile", str(p),
                                     "--runs-dir", str(tmp / "runs"), "--view-dir", str(tmp / "view"), *extra,
                                     "--", *cmd], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)


def _wait_applied(tmp: Path, timeout=15):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        for ev in (tmp / "runs").glob("*/events.jsonl"):
            if any('"segment_applied"' in ln for ln in ev.read_text().splitlines()):
                return ev.parent
        time.sleep(0.1)
    raise AssertionError("run never applied its limits")


@pytest.fixture
def full_sandbox(data_fs, netpeer):
    net, _ = netpeer
    sb = Sandbox(f"rprof-it-rs-{os.getpid()}-{int(time.time()) % 10000}", network=net,
                 extra=["-v", f"{data_fs}:/data"])
    yield sb, data_fs
    sb.rm()


@pytest.mark.parametrize("how", ["sigterm", "sigkill"])
def test_restore(full_sandbox, tmp_path, how):
    sb, data = full_sandbox
    before = _state(sb, data)
    proc = _start(sb, tmp_path, FULL_PROFILE)
    run_dir = _wait_applied(tmp_path)
    time.sleep(1.5)
    during = _state(sb, data)
    assert during[0]["memory.max"].strip() == str(1 << 30)
    assert "netem" in during[1] and "RPROF-OUT" in during[2] and during[4]
    assert during[0]["oom_score_adj"].strip() == "-1000"
    if how == "sigkill":
        proc.kill()                     # rprof dies; its command (sleep) is orphaned and holds the pipes
        proc.wait(timeout=30)
        os.killpg(proc.pid, signal.SIGKILL)
        err = ""
    else:
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
    if how == "sigkill":
        assert _state(sb, data)[0]["memory.max"].strip() == str(1 << 30)  # nothing restored yet
        r = subprocess.run(RPROF + ["reset", "--run", str(run_dir)], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    else:
        assert "ended (signal)" in err, err
    after = _state(sb, data)
    assert after[0] == before[0], {k: (before[0][k], after[0][k]) for k in before[0] if before[0][k] != after[0][k]}
    assert "netem" not in after[1] and "prio" not in after[1]
    assert "RPROF" not in after[2]
    assert after[3] == before[3]
    assert not after[4]


def test_lock(sandbox, tmp_path):
    proc = _start(sandbox, tmp_path, {"version": 1, "name": "lock", "defaults": {"pids": {"max": 500}}})
    try:
        _wait_applied(tmp_path)
        p = tmp_path / "p2.yaml"
        p.write_text("version: 1\nname: second\n")
        r = subprocess.run(RPROF + ["run", "--target", f"docker:{sandbox.name}", "--profile", str(p),
                                    "--runs-dir", str(tmp_path / "runs2"), "--", "true"],
                           cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert r.returncode == 74, (r.returncode, r.stderr)
        assert "locked" in r.stderr
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=60)


def test_protect(rprof_factory, tmp_path):
    # PID 1 is the biggest process, so without protection the kernel would pick it.
    sb = Sandbox(f"rprof-it-prot-{int(time.time() * 1000) % 100000}", cmd=["hog-mem", "300M", "3600"])
    try:
        time.sleep(2)
        rp = rprof_factory(sb)
        assert Path(f"/proc/{sb.pid}/oom_score_adj").read_text().strip() == "-1000"
        import threading
        res = {}
        th = threading.Thread(target=lambda: res.setdefault("r", rp.run("hog-mem 200M 10")))
        th.start()
        time.sleep(2.5)
        rp.apply({"mem.max": "400Mi", "mem.swap_max": 0})
        th.join()
        assert res["r"].exit_code == 137
        assert sb.is_running()
        rp.stop()
        assert Path(f"/proc/{sb.pid}/oom_score_adj").read_text().strip() == "0"
    finally:
        sb.rm()


INSIDE_HARNESS = r'''
import os, subprocess, sys, time
sys.path.insert(0, "/harness")
from client import Client
heavy = b"h" * (300 << 20)          # a heavy harness: the biggest process when the limit hits
rp = Client()
time.sleep(0.5)                     # rprof protects new processes within one sampler tick
info = rp.tool_start("c1", "hog-mem 512M 5")
t0 = time.monotonic()
p = subprocess.run(["hog-mem", "512M", "5"])
fb = rp.tool_end("c1", 128 - p.returncode if p.returncode < 0 else p.returncode, time.monotonic() - t0)
print("inside_harness", p.returncode, fb.cause, fb.explain, flush=True)
rp.mark("task_done")
'''


def test_inside_harness(tmp_path):
    hdir = tmp_path / "harness"
    hdir.mkdir()
    (hdir / "inside_harness.py").write_text(INSIDE_HARNESS)
    (hdir / "client.py").write_text((ROOT / "src" / "rprof" / "client.py").read_text())
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    sb = Sandbox(f"rprof-it-in-{int(time.time() * 1000) % 100000}",
                 extra=["-v", f"{hdir}:/harness:ro", "-v", f"{ctl}:/run/rprof-ctl"])
    try:
        prof = {"version": 1, "name": "inside", "defaults": {"mem": {"max": "450Mi", "swap_max": 0}}}
        proc = _start(sb, tmp_path, prof, "--harness", "inside", "--ctl-dir", str(ctl), "--protect", "inside_harness",
                      cmd=("docker", "exec", "-e", "RPROF_HARNESS_TOKEN", "-e", "RPROF_SOCKET", sb.name,
                           "python3", "/harness/inside_harness.py"))
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, (out, err)
        assert "inside_harness -9 memory" in out, out
        run_dir = next((tmp_path / "runs").glob("*"))
        evs = [json.loads(x) for x in (run_dir / "events.jsonl").read_text().splitlines()]
        assert any(e["type"] == "mark" and e["label"] == "task_done" for e in evs)
        assert any(e["type"] == "protect" and "inside_harness" in e.get("cmd", "") for e in evs)
        assert (run_dir / "token").stat().st_mode & 0o777 == 0o600 or os.environ.get("SUDO_UID")
    finally:
        sb.rm()


def test_attached_run_records_past_the_last_segment_until_the_container_exits(tmp_path):
    """No command: the run outlives the profile, keeps its defaults in force, and ends cleanly
    when the container stops (how `rprof run` is attached to a container another tool started)."""
    sb = Sandbox(f"rprof-it-exit-{os.getpid()}", cmd=["sleep", "8"])
    try:
        p = tmp_path / "p.yaml"
        p.write_text(yaml.safe_dump({"version": 1, "name": "exit", "defaults": {"mem": {"max": "1Gi"}},
                                     "segments": [{"from": 1, "to": 3, "mem": {"max": "2Gi"}}]}))
        r = subprocess.run(RPROF + ["run", "--target", f"docker:{sb.name}", "--profile", str(p),
                                    "--runs-dir", str(tmp_path / "runs"), "--view-dir", str(tmp_path / "view")],
                           cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        run = next((tmp_path / "runs").iterdir())
        meta = json.loads((run / "meta.json").read_text())
        assert meta["end_reason"] == "target_exit"
        assert 5 < meta["duration_s"] < 10                      # past the segment's end at 3 s
        events = [json.loads(x) for x in (run / "events.jsonl").read_text().splitlines()]
        back = [e for e in events if e["type"] == "segment_applied" and e["boundary"] == 3]
        assert back and back[0]["limits"]["mem"]["max"] == 1 << 30   # the defaults, after the segment
        assert any(e["type"] == "target_exit" for e in events)
        assert not any(e["type"] == "error" for e in events)
        assert (run / "report.json").exists()
    finally:
        sb.rm()
