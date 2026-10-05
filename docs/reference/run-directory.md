# Run directory reference

Each `rprof run` writes a directory named `<date>T<time>-<name>`, for example
`runs/2026-10-02T1803-cpu-mem-demo/`. When rprof runs under `sudo`, the directory is given to
the user who ran `sudo`. For a guide to reading these files, see
[Read the results](../guides/results.md).

| File | Format | Written | Contents |
| --- | --- | --- | --- |
| `meta.json` | JSON | At start and end | The host, target, settings and outcome of the run |
| `profile.yaml` | YAML | At start | The profile as applied, with every default filled in |
| `snapshot.json` | JSON | Before the first change | Everything rprof must undo: original settings (enforce mode) and cgroup masks (`--hide-limits`) |
| `samples.jsonl` | One JSON object per line | Ten times a second | Raw usage counters |
| `events.jsonl` | One JSON object per line | As events happen | Limit changes, tool calls, marks, warnings, errors |
| `timeline.jsonl` | One JSON object per line | At the end | Usage for each interval between tool-call starts, ends and limit changes |
| `report.json`, `report.md` | JSON, Markdown | At the end | Whether each limit bound in each segment, and every tool call |
| `agentview/` | `now.txt`, `state.json` | When the view changes | The last view the agent could see |
| `rprof.log` | Text | During the run | Every command rprof ran, such as `tc` and `iptables`, with its duration |
| `token` | Text, owner-only | At start | The harness token. `--harness inside` only. |

`control.sock` exists only while the run is in progress.

All times named `t`, `t0` or `t1` are seconds since the run started. Sizes are in bytes.

## meta.json

| Field | Description |
| --- | --- |
| `run_id`, `rprof_version`, `git_sha` | Which run, and which rprof produced it |
| `started_at`, `ended_at`, `duration_s` | When the run started and ended |
| `end_reason` | `command_exit`, `profile_end`, `duration`, `signal` or `error` |
| `write_errors` | Files rprof couldn't fully write, such as `samples.jsonl`, with the first error. Empty when all writes succeeded. |
| `mode`, `harness`, `hz`, `command`, `protect` | The run's settings |
| `host` | `hostname`, `kernel`, `docker_version`, `cgroup_driver`, `cpus`, `mem_bytes`, `swap_bytes` |
| `target` | `spec`, `cgroup_path`, `container_id`, `init_pid`, `net` (interfaces), `io_device`, `data_mount` |
| `profile` | The profile's `name`, `path`, `visibility` and number of segments |
| `managed_knobs` | The knobs rprof set during the run |
| `hide_limits`, `hidden_limits` | Whether `--hide-limits` was given, and the containers it masked |
| `degraded_knobs` | Knobs skipped because of `--allow-degraded` |
| `knob_capabilities` | For each knob, `null` if the host can enforce it, or the reason it can't |
| `capabilities` | A copy of the host's selftest results, if any |
| `features` | Kernel features found during the run, such as `memory_peak_reset` |

## samples.jsonl

One line per sample. A field the host can't provide is left out of the line, not set to
`null`. Counters only grow; compute rates from the difference between two lines.

