# Read the results

Every run writes a directory of results, by default under `runs/`. This guide shows how to
answer the usual questions from it: did each limit bind, what did each tool call experience,
and how far did usage go. The [run directory reference](../reference/run-directory.md) lists
every file and field.

| File | Contains |
| --- | --- |
| `report.md`, `report.json` | Whether each limit bound in each segment, and the outcome of each tool call |
| `timeline.jsonl` | Usage for each stretch of time between tool-call starts, ends and limit changes |
| `samples.jsonl` | Raw kernel counters, ten lines per second |
| `events.jsonl` | Limit changes, tool calls, marks, warnings and errors, in time order |
| `meta.json` | The host, the target, the rprof version and the run's settings |
| `profile.yaml` | The profile as applied, with every default filled in |

## Did each limit bind?

Open `report.md`. Its table has one row for each limit in each segment. Shortened, from the
[getting started](../getting-started.md) run:

```text
| seg | window (s) | knob      | limit | mean   | p95     | max     | bound   | evidence |
| 0   | 0–10       | cpu.cores | 2     | 1.58   | 2.00    | 2.01    | no      | throttled_frac=0.0014, throttled_periods_frac=0.3088, cpu_pressure=0.0008 |
| 1   | 10–25      | cpu.cores | 0.5   | 0.25   | 0.52    | 1.99    | **yes** | throttled_frac=0.7082, throttled_periods_frac=0.972, cpu_pressure=0.3541 |
| 2   | 25–36.3    | mem.max   | 512Mi | 78 MiB | 262 MiB | 434 MiB | **yes** | mem_max_events=35, oom_kill=1 |
```

`mean`, `p95` and `max` summarize usage during the segment, in the limit's own unit. `bound`
says whether the limit actually constrained the workload. `evidence` holds the measurements
behind that decision:

| Evidence | Meaning |
| --- | --- |
| `throttled_frac` | Share of the segment during which the CPU limit held the sandbox back |
| `throttled_periods_frac` | Share of scheduling periods in which the sandbox used up its CPU allowance, even briefly |
| `cpu_pressure`, `mem_pressure`, `io_pressure` | Share of the segment during which some process waited for that resource |
| `mem_high_events`, `mem_max_events` | Times the sandbox went over the soft limit, or hit the hard limit |
| `oom_kill` | Processes the kernel killed for using too much memory |
| `pids_max_events` | Processes or threads that couldn't be created |
| `throughput_frac` | Peak throughput over one second, as a share of the limit |
| `qdisc_drops`, `partition_hits` | Packets rprof dropped, or rejected or dropped by a partition |
| `min_free_bytes`, `enospc_calls` | The least free space seen, and calls that failed for lack of space |

In segment 0, the sandbox reached its 2-core allowance in 31% of the periods, but only for
moments. It was held back for 0.14% of the time, so the limit did not bind.

The rule depends on the limit. Several rules use *pressure*: the share of time in which at
least one process in the sandbox was waiting for a resource. The kernel reports it as PSI
(pressure stall information).

| Knob | Counts as bound when |
| --- | --- |
| `cpu.cores` | `throttled_frac` or `cpu_pressure` was at least 5% |
| `cpu.cpus` | CPU pressure was at least 5% |
| `mem.high` | The kernel reported exceeding the soft limit, or memory pressure was at least 5% |
| `mem.max` | The kernel reported hitting the hard limit, or killed a process |
| `io.*` | I/O pressure was at least 5% and throughput reached 80% of the limit |
| `pids.max` | A process or thread could not be created |
| `net.rate` | Throughput reached 80% of the rate in either direction |
| `net.loss` | rprof dropped at least one packet |
| `net.partition` | rprof rejected or dropped at least one packet |
| `disk.capacity` | Free space fell to 1 MiB or less, or a tool call failed with `cause: disk` |

`net.delay` and `net.jitter` slow every packet. They describe the environment rather than a
budget, so the report lists them without a bound decision.

### Which memory counts

The kernel's memory figure for a cgroup includes the page cache: file data kept in memory
after it was read or written. Under a memory limit, the kernel drops that cache instead of
failing, so counting it would make a task that merely reads a large file look like it needs
that much memory.

The report therefore counts memory the kernel *can't* reclaim: total memory, minus page cache,
plus shared memory and tmpfs files, which can't be dropped without swap. The memory rows'
`basis` field says `non_reclaimable`, and `usage_total` gives the figures with page cache
included. To count everything, regenerate the report with `--memory-basis total`. Runs recorded
before rprof sampled shared memory use `non_reclaimable_without_shmem`, which leaves tmpfs data
out.

A segment in which no limit bound is marked *no effect*. It tested nothing, so tighten its
levels. To change the thresholds, regenerate the report:

```bash
rprof report runs/<run-id> --threshold pressure_frac=0.1 --threshold throughput_frac=0.9
```

