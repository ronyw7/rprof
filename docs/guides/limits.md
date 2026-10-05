# Choose limits

Each rprof knob is enforced by a mechanism in the Linux kernel, and each mechanism affects a
tool call in its own way. Some limits only slow a call down. Others make it fail. This guide
explains what every limit does, so you can tune it anywhere from no effect to certain failure.

## Try the examples

The examples set limits by hand with `rprof apply`. In a profile you write the same knobs as
YAML: `cpu.cores=0.5` becomes `cpu: {cores: 0.5}`. To follow along, start a sandbox from the
test image, as in [Getting started](../getting-started.md):

```bash
docker build -t rprof-testbox images/testbox
docker run -d --name sbx rprof-testbox sleep infinity
```

Limits set with `apply` add up until you remove them. Run `sudo rprof reset --target docker:sbx`
after each example.

## Summary

| Knob | What happens to a tool call when the limit is reached |
| --- | --- |
| `cpu.cores`, `cpu.cpus` | Runs slower. Fails only if the harness deadline expires. |
| `mem.high` | Slows down sharply as the kernel reclaims memory. May nearly stall. |
| `mem.max` | Killed by the kernel. The exit code is 137. |
| `io.rbps`, `io.wbps`, `io.riops`, `io.wiops` | Disk reads and writes run slower. |
| `pids.max` | Can't start new processes or threads: "Resource temporarily unavailable". |
| `net.rate`, `net.delay`, `net.jitter` | Network transfers run slower. |
| `net.loss` | Packets are lost. TCP resends them, so transfers slow down and may time out. |
| `net.partition` | Network connections are refused at once (`reject`) or hang until they time out (`drop`). |
| `disk.capacity` | Writes fail with "No space left on device". |

## CPU

`cpu.cores` caps how much CPU time the sandbox gets. The kernel enforces it by *throttling*:
in every scheduling period (100 ms by default), the sandbox may run for `cores × period`.
Once it has used that, its processes wait until the next period starts.

A CPU limit never makes a call fail by itself. The call takes longer. If you want slow calls
to fail, set a `harness.deadline` so your harness stops calls that run too long.

```bash
sudo rprof apply --target docker:sbx cpu.cores=0.5
docker exec sbx stress-ng --cpu 2 --timeout 15s     # gets 0.5 cores instead of 2
```

- `cpu.cpus` restricts the sandbox to specific CPUs, for example `0-3`. Parallel work slows
  down once it needs more CPUs than the set contains.
- `cpu.period` sets the length of the scheduling period, from 1 ms to 1 s. A longer period
  gives the same average but longer stalls: with 0.5 cores and a 1 s period, the sandbox can
  run for 0.5 s and then wait 0.5 s.

## Memory

`mem.max` is a hard memory cap. When the sandbox needs more memory than the cap and the
kernel cannot free enough, the kernel kills a process. This is called an OOM (out of memory)
kill. The killed command exits with code 137.

```bash
sudo rprof apply --target docker:sbx mem.max=512Mi
docker exec sbx hog-mem 1G 10; echo $?              # prints 137
```

`mem.high` is a soft cap. Above it, the kernel reclaims memory aggressively and slows down
the processes that allocate. Memory that is in active use can't be reclaimed without swap,
so a process whose working set doesn't fit under `mem.high` nearly stops. Pair `mem.high`
with a harness deadline, or allow some swap with `mem.swap_max`.

`mem.swap_max` limits how much memory the sandbox may move to swap. If you set any memory knob
but not `mem.swap_max`, rprof sets swap to 0, so the sandbox is killed instead of swapping.

> [!NOTE]
> Page cache, which holds file data the kernel has recently read or written, counts toward the
> memory limits. A low limit pushes cached file data out, so the workload reads more from disk.

