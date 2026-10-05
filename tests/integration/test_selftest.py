"""`rprof selftest --only fidelity`: known workloads, recorded through a real run."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from conftest import ROOT

pytestmark = pytest.mark.integration

# These depend only on bytes and process counts, not on how busy the machine is, so they
# must pass anywhere. `cpu` and `call_timing` measure speed and are only reported here.
LOAD_INDEPENDENT = ("memory_peak", "memory_after_exit", "disk_writes", "page_cache", "network_sent", "processes")


def test_selftest_fidelity(tmp_path):
    out = tmp_path / "caps.json"
    r = subprocess.run([sys.executable, "-m", "rprof.cli", "selftest", "--only", "fidelity", "--out", str(out)],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    print(r.stdout)
    fid = json.loads(out.read_text())["fidelity"]
    assert set(fid) >= set(LOAD_INDEPENDENT) | {"cpu", "call_timing"}
    for name in LOAD_INDEPENDENT:
        assert fid[name]["ok"] is True, (name, fid[name]["detail"])
    assert "DEBUG" not in r.stderr                     # rprof.log stays out of the console
    assert "\nFidelity\n" in r.stdout and "\nSummary\n" in r.stdout and "System is" in r.stdout
