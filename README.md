# rprof

rprof changes the CPU, memory, disk, network and process limits of a running Docker container
on a schedule you write, and records what the container used while each limit was in force.

Use it to test how an AI agent copes when its sandbox becomes slower or smaller partway through
a task.

```yaml
# squeeze.yaml: half a CPU core from 60 s to 120 s, then 1 GiB of memory until 180 s
version: 1
name: squeeze
segments:
  - {from: 60, to: 120, cpu: {cores: 0.5}}
  - {from: 120, to: 180, mem: {max: 1Gi}}
```

```bash
sudo rprof run --target docker:sbx --profile squeeze.yaml -- .venv/bin/python my_harness.py
cat runs/*-squeeze/report.md     # did each limit actually constrain the agent?
```

During the run, rprof:

- applies each limit at its scheduled time;
- samples the container's usage ten times a second;
- labels the samples with your harness's tool calls;
- explains failed calls, for example `Killed: memory limit 1 GiB reached`.

When the run ends, it restores the container's original limits.

## Install

rprof needs Linux with cgroup v2, the standard Docker daemon (not rootless Docker), Python 3.11
or later, and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/ronyw7/rprof.git && cd rprof
uv sync --extra plot
sudo ln -sf "$PWD/.venv/bin/rprof" /usr/local/bin/rprof    # so that `sudo rprof` works
sudo rprof doctor
```

## Documentation

- [Getting started](docs/getting-started.md): limit a container's CPU and memory and see the
  effect, in about 10 minutes.
- [Key concepts](docs/key-concepts.md): targets, knobs, profiles, segments and runs.
- User guides: [write a profile](docs/guides/writing-profiles.md),
  [choose limits](docs/guides/limits.md), [connect your harness](docs/guides/harness.md),
  [read the results](docs/guides/results.md), [set up a host](docs/guides/host-setup.md).
- Reference: [CLI](docs/reference/cli.md), [profile format](docs/reference/profile.md),
  [Python client](docs/reference/client.md), [control protocol](docs/reference/protocol.md),
  [run directory](docs/reference/run-directory.md).

## Versions

rprof's version comes from git, so every commit has its own and runs record exactly which code
produced them (`rprof_version` in each run's `meta.json`, and `rprof --version`):

| Checkout | Version |
| --- | --- |
| A release tag, `v0.1.0` | `0.1.0` |
| 3 commits after `v0.1.0` | `0.1.1.dev3+g1a2b3c4d5` |
| The same, with uncommitted changes | `0.1.1.dev3+g1a2b3c4d5.d20261005` |

This is the scheme setuptools-scm uses. Running from a git checkout, rprof reads the version
from git when it starts, so `git pull` updates it without reinstalling. To make a release, tag it
and push the tag: `git tag -a v0.2.0 -m "rprof 0.2.0" && git push --tags`.

## Development

```bash
uv run pytest tests/unit          # no root needed
sudo env PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider tests/integration tests/e2e
scripts/sync-remote.sh <host>     # copy the tree to a Linux host and set up its environment
```

The integration and end-to-end tests need root, Docker, cgroup v2 and the test images
(`docker build -t rprof-testbox images/testbox`, and the same for `images/netpeer`).
`PYTHONDONTWRITEBYTECODE=1` stops root from leaving cache files that your user can't delete.

CI (`.github/workflows/ci.yml`) runs the unit tests on Python 3.11 and 3.12, then the
integration and end-to-end tests as root on a GitHub-hosted Ubuntu VM. It skips tests marked
`timing`, whose pass bands (measured CPU, bandwidth, latency, rprof's own overhead) assume a
quiet, dedicated machine. Run the full suite, timing tests included, on the experiment host
before each batch of runs.
