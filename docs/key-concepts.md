# Key concepts

This page explains the terms used throughout the rprof docs. Each section ends with a link to
the guide that covers the topic in depth.

## Target

The target is the set of processes that rprof limits and measures. Usually it is one Docker
container, written `docker:<name>`. The container is also called the *sandbox*.

The target can also be a cgroup, written `cgroup:<path>`. A cgroup is a group of processes
that the Linux kernel limits and measures together. Each container has one, and a parent
cgroup can hold several containers, which lets you treat them as one sandbox. rprof doesn't
need to know what runs inside the target. It limits whatever is there.

See [Set up a host](guides/host-setup.md).

## Knobs

A knob is one adjustable limit, such as `cpu.cores` (how many CPU cores the target may use) or
`mem.max` (how much memory it may use). Knobs are grouped by resource: `cpu`, `mem`, `io`,
`pids`, `net` and `disk`. Each knob maps to a mechanism in the Linux kernel. rprof sets the
value, and the kernel enforces it. Knobs that set a limit default to no limit. A few knobs set
a condition instead, such as the scheduling period `cpu.period` or the swap allowance
`mem.swap_max`, and have their own defaults.

Two more knobs, `harness.deadline` and `harness.feedback`, are not enforced by rprof. rprof
passes them to your harness, which decides what to do with them.

See [Choose limits](guides/limits.md).

## Profile and segments

A profile is a YAML file that schedules limits over time. Its `defaults` hold the limits for
the whole run. Each *segment* overrides some of those limits for a window of time, given in
seconds since the run started. Segments are numbered 1, 2, 3 and so on in the order they
appear in the file. Segment 0 means no segment is active and only the defaults apply.

Limits change on the clock alone. If a segment starts in the middle of a tool call, the limit
changes in the middle of that call.

See [Write a profile](guides/writing-profiles.md).

## Run

A run is one execution of `rprof run`. It applies the profile to the target, samples usage ten
times a second, and listens for tool-call events from your harness. When it ends, rprof
restores the target's original limits and writes a *run directory* containing the raw data and
a report.

See [Read the results](guides/results.md).

## Enforce and measure modes

In `enforce` mode, the default, rprof writes the profile's limits. In `measure` mode it writes
nothing. It still records usage, and its report shows how far usage went over each limit.

Measure mode has two uses. With no profile, it records an unconstrained baseline run. With a
profile the agent can see, it tests whether the agent stays within limits it was only told
about.

See [How far did usage go over the limits?](guides/results.md#how-far-did-usage-go-over-the-limits)

## Harness and tool calls

The harness is the program that runs your agent and executes its tool calls in the sandbox.
For each call, the harness sends rprof a `tool_start` message before the command runs and a
`tool_end` message after it returns. These messages never change limits. rprof uses them to
label the usage samples, to report usage per call, and to explain failures.

See [Connect your harness](guides/harness.md).

## Agent view and visibility

The agent view is what the agent may learn about its limits. It can include the limits in
force now, and the full schedule with what changes next. The profile's `visibility` field sets
how much the agent may see: `none`, `current` or `full`. rprof writes the view to files the
sandbox can read, and returns it to the harness, which decides where the agent sees it.
`visibility` doesn't stop the sandbox from reading its cgroup files; `rprof run --hide-limits`
does.

See [Show the agent its limits](guides/harness.md#show-the-agent-its-limits).

## Bound and no effect

A limit *bound* during a segment if it actually constrained the workload there. For example,
the CPU was throttled for a meaningful share of the time, or a process was killed for using too
much memory. The report states, for each segment and limit, whether the limit bound and gives
the evidence.

A segment in which no limit bound is marked *no effect*. If the segment was meant to squeeze
the workload, it tested nothing, and its levels should be tighter.

See [Read the results](guides/results.md#did-each-limit-bind).

## Snapshot and restore

Before rprof first changes a setting, it saves the setting's original value to the run
directory. It writes the originals back when the run ends normally, on Ctrl-C, and on
`SIGTERM`. If rprof is killed outright, `rprof reset --run <run-dir>` restores from the saved
copy.

See [Recover after a crash](guides/host-setup.md#recover-after-a-crash).
