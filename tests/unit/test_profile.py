import pytest
import yaml

from rprof.profile import ProfileError, load_profile, profile_from_dict
from rprof.runner import managed_knobs, used_knobs

from unit_helpers import EXAMPLE


def _base(**kw):
    d = {"version": 1, "name": "t"}
    d.update(kw)
    return d


def _problems(d):
    with pytest.raises(ProfileError) as ei:
        profile_from_dict(d)
    return [f"{p}: {m}" for p, m in ei.value.problems]


def test_design_example_loads(example):
    p = example
    assert p.name == "mem-squeeze-mid" and p.visibility == "full" and len(p.segments) == 4
    assert p.boundaries() == [60, 120, 150, 180, 270]
    assert p.end() == 270


def test_error_messages():
    d = yaml.safe_load(EXAMPLE.read_text())
    d["segments"][0]["mem"]["max"] = "1Gx"
    assert "segments[0].mem.max: 1Gx is not a byte size" in _problems(d)
    d = yaml.safe_load(EXAMPLE.read_text())
    d["defaults"]["cpu"]["perod"] = "100ms"
    assert "defaults.cpu.perod: unknown key" in _problems(d)
    assert "colour: unknown key" in _problems(_base(colour="red"))
    assert any(x.startswith("version:") for x in _problems({"version": 2, "name": "t"}))
    assert any(x.startswith("name:") for x in _problems(_base(name="Bad Name")))
    assert "segments[0].net.loss: 30 is not a percentage (write e.g. 30%)" in _problems(
        _base(segments=[{"from": 0, "to": 1, "net": {"loss": 30}}]))


def test_to_must_exceed_from():
    probs = _problems(_base(segments=[{"from": 10, "to": 5, "pids": {"max": 3}}]))
    assert probs and probs[0].startswith("segments[0].to:")


def test_overlap_rules():
    probs = _problems(_base(segments=[{"from": 0, "to": 10, "mem": {"max": "1Gi"}},
                                      {"from": 5, "to": 15, "mem": {"max": "2Gi"}}]))
    assert len(probs) == 1 and probs[0].startswith("segments[1]:") and "mem.max" in probs[0]
    p = profile_from_dict(_base(segments=[{"from": 0, "to": 10, "mem": {"max": "1Gi"}},
                                          {"from": 5, "to": 15, "pids": {"max": 8}}]))
    assert p.segment_at(7) == (1, [1, 2])
    lim = p.limits_at(7)
    assert lim["mem.max"] == 2**30 and lim["pids.max"] == 8
    # Touching (half-open) segments do not overlap.
    profile_from_dict(_base(segments=[{"from": 0, "to": 10, "mem": {"max": "1Gi"}},
                                      {"from": 10, "to": 15, "mem": {"max": "2Gi"}}]))


def test_lookup(example):
    p = example
    assert p.segment_at(0) == (0, [])
    assert p.segment_at(60) == (1, [1])
    assert p.segment_at(119.999) == (1, [1])
    assert p.segment_at(120) == (2, [2])
    assert p.segment_at(270) == (0, [])
    assert p.limits_at(84)["mem.max"] == 2**30
    assert p.limits_at(84)["cpu.cores"] == 4.0
    assert p.limits_at(200)["cpu.cores"] == 0.5
    assert p.limits_at(200)["harness.deadline"] == 30.0
    assert p.limits_at(10)["harness.deadline"] == 300.0
    assert p.next_boundary(84) == 120
    assert p.next_boundary(60) == 120
    assert p.next_boundary(300) is None
    assert p.changes_from_defaults(130) == {"net.loss": "30%"}
    assert p.changes_from_defaults(200) == {"cpu.cores": 0.5, "harness.deadline": "30s"}
    assert p.changes_from_defaults(10) == {}
    assert p.intervals()[0] == (0.0, 60)
    assert p.intervals()[-1] == (270, None)


def test_resolved_dump_roundtrips(example):
    again = profile_from_dict(yaml.safe_load(example.dump_yaml()))
    assert again.boundaries() == example.boundaries()
    assert again.limits_at(84) == example.limits_at(84)


def test_managed_and_used_knobs():
    p = profile_from_dict(_base(segments=[{"from": 0, "to": 5, "mem": {"max": "1Gi"}}]))
    assert p.managed_knobs() == {"mem.max"}
    # Mentioning a group manages all of its enforced knobs; other groups are untouched.
    assert managed_knobs(p) == {"mem.high", "mem.max", "mem.swap_max"}
    assert used_knobs(p) == {"mem.max"}
    p = profile_from_dict(_base(defaults={"cpu": {"cores": "max"}, "harness": {"deadline": "10s"}}))
    assert managed_knobs(p) == {"cpu.cores", "cpu.cpus", "cpu.period"}
    assert used_knobs(p) == set()          # max everywhere: nothing limiting
    p = profile_from_dict(_base(segments=[{"from": 0, "to": 5, "unified": {"cpu.weight": 50}}]))
    assert "unified.cpu.weight" in managed_knobs(p) and "unified.cpu.weight" in used_knobs(p)
    assert p.segments[0].values["unified.cpu.weight"] == "50"


def test_load_profile_missing_file(tmp_path):
    with pytest.raises(ProfileError):
        load_profile(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: [1\n")
    with pytest.raises(ProfileError):
        load_profile(bad)


def test_cpu_quota_minimum():
    probs = _problems(_base(defaults={"cpu": {"cores": 0.001, "period": "100ms"}}))
    assert any("1 ms" in x for x in probs)
