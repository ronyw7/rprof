"""Hot-path parsers and kernel-quirk handling added while testing on a real host."""

from __future__ import annotations

import errno

import pytest

from rprof.clock import SimClock
from rprof.controllers import FileCache, build
from rprof.controllers.cpu import write_cpuset_all
from rprof.controllers.pids import PidsController
from rprof.sampler import Sampler
from rprof.target import Target
from rprof.target.cgroup import Cgroup, parse_flat, parse_psi, pick_flat
from unit_helpers import FAKE_FILES


def test_pick_flat_matches_parse_flat():
    for name in ("cpu.stat", "memory.stat", "memory.events", "pids.events"):
        text = FAKE_FILES[name]
        full = parse_flat(text)
        keys = tuple(full)
        assert pick_flat(text, keys) == full
    assert pick_flat("anon 1\nfile 2\n", ("file", "missing")) == {"file": 2}
    # A key that is a prefix of another must not match it.
    assert pick_flat("file_mapped 9\nfile 2\n", ("file",)) == {"file": 2}


def test_parse_psi_some_and_full():
    assert parse_psi(FAKE_FILES["cpu.pressure"]) == {"some_us": 1500, "full_us": 700}
    assert parse_psi("some avg10=0.00 avg60=0.00 avg300=0.00 total=42\n") == {"some_us": 42}
    assert parse_psi("") == {}


def test_pids_events_summed_over_subtree_without_local_file(fake_cg):
    child = fake_cg / "child"
    child.mkdir()
    (child / "pids.events").write_text("max 3\n")
    (child / "cgroup.procs").write_text("")
    t = Target(spec="cgroup:x", kind="cgroup", cgroup=Cgroup(fake_cg))
    c = PidsController(t)
    assert not c.hierarchical_events
    out: dict = {}
    c.sample(out, FileCache())
    assert out["pids"]["events_max"] == 3      # the refused fork was counted in the child


def test_pids_events_hierarchical_when_local_file_exists(fake_cg):
    (fake_cg / "pids.events.local").write_text("max 0\n")
    (fake_cg / "pids.events").write_text("max 5\n")
    child = fake_cg / "child"
    child.mkdir()
    (child / "pids.events").write_text("max 5\n")
    t = Target(spec="cgroup:x", kind="cgroup", cgroup=Cgroup(fake_cg))
    out: dict = {}
    PidsController(t).sample(out, FileCache())
    assert out["pids"]["events_max"] == 5      # not double counted


def test_cpuset_all_falls_back_to_parent_on_enospc(fake_cg, monkeypatch):
    import rprof.controllers.cpu as cpu
    (fake_cg.parent / "cpuset.cpus.effective").write_text("0-7\n")
    (fake_cg / "cpuset.cpus").write_text("0-1\n")
    writes = []

    def fake_write(path, value):
        writes.append(value)
        if value == "":
            raise OSError(errno.ENOSPC, "No space left on device")
        path.write_text(value)
    monkeypatch.setattr(cpu, "write_text", fake_write)
    errs: list[str] = []
    assert write_cpuset_all(fake_cg, errs) == "0-7"
    assert writes == ["", "0-7"] and not errs
    assert (fake_cg / "cpuset.cpus").read_text() == "0-7"


def test_secondary_counters_read_at_about_20hz(fake_cg):
    t = Target(spec="cgroup:x", kind="cgroup", cgroup=Cgroup(fake_cg))
    s = Sampler(t, build(t), SimClock(), hz=100)
    assert s.secondary_every == 5
    ticks = [s.tick() for _ in range(10)]
    with_psi = [i for i, x in enumerate(ticks) if "psi" in x]
    assert with_psi == [0, 5]
    assert all("cpu" in x and "mem" in x for x in ticks)   # primary counters every tick
    s10 = Sampler(t, build(t), SimClock(), hz=10)
    assert all("psi" in s10.tick() for _ in range(3))


def test_sampler_rejects_bad_rate(fake_cg):
    t = Target(spec="cgroup:x", kind="cgroup", cgroup=Cgroup(fake_cg))
    with pytest.raises(ValueError):
        Sampler(t, [], SimClock(), hz=200)
