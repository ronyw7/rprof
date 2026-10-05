"""``rprof apply`` / ``rprof reset``: set knobs once, outside a run, and undo them.

``apply`` keeps a live snapshot per target under ``$RPROF_STATE_DIR/live/<target>/``
(original values plus what has been applied so far); ``reset --target`` restores
from it. ``reset --run DIR`` restores from a run's ``snapshot.json``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from . import controllers as C
from . import knobs as K
from .snapshot import Snapshot, TargetLock, restore, state_dir
from .target import Target, resolve
from .units import UnitError
from .util import RprofError, RestoreFailed, write_json


def parse_assignments(items: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for it in items:
        if "=" not in it:
            raise RprofError(f"{it!r}: expected knob=value, e.g. mem.max=512Mi", 2)
        name, raw = it.split("=", 1)
        name = name.strip()
        if K.is_unified(name):
            out[name] = raw
            continue
        kn = K.knob(name)
        if kn is None:
            raise RprofError(f"unknown knob {name!r}; known: {', '.join(K.KNOBS)}", 2)
        if not kn.enforced:
            raise RprofError(f"{name} is a harness knob; rprof does not enforce it", 2)
        val: Any = raw
        if name == "net.allow":
            val = [x for x in raw.split(",") if x]
        elif name in ("cpu.cores",):
            val = raw if raw == "max" else _num(raw)
        elif name in ("io.riops", "io.wiops", "pids.max"):
            val = raw if raw == "max" else _int(raw)
        elif name == "mem.swap_max" and raw.isdigit():
            val = int(raw)
        try:
            out[name] = kn.parse(val)
        except UnitError as e:
            raise RprofError(f"{name}: {e}", 2) from None
    return out


def _num(s: str):
    try:
        return float(s)
    except ValueError:
        return s


def _int(s: str):
    try:
        return int(s)
    except ValueError:
        return s


def live_dir(t: Target) -> Path:
    return state_dir() / "live" / t.lock_key


def apply_once(target_spec: str, values: dict[str, Any], **resolve_kw) -> tuple[Target, list[str]]:
    t = resolve(target_spec, **resolve_kw)
    with TargetLock(t.lock_key, "apply"):
        d = live_dir(t)
        d.mkdir(parents=True, exist_ok=True)
        snap_path = d / "snapshot.json"
        snap = Snapshot.load(snap_path) if snap_path.exists() else Snapshot(snap_path, "apply", t.to_meta())
        snap.path = snap_path
        applied_path = d / "applied.json"
        applied = json.loads(applied_path.read_text()) if applied_path.exists() else {}
        # Same group rule as `run`: touching memory also pins swap to its default (0) unless given,
        # otherwise mem.max only pushes the workload into swap on hosts that have it.
        if (any(k in values for k in ("mem.max", "mem.high")) and "mem.swap_max" not in values
                and "mem.swap_max" not in applied):
            values = {**values, "mem.swap_max": K.KNOBS["mem.swap_max"].default}
        unified = {k[len(K.UNIFIED_PREFIX):] for k in values if K.is_unified(k)}
        ctrls = C.build(t, snap, "apply", None, unified)
        caps = C.capabilities(ctrls)
        bad = {k: caps[k] for k in values if caps.get(k) and k != "net.rate"}
        if bad:
            raise RprofError("cannot apply: " + "; ".join(f"{k}: {r}" for k, r in bad.items()), 73)
        for c in ctrls:
            c.snapshot(values)
        full = K.defaults()
        full.update(applied)
        full.update(values)
        # Controllers are stateless across invocations: tell net/disk the whole group changed.
        errors: list[str] = []
        for c in ctrls:
            mine = {k for k in values if k in c.knobs}
            if c.name == "net" and mine:
                mine = set(c.knobs)
            if mine:
                errors.extend(f"{c.name}: {e}" for e in c.apply(full, mine))
        applied.update(values)
        write_json(applied_path, applied)
        snap.save()
    return t, errors


def reset_target(target_spec: str, **resolve_kw) -> tuple[Target, list[str], bool]:
    t = resolve(target_spec, **resolve_kw)
    with TargetLock(t.lock_key, "reset"):
        d = live_dir(t)
        snap_path = d / "snapshot.json"
        if not snap_path.exists():
            return t, [], False
        snap = Snapshot.load(snap_path)
        errors = restore(snap)
        if not errors:
            shutil.rmtree(d, ignore_errors=True)
        return t, errors, True


def reset_run(run_dir: str | Path) -> list[str]:
    p = Path(run_dir) / "snapshot.json"
    if not p.exists():
        raise RprofError(f"{p} not found (measure-mode runs write no snapshot)", 2)
    snap = Snapshot.load(p)
    errors = restore(snap)
    if errors:
        raise RestoreFailed("restore failed:\n  " + "\n  ".join(errors))
    return errors
