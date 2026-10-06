"""rprof command line (design: CLI reference)."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

import typer

from . import __version__
from .util import RprofError

app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_enable=False,
                  help="OS resource profiles for sandboxed agents.")
gen_app = typer.Typer(no_args_is_help=True, help="Generate profiles (optional helpers; output is a normal profile).")
app.add_typer(gen_app, name="gen")

TargetOpt = typer.Option(..., "--target", "-t", help="docker:<name|id> or cgroup:<path>")
RunTargetOpt = typer.Option(..., "--target", "-t", help="docker:<name|id>, cgroup:<path>, or harbor: the container "
                            "of the Harbor trial the command after `--` starts")


def _die(e: RprofError) -> None:
    typer.echo(f"rprof: {e}", err=True)
    raise typer.Exit(e.exit_code)


def _resolve(target: str, **kw):
    from .target import resolve
    try:
        return resolve(target, **kw)
    except RprofError as e:
        _die(e)


@app.callback(invoke_without_command=True)
def _main(version: bool = typer.Option(False, "--version", help="Print the version and exit."),
          verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging to stderr.")):
    if os.geteuid() == 0 and not os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKER_CONTEXT"):
        # rprof controls the standard (rootful) daemon. Under `sudo -E` the invoking user's HOME, and
        # with it their Docker config, is kept; its current context may be a rootless daemon.
        os.environ["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")
    # rprof's logger runs at DEBUG during a run so rprof.log gets everything; records reach the
    # console handler through propagation, so the console needs its own level.
    for h in logging.getLogger().handlers:
        h.setLevel(level)
    if version:
        typer.echo(f"rprof {__version__}")
        raise typer.Exit(0)


# ---------------------------------------------------------------- doctor / inspect / watch

@app.command()
def doctor(target: Optional[str] = typer.Option(None, "--target", "-t"),
           deep: bool = typer.Option(False, "--deep", help="Add quick enforcement probes (root)."),
           as_json: bool = typer.Option(False, "--json")):
    """Check cgroup v2, enabled controllers, tools and kernel features."""
    from .doctor import checks
    t = _resolve(target) if target else None
    cs = checks(t, deep)
    if as_json:
        typer.echo(json.dumps([c.__dict__ for c in cs], indent=2))
    else:
        for c in cs:
            mark = {True: "ok  ", False: "FAIL", None: "warn"}[c.ok]
            typer.echo(f"{mark}  {c.name:<34} {c.detail}")
    raise typer.Exit(1 if any(c.ok is False for c in cs) else 0)


@app.command()
def inspect(target: str = TargetOpt, as_json: bool = typer.Option(False, "--json"),
            io_device: Optional[str] = typer.Option(None, "--io-device"),
            data_path: str = typer.Option("/data", "--data-path")):
    """Show resolved paths, PIDs, devices and current limits."""
    from .inspect_ import inspect as do_inspect
    t = _resolve(target, io_device=io_device, data_path=data_path)
    d = do_inspect(t)
    if as_json:
        typer.echo(json.dumps(d, indent=2))
        return
    typer.echo(f"target      {d['spec']}  ({d['kind']})")
    typer.echo(f"cgroup      {d['cgroup_path']}")
    typer.echo(f"controllers {' '.join(d['controllers'])}" + (
        f"  (missing: {' '.join(d['missing_controllers'])})" if d['missing_controllers'] else ""))
    if d.get("container_id"):
        typer.echo(f"container   {d['container_name']} {d['container_id'][:12]}  init pid {d['init_pid']}")
    typer.echo(f"io device   {d['io_device'] or '-'} {d['io_device_name'] or ''} ({d['io_device_source'] or 'none'})")
    dm = d.get("data_mount")
    typer.echo(f"data fs     {(dm['container_path'] or '') + ' -> ' + dm['host_path'] + ' (' + str(dm['fstype']) + ')' if dm else '-'}")
    for n in d["net"]:
        typer.echo(f"net         {n['spec']} {n['ifname']} {','.join(n['addrs'])} <-> host {n['host_veth'] or '?'}")
    typer.echo("limits")
    for k, v in d["limits"].items():
        if v is not None:
            typer.echo(f"  {k:<22} {v if v else '(empty)'}")
    for k, v in d["qdiscs"].items():
        typer.echo(f"qdisc {k}: {v}")
    typer.echo(f"processes   {d['process_count']}")
    for p in d["processes"][:20]:
        typer.echo(f"  {p['pid']:>8}  adj={p['oom_score_adj']:>5}  {p['cmd']}")


@app.command()
def watch(target: str = TargetOpt, hz: float = typer.Option(10.0, "--hz"),
          out: Optional[str] = typer.Option(None, "--out", help="Record samples to a JSON-lines file."),
          duration: Optional[float] = typer.Option(None, "--duration"),
          once: bool = typer.Option(False, "--once", help="Print one 1 s sample and exit.")):
    """Live usage table."""
    from .watch import watch as do_watch
    t = _resolve(target)
    do_watch(t, hz=hz, out=out, duration=duration, once=once)


# ---------------------------------------------------------------- apply / reset

@app.command()
def apply(knobs: List[str] = typer.Argument(..., help="knob=value, e.g. mem.max=512Mi cpu.cores=0.5"),
          target: str = TargetOpt, io_device: Optional[str] = typer.Option(None, "--io-device"),
          data_path: str = typer.Option("/data", "--data-path"),
          data_dir: Optional[str] = typer.Option(None, "--data-dir")):
    """Set knobs once; they stay until `rprof reset`."""
    from .live import apply_once, parse_assignments
    from .util import is_root
    try:
        if not is_root():
            raise RprofError("apply needs root", 72)
        vals = parse_assignments(knobs)
        t, errors = apply_once(target, vals, io_device=io_device, data_path=data_path, data_dir=data_dir)
    except RprofError as e:
        _die(e)
    for e in errors:
        typer.echo(f"error: {e}", err=True)
    if errors:
        raise typer.Exit(70)
    typer.echo(f"applied to {t.spec} ({t.cgroup.rel}): " + ", ".join(knobs))


@app.command()
def reset(target: Optional[str] = typer.Option(None, "--target", "-t"),
          run: Optional[Path] = typer.Option(None, "--run", help="Restore from <run-dir>/snapshot.json.")):
    """Restore original limits from the live snapshot or a run directory."""
    from .live import reset_run, reset_target
    from .util import is_root
    try:
        if not is_root():
            raise RprofError("reset needs root", 72)
        if run is not None:
            reset_run(run)
            typer.echo(f"restored from {run}/snapshot.json")
            return
        if not target:
            raise RprofError("give --target or --run", 2)
        t, errors, had = reset_target(target)
        if not had:
            typer.echo(f"nothing to reset for {t.spec} (no live state from `rprof apply`)")
            return
        if errors:
            raise RprofError("restore failed:\n  " + "\n  ".join(errors), 75)
        typer.echo(f"restored {t.spec}")
    except RprofError as e:
        _die(e)


# ---------------------------------------------------------------- validate / run

def _print_schedule(p) -> None:
    from . import knobs as K
    end = p.end()
    n = len(p.segments)
    span = f" · 0–{end:g} s" if end is not None else ""
    typer.echo(f"OK · {p.name} · {p.clock} clock · {n or 'no'} segment{'s' if n != 1 else ''}"
               f"{span} · visibility {p.visibility}")
    rows = [("segment", "from", "to", "changes")]
    for s in p.segments:
        ch = ", ".join(f"{k} {v}" for k, v in s.raw.items() if not k.startswith("harness."))
        hz = ", ".join(f"{k} {v}" for k, v in s.raw.items() if k.startswith("harness."))
        rows.append((str(s.index), f"{s.t0:g} s", f"{s.t1:g} s", ch + (f"  [{hz}]" if hz else "")))
    if len(rows) > 1:
        w = [max(len(r[i]) for r in rows) for i in range(3)]
        for r in rows:
            typer.echo(f"{r[0]:<{w[0]}}  {r[1]:<{w[1]}}  {r[2]:<{w[2]}}  {r[3]}")
    d = [f"{k} {p.defaults_raw[k]}" for k in sorted(p.explicit)
         if not K.is_unified(k) and k in p.defaults_raw and not K.is_default(k, p.defaults[k])]
    typer.echo("outside segments: defaults" + (f" ({', '.join(d)})" if d else ""))


@app.command()
def validate(profile: Path = typer.Argument(..., exists=False),
             target: Optional[str] = typer.Option(None, "--target", "-t", help="Also check host support."),
             quiet: bool = typer.Option(False, "--quiet", "-q")):
    """Check a profile (schema, units, overlaps, host support) and print its schedule."""
    from .profile import ProfileError, load_profile
    try:
        p = load_profile(profile)
    except ProfileError as e:
        for path, msg in e.problems:
            typer.echo(f"{path}: {msg}" if path else msg)
        raise typer.Exit(1)
    problems = []
    if target:
        from .profile.validate import host_problems
        from .runner import load_selftest_caps
        try:
            from .target import resolve
            problems = host_problems(resolve(target), p, load_selftest_caps(None))
        except RprofError as e:
            problems.append(("host", str(e)))
    if problems:
        for path, msg in problems:
            typer.echo(f"{path}: {msg}")
        raise typer.Exit(1)
    if not quiet:
        _print_schedule(p)


@app.command()
def describe(profile: Path = typer.Argument(..., exists=True, dir_okay=False),
             capabilities: Optional[str] = typer.Option(None, "--capabilities",
                                                        help="selftest capabilities.json, for the measured disk speed.")):
    """Print what an agent is told about its resources under a profile (what --tell-agent appends)."""
    from .describe import describe as text_for
    from .profile import ProfileError, load_profile
    from .runner import load_selftest_caps
    try:
        p = load_profile(profile)
    except ProfileError as e:
        for path, msg in e.problems:
            typer.echo(f"{path}: {msg}" if path else msg, err=True)
        raise typer.Exit(1)
    text = text_for(p, load_selftest_caps(capabilities))
    if text is None:
        typer.echo("rprof: the profile's visibility is none: the agent is told nothing", err=True)
        raise typer.Exit(1)
    typer.echo(text)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": False})
def run(target: str = RunTargetOpt,
        profile: Optional[Path] = typer.Option(None, "--profile", "-p", help="Profile YAML (default: unlimited)."),
        mode: str = typer.Option("enforce", "--mode", help="enforce (write limits) or measure (record only)."),
        hz: float = typer.Option(10.0, "--hz", min=1, max=100),
        runs_dir: Path = typer.Option(Path("runs"), "--runs-dir"),
        name: Optional[str] = typer.Option(None, "--name", help="Run label (default: profile name)."),
        harness: str = typer.Option("outside", "--harness", help="outside or inside"),
        protect: List[str] = typer.Option([], "--protect", help="Regex on cmdline; matching PIDs get oom_score_adj=-1000."),
        net: List[str] = typer.Option([], "--net", help="docker:<name> whose netns gets net.* knobs (repeatable)."),
        io_device: Optional[str] = typer.Option(None, "--io-device", help="MAJ:MIN or /dev/... for io.max."),
        data_path: str = typer.Option("/data", "--data-path", help="Container path of the data filesystem."),
        data_dir: Optional[str] = typer.Option(None, "--data-dir", help="Host path of the data filesystem."),
        view_dir: Optional[str] = typer.Option(None, "--view-dir", help="Agent view dir (default /var/lib/rprof/view/<name>)."),
        ctl_dir: Optional[str] = typer.Option(None, "--ctl-dir", help="Host dir for the in-sandbox socket (inside mode)."),
        allow_degraded: bool = typer.Option(False, "--allow-degraded"),
        duration: Optional[float] = typer.Option(None, "--duration", help="Stop after this many seconds."),
        capabilities: Optional[str] = typer.Option(None, "--capabilities", help="selftest capabilities.json"),
        no_report: bool = typer.Option(False, "--no-report"),
        hide_limits: bool = typer.Option(False, "--hide-limits",
                                         help="Mask /sys/fs/cgroup in the sandbox so it can't read its limits."),
        tell_agent: bool = typer.Option(False, "--tell-agent", help="With --target harbor: tell the agent "
                                        "`rprof describe` of the profile (where: --tell-via)."),
        tell_via: str = typer.Option("instruction", "--tell-via", help="instruction: append it to the task's "
                                     "instruction; system-prompt: to the agent's system prompt (claude-code)."),
        agent_start: Optional[str] = typer.Option(None, "--agent-start", help="With --target harbor: start the "
                                                  "profile when a process whose command line contains this appears "
                                                  "(default: known per Harbor agent)."),
        command: Optional[List[str]] = typer.Argument(None, help="Command to launch after `--`.")):
    """Apply a profile (or only measure against it), record and report; optionally launch the harness."""
    from .profile import ProfileError
    from .runner import RunOptions
    from .runner import run as do_run
    if harness not in ("outside", "inside"):
        typer.echo("rprof: --harness must be outside or inside", err=True)
        raise typer.Exit(2)
    opts = RunOptions(target=target, profile=str(profile) if profile else None, mode=mode, hz=hz,
                      runs_dir=str(runs_dir), name=name, harness=harness, protect=list(protect), net=list(net),
                      io_device=io_device, data_path=data_path, data_dir=data_dir, view_dir=view_dir,
                      ctl_dir=ctl_dir, allow_degraded=allow_degraded, duration=duration,
                      command=list(command or []), capabilities=capabilities, report=not no_report,
                      hide_limits=hide_limits, agent_start=agent_start, tell_agent=tell_agent,
                      tell_via=tell_via)
    try:
        code, sess = do_run(opts)
    except ProfileError as e:
        for path, msg in e.problems:
            typer.echo(f"{path}: {msg}" if path else msg, err=True)
        raise typer.Exit(1)
    except RprofError as e:
        _die(e)
    if sess is None:                       # --target harbor: the trial never started
        raise typer.Exit(code)
    typer.echo(f"rprof: run {sess.run_id} ended ({sess.end_reason}); results in {sess.run_dir}", err=True)
    if sess.restore_errors:
        typer.echo("rprof: RESTORE FAILED; run `sudo rprof reset --run " + str(sess.run_dir) + "`", err=True)
    raise typer.Exit(code)


# ---------------------------------------------------------------- harness helpers

def _client(run_dir: Optional[str]):
    from .client import Client, RprofUnavailable
    try:
        return Client(run_dir=run_dir, timeout_s=2.0)
    except RprofUnavailable as e:
        typer.echo(f"rprof: {e}", err=True)
        raise typer.Exit(71)


@app.command()
def mark(label: str = typer.Argument(...),
         run: Optional[str] = typer.Option(None, "--run", envvar="RPROF_RUN"),
         data: List[str] = typer.Option([], "--data", help="key=value (repeatable)")):
    """Send a mark event from a shell."""
    from .client import RprofRequestError, RprofUnavailable
    c = _client(run)
    kv = dict(x.split("=", 1) for x in data if "=" in x)
    try:
        c.mark(label, **kv)
    except (RprofUnavailable, RprofRequestError) as e:
        typer.echo(f"rprof: {e}", err=True)
        raise typer.Exit(71)


@app.command()
def now(run: Optional[str] = typer.Option(None, "--run", envvar="RPROF_RUN"),
        as_json: bool = typer.Option(False, "--json")):
    """Print the agent's current view (what `visibility` allows)."""
    from .client import NOT_VISIBLE, RprofRequestError, RprofUnavailable
    try:
        v = _client(run).view(_via="now")
    except (RprofUnavailable, RprofRequestError) as e:
        typer.echo(f"rprof: {e}", err=True)
        raise typer.Exit(71)
    if v is None:
        typer.echo(NOT_VISIBLE)
    else:
        typer.echo(json.dumps(v.data, indent=2) if as_json else v.text.rstrip("\n"))


