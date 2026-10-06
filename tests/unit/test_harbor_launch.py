"""--target harbor: reading the agent from Harbor's command line, and the start markers."""

from __future__ import annotations

from rprof.harbor import AGENT_START, PROTECT, agent_of


def test_agent_of_reads_harbor_run_flags():
    assert agent_of(["harbor", "run", "-p", "t", "-a", "terminus-2", "-m", "x"]) == "terminus-2"
    assert agent_of(["harbor", "run", "--agent", "claude-code"]) == "claude-code"
    assert agent_of(["harbor", "run", "--agent=oracle"]) == "oracle"
    assert agent_of(["harbor", "run", "-p", "t"]) is None


def test_known_agents_have_start_markers_and_inside_agents_are_protected():
    assert {"terminus-2", "claude-code", "oracle"} <= set(AGENT_START)
    assert all(PROTECT[a] == AGENT_START[a] for a in PROTECT)    # protect the process that marks the start
    # Harbor checks the install with `claude --version`, which must not count as the agent starting.
    assert AGENT_START["claude-code"] not in "claude --version"


def test_jobs_dir_and_trial_dir(tmp_path):
    from rprof.harbor import find_trial_dir, jobs_dir
    assert jobs_dir(["harbor", "run", "-o", "out"], tmp_path) == (tmp_path / "out").resolve()
    assert jobs_dir(["harbor", "run", "--jobs-dir=/x/y"], tmp_path) == tmp_path.parent.joinpath("/x/y").resolve()
    assert jobs_dir(["harbor", "run"], tmp_path) == (tmp_path / "jobs").resolve()
    trial = tmp_path / "jobs" / "2026-10-05__19-24-29" / "external-sort__FirtDH5"
    trial.mkdir(parents=True)
    assert find_trial_dir(tmp_path / "jobs", "external-sort__firtdh5") == trial     # Compose lowercases it
    assert find_trial_dir(tmp_path / "jobs", "other__abc") is None


def _launch(tmp_path, monkeypatch, containers):
    import rprof.harbor as H
    monkeypatch.setattr(H, "_main_containers", lambda: containers)
    launch = H.Launch(["harbor", "run", "-o", str(tmp_path / "jobs")], say=lambda m: None)
    launch.jobs = tmp_path / "jobs"
    launch.trials_before = H._trial_names(launch.jobs)
    return launch


def test_parallel_runs_each_take_the_container_of_their_own_trial(tmp_path, monkeypatch):
    import pytest
    from rprof.util import RprofError
    (tmp_path / "jobs" / "2026-10-05__21-29-26" / "external-sort__wbJp8v6").mkdir(parents=True)
    # Two new trial containers: the other one belongs to a parallel run with its own -o.
    containers = {"a7dc": ("external-sort__ezt625f__env-main-1", "external-sort__ezt625f__env"),
                  "2b28": ("external-sort__wbjp8v6__env-main-1", "external-sort__wbjp8v6__env")}
    launch = _launch(tmp_path, monkeypatch, containers)
    launch.trials_before = set()
    assert launch._my_container(1.0) == ("2b28", "external-sort__wbjp8v6__env-main-1", "external-sort__wbjp8v6__env")
    # Two trials of our own: refuse to guess.
    (tmp_path / "jobs" / "2026-10-05__21-29-26" / "external-sort__ezt625F").mkdir()
    with pytest.raises(RprofError, match="2 Harbor trials"):
        launch._my_container(1.0)


def test_a_container_before_our_trial_directory_is_not_ours(tmp_path, monkeypatch):
    (tmp_path / "jobs").mkdir()
    launch = _launch(tmp_path, monkeypatch, {"a7dc": ("other__x1__env-main-1", "other__x1__env")})
    assert launch._my_container(60.0) is None
