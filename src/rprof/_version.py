"""rprof's version, derived from git so that every commit has its own.

- A tagged commit is that version: tag ``v0.2.0`` gives ``0.2.0``.
- A later commit is a development version of the next patch release: 3 commits after
  ``v0.2.0`` gives ``0.2.1.dev3+g1a2b3c4d5`` (``g`` + the commit's short hash).
- Uncommitted changes add the date: ``0.2.1.dev3+g1a2b3c4d5.d20261005``.

This is setuptools-scm's default scheme ("guess-next-dev" with "node-and-date"), which
builds use through hatch-vcs, so an installed rprof reports the same string.

Running from a git checkout (including source mounted into a container), the version is
read from git the first time it's needed, so ``git pull`` changes it without reinstalling.
Otherwise it comes from the installed package's metadata.
"""

from __future__ import annotations

import datetime as _dt
import functools
import re
import subprocess
from pathlib import Path

UNKNOWN = "0+unknown"
_DESCRIBE = re.compile(r"^v?(\d+(?:\.\d+)*)-(\d+)-g([0-9a-f]+)(-dirty)?$")


def _next_patch(base: str) -> str:
    parts = base.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def from_describe(describe: str, today: _dt.date | None = None) -> str | None:
    """``git describe --tags --long --dirty`` output -> PEP 440 version, or None if unparseable."""
    m = _DESCRIBE.match(describe.strip())
    if not m:
        return None
    base, distance, sha, dirty = m.group(1), int(m.group(2)), m.group(3), bool(m.group(4))
    if distance == 0 and not dirty:
        return base
    local = f"g{sha}"
    if dirty:
        local += f".d{(today or _dt.datetime.now(_dt.timezone.utc).date()):%Y%m%d}"
    return f"{_next_patch(base)}.dev{distance}+{local}"


def _git(root: Path, *args: str) -> str | None:
    try:
        # safe.directory: rprof often runs as root on a checkout owned by the user.
        r = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(root), *args],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def from_git(root: Path) -> str | None:
    # 9-character hashes, as hatch-vcs writes them, so source and installed versions match.
    d = _git(root, "describe", "--tags", "--long", "--dirty", "--abbrev=9", "--match", "v[0-9]*")
    if d:
        return from_describe(d)
    # No release tag yet: count commits from the start.
    count, sha = _git(root, "rev-list", "--count", "HEAD"), _git(root, "rev-parse", "--short=9", "HEAD")
    if not count or not sha:
        return None
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    return from_describe(f"v0.0.0-{count}-g{sha}{'-dirty' if dirty else ''}")


@functools.lru_cache(maxsize=None)
def get_version() -> str:
    root = Path(__file__).resolve().parents[2]       # src/rprof/_version.py -> the repository
    if (root / ".git").exists():
        v = from_git(root)
        if v:
            return v
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("rprof")
        except PackageNotFoundError:
            return UNKNOWN
    except ImportError:
        return UNKNOWN
