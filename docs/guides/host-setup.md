# Set up a host

The host is the Linux machine that runs Docker and rprof. Its kernel enforces the limits. This
guide covers what the host needs, how to check it, how to launch sandboxes for experiments,
and how to recover when something goes wrong.

## Requirements

| Requirement | How to check |
| --- | --- |
| Linux with cgroup v2 | `stat -fc %T /sys/fs/cgroup` prints `cgroup2fs` |
| The standard Docker daemon, which runs as root. Rootless Docker is not supported. | `sudo docker info` works |
| Python 3.11 or later, and uv | `uv --version` |
| `tc`, `iptables` and `nsenter`, for the network knobs | `which tc iptables nsenter` |
| sudo | `sudo true` |

rprof was developed on Ubuntu 22.04 with kernel 6.8. Ubuntu 22.04 and later, Debian 11 and
later, and Fedora 31 and later use cgroup v2 by default. Older releases such as Ubuntu 20.04 use
cgroup v1, which rprof does not support.

## Install

```bash
git clone https://github.com/ronyw7/rprof.git && cd rprof
uv sync --extra plot
sudo ln -sf "$PWD/.venv/bin/rprof" /usr/local/bin/rprof
```

Commands that change limits need root. `sudo` doesn't see your virtual environment, so the
`ln` command puts `rprof` in `/usr/local/bin`, where `sudo` finds it. The docs assume you did
this. Without it, call the executable by path: `sudo .venv/bin/rprof`.

## Check the host

`doctor` checks the kernel, the cgroup controllers and the tools rprof needs:

```bash
sudo rprof doctor --target docker:sbx
```

Each line starts with `ok`, `warn` or `FAIL`. `--deep` also runs quick tests that set limits on
a scratch cgroup and confirm the kernel enforces them.

`selftest` goes further: it starts real containers and runs workloads in them. It checks
two things.

- **Enforcement: does each limit hold?** For each knob, rprof sets the limit on a scratch
  container with the same code `rprof run` uses, then runs a workload that needs more than the
  limit. It checks the container's cgroup counters, or times the workload. For example, it
  limits the container to half a core and checks that `stress-ng --cpu 2` gets 0.40–0.60 cores.
- **Fidelity: does rprof record usage correctly?** rprof records a scratch container with a
  real measure-mode run at 20 samples a second, and runs workloads of known size in it as tool
  calls: 2 busy cores, 1 GiB of memory, a 512 MiB direct write, writing and reading a 512 MiB
  file, a 10 MiB transfer and 50 processes. It reads the run's samples back through the same
  code that builds reports, and compares them with the known sizes. Reading the file must not
  count as memory in use, because page cache can be reclaimed.

It takes about two minutes and needs the test images:

```bash
docker build -t rprof-testbox images/testbox
docker build -t rprof-netpeer images/netpeer
sudo rprof selftest
```

```text
rprof 0.1.1
host electrode · Linux 6.8.0-85-generic

Self-test
Verifying resource enforcement and measurement fidelity.

Enforcement

  CPU
  PASS  cpu.cores=0.5        expected 0.40–0.60 cores        observed 0.50 cores
  PASS  cpu.period=20ms      expected 40–60 periods/s        observed 50 periods/s
  PASS  cpu.cpus=0           expected ≤1.10 cores            observed 0.99 cores

  Memory
  PASS  mem.max=128Mi        expected OOM kill               observed exit 137, 1 OOM kill
  ...

Fidelity

  PASS  CPU usage            expected 1.80–2.20 cores        observed 2.00 cores
  PASS  memory peak          expected 922–1126 MiB           observed 1026 MiB
  PASS  page cache excluded  expected ≤64 MiB                observed +16 MiB
  ...

Summary

  PASS  Enforcement   18 / 18
  PASS  Fidelity       8 / 8

  System is fully supported.
  Capabilities saved to /var/lib/rprof/capabilities.json
```

