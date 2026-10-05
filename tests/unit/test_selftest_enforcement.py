"""The enforcement checks, run against a fake container: no Docker or root needed.

The fake parses every limit the way the real container does, so a value a knob can't take
fails here instead of halfway through a selftest on the host.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from rprof import knobs as K
from rprof import selftest as S


class FakeBox:
    def __init__(self, image, name, network=None, data_mb=None):
        self.name = name
        self.target = SimpleNamespace(cgroup=SimpleNamespace(has=lambda f: True, file=lambda f: f), io_device="8:0")
        self.applied = {}

    def apply(self, **kv):
        for k, v in kv.items():
            name = k.replace("_", ".", 1)
            self.applied[name] = K.KNOBS[name].parse(v)
        return []

    def exec(self, cmd, timeout=60):
        return 0, "", 1.0

    def stat(self, f):
        return {"usage_usec": 0, "nr_periods": 0}

    def close(self):
        pass


def _run_cmd(args, **kw):
    s = " ".join(args)
    out = "172.18.0.2" if "IPAddress" in s else "172.18.0.1" if "Gateway" in s else "true"
    return SimpleNamespace(ok=True, rc=0, out=out, err="")


def test_every_enforceable_knob_is_checked_with_values_it_accepts(monkeypatch):
    monkeypatch.setattr(S, "Box", FakeBox)
    monkeypatch.setattr(S, "run_cmd", _run_cmd)
    monkeypatch.setattr(S, "read_text", lambda f: "0-3")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    lines: list[str] = []
    knobs, features = S._enforcement(quick=True, image="rprof-testbox", out=S.Reporter(lines.append))
    assert set(knobs) == {k.name for k in K.KNOBS.values() if k.enforced}
    for name, r in knobs.items():
        assert r["expected"] and r["observed"] and r["workload"] and "ok" in r, name
    statuses = [ln.split()[0] for ln in lines if ln.startswith("  ") and ln.split()[0] in S.COLORS]
    assert statuses.count("INFO") == 1                                  # buffered writes
    assert len(statuses) == len(knobs) + 1
    assert [ln.strip() for ln in lines if ln.strip() in ("CPU", "Memory", "Processes", "I/O", "Network")] == [
        "CPU", "Memory", "Processes", "I/O", "Network"]
    assert "io_buffered_writes_throttled" in features


PASS = S.Check("net.rate", True, "8–12 Mbit/s", "9.56 Mbit/s", "iperf3 to a peer container for 8 s",
               label="net.rate=10mbit", measured=9.56, unit="Mbit/s")
FAIL = S.Check("net.rate", False, "8–12 Mbit/s", "38.40 Mbit/s", "iperf3 to a peer container for 8 s",
               label="net.rate=10mbit", note="tc qdisc replace failed")


def _shown(check, **kw) -> list[str]:
    lines: list[str] = []
    S.Reporter(lines.append, **kw).check(check)
    return lines


def test_a_passing_check_is_one_row_of_expected_then_observed():
    [ln] = _shown(PASS)
    assert ln.split()[:2] == ["PASS", "net.rate=10mbit"]
    assert ln.index("expected 8–12 Mbit/s") < ln.index("observed 9.56 Mbit/s")
    other = _shown(S.Check("cpu.cores", True, "0.40–0.60 cores", "0.50 cores", "x", label="cpu.cores=0.5"))[0]
    assert ln.index("expected") == other.index("expected") and ln.index("observed") == other.index("observed")


def test_failures_expand_with_workload_and_note_and_verbose_expands_everything():
    lines = _shown(FAIL)
    assert lines[0].split() == ["FAIL", "net.rate=10mbit"]
    assert [ln.split()[0] for ln in lines[1:]] == ["expected", "observed", "workload", "note"]
    assert len(_shown(PASS)) == 1 and len(_shown(PASS, verbose=True)) == 4
    assert _shown(PASS, quiet=True) == []
    assert "\033[1;31mFAIL" in _shown(FAIL, color=True)[0] and "\033" not in _shown(FAIL)[0]


def _summary(knobs, fid) -> list[str]:
    lines: list[str] = []
    S.summarize(S.Reporter(lines.append), knobs, fid, Path("/var/lib/rprof/capabilities.json"), {})
    return lines


def test_summary_states_what_the_results_mean_for_rprof_run():
    good = {"cpu.cores": PASS.as_dict(), "net.rate": PASS.as_dict()}
    fid = {"cpu": {"ok": True}}
    lines = _summary(good, fid)
    assert any(ln.split()[:4] == ["PASS", "Enforcement", "2", "/"] for ln in lines)
    assert "  System is fully supported." in lines

    bad = {"cpu.cores": PASS.as_dict(), "net.rate": FAIL.as_dict()}
    lines = _summary(bad, fid)
    assert any(ln.split()[:2] == ["WARN", "Enforcement"] for ln in lines)
    assert "  System is partially supported." in lines
    assert any("refuses profiles that set net.rate" in ln and "--allow-degraded" in ln for ln in lines)
    assert S.quiet_line(bad, fid) == "rprof selftest: WARN (1/2 enforcement, 1/1 fidelity); failed: net.rate"
    assert S.quiet_line(good, fid) == "rprof selftest: PASS (2/2 enforcement, 1/1 fidelity)"

    skipped = {"mem.swap_max": {"ok": None, "note": "host swap is 0 MiB; the test needs 256 MiB"}}
    lines = _summary(skipped, fid)
    assert "  System is supported; some checks could not run here." in lines
    assert any("Not tested: mem.swap_max (host swap is 0 MiB" in ln for ln in lines)