## What did each tool call experience?

`report.md` ends with a table of tool calls. Shortened:

```text
| call | cmd                             | start (s) | duration (s) | segments | exit | cause  |
| c1   | stress-ng --cpu 2 --timeout 15s | 2.0       | 15.1         | 0, 1     | 0    | -      |
| c2   | hog-mem 1G 5                    | 27.0      | 0.4          | 2        | 137  | memory |
```

`segments` lists every segment the call ran in. Call `c1` started under the defaults and was
still running when segment 1 began. `report.json` also records the limits in force when each
call started, so you can compare two runs call by call even if the calls happened at
different times.

For each call, `report.json` also gives its peak memory, with and without page cache
(`mem_peak_bytes`, `mem_peak_nonreclaimable_bytes`), the tightest `mem.max` in force while it
ran (`mem_max_bytes`), and `mem_fit`: whether the call's non-reclaimable peak stayed under that
limit. In measure mode this answers "would this command have fit under the profile?" directly.
Peaks come from the samples, so a spike shorter than one sample can be missed on kernels older
than 6.12. Calls that overlap share their peaks.

For usage over time, print the timeline:

```bash
rprof timeline runs/<run-id>
```

```text
t (s)      segment  limits         running calls  cpu  mem peak  io write  events
2.0–10.0   0        defaults       c1 stress-ng   2.0  11.6 MiB  0
10.0–17.1  1        cpu.cores 0.5  c1 stress-ng   0.5  11.1 MiB  0
27.0–27.4  2        mem.max 512Mi  c2 hog-mem     0.8  434 MiB   0         oom_kill: 1; failed: c2
```

A new row starts whenever a tool call starts or ends or a limit changes. So within a row, the
same calls are running under the same limits. A call that ran alone gets exact numbers. Calls
that overlap share their rows' usage. `rprof timeline --json` prints every field.

## Plot runs

`rprof plot` draws figures in the style of systems papers: Times type, one panel per metric,
each limit as a step line. Give it several runs to compare them, for example an unconstrained
baseline and the same workload under a profile:

```bash
rprof plot runs/<baseline> runs/<squeezed> --label Unconstrained --label Constrained
```

This writes `plot.pdf` in the first run's directory: one row of panels, four of which fill a
two-column page. The other layouts are:

| Option | Draws |
| --- | --- |
| `--row` | All metrics side by side in one row (the default) |
| `--paper` | Each metric as its own single-column figure, `<metric>.pdf`, in a directory |
| `--dashboard` | One run's debugging view: every metric, the tool calls as spans and failed calls as markers |

`--style classic` (the default) draws thin lines, hollow markers and a dotted grid, like
gnuplot figures. `--style bold` draws bold labels, filled markers and a boxed legend.

By default the figure shows CPU, memory, disk writes and data sent over the network, plus a
panel for any other limit an enforced run set, such as processes for `pids.max`. Choose the
panels yourself with `--metrics`, for example `--metrics cpu,cpu-stall,memory`.
`rprof plot --list-metrics` lists them all, including CPU throttling, swap, disk reads, IOPS,
data received, TCP retransmits and pressure stalls.

Values are averaged over half-second bins. Bandwidth that spans several orders of magnitude,
such as an unconstrained run next to a 10 Mbit/s limit, gets a log axis. PDFs embed their fonts
as TrueType, which camera-ready checks require. Plotting needs the `plot` extra:
`uv sync --extra plot`.

## How far did usage go over the limits?

In `--mode measure`, rprof writes no limits, so nothing binds. The report shows instead how
much usage exceeded each limit:

```text
| seg | window (s) | knob    | limit | mean    | p95     | max     | over limit (time) | peak over |
| 1   | 3–6.8      | mem.max | 256Mi | 99 MiB  | 608 MiB | 608 MiB | 15%               | 352 MiB   |
```

`over limit (time)` is the share of the segment during which usage was above the limit.
`peak over` is how far above the limit usage went at its highest. Memory is counted as
described in [Which memory counts](#which-memory-counts). For `net.partition`, any traffic
counts as over the limit.

## Work with the raw samples

`samples.jsonl` stores raw kernel counters, one JSON object per line. Counters only grow, so
compute rates from differences between lines. For example, CPU cores used over time:

```python
import json

with open("runs/<run-id>/samples.jsonl") as f:
    samples = [json.loads(line) for line in f]

for a, b in zip(samples, samples[1:]):
    cores = (b["cpu"]["usage_usec"] - a["cpu"]["usage_usec"]) / 1e6 / (b["t"] - a["t"])
    print(f"{b['t']:7.2f}  {cores:5.2f} cores  calls={b['running_calls']}")
```

Each line carries `running_calls`, the tool calls in progress at that sample. Fields the host
can't provide are left out of a line rather than set to `null`.
