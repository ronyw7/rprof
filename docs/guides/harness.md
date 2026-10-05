# Connect your harness

Your harness is the program that runs the agent and executes its tool calls in the sandbox.
This guide shows how to report each tool call to rprof, enforce deadlines, tell the agent why a
call failed, and show the agent its limits.

rprof never changes limits because of what the harness reports. Limits follow the profile's
clock. The reports let rprof label its measurements and explain failures.

## Launch the harness through rprof

Put your harness command after `--`:

```bash
sudo rprof run --target docker:sbx --profile p.yaml -- .venv/bin/python my_harness.py
```

rprof applies the limits for the start of the run, then starts your command. It passes the
location of the run directory in the `RPROF_RUN` environment variable, which the client uses
to find rprof. The run ends when your command exits, and rprof exits with your command's exit
code.

The command runs as root, like rprof. Under `sudo`, plain `python` is the system's Python,
which can't import rprof's client. Use the Python of the environment where you installed
rprof, as above, or install rprof into your harness's own environment.

If the run ends first, because of `--duration` or Ctrl-C, rprof sends your command `SIGTERM`.
If it is still running 10 seconds later, rprof sends `SIGKILL`.

## Report each tool call

Use the Python client in `rprof.client`. Call `tool_start` just before a command runs in the
sandbox and `tool_end` just after it returns:

```python
import subprocess
import time

from rprof.client import Client

rp = Client()  # finds rprof through $RPROF_RUN


def run_tool(call_id: str, cmd: str, step: int) -> str:
    info = rp.tool_start(call_id, cmd, step=step)
    start = time.monotonic()
    try:
        p = subprocess.run(["docker", "exec", "sbx", "sh", "-c", cmd],
                           capture_output=True, text=True, timeout=info.deadline_s)
        exit_code, output, timed_out = p.returncode, p.stdout + p.stderr, False
    except subprocess.TimeoutExpired:
        exit_code, output, timed_out = None, "", True
    result = rp.tool_end(call_id, exit_code, time.monotonic() - start, timed_out=timed_out,
                         output=output)

    # Tell the agent as much as the profile's `harness.feedback` allows.
    if info.feedback == "errno":
        output += f"\n[exit code {exit_code}]"
    elif info.feedback == "explain":
        output += f"\n[exit code {exit_code}] {result.explain or ''}"
    return output
```

- `call_id` names the call. It must be unique within a run.
- `step` is optional. It records which step of the agent's loop the call belongs to.
- `tool_start` replies at once, with the limits in force at that moment.
- Passing `output` to `tool_end` is optional. It lets rprof recognize out-of-memory errors that
  a program reports itself, described below. rprof doesn't store the output.

`tool_start` returns what the harness needs to run the call:

| Field | Meaning |
| --- | --- |
| `info.deadline_s` | The profile's `harness.deadline` in seconds, or `None` for no deadline |
| `info.feedback` | The profile's `harness.feedback`: `none`, `errno` or `explain` |
| `info.view_text` | The agent view as text, or `None` if the profile's `visibility` is `none` |
| `info.segment` | The segment in force (0 means only the defaults) |
| `info.limits` | Every limit in force, as a dictionary |

`tool_end` returns `result.cause` and `result.explain`, described below. The
[client reference](../reference/client.md) lists every method and field.

Two complete harnesses come with rprof. `examples/fake_harness.py` runs commands at fixed
times. `examples/sql_agent.py` runs a list of SQL statements against SQLite and Postgres, and
when a call fails it waits for the current segment to end and tries again.

## Enforce deadlines

rprof does not stop slow tool calls. Your harness does, using `info.deadline_s`, as in the
`timeout=` argument above. Report a stopped call with `timed_out=True`.

Deadlines matter because CPU, disk I/O and soft memory (`mem.high`) limits make calls slow
rather than making them fail. Without a deadline, a heavily limited call can run for a very
long time.

## Tell the agent why a call failed

When a call fails, `tool_end` says what caused it:

| `result.cause` | Meaning | Example `result.explain` |
| --- | --- | --- |
| `memory` | A process was killed for exceeding the memory limit | `Killed: memory limit 512 MiB reached (segment 2, 25–40 s).` |
| `memory` | A process was killed, but the call still exited 0, as in `python sort.py; sha256sum out` | `A process was killed: memory limit 512 MiB reached, but the call exited 0.` |
| `memory` | The program reported running out of memory itself, as DuckDB and Python do | `Failed: out of memory under the memory limit 512 MiB (the program reported "Error: Out of Memory Error: …").` |
| `pids` | A process could not be started because of the process limit | `Failed: process limit of 16 reached (fork failed).` |
| `disk` | The data volume ran out of space | `Failed: disk capacity 100 MiB reached (no space left on device).` |
| `network` | Packets were dropped or connections were blocked | `Failed: network blocked (reject).` |
| `deadline` | The call timed out | `Timed out after 30 s: cpu was the most throttled resource (92% of the call; limit: cpu 0.5 cores).` |
| `None` | The call succeeded with no process killed, or no limit explains the failure | `None` |

