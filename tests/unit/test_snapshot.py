import json

from rprof import snapshot as S
from rprof.controllers.cpu import CpuController
from rprof.controllers.memory import MemoryController
from rprof.controllers.pids import PidsController
from rprof import knobs as K


def test_snapshot_restore_byte_identical(target, fake_cg, tmp_path):
    before = {p.name: p.read_bytes() for p in fake_cg.iterdir()}
    snap = S.Snapshot(tmp_path / "snapshot.json", "r1")
    ctrls = [CpuController(target, snap), MemoryController(target, snap), PidsController(target, snap)]
    knobs = {"cpu.cores", "cpu.cpus", "mem.high", "mem.max", "mem.swap_max", "pids.max"}
    for c in ctrls:
        c.snapshot(knobs)
    lim = K.defaults()
    lim.update({"cpu.cores": 0.5, "cpu.cpus": "0", "mem.high": 2**28, "mem.max": 2**29, "pids.max": 9})
    for c in ctrls:
        c.apply(lim, knobs)
    assert (fake_cg / "memory.max").read_text() == str(2**29)
    # Restore from the persisted JSON, as `rprof reset --run` would.
    errs = S.restore(S.Snapshot.load(tmp_path / "snapshot.json"))
    assert errs == []
    after = {p.name: p.read_bytes() for p in fake_cg.iterdir()}
    # restore writes stripped values; compare ignoring trailing newline (cgroupfs adds it back on read)
    for name, b in before.items():
        a = after[name]
        assert a.strip() == b.strip(), name


def test_snapshot_persisted_before_first_write(target, fake_cg, tmp_path, monkeypatch):
    sp = tmp_path / "snapshot.json"
    snap = S.Snapshot(sp, "r1")
    seen = []
    import rprof.controllers.base as base
    real = base.write_text

    def spy(path, value):
        seen.append(json.loads(sp.read_text())["files"].get(str(path)))
        real(path, value)
    monkeypatch.setattr(base, "write_text", spy)
    PidsController(target, snap).apply({**K.defaults(), "pids.max": 3}, {"pids.max"})
    assert seen == ["max\n"]


def test_record_file_keeps_first_original(tmp_path):
    f = tmp_path / "pids.max"
    f.write_text("max\n")
    snap = S.Snapshot(tmp_path / "s.json", "r")
    snap.record_file(f)
    f.write_text("5\n")
    snap.record_file(f)
    assert snap.original(f) == "max\n"
    assert not snap.record_file(tmp_path / "missing")


def test_restore_io_max_per_device(tmp_path, monkeypatch):
    p = tmp_path / "io.max"
    p.write_text("259:0 rbps=max wbps=20971520 riops=max wiops=max\n8:0 rbps=100 wbps=max riops=max wiops=max\n")
    writes = []
    monkeypatch.setattr(S, "write_text", lambda path, v: writes.append(v))
    S.restore_file(str(p), "8:0 rbps=100 wbps=max riops=max wiops=max\n")
    assert writes == ["259:0 rbps=max wbps=max riops=max wiops=max",
                      "8:0 rbps=100 wbps=max riops=max wiops=max"]


def test_restore_skips_vanished_files_and_removes_ballast(tmp_path):
    ballast = tmp_path / ".rprof-ballast"
    ballast.write_bytes(b"x" * 10)
    snap = S.Snapshot(tmp_path / "s.json", "r")
    snap.data["files"][str(tmp_path / "gone" / "memory.max")] = "max\n"
    snap.set_ballast(str(ballast))
    assert S.restore(snap) == []
    assert not ballast.exists()
    assert snap.data["ballast"] is None


def test_merge_keeps_older_originals(tmp_path):
    a = S.Snapshot(None, "a")
    a.data["files"]["/x"] = "1"
    b = S.Snapshot(None, "b")
    b.data["files"].update({"/x": "2", "/y": "3"})
    b.add_tc({"side": "host", "dev": "veth0"})
    a.merge(b)
    assert a.data["files"] == {"/x": "1", "/y": "3"}
    assert a.data["tc"] == [{"side": "host", "dev": "veth0"}]


def test_target_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("RPROF_LOCK_DIR", str(tmp_path / "locks"))
    import pytest
    from rprof.util import TargetLocked
    lk = S.TargetLock("sbx", "run=a").acquire()
    with pytest.raises(TargetLocked) as ei:
        S.TargetLock("sbx", "run=b").acquire()
    assert ei.value.exit_code == 74 and "run=a" in str(ei.value)
    lk.release()
    S.TargetLock("sbx", "run=b").acquire().release()
