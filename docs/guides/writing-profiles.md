# Write a profile

A profile is a YAML file that schedules limits over time. This guide builds a profile step by
step, explains the rules rprof checks, and shows how to generate profiles instead of writing
them by hand. Every field is listed in the [profile format reference](../reference/profile.md).

## Start with one limit

The smallest useful profile limits one resource for one window of time:

```yaml
version: 1
name: cpu-squeeze
segments:
  - {from: 60, to: 120, cpu: {cores: 0.5}}
```

For the first 60 seconds of the run, the CPU is not limited. From 60 s to 120 s, the sandbox
may use half a CPU core. After 120 s, the limit is lifted again.

Every profile needs `version: 1` and a `name`. A name may use letters, digits, `.`, `_` and
`-`, must start with a letter or digit, and may be up to 64 characters long.

## Set defaults

`defaults` holds the limits that apply whenever no segment says otherwise. Use it to set a
baseline for the whole run:

```yaml
version: 1
name: cpu-squeeze
defaults:
  cpu: {cores: 4}
segments:
  - {from: 60, to: 120, cpu: {cores: 0.5}}
```

Now the sandbox has 4 cores for the whole run, except from 60 s to 120 s, when it has half a
core. Any knob you leave out of `defaults` has no limit.

## Add segments

Each segment has a start time, an end time, and the knobs it changes. Times are seconds since
the run started.

```yaml
segments:
  - {from: 60, to: 120, label: memory squeeze, mem: {high: 800Mi, max: 1Gi}, pids: {max: 16}}
  - {from: 120, to: 150, net: {loss: 30%}}
  - {from: 150, to: 180, net: {partition: reject}}
```

- A segment covers `from` up to, but not including, `to`. Segments that meet end to end, such
  as `60–120` and `120–150`, do not overlap.
- `label` is optional. It appears in reports.
- When a segment ends, its knobs go back to their defaults.

Segments may overlap only if they change different knobs. Two overlapping segments that both
set the same knob would leave the value ambiguous, so `validate` rejects them:

```text
segments[1]: overlaps segments[0] (0–60 s) and both set mem.max
```

## Write values

Values are absolute amounts, not percentages of what the machine has. This keeps a profile's
meaning the same on every host.

| Kind | Examples | Notes |
| --- | --- | --- |
| Bytes | `512Mi`, `1Gi`, `2G`, `1048576` | `Ki`, `Mi`, `Gi`, `Ti` are powers of 1024. `K`, `M`, `G`, `T` are powers of 1000. |
| Disk I/O rate | `20Mi` | Bytes per second |
| Network rate | `10mbit`, `500kbit`, `1gbit` | Bits per second, in the units of `tc`, the Linux traffic-control tool |
| Duration | `100ms`, `30s`, `5m` | A unit is required |
| Percentage | `30%` | The `%` sign is required |
| Count | `16` | Processes, or I/O operations per second |
| No limit | `max` | For any knob that is a limit |

## Know which settings rprof changes

rprof changes only the resources your profile mentions. If the profile sets any `mem.*` knob,
in `defaults` or in a segment, rprof controls every `mem.*` setting for the run. The ones you
leave out get their defaults: no limit, except that swap is set to 0. The same goes for
`cpu.*`, `io.*` and the other groups. Resources the profile never mentions keep the
container's own settings.

For example, a profile that sets only `mem.max` also sets `mem.high` to no limit and
`mem.swap_max` to 0. It does not touch CPU, so a `--cpus` limit you gave `docker run` stays in
place.

## Let the agent see the schedule

`visibility` decides how much the agent may learn about its limits. The default is `none`.

```yaml
visibility: full
```

Here is what the agent sees 84 seconds into a run of `profiles/examples/mem-squeeze-mid.yaml`,
which has four segments. With `visibility: current`, it sees only the limits in force now:

```text
t = 84 s
now:  memory 1 GiB hard (800 MiB soft) · max 16 processes · cpu 4 cores · disk not throttled
```

With `visibility: full`, it also sees which segment it is in and the next three changes:

```text
t = 84 s · segment 1 of 4
now:  memory 1 GiB hard (800 MiB soft) · max 16 processes · cpu 4 cores · disk not throttled
next: at 120 s → network loss 30% · at 150 s → network blocked (reject) · at 180 s → cpu 0.5 cores
```

