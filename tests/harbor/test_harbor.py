"""rprof with real Harbor trials.

1. One command, `rprof run --target harbor -- harbor run ...`, runs a trial under a profile from
   the moment its agent starts, and ends cleanly when Harbor's container exits.
2. Attached by hand (`--target docker:<id>`), a profile's limits replace the ones Harbor sets
   from task.toml, and Harbor's come back if rprof stops before the container does.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time

import pytest

from harbor_trial import (RPROF, ROOT, give_to_user, harbor_bin, harbor_task, limits,  # noqa: F401 (fixtures)
                          rprof, run_trial, wait_for_event, workspace)
from rprof.report.data import RunData

pytestmark = [pytest.mark.harbor, pytest.mark.slow]
MiB = 1 << 20


def test_one_command_runs_a_harbor_trial_under_a_profile(harbor_bin, harbor_task, workspace, tmp_path):
    """`rprof run --target harbor -- harbor run ...`: rprof starts Harbor, waits for the trial's agent,
    applies the profile from then on, records until the container exits, and exits with Harbor's code."""
    task = harbor_task("rprof-oneshot", "hog-mem 256M 4\nsleep 4", cpus=2, memory_mb=1536)
    jobs = workspace / "jobs"
    jobs.mkdir()
    give_to_user(jobs)
    p = tmp_path / "p.yaml"
    p.write_text("version: 1\nname: oneshot\nvisibility: full\n"
                 "defaults: {cpu: {cores: 2}, mem: {max: 1536Mi, swap_max: 1536Mi}}\n"
                 "segments:\n  - {from: 1, to: 3, cpu: {cores: 0.5}}\n")
    runs = tmp_path / "runs"
    r = subprocess.run(RPROF + ["run", "--target", "harbor", "--profile", str(p), "--runs-dir", str(runs),
                                "--view-dir", str(tmp_path / "view"), "--tell-agent", "--",
                                harbor_bin, "run", "-p", str(task), "-a", "oracle", "-o", str(jobs), "-y"],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-3000:]
    assert "waiting for the agent to start ('solve.sh')" in r.stderr

    run = next(runs.iterdir())
    meta = json.loads((run / "meta.json").read_text())
    assert meta["end_reason"] == "target_exit"
    h = meta["harbor"]
    assert h["agent"] == "oracle" and h["agent_start"] == "solve.sh" and h["waited_agent_s"] is not None
    trial_dirs = [d for d in jobs.glob("*/*") if d.name.lower() == h["trial"]]     # Compose lowercases it
    assert trial_dirs and (trial_dirs[0] / "verifier" / "reward.txt").read_text().strip() == "1"
    assert trial_dirs[0].stat().st_uid == int(os.environ.get("SUDO_UID", os.getuid()))   # Harbor ran as the user
    # The run keeps a copy of the trial: agent logs, verifier output and result.
    assert h["kept"] == "harbor-trial" and h["trial_dir"] == str(trial_dirs[0])
    assert (run / "harbor-trial" / "verifier" / "reward.txt").read_text().strip() == "1"
    assert (run / "harbor-trial" / "agent").is_dir() and (run / "harbor-trial" / "result.json").exists()
    # --tell-agent: Harbor received exactly the text rprof describes, and the run records it.
    told = h["told_agent"]
    assert told.startswith("Resource environment:") and "1.5 GiB of memory" in told
    assert json.loads((run / "harbor-trial" / "config.json").read_text())["extra_instructions"] == [told]

    events = [json.loads(x) for x in (run / "events.jsonl").read_text().splitlines()]
    assert [e["boundary"] for e in events if e["type"] == "segment_applied"] == [0, 1, 3]
    rd = RunData(run)
    peak = max(v for v in rd.mem_series("non_reclaimable")[0] if v is not None)
    assert 240 * MiB <= peak <= 512 * MiB, peak / MiB        # hog-mem's 256 MiB, from the agent's start


