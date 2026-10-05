"""``report.json`` / ``report.md``: per segment and knob, the limit, usage and whether it bound."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import knobs as K
from ..units import fmt_bytes, fmt_rate_bits
from ..util import JsonlWriter, write_json
from .binding import CONTEXT_KNOBS, MEMORY_BASES, Thresholds, evaluate
from .data import RunData
from .timeline import build_timeline

SKIP = {"net.allow"}


def _segment_windows(rd: RunData) -> list[dict]:
    T = rd.t_end
    out = []
    for s in rd.profile.segments:
        a, b = max(0.0, s.t0), min(s.t1, T)
        out.append({"segment": s.index, "t0": s.t0, "t1": s.t1, "label": s.label,
                    "windows": [(a, b)] if b > a else [], "reached": s.t0 < T})
    # Segment 0: wherever no segment is active.
    pts = sorted({0.0, T} | {x for s in rd.profile.segments for x in (s.t0, s.t1) if 0 < x < T})
    w0 = [(a, b) for a, b in zip(pts[:-1], pts[1:]) if b > a and not rd.profile.active((a + b) / 2)]
    out.insert(0, {"segment": 0, "t0": 0.0, "t1": None, "label": "defaults", "windows": w0, "reached": bool(w0)})
    return out


def _knobs_for(rd: RunData, seg: int) -> dict[str, tuple[Any, Any]]:
    """knob -> (canonical limit, raw value) evaluated in this segment."""
    p = rd.profile
    vals: dict[str, tuple[Any, Any]] = {}
    own = p.segments[seg - 1] if seg else None
    for k, v in p.defaults.items():
        if k.startswith("harness.") or k in SKIP or K.is_unified(k):
            continue
        if own and k in own.values:
            continue
        if not K.is_default(k, v) and k not in CONTEXT_KNOBS:
            vals[k] = (v, p.defaults_raw[k])
    if own:
        for k, v in own.values.items():
            if k.startswith("harness.") or k in SKIP:
                continue
            vals[k] = (v, own.raw[k])
    return vals


def build_report(rd: RunData, thresholds: Thresholds | None = None,
                 memory_basis: str = "non_reclaimable") -> dict:
    if memory_basis not in MEMORY_BASES:
        raise ValueError(f"memory basis must be one of {', '.join(MEMORY_BASES)}")
    th = thresholds or Thresholds()
    segs = []
    for sw in _segment_windows(rd):
        win = sw["windows"]
        started = [c for c in rd.calls if any(a <= c.t0 < b for a, b in win)]
        failed = [c for c in rd.calls if c.failed and c.t1 is not None and any(a < c.t1 <= b for a, b in win)]
        knobs = []
        if win:
            for k, (lim, raw) in sorted(_knobs_for(rd, sw["segment"]).items()):
                row = evaluate(rd, k, lim, win, failed, th, rd.mode, memory_basis)
                row["limit_raw"] = raw
                knobs.append(row)
        evaluable = [r for r in knobs if r["bound"] is not None]
        no_effect = (not any(r["bound"] for r in evaluable)) if (evaluable and rd.mode == "enforce") else None
        segs.append({"segment": sw["segment"], "t0": sw["t0"], "t1": sw["t1"], "label": sw["label"],
                     "reached": sw["reached"], "windows": [[round(a, 3), round(b, 3)] for a, b in win],
                     "duration_s": round(sum(b - a for a, b in win), 3), "no_effect": no_effect,
                     "calls": {"started": len(started), "failed": len(failed),
                               "started_ids": [c.call_id for c in started],
                               "failed_ids": [c.call_id for c in failed]},
                     "knobs": knobs})
    calls = []
    for c in rd.calls:
        t1 = c.end(rd.t_end)
        seg_ids = sorted({x for s in rd.profile.segments if s.t0 < t1 and c.t0 < s.t1 for x in [s.index]})
        if any(not rd.profile.active(t) for t in (c.t0, (c.t0 + t1) / 2, max(c.t0, t1 - 1e-6))):
            seg_ids = [0] + seg_ids
        changes = [b for b in rd.profile.boundaries() if c.t0 < b < t1]
        # The tightest memory cap in force at any point of the call, and whether the call fit under it.
        caps = [rd.profile.limits_at(x)["mem.max"] for x in [c.t0] + changes]
        caps = [x for x in caps if x is not None]
        mem_cap = min(caps) if caps else None
        peak = c.mem_peak_nonreclaimable if c.mem_peak_nonreclaimable is not None else c.mem_peak
        calls.append({"call_id": c.call_id, "cmd": c.cmd, "step": c.step, "t0": round(c.t0, 4),
                      "t1": None if c.t1 is None else round(c.t1, 4),
                      "duration_s": round(t1 - c.t0, 4), "exit_code": c.exit_code, "timed_out": c.timed_out,
                      "cause": c.cause, "evidence": c.evidence, "failed": c.failed, "finished": c.finished,
                      "segments": seg_ids, "limit_changes_during_call": changes,
                      "mem_peak_bytes": c.mem_peak, "mem_peak_nonreclaimable_bytes": c.mem_peak_nonreclaimable,
                      "mem_max_bytes": mem_cap,
                      "mem_fit": None if mem_cap is None or peak is None else peak <= mem_cap,
                      "limits_at_start": K.limits_json(rd.profile.limits_at(c.t0))})
    warnings = [{"t": e["t"], "type": e["type"], "code": e.get("code"), "message": e.get("message")}
                for e in rd.events if e.get("type") in ("warning", "error")]
    applies = [e for e in rd.events if e.get("type") == "segment_applied" and e.get("changed")]
    ms = sorted(e.get("apply_ms", 0) for e in applies if e.get("enforced"))
    return {
        "run_id": rd.run_id, "mode": rd.mode, "profile": rd.profile.name, "t_end": round(rd.t_end, 3),
        "samples": len(rd.samples), "thresholds": th.__dict__,
        "memory_basis": rd.mem_series(memory_basis)[1],
        "segments": segs, "calls": calls,
        "summary": {"calls": len(rd.calls), "failed": sum(c.failed for c in rd.calls),
                    "no_effect_segments": [s["segment"] for s in segs if s["no_effect"]],
                    "apply_ms_p95": ms[int(round(0.95 * (len(ms) - 1)))] if ms else None,
                    "warnings": len(warnings)},
        "warnings": warnings,
    }


# ---------------------------------------------------------------- markdown

def _fmt_usage(unit: str | None, x: float | None) -> str:
    if x is None:
        return "-"
    if unit == "bytes":
        return fmt_bytes(int(x))
    if unit == "bytes/s":
        return f"{fmt_bytes(int(x))}/s"
    if unit == "bit/s":
        return fmt_rate_bits(int(x))
    if unit == "cores":
        return f"{x:.2f}"
    return f"{x:.0f}"


def _fmt_evidence(ev: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in ev.items() if v is not None) or "-"


def render_md(rep: dict, meta: dict | None = None) -> str:
    meta = meta or {}
    L = [f"# rprof report: {rep['run_id']}", ""]
    host = (meta.get("host") or {})
    L.append(f"- mode: **{rep['mode']}**; profile: `{rep['profile']}`; duration {rep['t_end']:.1f} s; "
             f"{rep['samples']} samples")
    if host:
        L.append(f"- host: {host.get('hostname')} (kernel {host.get('kernel')}, Docker {host.get('docker_version')})")
    if meta.get("target"):
        L.append(f"- target: `{meta['target'].get('spec')}` → `{meta['target'].get('cgroup_path')}`")
    basis = rep.get("memory_basis")
    if basis:
        L.append("- memory usage counts " + {
            "non_reclaimable": "memory the kernel can't reclaim: current − (page cache − shmem)",
            "non_reclaimable_without_shmem": "memory the kernel can't reclaim, approximated as current − page cache "
                                             "(this run did not record shmem)",
            "total": "all memory, page cache included (memory.current)"}.get(basis, basis))
    s = rep["summary"]
    L.append(f"- tool calls: {s['calls']} ({s['failed']} failed); apply p95: {s['apply_ms_p95']} ms; "
             f"warnings/errors: {s['warnings']}")
    if s["no_effect_segments"]:
        L.append(f"- **no effect** in segments {', '.join(map(str, s['no_effect_segments']))}: "
                 "nothing bound there, so those levels tested nothing")
    L += ["", "## Segments", ""]
    measure = rep["mode"] == "measure"
    hdr = ("| seg | window (s) | knob | limit | mean | p95 | max | "
           + ("over limit (time) | peak over |" if measure else "bound | evidence |") + " calls (failed) |")
    L += [hdr, "|" + "---|" * (hdr.count("|") - 1)]
    for sg in rep["segments"]:
        win = ", ".join(f"{a:g}–{b:g}" for a, b in sg["windows"]) or ("not reached" if not sg["reached"] else "-")
        flag = " *(no effect)*" if sg["no_effect"] else ""
        calls = f"{sg['calls']['started']} ({sg['calls']['failed']})"
        if not sg["knobs"]:
            L.append(f"| {sg['segment']}{flag} | {win} | - | - | - | - | - | - | - | {calls} |")
            continue
        for i, k in enumerate(sg["knobs"]):
            u = k["usage"] or {}
            lead = f"| {sg['segment']}{flag} | {win} " if i == 0 else "| | "
            row = (lead + f"| {k['knob']} | {k['limit_raw']} | {_fmt_usage(k['unit'], u.get('mean'))} | "
                   f"{_fmt_usage(k['unit'], u.get('p95'))} | {_fmt_usage(k['unit'], u.get('max'))} | ")
            if measure:
                v = k["violation"] or {}
                tf = v.get("time_frac")
                row += (f"{tf:.0%} | {_fmt_usage(k['unit'], v.get('peak_over'))} |" if tf is not None
                        else "context | - |")
            else:
                b = k["bound"]
                row += f"{'**yes**' if b else ('no' if b is False else 'context')} | {_fmt_evidence(k['evidence'])} |"
            row += f" {calls if i == 0 else ''} |"
            L.append(row)
    if rep["calls"]:
        L += ["", "## Tool calls", "",
              "| call | cmd | start (s) | duration (s) | segments | exit | cause | peak memory (non-reclaimable) |",
              "|---|---|---|---|---|---|---|---|"]
        for c in rep["calls"]:
            cmd = c["cmd"].replace("|", "\\|")
            cmd = cmd if len(cmd) <= 60 else cmd[:57] + "..."
            exit_ = "timeout" if c["timed_out"] else ("running" if not c["finished"] else c["exit_code"])
            cause = c["cause"] or "-"
            if (c.get("evidence") or {}).get("exited_ok"):
                cause += " (process killed, call exited 0)"
            elif "output_match" in (c.get("evidence") or {}):
                cause += " (reported by the program)"
            mem = "-"
            if c.get("mem_peak_bytes") is not None:
                mem = fmt_bytes(int(c["mem_peak_bytes"]))
                if c.get("mem_peak_nonreclaimable_bytes") is not None:
                    mem += f" ({fmt_bytes(int(c['mem_peak_nonreclaimable_bytes']))})"
            L.append(f"| {c['call_id']} | `{cmd}` | {c['t0']:.1f} | {c['duration_s']:.1f} | "
                     f"{', '.join(map(str, c['segments']))} | {exit_} | {cause} | {mem} |")
    if rep["warnings"]:
        L += ["", "## Warnings and errors", ""]
        for w in rep["warnings"][:50]:
            L.append(f"- t={w['t']:.1f} {w['type']} `{w['code']}`: {w['message']}")
    return "\n".join(L) + "\n"


def write_reports(run_dir: str | Path, thresholds: Thresholds | None = None,
                  memory_basis: str = "non_reclaimable") -> dict:
    run_dir = Path(run_dir)
    rd = RunData(run_dir)
    tl = build_timeline(rd)
    p = run_dir / "timeline.jsonl"
    p.unlink(missing_ok=True)
    w = JsonlWriter(p)
    for iv in tl:
        w.write(iv)
    w.close()
    rep = build_report(rd, thresholds, memory_basis)
    write_json(run_dir / "report.json", rep)
    (run_dir / "report.md").write_text(render_md(rep, rd.meta))
    return rep
