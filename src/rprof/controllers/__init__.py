"""Resource controllers: one per resource, each owning a few knobs."""

from __future__ import annotations

from ..knobs import KNOBS, is_unified
from .base import Controller, FileCache  # noqa: F401
from .cpu import CpuController
from .disk import DiskController
from .io import IoController
from .memory import MemoryController
from .net import NetController
from .pids import PidsController
from .unified import UnifiedController

CLASSES = (CpuController, MemoryController, IoController, PidsController, NetController, DiskController)


def build(target, snap=None, run_id: str = "", events=None, unified_files: set[str] | None = None
          ) -> list[Controller]:
    ctrls: list[Controller] = [c(target, snap, run_id, events) for c in CLASSES]
    ctrls.append(UnifiedController(target, snap, run_id, events, files=unified_files or set()))
    return ctrls


def owner(ctrls: list[Controller], knob: str) -> Controller | None:
    for c in ctrls:
        if knob in c.knobs:
            return c
    return None


def capabilities(ctrls: list[Controller]) -> dict[str, str | None]:
    caps: dict[str, str | None] = {}
    for c in ctrls:
        caps.update(c.capabilities())
    for k in KNOBS:
        caps.setdefault(k, None)
    return caps


__all__ = ["build", "owner", "capabilities", "Controller", "FileCache", "KNOBS", "is_unified"]
