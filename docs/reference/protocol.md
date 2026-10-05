# Control protocol reference

A harness talks to a running `rprof run` over a Unix socket, using one line of JSON per
message. The Python client in `rprof.client` implements this protocol. Use this page to write
a client in another language. For how to use the messages, see
[Connect your harness](../guides/harness.md).

Messages never change limits. Limits follow the profile's clock.

## Connect

| Socket | Path | Who uses it |
| --- | --- | --- |
| Host socket | `<run-dir>/control.sock` | A harness on the host. `rprof run` passes the run directory in `RPROF_RUN`. |
| In-sandbox socket | `<ctl-dir>/control.sock` on the host, `$RPROF_SOCKET` inside the sandbox | A harness inside the sandbox, with `rprof run --harness inside` |

The host socket can be opened by root and by the group `rprof`, if it exists. Otherwise, by
the group of the user who ran `sudo`. The in-sandbox socket can be opened by anyone in the
sandbox, so every request on it must include a `token` field equal to
`$RPROF_HARNESS_TOKEN`. rprof creates a new token for each run, saves it in `<run-dir>/token`
(readable only by its owner), and passes it only to the command it starts.

Unix socket paths are limited to about 100 bytes. If `<run-dir>/control.sock` would be longer,
rprof creates the socket under `/run/rprof/sockets/` (or `$RPROF_SOCKET_DIR`) and makes
`<run-dir>/control.sock` a symlink to it. Clients should connect to the symlink's target.

You may open several connections at once, for example one per thread. On each connection,
rprof answers requests one at a time, in order.

## Message format

Each request and reply is one JSON object on one line, encoded as UTF-8, of at most 1 MiB.

```text
request:  {"id": 7, "type": "tool_start", ...fields}
reply:    {"id": 7, "ok": true, ...fields}
error:    {"id": 7, "ok": false, "error": {"code": "unknown_call", "message": "no running call 'c9'"}}
```

`id` is any integer you choose. The reply carries the same `id`.

| Error code | Meaning |
| --- | --- |
| `bad_request` | The line isn't valid JSON, the `type` is unknown, a field is missing or has the wrong type, or the line is too long |
| `unknown_call` | `tool_end` for a `call_id` that isn't running |
| `duplicate_call` | `tool_start` with a `call_id` already used in this run |
| `auth_required` | A request on the in-sandbox socket has a missing or wrong `token` |
| `internal` | rprof failed while handling the request. It also logs an `error` event. |

Times named `t` are seconds since the run started.

## Messages