> [!WARNING]
> When the limit is reached, the kernel kills the *largest* process in the sandbox. That can be
> a database server or your harness. During `rprof run`, these are never chosen:
>
> - the container's first process;
> - if that process is an init shim such as `docker-init` (from `docker run --init`), `tini`
>   or `dumb-init`, the children it had when the run started. The container exits when that
>   child dies;
> - processes matching `--protect PATTERN`. See
>   [Run the harness inside the sandbox](harness.md#run-the-harness-inside-the-sandbox).
>
> Processes that start later are not protected, including background jobs adopted by the init
> shim. `rprof apply` protects nothing.

If a segment lowers `mem.max` below what the sandbox already uses, the kernel frees what it
can, then kills a process at the start of the segment. This is intended: it is the moment the
environment changed. rprof logs a `mem_max_below_usage` warning each time it happens, with
the usage at that moment. If every remaining process is protected, the kernel kills nothing
and usage stays above the limit until memory is freed.

## Disk I/O

The `io` knobs cap throughput on one disk. By default that is the disk under the sandbox's
*data volume*, the volume mounted at `/data`. If there is no data volume, it is the disk that
holds Docker's storage. `io.rbps` and `io.wbps` cap bytes read and written per second.
`io.riops` and `io.wiops` cap read and write operations per second.

```bash
sudo rprof apply --target docker:sbx io.wbps=20Mi
docker exec sbx dd if=/dev/zero of=/var/tmp/f bs=1M count=200 oflag=direct   # takes about 10 s
```

Disk limits slow calls down. Like CPU limits, they cause failures only through a deadline.

> [!NOTE]
> Reads served from the page cache never touch the disk, so disk limits can't slow them.
> Writes that go through the page cache are limited only on filesystems that support it.
> `rprof selftest` checks this on your host.

To limit a different disk, pass `--io-device MAJ:MIN` to `rprof run` or `rprof apply`. `lsblk`
lists each disk's `MAJ:MIN` numbers.

## Processes

`pids.max` caps the number of processes and threads in the sandbox. When the sandbox is at the
cap, creating another one fails. Shells report `Cannot fork`. Programs report "Resource
temporarily unavailable".

```bash
sudo rprof apply --target docker:sbx pids.max=20
docker exec sbx sh -c 'for i in $(seq 50); do sleep 30 & done'   # Cannot fork
```

The cap counts threads too, so a multi-threaded program reaches it sooner than its number of
processes suggests.

## Network

Network knobs act on traffic between the sandbox and other machines or containers. Traffic
that stays inside the sandbox, over its loopback interface, is never affected.

- `net.rate` caps bandwidth in each direction, for example `10mbit`.
- `net.delay` and `net.jitter` add latency to every packet the sandbox sends, for example
  `50ms` with `10ms` of jitter.
- `net.loss` drops a share of the packets the sandbox sends, for example `30%`. TCP resends
  lost packets, so connections slow down and may time out.
- `net.partition` blocks all traffic. With `reject`, connections fail at once with "Connection
  refused". With `drop`, packets vanish and connections hang until the program gives up.
- `net.allow` lists address ranges that no network knob touches, for example a database
  container at `172.18.0.0/16`.

rprof applies these with the kernel's traffic-control queues (`tc` and its `netem` emulator)
and firewall rules (`iptables`) inside the container's network namespace.

To try them, put the sandbox and a test server on one Docker network:

```bash
docker build -t rprof-netpeer images/netpeer
docker network create rprof-test
docker run -d --name netpeer --network rprof-test rprof-netpeer
docker network connect rprof-test sbx

sudo rprof apply --target docker:sbx net.loss=30%
docker exec sbx ping -c 200 -i 0.05 -q netpeer       # about 30% packet loss
sudo rprof apply --target docker:sbx net.loss=0% net.rate=10mbit
docker exec sbx iperf3 -c netpeer -t 5              # about 10 Mbit/s
sudo rprof apply --target docker:sbx net.rate=max net.partition=reject
docker exec sbx curl -m 5 http://netpeer/           # Connection refused, at once
sudo rprof reset --target docker:sbx
```

> [!NOTE]
> If your harness runs inside the sandbox, network knobs also slow its calls to the language
> model's API. Add the API's addresses to `net.allow`, or run the harness outside the sandbox.

## Disk space

`disk.capacity` limits how many bytes the sandbox can store on its data volume. rprof enforces
it by creating a hidden file, `.rprof-ballast`, that takes up the rest of the volume. When the
sandbox's files on the volume add up to `capacity` bytes, the next write fails with "No space
left on device".

Because the ballast fills the whole volume, the data volume must be a filesystem of its own
with a fixed size, not a directory on the host's main disk.
[Limit disk space](host-setup.md#limit-disk-space) shows how to create one. Then:

```bash
sudo rprof apply --target docker:sbx disk.capacity=100Mi
docker exec sbx dd if=/dev/zero of=/data/big bs=1M count=200   # fails after about 100 MiB
```

If the sandbox already stores more than the new capacity, rprof fills all the remaining free
space and logs a `disk_overcommitted` warning.

## Hide the limits from the sandbox

A profile's `visibility: none` stops rprof from *telling* the agent its limits, but the sandbox
can still *read* them. A container sees its own cgroup at `/sys/fs/cgroup`, so
`cat /sys/fs/cgroup/memory.max` prints the real memory limit. Agents often check after a
process is killed without explanation.

To hide the cgroup files, pass `--hide-limits` to `rprof run`. While the run lasts, the sandbox
sees a read-only stand-in at `/sys/fs/cgroup` whose files report no limits:

```bash
sudo rprof run --target docker:sbx --profile p.yaml --hide-limits -- .venv/bin/python my_harness.py
docker exec sbx cat /sys/fs/cgroup/memory.max     # prints "max" during the run
```

The kernel still enforces the real limits. rprof removes the stand-in when the run ends, and
`rprof reset --run` removes it after a crash. It covers every container in the target that
exists when the run starts.

Some traces of the limits remain:

- Programs that size themselves from the cgroup, such as DuckDB, the JVM and Go, see no limit.
  They behave as on an unlimited machine and get killed instead of adapting. That is usually
  what a hidden-limit experiment wants, but it changes the workload.
- `tc qdisc show` inside the sandbox shows the network shaping, if the image has `tc`.
- `/data/.rprof-ballast` shows that disk space is being held back.
- Exit code 137 and slow calls still show that *something* happened.

Without `--hide-limits`, you can hide a single file yourself by bind-mounting a file containing
`max` over it when you start the container, for example
`-v /path/to/max-file:/sys/fs/cgroup/memory.max:ro`.

## Raw cgroup files

For a setting rprof has no knob for, a segment can write the container's cgroup files
directly with a `unified` map. rprof saves and restores these files like any other setting:

```yaml
segments:
  - {from: 60, to: 120, unified: {cpu.weight: "50", memory.low: "64M"}}
```

## Tune a limit from no effect to failure

A limit tests something only if the workload reaches it. To pick levels:

1. Run the task once with no profile, and look at its usage with `rprof timeline` or
   `rprof plot`. Note the peak memory, the CPU cores used, and so on.
2. Write the levels you are considering into a profile, and run the task with
   `--mode measure`. rprof writes no limits, but the report shows how often and by how much
   usage went over each one.
3. Run the levels that usage went over in enforce mode. To compare several levels of one knob,
   generate a family of profiles with `rprof gen sweep`. Segments marked *no effect* had
   levels that were too loose.

[Read the results](results.md) explains how the report decides whether a limit bound.
