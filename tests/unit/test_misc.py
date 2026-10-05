import pytest

from rprof.agentview import AgentView
from rprof.live import parse_assignments
from rprof.profile import profile_from_dict
from rprof.runner import new_run_id
from rprof.util import RprofError


def test_apply_assignments():
    v = parse_assignments(["cpu.cores=0.5", "pids.max=16", "net.allow=10.0.0.0/8,172.18.0.0/16", "cpu.cpus=0",
                           "io.wbps=20Mi", "mem.swap_max=0", "net.loss=30%", "net.rate=10mbit",
                           "unified.cpu.weight=50", "mem.max=max"])
    assert v == {"cpu.cores": 0.5, "pids.max": 16, "net.allow": ["10.0.0.0/8", "172.18.0.0/16"], "cpu.cpus": "0",
                 "io.wbps": 20 * 2**20, "mem.swap_max": 0, "net.loss": 30.0, "net.rate": 10_000_000,
                 "unified.cpu.weight": "50", "mem.max": None}


@pytest.mark.parametrize("bad", ["harness.deadline=3s", "foo.bar=1", "mem.max", "pids.max=abc", "cpu.cores=-1",
                                 "mem.max=1Gx"])
def test_apply_assignment_errors(bad):
    with pytest.raises(RprofError) as ei:
        parse_assignments([bad])
    assert ei.value.exit_code == 2


def test_run_ids_unique(tmp_path):
    a = new_run_id(tmp_path, "task12")
    (tmp_path / a).mkdir()
    b = new_run_id(tmp_path, "task12")
    assert a.endswith("-task12") and b == a + "-2"


def test_intervals_clipped():
    p = profile_from_dict({"version": 1, "name": "x", "segments": [{"from": 5, "to": 10, "pids": {"max": 3}}]})
    assert p.intervals(7) == [(0.0, 5.0), (5.0, 7)]
    assert p.intervals(20) == [(0.0, 5.0), (5.0, 10.0), (10.0, 20)]


def test_harness_only_segment_not_called_defaults():
    p = profile_from_dict({"version": 1, "name": "x", "visibility": "full",
                           "segments": [{"from": 5, "to": 10, "harness": {"deadline": "3s"}}]})
    nxt = AgentView(p).text(1).splitlines()[2]
    assert not nxt.startswith("next: at 5 s → back to defaults")


def test_swap_unlimited_is_not_a_limit():
    p = profile_from_dict({"version": 1, "name": "x", "visibility": "current",
                           "defaults": {"mem": {"swap_max": "max"}}})
    assert AgentView(p).text(1).splitlines()[1] == "now:  no limits"
