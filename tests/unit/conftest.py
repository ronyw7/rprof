"""Fixtures for the unit tier: a fake cgroupfs and the design's example profile."""

from __future__ import annotations

import pytest

from rprof.profile import load_profile
from rprof.target import Cgroup, Target
from unit_helpers import EXAMPLE, make_cgroup


def _truncating_write(path, value):
    """Real cgroupfs files replace their value on write(2); a plain file needs O_TRUNC."""
    with open(path, "w") as f:
        f.write(value if value else "\n")


@pytest.fixture
def fake_cg(tmp_path, monkeypatch):
    import rprof.controllers.base
    import rprof.controllers.cpu
    import rprof.protect
    import rprof.snapshot
    import rprof.target.cgroup
    for mod in (rprof.target.cgroup, rprof.controllers.base, rprof.controllers.cpu, rprof.snapshot, rprof.protect):
        monkeypatch.setattr(mod, "write_text", _truncating_write)
    root = tmp_path / "cgroup"
    monkeypatch.setenv("RPROF_CGROUP_ROOT", str(root))
    return make_cgroup(root)


@pytest.fixture
def target(fake_cg):
    return Target(spec="cgroup:rprof.slice/sbx", kind="cgroup", cgroup=Cgroup(fake_cg), io_device="259:0")


@pytest.fixture
def example():
    return load_profile(EXAMPLE)
