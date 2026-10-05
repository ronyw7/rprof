import asyncio
import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from rprof.agentview import AgentView
from rprof.client import NOT_VISIBLE, Client, RprofRequestError, RprofUnavailable
from rprof.clock import SimClock
from rprof.events import ControlHandler, ControlServer, EventLog
from rprof.profile import load_profile

from unit_helpers import EXAMPLE


class FakeRun:
    def __init__(self, visibility="full", token=None):
        self.clock = SimClock(61.2)
        self.profile = load_profile(EXAMPLE)
        self.profile.visibility = visibility
        self.mode = "enforce"
        self.run_id = "2026-10-02T1531-task12"
        self.token = token
        self.view = AgentView(self.profile)
        self.events = EventLog(None, self.clock)
        self.log = []
        self.events.listeners.append(self.log.append)
        self.counters = {"mem": {"events": {"oom_kill": 0}}, "pids": {"events_max": 0}}

    def read_now(self):
        import copy
        return copy.deepcopy(self.counters)


@pytest.fixture
def run():
    return FakeRun()


@pytest.fixture
def h(run):
    return ControlHandler(run)


def test_hello(h):
    r = h.handle({"id": 1, "type": "hello", "client": "x", "version": "1"})
    assert r["id"] == 1 and r["ok"] is True
    assert r["run_id"] == "2026-10-02T1531-task12" and r["mode"] == "enforce" and r["visibility"] == "full"
    assert r["t"] == 61.2 and "rprof_version" in r


def test_tool_start_end_memory_failure(h, run):
    r = h.handle({"id": 7, "type": "tool_start", "call_id": "c12", "cmd": "psql -f load.sql", "step": 3})
    assert r["ok"] and r["segment"] == 1 and r["t"] == 61.2
    assert r["limits"]["mem"]["max"] == 2**30 and r["limits"]["pids"]["max"] == 16
    assert r["deadline_s"] == 300.0 and r["feedback"] == "errno"
    assert r["view_text"].startswith("t = 61 s · segment 1 of 4")
    assert h.running_ids() == ["c12"]
    run.clock.t = 63.2
    run.counters["mem"]["events"]["oom_kill"] = 1
    r = h.handle({"id": 8, "type": "tool_end", "call_id": "c12", "exit_code": 137, "duration_s": 2.0,
                  "timed_out": False})
    assert r == {"id": 8, "ok": True, "t": 63.2, "cause": "memory",
                 "explain": "Killed: memory limit 1 GiB reached (segment 1, 60–120 s)."}
    types = [e["type"] for e in run.log]
    assert types == ["tool_start", "view", "tool_end"]
    assert run.log[-1]["cause"] == "memory" and run.log[-1]["call_id"] == "c12"


def test_tool_end_success(h):
    h.handle({"id": 1, "type": "tool_start", "call_id": "a", "cmd": "ls"})
    r = h.handle({"id": 2, "type": "tool_end", "call_id": "a", "exit_code": 0, "duration_s": 0.1})
    assert r["cause"] is None and r["explain"] is None


def test_errors(h, run):
    def code(req, **kw):
        r = h.handle(req, **kw)
        assert r["ok"] is False
        return r["error"]["code"]
    assert code({"id": 1, "type": "nope"}) == "bad_request"
    assert code({"id": 1, "type": "tool_start", "cmd": "x"}) == "bad_request"
    assert code({"id": 1, "type": "tool_start", "call_id": "a", "cmd": "x", "step": "two"}) == "bad_request"
    assert code({"id": 1, "type": "tool_end", "call_id": "zzz", "exit_code": 0, "duration_s": 1}) == "unknown_call"
    h.handle({"id": 2, "type": "tool_start", "call_id": "a", "cmd": "x"})
    assert code({"id": 3, "type": "tool_start", "call_id": "a", "cmd": "x"}) == "duplicate_call"
    h.handle({"id": 4, "type": "tool_end", "call_id": "a", "exit_code": 0, "duration_s": 1})
    assert code({"id": 5, "type": "tool_start", "call_id": "a", "cmd": "x"}) == "duplicate_call"
    assert code(["not", "an", "object"]) == "bad_request"
    run.token = "secret"
    assert code({"id": 6, "type": "hello"}, inside=True) == "auth_required"
    assert code({"id": 6, "type": "hello", "token": "wrong"}, inside=True) == "auth_required"
    assert h.handle({"id": 6, "type": "hello", "token": "secret"}, inside=True)["ok"]


def test_view_state_mark(h, run):
    r = h.handle({"id": 1, "type": "view"})
    assert r["text"].startswith("t = 61 s") and r["data"]["segment"] == 1
    r = h.handle({"id": 2, "type": "state"})
    assert r["segment"] == 1 and r["mode"] == "enforce" and r["next"] == {"t": 120, "segment": 2}
    assert r["limits"]["mem"]["high"] == 800 * 2**20
    r = h.handle({"id": 3, "type": "mark", "label": "task_done", "data": {"x": 1}})
    assert r == {"id": 3, "ok": True, "t": 61.2}
    assert run.log[-1] == {"t": 61.2, "type": "mark", "label": "task_done", "data": {"x": 1}}
    assert [e["via"] for e in run.log if e["type"] == "view"] == ["view"]