# ---------------------------------------------------------------- analysis

def _thresholds(items: List[str]):
    from .report.binding import Thresholds
    try:
        return Thresholds.from_dict(dict(x.split("=", 1) for x in items))
    except ValueError as e:
        typer.echo(f"rprof: {e}", err=True)
        raise typer.Exit(2)


@app.command()
def report(run_dir: Path = typer.Argument(..., exists=True, file_okay=False),
           threshold: List[str] = typer.Option([], "--threshold",
                                               help="throttle_frac=0.05, pressure_frac=0.05, throughput_frac=0.8"),
           memory_basis: str = typer.Option("non_reclaimable", "--memory-basis",
                                            help="non_reclaimable (page cache excluded) or total"),
           as_json: bool = typer.Option(False, "--json")):
    """Per-segment binding report (rewrites report.json, report.md and timeline.jsonl)."""
    from .report.report import render_md, write_reports
    if memory_basis not in ("non_reclaimable", "total"):
        typer.echo("rprof: --memory-basis must be non_reclaimable or total", err=True)
        raise typer.Exit(2)
    rep = write_reports(run_dir, _thresholds(threshold), memory_basis)
    typer.echo(json.dumps(rep, indent=2) if as_json else render_md(rep, json.loads((run_dir / "meta.json").read_text())
                                                                   if (run_dir / "meta.json").exists() else {}))


