"""Event log (``events.jsonl``) and the control-socket server (design Appendix A.2).

The server speaks newline-delimited JSON over Unix sockets: one reply per request,
in order per connection, many connections at once. Harness events label samples
and fetch the agent's view; they never change limits.
"""

from __future__ import annotations

import asyncio
import grp
import json
import math
import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from . import knobs as K
from .controllers.memory import non_reclaimable
from .explain import attribute, counters, explain_text
from .util import JsonlWriter, RprofError, log

if TYPE_CHECKING:
    from .runner import RunSession

MAX_LINE = 1 << 20


class EventLog:
    def __init__(self, path: Path | None, clock):
        self.clock = clock
        self.w = JsonlWriter(path) if path else None
        self.lock = threading.Lock()
        self.listeners: list = []

    def emit(self, type: str, t: float | None = None, **fields) -> dict:
        ev = {"t": round(self.clock.now() if t is None else t, 4), "type": type}
        if self.w is None and not self.listeners:
            return ev
        ev.update(fields)
        if self.w:
            self.w.write(ev, flush=True)
        for fn in self.listeners:
            try:
                fn(ev)
            except Exception:  # noqa: BLE001
                pass
        return ev

    def close(self) -> str | None:
        """Returns a write error for events.jsonl, if any."""
        return self.w.close() if self.w else None


@dataclass
class Call:
    call_id: str
    cmd: str
    step: int | None
    t0: float
    start: dict
    min_free: float | None = None
    meta: dict = field(default_factory=dict)
    mem_peak: float | None = None              # memory.current / memory.peak, page cache included
    mem_peak_nonreclaimable: float | None = None
    memory_limited: bool = False
    lifetime_peak0: float | None = None        # the container's highest memory ever, at the call's start

    def see_memory(self, sample: dict) -> None:
        total, nr = memory_points(sample)
        if total is not None:
            self.mem_peak = total if self.mem_peak is None else max(self.mem_peak, total)
        if nr is not None:
            self.mem_peak_nonreclaimable = nr if self.mem_peak_nonreclaimable is None \
                else max(self.mem_peak_nonreclaimable, nr)


def memory_points(sample: dict) -> tuple[float | None, float | None]:
    """(total, non-reclaimable) memory in a sample; either may be None."""
    m = sample.get("mem")
    if not isinstance(m, dict) or "current" not in m:
        return None, None
    cur = m["current"]
    total = max(cur, m.get("peak", cur))
    nr = non_reclaimable(cur, m.get("file"), m.get("shmem")) if "file" in m else None
    return total, nr


class ProtocolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _req_str(req: dict, k: str, optional: bool = False) -> str | None:
    v = req.get(k)
    if v is None and optional:
        return None
    if not isinstance(v, str) or not v:
        raise ProtocolError("bad_request", f"{k} must be a non-empty string")
    return v


def _memory_limited(run, lim: dict) -> bool:
    """True when rprof is enforcing a memory limit (what output matching requires)."""
    return getattr(run, "mode", "enforce") == "enforce" and (
        lim.get("mem.max") is not None or lim.get("mem.high") is not None)


def _int(x: float | None) -> int | None:
    return None if x is None else int(x)


