# Run rprof on a Mac

Docker Desktop runs your containers inside a Linux virtual machine that uses cgroup v2. rprof
can't run on macOS itself, but it can run in a helper container that shares the virtual
machine's cgroups, processes and network. This is useful for trying rprof out. Use a Linux host
for experiments, because timing on a laptop VM is noisy.

## Start the helper container

From the repository root:

```bash
docker build -t rprof-dev images/dev
docker run --rm -it --privileged --pid=host --cgroupns=host --net=host \
  -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD":/src -w /src \
  rprof-dev bash -c 'pip install -q -e . && rprof doctor --deep'
```

| Option | Why |
| --- | --- |
| `--privileged` | rprof writes kernel settings, which needs full privileges |
| `--pid=host`, `--cgroupns=host`, `--net=host` | Lets rprof see the VM's processes, cgroups and network, where your sandboxes run |
| `-v /var/run/docker.sock:…` | Lets rprof look up containers by name |
| `-v "$PWD":/src` | Mounts the repository, so changes on your Mac take effect at once |

Replace `rprof doctor --deep` with `bash` to get a shell. Inside it, `rprof` works as on a
Linux host. `--target docker:<name>` finds containers you started on your Mac.

## Differences from a Linux host

- **Mount paths come from your Mac.** When you start a sandbox with `-v SOURCE:DEST`, `SOURCE`
  is a path on your Mac, so `/var/lib/rprof/view/sbx` doesn't exist. Share a named volume
  between the helper and the sandbox instead. Mount `-v rprof-state:/var/lib/rprof` in the
  helper, and
  `--mount type=volume,src=rprof-state,dst=/run/rprof,volume-subpath=view/sbx,readonly` in the
  sandbox. Alternatively, pass `--view-dir` with a path under `/src`.
- **Sockets need a named volume too.** With `--harness inside`, put `--ctl-dir` on a named
  volume, because Unix sockets don't work across the Mac file share.
