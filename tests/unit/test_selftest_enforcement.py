"""The enforcement checks, run against a fake container: no Docker or root needed.

The fake parses every limit the way the real container does, so a value a knob can't take
fails here instead of halfway through a selftest on the host.
"""

from __future__ import annotations

import time
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
    knobs, features = S._enforcement(quick=True, image="rprof-testbox", echo=lines.append)
    assert set(knobs) == {k.name for k in K.KNOBS.values() if k.enforced}
    for name, r in knobs.items():
        assert r["workload"] and r["result"] and "ok" in r, name
    rows = [ln for ln in lines if ln.lstrip().startswith(("ok", "FAIL", "skip"))]
    assert len(rows) == len(knobs)
    assert any(ln.lstrip().startswith("info") for ln in lines)          # buffered writes
    assert "io_buffered_writes_throttled" in features


def test_row_columns_line_up():
    a = S.row("ok", "cpu.cores=0.5", "stress-ng --cpu 2 for 8 s", "0.50 cores", "0.40–0.60 cores")
    b = S.row("FAIL", "net.partition=reject", "curl to the peer", "refused in 0.10 s", "refused within 1.5 s")
    assert a.index("stress-ng") == b.index("curl") and a.index("0.50") == b.index("refused in")
