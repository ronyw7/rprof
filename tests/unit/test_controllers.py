import json

import pytest

from rprof import controllers as C
from rprof import knobs as K
from rprof.controllers.cpu import CpuController, cpu_max_value
from rprof.controllers.io import IoController, io_max_line
from rprof.controllers.memory import MemoryController
from rprof.controllers.pids import PidsController
from rprof.controllers.unified import UnifiedController
from rprof.snapshot import Snapshot


def _lim(**kw):
    d = K.defaults()
    for k, v in kw.items():
        d[k.replace("__", ".")] = v
    return d


def test_cpu_max_values():
    assert cpu_max_value(0.5, 100) == "50000 100000"
    assert cpu_max_value(None, 100) == "max 100000"
    assert cpu_max_value(2, 50) == "100000 50000"
    assert cpu_max_value(0.001, 100) == "1000 100000"   # kernel minimum quota


def test_cpu_controller_writes(target, fake_cg):
    c = CpuController(target)
    assert c.capabilities() == {"cpu.cores": None, "cpu.period": None, "cpu.cpus": None}
    assert c.apply(_lim(cpu__cores=0.5), {"cpu.cores"}) == []
    assert (fake_cg / "cpu.max").read_text() == "50000 100000"
    c.apply(_lim(cpu__cores=0.5, cpu__period=20.0), {"cpu.period"})
    assert (fake_cg / "cpu.max").read_text() == "10000 20000"
    c.apply(_lim(cpu__cpus="0-1"), {"cpu.cpus"})
    assert (fake_cg / "cpuset.cpus").read_text() == "0-1"
    c.apply(_lim(cpu__cpus=None), {"cpu.cpus"})
    assert (fake_cg / "cpuset.cpus").read_text() == "\n"     # empty = inherit from parent


def test_cpu_unchanged_knobs_not_written(target, fake_cg):
    CpuController(target).apply(_lim(cpu__cores=0.5), {"mem.max"})
    assert (fake_cg / "cpu.max").read_text() == "max 100000\n"


def test_memory_controller(target, fake_cg):
    c = MemoryController(target)
    c.apply(_lim(mem__high=800 * 2**20, mem__max=2**30, mem__swap_max=0), {"mem.high", "mem.max", "mem.swap_max"})
    assert (fake_cg / "memory.high").read_text() == "838860800"
    assert (fake_cg / "memory.max").read_text() == "1073741824"
    assert (fake_cg / "memory.swap.max").read_text() == "0"
    c.apply(_lim(), {"mem.max"})
    assert (fake_cg / "memory.max").read_text() == "max"


def test_memory_swap_capability(target, fake_cg):
    (fake_cg / "memory.swap.max").unlink()
    caps = MemoryController(target).capabilities()
    assert caps["mem.swap_max"] and caps["mem.max"] is None


def test_io_controller(target, fake_cg):
    assert io_max_line("259:0", _lim(io__wbps=20 * 2**20)) == "259:0 rbps=max wbps=20971520 riops=max wiops=max"
    c = IoController(target)
    c.apply(_lim(io__wbps=20 * 2**20), {"io.wbps"})
    assert (fake_cg / "io.max").read_text() == "259:0 rbps=max wbps=20971520 riops=max wiops=max"
    target.io_device = None
    assert IoController(target).capabilities()["io.rbps"].startswith("no block device")


def test_pids_controller(target, fake_cg):
    c = PidsController(target)
    c.apply(_lim(pids__max=16), {"pids.max"})
    assert (fake_cg / "pids.max").read_text() == "16"
    c.apply(_lim(), {"pids.max"})
    assert (fake_cg / "pids.max").read_text() == "max"


def test_missing_controller_capability(target, fake_cg):
    (fake_cg / "cpu.max").unlink()
    (fake_cg / "pids.max").unlink()
    caps = C.capabilities(C.build(target))
    assert caps["cpu.cores"] and caps["pids.max"]
    assert caps["mem.max"] is None
    # No netns and no data fs on a raw cgroup target.
    assert caps["net.loss"] and caps["disk.capacity"]


def test_unified_controller_sets_and_reverts(target, fake_cg, tmp_path):
    (fake_cg / "cpu.weight").write_text("100\n")
    snap = Snapshot(tmp_path / "snap.json", "r1")
    u = UnifiedController(target, snap, files={"cpu.weight"})
    assert u.knobs == ("unified.cpu.weight",)
    u.snapshot(u.knobs)
    u.apply({"unified.cpu.weight": "50"}, {"unified.cpu.weight"})
    assert (fake_cg / "cpu.weight").read_text() == "50"
    u.apply({}, {"unified.cpu.weight"})            # segment ended
    assert (fake_cg / "cpu.weight").read_text() == "100"


def test_write_records_snapshot_before_writing(target, fake_cg, tmp_path):
    sp = tmp_path / "snapshot.json"
    snap = Snapshot(sp, "r1")
    errs = PidsController(target, snap).apply(_lim(pids__max=4), {"pids.max"})
    assert errs == []
    data = json.loads(sp.read_text())
    assert data["files"][str(fake_cg / "pids.max")] == "max\n"


def test_write_error_reported(target, fake_cg):
    (fake_cg / "pids.max").unlink()
    (fake_cg / "pids.max").mkdir()   # writing a directory fails
    errs = PidsController(target).apply(_lim(pids__max=4), {"pids.max"})
    assert len(errs) == 1 and errs[0].startswith("pids.max='4'")


@pytest.mark.parametrize("cls", C.CLASSES)
def test_every_controller_capabilities_cover_its_knobs(target, cls):
    c = cls(target)
    assert set(c.capabilities()) == set(c.knobs)
