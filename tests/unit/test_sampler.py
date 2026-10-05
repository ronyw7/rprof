import json

from rprof import controllers as C
from rprof.clock import SimClock
from rprof.sampler import Sampler
from rprof.target.cgroup import parse_flat, parse_nested, parse_psi


def test_parsers():
    assert parse_flat("usage_usec 10\nnr_periods 3\nbad line here\n") == {"usage_usec": 10, "nr_periods": 3}
    assert parse_psi("some avg10=0.00 avg60=0.00 avg300=0.00 total=1500\n"
                     "full avg10=0.00 avg60=0.00 avg300=0.00 total=700\n") == {"some_us": 1500, "full_us": 700}
    assert parse_nested("259:0 rbytes=4096 wbytes=8192 rios=1\n8:0 rbps=max wbps=10\n") == {
        "259:0": {"rbytes": 4096, "wbytes": 8192, "rios": 1}, "8:0": {"rbps": "max", "wbps": 10}}


def test_tick_fields(target, tmp_path):
    clock = SimClock(1.25)
    out = tmp_path / "samples.jsonl"
    s = Sampler(target, C.build(target), clock, hz=10, out=out, state_fn=lambda t: (2, ["c1"]))
    smp = s.tick()
    s.close()
    assert smp["t"] == 1.25 and smp["segment"] == 2 and smp["running_calls"] == ["c1"]
    assert smp["t_wall"].endswith("Z")
    assert smp["cpu"] == {"usage_usec": 1000000, "user_usec": 600000, "system_usec": 400000,
                          "nr_periods": 10, "nr_throttled": 2, "throttled_usec": 5000}
    m = smp["mem"]
    assert m["current"] == 104857600 and m["anon"] == 52428800 and m["file"] == 41943040
    assert m["pgmajfault"] == 7 and m["swap_current"] == 0
    assert m["events"] == {"high": 3, "max": 4, "oom": 1, "oom_kill": 1}
    assert "peak" not in m                  # no memory.peak file: omitted, not null
    assert smp["io"] == {"259:0": {"rbytes": 4096, "wbytes": 8192, "rios": 1, "wios": 2}}
    assert smp["pids"] == {"current": 5, "events_max": 0}
    assert smp["psi"]["cpu"] == {"some_us": 1500, "full_us": 700}
    assert "net" not in smp and "disk" not in smp
    assert json.loads(out.read_text().splitlines()[0])["t"] == 1.25


def test_tick_rereads_files(target, fake_cg):
    s = Sampler(target, C.build(target), SimClock(), hz=10)
    assert s.tick()["pids"]["current"] == 5
    (fake_cg / "pids.current").write_text("17\n")
    assert s.tick()["pids"]["current"] == 17
    s.close()


def test_omits_missing_metrics(target, fake_cg):
    for f in ("memory.swap.current", "io.stat", "io.pressure"):
        (fake_cg / f).unlink()
    s = Sampler(target, C.build(target), SimClock(), hz=10)
    smp = s.tick()
    s.close()
    assert "swap_current" not in smp["mem"]
    assert "io" not in smp
    assert "io" not in smp["psi"]
    assert "null" not in json.dumps(smp)


def test_hz_bounds(target):
    import pytest
    for hz in (0.5, 101):
        with pytest.raises(ValueError):
            Sampler(target, [], SimClock(), hz=hz)