[Show the agent its limits](harness.md#show-the-agent-its-limits) explains where this text
appears.

`visibility` controls only what rprof tells the agent. The sandbox can still read its real
limits from `/sys/fs/cgroup` unless you run with `--hide-limits`. See
[Hide the limits from the sandbox](limits.md#hide-the-limits-from-the-sandbox).

## Pass a deadline and feedback level to the harness

The `harness` group is not enforced by rprof. Your harness receives these values at the start
of every tool call and applies them itself:

```yaml
defaults:
  harness: {deadline: 300s, feedback: errno}
segments:
  - {from: 180, to: 270, cpu: {cores: 0.5}, harness: {deadline: 30s}}
```

- `deadline` is how long the harness should let a tool call run before stopping it. Use it
  with CPU, disk I/O and soft memory (`mem.high`) limits, which slow calls down rather than
  failing them.
- `feedback` is how much the agent is told when a call fails: `none` (raw output only),
  `errno` (also the exit code) or `explain` (also rprof's explanation of the cause).

## Check a profile

```bash
rprof validate cpu-squeeze.yaml
```

`validate` checks the structure, the units and the overlap rule, then prints the schedule.
It exits with code 0 if the profile is valid. Otherwise it exits with code 1 and prints one
problem per line, with the location of the problem first:

```text
segments[0].mem.max: 1Gx is not a byte size
segments[1].cpu.cors: unknown key
```

Locations count list items from 0, so `segments[0]` is the first segment in the file, which
the reports call segment 1.

Unknown keys are errors, so a typo can never silently do nothing.

Add `--target` to also check that the host can enforce every knob the profile uses:

```bash
sudo rprof validate cpu-squeeze.yaml --target docker:sbx
```

## Generate profiles

For families of experiments, `rprof gen` writes profiles from a pattern. The output is an
ordinary profile file that you can read and edit.

| Generator | Produces | Example |
| --- | --- | --- |
| `const` | One limit for the whole run | `rprof gen const --knob cpu.cores --level 0.5 --duration 300` |
| `step` | One squeeze window | `rprof gen step --knob mem.max --level 512Mi --from 60 --to 120` |
| `square` | A limit that switches on and off | `rprof gen square --knob cpu.cores --low 0.5 --period 60 --duty 0.3 --duration 300` |
| `random` | Random windows for each knob | `rprof gen random --knobs mem.max,cpu.cores --levels "mem.max:512Mi,1Gi;cpu.cores:0.5,1" --seed 7` |
| `trace` | Limits from a recorded usage trace | `rprof gen trace neighbour.csv --knob cpu.cores --capacity 8` |
| `sweep` | One profile per level of a knob | `rprof gen sweep base.yaml --knob mem.max --levels 2Gi,1Gi,512Mi --from 60 --to 120 -o sweep/` |

Most generators print the profile. Pass `-o FILE` to write a file instead. `sweep` always
writes one file per level, into the directory given with `-o`.

- `random` draws window lengths from an exponential distribution and picks a level for each
  window. The same seed and rprof version produce the same file, byte for byte.
- `trace` reads a CSV of `time,usage` rows, for example a neighbouring container's CPU use, and
  limits the sandbox to `capacity − usage`. With `--format mahimahi` it reads a network
  bandwidth recording in the format of the Mahimahi network emulator, for `net.rate`.
- `sweep` is how you tune a limit from no effect to hard failure: run the family and see where
  the report starts marking the segment as bound.

## Example profiles

The repository includes profiles to start from:

| File | What it does |
| --- | --- |
| `profiles/unlimited.yaml` | No limits. Use with `--mode measure` for a baseline run. |
| `profiles/examples/mem-squeeze-mid.yaml` | A memory squeeze, then packet loss, a network outage and a CPU squeeze |
| `profiles/examples/cpu-square-wave.yaml` | CPU drops to half a core for 20 s of every 60 s |
| `profiles/examples/net-outage.yaml` | A slow link, then packet loss, then a full outage |
| `profiles/examples/io-throttle.yaml` | Disk write and read throttling |
| `profiles/examples/disk-filling.yaml` | Free disk space shrinks in steps until writes fail |
