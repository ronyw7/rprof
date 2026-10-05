"""The git-derived version (src/rprof/_version.py)."""

from __future__ import annotations

import datetime as dt
import subprocess

import pytest

import rprof
from rprof._version import from_describe, from_git

DAY = dt.date(2026, 10, 5)


@pytest.mark.parametrize("describe,want", [
    ("v0.1.0-0-gf71faddc9", "0.1.0"),                                   # a tagged commit
    ("v0.1.0-1-gf71faddc9", "0.1.1.dev1+gf71faddc9"),                   # one commit later
    ("v0.1.0-12-gf71faddc9", "0.1.1.dev12+gf71faddc9"),
    ("v0.1.0-3-gf71faddc9-dirty", "0.1.1.dev3+gf71faddc9.d20261005"),   # uncommitted changes
    ("v0.1.0-0-gf71faddc9-dirty", "0.1.1.dev0+gf71faddc9.d20261005"),
    ("v1.9-2-gabcdef123", "1.10.dev2+gabcdef123"),
    ("not-a-version", None),
])
def test_from_describe(describe, want):
    assert from_describe(describe, today=DAY) == want


def test_versions_increase_with_every_commit():
    from packaging.version import Version
    seq = ["v0.1.0-0-ga", "v0.1.0-1-gb", "v0.1.0-2-gc", "v0.1.1-0-gd", "v0.1.1-1-ge"]
    vs = [Version(from_describe(d.replace("g", "g000000", 1))) for d in seq]
    assert vs == sorted(vs) and len(set(vs)) == len(vs)


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


def test_from_git_on_a_real_repository(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "f").write_text("1")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "commit", "-qm", "one")
    assert from_git(tmp_path).startswith("0.0.1.dev1+g")             # no tag yet
    _git(tmp_path, "tag", "-a", "v0.3.0", "-m", "x")
    assert from_git(tmp_path) == "0.3.0"
    (tmp_path / "f").write_text("2")
    assert ".d" in from_git(tmp_path)                                  # uncommitted change
    _git(tmp_path, "commit", "-qam", "two")
    assert from_git(tmp_path).startswith("0.3.1.dev1+g")


def test_package_version_is_pep440():
    from packaging.version import Version
    Version(rprof.__version__)
