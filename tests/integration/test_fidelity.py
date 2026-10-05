"""Fidelity: does tracking see the truth? (design: Testing plan)"""

from __future__ import annotations

import time

import pytest

from conftest import MiB, tol

pytestmark = pytest.mark.integration


def test_mem_tracks(rprof, sandbox):
    base = rprof.samples[-1]["mem"]["current"]
    r = rprof.run("hog-mem 512M 8")
    assert r.exit_code == 0, r.output
    w = rprof.window(r)
    peak = w.max("mem.current") - base
    assert abs(peak - 512 * MiB) <= tol("fidelity", "mem_tracks_rel") * 512 * MiB, peak / MiB
    time.sleep(2.0)
    assert rprof.samples[-1]["mem"]["current"] - base <= tol("fidelity", "mem_release_bytes")


@pytest.mark.timing
def test_cpu_tracks(rprof):
    r = rprof.run("stress-ng --cpu 2 --timeout 10s")
    assert r.exit_code == 0, r.output
    # Trim docker exec start-up and the tail: measure the steady 10 s inside the call.
    w = rprof.window(r)
    s = [x for x in w.samples if r.t0 + 1.0 <= x["t"] <= r.t1 - 1.0]
    cores = (s[-1]["cpu"]["usage_usec"] - s[0]["cpu"]["usage_usec"]) / 1e6 / (s[-1]["t"] - s[0]["t"])
    assert abs(cores - 2.0) <= tol("fidelity", "cpu_tracks_rel") * 2.0, cores


def test_io_tracks(rprof):
    dev = rprof.sess.target.io_device
    r = rprof.run("dd if=/dev/zero of=/var/tmp/dd bs=1M count=512 oflag=direct && rm -f /var/tmp/dd")
    assert r.exit_code == 0, r.output
    w = rprof.window(r)
    wb = w.delta(f"io.{dev}.wbytes")
    assert abs(wb - 512 * MiB) <= tol("fidelity", "io_tracks_rel") * 512 * MiB, wb / MiB


def test_net_tracks(rprof_factory, net_sandbox, netpeer):
    _, peer = netpeer
    rp = rprof_factory(net_sandbox)
    r = rp.run(f"iperf3 -c {peer} -n 200M -J")
    assert r.exit_code == 0, r.output[-500:]
    import json
    d = json.loads(r.output)
    assert "sum_sent" in d["end"], d.get("error")
    sent = d["end"]["sum_sent"]["bytes"]
    w = rp.window(r)
    tx = sum(w.delta(f"net.{k}.tx_bytes") for k in w.samples[-1]["net"] if isinstance(w.samples[-1]["net"][k], dict))
    assert abs(tx - sent) <= tol("fidelity", "net_tracks_rel") * sent, (tx, sent)


def test_pids_tracks(rprof):
    base = rprof.samples[-1]["pids"]["current"]
    r = rprof.run("for i in $(seq 50); do sleep 4 & done; sleep 1")
    assert r.exit_code == 0
    time.sleep(0.3)
    peak = max(s["pids"]["current"] for s in rprof.samples if s["t"] >= r.t0)
    assert abs((peak - base) - 50) <= tol("fidelity", "pids_tracks_abs") + 1, peak - base  # +1: the sh itself


@pytest.mark.timing
def test_alignment(rprof):
    base = rprof.samples[-1]["mem"]["current"]
    r = rprof.run("hog-mem 256M 2")
    rise = next(s for s in rprof.samples if s["t"] >= r.t0 and s["mem"]["current"] - base > 128 * MiB)
    period = 1 / rprof.sess.opts.hz
    slack = tol("fidelity", "alignment_samples") * period + tol("fidelity", "alignment_exec_slack_s")
    assert rise["t"] - r.t0 <= slack, rise["t"] - r.t0


def test_parallel_timeline(rprof):
    import threading
    out = {}

    def call(name, delay, cmd):
        time.sleep(delay)
        out[name] = rprof.run(cmd, call_id=name)
    a = threading.Thread(target=call, args=("hog", 0, "hog-mem 512M 6"))
    b = threading.Thread(target=call, args=("stress", 2, "stress-ng --cpu 1 --timeout 8s"))
    a.start(); b.start(); a.join(); b.join()
    rprof.stop()
    from rprof.report.data import RunData
    from rprof.report.timeline import build_timeline
    rows = [iv for iv in build_timeline(RunData(rprof.run_dir)) if iv["running_calls"]]
    seq = [tuple(c["call_id"] for c in iv["running_calls"]) for iv in rows]
    assert seq == [("hog",), ("hog", "stress"), ("stress",)], seq
    # The interval with only hog-mem shows its memory; the stress-only one shows ~1 core.
    assert rows[0]["usage"]["mem_peak"] > 400 * MiB
    assert 0.7 <= rows[2]["usage"]["cpu_cores_mean"] <= 1.3, rows[2]["usage"]
