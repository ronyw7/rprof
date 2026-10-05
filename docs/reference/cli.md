# CLI reference

```text
rprof [--version] [--verbose] COMMAND [OPTIONS] [ARGS]
```

`--verbose` (`-v`) prints debug logging to standard error. Every command accepts `--help`.

Commands that change limits need root: `apply`, `reset`, `selftest`, and `run` in enforce mode.
`watch` and `inspect` also need root for Docker targets, because they look inside the
container's network namespace. `validate`, `report`, `timeline`, `plot`, `mark` and `now` work
without root.

A *target* is written `docker:<name or id>` or `cgroup:<path>`. A `cgroup:` path is either
absolute or relative to `/sys/fs/cgroup`.

| Command | Summary |
| --- | --- |
| [`doctor`](#doctor) | Check that the host can run rprof |
| [`inspect`](#inspect) | Show what rprof found for a target |
| [`watch`](#watch) | Show live usage |
| [`apply`](#apply) | Set limits until reset |
| [`reset`](#reset) | Restore original limits |
| [`validate`](#validate) | Check a profile |
| [`run`](#run) | Apply a profile, record usage and write a report |
| [`mark`](#mark) | Record an event in a running run |
| [`now`](#now) | Print the agent view of a running run |
| [`report`](#report) | Regenerate a run's report |
| [`timeline`](#timeline) | Print usage per interval |
| [`plot`](#plot) | Plot usage and limits |
| [`selftest`](#selftest) | Test enforcement on this host |
| [`gen`](#gen) | Generate profiles |

## doctor

Checks cgroup v2, the cgroup controllers, the required tools and kernel features. With a
target, also checks what rprof can limit for that target.

```text
rprof doctor [--target TARGET] [--deep] [--json]
```

| Option | Description |
| --- | --- |
| `--target`, `-t` | Also check this target |
| `--deep` | Also set limits on a scratch cgroup and confirm the kernel enforces them. Needs root. |
| `--json` | Print the checks as JSON |

Exits with 1 if any check fails.

```bash
sudo rprof doctor --target docker:sbx --deep
```

## inspect

Shows the target's cgroup, processes, disk, network interfaces, data volume and current
limits.

```text
rprof inspect --target TARGET [--json] [--io-device DEV] [--data-path PATH]
```

| Option | Description |
| --- | --- |
| `--target`, `-t` | Required. The target. |
| `--json` | Print as JSON |
| `--io-device` | The disk to report for I/O limits, as `MAJ:MIN` or `/dev/…` |
| `--data-path` | The data volume's path inside the container. Default: `/data`. |

## watch

Shows a table of live usage that refreshes twice a second.

```text
rprof watch --target TARGET [--hz HZ] [--out FILE] [--duration SECONDS] [--once]
```

| Option | Description |
| --- | --- |
| `--target`, `-t` | Required. The target. |
| `--hz` | Samples per second. Default: 10. |
| `--out` | Also write every sample to this file, in the format of `samples.jsonl` |
| `--duration` | Stop after this many seconds |
| `--once` | Print one line, measured over one second, and exit |

## apply

Sets limits immediately. They stay until `rprof reset --target`. rprof saves the original
values the first time it changes each setting.

```text
rprof apply --target TARGET KNOB=VALUE [KNOB=VALUE ...] [--io-device DEV] [--data-path PATH] [--data-dir DIR]
```

| Option | Description |
| --- | --- |
| `--target`, `-t` | Required. The target. |
| `--io-device` | The disk for I/O limits, as `MAJ:MIN` or `/dev/…` |
| `--data-path` | The data volume's path inside the container. Default: `/data`. |
| `--data-dir` | The data volume's path on the host, instead of `--data-path` |

Knob values use the [profile formats](profile.md#value-formats). `net.allow` takes a
comma-separated list. Setting `mem.max` or `mem.high` also sets `mem.swap_max=0` unless you
give it.

```bash
sudo rprof apply --target docker:sbx cpu.cores=0.5 mem.max=512Mi io.wbps=20Mi
```

## reset

Restores original limits, and removes network rules and disk ballast.

```text
rprof reset --target TARGET
rprof reset --run RUN_DIR
```

| Option | Description |
| --- | --- |
| `--target`, `-t` | Undo everything `rprof apply` set on this target |
| `--run` | Restore from a run's `snapshot.json`, after rprof was killed |

## validate

Checks a profile's structure, units and overlaps, then prints its schedule.

```text
rprof validate PROFILE [--target TARGET] [--quiet]
```

| Option | Description |
| --- | --- |
| `--target`, `-t` | Also check that the host can enforce every knob the profile uses |
| `--quiet`, `-q` | Print nothing when the profile is valid |

Exits with 0 when valid. Otherwise exits with 1 and prints one problem per line as
`<location>: <problem>`, for example `segments[0].mem.max: 1Gx is not a byte size`.

## run

Applies a profile to a target on its schedule, samples usage, listens for tool-call events,
restores the original limits at the end, and writes a run directory.

```text
rprof run --target TARGET [--profile PROFILE] [OPTIONS] [-- COMMAND ...]
```

| Option | Description |
| --- | --- |
| `--target`, `-t` | Required. The target. |
| `--profile`, `-p` | The profile. Default: no limits. |
| `--mode` | `enforce` writes the limits. `measure` only records usage against them. Default: `enforce`. |
| `--hz` | Samples per second, from 1 to 100. Default: 10. |
| `--runs-dir` | Where to create the run directory. Default: `runs`. |
| `--name` | Label used in the run directory's name. Same rule as profile names: letters, digits, `.`, `_` and `-`, starting with a letter or digit, at most 64 characters. Default: the profile's name. |
| `--harness` | `outside` or `inside` the sandbox. `inside` also opens a socket for the sandbox. Default: `outside`. |
| `--protect` | Regular expression matched against process command lines. Matching processes are never chosen for memory kills. Repeatable. |
| `--net` | `docker:<name>` whose network the `net` knobs apply to. Repeatable. Default: the target container. |
| `--io-device` | The disk for I/O limits, as `MAJ:MIN` or `/dev/…` |
| `--data-path` | The data volume's path inside the container. Default: `/data`. |
| `--data-dir` | The data volume's path on the host |
| `--view-dir` | Where to write the agent view. Default: `/var/lib/rprof/view/<container name>`. |
| `--ctl-dir` | Where to create the in-sandbox socket, for `--harness inside`. Default: `/var/lib/rprof/ctl/<container name>`. |
| `--allow-degraded` | Run even if the host can't enforce some knobs the profile uses, and skip those knobs |
| `--duration` | Stop after this many seconds |
| `--capabilities` | Selftest results to check against. Default: `/var/lib/rprof/capabilities.json`. |
| `--no-report` | Don't write `report.md`, `report.json` and `timeline.jsonl` |
| `--hide-limits` | Mount a stand-in over `/sys/fs/cgroup` in the sandbox, so it can't read its real limits. See [Hide the limits from the sandbox](../guides/limits.md#hide-the-limits-from-the-sandbox). |

With a command after `--`, rprof starts it once the first limits are in place, with
`RPROF_RUN` set, and stops when it exits. Without a command, rprof stops when the last segment
ends, after `--duration`, or on Ctrl-C.

rprof exits with the command's exit code, or with one of the [exit codes](#exit-codes) below.

```bash
sudo rprof run --target docker:sbx --profile p.yaml -- .venv/bin/python my_harness.py
sudo rprof run --target docker:sbx --mode measure --duration 600      # a baseline with no limits
```

## mark

Records a `mark` event in a running run, from a shell.

```text
rprof mark LABEL [--run RUN_DIR] [--data KEY=VALUE ...]
```

| Option | Description |
| --- | --- |
| `--run` | The run directory. Default: `$RPROF_RUN`. |
| `--data` | Extra data to record. Repeatable. |

## now

Prints the agent view of a running run, as much as the profile's `visibility` allows.

```text
rprof now [--run RUN_DIR] [--json]
```

| Option | Description |
| --- | --- |
| `--run` | The run directory. Default: `$RPROF_RUN`. |
| `--json` | Print the view's data instead of its text |

## report

Rewrites a run's `report.json`, `report.md` and `timeline.jsonl` from its raw data, and prints
the report.

```text
rprof report RUN_DIR [--threshold NAME=VALUE ...] [--memory-basis BASIS] [--json]
```

| Option | Description |
| --- | --- |
| `--memory-basis` | `non_reclaimable` (default) counts memory the kernel can't reclaim, leaving out page cache. `total` counts all memory, page cache included. See [Which memory counts](../guides/results.md#which-memory-counts). |
| `--threshold` | Change a threshold for deciding whether a limit bound: `throttle_frac` (default 0.05), `pressure_frac` (0.05) or `throughput_frac` (0.8). Repeatable. |
| `--json` | Print `report.json` instead of the Markdown |

## timeline

Prints usage for each interval between tool-call starts, ends and limit changes.

```text
rprof timeline RUN_DIR [--json]
```

`--json` prints one JSON object per interval, with every field.

## plot

Draws usage over the run, with limits as step lines, tool calls as spans and failed calls as
markers. Needs `uv sync --extra plot`.

```text
rprof plot RUN_DIR [-o FILE]
```

`-o`, `--out` sets the image file. Default: `<run-dir>/run.png`.

## selftest

Checks, in test containers, that each limit works (enforcement) and that rprof's measurements
of known workloads are right (fidelity). Writes the results for `rprof run` to check against.
See [Check the host](../guides/host-setup.md#check-the-host).

```text
rprof selftest [--quick] [--only PART] [--out FILE] [--image IMAGE]
```

| Option | Description |
| --- | --- |
| `--quick` | Shorter enforcement workloads |
| `--only` | `enforcement` or `fidelity`: run one part and keep the other part's earlier results |
| `--out` | Where to write the results. Default: `/var/lib/rprof/capabilities.json`. |
| `--image` | The image the workloads run in. Default: `rprof-testbox`. Network checks also need `rprof-netpeer`. |

Exits with 1 if any check fails.

## gen

Generates a profile from a pattern and prints it, or writes it with `-o FILE`. Every
generator except `sweep` also takes `--name` and `--visibility`. `sweep` writes files instead
of printing.

| Generator | Usage |
| --- | --- |
| `const` | `rprof gen const --knob KNOB --level VALUE --duration SECONDS` |
| `step` | `rprof gen step --knob KNOB --level VALUE --from SECONDS --to SECONDS` |
| `square` | `rprof gen square --knob KNOB --low VALUE [--high VALUE] --period SECONDS [--duty FRACTION] --duration SECONDS` |
| `random` | `rprof gen random --knobs K1,K2 --levels LEVELS --seed N [--duration S] [--mean S] [--min S] [--max S]` |
| `trace` | `rprof gen trace FILE --knob KNOB --capacity VALUE [--time-scale X] [--merge FRACTION] [--min S] [--format csv\|mahimahi]` |
| `sweep` | `rprof gen sweep BASE --knob KNOB --levels V1,V2 [--from S --to S] [-o DIR]` |

- `square`: `--high` defaults to `max`. `--duty` is the share of each period spent at the low
  level, default 0.5.
- `random`: `--levels` is a list shared by all knobs, such as `512Mi,max`, or one list per
  knob, such as `"mem.max:512Mi,1Gi;cpu.cores:0.5,1"`. Levels a knob can't take are skipped.
  Defaults: 300 s long, windows of 30 s on average, between 5 s and 120 s.
- `trace`: a CSV of `time,usage` rows. The limit is `capacity − usage`. `--format mahimahi`
  reads a Mahimahi link trace for `net.rate`.
- `sweep`: writes one file per level into `-o DIR`. With `--from` and `--to`, the level applies
  in that window. Otherwise it becomes the default.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | A check failed: an invalid profile, or a failed `doctor` or `selftest` check |
| 2 | Wrong command-line usage |
| 70 | Internal error |
| 71 | Target not found |
| 72 | Permission denied: not root, or the cgroup can't be written |
| 73 | The host can't enforce a knob the profile uses. See `--allow-degraded`. |
| 74 | The target is locked by another rprof process |
| 75 | Restoring the original limits failed. Run `rprof reset`. |

`rprof run -- COMMAND` exits with the command's exit code unless rprof itself fails.

## Environment variables

| Variable | Set by | Used by |
| --- | --- | --- |
| `RPROF_RUN` | `rprof run`, for the command it starts | The client, `mark` and `now`, to find the run |
| `RPROF_SOCKET` | `rprof run --harness inside`, or you | The client inside the sandbox, to find the socket |
| `RPROF_HARNESS_TOKEN` | `rprof run --harness inside`, for the command it starts | The client inside the sandbox, to authenticate |
| `DOCKER_HOST`, `DOCKER_CONTEXT` | You | rprof's `docker` commands |
| `RPROF_STATE_DIR` | You | Where rprof keeps capabilities, views and apply state. Default: `/var/lib/rprof`. |
| `RPROF_SOCKET_DIR` | You | Where rprof creates the control socket when the run directory is too deep for a Unix socket path. Default: `/run/rprof/sockets`. |
