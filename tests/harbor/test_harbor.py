"""rprof attached to real Harbor trials, as in the experiment runbook.

1. rprof records a Harbor trial from outside and ends cleanly when Harbor's container exits.
2. A profile's limits replace the ones Harbor sets from task.toml while it runs, and Harbor's
   come back if rprof stops before the container does.
"""

from __future__ import annotations

import json
import signal

import pytest

from harbor_trial import (harbor_bin, harbor_task, limits, rprof, run_trial,  # noqa: F401 (fixtures)
                          wait_for_event, workspace)
from rprof.report.data import RunData

pytestmark = [pytest.mark.harbor, pytest.mark.slow]
MiB = 1 << 20


def test_rprof_records_a_harbor_trial_until_its_container_exits(harbor_task, run_trial, rprof, tmp_path):
    task = harbor_task("rprof-record", "hog-mem 256M 4\nsleep 4")
    trial = run_trial(task)
    cid = trial.container()
    runs = tmp_path / "runs"
    proc = rprof(cid, runs, "--mode", "measure")
    assert trial.wait() == 0, trial.log()[-3000:]
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert trial.reward() == "1"

    run = next(runs.iterdir())
    meta = json.loads((run / "meta.json").read_text())
    assert meta["end_reason"] == "target_exit"
    rd = RunData(run)
    peak = max(v for v in rd.mem_series("non_reclaimable")[0] if v is not None)
    assert 240 * MiB <= peak <= 512 * MiB, peak / MiB        # hog-mem's 256 MiB, seen from outside
    assert (run / "report.json").exists()


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
