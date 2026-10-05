import json

import pytest
import yaml

from rprof.report.binding import Thresholds
from rprof.report.data import RunData
from rprof.report.report import build_report, render_md, write_reports
from rprof.report.timeline import build_timeline, render_timeline

MiB = 2**20
PROFILE = {"version": 1, "name": "synthetic", "visibility": "full",
           "segments": [{"from": 10, "to": 20, "cpu": {"cores": 0.5}, "mem": {"max": "256Mi"}, "pids": {"max": 20}},
                        {"from": 20, "to": 30, "mem": {"high": "512Mi"}}]}


def cores_at(t):
    if 10 <= t < 15:
        return 1.0
    if 15 <= t < 20:
        return 0.2
    return 0.1


def make_run(tmp_path, mode="enforce", calls=True):
    d = tmp_path / "run"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"run_id": "r1", "mode": mode, "target": {"io_device": None}}))
    (d / "profile.yaml").write_text(yaml.safe_dump(PROFILE))
    usage = thr = per = nthr = 0.0
    oom = mmax = 0
    rows = []
    for i in range(301):
        t = round(i * 0.1, 1)
        if i:
            mid = t - 0.05
            usage += cores_at(mid) * 1e5
            if 10 <= mid < 20:
                thr += 0.4e5
                per += 1
                nthr += 0.8
        if t == 12.5:
            oom += 1
            mmax += 5
        cur = 300 * MiB if 12 <= t < 13 else 100 * MiB
        rows.append({"t": t, "t_wall": "x", "segment": 0, "running_calls": [],
                     "cpu": {"usage_usec": int(usage), "throttled_usec": int(thr), "nr_periods": int(per),
                             "nr_throttled": int(nthr)},
                     "mem": {"current": cur, "events": {"high": 0, "max": mmax, "oom": oom, "oom_kill": oom}},
                     "pids": {"current": 5, "events_max": 0},
                     "psi": {"cpu": {"some_us": 0}, "memory": {"some_us": 0, "full_us": 0},
                             "io": {"some_us": 0, "full_us": 0}}})
    (d / "samples.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    ev = [{"t": 0.0, "type": "run_start"}]
    if calls:
        ev += [{"t": 0.0, "type": "tool_start", "call_id": "A", "cmd": "hog-mem 512M 6"},
               {"t": 2.0, "type": "tool_start", "call_id": "B", "cmd": "stress-ng --cpu 1"},
               {"t": 6.0, "type": "tool_end", "call_id": "A", "exit_code": 0, "timed_out": False, "cause": None},
               {"t": 8.0, "type": "tool_end", "call_id": "B", "exit_code": 0, "timed_out": False, "cause": None},
               {"t": 12.0, "type": "tool_start", "call_id": "C", "cmd": "psql -f load.sql"},
               {"t": 25.0, "type": "tool_end", "call_id": "C", "exit_code": 137, "timed_out": False,
                "cause": "memory"}]
    ev += [{"t": 30.0, "type": "run_end", "reason": "profile_end"}]
    ev.sort(key=lambda e: e["t"])
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ev))
    return d


def _seg(rep, n):
    return next(s for s in rep["segments"] if s["segment"] == n)


def _knob(seg, k):
    return next(r for r in seg["knobs"] if r["knob"] == k)


def test_enforce_binding(tmp_path):
    rep = build_report(RunData(make_run(tmp_path)))
    s1 = _seg(rep, 1)
    assert s1["no_effect"] is False and s1["windows"] == [[10.0, 20.0]]
    cpu = _knob(s1, "cpu.cores")
    assert cpu["bound"] is True and cpu["evidence"]["throttled_frac"] == pytest.approx(0.4, abs=0.01)
    assert cpu["evidence"]["throttled_periods_frac"] == pytest.approx(0.8, abs=0.02)
    assert cpu["usage"]["max"] == pytest.approx(1.0, abs=0.01)
    mm = _knob(s1, "mem.max")
    assert mm["bound"] is True and mm["evidence"] == {"mem_max_events": 5, "oom_kill": 1}
    assert mm["limit"] == 256 * MiB and mm["limit_raw"] == "256Mi"
    assert _knob(s1, "pids.max")["bound"] is False
    s2 = _seg(rep, 2)
    assert s2["no_effect"] is True and _knob(s2, "mem.high")["bound"] is False
    assert rep["summary"]["no_effect_segments"] == [2]
    assert s1["calls"]["started"] == 1 and s2["calls"]["failed"] == 1 and s2["calls"]["failed_ids"] == ["C"]
    assert all(r["violation"] is None for r in s1["knobs"])
    s0 = _seg(rep, 0)
    assert s0["windows"] == [[0.0, 10.0]] and s0["knobs"] == [] and s0["no_effect"] is None


