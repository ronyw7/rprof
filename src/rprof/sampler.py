"""Sampler: raw counters at a fixed rate into ``samples.jsonl`` (design Appendix B)."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Callable

from .controllers import Controller, FileCache
from .target import Target
from .target.cgroup import parse_flat, parse_psi
from .util import JsonlWriter, log

PSI_FILES = (("cpu", "cpu.pressure"), ("memory", "memory.pressure"), ("io", "io.pressure"))


class Sampler:
    def __init__(self, target: Target, controllers: list[Controller], clock, hz: float = 10.0,
                 out: Path | None = None, state_fn: Callable[[float], tuple[int, list[str]]] | None = None,
                 self_cgroup: Path | None = None):
        if not 1 <= hz <= 100:
            raise ValueError("--hz must be between 1 and 100")
        self.target = target
        self.controllers = controllers
        self.clock = clock
        self.hz = hz
        self.period = 1.0 / hz
        self.state_fn = state_fn
        self.self_cgroup = self_cgroup
        self._self_stat = str(self_cgroup / "cpu.stat") if self_cgroup is not None else ""
        self.fc = FileCache()          # the sampling thread's own
        self.host_fc = FileCache()     # poll_host runs in another thread: never share a cache
        self.writer = JsonlWriter(out) if out else None
        self.listeners: list[Callable[[dict], None]] = []
        self.last: dict | None = None
        self.count = 0
        self.overruns = 0
        self.target_gone = False
        self.host_psi: dict = {}
        self.secondary_every = max(1, round(hz / 20))
        self._n = 0
        self.poll_host()

    def read(self, t: float | None = None) -> dict:
        """One sample of every metric (no labels, nothing written)."""
        s: dict = {}
        # Above 20 Hz, PSI and net-device counters are read every Nth tick (about 20 Hz) and left out
        # of the other lines: they are counters, so this loses timing detail, never usage.
        secondary = self._n % self.secondary_every == 0
        self._n += 1
        for c in self.controllers:
            if c.name == "net" and not secondary:
                continue
            try:
                c.sample(s, self.fc)
            except Exception:  # noqa: BLE001  a metric that fails to parse is omitted
                pass
        cg = self.target.cgroup
        if secondary:
            psi = {}
            for name, f in PSI_FILES:
                txt = self.fc.read(cg.fstr(f))
                if txt:
                    psi[name] = parse_psi(txt)
            if psi:
                s["psi"] = psi
        if self.host_psi:
            s["host"] = {"psi": self.host_psi}
        if self.self_cgroup is not None:
            txt = self.fc.read(self._self_stat)
            if txt:
                s["self"] = {"cpu": {"usage_usec": parse_flat(txt).get("usage_usec", 0)}}
        return s

    def poll_host(self) -> None:
        """Host-wide PSI (1 Hz: it only flags runs disturbed by other load, and it is a counter)."""
        host = {}
        for name, _ in PSI_FILES:
            txt = self.host_fc.read(f"/proc/pressure/{name}")
            if txt:
                host[name] = parse_psi(txt)
        self.host_psi = host

    def tick(self) -> dict:
        t = self.clock.now()
        body = self.read(t)
        seg, calls = self.state_fn(t) if self.state_fn else (0, [])
        s = {"t": round(t, 4), "t_wall": self.clock.wall_iso(t), "segment": seg, "running_calls": calls}
        s.update(body)
        if "cpu" not in s and "mem" not in s:
            self.target_gone = True
        self.last = s
        self.count += 1
        if self.writer:
            self.writer.write(s)
        for fn in self.listeners:
            try:
                fn(s)
            except Exception:  # noqa: BLE001  one bad listener must not stop sampling
                log.exception("sample listener failed")
        return s

    def run_blocking(self, stop: threading.Event) -> None:
        """Sample until ``stop`` is set, in the calling thread.

        ``rprof run`` samples from its own thread, so a read that blocks (a cgroup file, or
        /proc/<pid>/cmdline of a process stuck in reclaim) can delay samples but never the
        event loop that serves the control socket.
        """
        nxt = time.monotonic()
        while not stop.is_set():
            self.tick()
            nxt += self.period
            delay = nxt - time.monotonic()
            if delay < 0:
                self.overruns += 1
                nxt = time.monotonic()  # fell behind: re-anchor instead of bursting
                delay = 0
            stop.wait(delay)

    async def run(self, stop: asyncio.Event) -> None:
        nxt = time.monotonic()
        while not stop.is_set():
            self.tick()
            nxt += self.period
            delay = nxt - time.monotonic()
            if delay < 0:
                self.overruns += 1
                nxt = time.monotonic()  # fell behind: re-anchor instead of bursting
                delay = 0
            await asyncio.sleep(delay)

    def close(self, sampling_thread_alive: bool = False) -> str | None:
        """Flush samples.jsonl and release files; returns a write error, if any.

        If the sampling thread is still alive (stuck in a read), its file descriptors are left
        open: closing them under it could hand a reused descriptor to another file.
        """
        err = self.writer.close() if self.writer else None
        self.host_fc.close()
        if sampling_thread_alive:
            return err
        self.fc.close()
        for c in self.controllers:
            if hasattr(c, "close"):
                try:
                    c.close()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
        return err
