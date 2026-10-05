"""Paper figures (``rprof plot``): layouts, styles, metric choice and camera-ready output."""

from __future__ import annotations

import json

import pytest
import yaml

pytest.importorskip("matplotlib")

from typer.testing import CliRunner  # noqa: E402

from rprof.cli import app  # noqa: E402
from rprof.report import figures as F  # noqa: E402
from rprof.report.data import RunData  # noqa: E402

MiB = 1 << 20


def make_run(tmp_path, name: str, mode: str, limits: dict | None = None):
    """A 20 s run at 10 Hz: 1 core, then 2; 200 MiB of memory; disk and network traffic from 10 s."""
    d = tmp_path / f"2026-10-05T1200-{name}"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"run_id": d.name, "mode": mode, "target": {"io_device": "8:0"}}))
    prof = {"version": 1, "name": "p"}
    if limits:
        prof["segments"] = [{"from": 5, "to": 15, **limits}]
    (d / "profile.yaml").write_text(yaml.safe_dump(prof))
    rows, usage, wb, tx = [], 0.0, 0, 0
    for i in range(201):
        t = round(i * 0.1, 1)
        usage += (2e6 if t > 10 else 1e6) * 0.1
        if t > 10:
            wb, tx = wb + 50 * MiB // 10, tx + 100 * MiB // 10      # 50 MiB/s, 800 Mbit/s
        rows.append({"t": t, "cpu": {"usage_usec": int(usage), "throttled_usec": 0},
                     "mem": {"current": 200 * MiB, "file": 0, "shmem": 0, "swap_current": 0},
                     "io": {"8:0": {"rbytes": 0, "wbytes": wb, "rios": 0, "wios": wb // 4096}},
                     "net": {"eth0": {"rx_bytes": 0, "tx_bytes": tx}, "tcp_retrans_segs": 0},
                     "pids": {"current": 3}, "psi": {"cpu": {"some_us": 0}}})
    (d / "samples.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / "events.jsonl").write_text(json.dumps({"t": 20.0, "type": "run_end"}) + "\n")
    return d


@pytest.fixture
def two_runs(tmp_path):
    return [make_run(tmp_path, "baseline", "measure"),
            make_run(tmp_path, "squeezed", "enforce", {"cpu": {"cores": 0.5}, "net": {"rate": "10mbit"}})]


def test_row_is_one_figure_and_paper_is_one_per_metric(two_runs, tmp_path):
    row = F.plot_row(two_runs, tmp_path / "row.pdf")
    assert row.stat().st_size > 1000
    paths = F.plot_paper(two_runs, tmp_path / "figs", style="bold", fmt="png")
    assert [p.name for p in paths] == ["cpu.png", "memory.png", "disk-write.png", "net-send.png"]


def test_pdfs_embed_truetype_fonts_not_type3(two_runs, tmp_path):
    for style in F.STYLES:
        pdf = F.plot_row(two_runs, tmp_path / f"{style}.pdf", style=style).read_bytes()
        assert b"/Type3" not in pdf and b"/FontFile2" in pdf, style


def test_default_metrics_add_limits_the_core_four_do_not_show(tmp_path):
    runs = [RunData(make_run(tmp_path, "a", "enforce", {"pids": {"max": 20}, "net": {"rate": "10mbit"}}))]
    assert F.default_metrics(runs) == ["cpu", "memory", "disk-write", "net-send", "processes"]
    # A measure-mode run's profile wasn't applied, so its limits don't count.
    runs = [RunData(make_run(tmp_path, "b", "measure", {"pids": {"max": 20}}))]
    assert F.default_metrics(runs) == list(F.CORE)


def test_log_axis_only_when_values_span_decades():
    assert F._log_floor([[0.0, 1.0, 1000.0]], [10.0]) == 1.0
    assert F._log_floor([[1.0, 2.0, 3.0]], []) is None


def test_bins_average_rates_over_half_seconds(two_runs):
    rd = RunData(two_runs[0])
    xs, ys = F.binned(rd, F.METRICS["cpu"])
    assert xs[0] == 0.25 and xs[1] - xs[0] == F.BIN_S
    assert ys[5] == pytest.approx(1.0) and ys[-5] == pytest.approx(2.0)


def test_labels_default_to_the_run_names(two_runs):
    assert [F.run_label(RunData(d)) for d in two_runs] == ["baseline", "squeezed"]


def test_bad_input_is_reported(two_runs, tmp_path):
    with pytest.raises(ValueError, match="unknown metric"):
        F.plot_row(two_runs, tmp_path / "x.pdf", metrics=["cpu", "gpu"])
    with pytest.raises(ValueError, match="unknown style"):
        F.plot_row(two_runs, tmp_path / "x.pdf", style="fancy")
    with pytest.raises(ValueError, match="1 labels for 2 runs"):
        F.plot_row(two_runs, tmp_path / "x.pdf", labels=["one"])


def test_cli(two_runs, tmp_path):
    cli = CliRunner()
    r = cli.invoke(app, ["plot", *map(str, two_runs)])
    assert r.exit_code == 0, r.output
    assert (two_runs[0] / "plot.pdf").exists()                     # classic row by default
    r = cli.invoke(app, ["plot", *map(str, two_runs), "--paper", "--metrics", "cpu,processes", "-o", str(tmp_path / "f")])
    assert r.exit_code == 0, r.output
    assert sorted(p.name for p in (tmp_path / "f").iterdir()) == ["cpu.pdf", "processes.pdf"]
    assert cli.invoke(app, ["plot", str(two_runs[0]), "--row", "--paper"]).exit_code == 2
    assert cli.invoke(app, ["plot", *map(str, two_runs), "--dashboard"]).exit_code == 2
    r = cli.invoke(app, ["plot", "--list-metrics"])
    assert r.exit_code == 0 and r.output.splitlines()[0].split()[:2] == ["cpu", "CPU"]


def test_a_row_never_wraps(two_runs, tmp_path, monkeypatch):
    import matplotlib.pyplot as plt
    seen = {}
    real = plt.subplots

    def spy(*a, **kw):
        seen["shape"], seen["size"] = a[:2], kw["figsize"]
        return real(*a, **kw)

    monkeypatch.setattr(plt, "subplots", spy)
    F.plot_row(two_runs, tmp_path / "six.pdf", metrics=["cpu", "memory", "disk-write", "net-send", "processes",
                                                       "cpu-stall"])
    assert seen["shape"] == (1, 6) and seen["size"][0] == pytest.approx(6 * F.PANEL_IN)
