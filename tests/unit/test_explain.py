from rprof.explain import attribute, counters, explain_text, throttle_fractions
from rprof.profile import profile_from_dict

BASE = {"oom_kill": 0, "mem_max": 0, "pids_max": 0, "qdisc_drops": 0, "partition_hits": 0,
        "disk_free": 10**9, "throttled_usec": 0, "psi_cpu": 0, "psi_mem": 0, "psi_io": 0}


def end(**kw):
    d = dict(BASE)
    d.update(kw)
    return d


def test_success_has_no_cause():
    assert attribute(BASE, end(pids_max=1, qdisc_drops=3), 0, 1.0, 0, False) == (None, {})


def test_oom_kill_reported_even_when_call_exits_0():
    # `python sort.py; sha256sum out`: the killed step's status is hidden by the last command's.
    assert attribute(BASE, end(oom_kill=1), None, 1.0, 0, False) == ("memory", {"oom_kill": 1, "exited_ok": True})
    assert attribute(BASE, end(oom_kill=2), None, 1.0, 137, False) == ("memory", {"oom_kill": 2})


def test_oom_reported_by_the_program():
    out = "loading...\nError: Out of Memory Error: failed to allocate block of 262144 bytes\n"
    cause, ev = attribute(BASE, BASE, None, 1.0, 1, False, output=out, memory_limited=True)
    assert cause == "memory"
    assert ev == {"output_match": "Error: Out of Memory Error: failed to allocate block of 262144 bytes"}
    for line in ("MemoryError", "malloc: Cannot allocate memory", "terminate called after throwing an instance "
                 "of 'std::bad_alloc'", "java.lang.OutOfMemoryError: Java heap space", "fatal: out of memory"):
        assert attribute(BASE, BASE, None, 1.0, 1, False, output=line, memory_limited=True)[0] == "memory", line


def test_oom_output_only_counts_for_failed_calls_under_a_memory_limit():
    out = "MemoryError"
    assert attribute(BASE, BASE, None, 1.0, 0, False, output=out, memory_limited=True) == (None, {})
    assert attribute(BASE, BASE, None, 1.0, 1, False, output=out, memory_limited=False) == (None, {})
    assert attribute(BASE, BASE, None, 1.0, 1, False, output="all good", memory_limited=True) == (None, {})
    # Kernel evidence of a fork failure wins over a message in the output.
    assert attribute(BASE, end(pids_max=1), None, 1.0, 1, False, output=out, memory_limited=True)[0] == "pids"


def test_explain_texts_for_new_memory_cases():
    p = profile_from_dict({"version": 1, "name": "x", "visibility": "none",
                           "segments": [{"from": 0, "to": 60, "mem": {"max": "1Gi"}}]})
    lim = p.limits_at(10)
    t = explain_text("memory", {"oom_kill": 1, "exited_ok": True}, lim, p, 10, None)
    assert t == "A process was killed: memory limit 1 GiB reached, but the call exited 0."
    t = explain_text("memory", {"output_match": "Out of Memory Error: failed to allocate"}, lim, p, 10, None)
    assert t == 'Failed: out of memory under the memory limit 1 GiB (the program reported "Out of Memory Error: failed to allocate").'


def test_priorities():
    assert attribute(BASE, end(oom_kill=1, pids_max=2, qdisc_drops=5), 0, 1.0, 137, False)[0] == "memory"
    assert attribute(BASE, end(pids_max=2, qdisc_drops=5), 0, 1.0, 1, False)[0] == "pids"
    assert attribute(BASE, end(qdisc_drops=5), 100, 1.0, 1, False)[0] == "disk"
    assert attribute(BASE, end(qdisc_drops=5), None, 1.0, 1, True)[0] == "network"
    assert attribute(BASE, end(partition_hits=1), None, 1.0, 7, False) == ("network", {"partition_hits": 1})
    c, ev = attribute(BASE, end(throttled_usec=800_000), None, 1.0, None, True)
    assert c == "deadline" and ev["cpu"] == 0.8
    assert attribute(BASE, BASE, None, 1.0, 1, False) == (None, {})


def test_counters_extraction_missing_is_nan():
    c = counters({"mem": {"events": {"oom_kill": 2}}, "pids": {"events_max": 1}})
    assert c["oom_kill"] == 2 and c["pids_max"] == 1
    assert c["disk_free"] != c["disk_free"]  # NaN
    # NaN never "rises"
    assert attribute(counters({}), counters({}), None, 1.0, 1, False) == (None, {})


def test_throttle_fractions():
    f = throttle_fractions(BASE, end(psi_io=500_000, psi_mem=100_000), 1.0)
    assert f == {"cpu": 0.0, "memory": 0.1, "io": 0.5}


def test_explain_text(example):
    lim = example.limits_at(84)
    assert explain_text("memory", {}, lim, example, 84, None) == \
        "Killed: memory limit 1 GiB reached (segment 1, 60–120 s)."
    assert explain_text("pids", {}, lim, example, 84, None) == \
        "Failed: process limit of 16 reached (fork failed) (segment 1, 60–120 s)."
    assert explain_text("network", {}, example.limits_at(130), example, 130, None) == \
        "Failed: network loss 30% in effect (segment 2, 120–150 s)."
    assert explain_text("network", {}, example.limits_at(160), example, 160, None).startswith(
        "Failed: network blocked (reject)")
    txt = explain_text("deadline", {"cpu": 0.6, "memory": 0.0, "io": 0.0}, example.limits_at(200), example, 200, 30)
    assert txt.startswith("Timed out after 30 s: cpu was the most throttled resource (60% of the call")
    assert "cpu 0.5 cores" in txt
    assert explain_text(None, {}, lim, example, 84, None) is None


def test_explain_without_full_visibility():
    p = profile_from_dict({"version": 1, "name": "x", "visibility": "current",
                           "segments": [{"from": 0, "to": 9, "disk": {"capacity": "100Mi"}}]})
    assert explain_text("disk", {}, p.limits_at(1), p, 1, None) == \
        "Failed: disk capacity 100 MiB reached (no space left on device)."
