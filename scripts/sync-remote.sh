#!/usr/bin/env bash
# Copy the working tree to a remote host and set up its venv: scripts/sync-remote.sh <host> [dest]
# The default dest is ~/rprof-dev, so a sync never overwrites a git checkout at ~/rprof.
set -euo pipefail
host=${1:?host}
dest=${2:-rprof-dev}
cd "$(dirname "$0")/.."
rsync -az --delete --exclude .venv --exclude _private --exclude runs --exclude '__pycache__' \
  --exclude .pytest_cache --exclude .git ./ "$host:$dest/"
# The copy has no .git, so pass the local version for the build to record.
version=$(uv run -q python -c "from rprof._version import get_version; print(get_version())")
ssh "$host" "cd $dest && PATH=\$PATH:/snap/bin:\$HOME/.local/bin SETUPTOOLS_SCM_PRETEND_VERSION=$version \
  uv sync -q --python 3.12 --extra plot --reinstall-package rprof && echo synced \$(pwd) at $version"
