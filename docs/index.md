# rprof documentation

rprof changes the CPU, memory, disk, network and process limits of a running Docker container
on a schedule you write, and records what the container used while each limit was in force.

Use it to test how an AI agent copes when its sandbox becomes slower or smaller partway through
a task. Does the agent notice, wait, reorder its tool calls, and still finish with the right
result?

## How it works

You give rprof two things:

- a **target**: the container to limit;
- a **profile**: a YAML file that says which limits apply at which second of the run.

Then you start your agent's *harness*, the program that runs the agent and executes its tool
calls, through rprof:

```bash
sudo rprof run --target docker:sbx --profile squeeze.yaml -- .venv/bin/python my_harness.py
```

While the harness runs, three things happen:

1. **Limits change on a timer.** At each time the profile names, rprof writes the new limits
   into the container's cgroup, the Linux kernel's resource-control group for its processes.
   From then on, the kernel enforces them.
2. **Usage is sampled.** Ten times a second, rprof reads how much CPU, memory, disk I/O,
   network and processes the container is using.
3. **Tool calls are labeled.** Your harness tells rprof when each tool call starts and ends.
   rprof attaches the calls to the usage samples. When a call fails, it explains why, for
   example `Killed: memory limit 512 MiB reached`.

When the harness exits, rprof puts back the container's original limits. It then writes a
report that says, for each part of the schedule, whether each limit actually constrained the
workload.

```text
   squeeze.yaml                          my_harness.py
   (limits over time)                    (runs the agent's tool calls)
        │                                     │ tool started / tool ended
        ▼                                     ▼
 ┌───────────────────────────── rprof run ───────────────────────────────┐
 │  scheduler: writes limits on a timer     sampler: reads usage 10×/s   │
 └───────────────┬──────────────────────────────────────┬────────────────┘
                 ▼                                      ▼
   the container's cgroup                       runs/<run-id>/
   (the kernel enforces the limits)             samples, events, report
```

## Why rprof

- **Reproducible.** A run is defined by its profile file. The same file gives the same schedule
  of limits every time.
- **Limits change on the clock, not on the agent's actions.** A limit can change in the middle
  of a tool call, the way real contention would.
- **It tells you whether a limit mattered.** A limit the workload never reached is flagged *no
  effect*, so you know when an experiment tested nothing.
- **It works with any agent.** rprof limits whatever runs in the container and needs only two
  messages per tool call from your harness.
- **It cleans up.** Original limits are restored when the run ends, on Ctrl-C, and, with one
  command, after a crash.

## Learn more

| Page | Read it to |
| --- | --- |
| [Getting started](getting-started.md) | Limit a container's CPU and memory and see the effect, in about 10 minutes |
| [Key concepts](key-concepts.md) | Learn the terms the rest of the docs use |
| **User guides** | |
| [Write a profile](guides/writing-profiles.md) | Schedule limits over time, check a profile, generate profiles |
| [Choose limits](guides/limits.md) | Understand what each limit does to a tool call, from slowing it to failing it |
| [Connect your harness](guides/harness.md) | Report tool calls, enforce deadlines, explain failures, show the agent its limits |
| [Run Harbor tasks](guides/harbor.md) | Run a Harbor or Terminal-Bench trial under a profile with one command |
| [Read the results](guides/results.md) | Find out which limits bound, what each call experienced, and how far usage went |
| [Set up a host](guides/host-setup.md) | Check a machine, launch sandboxes, recover after a crash, fix common problems |
| **Reference** | |
| [CLI](reference/cli.md) | Every command and option, exit codes, environment variables |
| [Profile format](reference/profile.md) | Every field and knob, value formats and defaults |
| [Python client](reference/client.md) | The client a harness uses to report tool calls |
| [Control protocol](reference/protocol.md) | The socket messages between a harness and rprof, for other languages |
| [Run directory](reference/run-directory.md) | Every file a run writes and every field in it |
