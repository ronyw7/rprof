from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from rprof import gen
from rprof.cli import app
from rprof.profile import profile_from_dict

GOLDEN = Path(__file__).parent / "golden" / "random-seed7.yaml"
ARGS = ["gen", "random", "--knobs", "mem.max,pids.max", "--levels", "mem.max:512Mi,1Gi,max;pids.max:16,64,max",
        "--seed", "7", "--duration", "300", "--name", "golden-random"]


def _valid(d):
    return profile_from_dict(yaml.safe_load(gen.dump(d)))


def test_random_matches_golden_byte_for_byte():
    r = CliRunner().invoke(app, ARGS)
    assert r.exit_code == 0, r.output
    assert r.stdout == GOLDEN.read_text()


def test_random_deterministic_and_seed_sensitive():
    a = gen.dump(gen.random_profile(["cpu.cores"], {"cpu.cores": ["0.5", "1", "max"]}, 300, 30, 5, 120, 1))
    b = gen.dump(gen.random_profile(["cpu.cores"], {"cpu.cores": ["0.5", "1", "max"]}, 300, 30, 5, 120, 1))
    c = gen.dump(gen.random_profile(["cpu.cores"], {"cpu.cores": ["0.5", "1", "max"]}, 300, 30, 5, 120, 2))
    assert a == b and a != c


def test_random_segment_lengths_and_shared_levels():
    d = gen.random_profile(["mem.max", "pids.max"], {"mem.max": ["512Mi", "max"], "pids.max": ["512Mi", "max"]},
                           600, 30, 5, 60, 3)
    p = _valid(d)
    assert d["source"]["levels"]["pids.max"] == ["max"]          # invalid shared levels dropped per knob
    for s in p.segments:
        assert s.t1 <= 600 and set(s.values) == {"mem.max"}
    with pytest.raises(ValueError):
        gen.random_profile(["pids.max"], {"pids.max": ["1Gi"]}, 60, 10, 1, 20, 1)


def test_const_step_square():
    p = _valid(gen.const("mem.max", "1Gi", 120))
    assert [(s.t0, s.t1) for s in p.segments] == [(0, 120)] and p.limits_at(5)["mem.max"] == 2**30
    p = _valid(gen.step("cpu.cores", "0.5", 60, 120))
    assert p.limits_at(90)["cpu.cores"] == 0.5 and p.limits_at(30)["cpu.cores"] is None
    p = _valid(gen.square("cpu.cores", "0.5", "4", 20, 0.25, 100))
    assert len(p.segments) == 5 and (p.segments[1].t0, p.segments[1].t1) == (20, 25)
    assert p.limits_at(22)["cpu.cores"] == 0.5 and p.limits_at(30)["cpu.cores"] == 4.0
    with pytest.raises(ValueError):
        gen.step("cpu.cores", "0.5", 10, 5)
    with pytest.raises(ValueError):
        gen.square("cpu.cores", "0.5", "max", 10, 1.5, 100)


def test_trace_csv(tmp_path):
    f = tmp_path / "neighbor.csv"
    f.write_text("time,usage\n0,1.0\n10,1.02\n20,3.0\n30,3.0\n40,0.5\n")
    d = gen.trace(f, "cpu.cores", "4", time_scale=1.0, merge=0.05, min_len=1.0)
    p = _valid(d)
    lv = [s.values["cpu.cores"] for s in p.segments]
    assert lv[0] == pytest.approx(2.99, abs=0.02) and lv[1] == 1.0 and lv[-1] == 3.5
    assert p.segments[0].t0 == 0 and p.segments[-1].t1 == 50
    d2 = gen.trace(f, "cpu.cores", "4", time_scale=0.5)
    assert _valid(d2).segments[-1].t1 == 25


def test_trace_mahimahi(tmp_path):
    f = tmp_path / "link.trace"
    f.write_text("\n".join(str(ms) for ms in list(range(0, 1000, 1)) + list(range(1000, 2000, 10))) + "\n")
    p = _valid(gen.trace(f, "net.rate", "max", fmt="mahimahi", merge=0.0))
    rates = [s.values["net.rate"] for s in p.segments]
    assert rates == [1000 * 1500 * 8, 100 * 1500 * 8]


def test_sweep(tmp_path, example):
    base = Path(example.path)
    paths = gen.sweep(base, "mem.max", ["4Gi", "512Mi"], tmp_path, start=60, end=120)
    assert [p.name for p in paths] == ["mem-squeeze-mid-mem-max-4gi.yaml", "mem-squeeze-mid-mem-max-512mi.yaml"]
    for path, lv in zip(paths, (4 * 2**30, 512 * 2**20)):
        p = profile_from_dict(yaml.safe_load(path.read_text()))
        assert p.limits_at(90)["mem.max"] == lv
        assert p.limits_at(90)["pids.max"] == 16          # the rest of segment 1 survives
    paths = gen.sweep(base, "cpu.cores", ["1", "2"], tmp_path / "d")
    p = profile_from_dict(yaml.safe_load(paths[0].read_text()))
    assert p.limits_at(10)["cpu.cores"] == 1.0 and p.limits_at(200)["cpu.cores"] == 0.5