def test_view_none_visibility():
    run = FakeRun("none")
    h = ControlHandler(run)
    assert h.handle({"id": 1, "type": "view"})["text"] is None
    r = h.handle({"id": 2, "type": "tool_start", "call_id": "c", "cmd": "x"})
    assert r["view_text"] is None and r["limits"]["mem"]["max"] == 2**30
    assert [e["type"] for e in run.log] == ["view", "tool_start"]


def test_on_sample_tracks_min_free(h):
    h.handle({"id": 1, "type": "tool_start", "call_id": "a", "cmd": "dd"})
    h.on_sample({"disk": {"free_bytes": 500}})
    h.on_sample({"disk": {"free_bytes": 900}})
    assert h.running_calls()[0].min_free == 500
    r = h.handle({"id": 2, "type": "tool_end", "call_id": "a", "exit_code": 1, "duration_s": 1})
    assert r["cause"] == "disk"


# ---------------------------------------------------------------- over a real socket

class ServerThread:
    def __init__(self, run, inside=False):
        self.dir = Path(tempfile.mkdtemp(dir="/tmp", prefix="rprof-ut-"))
        self.path = self.dir / "control.sock"
        self.loop = asyncio.new_event_loop()
        self.srv = ControlServer(ControlHandler(run))
        self.started = threading.Event()
        self.th = threading.Thread(target=self._run, args=(inside,), daemon=True)
        self.th.start()
        assert self.started.wait(5)

    def _run(self, inside):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.srv.listen(self.path, inside=inside))
        self.started.set()
        self.loop.run_forever()

    def stop(self):
        fut = asyncio.run_coroutine_threadsafe(self.srv.close(), self.loop)
        fut.result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.th.join(5)
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def server(run):
    s = ServerThread(run)
    yield s
    s.stop()


def test_client_roundtrip(server, run):
    c = Client(socket=str(server.path))
    assert c.hello()["run_id"] == run.run_id
    info = c.tool_start("c1", "hog-mem 1G 5", step=1)
    assert info.segment == 1 and info.deadline_s == 300.0 and info.view_text.startswith("t = 61 s")
    run.counters["pids"]["events_max"] = 3
    fb = c.tool_end("c1", 1, 0.5)
    assert fb.cause == "pids" and fb.explain.startswith("Failed: process limit of 16 reached")
    st = c.state()
    assert st.segment == 1 and st.next == {"t": 120, "segment": 2}
    v = c.view()
    assert v.text.startswith("t = 61 s") and v.data["segment"] == 1
    c.mark("task_done", attempt=2)
    assert run.log[-1]["label"] == "task_done" and run.log[-1]["data"] == {"attempt": 2}
    with pytest.raises(RprofRequestError) as ei:
        c.tool_end("nope", 0, 1.0)
    assert ei.value.code == "unknown_call"
    c.close()


def test_agent_tool_formats(server, run):
    c = Client(socket=str(server.path))
    a = c.agent_tool()
    assert a.definition["name"] == "resource_status" and a.definition["input_schema"]["properties"] == {}
    o = c.agent_tool("openai")
    assert o.definition["type"] == "function" and o.definition["function"]["name"] == "resource_status"
    assert a.call().startswith("t = 61 s")
    assert run.log[-1]["type"] == "view" and run.log[-1]["via"] == "agent_tool"
    with pytest.raises(ValueError):
        c.agent_tool("gemini")


def test_agent_tool_not_visible():
    run = FakeRun("none")
    s = ServerThread(run)
    try:
        c = Client(socket=str(s.path))
        assert c.view() is None
        assert c.agent_tool().call() == NOT_VISIBLE
    finally:
        s.stop()


def test_parallel_clients(server, run):
    c = Client(socket=str(server.path))
    errs = []

    def work(i):
        try:
            c.tool_start(f"p{i}", "x")
            c.tool_end(f"p{i}", 0, 0.1)
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ths = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert not errs
    assert sum(e["type"] == "tool_end" for e in run.log) == 8


def test_inside_socket_token():
    run = FakeRun(token="tok")
    s = ServerThread(run, inside=True)
    try:
        with pytest.raises(RprofRequestError) as ei:
            Client(socket=str(s.path), token="bad").hello()
        assert ei.value.code == "auth_required"
        assert Client(socket=str(s.path), token="tok").hello()["ok"]
    finally:
        s.stop()


def test_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("RPROF_RUN", raising=False)
    monkeypatch.delenv("RPROF_SOCKET", raising=False)
    with pytest.raises(RprofUnavailable):
        Client()
    c = Client(socket="/tmp/rprof-definitely-missing.sock", timeout_s=0.2)
    with pytest.raises(RprofUnavailable):
        c.state()


def test_client_env_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("RPROF_RUN", str(tmp_path))
    monkeypatch.delenv("RPROF_SOCKET", raising=False)
    monkeypatch.setenv("RPROF_HARNESS_TOKEN", "t0k")
    c = Client()
    assert c.socket_path == str(tmp_path / "control.sock") and c.token == "t0k"
