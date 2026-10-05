"""Shared helpers for the unit tier (imported by conftest and tests)."""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "profiles" / "examples" / "mem-squeeze-mid.yaml"

FAKE_FILES = {
    "cgroup.controllers": "cpuset cpu io memory pids\n",
    "cgroup.subtree_control": "\n",
    "cgroup.procs": "101\n102\n",
    "cpu.max": "max 100000\n",
    "cpuset.cpus": "\n",
    "memory.high": "max\n",
    "memory.max": "max\n",
    "memory.swap.max": "max\n",
    "io.max": "",
    "pids.max": "max\n",
    "cpu.stat": ("usage_usec 1000000\nuser_usec 600000\nsystem_usec 400000\nnr_periods 10\n"
                 "nr_throttled 2\nthrottled_usec 5000\nnr_bursts 0\nburst_usec 0\n"),
    "memory.current": "104857600\n",
    "memory.stat": "anon 52428800\nfile 41943040\nkernel 1000\npgmajfault 7\n",
    "memory.events": "low 0\nhigh 3\nmax 4\noom 1\noom_kill 1\noom_group_kill 0\n",
    "memory.swap.current": "0\n",
    "io.stat": "259:0 rbytes=4096 wbytes=8192 rios=1 wios=2 dbytes=0 dios=0\n",
    "pids.current": "5\n",
    "pids.events": "max 0\n",
    "cpu.pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=1500\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=700\n",
    "memory.pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=200\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=100\n",
    "io.pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=30\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=10\n",
}


def make_cgroup(root: Path, rel: str = "rprof.slice/sbx", files: dict | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
    (root / "cgroup.subtree_control").write_text("cpuset cpu io memory pids\n")
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    for name, text in (files or FAKE_FILES).items():
        (d / name).write_text(text)
    return d


