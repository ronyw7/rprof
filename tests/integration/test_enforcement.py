"""Enforcement: does each limit do what it claims? (design: Testing plan)"""

from __future__ import annotations

import json
import time

import pytest

from conftest import MiB, Sandbox, tol

pytestmark = pytest.mark.integration


@pytest.mark.timing
def test_cpu_quota(rprof):
    rprof.apply({"cpu.cores": 0.5})
    r = rprof.run("stress-ng --cpu 2 --timeout 8s")
    w = rprof.window(r)
    s = [x for x in w.samples if r.t0 + 1.0 <= x["t"] <= r.t1 - 0.5]
    cores = (s[-1]["cpu"]["usage_usec"] - s[0]["cpu"]["usage_usec"]) / 1e6 / (s[-1]["t"] - s[0]["t"])
    assert abs(cores - 0.5) <= tol("enforcement", "cpu_quota_rel") * 0.5, cores
    assert w.delta("cpu.throttled_usec") > 0


@pytest.mark.timing
def test_cpu_cpuset(rprof):
    rprof.apply({"cpu.cpus": "0"})
    r = rprof.run("stress-ng --cpu 4 --timeout 6s")
    assert rprof.window(r).cores() <= tol("enforcement", "cpuset_max_cores")


def test_mem_high(rprof, sandbox):
    rprof.apply({"mem.high": "256Mi", "mem.swap_max": 0})
    r = rprof.run("timeout 6 hog-mem 512M 5; echo rc=$?", timeout=30)
    w = rprof.window(r)
    assert w.delta("mem.events.high") > 0
    assert w.delta("psi.memory.some_us") > 0
    # The process stalls instead of dying: it is alive when `timeout` stops it (rc 124), or it finished (rc 0).
    assert "rc=124" in r.output or "rc=0" in r.output, r.output
    assert sandbox.is_running()


def test_mem_max_kills_only_the_tool(rprof, sandbox):
    rprof.apply({"mem.max": "256Mi", "mem.swap_max": 0})
    r = rprof.run("hog-mem 512M 5")
    w = rprof.window(r)
    assert r.exit_code == 137
    assert w.delta("mem.events.oom_kill") >= 1
    assert w.max("mem.current") <= 256 * MiB
    assert sandbox.is_running()                     # container PID 1 survived
    assert r.cause == "memory"


@pytest.mark.timing
def test_io_wbps(rprof):
    rprof.apply({"io.wbps": "20Mi"})
    r = rprof.run("dd if=/dev/zero of=/var/tmp/dd bs=1M count=200 oflag=direct; rm -f /var/tmp/dd")
    assert abs(r.duration - 10.0) <= tol("enforcement", "io_wbps_rel") * 10.0, r.duration
    assert rprof.window(r).delta("psi.io.some_us") > 0


def test_io_buffered(rprof):
    """Records whether buffered writes are throttled (cgroup writeback); never fails."""
    rprof.apply({"io.wbps": "20Mi"})
    r = rprof.run("dd if=/dev/zero of=/var/tmp/dd bs=1M count=100; sync; rm -f /var/tmp/dd")
    throttled = r.duration >= 0.6 * 5.0
    print(f"io-buffered: 100 MiB buffered + sync took {r.duration:.1f} s -> throttled={throttled}")


def test_pids(rprof):
    rprof.apply({"pids.max": 20})
    r = rprof.run("for i in $(seq 50); do sleep 3 & done 2>&1; wait", timeout=30)
    w = rprof.window(r)
    assert "Resource temporarily unavailable" in r.output or "fork" in r.output.lower(), r.output[-300:]
    assert w.delta("pids.events_max") > 0
    assert w.max("pids.current") <= 20


@pytest.mark.timing
def test_net_rate(rprof_factory, net_sandbox, netpeer):
    _, peer = netpeer
    rp = rprof_factory(net_sandbox)
    rp.apply({"net.rate": "10mbit"})
    for rev in ("", "-R"):
        r = rp.run(f"iperf3 -c {peer} -t 6 -J {rev}")
        assert r.exit_code == 0, r.output[-300:]
        d = json.loads(r.output)
        assert "sum_received" in d["end"], d.get("error")
        mbit = d["end"]["sum_received"]["bits_per_second"] / 1e6
        assert abs(mbit - 10) <= tol("enforcement", "net_rate_rel") * 10, (rev, mbit)


@pytest.mark.timing
def test_net_loss(rprof_factory, net_sandbox, netpeer):
    _, peer = netpeer
    rp = rprof_factory(net_sandbox)
    rp.apply({"net.loss": "30%"})
    r = rp.run(f"ping -c 200 -i 0.05 -q {peer} | grep -o '[0-9.]*% packet loss'")
    loss = float(r.output.split("%")[0])
    assert abs(loss - 30) <= tol("enforcement", "net_loss_abs_pct"), loss
    time.sleep(1.2)  # netem counters are polled at 1 Hz
    assert rp.samples[-1]["net"]["qdisc_drops"] > 0


def test_net_reject(rprof_factory, net_sandbox, netpeer):
    _, peer = netpeer
    rp = rprof_factory(net_sandbox)
    rp.apply({"net.partition": "reject"})
    r = rp.run(f"curl -s -m 5 -o /dev/null http://{peer}/")
    assert r.exit_code == 7                      # connection refused
    assert r.duration < tol("enforcement", "net_reject_max_s") + 0.5  # + docker exec overhead
    assert r.cause == "network" or r.cause is None  # hits are polled at 1 Hz


def test_net_drop(rprof_factory, net_sandbox, netpeer):
    _, peer = netpeer
    rp = rprof_factory(net_sandbox)
    rp.apply({"net.partition": "drop"})
    r = rp.run(f"curl -s --max-time 5 -o /dev/null http://{peer}/")
    lo, hi = tol("enforcement", "net_drop_timeout_s")
    assert r.exit_code == 28 and lo <= r.duration <= hi, (r.exit_code, r.duration)


def test_disk_capacity(rprof_factory, data_fs):
    sb = Sandbox(f"rprof-it-disk-{int(time.time() * 1000) % 100000}", extra=["-v", f"{data_fs}:/data"])
    try:
        rp = rprof_factory(sb)
        rp.apply({"disk.capacity": "100Mi"})
        r = rp.run("dd if=/dev/zero of=/data/fill bs=1M count=200 2>&1; ls -l /data/fill")
        assert "No space left on device" in r.output, r.output
        size = int(r.output.strip().splitlines()[-1].split()[4])
        assert abs(size - 100 * MiB) <= tol("enforcement", "disk_capacity_rel") * 100 * MiB, size / MiB
        rp.stop()
        assert not (data_fs / ".rprof-ballast").exists()
    finally:
        sb.rm()


def test_apply_latency(rprof_factory, net_sandbox, netpeer):
    rp = rprof_factory(net_sandbox)
    ms = [rp.apply({"cpu.cores": 0.5 if i % 2 else 2}) for i in range(20)]
    p95 = sorted(ms)[int(0.95 * 19)]
    assert p95 < tol("enforcement", "apply_p95_cgroup_ms"), ms
    ms = [rp.apply({"net.delay": "10ms" if i % 2 else "20ms"}) for i in range(20)]
    p95n = sorted(ms)[int(0.95 * 19)]
    print(f"apply p95: cgroup {p95:.2f} ms, net {p95n:.2f} ms")
    assert p95n < tol("enforcement", "apply_p95_net_ms"), ms
