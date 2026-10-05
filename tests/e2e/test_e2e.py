"""End-to-end: a scripted agent runs 20 SQL calls against SQLite and Postgres under a fail-knob profile.

Passes if the report marks the squeezed segments as bound, failures happen only inside
fail-knob segments, and the final DB dumps match an unconstrained (measure-mode) run.
Needs root, Docker, cgroup v2 and the postgres:16-alpine image:
    sudo .venv/bin/pytest tests/e2e
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.e2e, pytest.mark.slow]
ROOT = Path(__file__).resolve().parents[2]
PG_IMAGE = "postgres:16-alpine"

PROFILE = {
    "version": 1, "name": "e2e-sql", "visibility": "full",
    "defaults": {"harness": {"deadline": "20s", "feedback": "explain"}},
    "segments": [
        {"from": 6, "to": 14, "label": "network outage", "net": {"partition": "reject"}},
        {"from": 20, "to": 26, "label": "fork limit", "pids": {"max": 12}},
        # The SQL calls are short CPU bursts. At 0.2 cores they spent ~4% of the segment
        # throttled on a fast CPU, just under the 5% that counts as binding; 0.05 cores binds
        # on any host.
        {"from": 30, "to": 36, "label": "cpu squeeze", "cpu": {"cores": 0.05}},
    ],
}
FAIL_SEGMENTS = {1, 2}


def sh(*a, check=True, timeout=120):
    return subprocess.run(list(a), capture_output=True, text=True, check=check, timeout=timeout)


@pytest.fixture
def sandbox_pair(tmp_path):
    if sh("docker", "image", "inspect", PG_IMAGE, check=False).returncode:
        pytest.skip(f"docker pull {PG_IMAGE}")
    tag = uuid.uuid4().hex[:6]
    slice_ = f"rprofe2e{tag}.slice"          # no dashes: systemd would nest dashed slice names
    net = f"rprof-e2e-{tag}"
    sbx, pg = f"rprof-e2e-sbx-{tag}", f"rprof-e2e-pg-{tag}"
    sh("docker", "network", "create", net)
    sh("docker", "run", "-d", "--name", pg, f"--cgroup-parent={slice_}", "--network", net,
       "-e", "POSTGRES_PASSWORD=pw", PG_IMAGE)
    sh("docker", "run", "-d", "--name", sbx, f"--cgroup-parent={slice_}", "--network", net,
       "--tmpfs", "/data", "rprof-testbox", "sleep", "infinity")
    for _ in range(120):
        if sh("docker", "exec", pg, "pg_isready", "-U", "postgres", check=False).returncode == 0:
            break
        time.sleep(0.5)
    time.sleep(1.0)
    pid = int(sh("docker", "inspect", "-f", "{{.State.Pid}}", sbx).stdout)
    rel = open(f"/proc/{pid}/cgroup").read().strip().split("::")[1]
    parent = (Path("/sys/fs/cgroup") / rel.lstrip("/")).parent
    yield sbx, pg, parent
    sh("docker", "rm", "-f", sbx, pg, check=False)
    sh("docker", "network", "rm", net, check=False)


def _reset_dbs(sbx, pg):
    sh("docker", "exec", sbx, "rm", "-f", "/data/agent.db")
    sh("docker", "exec", pg, "psql", "-U", "postgres", "-q", "-c", "DROP TABLE IF EXISTS orders;")


def _profile(parent: Path) -> dict:
    """The fail-knob profile, with the fork limit one above the sandbox's idle task count."""
    idle = int((parent / "pids.current").read_text())
    prof = json.loads(json.dumps(PROFILE))
    prof["segments"][1]["pids"]["max"] = idle + 1
    return prof


def _run(tmp: Path, sbx, pg, parent, mode: str, name: str):
    p = tmp / f"{name}.yaml"
    p.write_text(yaml.safe_dump(_profile(parent)))
    dump = tmp / f"dump-{name}"
    r = subprocess.run([sys.executable, "-m", "rprof.cli", "run", "--target", f"cgroup:{parent}",
                        "--net", f"docker:{sbx}", "--profile", str(p), "--mode", mode, "--name", name,
                        "--runs-dir", str(tmp / "runs"), "--view-dir", str(tmp / f"view-{name}"), "--",
                        sys.executable, str(ROOT / "examples" / "sql_agent.py"), "--sbx", sbx, "--pg", pg,
                        "--steps", "20", "--dump", str(dump)],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    run_dir = next((tmp / "runs").glob(f"*-{name}"))
    return run_dir, dump, r.stdout


def test_sql_agent_matches_unconstrained(sandbox_pair, tmp_path):
    sbx, pg, parent = sandbox_pair
    base_dir, base_dump, _ = _run(tmp_path, sbx, pg, parent, "measure", "baseline")
    _reset_dbs(sbx, pg)
    run_dir, dump, out = _run(tmp_path, sbx, pg, parent, "enforce", "squeezed")
    print(out)

    # Same end state as the unconstrained run.
    for f in ("sqlite.sql", "pg.sql"):
        assert (dump / f).read_text() == (base_dump / f).read_text(), f
    assert "INSERT INTO public.orders" in (dump / "pg.sql").read_text()

    rep = json.loads((run_dir / "report.json").read_text())
    segs = {s["segment"]: s for s in rep["segments"]}
    # Failures happen only inside fail-knob segments, and some did happen there.
    failed = [c for c in rep["calls"] if c["failed"]]
    assert failed, "the profile should have made some calls fail"
    for c in failed:
        assert set(c["segments"]) & FAIL_SEGMENTS, c
    # The squeezed segments bound.
    for n in (1, 2, 3):
        assert segs[n]["no_effect"] is False, segs[n]
        assert any(k["bound"] for k in segs[n]["knobs"]), segs[n]["knobs"]
    # The baseline (measure mode) wrote no limits and failed nothing.
    base = json.loads((base_dir / "report.json").read_text())
    assert not any(c["failed"] for c in base["calls"])
    assert base["mode"] == "measure"