| Field | Kind | Description |
| --- | --- | --- |
| `t`, `t_wall` | | Run time in seconds, and the same moment as UTC ISO 8601 |
| `segment` | | The active segment |
| `running_calls` | | `call_id`s of the tool calls in progress |
| `cpu.usage_usec`, `cpu.user_usec`, `cpu.system_usec` | Counter, µs | CPU time used, in total, in user mode and in the kernel |
| `cpu.nr_periods`, `cpu.nr_throttled` | Counter | Scheduling periods, and periods in which the CPU limit held the sandbox back |
| `cpu.throttled_usec` | Counter, µs | Time held back by the CPU limit, summed over CPUs |
| `mem.current` | Gauge | Memory in use, including page cache |
| `mem.peak` | Gauge | Highest memory use since the previous sample. Kernel 6.12 or later only. |
| `mem.anon`, `mem.file` | Gauge | Process memory, and page cache |
| `mem.shmem` | Gauge | Shared memory and tmpfs files. Counted in `mem.file`, but not reclaimable without swap. |
| `mem.pgmajfault` | Counter | Page faults that had to read from disk |
| `mem.swap_current` | Gauge | Swap in use |
| `mem.events.high`, `.max`, `.oom`, `.oom_kill` | Counter | Times the soft limit was exceeded, the hard limit was hit, memory ran out, and a process was killed |
| `io.<MAJ:MIN>.rbytes`, `.wbytes` | Counter | Bytes read and written on each disk |
| `io.<MAJ:MIN>.rios`, `.wios` | Counter | Read and write operations on each disk |
| `pids.current` | Gauge | Processes and threads |
| `pids.events_max` | Counter | Processes or threads that couldn't be created because of the limit |
| `psi.cpu`, `psi.memory`, `psi.io` | Counter, µs | Pressure: `some_us` is time in which at least one process waited for the resource; `full_us` is time in which all did |
| `net.<interface>.rx_bytes`, `.tx_bytes` | Counter | Bytes received and sent |
| `net.<interface>.rx_packets`, `.tx_packets`, `.rx_drop`, `.tx_drop` | Counter | Packets, and packets the interface dropped |
| `net.tcp_retrans_segs` | Counter | TCP segments sent again. Updated once a second. |
| `net.qdisc_drops` | Counter | Packets rprof dropped for `net.loss` and `net.rate`. Updated once a second. |
| `net.partition_hits` | Counter | Packets rprof rejected or dropped for `net.partition`. Updated once a second. |
| `disk.used_bytes`, `disk.free_bytes` | Gauge | Space used and space writable on the data volume |
| `disk.ballast_bytes` | Gauge | Size of rprof's ballast file |
| `host.psi` | Counter, µs | Pressure for the whole host. Shows if other load disturbed the run. Updated once a second. |
| `self.cpu.usage_usec` | Counter, µs | CPU used by rprof itself |

Above 20 samples per second, `psi` and the `net.<interface>` fields are read about 20 times a
second and left out of the other lines.

## events.jsonl

Each line is `{"t": ..., "type": ..., ...}`, in time order.

| `type` | Fields | Logged when |
| --- | --- | --- |
| `run_start` | `mode`, `profile_name`, `run_id`, `harness`, `hz` | The run starts |
| `segment_applied` | `boundary`, `segment`, `active_segments`, `limits`, `changed`, `enforced`, `apply_ms`, `late_ms`, `errors` | Limits change at a segment boundary, including at 0 s |
| `command_start`, `command_exit` | `pid`, `argv` / `exit_code` | The command after `--` starts or exits |
| `tool_start` | `call_id`, `cmd`, `step`, `segment`, `meta` | A harness reports a tool call starting |
| `tool_end` | `call_id`, `exit_code`, `duration_s`, `timed_out`, `cause`, `evidence`, `segment`, `t_start`, `mem_peak_bytes`, `mem_peak_nonreclaimable_bytes` | A harness reports a tool call ending. The peaks are the sandbox's highest memory while the call ran, with and without page cache. |
| `view` | `via`, and `call_id` or `visible` | The agent view is fetched |
| `mark` | `label`, `data` | A harness or `rprof mark` records a mark |
| `protect`, `unprotect_child` | `pid`, `cmd` (and `ppid`) | A process is shielded from memory kills, or a child loses the shield it inherited |
| `limits_hidden` | `pid`, `path` | `--hide-limits` masked `/sys/fs/cgroup` in a container |
| `signal` | `signal` | rprof receives Ctrl-C or `SIGTERM` |
| `run_end` | `reason`, `exit_code`, `restore_errors`, `apply_ms_p95`, `sampler_overruns` | The run ends |
| `warning`, `error` | `code`, `message` | Something went wrong. See below. |

Warning and error codes:

| `code` | Meaning |
| --- | --- |
| `apply_failed` | A limit couldn't be written |
| `mem_max_below_usage` | `mem.max` was set below the memory in use, so the kernel reclaimed and possibly killed a process |
| `hide_limits_failed` | `--hide-limits` couldn't mask a container. The run stops with exit code 73. |
| `hide_limits_nothing` | `--hide-limits` found no container in the target to mask |
| `restore_failed` | An original value couldn't be written back. Run `rprof reset --run`. |
| `controllers_enabled` | rprof enabled cgroup controllers the target was missing |
| `degraded` | The run skipped knobs because of `--allow-degraded` |
| `disk_overcommitted` | The sandbox already used more than `disk.capacity` |
| `ip6tables_unavailable` | IPv6 partition rules couldn't be installed |
| `protect_failed` | A process couldn't be shielded from memory kills |
| `self_cgroup` | rprof couldn't move itself into its own cgroup |
| `target_gone` | The target disappeared during the run |
| `write_failed` | Some lines couldn't be written to `samples.jsonl`. The message gives the count and the first error. |
| `sampler_stuck` | The sampling thread didn't stop within 10 s at the end of the run, so the final sample was skipped |
| `command_failed` | The command after `--` couldn't start |
| `command_still_running` | `--duration` ended the run while the command was running |
| `internal` | An internal error |

