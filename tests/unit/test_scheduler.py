import random

from rprof.clock import SimClock
from rprof.controllers.base import Controller
from rprof.events import EventLog
from rprof.profile import profile_from_dict
from rprof.runner import managed_knobs
from rprof.scheduler import Scheduler


class FakeCtl(Controller):
    name = "fake"

    def __init__(self, knobs):
        self.knobs = tuple(knobs)
        self.calls = []

    def apply(self, limits, changed):
        self.calls.append(({k: limits[k] for k in sorted(changed)}))
        return []


def _profile():
    return profile_from_dict({"version": 1, "name": "sched",
                              "defaults": {"cpu": {"cores": 4}},
                              "segments": [{"from": 5, "to": 10, "cpu": {"cores": 0.5}},
                                           {"from": 8, "to": 12, "mem": {"max": "1Gi"}}]})


def _sched(mode="enforce"):
    p = _profile()
    clock = SimClock()
    ev = EventLog(None, clock)
    seen = []
    ev.listeners.append(seen.append)
    ctl = FakeCtl(["cpu.cores", "cpu.period", "cpu.cpus", "mem.high", "mem.max", "mem.swap_max"])
    s = Scheduler(p, [ctl], managed_knobs(p), mode, clock, ev)
    return s, ctl, seen


def test_applies_only_changed_knobs():
    s, ctl, seen = _sched()
    s.apply_at(0.0)
    assert set(ctl.calls[0]) == {"cpu.cores", "cpu.period", "cpu.cpus", "mem.high", "mem.max", "mem.swap_max"}
    s.apply_at(5.0)
    assert ctl.calls[1] == {"cpu.cores": 0.5}
    s.apply_at(8.0)
    assert ctl.calls[2] == {"mem.max": 2**30}
    s.apply_at(10.0)
    assert ctl.calls[3] == {"cpu.cores": 4.0}
    s.apply_at(12.0)
    assert ctl.calls[4] == {"mem.max": None}
    applied = [e for e in seen if e["type"] == "segment_applied"]
    assert [e["segment"] for e in applied] == [0, 1, 1, 2, 0]
    assert applied[2]["active_segments"] == [1, 2]
    assert applied[1]["changed"] == ["cpu.cores"] and applied[1]["errors"] == []
    s.shutdown()


def test_measure_mode_writes_nothing():
    s, ctl, seen = _sched("measure")
    for b in [0.0] + s.profile.boundaries():
        s.apply_at(b)
    assert ctl.calls == []
    applied = [e for e in seen if e["type"] == "segment_applied"]
    assert len(applied) == 5 and not any(e["enforced"] for e in applied)
    s.shutdown()


def test_event_independence():
    """The active segment at each second depends on time alone, whatever tool calls arrive."""
    p = _profile()
    expected = {t: p.segment_at(t)[0] for t in range(0, 15)}
    rng = random.Random(1)
    for _ in range(3):
        clock = SimClock()
        got = {}
        for t in range(0, 15):
            clock.t = t
            for _ in range(rng.randint(0, 3)):   # tool_start / tool_end noise has no effect on the schedule
                clock.t = t + rng.random() * 0.9
            clock.t = t
            got[t] = p.segment_at(clock.now())[0]
        assert got == expected
    assert expected[4] == 0 and expected[5] == 1 and expected[9] == 1 and expected[10] == 2 and expected[12] == 0


def test_scheduler_run_sleeps_to_boundaries():
    import asyncio
    s, ctl, seen = _sched()
    s.profile = profile_from_dict({"version": 1, "name": "fast",
                                   "segments": [{"from": 0.05, "to": 0.1, "cpu": {"cores": 1}}]})
    s.managed = managed_knobs(s.profile)
    from rprof.clock import RunClock
    s.clock = RunClock()
    s.apply_at(0.0)

    async def go():
        stop = asyncio.Event()
        await s.run(stop)
    asyncio.run(go())
    assert [c.get("cpu.cores", "-") for c in ctl.calls[1:]] == [1.0, None]
    s.shutdown()
