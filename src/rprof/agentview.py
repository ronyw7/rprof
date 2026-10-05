"""Agent view: current and upcoming limits, as ``state.json`` and ``now.txt``.

``visibility`` decides the content: ``none`` writes nothing, ``current`` shows the
limits in force, ``full`` adds past and upcoming intervals. Files are written
atomically into the view directory (mounted read-only at ``/run/rprof`` in the
sandbox) and copied into ``<run-dir>/agentview/``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import knobs as K
from .profile import Profile
from .units import fmt_num
from .util import atomic_write, dumps

NEXT_SHOWN = 3


def _phrases(flat: dict[str, Any]) -> list[str]:
    return [p for p in K.describe_limits({k: v for k, v in flat.items() if not k.startswith("harness.")})
            if not p.startswith("deadline")]


class AgentView:
    def __init__(self, profile: Profile, view_dir: Path | None = None, copy_dir: Path | None = None):
        self.profile = profile
        self.visibility = profile.visibility
        self.view_dir = Path(view_dir) if view_dir else None
        self.copy_dir = Path(copy_dir) if copy_dir else None
        self.step: int | None = None
        self._last: tuple | None = None
        self._last_content: tuple | None = None

    # ------------------------------------------------------------ content
    def data(self, t: float) -> dict | None:
        if self.visibility == "none":
            return None
        p = self.profile
        seg, active = p.segment_at(t)
        cur0, cur1 = 0.0, None
        ivs = p.intervals()
        for a, b in ivs:
            if a <= t and (b is None or t < b):
                cur0, cur1 = a, b
                break
        d: dict[str, Any] = {"t": round(t, 3), "visibility": self.visibility, "segment": seg}
        if len(active) > 1:
            d["active_segments"] = active
        if self.step is not None:
            d["step"] = self.step
        d["current"] = {"t0": cur0, "t1": cur1, "limits": K.limits_json(p.limits_at(t))}
        if self.visibility == "full":
            d["segments_total"] = len(p.segments)
            d["past"] = [{"t0": a, "t1": b, "limits": K.limits_json(p.limits_at(a))}
                         for a, b in ivs if b is not None and b <= cur0]
            d["upcoming"] = [{"t0": a, "t1": b, "changes": p.changes_from_defaults(a)}
                             for a, b in ivs if a > t]
        return d

    def text(self, t: float) -> str | None:
        d = self.data(t)
        if d is None:
            return None
        p = self.profile
        lines = []
        if self.visibility == "full":
            n = len(p.segments)
            head = f"t = {t:.0f} s · " + (f"segment {d['segment']} of {n}" if d["segment"] else
                                          f"defaults ({n} segment{'s' if n != 1 else ''} in profile)")
        else:
            head = f"t = {t:.0f} s"
        lines.append(head)
        now = _phrases(p.limits_at(t))
        lines.append("now:  " + (" · ".join(now) if now else "no limits"))
        if self.visibility == "full":
            nxt = []
            for u in d["upcoming"][:NEXT_SHOWN]:
                vals = {k: K.KNOBS[k].parse(v) if not K.is_unified(k) else v for k, v in u["changes"].items()}
                ph = _phrases(vals)
                if not ph:  # a segment that only changes harness knobs (e.g. its deadline)
                    ph = [d for k, v in vals.items() if k.startswith("harness.") and (d := K.describe(k, v))]
                desc = " · ".join(ph) if ph else ("back to defaults" if not u["changes"] else
                                                  "no resource limit changes")
                nxt.append(f"at {fmt_num(u['t0'])} s → {desc}")
            lines.append("next: " + (" · ".join(nxt) if nxt else "no further changes"))
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------ files
    def _dirs(self) -> list[Path]:
        return [d for d in (self.view_dir, self.copy_dir) if d is not None]

    def clear(self) -> None:
        for d in self._dirs():
            for name in ("now.txt", "state.json"):
                try:
                    os.unlink(d / name)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def write(self, t: float, force: bool = False) -> None:
        if self.visibility == "none":
            return
        d = self.data(t)
        txt = self.text(t)
        key = (txt, d.get("segment") if d else None)
        if key == self._last:   # now.txt shows whole seconds: skip identical rewrites
            return
        # The run-dir copy only needs the view when its content (not the clock) changes.
        content = (d.get("segment") if d else None, d.get("step") if d else None)
        copy = force or content != self._last_content
        self._last, self._last_content = key, content
        for dd in self._dirs():
            if dd == self.copy_dir and not copy and self.view_dir is not None:
                continue
            dd.mkdir(parents=True, exist_ok=True)
            atomic_write(dd / "state.json", dumps(d) + "\n", fsync=False)
            atomic_write(dd / "now.txt", txt or "", fsync=False)
