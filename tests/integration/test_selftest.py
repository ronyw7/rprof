"""`rprof selftest --only fidelity`: known workloads, recorded through a real run."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from conftest import ROOT

pytestmark = pytest.mark.integration

# These depend only on bytes and process counts, not on how busy the machine is, so they
# must pass anywhere. `cpu` and `alignment` measure speed and are only reported here.
LOAD_INDEPENDENT = ("memory", "memory_release", "io", "page_cache", "network", "pids")


def test_selftest_fidelity(tmp_path):
    out = tmp_path / "caps.json"
    r = subprocess.run([sys.executable, "-m", "rprof.cli", "selftest", "--only", "fidelity", "--out", str(out)],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    print(r.stdout)
    fid = json.loads(out.read_text())["fidelity"]
    assert set(fid) >= set(LOAD_INDEPENDENT) | {"cpu", "alignment"}
    for name in LOAD_INDEPENDENT:
        assert fid[name]["ok"] is True, (name, fid[name]["detail"])
    assert "DEBUG" not in r.stderr                     # rprof.log stays out of the console