@app.command()
def timeline(run_dir: Path = typer.Argument(..., exists=True, file_okay=False),
             as_json: bool = typer.Option(False, "--json")):
    """Per-interval view of running calls, limits and usage."""
    from .report.data import RunData
    from .report.timeline import build_timeline, render_timeline
    rd = RunData(run_dir)
    rows = build_timeline(rd)
    if as_json:
        for r in rows:
            typer.echo(json.dumps(r))
    else:
        typer.echo(render_timeline(rd, rows), nl=False)


@app.command()
def plot(runs: Optional[List[Path]] = typer.Argument(None, help="Run directories; several are overlaid."),
         out: Optional[Path] = typer.Option(None, "-o", "--out", help="Output file, or directory for --paper. "
                                            "Default: in the first run's directory."),
         row: bool = typer.Option(False, "--row", help="All metrics in one figure, side by side (the default)."),
         paper: bool = typer.Option(False, "--paper", help="One single-column figure per metric."),
         dashboard: bool = typer.Option(False, "--dashboard", help="One run's debugging view: every metric, "
                                        "tool calls and failures."),
         style: str = typer.Option("classic", "--style", help="classic or bold."),
         metrics: Optional[str] = typer.Option(None, "--metrics", help="Comma-separated; see --list-metrics."),
         labels: Optional[List[str]] = typer.Option(None, "--label", help="Legend label for each run, in order."),
         fmt: str = typer.Option("pdf", "--format", help="pdf, png or svg, when -o doesn't say."),
         width: Optional[float] = typer.Option(None, "--width", help="Figure width in inches. Default: 2.4 per "
                                               "panel of a row; 3.33 (one column) for --paper."),
         list_metrics: bool = typer.Option(False, "--list-metrics", help="List the metrics and exit.")):
    """Figures of usage over a run, with limits as step lines. Several runs are overlaid."""
    try:
        from .report import figures
        from .report.plot import plot_run
    except ImportError as e:
        typer.echo(f"rprof: plotting needs matplotlib (pip install 'rprof[plot]'): {e}", err=True)
        raise typer.Exit(2)
    if list_metrics:
        for m in figures.METRICS.values():
            knobs = ", ".join(k for k, _ in m.knobs)
            typer.echo(f"{m.name:<14} {m.ylabel:<22} {'default' if m.name in figures.CORE else '':<8} "
                       f"{'limits: ' + knobs if knobs else ''}".rstrip())
        raise typer.Exit(0)
    if not runs:
        typer.echo("rprof: give at least one run directory", err=True)
        raise typer.Exit(2)
    if row + paper + dashboard > 1:
        typer.echo("rprof: choose one of --row, --paper and --dashboard", err=True)
        raise typer.Exit(2)
    for r in runs:
        if not r.is_dir():
            typer.echo(f"rprof: {r} is not a run directory", err=True)
            raise typer.Exit(2)
    names = [m.strip() for m in metrics.split(",") if m.strip()] if metrics else None
    try:
        if dashboard:
            if len(runs) > 1:
                typer.echo("rprof: --dashboard shows one run", err=True)
                raise typer.Exit(2)
            typer.echo(str(plot_run(runs[0], out or runs[0] / "run.png")))
        elif paper:
            for p in figures.plot_paper(runs, out or runs[0] / "figures", style, names, labels, fmt, width):
                typer.echo(str(p))
        else:
            typer.echo(str(figures.plot_row(runs, out or runs[0] / f"plot.{fmt}", style, names, labels, width)))
    except (ValueError, FileNotFoundError) as e:
        typer.echo(f"rprof: {e}", err=True)
        raise typer.Exit(2)


