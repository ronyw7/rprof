"""Leases: the profile section, the manager's grants and ends, and the in-sandbox `lease` command."""

from __future__ import annotations

import os
import stat
import subprocess

import pytest

from rprof.describe import describe
from rprof.lease import BIN, DIR, SCRIPT, LeaseLoop, LeaseManager, parse_seconds, parse_size
from rprof.profile import ProfileError, profile_from_dict

GiB = 1 << 30


def _profile(**leases):
    return profile_from_dict({"version": 1, "name": "x", "visibility": "full",
                              "defaults": {"cpu": {"cores": 1}, "mem": {"max": "1Gi", "swap_max": 0}},
                              "leases": {"max": {"cpu": {"cores": 4}, "mem": {"max": "8Gi"}}, **leases}})


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def mgr(tmp_path):
    clock, applied, events = Clock(), [], []
    m = LeaseManager.for_profile(_profile(max_duration=1800, grace=10), tmp_path,
                                 lambda v: applied.append(dict(v)) or [], clock,
                                 lambda kind, **f: events.append((kind, f)))
    m.install()
    m.clock, m.applied, m.events = clock, applied, events
    return m


def ask(m, rid, **fields):
    (m.dir / "req" / rid).write_text("".join(f"{k}={v}\n" for k, v in fields.items()))
    m.poll()
    status, *rest = (m.dir / "resp" / rid).read_text().splitlines()
    return status, "\n".join(rest)


def test_parsing():
    assert parse_size("8G") == 8 * GiB == parse_size("8GiB") == parse_size("8gb")
    assert parse_size("512M") == 512 << 20 and parse_size("1.5g") == int(1.5 * GiB)
    for bad in ("8", "lots", "8X"):
        with pytest.raises(ValueError):
            parse_size(bad)
    assert parse_seconds("600") == 600 == parse_seconds("10m") == parse_seconds("600s")
    assert parse_seconds("1h30m") == 5400 and parse_seconds("90s") == 90
    with pytest.raises(ValueError):
        parse_seconds("soon")


def test_the_profile_section():
    p = _profile()
    assert p.lease_max == {"cpu.cores": 4.0, "mem.max": 8 * GiB}
    assert "mem.high" in p.managed_knobs()                  # a lease moves memory.high too
    assert p.resolved_dict()["leases"]["max"] == {"cpu": {"cores": 4}, "mem": {"max": "8Gi"}}
    bad = [({"max": {"io": {"wbps": "1Mi"}}}, "can't be leased"),
           ({"max": {"mem": {"max": "512Mi"}}}, "below the baseline")]
    for leases, msg in bad:
        with pytest.raises(ProfileError, match=msg):
            profile_from_dict({"version": 1, "name": "x", "defaults": {"mem": {"max": "1Gi"}}, "leases": leases})
    with pytest.raises(ProfileError, match="set the baseline"):
        profile_from_dict({"version": 1, "name": "x", "leases": {"max": {"cpu": {"cores": 4}}}})
    with pytest.raises(ProfileError, match="can't have segments"):
        profile_from_dict({"version": 1, "name": "x", "defaults": {"mem": {"max": "1Gi"}},
                           "segments": [{"from": 0, "to": 10, "mem": {"max": "2Gi"}}],
                           "leases": {"max": {"mem": {"max": "4Gi"}}}})


def test_describe_tells_the_baseline_the_ceiling_and_the_command():
    text = describe(_profile(max_duration=3600))
    assert text.startswith("Resource environment: your container has 1 CPU core, 1 GiB of memory")
    assert "up to 4 CPU cores and 8 GiB of memory in total" in text
    assert "`lease request --cpus 4 --mem 8G --for 10m`" in text and "at most 1 h" in text
    assert "lease only what you need" in text and text.endswith("given these resource limits.")


def test_install_puts_the_command_and_its_directories_in_the_sandbox(mgr):
    b = mgr.root / BIN
    assert b.read_text() == SCRIPT and os.stat(b).st_mode & 0o111
    for sub in ("req", "resp"):
        mode = os.stat(mgr.root / DIR / sub).st_mode
        assert stat.S_IMODE(mode) == 0o1777


