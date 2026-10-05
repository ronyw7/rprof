#!/usr/bin/env bash
# Copy the working tree to a remote host and set up its venv: scripts/sync-remote.sh <host> [dest]
set -euo pipefail
host=${1:?host}
dest=${2:-rprof}
cd "$(dirname "$0")/.."
rsync -az --delete --exclude .venv --exclude _private --exclude runs --exclude '__pycache__' \
  --exclude .pytest_cache --exclude .git ./ "$host:$dest/"
ssh "$host" "cd $dest && PATH=\$PATH:/snap/bin:\$HOME/.local/bin uv sync -q --python 3.12 --extra plot && echo synced \$(pwd)"