@app.command()
def selftest(quick: bool = typer.Option(False, "--quick", help="Shorter enforcement workloads; about 2 minutes in all."),
             out: Optional[Path] = typer.Option(None, "--out", help="capabilities.json path "
                                                "(default /var/lib/rprof/capabilities.json)."),
             image: str = typer.Option("rprof-testbox", "--image", help="Image the workloads run in."),
             only: Optional[str] = typer.Option(None, "--only", help="enforcement or fidelity"),
             verbose: bool = typer.Option(False, "--verbose", "-v", help="Show each check's workload and method."),
             quiet: bool = typer.Option(False, "--quiet", "-q", help="Print one summary line.")):
    """Check that limits hold (enforcement) and measurements are right (fidelity); writes capabilities.json."""
    from .selftest import PARTS
    from .selftest import selftest as do_selftest
    from .util import is_root
    if only is not None and only not in PARTS:
        typer.echo(f"rprof: --only must be one of {', '.join(PARTS)}", err=True)
        raise typer.Exit(2)
    if not is_root():
        _die(RprofError("selftest needs root", 72))
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    caps, ok = do_selftest(quick=quick, out=out, image=image, echo=typer.echo, only=only, verbose=verbose,
                           quiet=quiet, color=color)
    raise typer.Exit(0 if ok else 1)