def test_profile_limits_replace_harbors_and_harbors_come_back(harbor_task, run_trial, rprof, limits,
                                                              wait_for_event, tmp_path):
    task = harbor_task("rprof-override", "sleep 25", cpus=2, memory_mb=1536)
    trial = run_trial(task)
    cid = trial.container()
    harbor = limits(cid)
    assert harbor == {"memory.max": str(1536 * MiB), "memory.swap.max": str(1536 * MiB),
                      "cpu.max": "200000 100000"}                 # task.toml's 2 CPUs and 1536 MB

    # Defaults at Harbor's budget, and a squeeze from 2 s to 5 s.
    p = tmp_path / "p.yaml"
    p.write_text("version: 1\nname: override\n"
                 "defaults: {cpu: {cores: 2}, mem: {max: 1536Mi}}\n"
                 "segments:\n  - {from: 2, to: 5, cpu: {cores: 0.5}, mem: {max: 512Mi}}\n")
    runs = tmp_path / "runs"
    proc = rprof(cid, runs, "--profile", str(p))
    try:
        applied = lambda b: (lambda e: e["type"] == "segment_applied" and e["boundary"] == b)  # noqa: E731
        wait_for_event(runs, applied(0))
        assert limits(cid) == {"memory.max": str(1536 * MiB), "memory.swap.max": "0", "cpu.max": "200000 100000"}
        wait_for_event(runs, applied(2))
        assert limits(cid) == {"memory.max": str(512 * MiB), "memory.swap.max": "0", "cpu.max": "50000 100000"}
        wait_for_event(runs, applied(5))
        assert limits(cid)["memory.max"] == str(1536 * MiB) and limits(cid)["cpu.max"] == "200000 100000"
    finally:
        proc.send_signal(signal.SIGTERM)                          # stop rprof before the container exits
        _, err = proc.communicate(timeout=60)
    assert proc.returncode in (0, 130, 143), err
    assert limits(cid) == harbor                                   # Harbor's limits, swap included, are back
    assert trial.wait() == 0, trial.log()[-3000:]
    assert trial.reward() == "1"


def test_parallel_runs_each_follow_their_own_trial(harbor_bin, harbor_task, workspace, tmp_path):
    """Two `rprof run --target harbor` at once, each with its own -o: each attaches to its own trial."""
    task = harbor_task("rprof-parallel", "sleep 20", cpus=2, memory_mb=1536)
    procs = {}
    for cond, mem in (("one", "1Gi"), ("two", "1280Mi")):
        jobs = workspace / f"jobs-{cond}"
        jobs.mkdir()
        give_to_user(jobs)
        p = tmp_path / f"{cond}.yaml"
        p.write_text(f"version: 1\nname: {cond}\ndefaults: {{mem: {{max: {mem}}}}}\n")
        procs[cond] = (subprocess.Popen(
            RPROF + ["run", "--target", "harbor", "--profile", str(p), "--runs-dir", str(tmp_path / "runs"),
                     "--view-dir", str(tmp_path / f"view-{cond}"), "--",
                     harbor_bin, "run", "-p", str(task), "-a", "oracle", "-o", str(jobs), "-y"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True), jobs, mem)
        time.sleep(1)
    seen = set()
    for cond, (proc, jobs, mem) in procs.items():
        _, err = proc.communicate(timeout=600)
        assert proc.returncode == 0, err[-3000:]
        run = next((tmp_path / "runs").glob(f"*-{cond}"))
        meta = json.loads((run / "meta.json").read_text())
        own = {d.name.lower() for d in jobs.glob("*/*__*")}
        assert meta["harbor"]["trial"] in own, (cond, meta["harbor"]["trial"], own)
        seen.add(meta["harbor"]["container_id"])
        limits = [e["limits"]["mem"]["max"] for e in map(json.loads, (run / "events.jsonl").read_text().splitlines())
                  if e["type"] == "segment_applied"]
        assert limits == [{"1Gi": 1 << 30, "1280Mi": 1280 << 20}[mem]]
    assert len(seen) == 2
