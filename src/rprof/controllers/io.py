"""Block I/O: io.rbps/wbps/riops/wiops -> one io.max line for the resolved whole disk."""

from __future__ import annotations

from typing import Any

from ..target.cgroup import parse_nested
from .base import Controller, FileCache

IO_KEYS = ("rbytes", "wbytes", "rios", "wios")


def io_max_line(dev: str, limits: dict[str, Any]) -> str:
    def v(k):
        x = limits.get(k)
        return "max" if x is None else str(int(x))
    return f"{dev} rbps={v('io.rbps')} wbps={v('io.wbps')} riops={v('io.riops')} wiops={v('io.wiops')}"


class IoController(Controller):
    name = "io"
    knobs = ("io.rbps", "io.wbps", "io.riops", "io.wiops")

    def capabilities(self):
        if not self.target.cgroup.has("io.max"):
            reason = "io controller not enabled for the target (no io.max)"
        elif not self.target.io_device:
            reason = "no block device resolved (use --io-device)"
        else:
            reason = None
        return {k: reason for k in self.knobs}

    def files_for(self, knobs):
        if set(knobs) & set(self.knobs) and self.target.cgroup.has("io.max"):
            return [self.target.cgroup.file("io.max")]
        return []

    def apply(self, limits: dict[str, Any], changed: set[str]) -> list[str]:
        errs: list[str] = []
        if changed & set(self.knobs) and self.target.io_device:
            self.write(self.target.cgroup.file("io.max"), io_max_line(self.target.io_device, limits), errs)
        return errs

    def sample(self, out: dict, fc: FileCache) -> None:
        txt = fc.read(self.target.cgroup.fstr("io.stat"))
        if txt is None:
            return
        io = {}
        for dev, d in parse_nested(txt).items():
            io[dev] = {k: d[k] for k in IO_KEYS if k in d}
        # io.stat has no line for a device until its first I/O: that counter is zero, not missing.
        if self.target.io_device and self.target.io_device not in io:
            io[self.target.io_device] = {k: 0 for k in IO_KEYS}
        out["io"] = io
