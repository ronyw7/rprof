"""Schedule: do limits change at the right moment? (design: Testing plan)"""

from __future__ import annotations

import random
import threading
import time

import pytest

from conftest import MiB, tol

pytestmark = pytest.mark.integration


def _tick_cores(samples):
    out = []
    for a, b in zip(samples, samples[1:]):
        dt = b["t"] - a["t"]
        out.append((b["t"], (b["cpu"]["usage_usec"] - a["cpu"]["usage_usec"]) / 1e6 / dt))
    return out


@pytest.mark.timing
def test_wall_steps(sandbox, rprof_factory):
    prof = {"version": 1, "name": "wall-steps", "defaults": {"cpu": {"cores": 4}},
            "segments": [{"from": 5, "to": 10, "cpu": {"cores": 0.5}}]}
    rp = rprof_factory(sandbox, prof)
    r = rp.run("stress-ng --cpu 4 --timeout 14s")
    assert r.exit_code == 0
    ticks = _tick_cores(rp.samples)
    period = 1 / rp.sess.opts.hz
    # One sample of tracking lag, one tick for the rate window, one CFS period (100 ms) for the quota to bite.
    slack = (tol("schedule", "boundary_samples") + 1) * period + 0.1
    down = next(t for t, c in ticks if t > 5 and c < 2.25)
    up = next(t for t, c in ticks if t > 10 and c > 2.25)
    assert down - 5 <= slack, down
    assert up - 10 <= slack, up
    mid = [c for t, c in ticks if 6 < t < 9.5]
    assert abs(sum(mid) / len(mid) - 0.5) < 0.1


def test_mem_boundary(sandbox, rprof_factory):
    prof = {"version": 1, "name": "mem-boundary",
            "segments": [{"from": 5, "to": 60, "mem": {"max": "256Mi", "swap_max": 0}}]}
    rp = rprof_factory(sandbox, prof)
    r = rp.run("hog-mem 512M 20", timeout=60)
    assert r.exit_code == 137
    rp.window(r)    # the kill shows up in the first sample after the call returns: wait for it
    kill = next(s["t"] for s in rp.samples if s["mem"]["events"].get("oom_kill", 0) > 0)
    assert 5 <= kill <= 5 + tol("schedule", "mem_boundary_s"), kill
    assert r.cause == "memory"


def test_event_independence(sandbox, rprof_factory):
    prof = {"version": 1, "name": "indep", "defaults": {"cpu": {"cores": 2}},
            "segments": [{"from": 2, "to": 4, "cpu": {"cores": 0.5}}, {"from": 6, "to": 8, "cpu": {"cores": 1}}]}
    rp = rprof_factory(sandbox, prof)
    rng = random.Random(1)
    stop = time.monotonic() + 9

    def harness(k):
        i = 0
        while time.monotonic() < stop:
            time.sleep(rng.uniform(0.05, 0.4))
            i += 1
            rp.run("true", call_id=f"h{k}-{i}")
    ths = [threading.Thread(target=harness, args=(k,)) for k in range(3)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    rp.stop()
    ev = rp.events()
    applied = [e for e in ev if e["type"] == "segment_applied"]
    assert [e["boundary"] for e in applied] == [0.0, 2.0, 4.0, 6.0, 8.0]
    assert all(e["late_ms"] <= tol("schedule", "apply_late_ms") for e in applied), [e["late_ms"] for e in applied]
    assert all(abs(e["t"] - e["boundary"]) < 0.05 for e in applied)
    assert len([e for e in ev if e["type"] == "tool_start"]) > 20


def test_mid_call_change(sandbox, rprof_factory):
    prof = {"version": 1, "name": "midcall", "segments": [{"from": 3, "to": 30, "cpu": {"cores": 0.5}}]}
    rp = rprof_factory(sandbox, prof)
    r = rp.run("stress-ng --cpu 1 --timeout 6s")
    assert r.t0 < 3 < r.t1
    rp.stop()
    import json
    rep = json.loads((rp.run_dir / "report.json").read_text())
    call = next(c for c in rep["calls"] if c["call_id"] == r.call_id)
    assert call["segments"] == [0, 1]
    assert call["limit_changes_during_call"] == [3.0]


def test_overlap(sandbox, rprof_factory):
    prof = {"version": 1, "name": "overlap",
            "segments": [{"from": 1, "to": 6, "mem": {"max": "512Mi"}}, {"from": 3, "to": 8, "pids": {"max": 50}}]}
    rp = rprof_factory(sandbox, prof)
    cg = rp.sess.target.cgroup
    while rp.now() < 4:
        time.sleep(0.05)
    assert cg.read("memory.max").strip() == str(512 * MiB)
    assert cg.read("pids.max").strip() == "50"
    while rp.now() < 7:
        time.sleep(0.05)
    assert cg.read("memory.max").strip() == "max"
    assert cg.read("pids.max").strip() == "50"


def test_unified_files(sandbox, rprof_factory):
    """A segment's `unified` map writes raw cgroup files and puts them back afterwards."""
    prof = {"version": 1, "name": "unified",
            "segments": [{"from": 1, "to": 3, "unified": {"cpu.weight": "50", "memory.low": "64M"}}]}
    rp = rprof_factory(sandbox, prof)
    cg = rp.sess.target.cgroup
    before = (cg.read("cpu.weight"), cg.read("memory.low"))
    while rp.now() < 2:
        time.sleep(0.05)
    assert cg.read("cpu.weight").strip() == "50"
    assert cg.read("memory.low").strip() == str(64 * MiB)
    while rp.now() < 3.5:
        time.sleep(0.05)
    assert (cg.read("cpu.weight"), cg.read("memory.low")) == before
