"""Fixes from the 395-run review: memory basis, OOM attribution, writer thread, socket paths, names."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest
import yaml

from rprof import knobs as K
from rprof.client import Client
from rprof.controllers.memory import MemoryController, non_reclaimable
from rprof.events import SOCKET_PATH_MAX, ControlHandler, ControlServer, socket_path_for
from rprof.profile import ProfileError, check_name, profile_from_dict
from rprof.report.data import RunData
from rprof.report.report import build_report
from rprof.runner import RunOptions, RunSession
from rprof.target import Target
from rprof.target.cgroup import Cgroup
from rprof.util import JsonlWriter, RprofError, read_jsonl

from test_protocol import FakeRun

MiB = 2**20


# ---------------------------------------------------------------- 1. memory basis

def _measure_run(tmp_path: Path, file_mib: int | None, shmem_mib: int | None) -> Path:
    d = tmp_path / "run"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"run_id": "m", "mode": "measure", "target": {}}))
    prof = {"version": 1, "name": "cache", "segments": [{"from": 0, "to": 10, "mem": {"max": "128Mi"}}]}
    (d / "profile.yaml").write_text(yaml.safe_dump(prof))
    rows = []
    for i in range(101):
        m = {"current": 600 * MiB, "events": {"oom_kill": 0}}
        if file_mib is not None:
            m["file"] = file_mib * MiB
        if shmem_mib is not None:
            m["shmem"] = shmem_mib * MiB
        rows.append({"t": round(i * 0.1, 1), "t_wall": "x", "segment": 1, "running_calls": [], "mem": m})
    (d / "samples.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / "events.jsonl").write_text(json.dumps({"t": 0.0, "type": "run_start"}) + "\n"
                                    + json.dumps({"t": 10.0, "type": "run_end"}) + "\n")
    return d


def _mem_row(rep: dict) -> dict:
    seg = next(s for s in rep["segments"] if s["segment"] == 1)
    return next(k for k in seg["knobs"] if k["knob"] == "mem.max")


def test_page_cache_is_not_a_violation(tmp_path):
    # Reading a large file fills the page cache: 580 of 600 MiB is reclaimable file data.
    rd = RunData(_measure_run(tmp_path, file_mib=580, shmem_mib=0))
    rep = build_report(rd)
    row = _mem_row(rep)
    assert rep["memory_basis"] == "non_reclaimable" and row["basis"] == "non_reclaimable"
    assert row["violation"] == {"time_frac": 0.0, "peak_over": 0}
    assert row["usage"]["max"] == 20 * MiB
    assert row["usage_total"]["max"] == 600 * MiB
    total = _mem_row(build_report(rd, memory_basis="total"))
    assert total["basis"] == "total" and total["violation"]["time_frac"] == 1.0


def test_shmem_counts_as_non_reclaimable(tmp_path):
    rd = RunData(_measure_run(tmp_path, file_mib=580, shmem_mib=500))   # tmpfs data can't be dropped
    row = _mem_row(build_report(rd))
    assert row["usage"]["max"] == 520 * MiB
    assert row["violation"]["time_frac"] == 1.0
    assert row["violation"]["peak_over"] == 392 * MiB


def test_old_runs_without_shmem_say_so(tmp_path):
    rd = RunData(_measure_run(tmp_path, file_mib=580, shmem_mib=None))
    rep = build_report(rd)
    assert rep["memory_basis"] == _mem_row(rep)["basis"] == "non_reclaimable_without_shmem"
    (tmp_path / "b").mkdir()
    no_stat = RunData(_measure_run(tmp_path / "b", file_mib=None, shmem_mib=None))
    assert build_report(no_stat)["memory_basis"] == "total"


def test_non_reclaimable():
    assert non_reclaimable(600, 580, 0) == 20
    assert non_reclaimable(600, 580, 500) == 520
    assert non_reclaimable(600, None, None) == 600
    assert non_reclaimable(100, 150, 0) == 0


# ---------------------------------------------------------------- 5. warning when mem.max goes below usage

class Events:
    def __init__(self):
        self.got = []

    def emit(self, type_, **kw):
        self.got.append({"type": type_, **kw})


def test_warns_when_mem_max_below_usage(fake_cg):
    ev = Events()
    c = MemoryController(Target(spec="cgroup:x", kind="cgroup", cgroup=Cgroup(fake_cg)), None, "r", ev)
    lim = K.defaults()
    lim["mem.max"] = 50 * MiB              # fake memory.current is 100 MiB
    c.apply(lim, {"mem.max"})
    assert [e["code"] for e in ev.got] == ["mem_max_below_usage"]
    assert "below current usage 100 MiB" in ev.got[0]["message"]
    ev.got.clear()
    lim["mem.max"] = 200 * MiB
    c.apply(lim, {"mem.max"})
    assert ev.got == []


# ---------------------------------------------------------------- 4. background writer

def test_jsonl_writer_keeps_order_across_threads(tmp_path):
    w = JsonlWriter(tmp_path / "x.jsonl")

    def work(k):
        for i in range(500):
            w.write({"k": k, "i": i})
    ths = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    w.close()
    w.write({"late": True})                 # ignored after close
    rows = read_jsonl(tmp_path / "x.jsonl")
    assert len(rows) == 2000
    for k in range(4):
        assert [r["i"] for r in rows if r["k"] == k] == list(range(500))


def test_jsonl_writer_flush_requests(tmp_path):
    w = JsonlWriter(tmp_path / "e.jsonl", flush_every=60)
    w.write({"a": 1}, flush=True)
    for _ in range(200):
        if (tmp_path / "e.jsonl").read_text():
            break
        threading.Event().wait(0.01)
    assert json.loads((tmp_path / "e.jsonl").read_text()) == {"a": 1}
    w.close()


# ---------------------------------------------------------------- 6. long socket paths

def test_long_socket_path_uses_short_path_and_symlink(tmp_path, monkeypatch):
    short_dir = Path(os.path.realpath("/tmp")) / f"rprof-ut-sock-{os.getpid()}"
    monkeypatch.setenv("RPROF_SOCKET_DIR", str(short_dir))
    deep = tmp_path / ("d" * 60) / ("e" * 60) / "2026-10-02T1803-a-long-run-label"
    deep.mkdir(parents=True)
    want = deep / "control.sock"
    assert len(str(want)) > SOCKET_PATH_MAX
    bind, link = socket_path_for(want)
    assert link == want and bind.parent == short_dir and len(str(bind)) <= SOCKET_PATH_MAX

    run = FakeRun()
    srv = ControlServer(ControlHandler(run))
    loop = asyncio.new_event_loop()
    started = threading.Event()

    def main():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(srv.listen(want))
        started.set()
        loop.run_forever()
    th = threading.Thread(target=main, daemon=True)
    th.start()
    assert started.wait(5)
    try:
        assert want.is_symlink()
        assert Client(run_dir=str(deep)).hello()["run_id"] == run.run_id
    finally:
        asyncio.run_coroutine_threadsafe(srv.close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        th.join(5)
    assert not want.exists() and not want.is_symlink() and not bind.exists()
    with pytest.raises(RprofError):
        asyncio.new_event_loop().run_until_complete(ControlServer(ControlHandler(run)).listen(want, inside=True))


# ---------------------------------------------------------------- 8. names

@pytest.mark.parametrize("name", ["cpu-mem-demo", "task_12", "Run.v2", "a", "x" * 64])
def test_valid_names(name):
    assert check_name(name) is None
    profile_from_dict({"version": 1, "name": name})


@pytest.mark.parametrize("name", ["", "-lead", ".hidden", "a/b", "../x", "with space", "x" * 65])
def test_invalid_names(name):
    assert check_name(name)
    with pytest.raises(ProfileError) as e:
        profile_from_dict({"version": 1, "name": name})
    assert e.value.problems[0][0] == "name"


def test_run_name_is_checked():
    with pytest.raises(RprofError) as e:
        RunSession(RunOptions(target="docker:x", name="../../etc"))
    assert e.value.exit_code == 2


# ---------------------------------------------------------------- 2/3 + per-call peaks, through the handler

def test_tool_end_records_memory_peaks_and_output_match():
    run = FakeRun()                      # t = 61.2 s: segment 1 of the example, mem.max 1 GiB
    run.counters = {"mem": {"current": 100 * MiB, "file": 50 * MiB, "shmem": 0, "events": {"oom_kill": 0}},
                    "pids": {"events_max": 0}}
    h = ControlHandler(run)
    h.handle({"id": 1, "type": "tool_start", "call_id": "c1", "cmd": "duckdb -c ..."})
    h.on_sample({"mem": {"current": 400 * MiB, "file": 300 * MiB, "shmem": 10 * MiB}})
    rep = h.handle({"id": 2, "type": "tool_end", "call_id": "c1", "exit_code": 1, "duration_s": 3.0,
                    "output": "Error: Out of Memory Error: could not allocate block"})
    assert rep["cause"] == "memory"
    assert rep["explain"].startswith("Failed: out of memory under the memory limit 1 GiB")
    end = [e for e in run.log if e["type"] == "tool_end"][-1]
    assert end["mem_peak_bytes"] == 400 * MiB
    assert end["mem_peak_nonreclaimable_bytes"] == 110 * MiB
    assert end["evidence"] == {"output_match": "Error: Out of Memory Error: could not allocate block"}
    assert "output" not in end                          # the output itself is not stored
    bad = h.handle({"id": 3, "type": "tool_end", "call_id": "x", "exit_code": 1, "output": 5})
    assert bad["error"]["code"] == "bad_request"


def test_output_ignored_in_measure_mode():
    run = FakeRun()
    run.mode = "measure"
    h = ControlHandler(run)
    h.handle({"id": 1, "type": "tool_start", "call_id": "c1", "cmd": "x"})
    rep = h.handle({"id": 2, "type": "tool_end", "call_id": "c1", "exit_code": 1, "output": "MemoryError"})
    assert rep["cause"] is None


# ---------------------------------------------------------------- lock files don't pile up

def test_lock_file_removed_on_release(tmp_path, monkeypatch):
    from rprof.snapshot import TargetLock
    from rprof.util import TargetLocked
    monkeypatch.setenv("RPROF_LOCK_DIR", str(tmp_path))
    a = TargetLock("sbx", "a").acquire()
    with pytest.raises(TargetLocked):
        TargetLock("sbx", "b").acquire()
    a.release()
    assert list(tmp_path.iterdir()) == []
    b = TargetLock("sbx", "b").acquire()     # and the target can be locked again
    b.release()


# ---------------------------------------------------------------- the FileCache race

def test_file_cache_shared_by_two_threads_never_mixes_files(tmp_path):
    """Regression: poll_host and the sampler once read through one cache and its one buffer."""
    from rprof.controllers import FileCache
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("A" * 3000)
    b.write_text("B" * 50)
    fc = FileCache()
    bad = []

    def reader(path, want):
        for _ in range(3000):
            if fc.read(str(path)) != want:
                bad.append(path.name)
    ths = [threading.Thread(target=reader, args=(a, "A" * 3000)), threading.Thread(target=reader, args=(b, "B" * 50))]
    [t.start() for t in ths]
    [t.join() for t in ths]
    fc.close()
    assert bad == []


def test_host_pressure_has_its_own_cache(fake_cg):
    from rprof.clock import SimClock
    from rprof.controllers import build
    from rprof.sampler import Sampler
    t = Target(spec="cgroup:x", kind="cgroup", cgroup=Cgroup(fake_cg))
    s = Sampler(t, build(t), SimClock(), hz=10)
    assert s.host_fc is not s.fc


def test_write_errors_are_reported(tmp_path):
    w = JsonlWriter(tmp_path / "s.jsonl")
    w.write({"ok": 1})
    w.write({"bad": object()})        # not JSON-serializable
    w.write({"ok": 2})
    err = w.close()
    assert err and "1 write(s) to s.jsonl failed" in err and "TypeError" in err
    assert [r["ok"] for r in read_jsonl(tmp_path / "s.jsonl")] == [1, 2]
    clean = JsonlWriter(tmp_path / "c.jsonl")
    clean.write({"x": 1})
    assert clean.close() is None


def test_server_close_closes_open_connections(tmp_path):
    """A connected client must not keep rprof's side of the socket open after the run ends."""
    import time as _time
    path = Path(os.path.realpath("/tmp")) / f"rprof-ut-close-{os.getpid()}.sock"
    srv = ControlServer(ControlHandler(FakeRun()))
    loop = asyncio.new_event_loop()
    started = threading.Event()

    def main():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(srv.listen(path))
        started.set()
        loop.run_forever()
    th = threading.Thread(target=main, daemon=True)
    th.start()
    assert started.wait(5)
    c = Client(socket=str(path))
    c.hello()                                    # connected, and stays connected
    t0 = _time.monotonic()
    asyncio.run_coroutine_threadsafe(srv.close(), loop).result(5)
    assert _time.monotonic() - t0 < 0.5          # no 1 s wait for the open connection
    assert srv.conns == set()
    c._sock.settimeout(2)
    assert c._sock.recv(1) == b""                # the client sees end-of-file
    loop.call_soon_threadsafe(loop.stop)
    th.join(5)