# ---------------------------------------------------------------- gen

def _write_profile(d: dict, out: Optional[Path]) -> None:
    from .gen import dump
    from .profile import ProfileError, profile_from_dict
    try:
        profile_from_dict(d)
    except ProfileError as e:
        typer.echo("rprof: generated profile is invalid:\n" + str(e), err=True)
        raise typer.Exit(1)
    text = dump(d)
    if out:
        out.write_text(text)
        typer.echo(str(out), err=True)
    else:
        typer.echo(text, nl=False)


def _split(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


@gen_app.command("const")
def gen_const(knob: str = typer.Option(..., "--knob"), level: str = typer.Option(..., "--level"),
              duration: float = typer.Option(..., "--duration"), name: Optional[str] = typer.Option(None, "--name"),
              visibility: str = typer.Option("none", "--visibility"), o: Optional[Path] = typer.Option(None, "-o")):
    """One segment covering the whole run."""
    from .gen import const
    _write_profile(const(knob, level, duration, name=name, visibility=visibility), o)


@gen_app.command("step")
def gen_step(knob: str = typer.Option(..., "--knob"), level: str = typer.Option(..., "--level"),
             start: float = typer.Option(..., "--from"), end: float = typer.Option(..., "--to"),
             name: Optional[str] = typer.Option(None, "--name"), visibility: str = typer.Option("none", "--visibility"),
             o: Optional[Path] = typer.Option(None, "-o")):
    """One squeeze window."""
    from .gen import step
    _write_profile(step(knob, level, start, end, name=name, visibility=visibility), o)


@gen_app.command("square")
def gen_square(knob: str = typer.Option(..., "--knob"), low: str = typer.Option(..., "--low"),
               high: str = typer.Option("max", "--high"), period: float = typer.Option(..., "--period"),
               duty: float = typer.Option(0.5, "--duty", help="Fraction of each period at the low level."),
               duration: float = typer.Option(..., "--duration"), name: Optional[str] = typer.Option(None, "--name"),
               visibility: str = typer.Option("none", "--visibility"), o: Optional[Path] = typer.Option(None, "-o")):
    """Alternating windows."""
    from .gen import square
    _write_profile(square(knob, low, high, period, duty, duration, name=name, visibility=visibility), o)


@gen_app.command("random")
def gen_random(knobs: str = typer.Option(..., "--knobs", help="Comma-separated knobs."),
               levels: str = typer.Option(..., "--levels", help="Comma-separated levels (or knob:levels;...)."),
               duration: float = typer.Option(300, "--duration"),
               mean: float = typer.Option(30, "--mean", help="Mean segment length (s)."),
               min_len: float = typer.Option(5, "--min"), max_len: float = typer.Option(120, "--max"),
               seed: int = typer.Option(..., "--seed"), name: Optional[str] = typer.Option(None, "--name"),
               visibility: str = typer.Option("none", "--visibility"), o: Optional[Path] = typer.Option(None, "-o")):
    """An independent piecewise-constant schedule per knob."""
    from .gen import random_profile
    lv = _parse_levels(_split(knobs), levels)
    _write_profile(random_profile(_split(knobs), lv, duration, mean, min_len, max_len, seed,
                                  name=name, visibility=visibility), o)


def _parse_levels(knobs: list[str], s: str) -> dict[str, list[str]]:
    if ";" in s or any(f"{k}:" in s for k in knobs):
        out = {}
        for part in s.split(";"):
            k, _, v = part.partition(":")
            out[k.strip()] = _split(v)
        return out
    return {k: _split(s) for k in knobs}


@gen_app.command("trace")
def gen_trace(trace: Path = typer.Argument(..., exists=True), knob: str = typer.Option(..., "--knob"),
              capacity: str = typer.Option(..., "--capacity"),
              time_scale: float = typer.Option(1.0, "--time-scale", help="Profile seconds per trace second."),
              merge: float = typer.Option(0.05, "--merge", help="Merge steps whose relative change is below this."),
              min_len: float = typer.Option(1.0, "--min"), fmt: str = typer.Option("csv", "--format", help="csv or mahimahi"),
              name: Optional[str] = typer.Option(None, "--name"), visibility: str = typer.Option("none", "--visibility"),
              o: Optional[Path] = typer.Option(None, "-o")):
    """Available = capacity − a neighbour's usage, merged into segments (CSV time,usage; or a Mahimahi trace)."""
    from .gen import trace as gtrace
    _write_profile(gtrace(trace, knob, capacity, time_scale, merge, min_len, fmt=fmt, name=name,
                          visibility=visibility), o)


@gen_app.command("sweep")
def gen_sweep(base: Path = typer.Argument(..., exists=True), knob: str = typer.Option(..., "--knob"),
              levels: str = typer.Option(..., "--levels"), start: Optional[float] = typer.Option(None, "--from"),
              end: Optional[float] = typer.Option(None, "--to"),
              out_dir: Path = typer.Option(Path("."), "-o", "--out-dir")):
    """A family of profiles that differ in one knob."""
    from .gen import sweep
    paths = sweep(base, knob, _split(levels), out_dir, start, end)
    for p in paths:
        typer.echo(str(p))


def main() -> None:
    try:
        app()
    except RprofError as e:
        typer.echo(f"rprof: {e}", err=True)
        sys.exit(e.exit_code)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()

