# Getting started

In this tutorial you limit a container's CPU and memory by hand and watch the kernel enforce
the limits. Then you record a timed run and read its report. It takes about 10 minutes.

## Before you begin

You need a Linux machine with:

- **cgroup v2.** Check with `stat -fc %T /sys/fs/cgroup`. It must print `cgroup2fs`.
- **The standard Docker daemon**, which runs as root. Rootless Docker is not supported.
- **Python 3.11 or later**, and [uv](https://docs.astral.sh/uv/).
- **sudo.** rprof changes kernel settings, so most of its commands need root.

> [!NOTE]
> If your user also has a rootless Docker, your `docker` commands go to it by default, while
> `sudo rprof` uses the standard daemon. Run `export DOCKER_CONTEXT=default` in each terminal
> so both see the same containers.

## 1. Install rprof

```bash
git clone https://github.com/ronyw7/rprof.git && cd rprof
uv sync --extra plot
sudo ln -sf "$PWD/.venv/bin/rprof" /usr/local/bin/rprof
sudo rprof doctor
```

The `ln` command makes `rprof` available everywhere, including under `sudo`, which doesn't
see your virtual environment. The rest of the docs assume you ran it.

`doctor` checks that the machine can run rprof:

```text
ok    cgroup v2 mounted at /sys/fs/cgroup unified hierarchy
ok    running as root
ok    controller cpu                     enabled at root
...
warn  memory.peak per-fd reset           kernel 6.8.0-85-generic < 6.12: mem.peak omitted, ...
ok    docker daemon                      29.1.3, cgroup driver systemd
```

Each line starts with `ok`, `warn` or `FAIL`. Warnings are fine for this tutorial. A `FAIL`
names what is missing.

## 2. Start a sandbox

Build the test image and start a container from it. In these docs, the container that rprof
limits is called the *sandbox*.

```bash
docker build -t rprof-testbox images/testbox
docker run -d --name sbx --cgroup-parent=rprof-sbx.slice rprof-testbox sleep infinity
```

The test image includes two programs you will use:

- `stress-ng --cpu N` keeps N CPU cores busy.
- `hog-mem SIZE SECONDS` allocates SIZE of memory, for example `1G`, and holds it for SECONDS.

Linux limits resources through *cgroups*: groups of processes, arranged in a tree, that the
kernel limits and measures together. Docker gives each container its own cgroup.
`--cgroup-parent` puts this container's cgroup under a parent named `rprof-sbx.slice`. It
isn't required, but it keeps sandboxes apart from your other containers.

## 3. Watch the sandbox

Open a second terminal on the same machine and run:

```bash
sudo rprof watch --target docker:sbx
```

`--target docker:sbx` tells rprof which container to look at. The table refreshes twice a
second. These columns matter for this tutorial:

| Column | Shows |
| --- | --- |
| `cpu` | CPU cores in use / the CPU limit |
| `thr` | Share of time the CPU limit held the container back |
| `mem` | Memory in use |
| `high/max` | The soft and hard memory limits |
| `oom` | How many processes the kernel has killed for using too much memory |
| `pids` | Processes and threads / the process limit |

Leave `watch` running.

## 4. Run work with no limits

Back in the first terminal, run some CPU work and then some memory work:

```bash
docker exec sbx stress-ng --cpu 2 --timeout 15s
docker exec sbx hog-mem 1G 10
```

In `watch`, `cpu` shows about `2.00 / max` while `stress-ng` runs. Then `mem` climbs to about
1 GiB and drops back after 10 seconds. Nothing is limited yet.

## 5. Limit CPU, memory and processes

Apply three limits:

```bash
sudo rprof apply --target docker:sbx cpu.cores=0.5 mem.max=512Mi pids.max=20
```

```text
applied to docker:sbx (/rprof.slice/rprof-sbx.slice/docker-8a4ebac6be97….scope): cpu.cores=0.5, mem.max=512Mi, pids.max=20
```

Each `name=value` pair sets one limit. rprof calls these settings *knobs*. Here, the sandbox
may use half a CPU core, 512 MiB of memory, and 20 processes. Now run the same work again:

```bash
docker exec sbx stress-ng --cpu 2 --timeout 15s
docker exec sbx hog-mem 1G 10; echo "exit code $?"
docker exec sbx sh -c 'for i in $(seq 50); do sleep 30 & done'
```

| Command | What happens | What `watch` shows |
| --- | --- | --- |
| `stress-ng` | Still runs for 15 s, but gets only half a core | `cpu` about `0.50 / 0.50`; `thr` high |
| `hog-mem 1G 10` | Killed at once; prints `exit code 137` | `oom` goes to 1; `mem` never passes 512 MiB |
| 50 × `sleep` | Stops with `Cannot fork` | `pids` at `20 / 20` |

The three limits act differently. The CPU limit only slows the work down: the kernel
*throttles* the sandbox, pausing its processes once they have used their share of CPU time.
The memory limit kills the process that needs more. The process limit stops new processes
from starting. This difference matters when you design experiments: some limits slow a tool
call, others make it fail. [Choose limits](guides/limits.md) covers every knob this way.

> [!NOTE]
> When you set a memory limit, rprof also sets the sandbox's swap allowance to zero, unless you
> set `mem.swap_max` yourself. Otherwise the sandbox would swap instead of being killed.

Limits set with `apply` stay until you remove them. Remove them now:

```bash
sudo rprof reset --target docker:sbx
```

rprof prints `restored docker:sbx`. The container's limits are back to what they were before
step 5.

## 6. Record a timed run

In an experiment you don't set limits by hand. You write a *profile*: a YAML file that
schedules limits over time. Create one:

```bash
cat > /tmp/demo.yaml <<'EOF'
version: 1
name: cpu-mem-demo
visibility: full
defaults:
  cpu: {cores: 2}
segments:
  - {from: 10, to: 25, cpu: {cores: 0.5}}
  - {from: 25, to: 40, mem: {max: 512Mi}}
EOF
rprof validate /tmp/demo.yaml
```

```text
OK · cpu-mem-demo · wall clock · 2 segments · 0–40 s · visibility full
segment  from  to    changes
1        10 s  25 s  cpu.cores 0.5
2        25 s  40 s  mem.max 512Mi
outside segments: defaults (cpu.cores 2)
```

The profile gives the sandbox 2 CPU cores by default. Each entry under `segments` changes some
limits for a window of time, in seconds since the run started. Segment 1, from 10 s to 25 s,
gives the sandbox half a core. Segment 2, from 25 s to 40 s, caps its memory at 512 MiB. When
no segment is active, rprof reports *segment 0*: only the defaults apply. `visibility: full`
lets the agent see the whole schedule. You'll see what that looks like in a moment.

Now run the profile. An experiment would launch your agent's harness here: the program that
runs the agent and its tool calls. This tutorial uses `examples/fake_harness.py`, which runs
fixed commands in the sandbox at fixed times and reports each one to rprof:

```bash
sudo rprof run --target docker:sbx --profile /tmp/demo.yaml --runs-dir ~/rprof-runs -- \
  .venv/bin/python examples/fake_harness.py sbx \
  "2:stress-ng --cpu 2 --timeout 15s" "27:hog-mem 1G 5" "33:hog-mem 256M 3"
```

Everything after `--` is the command rprof starts once the first limits are in place. Use the
virtual environment's Python, because the harness imports rprof's client. The run ends when
the command exits. Each `"seconds:command"` argument asks the fake harness to start a command
at that time. The first one starts before the CPU limit drops at 10 s and keeps running
through the change.

If `watch` is still open, you can see CPU fall from 2 cores to half a core at 10 s, and the
memory kill at 27 s. The fake harness first prints what the agent may see about its limits.
Then, for each command, it prints the segment it ran in and what rprof said about it. rprof
calls each command a *tool call*:

```text
view: t = 0 s · defaults (2 segments in profile)
now:  cpu 2 cores
next: at 10 s → cpu 0.5 cores · at 25 s → memory 512 MiB hard · at 40 s → back to defaults

c1 seg=0 exit=0 cause=None explain=None
c2 seg=2 exit=137 cause=memory explain=Killed: memory limit 512 MiB reached (segment 2, 25–40 s).
c3 seg=2 exit=0 cause=None explain=None
rprof: run 2026-10-02T1803-cpu-mem-demo ended (command_exit); results in /home/you/rprof-runs/2026-10-02T1803-cpu-mem-demo
```

Call `c2` failed, and rprof worked out why. A harness can pass that explanation to the agent.

## 7. Read the results

The run wrote a directory under `~/rprof-runs`. Print its timeline:

```bash
R=$(ls -td ~/rprof-runs/*/ | head -1)
rprof timeline $R
```

```text
t (s)      segment  limits         running calls  cpu  mem peak  io write  events
0.0–2.0    0        defaults       -              0.0  3.4 MiB   0
2.0–10.0   0        defaults       c1 stress-ng   2.0  11.6 MiB  0
10.0–17.1  1        cpu.cores 0.5  c1 stress-ng   0.5  11.1 MiB  0
17.1–25.0  1        cpu.cores 0.5  -              0.0  3.6 MiB   0
25.0–27.0  2        mem.max 512Mi  -              0.0  3.4 MiB   0
27.0–27.4  2        mem.max 512Mi  c2 hog-mem     0.8  434 MiB   0         oom_kill: 1; mem_max: 27; failed: c2
27.4–33.0  2        mem.max 512Mi  -              0.0  1.2 MiB   0         mem_max: 8
33.0–36.3  2        mem.max 512Mi  c3 hog-mem     0.1  262 MiB   0
```

A new row starts whenever a tool call starts or ends, or a limit changes. Call `c1` used 2
cores until the limit dropped at 10 s, then half a core. The `mem_max` events count the times
the sandbox hit its memory limit.

Then read the report:

```bash
cat $R/report.md
```

For each segment and limit, the report says whether the limit *bound*, meaning it actually
constrained the workload, and gives the evidence:

- In segment 1, the half-core limit bound: it held the sandbox back for 71% of the segment.
- In segment 2, the memory limit bound: the kernel killed one process.
- The 2-core default never bound, so segment 0 is marked *no effect*. Here that is expected,
  because segment 0 is the unconstrained part of the run. In an experiment, a segment with no
  effect tested nothing.

To see the run as a picture, run `rprof plot $R -o run.png`.

## 8. Clean up

```bash
docker rm -f sbx
```

## Next steps

- [Key concepts](key-concepts.md) explains targets, knobs, profiles and the other terms used here.
- [Write a profile](guides/writing-profiles.md) shows everything a profile can schedule.
- [Connect your harness](guides/harness.md) shows how to report your own agent's tool calls.
- [Choose limits](guides/limits.md) explains what each limit does to a tool call.
