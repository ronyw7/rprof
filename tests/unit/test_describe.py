"""`rprof describe` and --tell-agent: what an agent is told about its resources."""

from __future__ import annotations

from typer.testing import CliRunner

from rprof.cli import app
from rprof.describe import describe
from rprof.profile import profile_from_dict

CAPS = {"features": {"disk_bandwidth": {"write_bps": 3.9e9, "read_bps": 4.4e9, "bytes": 1 << 32},
                     "swap_bytes": 2 << 30}}


def _p(d):
    return profile_from_dict({"version": 1, "name": "x", **d})


def test_a_fixed_budget_is_one_sentence():
    p = _p({"visibility": "full", "defaults": {"cpu": {"cpus": "0,2,4,6,56,58,60,62"}, "mem": {"max": "2Gi"}}})
    assert describe(p, CAPS) == (
        "Resource environment: your container has 8 CPUs, 2 GiB of memory (processes that go above it are "
        "killed) and disk bandwidth that is not throttled (about 3.9 GB/s write, 4.4 GB/s read). "
        "Be aware of these resources and plan your work around them.")


def test_without_a_measurement_the_disk_is_just_not_throttled():
    p = _p({"visibility": "full", "defaults": {"mem": {"high": "900Mi", "max": "1Gi"}}})
    assert describe(p) == (
        "Resource environment: your container has 1 GiB of memory (processes that go above it are killed; "
        "above 900 MiB they are slowed down) and disk bandwidth that is not throttled. "
        "Be aware of these resources and plan your work around them.")


def test_a_schedule_lists_each_interval_and_time_zero():
    p = _p({"visibility": "full", "defaults": {"mem": {"max": "1Gi"}, "io": {"wbps": "10Mi"}},
            "segments": [{"from": 400, "to": 1200, "mem": {"max": "48Gi"}, "io": {"wbps": "max"}}]})
    lines = describe(p, CAPS).splitlines()
    assert "Time 0 is when you receive this task" in lines[0]
    assert lines[1] == "- 0–400 s: 1 GiB of memory (processes that go above it are killed) and disk writes limited to 10 MiB/s."
    assert lines[2].startswith("- 400–1200 s: 48 GiB of memory") and "not throttled (about 3.9 GB/s write" in lines[2]
    assert lines[3].startswith("- after 1200 s: 1 GiB of memory")


def test_visibility_decides_how_much_is_said():
    d = {"defaults": {"mem": {"max": "1Gi"}}, "segments": [{"from": 60, "to": 120, "mem": {"max": "2Gi"}}]}
    assert describe(_p({**d, "visibility": "none"})) is None
    cur = describe(_p({**d, "visibility": "current"}))
    assert "1 GiB of memory" in cur and "may change" in cur and "2 GiB" not in cur


def test_cli_describe(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text("version: 1\nname: x\nvisibility: full\ndefaults: {mem: {max: 1Gi}}\n")
    r = CliRunner().invoke(app, ["describe", str(p), "--capabilities", str(tmp_path / "missing.json")])
    assert r.exit_code == 0 and r.output.startswith("Resource environment: your container has 1 GiB of memory")
    p.write_text("version: 1\nname: x\nvisibility: none\n")
    assert CliRunner().invoke(app, ["describe", str(p)]).exit_code == 1


def test_tell_agent_appends_the_description_to_harbors_command(monkeypatch, tmp_path):
    import rprof.harbor as H
    from rprof import runner
    seen = {}

    class FakeLaunch:
        def __init__(self, command, **kw):
            seen["command"] = command

        def start(self):
            pass

        def find_trial(self):
            return None                     # Harbor "exits" before the trial starts: nothing to record

        def wait(self):
            return 0

        def stop(self):
            pass

    monkeypatch.setattr(H, "Launch", FakeLaunch)
    p = tmp_path / "p.yaml"
    p.write_text("version: 1\nname: x\nvisibility: full\ndefaults: {mem: {max: 1Gi}}\n")
    opts = runner.RunOptions(target="harbor", profile=str(p), command=["harbor", "run", "-p", "t"], tell_agent=True,
                             capabilities=str(tmp_path / "none.json"), quiet=True)
    code, sess = runner.run(opts)
    assert sess is None and seen["command"][:4] == ["harbor", "run", "-p", "t"]
    assert seen["command"][4] == "--extra-instruction"
    assert seen["command"][5].startswith("Resource environment: your container has 1 GiB of memory")


def test_swap_is_what_the_host_can_actually_give():
    p = _p({"visibility": "full", "defaults": {"mem": {"max": "48Gi", "swap_max": "48Gi"}}})
    assert "2 GiB of swap" in describe(p, CAPS)          # the host has 2 GiB, not the allowance's 48
    assert "swap" not in describe(p)                     # host swap unknown: left out