class ControlHandler:
    """Request dispatch; shared by every socket of a run."""

    def __init__(self, run: "RunSession"):
        self.run = run
        self.running: dict[str, Call] = {}
        self.seen: set[str] = set()
        self.lock = threading.Lock()

    # Called by the sampler every tick.
    def on_sample(self, sample: dict) -> None:
        free = sample.get("disk", {}).get("free_bytes")
        with self.lock:
            for c in self.running.values():
                if free is not None:
                    c.min_free = free if c.min_free is None else min(c.min_free, free)
                c.see_memory(sample)

    def running_ids(self) -> list[str]:
        with self.lock:
            return list(self.running)

    def running_calls(self) -> list[Call]:
        with self.lock:
            return list(self.running.values())

    def handle(self, req: Any, inside: bool = False) -> dict:
        rid = req.get("id") if isinstance(req, dict) else None
        try:
            if not isinstance(req, dict):
                raise ProtocolError("bad_request", "request must be a JSON object")
            if inside and not secrets.compare_digest(str(req.get("token", "")), self.run.token or "\0"):
                raise ProtocolError("auth_required", "missing or wrong token")
            typ = req.get("type")
            fn = getattr(self, f"_h_{typ}", None) if isinstance(typ, str) else None
            if fn is None:
                raise ProtocolError("bad_request", f"unknown type {typ!r}")
            out = {"id": rid, "ok": True}
            out.update(fn(req))
            return out
        except ProtocolError as e:
            return {"id": rid, "ok": False, "error": {"code": e.code, "message": str(e)}}
        except Exception as e:  # noqa: BLE001
            log.exception("control request failed")
            self.run.events.emit("error", code="internal", message=f"{type(e).__name__}: {e}")
            return {"id": rid, "ok": False, "error": {"code": "internal", "message": str(e)}}

    # ------------------------------------------------------------ handlers
    def _h_hello(self, req):
        r = self.run
        return {"rprof_version": __version__, "run_id": r.run_id, "mode": r.mode,
                "visibility": r.profile.visibility, "t": round(r.clock.now(), 4)}

    def _h_tool_start(self, req):
        r = self.run
        call_id = _req_str(req, "call_id")
        cmd = req.get("cmd", "")
        if not isinstance(cmd, str):
            raise ProtocolError("bad_request", "cmd must be a string")
        step = req.get("step")
        if step is not None and (not isinstance(step, int) or isinstance(step, bool)):
            raise ProtocolError("bad_request", "step must be an integer")
        meta = req.get("meta") or {}
        if not isinstance(meta, dict):
            raise ProtocolError("bad_request", "meta must be an object")
        t = r.clock.now()
        now = r.read_now()
        start = counters(now)
        lim = r.profile.limits_at(t)
        call = Call(call_id, cmd, step, t, start, meta=meta, memory_limited=_memory_limited(r, lim),
                    lifetime_peak0=(now.get("mem") or {}).get("lifetime_peak"))
        call.see_memory(now)
        with self.lock:
            if call_id in self.seen:
                raise ProtocolError("duplicate_call", f"call_id {call_id!r} was already used in this run")
            self.seen.add(call_id)
            self.running[call_id] = call
        if step is not None:
            r.view.step = step
        seg, active = r.profile.segment_at(t)
        view_text = r.view.text(t)
        ev = {"call_id": call_id, "cmd": cmd, "step": step, "segment": seg}
        if meta:
            ev["meta"] = meta
        r.events.emit("tool_start", t=t, **ev)
        if view_text is not None:
            r.events.emit("view", t=t, via="tool_start", call_id=call_id)
        return {"t": round(t, 4), "segment": seg, "active_segments": active, "limits": K.limits_json(lim),
                "deadline_s": lim["harness.deadline"], "feedback": lim["harness.feedback"],
                "view_text": view_text}

    def _h_tool_end(self, req):
        r = self.run
        call_id = _req_str(req, "call_id")
        exit_code = req.get("exit_code")
        if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
            raise ProtocolError("bad_request", "exit_code must be an integer or null")
        dur = req.get("duration_s")
        timed_out = bool(req.get("timed_out", False))
        output = req.get("output")
        if output is not None and not isinstance(output, str):
            raise ProtocolError("bad_request", "output must be a string")
        t = r.clock.now()
        with self.lock:
            call = self.running.pop(call_id, None)
        if call is None:
            raise ProtocolError("unknown_call", f"no running call {call_id!r}")
        if not isinstance(dur, (int, float)) or isinstance(dur, bool) or not math.isfinite(dur):
            dur = t - call.t0
        if (timed_out or exit_code != 0) and hasattr(r, "refresh_slow_counters"):
            r.refresh_slow_counters()   # netem drops / partition hits are polled at 1 Hz: refresh now
        now = r.read_now()
        end = counters(now)
        call.see_memory(now)
        life = (now.get("mem") or {}).get("lifetime_peak")
        if life is not None and call.lifetime_peak0 is not None and life > call.lifetime_peak0:
            # A new all-time high was reached during the call, perhaps between samples: it's the call's.
            call.mem_peak = max(call.mem_peak or 0, life)
        lim = r.profile.limits_at(t)
        memory_limited = call.memory_limited or _memory_limited(r, lim)
        cause, evidence = attribute(call.start, end, call.min_free, float(dur), exit_code, timed_out,
                                    output=output, memory_limited=memory_limited)
        explain = explain_text(cause, evidence, lim, r.profile, t, lim["harness.deadline"])
        seg, _ = r.profile.segment_at(t)
        r.events.emit("tool_end", t=t, call_id=call_id, exit_code=exit_code, duration_s=dur,
                      timed_out=timed_out, cause=cause, evidence=evidence, segment=seg, t_start=round(call.t0, 4),
                      mem_peak_bytes=_int(call.mem_peak),
                      mem_peak_nonreclaimable_bytes=_int(call.mem_peak_nonreclaimable))
        return {"t": round(t, 4), "cause": cause, "explain": explain}

    def _h_view(self, req):
        r = self.run
        t = r.clock.now()
        via = req.get("via", "view")
        if via not in ("view", "agent_tool", "now"):
            via = "view"
        r.events.emit("view", t=t, via=via, visible=r.profile.visibility != "none")
        return {"text": r.view.text(t), "data": r.view.data(t)}

    def _h_state(self, req):
        r = self.run
        t = r.clock.now()
        seg, active = r.profile.segment_at(t)
        nb = r.profile.next_boundary(t)
        nxt = None
        if nb is not None:
            nxt = {"t": nb, "segment": r.profile.segment_at(nb)[0]}
        return {"t": round(t, 4), "mode": r.mode, "segment": seg, "active_segments": active,
                "limits": K.limits_json(r.profile.limits_at(t)), "next": nxt,
                "running_calls": self.running_ids()}

    def _h_mark(self, req):
        r = self.run
        label = _req_str(req, "label")
        data = req.get("data")
        t = r.clock.now()
        r.events.emit("mark", t=t, label=label, data=data if isinstance(data, dict) else None)
        return {"t": round(t, 4)}


