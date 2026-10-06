"""Fixes from the 395-run review, checked against the real kernel."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid

import pytest

from conftest import MiB, ROOT, Sandbox
from rprof import mask
from rprof.report.data import RunData
from rprof.report.report import build_report

pytestmark = pytest.mark.integration
RPROF = [sys.executable, "-m", "rprof.cli"]
LIMITED = {"version": 1, "name": "limited", "segments": [{"from": 0, "to": 3600, "mem": {"max": "256Mi"}}]}


def _mem_row(rep: dict, seg: int = 1) -> dict:
    s = next(x for x in rep["segments"] if x["segment"] == seg)
    return next(k for k in s["knobs"] if k["knob"] == "mem.max")


def test_reading_a_large_file_is_not_a_violation(sandbox, rprof_factory):
    """Review check for #1: page cache from reading a file doesn't count against the limit."""
    rp = rprof_factory(sandbox, {**LIMITED, "name": "cache"}, mode="measure")
    r = rp.run("dd if=/dev/zero of=/var/tmp/big bs=1M count=512 status=none && sync && "
               "cat /var/tmp/big > /dev/null && cat /var/tmp/big > /dev/null")
    assert r.exit_code == 0, r.output
    rp.stop()
    rd = RunData(rp.run_dir)
    row = _mem_row(build_report(rd))
    assert row["basis"] == "non_reclaimable"
    assert row["usage_total"]["max"] > 400 * MiB          # the page cache was there...
    assert row["violation"]["time_frac"] == 0.0, row       # ...but it isn't a violation
    total = _mem_row(build_report(rd, memory_basis="total"))
    assert total["violation"]["time_frac"] > 0


def test_oom_kill_in_a_call_that_exits_0(sandbox, rprof_factory):
    """#2: `a; b` hides a's exit status; rprof still reports the kill."""
    rp = rprof_factory(sandbox, LIMITED)
    r = rp.run("hog-mem 1G 2; true")
    assert r.exit_code == 0
    assert r.cause == "memory"
    assert r.explain == "A process was killed: memory limit 256 MiB reached, but the call exited 0."
    rp.stop()
    end = next(e for e in rp.events("tool_end") if e["call_id"] == r.call_id)
    assert end["evidence"]["exited_ok"] is True and end["evidence"]["oom_kill"] >= 1
    # The allocation can rise, be killed and the call return within one 50 ms sample. rprof also
    # reads the peak at tool_end (since the last sample on Linux 6.12+, or a new lifetime high on
    # older kernels), so the spike still counts; this once flaked at 8 MiB on a fast CI runner.
    assert end["mem_peak_bytes"] > 64 * MiB


def test_oom_reported_by_the_program(sandbox, rprof_factory):
    """#3: a program that fails its own allocation exits non-zero without any kernel OOM event."""
    rp = rprof_factory(sandbox, LIMITED)
    r = rp.run("python3 -c 'bytearray(1 << 44)'")
    assert r.exit_code == 1 and "MemoryError" in r.output
    assert r.cause == "memory"
    assert "the program reported" in r.explain and "MemoryError" in r.explain


def test_init_shim_child_survives_a_squeeze(rprof_factory):
    """#5: with --init, PID 1 is docker-init and the container dies with its child."""
    sb = Sandbox(f"rprof-it-init-{uuid.uuid4().hex[:6]}", extra=["--init", "--tmpfs", "/scratch:size=1g"])
    try:
        # 300 MiB of tmpfs data: charged to the container, and not reclaimable without swap.
        rc, out, _, _ = sb.exec("head -c 300M /dev/zero > /scratch/fill")
        assert rc == 0, out
        rp = rprof_factory(sb)
        rp.apply({"mem.max": "64Mi"})               # far below usage: the kernel looks for a victim
        time.sleep(1)
        assert sb.is_running(), "the sandbox died with its init's child"
        rp.stop()
        codes = [e["code"] for e in rp.events("warning")]
        assert "mem_max_below_usage" in codes
        protected = {e["pid"] for e in rp.events("protect")}
        assert len(protected) == 2                  # docker-init and its child, by host PID
    finally:
        sb.rm()