The text includes the segment's time window only when the profile's `visibility` is `full`.

How much of this the agent sees is an experimental choice, set by the profile's
`harness.feedback`. The end of `run_tool` above follows it. With `none`, the agent sees only
the command's own output.

rprof recognizes a program's own out-of-memory message only if you pass `output`, the call
failed, and a memory limit is in force. It looks for `out of memory`, `MemoryError`, `Cannot
allocate memory`, `std::bad_alloc` and `OutOfMemoryError`.

> [!NOTE]
> rprof finds the cause by comparing kernel counters at the start and end of the call. These
> counters cover the whole sandbox. If two calls overlap and a process is killed, both calls
> report `memory`.

## Show the agent its limits

The agent view describes the limits in force, and with `visibility: full`, what changes next.
[Let the agent see the schedule](writing-profiles.md#let-the-agent-see-the-schedule) shows
what it looks like.

Where the agent sees it is another experimental choice. There are four options:

| Where | How | What the agent gets |
| --- | --- | --- |
| In the system prompt | Put `rp.view().text` in the prompt when the task starts | The schedule up front |
| After every tool call | Append `info.view_text` to each call's output | The current limits at every step |
| On request | Register `rp.agent_tool()` as a tool | The limits whenever it calls `resource_status` |
| In the sandbox | Mount the view directory, described below | The limits only if it thinks to look |

`rp.view()` returns `None` when visibility is `none`. `rp.agent_tool()` returns a ready-made
tool named `resource_status`. Pass `"anthropic"` (the default) or `"openai"` to choose the
definition format:

```python
tool = rp.agent_tool("anthropic")
tool.definition   # register this with your model
tool.call()       # run this when the model calls the tool; returns the view text
```

> [!IMPORTANT]
> `visibility` controls only what rprof shows. The sandbox can still read its real limits from
> its cgroup files, for example with `cat /sys/fs/cgroup/memory.max`. Agents do this after an
> unexplained failure. To hide those files, see
> [Hide the limits from the sandbox](limits.md#hide-the-limits-from-the-sandbox).

To make the view readable inside the sandbox, mount the view directory when you start the
container. rprof keeps two files there, `now.txt` and `state.json`, and replaces them
atomically, so the agent never reads a half-written file:

```bash
sudo mkdir -p /var/lib/rprof/view/sbx
docker run -d --name sbx -v /var/lib/rprof/view/sbx:/run/rprof:ro rprof-testbox sleep infinity
docker exec sbx cat /run/rprof/now.txt    # during a run
```

rprof logs each request for the view in `events.jsonl`, with how it was made: through
`tool_start`, `rp.view()`, the agent tool, or `rprof now`. Reads of `now.txt` inside the
sandbox can't be logged, so if you need to know whether the agent looked, use one of the
other options.

## Run tool calls in parallel

The client is thread-safe. Calls from several threads can overlap, each with its own
`call_id`. rprof can't split the sandbox's usage between calls that overlap, so the timeline
reports usage for each stretch of time and lists the calls running in it.

## Mark points in the run

Record events of your own, such as the end of the task:

```python
rp.mark("task_done", rows=42)
```

Marks appear in `events.jsonl` with the time and your data.

## Use a shell harness

A shell script started by `rprof run` can use the command line instead of the client:

```bash
rprof now                            # print the agent view
rprof mark task_done --data rows=42
```

Both find rprof through `RPROF_RUN`.

## Run the harness inside the sandbox

Some harnesses, such as off-the-shelf coding agents, run inside the sandbox. Use
`--harness inside`. rprof then also listens on a socket that you mount into the sandbox:

```bash
sudo mkdir -p /var/lib/rprof/ctl/sbx
docker run -d --name sbx -v /var/lib/rprof/ctl/sbx:/run/rprof-ctl sandbox-image sleep infinity

sudo rprof run --target docker:sbx --profile p.yaml --harness inside --protect my-harness -- \
  docker exec -e RPROF_SOCKET -e RPROF_HARNESS_TOKEN sbx my-harness
```

- rprof creates a secret token for the run and passes it to your command in
  `RPROF_HARNESS_TOKEN`. Requests on the in-sandbox socket must include it. The client does this
  for you.
- `RPROF_SOCKET` tells the client where the socket is inside the sandbox.
- The harness's own CPU and memory count toward the limits.
- `--protect my-harness` shields processes whose command line matches the pattern from being
  killed when memory runs out. Processes the harness starts are not shielded, so a memory limit
  kills the tool, not the harness.
- Network limits also slow the harness's calls to the language model's API. Exempt the API's
  addresses with `net.allow`.

`client.py` uses only the Python standard library, so you can copy it into the sandbox image.

## If rprof is unreachable

If rprof doesn't answer within 10 seconds, client calls raise `RprofUnavailable`. Pass
`Client(timeout_s=…)` to change this. Your harness
decides whether to continue. Calls made while rprof was unreachable are missing from the run's
data.

To write a client in another language, see the [control protocol](../reference/protocol.md).
