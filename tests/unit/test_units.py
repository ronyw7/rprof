import pytest

from rprof import knobs as K
from rprof import units as u


@pytest.mark.parametrize("v,exp", [
    ("512Mi", 512 * 2**20), ("1Gi", 2**30), ("800Mi", 800 * 2**20), ("1G", 10**9), ("10K", 10_000),
    ("2Ti", 2 * 2**40), (4096, 4096), ("max", None), ("123", 123),
])
def test_parse_bytes(v, exp):
    assert u.parse_bytes(v) == exp


@pytest.mark.parametrize("v", ["1Gx", "1.5Gi", "-1", "Gi", "ten", 0, True, 1.5])
def test_parse_bytes_errors(v):
    with pytest.raises(u.UnitError):
        u.parse_bytes(v)


def test_parse_bytes_message():
    with pytest.raises(u.UnitError, match="^1Gx is not a byte size$"):
        u.parse_bytes("1Gx")


def test_bytes_zero_allowed_for_swap():
    assert K.KNOBS["mem.swap_max"].parse(0) == 0
    with pytest.raises(u.UnitError):
        K.KNOBS["mem.max"].parse(0)


@pytest.mark.parametrize("v,unit,exp", [
    ("100ms", "s", 0.1), ("100ms", "ms", 100.0), ("30s", "s", 30.0), ("2m", "s", 120.0), ("1.5s", "ms", 1500.0),
])
def test_parse_duration(v, unit, exp):
    assert u.parse_duration(v, unit=unit) == pytest.approx(exp)


@pytest.mark.parametrize("v", ["100", 100, "1h", "ms"])
def test_parse_duration_errors(v):
    with pytest.raises(u.UnitError):
        u.parse_duration(v)


def test_parse_percent():
    assert u.parse_percent("30%") == 30.0
    assert u.parse_percent("0%") == 0.0
    for bad in ("30", 30, "101%", "x%"):
        with pytest.raises(u.UnitError):
            u.parse_percent(bad)


@pytest.mark.parametrize("v,exp", [
    ("10mbit", 10_000_000), ("500kbit", 500_000), ("1mibit", 2**20), ("1mbps", 8_000_000),
    ("10Mbit", 10_000_000), ("100bit", 100), ("1gbit", 10**9), ("max", None),
])
def test_parse_rate(v, exp):
    assert u.parse_rate(v) == exp


@pytest.mark.parametrize("v", ["10", 10, "10mb", "fast"])
def test_parse_rate_errors(v):
    with pytest.raises(u.UnitError):
        u.parse_rate(v)


def test_cpuset_and_cores():
    assert u.parse_cpuset("0-3,6") == "0-3,6"
    assert u.parse_cpuset(0) == "0"
    assert u.parse_cpuset("all") is None
    assert u.cpuset_size("0-3,6") == 5
    for bad in ("3-1", "a", "0-"):
        with pytest.raises(u.UnitError):
            u.parse_cpuset(bad)
    assert u.parse_cores(0.5) == 0.5
    assert u.parse_cores(4) == 4.0
    assert u.parse_cores("max") is None
    for bad in (0, -1, "half", True):
        with pytest.raises(u.UnitError):
            u.parse_cores(bad)


def test_counts_deadline_enum_cidrs():
    assert u.parse_count(16) == 16
    assert u.parse_count("max") is None
    with pytest.raises(u.UnitError):
        u.parse_count(0)
    assert u.parse_deadline("30s") == 30.0
    assert u.parse_deadline("none") is None
    with pytest.raises(u.UnitError):
        u.parse_enum("maybe", ("none", "reject", "drop"))
    assert u.parse_cidrs(["172.18.0.1/16"]) == ["172.18.0.0/16"]
    with pytest.raises(u.UnitError):
        u.parse_cidrs(["nope"])


def test_fmt():
    assert u.fmt_bytes(2**30) == "1 GiB"
    assert u.fmt_bytes(800 * 2**20) == "800 MiB"
    assert u.fmt_bytes(None) == "unlimited"
    assert u.fmt_rate_bits(10_000_000) == "10 Mbit/s"


def test_describe_limits_design_phrasing():
    lim = {"mem.high": 800 * 2**20, "mem.max": 2**30, "pids.max": 16, "cpu.cores": 4.0}
    parts = K.describe_limits(lim)
    assert parts[0] == "memory 1 GiB hard (800 MiB soft)"
    assert "max 16 processes" in parts
    assert "cpu 4 cores" in parts
    assert K.describe_limits({}) == []
    assert K.describe("net.partition", "reject") == "network blocked (reject)"
    assert K.describe("net.loss", 30.0) == "network loss 30%"
    assert K.describe("cpu.cores", 1.0) == "cpu 1 core"


def test_limits_json_shape():
    j = K.limits_json({"cpu.cores": 0.5, "mem.max": 2**30, "net.rate": 10_000_000, "unified.cpu.weight": "50"})
    assert j["cpu"] == {"cores": 0.5, "cpus": None, "period_ms": 100.0}
    assert j["mem"]["max"] == 2**30 and j["mem"]["swap_max"] == 0
    assert j["net"]["rate_bps"] == 10_000_000 and j["net"]["partition"] == "none"
    assert j["harness"] == {"deadline_s": None, "feedback": "errno"}
    assert j["unified"] == {"cpu.weight": "50"}