def test_hide_limits(rprof_factory):
    """#7: with --hide-limits the sandbox reads "max" while the real limit is enforced."""
    sb = Sandbox(f"rprof-it-hide-{uuid.uuid4().hex[:6]}", extra=["--memory=1g"])
    try:
        before = sb.exec("cat /sys/fs/cgroup/memory.max")[1].strip()
        assert before == str(1 << 30)
        rp = rprof_factory(sb, LIMITED, hide_limits=True)
        assert sb.exec("cat /sys/fs/cgroup/memory.max")[1].strip() == "max"
        assert sorted(sb.exec("ls /sys/fs/cgroup")[1].split()) == sorted(mask.MASK_FILES)
        r = rp.run("hog-mem 512M 2")                # the real 256 MiB limit still holds
        assert r.exit_code == 137 and r.cause == "memory"
        rp.stop()
        assert sb.exec("cat /sys/fs/cgroup/memory.max")[1].strip() == before
        assert not mask.is_masked(sb.pid)
    finally:
        sb.rm()


def test_hide_limits_undone_by_reset_after_sigkill(sandbox, tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text(json.dumps(LIMITED))
    proc = subprocess.Popen(RPROF + ["run", "--target", f"docker:{sandbox.name}", "--profile", str(p),
                                     "--hide-limits", "--runs-dir", str(tmp_path / "runs"),
                                     "--view-dir", str(tmp_path / "view"), "--", "sleep", "60"],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(100):
            if mask.is_masked(sandbox.pid):
                break
            time.sleep(0.1)
        assert mask.is_masked(sandbox.pid)
        proc.kill()
        proc.wait(10)
        assert mask.is_masked(sandbox.pid)          # killed outright: nothing was undone
        run_dir = next((tmp_path / "runs").glob("*"))
        r = subprocess.run(RPROF + ["reset", "--run", str(run_dir)], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert not mask.is_masked(sandbox.pid)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_long_runs_dir(sandbox, tmp_path):
    """#6: a run directory too deep for a Unix socket path still works."""
    runs = tmp_path / ("a" * 50) / ("b" * 50)
    probe = "from rprof.client import Client; c = Client(); print('mode', c.state().mode)"
    r = subprocess.run(RPROF + ["run", "--target", f"docker:{sandbox.name}", "--runs-dir", str(runs),
                                "--name", "a_long_run.label", "--", sys.executable, "-c", probe],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "mode enforce" in r.stdout
    run_dir = next(runs.glob("*a_long_run.label"))
    assert len(str(run_dir / "control.sock")) > 103
    assert not (run_dir / "control.sock").is_symlink()     # removed at the end


@pytest.mark.timing
def test_socket_and_sampler_survive_a_frozen_run_dir(sandbox, data_fs):
    """#4 regression: every write to the run directory blocks for 5 s (fsfreeze).

    Before the fix the sampler, and the event loop serving the socket, stalled for the whole
    freeze. Now writes wait in background threads and nothing else does.
    """
    from conftest import Rprof
    from rprof.util import read_jsonl
    rp = Rprof(sandbox, data_fs, hz=10)          # run directory and agent view on the loop filesystem
    lat: list[float] = []
    try:
        time.sleep(1.0)
        subprocess.run(["fsfreeze", "-f", str(data_fs)], check=True)
        try:
            t_end = time.monotonic() + 5.0
            r = rp.run("true")                    # a whole tool call during the freeze
            while time.monotonic() < t_end:
                a = time.monotonic()
                rp.client.state()
                lat.append(time.monotonic() - a)
                time.sleep(0.05)
        finally:
            subprocess.run(["fsfreeze", "-u", str(data_fs)], check=True)
        assert r.exit_code == 0
        time.sleep(0.5)
    finally:
        rp.stop()
    assert max(lat) < 0.5, f"state took {max(lat):.2f} s while the run directory was frozen"
    gaps = [b["t"] - a["t"] for a, b in zip(rp.samples, rp.samples[1:])]
    assert max(gaps) < 0.5, f"sampling stalled for {max(gaps):.2f} s"
    assert len(read_jsonl(rp.run_dir / "samples.jsonl")) == len(rp.samples)   # nothing lost after the thaw
    assert not any(e["code"] == "write_failed" for e in rp.events("error"))
