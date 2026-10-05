from typer.testing import CliRunner

from rprof.cli import app

from unit_helpers import EXAMPLE

runner = CliRunner()


def test_validate_ok():
    r = runner.invoke(app, ["validate", str(EXAMPLE)])
    assert r.exit_code == 0, r.output
    lines = r.stdout.splitlines()
    assert lines[0].startswith("OK · mem-squeeze-mid · wall clock · 4 segments · 0–270 s")
    assert lines[1].split() == ["segment", "from", "to", "changes"]
    assert "mem.high 800Mi, mem.max 1Gi, pids.max 16" in lines[2]
    assert "net.partition reject" in lines[4]
    assert lines[-1].startswith("outside segments: defaults")


def test_validate_invalid(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text(EXAMPLE.read_text().replace("max: 1Gi", "max: 1Gx").replace("loss: 30%", "loss: 30"))
    r = runner.invoke(app, ["validate", str(f)])
    assert r.exit_code == 1
    assert r.stdout.splitlines() == ["segments[0].mem.max: 1Gx is not a byte size",
                                     "segments[1].net.loss: 30 is not a percentage (write e.g. 30%)"]


def test_validate_quiet_and_missing(tmp_path):
    assert runner.invoke(app, ["validate", "-q", str(EXAMPLE)]).stdout == ""
    r = runner.invoke(app, ["validate", str(tmp_path / "missing.yaml")])
    assert r.exit_code == 1 and "cannot read" in r.stdout


def test_gen_const(tmp_path):
    out = tmp_path / "c.yaml"
    r = runner.invoke(app, ["gen", "const", "--knob", "mem.max", "--level", "1Gi", "--duration", "60", "-o", str(out)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["validate", str(out)])
    assert r.exit_code == 0 and "mem.max 1Gi" in r.stdout


def test_gen_invalid_level():
    r = runner.invoke(app, ["gen", "const", "--knob", "pids.max", "--level", "1Gi", "--duration", "60"])
    assert r.exit_code == 1


def test_version():
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and r.stdout.startswith("rprof ")


def test_apply_requires_root_and_valid_knobs():
    import os
    if os.geteuid() == 0:
        return
    r = runner.invoke(app, ["apply", "--target", "docker:x", "mem.max=1Gi"])
    assert r.exit_code == 72
