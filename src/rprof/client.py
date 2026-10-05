"""Harness client for the rprof control socket (design Appendix A.3).

Standard library only, so a harness can vendor this one file, including one that
runs inside the sandbox.

    from rprof.client import Client
    rp = Client()                                   # uses $RPROF_RUN / $RPROF_SOCKET
    info = rp.tool_start(call_id, cmd, step=3)      # current limits; never changes them
    res = sandbox_exec(cmd, timeout=info.deadline_s)
    fb = rp.tool_end(call_id, res.exit_code, res.duration_s)
"""

from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

NOT_VISIBLE = "Resource limits are not visible in this run."
OUTPUT_TAIL_CHARS = 65536


class RprofUnavailable(Exception):
    """rprof could not be reached within the client's timeout."""


class RprofRequestError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ToolStart:
    t: float
    segment: int | None
    limits: dict
    deadline_s: float | None
    feedback: str
    view_text: str | None
    active_segments: tuple = ()


@dataclass(frozen=True)
class ToolEnd:
    t: float
    cause: str | None
    explain: str | None


@dataclass(frozen=True)
class View:
    text: str
    data: dict


@dataclass(frozen=True)
class State:
    t: float
    mode: str
    segment: int | None
    limits: dict
    next: dict | None
    active_segments: tuple = ()
    running_calls: tuple = ()


@dataclass
class AgentTool:
    """A ready-made ``resource_status`` tool: ``definition`` for the model, ``call()`` to run it."""

    definition: dict
    call: Callable[..., str]
    name: str = "resource_status"
    description: str = field(default="")


_DESC = ("Show the sandbox's current resource limits (CPU, memory, disk I/O, processes, network, disk "
         "space) and, when available, upcoming changes. Takes no input.")


class Client:
    def __init__(self, run_dir: str | None = None, socket: str | None = None, token: str | None = None,
                 timeout_s: float = 10.0):
        self.run_dir = run_dir or os.environ.get("RPROF_RUN")
        if socket is None:
            env_sock = os.environ.get("RPROF_SOCKET")
            if env_sock and os.path.exists(env_sock):
                socket = env_sock
            elif self.run_dir:
                socket = os.path.join(self.run_dir, "control.sock")
            elif env_sock:
                socket = env_sock
        if not socket:
            raise RprofUnavailable("no rprof socket: set RPROF_RUN or RPROF_SOCKET, or pass socket=")
        self.socket_path = socket
        self.token = token if token is not None else os.environ.get("RPROF_HARNESS_TOKEN")
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._sock: Any = None
        self._buf = b""
        self._id = 0

    # ------------------------------------------------------------ transport
    def _connect(self):
        # A run directory too deep for a Unix socket path holds a symlink to a short path.
        path = os.path.realpath(self.socket_path) if os.path.islink(self.socket_path) else self.socket_path
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout_s)
        try:
            s.connect(path)
        except OSError as e:
            s.close()
            raise RprofUnavailable(f"cannot connect to {self.socket_path}: {e}") from None
        self._sock, self._buf = s, b""

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock, self._buf = None, b""

    def request(self, type: str, **fields) -> dict:
        with self._lock:
            self._id += 1
            req = {"id": self._id, "type": type, **fields}
            if self.token:
                req["token"] = self.token
            line = (json.dumps(req) + "\n").encode()
            for attempt in (0, 1):
                try:
                    if self._sock is None:
                        self._connect()
                    self._sock.sendall(line)
                    while b"\n" not in self._buf:
                        chunk = self._sock.recv(65536)
                        if not chunk:
                            raise ConnectionError("rprof closed the connection")
                        self._buf += chunk
                    raw, self._buf = self._buf.split(b"\n", 1)
                    break
                except RprofUnavailable:
                    raise
                except (OSError, ConnectionError) as e:
                    self._close()
                    if attempt == 1 or isinstance(e, socket.timeout):
                        raise RprofUnavailable(f"rprof did not answer within {self.timeout_s:g} s: {e}") from None
        rep = json.loads(raw)
        if not rep.get("ok"):
            err = rep.get("error") or {}
            raise RprofRequestError(err.get("code", "internal"), err.get("message", ""))
        return rep

    def close(self) -> None:
        with self._lock:
            self._close()

    # ------------------------------------------------------------ API
    def hello(self, client: str = "rprof.client", version: str = "1") -> dict:
        return self.request("hello", client=client, version=version)

    def tool_start(self, call_id: str, cmd: str, step: int | None = None, meta: dict | None = None) -> ToolStart:
        f: dict[str, Any] = {"call_id": call_id, "cmd": cmd}
        if step is not None:
            f["step"] = step
        if meta:
            f["meta"] = meta
        r = self.request("tool_start", **f)
        return ToolStart(r["t"], r.get("segment"), r.get("limits") or {}, r.get("deadline_s"),
                         r.get("feedback", "errno"), r.get("view_text"), tuple(r.get("active_segments") or ()))

    def tool_end(self, call_id: str, exit_code: int | None, duration_s: float, timed_out: bool = False,
                 output: str | None = None) -> ToolEnd:
        """``output`` (optional) lets rprof spot out-of-memory errors a program reports itself.

        Only its last 64 KiB is sent; rprof keeps only the matching line.
        """
        f: dict[str, Any] = {"call_id": call_id, "exit_code": exit_code, "duration_s": duration_s,
                             "timed_out": timed_out}
        if output:
            f["output"] = output[-OUTPUT_TAIL_CHARS:]
        r = self.request("tool_end", **f)
        return ToolEnd(r["t"], r.get("cause"), r.get("explain"))

    def view(self, _via: str = "view") -> View | None:
        r = self.request("view", via=_via)
        if r.get("text") is None:
            return None
        return View(r["text"], r.get("data") or {})

    def state(self) -> State:
        r = self.request("state")
        return State(r["t"], r["mode"], r.get("segment"), r.get("limits") or {}, r.get("next"),
                     tuple(r.get("active_segments") or ()), tuple(r.get("running_calls") or ()))

    def mark(self, label: str, **data) -> None:
        self.request("mark", label=label, data=data or None)

    def agent_tool(self, format: str = "anthropic") -> AgentTool:
        if format == "anthropic":
            definition = {"name": "resource_status", "description": _DESC,
                          "input_schema": {"type": "object", "properties": {}}}
        elif format == "openai":
            definition = {"type": "function", "function": {
                "name": "resource_status", "description": _DESC,
                "parameters": {"type": "object", "properties": {}}}}
        else:
            raise ValueError("format must be 'anthropic' or 'openai'")

        def call(*_a, **_kw) -> str:
            v = self.view(_via="agent_tool")
            return v.text if v is not None else NOT_VISIBLE
        return AgentTool(definition, call, description=_DESC)
