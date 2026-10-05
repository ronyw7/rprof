# Python client reference

`rprof.client` lets a harness talk to a running `rprof run`. It uses only the Python standard
library, so you can copy `src/rprof/client.py` into any environment, including a sandbox
image. For how to use it, see [Connect your harness](../guides/harness.md).

```python
from rprof.client import Client, RprofRequestError, RprofUnavailable
```

## Client

```python
Client(run_dir=None, socket=None, token=None, timeout_s=10.0)
```

| Argument | Default | Description |
| --- | --- | --- |
| `run_dir` | `$RPROF_RUN` | The run directory |
| `socket` | See below | Path of the control socket |
| `token` | `$RPROF_HARNESS_TOKEN` | Token for the in-sandbox socket. Sent with every request when set. |
| `timeout_s` | `10.0` | How long to wait for rprof before raising `RprofUnavailable` |

The client picks the socket in this order: the `socket` argument; `$RPROF_SOCKET` if that
file exists; `<run_dir>/control.sock`. If none is available, the constructor raises
`RprofUnavailable`.

A client keeps one connection and is safe to share between threads. If the connection
drops, the client reconnects once before raising. If the socket path is a symlink, which
rprof creates when the run directory is too deep for a Unix socket path, the client connects
to its target.

## Methods

| Method | Returns | Description |
| --- | --- | --- |
| `tool_start(call_id, cmd, step=None, meta=None)` | `ToolStart` | Report that a tool call is about to run |
| `tool_end(call_id, exit_code, duration_s, timed_out=False, output=None)` | `ToolEnd` | Report that a tool call returned. `exit_code` may be `None` if unknown. Pass the call's `output` so rprof can recognize out-of-memory errors the program reported itself; only its last 64 KiB is sent. |
| `view()` | `View` or `None` | The agent view. `None` when the profile's visibility is `none`. |
| `agent_tool(format="anthropic")` | `AgentTool` | A `resource_status` tool for the model. `format` is `"anthropic"` or `"openai"`. |
| `state()` | `State` | Where the run is in the schedule. Not limited by visibility. |
| `mark(label, **data)` | `None` | Record a `mark` event with optional data |
| `hello(client="rprof.client", version="1")` | `dict` | Check the connection. Returns rprof's version, the run ID, mode and visibility. |
| `request(type, **fields)` | `dict` | Send any [protocol message](protocol.md) and return the raw reply |
| `close()` | `None` | Close the connection |

## Return types

All are frozen dataclasses.

| Type | Fields |
| --- | --- |
| `ToolStart` | `t`, `segment`, `limits`, `deadline_s`, `feedback`, `view_text`, `active_segments` |
| `ToolEnd` | `t`, `cause`, `explain` |
| `View` | `text`, `data` |
| `State` | `t`, `mode`, `segment`, `limits`, `next`, `active_segments`, `running_calls` |

The fields have the meanings given in the [control protocol](protocol.md#messages).

`AgentTool` has:

- `definition`: the tool definition to register with the model, in the chosen format. The tool
  takes no input.
- `call()`: returns the current view text, or `Resource limits are not visible in this run.`
  when visibility is `none`. Each call is logged as a `view` event with `via: agent_tool`.

## Exceptions

| Exception | Raised when |
| --- | --- |
| `RprofUnavailable` | No socket can be found, or rprof doesn't answer within `timeout_s` |
| `RprofRequestError` | rprof rejects a request. `.code` is the [error code](protocol.md#message-format), such as `duplicate_call`; `.message` explains it. |
