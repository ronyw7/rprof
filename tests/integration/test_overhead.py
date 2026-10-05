"""Overhead: rprof's own CPU and its impact on the workload (design: Testing plan)."""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys

import pytest

from conftest import ROOT, tol

pytestmark = [pytest.mark.integration, pytest.mark.timing]


@pytest.mark.parametrize("hz,budget", [(10, "sampler_10hz_core_frac"), (100, "sampler_100hz_core_frac")])
def test_sampler_cpu(sandbox, tmp_path, hz, budget):
    dur = 20
    r = subprocess.run([sys.executable, "-m", "rprof.cli", "run", "--target", f"docker:{sandbox.name}",
                        "--runs-dir", str(tmp_path), "--hz", str(hz), "--duration", str(dur), "--no-report"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    run_dir = next(tmp_path.glob("*"))
    samples = [json.loads(x) for x in (run_dir / "samples.jsonl").read_text().splitlines()]
    s = [x for x in samples if "self" in x]
    assert s, "rprof did not record its own cgroup"
    frac = (s[-1]["self"]["cpu"]["usage_usec"] - s[0]["self"]["cpu"]["usage_usec"]) / 1e6 / (s[-1]["t"] - s[0]["t"])
    print(f"rprof at {hz} Hz used {frac:.2%} of a core")
    assert frac < tol("overhead", budget), frac
    assert abs(len(samples) - dur * hz) <= 0.05 * dur * hz + 2


def _bogo(sandbox) -> float:
    rc, out, _, _ = sandbox.exec("stress-ng --cpu 1 --cpu-method int64 --timeout 4s --metrics-brief 2>&1")
    m = re.search(r"cpu\s+(\d+)\s", out)
    assert m, out
    return float(m.group(1))


@pytest.mark.slow
def test_workload_impact(sandbox, rprof_factory):
    sandbox.exec("taskset -pc 1 1 >/dev/null 2>&1 || true")
    base = statistics.median(_bogo(sandbox) for _ in range(5))
    rp = rprof_factory(sandbox, hz=10)
    with_ = statistics.median(_bogo(sandbox) for _ in range(5))
    rp.stop()
    rel = abs(with_ - base) / base
    print(f"bogo ops median: without {base:.0f}, with sampler {with_:.0f} ({rel:.2%})")
    assert rel <= tol("overhead", "workload_impact_rel"), (base, with_)