Each row gives the status, the limit or measurement tested, the pass condition and what was
observed. `PASS` and `FAIL` are what they say. `SKIP` means the check can't run on this host,
for example because it has too little swap, or too few idle CPUs for a 2-core workload. `INFO`
reports how the host behaves, with no pass condition: whether disk limits also slow buffered
writes, and the disk's native bandwidth, which `rprof describe` reports for disks a profile doesn't
throttle. A failed or skipped check also shows the
workload that ran and a note on what went wrong. If a fidelity check fails, rprof keeps that
recording and prints its path, so you can look at the samples.

The summary says what the results mean for `rprof run`: for example, which knobs it will
refuse. `--verbose` shows every check's workload. `--quiet` prints one line, such as
`rprof selftest: PASS (18/18 enforcement, 8/8 fidelity)`, which suits provisioning scripts;
the exit code is 1 if any check failed. `--quick` runs shorter workloads. `--only enforcement`
or `--only fidelity` runs one part and keeps the other part's earlier results.

rprof saves the results in `/var/lib/rprof/capabilities.json` and copies them into every
run's `meta.json`, so you can compare results from different hosts. `rprof run` refuses to use
a knob that failed enforcement and exits with code 73. To run anyway without that knob, pass
`--allow-degraded`. If a fidelity check failed, `run` still runs but prints a warning and logs a
`fidelity_failed` event, because the recorded numbers may be off. Run the selftest again after
any kernel or Docker upgrade.

## Launch a sandbox

For experiments, start the sandbox like this:

```bash
sudo mkdir -p /var/lib/rprof/view/sbx /var/lib/rprof/data/sbx
docker run -d --name sbx \
  --cgroup-parent=rprof-sbx.slice \
  -v /var/lib/rprof/view/sbx:/run/rprof:ro \
  -v /var/lib/rprof/data/sbx:/data \
  sandbox-image sleep infinity
```

| Option | Why |
| --- | --- |
| `--cgroup-parent=rprof-sbx.slice` | Puts the container's cgroup under a parent cgroup for the sandbox. See below. |
| `-v /var/lib/rprof/view/sbx:/run/rprof:ro` | Lets the agent read its limits from `/run/rprof/now.txt`. Read-only, so the agent can't change them. |
| `-v /var/lib/rprof/data/sbx:/data` | The *data volume*. rprof limits disk I/O on the disk under it, and `disk.capacity` acts on it. |

None of these is required. Without the view mount, the agent can't read its limits from a
file. Without a data volume, rprof limits I/O on the disk that holds Docker's storage, and
`disk.capacity` is unavailable.

A cgroup is a group of processes that the kernel limits and measures together. Docker gives
each container one. On most distributions, systemd manages the cgroup tree and calls a
parent group a *slice*, so parent names end in `.slice`. systemd also nests slices by the
dashes in their names: `rprof-sbx.slice` is created inside `rprof.slice`, at
`/sys/fs/cgroup/rprof.slice/rprof-sbx.slice/`. Putting a sandbox's containers under one slice
lets you limit them together, as described below.

Check what rprof found:

```bash
sudo rprof inspect --target docker:sbx
```

`inspect` prints the container's cgroup, its processes, the disk and network interface rprof
will limit, and the limits currently set.

## Limit disk space

`disk.capacity` works by filling the data volume, so the data volume must be a filesystem of
its own with a fixed size. A file-backed ext4 filesystem works well:

```bash
sudo fallocate -l 2G /var/lib/rprof/data-sbx.img
sudo mkfs.ext4 -q -m 0 /var/lib/rprof/data-sbx.img
sudo mount -o loop /var/lib/rprof/data-sbx.img /var/lib/rprof/data/sbx
```

Then start the sandbox with `-v /var/lib/rprof/data/sbx:/data`. The `-m 0` option stops ext4
from reserving space for root. Without it, processes running as root could write past the
capacity.

The mount does not survive a reboot. To make it permanent, add it to `/etc/fstab`.

## Limit a sandbox made of several containers

If the agent's database runs in its own container, limit both containers together. Start them
under the same parent slice:

```bash
docker run -d --name agent --cgroup-parent=rprof-exp1.slice sandbox-image sleep infinity
docker run -d --name db --cgroup-parent=rprof-exp1.slice -e POSTGRES_PASSWORD=pw postgres:16
```

Find the slice's directory. It is the parent of the container's `cgroup` path, which
`inspect` prints:

```bash
sudo rprof inspect --target docker:agent | grep '^cgroup'
# cgroup  /sys/fs/cgroup/rprof.slice/rprof-exp1.slice/docker-3f2a....scope
```

Target the slice, and name each container whose network you want to limit:

```bash
sudo rprof run --target cgroup:/sys/fs/cgroup/rprof.slice/rprof-exp1.slice \
  --net docker:agent --net docker:db --profile p.yaml -- .venv/bin/python my_harness.py
```

Limits on the slice cap the total for all its containers. For example, `mem.max: 2Gi` lets the
agent and the database use 2 GiB together.

## Recover after a crash

rprof restores the original limits when a run ends, on Ctrl-C, and on `SIGTERM`. If rprof is
killed with `SIGKILL`, or the host crashes, the limits stay in place. Restore them from the
run directory:

```bash
sudo rprof reset --run runs/<run-id>
```

This also removes the network rules and the disk ballast file the run created.

Limits set with `rprof apply` are restored with `sudo rprof reset --target docker:sbx`.

Only one rprof process can control a target at a time. A second `run` or `apply` on the same
target exits with code 74. The lock is released when the first process exits, even if it was
killed.

## Troubleshooting

| Problem | Cause | Fix |
| --- | --- | --- |
| `docker container 'sbx' not found` (exit 71), but `docker ps` lists it | Your shell uses a rootless Docker; `sudo rprof` uses the system one | `export DOCKER_CONTEXT=default`, then start the container again |
| `… is not cgroup v2` (exit 73) | The host uses cgroup v1 | Use a host with cgroup v2 |
| `needs root` (exit 72) | Run without sudo | `sudo .venv/bin/rprof …` |
| `target is locked by another rprof process` (exit 74) | Another `run` or `apply` controls the target | Wait for it, or stop it |
| `host cannot enforce knobs this profile uses` (exit 73) | A knob is unavailable or failed the selftest | Read the reason in the message. Pass `--allow-degraded` to run without that knob. |
| `disk.capacity: no data filesystem` | The sandbox has no data volume at `/data` | Mount one, as in [Limit disk space](#limit-disk-space), or pass `--data-dir` |
| `io.*: no block device resolved` | rprof couldn't find the disk to limit | Pass `--io-device MAJ:MIN`; `lsblk` lists the numbers |
| A process over its memory limit slows down instead of being killed | It is swapping. The limit came from outside rprof, such as `docker run --memory`, or the profile set `mem.swap_max` above 0. | Set the memory limit with rprof, which sets swap to 0 unless told otherwise |
| `RESTORE FAILED` after a run | A setting couldn't be written back | `sudo rprof reset --run <run-dir>` |
| `--ctl-dir … is too long for a Unix socket` (exit 2) | With `--harness inside`, the socket path exceeds about 100 bytes | Use a shorter `--ctl-dir`. Long `--runs-dir` paths are handled automatically. |
| The sandbox exits when `mem.max` drops below its usage | Its main process was killed. rprof protects the first process and, behind an init shim, the shim's original children; anything else can be chosen. | Keep the sandbox's main process as PID 1 or a child of `docker-init`/`tini`, or match it with `--protect` |

## Kernel differences

Some behavior depends on the kernel version:

- **Kernel 6.12 or later** lets rprof record peak memory between samples (`mem.peak`). On older
  kernels that field is left out, so a memory spike shorter than a tenth of a second can be
  missed in the samples. Memory kills are still counted, so the report still catches them.
- **Restoring an unlimited CPU set.** A running container's `cpuset.cpus` can't be set back
  to empty. If a run limited `cpu.cpus`, rprof restores it to the parent's CPU list instead. The
  same CPUs are allowed, but the file shows a list such as `0-111` rather than being empty.
- **Throttling of buffered writes** depends on the filesystem. `rprof selftest` records
  whether it works on the host.

## Run rprof from a Mac

Docker Desktop runs containers in a Linux VM with cgroup v2. rprof can run inside a privileged
helper container that shares the VM's cgroups, processes and network. See
`images/dev/README.md`. Timing on a laptop VM is noisy, so use a Linux host for experiments.
