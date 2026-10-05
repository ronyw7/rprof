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
