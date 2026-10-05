"""``rprof run``: one root process beside the sandbox.

Setup (resolve, lock, capability gate, snapshot) -> apply t=0 limits -> launch the
command -> sample, schedule and serve harness events until the command exits or
the profile ends -> restore -> write timeline and report.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import logging.handlers
import os
import platform
import queue
import secrets
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any  # noqa: F401  (annotations)

from . import __version__
from . import controllers as C
from . import knobs as K
from .agentview import AgentView
from .clock import RunClock
from .events import ControlHandler, ControlServer, EventLog
from .profile import Profile, load_profile, unlimited_profile
from .profile.validate import managed_knobs, used_knobs
from .protect import Protector
from .sampler import Sampler
from .scheduler import Scheduler
from .snapshot import Snapshot, TargetLock, restore, state_dir
from .target import Target, docker, resolve
from .target.cgroup import Cgroup, cgroup_of_pid, cgroup_root, move_pid
from .util import (MissingCapability, PermissionDenied, RestoreFailed, RprofError, atomic_write, chown_tree,
                   is_root, log, run_cmd, sudo_owner, write_json)

INSIDE_SOCKET_DEFAULT = "/run/rprof-ctl/control.sock"


@dataclass
class RunOptions:
    target: str
    profile: str | None = None
    mode: str = "enforce"
    hz: float = 10.0
    runs_dir: str = "runs"
    name: str | None = None
    harness: str = "outside"
    protect: list[str] = field(default_factory=list)
    net: list[str] = field(default_factory=list)
    io_device: str | None = None
    data_path: str = "/data"
    data_dir: str | None = None
    view_dir: str | None = None
    ctl_dir: str | None = None
    allow_degraded: bool = False
    duration: float | None = None
    command: list[str] = field(default_factory=list)
    self_cgroup: bool = True
    report: bool = True
    capabilities: str | None = None
    quiet: bool = False
    hide_limits: bool = False


def git_sha() -> str | None:
    d = Path(__file__).resolve().parent
    r = run_cmd(["git", "-c", "safe.directory=*", "-C", str(d), "rev-parse", "HEAD"], quiet=True)
    return r.out.strip() if r.ok else None


def host_info() -> dict:
    mem = swap = None
    try:
        for ln in Path("/proc/meminfo").read_text().splitlines():
            if ln.startswith("MemTotal:"):
                mem = int(ln.split()[1]) * 1024
            elif ln.startswith("SwapTotal:"):
                swap = int(ln.split()[1]) * 1024
    except OSError:
        pass
    return {"hostname": socket.gethostname(), "kernel": platform.release(),
            "docker_version": docker.version(), "cgroup_driver": docker.cgroup_driver(),
            "cpus": os.cpu_count(), "mem_bytes": mem, "swap_bytes": swap}


def load_selftest_caps(path: str | None) -> dict | None:
    import json
    p = Path(path) if path else state_dir() / "capabilities.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def new_run_id(runs_dir: Path, label: str) -> str:
    stamp = _dt.datetime.now().strftime("%Y-%m-%dT%H%M")
    base = f"{stamp}-{label}"
    rid, i = base, 2
    while (runs_dir / rid).exists():
        rid = f"{base}-{i}"
        i += 1
    return rid


class RunSession:
    def __init__(self, opts: RunOptions, profile: Profile | None = None):
        self.opts = opts
        self.mode = opts.mode
        if self.mode not in ("enforce", "measure"):
            raise RprofError(f"--mode must be enforce or measure, not {self.mode}", 2)
        if opts.name is not None:
            from .profile import check_name
            problem = check_name(opts.name)
            if problem:
                raise RprofError(f"--name {problem}", 2)
        self.profile = profile or (load_profile(opts.profile) if opts.profile else unlimited_profile())
        self.clock = RunClock()
        self.clock.started = False  # setup events get t = 0 until main() anchors the clock
        self.token: str | None = None
        self.target: Target | None = None
        self.lock: TargetLock | None = None
        self.snap: Snapshot | None = None
        self.events: EventLog = EventLog(None, self.clock)
        self.controllers: list[C.Controller] = []
        self.run_dir: Path | None = None
        self.run_id = ""
        self.end_reason = "error"
        self.exit_code = 0
        self.child: asyncio.subprocess.Process | None = None
        self._read_lock = threading.Lock()
        self._read_fc = C.FileCache()
        self._read_fc.primary = False  # type: ignore[attr-defined]
        self.self_cg: Path | None = None
        self.orig_cg: Path | None = None
        self.restored = False
        self.restore_errors: list[str] = []

    # ------------------------------------------------------------ setup
    def setup(self) -> None:
        o = self.opts
        if self.mode == "enforce" and not is_root():
            raise PermissionDenied("rprof run --mode enforce needs root (cgroup writes, tc, iptables)")
        self.target = resolve(o.target, net_specs=o.net or None, io_device=o.io_device,
                              data_path=o.data_path, data_dir=o.data_dir)
        t = self.target
        if o.harness == "inside":
            from .events import SOCKET_PATH_MAX
            ctl = self._ctl_dir() / "control.sock"
            if len(os.fsencode(str(ctl.absolute()))) > SOCKET_PATH_MAX:
                raise RprofError(f"{ctl} is too long for a Unix socket (limit {SOCKET_PATH_MAX} bytes); "
                                 "use a shorter --ctl-dir", 2)
        runs_dir = Path(o.runs_dir)
        self.created_runs_dir = not runs_dir.exists()
        runs_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir = runs_dir
        self.run_id = new_run_id(runs_dir, o.name or self.profile.name)
        self.lock = TargetLock(t.lock_key, f"run={self.run_id}").acquire()
        self.run_dir = runs_dir / self.run_id
        self.run_dir.mkdir(parents=True)
        self.events = EventLog(self.run_dir / "events.jsonl", self.clock)
        # Every subprocess rprof runs (tc, iptables, nsenter, docker) is logged with its timing.
        # Through a queue: the thread that logs (e.g. the one applying limits) never waits on the
        # run directory's filesystem; a listener thread writes the file.
        file_handler = logging.FileHandler(self.run_dir / "rprof.log")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self._log_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._log_handler = logging.handlers.QueueHandler(self._log_queue)
        self._log_handler.setLevel(logging.DEBUG)
        self._log_listener = logging.handlers.QueueListener(self._log_queue, file_handler)
        self._log_listener.start()
        log.addHandler(self._log_handler)
        log.setLevel(logging.DEBUG)

        unified_files = {k[len(K.UNIFIED_PREFIX):] for k in self.profile.explicit if K.is_unified(k)}
        self.managed = managed_knobs(self.profile)
        used = used_knobs(self.profile)
        # The snapshot records what to undo: limits (enforce mode) and cgroup masks (--hide-limits).
        need_snap = self.mode == "enforce" or o.hide_limits
        self.snap = Snapshot(self.run_dir / "snapshot.json", self.run_id, t.to_meta()) if need_snap else None
        self.controllers = C.build(t, self.snap if self.mode == "enforce" else None, self.run_id, self.events,
                                   unified_files)

        # Controllers missing for the target cgroup (e.g. cpuset/io not delegated): enable them.
        self.enabled_controllers: list[str] = []
        if self.mode == "enforce":
            want = sorted({"cpu" if k.startswith("cpu.") and k != "cpu.cpus" else
                           "cpuset" if k == "cpu.cpus" else
                           "memory" if k.startswith("mem.") else
                           "io" if k.startswith("io.") else
                           "pids" if k.startswith("pids.") else "" for k in used} - {""})
            missing = t.cgroup.missing_controllers(want)
            if missing:
                self.enabled_controllers = t.cgroup.enable_controllers(missing)
                if self.enabled_controllers:
                    self.events.emit("warning", code="controllers_enabled",
                                     message="enabled " + ", ".join(self.enabled_controllers))

        caps = C.capabilities(self.controllers)
        self.selftest = load_selftest_caps(o.capabilities)
        if self.selftest:
            for k, v in (self.selftest.get("knobs") or {}).items():
                if isinstance(v, dict) and v.get("ok") is False and caps.get(k) is None:
                    caps[k] = f"failed selftest: {v.get('detail', '')}".strip()
        self.caps = caps
        failed_fid = sorted(k for k, v in ((self.selftest or {}).get("fidelity") or {}).items()
                            if isinstance(v, dict) and v.get("ok") is False)
        if failed_fid:
            # Recorded numbers may be wrong on this host: say so, but don't refuse to run.
            msg = ("rprof selftest found that measurements are off on this host for: " + ", ".join(failed_fid)
                   + ". See `rprof selftest --only fidelity`.")
            self.events.emit("warning", code="fidelity_failed", message=msg)
            if not o.quiet:
                print(f"rprof: warning: {msg}", file=sys.stderr)
        bad = {k: caps[k] for k in sorted(used) if caps.get(k)}
        if bad and self.mode == "enforce":
            msg = "; ".join(f"{k}: {r}" for k, r in bad.items())
            if not o.allow_degraded:
                raise MissingCapability(f"host cannot enforce knobs this profile uses ({msg}); "
                                        "pass --allow-degraded to run without them")
            self.events.emit("warning", code="degraded", message=msg)
        self.degraded = sorted(bad)
        # Never write knobs the host cannot apply.
        self.managed = {k for k in self.managed if not caps.get(k) or k in ("net.rate",)}

        self._write_meta()
        (self.run_dir / "profile.yaml").write_text(self.profile.dump_yaml())

        if o.self_cgroup and is_root():
            self._enter_self_cgroup()

        if self.snap is not None:
            if self.mode == "enforce":
                for c in self.controllers:
                    c.snapshot(self.managed)
            self.snap.save()
        self.protector = None
        if self.mode == "enforce" and t.kind == "docker" or (self.mode == "enforce" and o.protect):
            self.protector = Protector(t, o.protect, self.snap, self.events)

        # Agent view.
        view_dir = Path(o.view_dir) if o.view_dir else (
            state_dir() / "view" / (t.container_name or t.lock_key))
        self.view_dir = view_dir
        self.view = AgentView(self.profile, view_dir if is_root() or o.view_dir else None,
                              self.run_dir / "agentview")
        try:
            self.view.clear()
        except OSError:
            pass
        if o.harness == "inside":
            self.token = secrets.token_hex(16)
            atomic_write(self.run_dir / "token", self.token + "\n", mode=0o600)
        self.handler = ControlHandler(self)
        self.sampler = Sampler(t, self.controllers, self.clock, o.hz, self.run_dir / "samples.jsonl",
                               state_fn=self._state, self_cgroup=self.self_cg)
        self.sampler.listeners.append(self.handler.on_sample)
        self._protect_wake = threading.Event()
        if self.protector is not None:
            last = {"n": None}

            def _protect_tick(s: dict) -> None:
                n = (s.get("pids") or {}).get("current")
                if n != last["n"]:          # a task started or ended: look for new processes now
                    last["n"] = n
                    self._protect_wake.set()
            self.sampler.listeners.append(_protect_tick)
        self.scheduler = Scheduler(self.profile, self.controllers, self.managed, self.mode, self.clock,
                                   self.events, on_change=self._on_boundary)

    def _ctl_dir(self) -> Path:
        assert self.target is not None
        return Path(self.opts.ctl_dir) if self.opts.ctl_dir else \
            state_dir() / "ctl" / (self.target.container_name or "target")

    def _write_meta(self) -> None:
        o, t = self.opts, self.target
        assert t is not None and self.run_dir is not None
        meta = {
            "run_id": self.run_id, "rprof_version": __version__, "git_sha": git_sha(),
            "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds"),
            "mode": self.mode, "harness": o.harness, "hz": o.hz,
            "host": host_info(), "target": t.to_meta(),
            "profile": {"name": self.profile.name, "path": self.profile.path,
                        "visibility": self.profile.visibility, "segments": len(self.profile.segments)},
            "managed_knobs": sorted(self.managed), "degraded_knobs": self.degraded,
            "knob_capabilities": self.caps, "controllers_enabled": self.enabled_controllers,
            "capabilities": self.selftest, "command": o.command, "protect": o.protect,
            "rprof_cgroup": None, "features": {"memory_peak_reset": None}, "hide_limits": o.hide_limits,
        }
        self.meta = meta
        write_json(self.run_dir / "meta.json", meta)

    def _enter_self_cgroup(self) -> None:
        try:
            self.orig_cg = cgroup_of_pid(os.getpid())
            cg = cgroup_root() / "rprof.slice" / f"rprof-run-{os.getpid()}"
            cg.mkdir(parents=True, exist_ok=True)
            move_pid(cg, os.getpid())
            self.self_cg = cg
        except (OSError, RprofError) as e:
            self.events.emit("warning", code="self_cgroup", message=f"could not move rprof into rprof.slice: {e}")
            self.self_cg = None

    def _leave_self_cgroup(self) -> None:
        if self.self_cg is None:
            return
        try:
            if self.orig_cg is not None and Cgroup(self.orig_cg).exists():
                move_pid(self.orig_cg, os.getpid())
            os.rmdir(self.self_cg)
        except OSError:
            pass

    # ------------------------------------------------------------ helpers
    def _state(self, t: float) -> tuple[int, list[str]]:
        seg, _ = self.profile.segment_at(t)
        return seg, self.handler.running_ids()

    def _on_boundary(self, t: float) -> None:
        try:
            self.view.write(self.clock.now())
        except OSError as e:
            log.debug("agent view write failed: %s", e)

    def refresh_slow_counters(self) -> None:
        """Poll tc/iptables counters now (used before attributing a failed call)."""
        for c in self.controllers:
            if c.name == "net":
                try:
                    c.slow_poll()
                except Exception:  # noqa: BLE001
                    pass

    def read_now(self) -> dict:
        """Fresh counters for tool_start/tool_end (never resets memory.peak)."""
        with self._read_lock:
            out: dict = {}
            for c in self.controllers:
                try:
                    c.sample(out, self._read_fc)
                except Exception:  # noqa: BLE001
                    pass
            from .target.cgroup import parse_psi
            for name, f in (("cpu", "cpu.pressure"), ("memory", "memory.pressure"), ("io", "io.pressure")):
                assert self.target is not None
                txt = self._read_fc.read(self.target.cgroup.file(f))
                if txt:
                    out.setdefault("psi", {})[name] = parse_psi(txt)
            return out

    # ------------------------------------------------------------ main loop
    async def main(self) -> int:
        o = self.opts
        assert self.run_dir is not None and self.target is not None
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        self.stop = stop
        self.loop = loop
        server = ControlServer(self.handler)
        await server.listen(self.run_dir / "control.sock")
        if o.hide_limits and not await loop.run_in_executor(None, self._hide_limits):
            await server.close()
            return self.exit_code
        if o.harness == "inside":
            ctl = self._ctl_dir()
            await server.listen(ctl / "control.sock", inside=True)
            self.events.emit("inside_socket", path=str(ctl / "control.sock"))

        def on_signal(sig):
            if self.end_reason != "signal":
                self.end_reason = "signal"
                self.events.emit("signal", signal=signal.Signals(sig).name)
            if self.child and self.child.returncode is None and sig == signal.SIGTERM:
                try:
                    self.child.send_signal(sig)
                except ProcessLookupError:
                    pass
            stop.set()
        main_thread = threading.current_thread() is threading.main_thread()
        if main_thread:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, on_signal, sig)

        self.clock.start()
        self.events.emit("run_start", t=0.0, mode=self.mode, profile_name=self.profile.name,
                         run_id=self.run_id, harness=o.harness, hz=o.hz)
        # t = 0: apply the initial limits before anything runs.
        await loop.run_in_executor(self.scheduler.executor, self.scheduler.apply_at, 0.0)
        if self.protector:
            await loop.run_in_executor(None, self.protector.scan)
        await loop.run_in_executor(None, self.view.write, 0.0)
        sampler_stop = threading.Event()
        sampler_thread = threading.Thread(target=self.sampler.run_blocking, args=(sampler_stop,),
                                          name="rprof-sampler", daemon=True)
        sampler_thread.start()
        self._sampler_thread = sampler_thread
        protect_thread = None
        if self.protector is not None:
            protect_thread = threading.Thread(target=self._protect_loop, args=(sampler_stop,),
                                              name="rprof-protect", daemon=True)
            protect_thread.start()
        tasks = [asyncio.create_task(self.scheduler.run(stop), name="scheduler"),
                 asyncio.create_task(self._slow_loop(stop), name="slow")]
        mem = next((c for c in self.controllers if c.name == "memory"), None)

        waiters = []
        if o.command:
            env = dict(os.environ)
            env["RPROF_RUN"] = str(self.run_dir.resolve())
            if self.token:
                env["RPROF_HARNESS_TOKEN"] = self.token
                env.setdefault("RPROF_SOCKET", INSIDE_SOCKET_DEFAULT)
            try:
                self.child = await asyncio.create_subprocess_exec(*o.command, env=env)
            except OSError as e:
                self.events.emit("error", code="command_failed", message=f"{o.command[0]}: {e.strerror}")
                self.end_reason = "error"
                self.exit_code = 127
                stop.set()
            else:
                if self.self_cg is not None and self.orig_cg is not None:
                    try:  # the harness is not rprof: keep it out of rprof.slice
                        move_pid(self.orig_cg, self.child.pid)
                    except OSError:
                        pass
                self.events.emit("command_start", pid=self.child.pid, argv=o.command)
                waiters.append(asyncio.create_task(self._wait_child(stop)))
        # Without a command the run records until the target exits, --duration or a signal: the
        # profile's defaults stay in force after its last segment.
        end_t = o.duration
        if end_t is not None:
            waiters.append(asyncio.create_task(self._wait_until(end_t, stop)))
        waiters.append(asyncio.create_task(self._watch_target(stop)))

        await stop.wait()
        for w in waiters:
            w.cancel()
        if self.child and self.child.returncode is None:
            await self._stop_child()
        for tsk in tasks:
            try:
                await asyncio.wait_for(tsk, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                tsk.cancel()
        sampler_stop.set()
        self._protect_wake.set()
        await loop.run_in_executor(None, sampler_thread.join, 10)
        if protect_thread is not None:
            await loop.run_in_executor(None, protect_thread.join, 10)
        if sampler_thread.is_alive():
            # Stuck in a read: a final tick from here would share its file cache. Skip it.
            self.events.emit("warning", code="sampler_stuck",
                             message="the sampling thread did not stop within 10 s; no final sample")
        else:
            await loop.run_in_executor(None, self.sampler.tick)  # final sample
        try:
            # The run dir keeps the last view the agent saw.
            await loop.run_in_executor(None, lambda: self.view.write(self.clock.now(), force=True))
        except OSError:
            pass
        if mem is not None:
            self.meta["features"]["memory_peak_reset"] = getattr(mem, "peak_resettable", None)
        await server.close()
        if main_thread:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
        return self.exit_code

    def _hide_limits(self) -> bool:
        """Mask /sys/fs/cgroup in every mount namespace of the target; False (and exit 73) on failure."""
        from . import mask
        assert self.target is not None and self.snap is not None
        masked = []
        for ns, pid in mask.namespaces(self.target.pids()).items():
            entry = {"pid": pid, "ns": ns}
            self.snap.add_mask(entry)          # recorded first, so a crash can still be undone
            err = mask.mask(pid)
            if err:
                self.events.emit("error", code="hide_limits_failed", message=f"pid {pid}: {err}")
                self.end_reason, self.exit_code = "error", MissingCapability.exit_code
                return False
            masked.append(entry)
            self.events.emit("limits_hidden", pid=pid, path=mask.MASK_POINT)
        if not masked:
            self.events.emit("warning", code="hide_limits_nothing",
                             message="no container mount namespace found in the target; nothing hidden")
        self.meta["hidden_limits"] = masked
        return True

    # ------------------------------------------------------------ in-process control (tests, embedding)
    def request_stop(self, reason: str = "profile_end") -> None:
        """Thread-safe: end the run as if the profile had ended."""
        loop = getattr(self, "loop", None)
        if loop is None:
            return
        def _stop():
            if self.end_reason not in ("signal", "command_exit"):
                self.end_reason = reason
            self.stop.set()
        try:
            loop.call_soon_threadsafe(_stop)
        except RuntimeError:
            pass  # the run already ended on its own

    def apply_now(self, values: dict[str, Any]) -> tuple[list[str], float]:
        """Apply canonical knob values immediately, outside the schedule; returns (errors, ms)."""
        import time as _t
        def _do():
            t0 = _t.perf_counter()
            errs = self.scheduler.apply_knobs(values)
            ms = (_t.perf_counter() - t0) * 1e3
            self.events.emit("manual_apply", values={k: v for k, v in values.items()}, apply_ms=round(ms, 3),
                             errors=errs)
            return errs, ms
        return self.scheduler.executor.submit(_do).result()

    async def _wait_child(self, stop: asyncio.Event) -> None:
        assert self.child is not None
        rc = await self.child.wait()
        self.exit_code = rc if rc >= 0 else 128 - rc
        self.events.emit("command_exit", exit_code=rc)
        if self.end_reason not in ("signal",):
            self.end_reason = "command_exit"
        stop.set()

    async def _wait_until(self, t_end: float, stop: asyncio.Event) -> None:
        delay = t_end - self.clock.now()
        if delay > 0:
            await asyncio.sleep(delay)
        if self.end_reason not in ("signal", "command_exit"):
            self.end_reason = "profile_end" if self.opts.duration is None else "duration"
        if self.child and self.child.returncode is None:
            self.events.emit("warning", code="command_still_running", message="stopping command at end of --duration")
        stop.set()

    async def _watch_target(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            await asyncio.sleep(1.0)
            if self.target is not None and not await loop.run_in_executor(None, self.target.cgroup.exists):
                # The container exited: a normal end. Its limits went with it, so nothing is restored.
                self.events.emit("target_exit", cgroup=self.target.cgroup.path)
                if self.end_reason not in ("signal", "command_exit"):
                    self.end_reason = "target_exit"
                stop.set()

    async def _stop_child(self) -> None:
        assert self.child is not None
        for sig, wait in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
            try:
                self.child.send_signal(sig)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(self.child.wait(), wait)
                rc = self.child.returncode or 0
                self.exit_code = rc if rc >= 0 else 128 - rc
                return
            except asyncio.TimeoutError:
                continue

    async def _slow_loop(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        net = [c for c in self.controllers if c.name == "net"]
        while not stop.is_set():
            await loop.run_in_executor(None, self.sampler.poll_host)
            for c in net:
                await loop.run_in_executor(None, c.slow_poll)
            try:
                # File writes never run on the event loop: under heavy writeback they can block.
                await loop.run_in_executor(None, self.view.write, self.clock.now())
            except OSError:
                pass
            try:
                await asyncio.wait_for(stop.wait(), 1.0)
            except asyncio.TimeoutError:
                pass

    def close_log(self) -> None:
        """Detach and flush rprof.log (idempotent)."""
        if getattr(self, "_log_handler", None) is not None:
            log.removeHandler(self._log_handler)
            self._log_listener.stop()          # flushes what is queued
            for h in self._log_listener.handlers:
                h.close()
            self._log_handler = None

    def _protect_loop(self, stop: threading.Event) -> None:
        """Protection scans, off the sampling thread: protecting a process writes snapshot.json,
        which must not hold up samples if the run directory's filesystem stalls."""
        prot = self.protector
        assert prot is not None
        last_full = time.monotonic()
        while not stop.is_set():
            woken = self._protect_wake.wait(timeout=1.0)
            self._protect_wake.clear()
            if stop.is_set():
                break
            try:
                if time.monotonic() - last_full >= 1.0:   # full rescan once a second
                    prot.scan()
                    last_full = time.monotonic()
                elif woken:                               # new processes since the last look
                    prot.scan(new_only=True)
            except Exception:  # noqa: BLE001
                log.exception("protect scan failed")

    # ------------------------------------------------------------ teardown
    def teardown(self) -> int:
        """Restore, report and release; returns the final exit code."""
        code = self.exit_code
        if self.snap is not None and not self.restored:
            self.restore_errors = restore(self.snap)
            self.restored = True
            if self.restore_errors:
                self.events.emit("error", code="restore_failed", message="; ".join(self.restore_errors))
                code = RestoreFailed.exit_code
        if hasattr(self, "scheduler"):
            self.scheduler.shutdown()
        t_end = self.clock.now()
        self.events.emit("run_end", t=t_end, reason=self.end_reason, exit_code=self.exit_code,
                         restore_errors=self.restore_errors,
                         apply_ms_p95=_p95(getattr(self, "scheduler", None) and self.scheduler.apply_ms),
                         sampler_overruns=getattr(getattr(self, "sampler", None), "overruns", None))
        write_errors: dict[str, str] = {}
        if hasattr(self, "sampler"):
            th = getattr(self, "_sampler_thread", None)
            err = self.sampler.close(sampling_thread_alive=bool(th and th.is_alive()))
            if err:
                write_errors["samples.jsonl"] = err
                self.events.emit("error", code="write_failed", message=err)
        self._read_fc.close()
        err = self.events.close()
        if err:
            write_errors["events.jsonl"] = err
        for e in write_errors.values():
            print(f"rprof: {e}", file=sys.stderr)
        if self.run_dir is not None and hasattr(self, "meta"):
            self.meta["write_errors"] = write_errors
            self.meta["ended_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")
            self.meta["duration_s"] = round(t_end, 3)
            self.meta["end_reason"] = self.end_reason
            self.meta["rprof_cgroup"] = str(self.self_cg) if self.self_cg else None
            write_json(self.run_dir / "meta.json", self.meta)
            if self.opts.report:
                try:
                    from .report.report import write_reports
                    write_reports(self.run_dir)
                except Exception as e:  # noqa: BLE001
                    log.warning("report failed: %s", e)
                    if not self.opts.quiet:
                        print(f"rprof: report failed: {e}")
            if self.token is None:
                try:
                    (self.run_dir / "control.sock").unlink()
                except OSError:
                    pass
            owner = sudo_owner()
            if owner and is_root():
                chown_tree(self.run_dir, *owner)
                if getattr(self, "created_runs_dir", False):  # a runs dir rprof made belongs to the user too
                    try:
                        os.chown(self.runs_dir, *owner)
                    except OSError:
                        pass
        self._leave_self_cgroup()
        self.close_log()
        if self.lock:
            self.lock.release()
        return code


def _p95(xs) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def run(opts: RunOptions, profile: Profile | None = None) -> tuple[int, RunSession]:
    sess = RunSession(opts, profile)
    try:
        sess.setup()
    except BaseException:
        # Nothing was written yet except possibly enabled controllers; release what we hold.
        if sess.lock:
            sess.lock.release()
        sess.events.close()
        sess.close_log()
        raise
    code = 70
    try:
        code = asyncio.run(sess.main())
    except BaseException as e:  # noqa: BLE001
        sess.end_reason = "error"
        sess.events.emit("error", code="internal", message=f"{type(e).__name__}: {e}")
        sess.exit_code = 70
        if not isinstance(e, Exception):
            raise
    finally:
        code = sess.teardown()
    return code, sess


__all__ = ["RunOptions", "RunSession", "run", "managed_knobs", "used_knobs"]