## timeline.jsonl

One line per interval. A new interval starts whenever a tool call starts or ends, or a segment
boundary passes.

| Field | Description |
| --- | --- |
| `t0`, `t1` | The interval |
| `segment`, `limits` | The segment and the limits in force |
| `running_calls` | `{"call_id", "cmd"}` for each call running in the interval |
| `usage` | `cpu_cores_mean`, `cpu_cores_max`, `mem_mean`, `mem_peak`, `io_rbps`, `io_wbps` (bytes per second), `net_rx_bps`, `net_tx_bps` (bits per second), `pids_max` |
| `pressure` | `cpu_some`, `mem_some`, `mem_full`, `io_some`, `io_full`: the share of the interval, from 0 to 1 |
| `events` | `oom_kill`, `mem_high`, `mem_max`, `pids_max`, `net_drops`, `partition_hits`: how many happened in the interval |
| `failed_calls` | `call_id`s of calls that failed at the end of the interval |

## report.json

| Field | Description |
| --- | --- |
| `run_id`, `mode`, `profile`, `t_end`, `samples`, `thresholds` | The run and the thresholds used |
| `memory_basis` | Which memory the memory rows count: `non_reclaimable`, `non_reclaimable_without_shmem` or `total`. See [Which memory counts](../guides/results.md#which-memory-counts). |
| `segments` | One entry per segment, starting with segment 0 (defaults only), as below |
| `calls` | One entry per tool call, as below |
| `summary` | `calls`, `failed`, `no_effect_segments`, `apply_ms_p95`, `warnings` |
| `warnings` | Every warning and error event |

Each entry in `segments`:

| Field | Description |
| --- | --- |
| `segment`, `t0`, `t1`, `label` | The segment as written in the profile |
| `reached`, `windows`, `duration_s` | Whether the run reached the segment, the time actually covered, and its length |
| `no_effect` | `true` if no limit bound in the segment. `null` in measure mode. |
| `calls` | `started`, `failed`, `started_ids`, `failed_ids` |
| `knobs` | One entry per limit in force, as below |

Each entry in `knobs`:

| Field | Description |
| --- | --- |
| `knob`, `limit`, `limit_raw` | The knob, its value in JSON units, and its value as written in the profile |
| `usage`, `unit` | `mean`, `p95` and `max` of usage during the segment, and their unit |
| `basis`, `usage_total` | Memory rows only: the basis of `usage` and `violation`, and the same statistics with page cache included |
| `bound` | `true` or `false` in enforce mode, by the rules in [Did each limit bind?](../guides/results.md#did-each-limit-bind). `null` in measure mode and for knobs that set conditions rather than budgets, such as `net.delay`. |
| `evidence` | The measurements behind `bound`, explained in the same guide |
| `violation` | Measure mode only: `time_frac`, the share of the segment over the limit, and `peak_over`, the largest amount over it |

Each entry in `calls`:

| Field | Description |
| --- | --- |
| `call_id`, `cmd`, `step` | As reported by the harness |
| `t0`, `t1`, `duration_s` | When the call ran |
| `exit_code`, `timed_out`, `failed`, `finished`, `cause`, `evidence` | The outcome. `failed` follows the exit code, so a call with `cause: memory` and `evidence.exited_ok` is not failed. |
| `mem_peak_bytes`, `mem_peak_nonreclaimable_bytes` | The sandbox's highest memory while the call ran, with and without page cache |
| `mem_max_bytes`, `mem_fit` | The tightest `mem.max` in force during the call, and whether the non-reclaimable peak stayed under it |
| `segments` | Every segment the call ran in |
| `limit_changes_during_call` | Times at which limits changed while the call ran |
| `limits_at_start` | The limits in force when the call started |

## snapshot.json

| Field | Description |
| --- | --- |
| `files` | Each kernel file rprof changed, with its original contents |
| `tc` | Network queues rprof added, per network namespace and interface |
| `iptables` | Firewall chains rprof added. Each rule is tagged `rprof:<run-id>`. |
| `ballast` | The path of the disk ballast file, or `null` |
| `masks` | Containers whose `/sys/fs/cgroup` is masked by `--hide-limits`, by process and mount namespace |

`rprof reset --run <run-dir>` uses this file to undo the run.