| `type` | Send it | Reply |
| --- | --- | --- |
| [`hello`](#hello) | To check the connection | rprof's version, the run, mode and visibility |
| [`tool_start`](#tool_start) | Just before a tool call runs | The limits, deadline, feedback level and agent view |
| [`tool_end`](#tool_end) | Just after a tool call returns | The cause of a failure, and an explanation |
| [`view`](#view) | To fetch the agent view | The view as text and as data |
| [`state`](#state) | To fetch the current schedule position | Segment, limits, and the next change |
| [`mark`](#mark) | To record an event of your own | The time |

### hello

| Request field | Type | |
| --- | --- | --- |
| `client` | string | Optional. Your client's name. |
| `version` | string | Optional. Your client's version. |

| Reply field | Type | |
| --- | --- | --- |
| `rprof_version` | string | |
| `run_id` | string | The run directory's name |
| `mode` | string | `enforce` or `measure` |
| `visibility` | string | `none`, `current` or `full` |
| `t` | number | |

```json
→ {"id": 1, "type": "hello", "client": "my-harness"}
← {"id": 1, "ok": true, "rprof_version": "0.1.0", "run_id": "2026-10-02T1803-cpu-mem-demo", "mode": "enforce", "visibility": "full", "t": 0.04}
```

### tool_start

| Request field | Type | |
| --- | --- | --- |
| `call_id` | string | Required. Unique within the run. |
| `cmd` | string | Optional. The command, for the record. Default: empty. |
| `step` | integer | Optional. The agent's step number. |
| `meta` | object | Optional. Copied into the `tool_start` event. |

| Reply field | Type | |
| --- | --- | --- |
| `t` | number | |
| `segment` | integer | The active segment. 0 means only defaults apply. With overlapping segments, the lowest number. |
| `active_segments` | list of integers | Every active segment |
| `limits` | object | Every limit in force, as a [limits object](#limits-object) |
| `deadline_s` | number or null | The profile's `harness.deadline`, in seconds |
| `feedback` | string | The profile's `harness.feedback` |
| `view_text` | string or null | The agent view. `null` when visibility is `none`. |

```json
→ {"id": 7, "type": "tool_start", "call_id": "c12", "cmd": "psql -f load.sql", "step": 12}
← {"id": 7, "ok": true, "t": 61.2, "segment": 1, "active_segments": [1], "limits": {...}, "deadline_s": 30, "feedback": "explain", "view_text": "t = 61 s · segment 1 of 4\n..."}
```

### tool_end

| Request field | Type | |
| --- | --- | --- |
| `call_id` | string | Required. A `call_id` from an earlier `tool_start`. |
| `exit_code` | integer or null | The command's exit code. `null` if unknown. |
| `duration_s` | number | How long the call took. If missing, rprof uses its own clock. |
| `timed_out` | boolean | `true` if the harness stopped the call at its deadline |
| `output` | string | Optional. The end of the call's output, used to recognize out-of-memory errors that the program reported itself. rprof searches only the last 64 KiB and doesn't store it. |

| Reply field | Type | |
| --- | --- | --- |
| `t` | number | |
| `cause` | string or null | Why the call failed: `memory`, `pids`, `disk`, `network` or `deadline`. `null` if it succeeded or no limit explains it. |
| `explain` | string or null | One sentence for the agent, such as `Killed: memory limit 1 GiB reached (segment 1, 60–120 s).` |

```json
→ {"id": 8, "type": "tool_end", "call_id": "c12", "exit_code": 137, "duration_s": 2.0, "timed_out": false}
← {"id": 8, "ok": true, "t": 63.2, "cause": "memory", "explain": "Killed: memory limit 1 GiB reached (segment 1, 60–120 s)."}
```

rprof compares kernel counters from the start and end of the call, and reports the first cause
that fits:

| `cause` | Evidence |
| --- | --- |
| `memory` | The kernel killed a process for exceeding the memory limit. Checked for every call. If the call still exited 0, for example because a later command in `a; b` succeeded, the evidence includes `exited_ok: true`. |
| `pids` | A process or thread couldn't be created because of the process limit |
| `memory` | The call's `output` reports running out of memory. The evidence's `output_match` holds the matching line. See below. |
| `disk` | Free space on the data volume fell to 1 MiB or less during the call |
| `network` | rprof dropped or blocked packets during the call |
| `deadline` | The call timed out, and none of the above applies. `explain` names the most throttled resource. |

Every cause except the first applies only to failed calls: an `exit_code` other than 0
(including `null`), or `timed_out`. The evidence is recorded in the `tool_end` event.

Some programs enforce their own memory budget and fail without the kernel killing anything.
DuckDB, for example, caps itself at 80% of the cgroup's `memory.max` and exits with "Out of
Memory Error". rprof attributes such a failure to `memory` when all of these hold: the call's
`output` contains `out of memory`, `MemoryError`, `Cannot allocate memory`, `std::bad_alloc` or
`OutOfMemoryError` (ignoring case); rprof is in enforce mode; and a memory limit is in force.

`explain` includes the segment's time window only when visibility is `full`.

### view

| Request field | Type | |
| --- | --- | --- |
| `via` | string | Optional. `view` (default), `agent_tool` or `now`. Recorded in the `view` event, so you can count where the agent saw its limits. |

| Reply field | Type | |
| --- | --- | --- |
| `text` | string or null | The view as text. `null` when visibility is `none`. |
| `data` | object or null | The view as data, below. `null` when visibility is `none`. |

```json
→ {"id": 9, "type": "view"}
← {"id": 9, "ok": true, "text": "t = 84 s · segment 1 of 4\nnow:  ...\nnext: ...\n", "data": {...}}
```

`data` has these fields:

| Field | Present | Description |
| --- | --- | --- |
| `t` | Always | |
| `visibility` | Always | `current` or `full` |
| `segment` | Always | The active segment |
| `step` | After a `tool_start` with `step` | The latest step number |
| `current` | Always | `{"t0", "t1", "limits"}`: the current interval and its limits. `t1` is `null` for the last interval. |
| `segments_total` | `full` only | The number of segments in the profile |
| `past` | `full` only | Earlier intervals, each `{"t0", "t1", "limits"}` |
| `upcoming` | `full` only | Later intervals, each `{"t0", "t1", "changes"}`. `changes` maps knobs to their values in the profile's own spelling, such as `{"net.loss": "30%"}`. An empty map means back to defaults. |

### state

No request fields. `state` serves the harness, not the agent, so it ignores visibility.

| Reply field | Type | |
| --- | --- | --- |
| `t` | number | |
| `mode` | string | |
| `segment` | integer | |
| `active_segments` | list of integers | |
| `limits` | object | A [limits object](#limits-object) |
| `next` | object or null | `{"t", "segment"}`: when the limits change next, and the segment then. `null` if they never change again. |
| `running_calls` | list of strings | The `call_id`s that have started and not ended |

### mark

| Request field | Type | |
| --- | --- | --- |
| `label` | string | Required |
| `data` | object | Optional |

The reply has `t`. The mark is saved as a `mark` event.

## Limits object

Replies describe limits as an object grouped by resource. `null` means no limit.

```json
{"cpu":  {"cores": 0.5, "cpus": null, "period_ms": 100},
 "mem":  {"high": 838860800, "max": 1073741824, "swap_max": 0},
 "io":   {"rbps": null, "wbps": null, "riops": null, "wiops": null},
 "pids": {"max": 16},
 "net":  {"rate_bps": null, "delay_ms": 0, "jitter_ms": 0, "loss_pct": 0, "partition": "none", "allow": []},
 "disk": {"capacity": null},
 "harness": {"deadline_s": 30, "feedback": "explain"}}
```

A segment with a `unified` map adds `"unified": {"<file>": "<value>"}`.

| Unit | Fields |
| --- | --- |
| Bytes | `mem.*`, `disk.capacity` |
| Bytes per second | `io.rbps`, `io.wbps` |
| Bits per second | `net.rate_bps` |
| Milliseconds | `cpu.period_ms`, `net.delay_ms`, `net.jitter_ms` |
| Seconds | `harness.deadline_s` |

In measure mode, `limits` still shows the profile's values. They are what the agent is told,
even though rprof doesn't write them.