# sockaddr_un holds 108 bytes on Linux (104 on macOS), including the terminating NUL.
SOCKET_PATH_MAX = 103


def socket_dir() -> Path:
    if "RPROF_SOCKET_DIR" in os.environ:
        return Path(os.environ["RPROF_SOCKET_DIR"])
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return Path("/run/rprof/sockets")
    import tempfile
    return Path(tempfile.gettempdir()) / "rprof-sockets"


def socket_path_for(path: Path) -> tuple[Path, Path | None]:
    """Where to bind a socket meant to live at ``path``: (bind path, symlink to create or None).

    A path too long for a Unix socket is bound under a short directory instead, and ``path``
    becomes a symlink to it. Clients follow the symlink.
    """
    if len(os.fsencode(str(path.absolute()))) <= SOCKET_PATH_MAX:
        return path, None
    import hashlib
    d = socket_dir()
    d.mkdir(parents=True, exist_ok=True)
    short = d / (hashlib.sha1(os.fsencode(str(path.absolute()))).hexdigest()[:16] + ".sock")
    return short, path


class ControlServer:
    """Serves one ControlHandler on one or more Unix sockets."""

    def __init__(self, handler: ControlHandler):
        self.handler = handler
        self.servers: list[asyncio.base_events.Server] = []
        self.paths: list[Path] = []
        self.conns: set[asyncio.StreamWriter] = set()

    async def listen(self, path: Path, inside: bool = False, mode: int = 0o660) -> Path:
        """Listen at ``path`` (via a short path and a symlink if ``path`` is too long); returns the bind path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        bind, link = socket_path_for(path)
        if inside and link is not None:
            # The sandbox reaches this socket through a bind mount, where a host symlink can't point.
            raise RprofError(f"{path} is too long for a Unix socket ({len(os.fsencode(str(path)))} bytes, "
                             f"limit {SOCKET_PATH_MAX}); use a shorter --ctl-dir", 2)
        for p in (bind, link):
            if p is not None:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
        path = bind

        async def on_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            self.conns.add(writer)
            try:
                while True:
                    try:
                        line = await reader.readline()
                    except (ValueError, asyncio.LimitOverrunError):
                        reply = {"id": None, "ok": False, "error": {"code": "bad_request", "message": "line too long"}}
                        writer.write((json.dumps(reply) + "\n").encode())
                        break
                    if not line:
                        break
                    if not line.strip():
                        continue
                    try:
                        req = json.loads(line)
                    except json.JSONDecodeError as e:
                        reply = {"id": None, "ok": False, "error": {"code": "bad_request", "message": f"bad JSON: {e}"}}
                    else:
                        # Handlers read cgroup files; keep them off the event loop.
                        reply = await asyncio.get_running_loop().run_in_executor(
                            None, self.handler.handle, req, inside)
                    writer.write((json.dumps(reply, default=str) + "\n").encode())
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                self.conns.discard(writer)
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass

        srv = await asyncio.start_unix_server(on_conn, path=str(path), limit=MAX_LINE)
        os.chmod(path, mode if not inside else 0o666)
        _chgrp_rprof(path)
        self.servers.append(srv)
        self.paths.append(path)
        if link is not None:
            os.symlink(path, link)
            self.paths.append(link)
        return path

    async def close(self) -> None:
        """Stop listening and close open connections; connected clients see end-of-file.

        Connections must be closed explicitly: an accepted Unix socket keeps a reference to the
        listening socket's path, which would keep the run directory's filesystem busy.
        """
        for s in self.servers:
            s.close()
        conns = list(self.conns)
        for w in conns:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
        for w in conns:
            try:
                await asyncio.wait_for(w.wait_closed(), 1.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        for s in self.servers:
            try:
                await asyncio.wait_for(s.wait_closed(), 1.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        for p in self.paths:
            try:
                p.unlink()
            except OSError:
                pass


def _chgrp_rprof(path: Path) -> None:
    """Group 'rprof' if it exists, else the sudo caller's group, so a non-root harness can connect."""
    gid = None
    try:
        gid = grp.getgrnam("rprof").gr_gid
    except KeyError:
        if os.environ.get("SUDO_GID", "").isdigit():
            gid = int(os.environ["SUDO_GID"])
    if gid is not None:
        try:
            os.chown(path, -1, gid)
        except OSError:
            pass
