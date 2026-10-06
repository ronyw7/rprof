import json

from rprof.agentview import AgentView
from rprof.profile import profile_from_dict


def test_design_now_txt(example):
    txt = AgentView(example).text(84)
    lines = txt.splitlines()
    assert lines[0] == "t = 84 s · segment 1 of 4"
    assert "memory 1 GiB hard (800 MiB soft)" in lines[1]
    assert "max 16 processes" in lines[1] and "cpu 4 cores" in lines[1]
    assert lines[1].startswith("now:")
    assert lines[2].startswith("next: at 120 s → network loss 30% · at 150 s → network blocked (reject)")


def test_full_data(example):
    d = AgentView(example).data(84)
    assert d["visibility"] == "full" and d["segment"] == 1
    assert d["current"]["t0"] == 60 and d["current"]["t1"] == 120
    assert d["current"]["limits"]["mem"]["max"] == 2**30
    assert d["past"] == [{"t0": 0.0, "t1": 60, "limits": d["past"][0]["limits"]}]
    assert d["upcoming"][0] == {"t0": 120, "t1": 150, "changes": {"net.loss": "30%"}}
    assert d["upcoming"][1] == {"t0": 150, "t1": 180, "changes": {"net.partition": "reject"}}
    assert d["upcoming"][-1]["t1"] is None and d["upcoming"][-1]["changes"] == {}


def test_back_to_defaults(example):
    assert "at 270 s → back to defaults" in AgentView(example).text(200)
    assert AgentView(example).text(300).splitlines()[2] == "next: no further changes"


def test_current_visibility(example):
    example.visibility = "current"
    v = AgentView(example)
    d = v.data(84)
    assert "past" not in d and "upcoming" not in d
    txt = v.text(84)
    assert txt.splitlines()[0] == "t = 84 s"
    assert "next:" not in txt and "memory 1 GiB hard" in txt


def test_none_visibility(tmp_path):
    p = profile_from_dict({"version": 1, "name": "x", "segments": [{"from": 0, "to": 5, "pids": {"max": 3}}]})
    v = AgentView(p, tmp_path / "view", tmp_path / "copy")
    assert v.data(1) is None and v.text(1) is None
    v.write(1)
    assert not (tmp_path / "view").exists() or not any((tmp_path / "view").iterdir())


def test_write_atomic_files(example, tmp_path):
    v = AgentView(example, tmp_path / "view", tmp_path / "run" / "agentview")
    v.write(84)
    for d in (tmp_path / "view", tmp_path / "run" / "agentview"):
        assert json.loads((d / "state.json").read_text())["segment"] == 1
        assert (d / "now.txt").read_text().startswith("t = 84 s")
        assert not [p for p in d.iterdir() if p.name.startswith(".")]   # no temp files left
    v.clear()
    assert not (tmp_path / "view" / "now.txt").exists()


def test_no_limits_text():
    p = profile_from_dict({"version": 1, "name": "x", "visibility": "current"})
    assert AgentView(p).text(3).splitlines()[1] == "now:  no limits"


def test_cpu_sets_are_counted_and_unthrottled_disks_said():
    p = profile_from_dict({"version": 1, "name": "x", "visibility": "full",
                           "defaults": {"cpu": {"cpus": "0,2,4,6,56,58,60,62"}, "mem": {"max": "2Gi"}},
                           "segments": [{"from": 10, "to": 20, "io": {"wbps": "10Mi"}}]})
    lines = AgentView(p).text(0).splitlines()
    assert lines[1] == "now:  memory 2 GiB hard · 8 CPUs · disk not throttled"
    lines = AgentView(p).text(12).splitlines()
    assert lines[1] == "now:  memory 2 GiB hard · 8 CPUs · disk write 10 MiB/s"
    # Lifting a disk limit is announced as such, not dropped.
    q = profile_from_dict({"version": 1, "name": "y", "visibility": "full", "defaults": {"io": {"wbps": "10Mi"}},
                           "segments": [{"from": 10, "to": 20, "io": {"wbps": "max"}, "mem": {"max": "4Gi"}}]})
    assert "at 10 s → memory 4 GiB hard · disk not throttled" in AgentView(q).text(0)
