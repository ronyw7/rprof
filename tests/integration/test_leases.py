"""Leases on a real container: the agent's `lease` command raises and lowers its cgroup limits."""

from __future__ import annotations

import json
import time

MiB = 1 << 20
GiB = 1 << 30

PROFILE = {"version": 1, "name": "lease-it", "visibility": "full",
           "defaults": {"cpu": {"cores": 1}, "mem": {"max": "256Mi", "swap_max": 0}},
           "leases": {"max": {"cpu": {"cores": 2}, "mem": {"max": "1Gi"}}, "max_duration": 60, "grace": 2}}


def cg(r, f: str) -> str:
    return r.sess.target.cgroup.file(f).read_text().strip()


def lease(sb, args: str) -> tuple[int | None, str]:
    rc, out, _, _ = sb.exec(f"lease {args}", timeout=40)
    return rc, out


def test_leases_raise_and_lower_the_containers_limits(rprof_factory, sandbox):
    r = rprof_factory(sandbox, PROFILE)
    assert cg(r, "memory.max") == str(256 * MiB) and cg(r, "cpu.max").startswith("100000")
    rc, out = lease(sandbox, "status")
    assert rc == 0 and "Baseline: 1 CPU core and 256 MiB of memory" in out, out

    # Above the baseline without a lease: killed.
    rc, out, _, _ = sandbox.exec("hog-mem 512M 1")
    assert rc != 0

    rc, out = lease(sandbox, "request --cpus 2 --mem 1G --for 30s")
    assert rc == 0 and out.startswith("L1 granted: 2 CPU cores and 1 GiB of memory"), out
    assert cg(r, "memory.max") == str(GiB) and cg(r, "memory.high") == "max"
    assert cg(r, "cpu.max").startswith("200000")
    rc, out, _, _ = sandbox.exec("hog-mem 512M 1")
    assert rc == 0, out                                     # fits under the lease

    rc, out = lease(sandbox, "release")
    assert rc == 0 and "L1 released" in out
    assert cg(r, "cpu.max").startswith("100000") and cg(r, "memory.high") == str(256 * MiB)
    time.sleep(3)                                           # memory.max follows after the grace period
    assert cg(r, "memory.max") == str(256 * MiB) and cg(r, "memory.high") == "max"

    rc, out = lease(sandbox, "request --mem 4G")
    assert rc == 0 and "the most you can hold is 1 GiB" in out
    rc, out = lease(sandbox, "request --mem plenty")
    assert rc == 1 and "is not a size" in out

    r.stop()
    rec = json.loads((r.run_dir / "leases.json").read_text())
    assert [x["reason"] for x in rec["leases"]] == ["released", "run_end"]
    kinds = [e["type"] for e in r.events() if e["type"].startswith("lease_")]
    assert kinds[0] == "lease_installed" and kinds.count("lease_granted") == 2 and "lease_refused" in kinds


def test_memory_still_held_when_a_lease_expires_is_killed_after_the_grace_period(rprof_factory, sandbox):
    p = json.loads(json.dumps(PROFILE))
    p["leases"]["grace"] = 1
    r = rprof_factory(sandbox, p)
    rc, out = lease(sandbox, "request --mem 1G --for 3s")
    assert rc == 0, out
    # Holds 512 MiB for 10 s: past the lease's end and its grace period.
    rc, out, dur, _ = sandbox.exec("hog-mem 512M 10", timeout=30)
    assert rc != 0 and dur < 9, (rc, out, dur)
    assert sandbox.is_running()
    r.stop()
    assert any(e["type"] == "lease_ended" and e["reason"] == "expired" for e in r.events())
