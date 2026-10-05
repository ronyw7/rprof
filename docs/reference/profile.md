# Profile format reference

A profile is a YAML file. Its JSON Schema is `src/rprof/profile/schema.json`. For a
walkthrough, see [Write a profile](../guides/writing-profiles.md).

```yaml
version: 1
name: mem-squeeze-mid
clock: wall
visibility: full
source: {generator: random, seed: 7}
defaults:
  cpu:  {cores: 4, period: 100ms}
  mem:  {high: max, max: max, swap_max: 0}
  harness: {deadline: 300s, feedback: errno}
segments:
  - {from: 60, to: 120, mem: {high: 800Mi, max: 1Gi}, pids: {max: 16}}
  - {from: 120, to: 150, net: {loss: 30%}}
  - {from: 150, to: 180, net: {partition: reject}}
  - {from: 180, to: 270, cpu: {cores: 0.5}, harness: {deadline: 30s}}
```

Unknown keys are errors.

## Top-level fields

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `version` | Yes | | Must be `1` |
| `name` | Yes | | Letters, digits, `.`, `_` and `-`, starting with a letter or digit, at most 64 characters. Used in run directory names. |
| `clock` | No | `wall` | Segment times are seconds since the run started. `wall` is the only clock. |
| `visibility` | No | `none` | How much the agent may see: `none`, `current` or `full` |
| `source` | No | `{}` | Free-form notes on where the profile came from. rprof doesn't read it. |
| `defaults` | No | No limits | Knob values used wherever no segment sets them |
| `segments` | No | `[]` | Time windows that override defaults |

## Segment fields

| Field | Required | Description |
| --- | --- | --- |
| `from` | Yes | Start time in seconds, 0 or more. Included in the segment. |
| `to` | Yes | End time in seconds, greater than `from`. Not included in the segment. |
| `label` | No | A name shown in reports |
| `cpu`, `mem`, `io`, `pids`, `net`, `disk`, `harness` | No | Knob groups, as in the table below |
| `unified` | No | Raw cgroup v2 files to write, as `{file name: value}` |

Segments are numbered from 1 in file order. Two segments may overlap only if they set
different knobs. When a segment ends, its knobs return to the defaults.

`defaults` takes the same knob groups and `unified` map as a segment.

## Knobs

| Knob | Values | Default | Kernel interface |
| --- | --- | --- | --- |
| `cpu.cores` | Number above 0, or `max` | `max` | `cpu.max` quota = cores × period |
| `cpu.cpus` | CPU list such as `0-3,6`, or `all` | `all` | `cpuset.cpus` |
| `cpu.period` | Duration from `1ms` to `1s` | `100ms` | `cpu.max` period |
| `mem.high` | Bytes, or `max` | `max` | `memory.high` |
| `mem.max` | Bytes, or `max` | `max` | `memory.max` |
| `mem.swap_max` | Bytes (0 allowed), or `max` | `0` | `memory.swap.max` |
| `io.rbps`, `io.wbps` | Bytes per second, or `max` | `max` | `io.max` on the disk under the data volume |
| `io.riops`, `io.wiops` | Operations per second, or `max` | `max` | `io.max` on the disk under the data volume |
| `pids.max` | Integer 1 or more, or `max` | `max` | `pids.max` |
| `net.rate` | tc rate such as `10mbit`, or `max` | `max` | netem rate, on both ends of the container's network link |
| `net.delay` | Duration | `0ms` | netem delay on outgoing traffic |
| `net.jitter` | Duration | `0ms` | netem delay variation on outgoing traffic |
| `net.loss` | Percentage from `0%` to `100%` | `0%` | netem loss on outgoing traffic |
| `net.partition` | `none`, `reject` or `drop` | `none` | iptables rules in the container's network namespace |
| `net.allow` | List of CIDRs such as `[172.18.0.0/16]` | `[]` | Address ranges exempt from all network knobs |
| `disk.capacity` | Bytes, or `max` | `max` | A ballast file on the data volume |
| `harness.deadline` | Duration, or `none` | `none` | Not enforced. Returned to the harness. |
| `harness.feedback` | `none`, `errno` or `explain` | `errno` | Not enforced. Returned to the harness. |

[Choose limits](../guides/limits.md) describes what each knob does to a tool call.

**Which knobs rprof manages.** If a profile sets any knob of a group, in `defaults` or in a
segment, rprof manages every knob of that group. Knobs of the group the profile leaves out get
the defaults above. rprof doesn't touch groups the profile never mentions. The `harness` group
is never written to the system.

## Value formats

| Kind | Format | Examples |
| --- | --- | --- |
| Bytes | Integer with an optional suffix. `Ki`, `Mi`, `Gi`, `Ti` are powers of 1024; `K`, `M`, `G`, `T` are powers of 1000. | `536870912`, `512Mi`, `2G` |
| Duration | Number followed by `ms`, `s` or `m` | `100ms`, `30s`, `1.5m` |
| Percentage | Number followed by `%` | `30%`, `0.5%` |
| tc rate | Number followed by a `tc` unit: `bit`, `kbit`, `mbit`, `gbit` (bits per second), `bps`, `kbps`, `mbps` (bytes per second), or the binary forms such as `mibit` | `10mbit`, `500kbit` |
| No limit | `max` for limits, `none` for `harness.deadline` | |

Anything else is an error. For example, `30` is not a percentage and `100` is not a duration.

## Validation

`rprof validate` checks, in order:

1. The file is YAML and has the fields above, with no unknown keys.
2. Every value has the right format.
3. Every segment's `to` is greater than its `from`.
4. No two overlapping segments set the same knob.
5. `cpu.cores × cpu.period` is at least 1 ms, the smallest quota the kernel accepts.
6. With `--target`, the host can enforce every knob the profile sets to a limiting value.

Each problem is printed on its own line as `<location>: <problem>`.
