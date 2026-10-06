# Run Harbor tasks

[Harbor](https://github.com/harbor-framework/harbor) runs agents on benchmark tasks, such as
Terminal-Bench, each in a container it starts itself. rprof runs a Harbor trial under a profile
with one command: write the profile, then put `harbor run` after `--`.

```bash
sudo -E rprof run --target harbor --profile p.yaml -- harbor run -p tasks/my-task -a terminus-2 -m "$MODEL"
```

`--target harbor` means "the container of the trial this command starts". rprof:

1. Starts `harbor`, as you rather than root, against the Docker daemon rprof uses.
2. Waits for the trial's container, then for the agent's first process in it, and only then
   applies the profile and starts its clock. Time 0 in the profile is when the agent starts, not
   when Harbor builds the image or sets up the environment.
3. Records until the container exits, which ends the run normally (end reason `target_exit`).
4. Waits for `harbor` to finish writing its results, and exits with Harbor's exit code.

One rprof run follows one trial: give `harbor run` one task and `-k 1`.

## Before you begin

- rprof set up on the host, with `sudo rprof selftest` passing. See [Set up a host](host-setup.md).
- Harbor installed for your user: `uv tool install harbor`.
- For the demo below, the test image: `docker build -t rprof-testbox images/testbox`.

## Try it with Harbor's oracle agent

Harbor's `oracle` agent runs a task's own reference solution, `solution/solve.sh`, and calls no
model, so it shows the whole flow without an API key. `examples/harbor/demo-task` is a short task
whose solution acts like a scripted agent:

- **0–20 s:** keeps two CPU cores busy;
- **then:** pauses 12 s;
- **about 32 s:** holds 1 GiB of memory for 4 s, retrying with 256 MiB if that is killed;
- **last:** writes 300 MiB to disk.

`examples/harbor/squeeze.yaml` halves the CPU from 10 to 30 s and caps memory at 512 MiB from 30 to
45 s. Its defaults are the task's own budget from `task.toml`; see
[Write the profile](#write-the-profile).

Run the task with the profile (A), then without limits, recording only (B):

```bash
sudo rprof run --target harbor --profile examples/harbor/squeeze.yaml --name demo-A --runs-dir ~/rprof-runs -- \
  harbor run -p examples/harbor/demo-task -a oracle -o ~/harbor-jobs -y
sudo rprof run --target harbor --mode measure --name demo-B --runs-dir ~/rprof-runs -- \
  harbor run -p examples/harbor/demo-task -a oracle -o ~/harbor-jobs -y
```

```text
rprof: waiting for the Harbor trial's container
rprof: found demo-task__ff3tqrg__env-main-1; waiting for the agent to start ('solve.sh')
  ...Harbor's progress...
rprof: run 2026-10-05T1824-demo-A ended (target_exit); results in /home/you/rprof-runs/2026-10-05T1824-demo-A
```

Both trials pass. Under the profile, the 1 GiB step is killed at the 512 MiB cap and the
solution retries smaller. The timeline shows it:

```bash
rprof timeline ~/rprof-runs/*-demo-A
```

```text
t (s)      segment  limits         running calls  cpu  mem peak  io write  events
0.0–10.0   0        defaults       -              2.0  12.1 MiB  0
10.0–30.0  1        cpu.cores 0.5  -              0.2  11.8 MiB  0
30.0–45.0  2        mem.max 512Mi  -              0.0  437 MiB   20 MiB/s  oom_kill: 1; mem_max: 37
45.0–51.2  0        defaults       -              0.0  1.1 MiB   0
```

Each run's `report.md` starts with the run's peak memory and OOM kills, here
`437 MiB … OOM kills: 1` for A and `1 GiB … OOM kills: 0` for B. To compare the runs in one figure:

```bash
rprof plot ~/rprof-runs/*-demo-B ~/rprof-runs/*-demo-A --label Unconstrained --label Constrained
```

## Run a real agent

Use the same command with a real agent and model. Run it with `sudo -E` so that `harbor` gets
your API key; rprof starts `harbor` as you, with your environment.

```bash
export ANTHROPIC_API_KEY=...
sudo -E rprof run --target harbor --profile p.yaml --name compcert-A-1 -- \
  harbor run -d terminal-bench/terminal-bench-2-1 -i compile-compcert -a terminus-2 -m "$MODEL" -k 1
```

rprof knows when these agents start:

| Agent | Runs | rprof starts the profile when it sees |
| --- | --- | --- |
| `terminus-2` | On the host. It drives a tmux session in the container. | `tmux new-session` |
| `claude-code` | Inside the container. Harbor installs it there. | `claude --verbose` |
| `oracle` | The task's `solution/solve.sh`, inside the container | `solve.sh` |

For another agent, give a fragment of its first process's command line with
`--agent-start PATTERN`. Without one, rprof starts the profile as soon as the container appears,
and says so.

**Prefer an agent that runs on the host, such as `terminus-2`.** Then the limits apply to the
task's work alone. An agent that Harbor installs into the container, such as `claude-code`, shares
the container's limits:

- **Its memory counts against the limit.** Claude Code is a Node process of a few hundred MiB, so
  under a 1 GiB cap the task gets well under 1 GiB. Size squeezes as the agent's footprint plus
  what the task needs. A run without limits shows the agent's footprint as the memory in use
  before the heavy work starts.
- **It must not be the process the kernel kills.** rprof protects `claude-code` automatically, as
  if you passed `--protect 'claude --verbose'`. The commands it starts stay killable, so a limit
  kills the agent's `sort`, not the agent. If the agent alone outgrows the limit, nothing is left
  to kill and the container stalls, so keep limits well above its footprint.
- **Network limits apply to its model calls too.** Add the API's addresses to `net.allow`, or
  leave the network unlimited.

## Write the profile

**Start from the task's budget.** Harbor gives each trial's container the `cpus` and `memory_mb`
from the task's `task.toml`; compile-compcert, for example, has 2 CPUs and 4096 MB. Once a profile
mentions CPU or memory, rprof manages those limits for the whole run, and anything the profile
leaves unset means unlimited, not Harbor's value. So set `defaults` to the task's budget, and
squeeze from there:

```yaml
version: 1
name: compcert-squeeze
defaults: {cpu: {cores: 2}, mem: {max: 4Gi, swap_max: 4Gi}}
segments:
  - {from: 60, to: 900, label: squeeze, cpu: {cores: 1}, mem: {max: 1536Mi, swap_max: 0}}
```

**Set `swap_max` too.** Docker lets a Harbor container swap as much as its memory limit, while
rprof sets swap to 0 whenever it manages memory, unless told otherwise. Match Harbor in
`defaults`, and use `swap_max: 0` inside a squeeze, so that memory over the limit is killed
rather than swapped.

**Tell the agent, if it should know.** `--tell-agent` gives the agent `rprof describe` of the
profile, so every trial is told the same thing, in the same words, as is enforced. By default it is
appended to the task's instruction; `--tell-via system-prompt` appends it to Claude Code's system
prompt instead (Harbor's `--ak append_system_prompt`), leaving the task's instruction as it is:

```bash
sudo -E rprof run --target harbor --profile p.yaml --tell-agent -- harbor run ...
sudo -E rprof run --target harbor --profile p.yaml --tell-agent --tell-via system-prompt -- \
  harbor run -a claude-code ...
rprof describe p.yaml            # to read it first
```

How much it says follows the profile's `visibility`: the whole schedule for `full`, the limits at
the start for `current`. A schedule's text tells the agent that time 0 is when it receives the
task. The run's `meta.json` records the text (`harbor.told_agent`).

## Results

The run directory is the usual one; see [Read the results](results.md). `meta.json` also has a
`harbor` object:

| Field | Value |
| --- | --- |
| `container`, `container_id` | The trial's container |
| `trial` | Harbor's trial name, lowercased: Harbor's job directory has a directory of this name, in its original case |
| `agent`, `agent_start` | The agent from `-a`, and the command-line fragment that started the profile |
| `waited_container_s`, `waited_agent_s` | How long rprof waited for the container, then for the agent |
| `command` | The `harbor run` command line |
| `told_agent`, `told_via` | With `--tell-agent`, the text the agent was given, and where (`instruction` or `system-prompt`) |
| `trial_dir`, `kept` | Harbor's directory for the trial, and the copy of it in the run directory (`harbor-trial`) |

When Harbor finishes, rprof copies the trial's directory into the run directory as
`harbor-trial/`, so each run keeps what its agent did next to what rprof measured:

| In `harbor-trial/` | Holds |
| --- | --- |
| `agent/` | The agent's logs. For Terminus 2, its trajectory and terminal recording. |
| `verifier/` | `reward.txt` and the tests' output |
| `result.json`, `config.json`, `trial.log` | Harbor's result and settings for the trial, and its log |

The original stays in Harbor's job directory, `jobs/` by default or `-o DIR`. rprof can't see the
agent's individual tool calls, so take failed commands from the trajectory.

## Attach by hand

To start Harbor yourself, attach rprof to the trial's container once it appears. Without a
command, rprof records until the container exits:

```bash
CID=$(docker ps -q --filter label=com.docker.compose.service=main --filter "name=my-task__")
sudo rprof run --target "docker:$CID" --profile p.yaml
```

Harbor names a trial's container `<task directory>__<id>__env-main-1`. If your user's default
Docker is a rootless one, as on hosts that have both, run Harbor with
`DOCKER_HOST=unix:///var/run/docker.sock` so that rprof can see its containers. `--target harbor`
does this for you.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `cannot start harbor` | Harbor isn't installed for the user who ran sudo, in `~/.local/bin` or on the system path. Install it with `uv tool install harbor`, or give its full path after `--`. |
| `harbor exited (1) before its trial's agent started` | Harbor failed, for example while building the image. Its output above the message says why. Nothing was recorded. |
| rprof keeps waiting for the agent | The agent's start marker never appeared. Check with `docker top <container>`, and give `--agent-start` a fragment of the agent's command line. |
| The trial ran but its usage looks low | The profile started late: check `waited_agent_s` in `meta.json`. |