def test_a_request_raises_the_limits_until_it_expires(mgr):
    ok, text = ask(mgr, "a", op="request", cpus="4", mem="8G", **{"for": "10m"})
    assert ok == "ok" and text.startswith("L1 granted: 4 CPU cores and 8 GiB of memory until t = 600 s")
    assert mgr.applied[-1] == {"cpu.cores": 4.0, "mem.max": 8 * GiB, "mem.high": None}
    mgr.clock.t = 599
    mgr.poll()
    assert mgr.lease is not None
    mgr.clock.t = 600                        # expires: CPU back at once, memory.high now...
    mgr.poll()
    assert mgr.lease is None and mgr.history[0].reason == "expired"
    assert mgr.applied[-1] == {"cpu.cores": 1.0, "mem.high": GiB}
    mgr.clock.t = 610                        # ...memory.max after the grace period
    mgr.poll()
    assert mgr.applied[-1] == {"mem.max": GiB, "mem.high": None}
    assert [k for k, _ in mgr.events][:3] == ["lease_installed", "lease_limits", "lease_granted"]
    assert ("lease_ended", {"id": "L1", "reason": "expired", "held_s": 600.0}) in mgr.events


def test_requests_are_capped_and_never_go_below_the_baseline(mgr):
    ok, text = ask(mgr, "a", op="request", mem="64G", **{"for": "5h"})
    assert ok == "ok" and "the most you can hold is 8 GiB of memory" in text and "at most 1800 s" in text
    assert mgr.lease.values == {"cpu.cores": 1.0, "mem.max": 8 * GiB}      # CPUs left out: the baseline
    assert mgr.lease.t_end == 1800
    ok, text = ask(mgr, "b", op="request", cpus="0.5")
    assert mgr.lease.values["cpu.cores"] == 1.0                           # never below the baseline


def test_a_smaller_lease_replaces_the_larger_one_and_memory_comes_down_gently(mgr):
    ask(mgr, "a", op="request", cpus="4", mem="8G")
    mgr.clock.t = 100
    ok, text = ask(mgr, "b", op="request", cpus="2", mem="2G")
    assert ok == "ok" and "It replaces L1" in text and "being reclaimed" in text
    assert mgr.history[0].reason == "replaced" and mgr.lease.id == "L2"
    assert mgr.applied[-1] == {"cpu.cores": 2.0, "mem.high": 2 * GiB}
    mgr.clock.t = 110
    mgr.poll()
    assert mgr.applied[-1] == {"mem.max": 2 * GiB, "mem.high": None}     # still above the baseline


def test_release_and_status(mgr):
    ok, text = ask(mgr, "a", op="release")
    assert ok == "ok" and "no lease" in text
    ask(mgr, "b", op="request", cpus="3")
    mgr.clock.t = 50
    ok, text = ask(mgr, "c", op="status")
    assert "Baseline: 1 CPU core and 1 GiB of memory" in text and "L1: 3 CPU cores and 1 GiB of memory, 550 s left" in text
    ok, text = ask(mgr, "d", op="release")
    assert text.startswith("L1 released: back to the baseline") and mgr.applied[-1] == {"cpu.cores": 1.0, "mem.max": GiB,
                                                                                          "mem.high": None}


def test_bad_requests_are_refused_with_a_reason(mgr):
    for fields, msg in (({"op": "request"}, "ask for something"), ({"op": "request", "mem": "8"}, "512M or 8G"),
                        ({"op": "request", "cpus": "many"}, "not a number"), ({"op": "fly"}, "unknown operation")):
        ok, text = ask(mgr, "x" + str(len(msg)), **fields)
        assert ok == "refused" and msg in text, text
    assert mgr.lease is None and not [a for a in mgr.applied if a]


def test_the_lease_command_end_to_end(mgr):
    """The shell command a sandboxed agent runs, against a manager polling as rprof does."""
    loop = LeaseLoop(mgr, poll_s=0.02)
    loop.start()
    env = {**os.environ, "RPROF_LEASE_DIR": str(mgr.dir)}
    cmd = ["sh", str(mgr.root / BIN)]
    try:
        r = subprocess.run(cmd + ["request", "--cpus", "2", "--mem=4G", "--for", "90s"], env=env, capture_output=True,
                           text=True, timeout=30)
        assert r.returncode == 0 and r.stdout.startswith("L1 granted: 2 CPU cores and 4 GiB of memory"), r
        r = subprocess.run(cmd + ["status"], env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "L1: 2 CPU cores and 4 GiB" in r.stdout
        r = subprocess.run(cmd + ["request", "--mem", "lots"], env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 1 and "is not a size" in r.stdout
        r = subprocess.run(cmd + ["release"], env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "L1 released" in r.stdout
        r = subprocess.run(cmd + ["--help"], env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "lease request" in r.stdout
        r = subprocess.run(cmd + ["request", "--gpus", "1"], env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 2 and "unknown option" in r.stderr
    finally:
        loop.join()