def test_call_spanning_boundary_lists_both_segments(tmp_path):
    rep = build_report(RunData(make_run(tmp_path)))
    calls = {c["call_id"]: c for c in rep["calls"]}
    assert calls["C"]["segments"] == [1, 2] and calls["C"]["limit_changes_during_call"] == [20.0]
    assert calls["C"]["failed"] and calls["C"]["cause"] == "memory"
    assert calls["A"]["segments"] == [0] and not calls["A"]["failed"]
    assert calls["C"]["limits_at_start"]["mem"]["max"] == 256 * MiB


def test_measure_violations(tmp_path):
    rep = build_report(RunData(make_run(tmp_path, mode="measure")))
    s1 = _seg(rep, 1)
    assert s1["no_effect"] is None
    cpu = _knob(s1, "cpu.cores")
    assert cpu["bound"] is None
    assert cpu["violation"]["time_frac"] == pytest.approx(0.5, abs=0.02)
    assert cpu["violation"]["peak_over"] == pytest.approx(0.5, abs=0.01)
    mm = _knob(s1, "mem.max")
    assert mm["violation"]["time_frac"] == pytest.approx(0.1, abs=0.02)
    assert mm["violation"]["peak_over"] == 44 * MiB
    assert _knob(s1, "pids.max")["violation"] == {"time_frac": 0.0, "peak_over": 0}
    md = render_md(rep)
    assert "over limit (time)" in md


def test_thresholds(tmp_path):
    rep = build_report(RunData(make_run(tmp_path)), Thresholds(throttle_frac=0.9))
    assert _knob(_seg(rep, 1), "cpu.cores")["bound"] is False
    with pytest.raises(ValueError):
        Thresholds.from_dict({"bogus": 1})


def test_parallel_timeline(tmp_path):
    rd = RunData(make_run(tmp_path))
    tl = build_timeline(rd)
    head = [(iv["t0"], iv["t1"], [c["call_id"] for c in iv["running_calls"]]) for iv in tl[:3]]
    assert head == [(0.0, 2.0, ["A"]), (2.0, 6.0, ["A", "B"]), (6.0, 8.0, ["B"])]
    # Boundaries split intervals: 10 (segment 1) and 20 (segment 2) both appear.
    starts = [iv["t0"] for iv in tl]
    assert 10.0 in starts and 20.0 in starts and 12.0 in starts and 25.0 in starts
    iv = next(iv for iv in tl if iv["t0"] == 12.0)
    assert iv["segment"] == 1 and iv["events"]["oom_kill"] == 1 and iv["limits"]["cpu"]["cores"] == 0.5
    assert iv["usage"]["mem_peak"] == 300 * MiB
    end_c = next(iv for iv in tl if iv["t1"] == 25.0)
    assert end_c["failed_calls"] == ["C"]
    txt = render_timeline(rd, tl)
    assert txt.splitlines()[0].startswith("t (s)") and "oom_kill: 1" in txt and "failed: C" in txt


def test_timeline_usage_rates(tmp_path):
    tl = build_timeline(RunData(make_run(tmp_path)))
    iv = next(iv for iv in tl if iv["t0"] == 10.0)        # 10–12: 1.0 core
    assert iv["usage"]["cpu_cores_mean"] == pytest.approx(1.0, abs=0.01)
    assert iv["pressure"]["cpu_some"] == 0.0


def test_write_reports(tmp_path):
    d = make_run(tmp_path)
    rep = write_reports(d)
    assert json.loads((d / "report.json").read_text())["run_id"] == "r1"
    assert (d / "report.md").read_text().startswith("# rprof report: r1")
    assert len((d / "timeline.jsonl").read_text().splitlines()) == len(build_timeline(RunData(d)))
    assert rep["summary"]["calls"] == 3 and rep["summary"]["failed"] == 1


def test_not_a_run_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        RunData(tmp_path)


def test_summary_has_whole_run_peak_memory_and_oom_kills(tmp_path):
    for mode in ("enforce", "measure"):
        (tmp_path / mode).mkdir()
        s = build_report(RunData(make_run(tmp_path / mode, mode=mode)))["summary"]
        assert s["peak_memory_bytes"] == {"non_reclaimable": 300 * MiB, "total": 300 * MiB}
        assert s["oom_kills"] == 1
