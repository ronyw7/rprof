"""The fidelity analysis, on a synthetic run with known workloads."""

from __future__ import annotations

import json

import yaml

from rprof.fidelity import _bracket_delta, analyse
from rprof.report.data import RunData

MiB = 1 << 20
CALLS = {"cpu": (1.0, 6.0), "memory": (6.5, 9.5), "io": (12.01, 12.09), "page_cache": (13.0, 14.0),
         "network": (15.01, 15.09), "pids": (16.0, 17.0)}


def make_run(tmp_path, io_bytes=512 * MiB, mem_rise=1024 * MiB, release=True, leftover=False):
    d = tmp_path / "run"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"run_id": "f", "mode": "measure", "target": {"io_device": "8:0"}}))
    (d / "profile.yaml").write_text(yaml.safe_dump({"version": 1, "name": "selftest"}))
    base, file_base = 100 * MiB, 50 * MiB
    rows, usage = [], 0.0
    for i in range(401):                               # 20 s at 20 Hz
        t = round(i * 0.05, 2)
        if i and 1.0 < t <= 6.0:
            usage += 2e6 * 0.05                        # 2 busy cores
        cur, file = base, file_base
        if 6.7 <= t <= 9.5 or (not release and t > 9.5):
            cur += mem_rise                            # hog-mem holds its memory
        if 13.1 <= t <= 14.0:
            cur, file = cur + 528 * MiB, file + 512 * MiB   # page cache: reclaimable
        wbytes = io_bytes if t >= 12.05 else 0          # a burst between two samples
        tx = 10 * MiB if t >= 15.05 else 0
        pids = 51 if 16.1 <= t <= 17.0 else 1
        if leftover and 15.9 <= t <= 16.0:
            pids = 6                                    # the previous call's processes, exiting
        rows.append({"t": t, "t_wall": "x", "segment": 0, "running_calls": [],
                     "cpu": {"usage_usec": int(usage)},
                     "mem": {"current": cur, "file": file, "shmem": 0, "events": {"oom_kill": 0}},
                     "io": {"8:0": {"rbytes": 0, "wbytes": wbytes, "rios": 0, "wios": 0}},
                     "net": {"eth0": {"rx_bytes": 0, "tx_bytes": tx}}, "pids": {"current": pids}})
    (d / "samples.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    ev = [{"t": 0.0, "type": "run_start"}]
    for cid, (a, b) in CALLS.items():
        ev.append({"t": a, "type": "tool_start", "call_id": cid, "cmd": cid})
        peak = base + mem_rise + file_base if cid == "memory" else None
        ev.append({"t": b, "type": "tool_end", "call_id": cid, "exit_code": 0, "timed_out": False, "cause": None,
                   "mem_peak_bytes": peak, "mem_peak_nonreclaimable_bytes": None if peak is None else base + mem_rise})
    ev.append({"t": 20.0, "type": "run_end"})
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in sorted(ev, key=lambda e: e["t"])))
    return RunData(d)


def test_all_checks_pass_on_a_faithful_recording(tmp_path):
    checks = {c.name: c for c in analyse(make_run(tmp_path), {"cpu": True, "network": 10 * MiB})}
    assert {n: c.ok for n, c in checks.items()} == {
        "cpu": True, "memory_peak": True, "memory_after_exit": True, "call_timing": True, "disk_writes": True,
        "page_cache": True, "network_sent": True, "processes": True}
    assert checks["disk_writes"].measured == 512 * MiB


def test_wrong_recordings_fail(tmp_path):
    checks = {c.name: c for c in analyse(make_run(tmp_path, io_bytes=400 * MiB, mem_rise=700 * MiB, release=False),
                                         {"network": 10 * MiB})}
    assert checks["disk_writes"].ok is False
    assert checks["memory_peak"].ok is False
    assert checks["memory_after_exit"].ok is False
    assert checks["network_sent"].ok is True
    assert "cpu" not in checks            # not run, and not reported as skipped either


def test_bracket_delta_counts_a_burst_shorter_than_a_sample(tmp_path):
    rd = make_run(tmp_path)
    ys = rd.io_series("wbytes")
    # The burst lands between the samples at 12.00 and 12.05, inside a call from 12.01 to 12.09.
    assert _bracket_delta(rd, ys, 12.01, 12.09) == 512 * MiB
    a, b = rd._interp(ys, 12.01), rd._interp(ys, 12.09)
    assert b - a < 512 * MiB                # interpolating at the call's edges would miss part of it


def test_results_carry_expected_observed_and_workload(tmp_path):
    checks = {c.name: c for c in analyse(make_run(tmp_path), {"cpu": True, "network": 10 * MiB})}
    c = checks["disk_writes"]
    assert (c.status, c.label, c.expected, c.observed) == ("PASS", "disk writes", "502–522 MiB", "512 MiB")
    d = checks["memory_peak"].as_dict()
    assert set(d) >= {"ok", "label", "expected", "observed", "workload", "detail", "measured", "unit"}
    assert isinstance(d["measured"], int)                          # bytes, rounded


def test_processes_baseline_ignores_the_previous_calls_exit(tmp_path):
    checks = {c.name: c for c in analyse(make_run(tmp_path, leftover=True), {"network": 10 * MiB})}
    assert checks["processes"].ok is True and checks["processes"].measured == 50
