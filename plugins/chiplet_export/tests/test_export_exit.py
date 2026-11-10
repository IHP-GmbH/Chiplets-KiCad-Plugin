# SPDX-License-Identifier: GPL-3.0-or-later
"""chiplet-export's exit table, and the two headless drivers that use it.

export_exit is pure and is pinned first. The CLI and the wire-bond regen
driver are then run with pcbnew doubled and run_export returning a chosen
ExportResult, so what is checked is how each outcome leaves the process: the
code, the verdict line a caller greps, and the last line.
"""

import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from .test_die_outline_field import (  # noqa: E402
    CLI_SOURCE, PCBNEW, _Board, _die, _regen_argv, _regen_driver,
    _StrictModule,
)
from chiplet_export.pipeline.export_exit import (  # noqa: E402
    EXIT_BOARD, EXIT_DRC_NOT_PASSED, EXIT_NOT_WRITTEN, EXIT_OK,
    EXIT_TABLE_HELP, drc_outcome, export_exit,
)
from chiplet_export.pipeline.orchestrator import (  # noqa: E402
    ExportResult, describe_assembly_drc,
)

TRACEBACK = ("Traceback (most recent call last):\n"
             "  File \"orchestrator.py\", line 1, in run_export\n"
             "OSError: [Errno 2] No such file or directory: 'python'\n")
RAISED = "NOT RUN, the DRC stage raised (traceback on stderr)"
# The verdict a failed deck prints, as a packaging check matches it.
VERDICT_FAILED = re.compile(r"assembly DRC: FAILED \(exit 1\)$")


def _written(drc, **fields):
    return ExportResult(exit_code=0, assembly_drc_exit_code=drc, **fields)


PASSED = _written(0)
# Not passed, keyed by the outcome the last line names.
NOT_PASSED = {
    "failed or refused (run_drc exit 1)": _written(1),
    "failed or refused (run_drc exit 2)": _written(2),
    "REFUSED": _written(1, assembly_drc_refused=True),
    "NOT RUN": _written(-1),
    RAISED: _written(-1, assembly_drc_error=TRACEBACK),
}


def _fail_line(outcome):
    return ("FAIL: files written, assembly DRC not passed (%s); exit 3"
            % outcome)


def _allowed_line(outcome):
    return ("DRC not required (--allow-drc-fail): files written, assembly "
            "DRC not passed (%s); exit 0" % outcome)


# --- 1. export_exit ----------------------------------------------------------

def test_the_codes_are_the_table():
    assert (EXIT_OK, EXIT_NOT_WRITTEN, EXIT_BOARD, EXIT_DRC_NOT_PASSED) == (
        0, 1, 2, 3)


@pytest.mark.parametrize("allow", [False, True])
def test_a_passed_drc_exits_0_with_no_line(allow):
    assert export_exit(PASSED, allow) == (0, "")


@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize("drc", [0, 1, -1])
@pytest.mark.parametrize("fields", [
    {"exit_code": 1}, {"exit_code": -1},
    {"exit_code": 0, "error": "Cannot replace /out/demo.chiplet"},
], ids=["worker-failed", "worker-never-ran", "canonical-not-replaced"])
def test_nothing_written_exits_1_whatever_the_drc_says(fields, drc, allow):
    result = ExportResult(assembly_drc_exit_code=drc, **fields)
    assert export_exit(result, allow) == (1, "")


@pytest.mark.parametrize("outcome", sorted(NOT_PASSED))
def test_written_without_a_passed_drc_exits_3_naming_the_outcome(outcome):
    result = NOT_PASSED[outcome]
    assert drc_outcome(result) == outcome
    assert export_exit(result) == (3, _fail_line(outcome))


@pytest.mark.parametrize("outcome", sorted(NOT_PASSED))
def test_allow_drc_fail_exits_0_and_says_what_it_let_through(outcome):
    assert export_exit(NOT_PASSED[outcome], True) == (
        0, _allowed_line(outcome))


def test_a_stage_that_raised_is_never_passed_even_with_exit_0():
    """The field is set before the stage raises; the error decides."""
    result = _written(0, assembly_drc_error=TRACEBACK)
    assert drc_outcome(result) == RAISED
    assert export_exit(result)[0] == 3


@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize("outcome", sorted(NOT_PASSED))
def test_no_last_line_carries_the_verdict_token(outcome, allow):
    assert "assembly DRC:" not in export_exit(NOT_PASSED[outcome], allow)[1]


def test_the_verdict_line_is_unchanged_by_the_new_fields():
    assert describe_assembly_drc(_written(-1)) == "assembly DRC: NOT RUN"
    assert describe_assembly_drc(_written(0)) == "assembly DRC: PASSED"
    assert describe_assembly_drc(_written(1)) == "assembly DRC: FAILED (exit 1)"
    assert describe_assembly_drc(_written(2)) == "assembly DRC: FAILED (exit 2)"
    assert describe_assembly_drc(NOT_PASSED["REFUSED"]) == (
        "assembly DRC: FAILED (exit 1)")
    assert describe_assembly_drc(NOT_PASSED[RAISED]) == "assembly DRC: NOT RUN"


# --- 2. chiplet_export_cli ---------------------------------------------------

def _load_cli():
    spec = importlib.util.spec_from_file_location("_cli_exit_under_test",
                                                  CLI_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """The CLI with pcbnew doubled and run_export returning ``result``.

    Loaded under a private name, so the double pcbnew it imports never lands
    in sys.modules as the real CLI module."""
    board_file = tmp_path / "design.kicad_pcb"
    board_file.write_text("(kicad_pcb)\n", encoding="utf-8")
    board = _Board([])
    monkeypatch.setitem(sys.modules, "pcbnew", _StrictModule(
        **vars(PCBNEW), LoadBoard=lambda path: board))
    module = _load_cli()
    output = tmp_path / "out"
    state = SimpleNamespace(result=PASSED, exports=[], output=output)

    def run_export(_board, options, _plugin_dir, on_log=None):
        state.exports.append(options)
        return state.result

    monkeypatch.setattr(module, "run_export", run_export)

    def main(result, *flags):
        state.result = result
        return module.main(["--board", str(board_file),
                            "--output-dir", str(output), *flags])

    state.main = main
    return state


def _verdicts(out):
    return [line for line in out.splitlines() if "assembly DRC:" in line]


def test_a_failed_deck_exits_3_after_the_unchanged_verdict(cli, capsys):
    assert cli.main(NOT_PASSED["failed or refused (run_drc exit 1)"]) == 3
    out = capsys.readouterr().out
    assert _verdicts(out) == ["assembly DRC: FAILED (exit 1)"]
    assert VERDICT_FAILED.search(_verdicts(out)[0])
    assert out.splitlines()[-1] == _fail_line(
        "failed or refused (run_drc exit 1)")


@pytest.mark.parametrize("outcome", ["REFUSED", "NOT RUN"])
def test_a_refused_or_skipped_deck_exits_3(cli, capsys, outcome):
    assert cli.main(NOT_PASSED[outcome]) == 3
    out = capsys.readouterr().out
    assert len(_verdicts(out)) == 1
    assert out.splitlines()[-1] == _fail_line(outcome)


def test_require_drc_is_accepted_and_changes_nothing(cli, capsys):
    """Older callers pass it; it used to turn a failed deck into exit 1."""
    failed = NOT_PASSED["failed or refused (run_drc exit 1)"]
    assert cli.main(failed, "--require-drc") == 3
    with_flag = capsys.readouterr().out
    assert cli.main(failed) == 3
    assert capsys.readouterr().out == with_flag


@pytest.mark.parametrize("outcome", [
    "failed or refused (run_drc exit 1)", "REFUSED", "NOT RUN"])
def test_allow_drc_fail_exits_0_and_names_the_outcome(cli, capsys, outcome):
    assert cli.main(NOT_PASSED[outcome], "--allow-drc-fail") == 0
    out = capsys.readouterr().out
    assert len(_verdicts(out)) == 1
    assert out.splitlines()[-1] == _allowed_line(outcome)


def test_the_two_flags_together_are_a_usage_error(cli, capsys):
    with pytest.raises(SystemExit) as exited:
        cli.main(PASSED, "--require-drc", "--allow-drc-fail")
    assert exited.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
    assert cli.exports == []
    assert not cli.output.exists()


@pytest.mark.parametrize("flags", [(), ("--allow-drc-fail",),
                                   ("--require-drc",)])
def test_a_passed_deck_exits_0_and_adds_no_line(cli, capsys, flags):
    assert cli.main(PASSED, *flags) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "assembly DRC: PASSED"


def test_a_stage_that_raised_exits_3_with_the_traceback_on_stderr(cli,
                                                                  capsys):
    assert cli.main(NOT_PASSED[RAISED]) == 3
    captured = capsys.readouterr()
    assert captured.err == TRACEBACK
    assert "Traceback" not in captured.out
    assert _verdicts(captured.out) == ["assembly DRC: NOT RUN"]
    assert captured.out.splitlines()[-1] == _fail_line(RAISED)


@pytest.mark.parametrize("flags", [(), ("--allow-drc-fail",)])
def test_nothing_written_exits_1_with_no_line(cli, capsys, flags):
    failed = ExportResult(exit_code=1, assembly_drc_exit_code=-1)
    assert cli.main(failed, *flags) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "assembly DRC: NOT RUN"


def test_a_missing_board_exits_2(tmp_path, cli, capsys):
    missing = tmp_path / "absent.kicad_pcb"
    assert _load_cli().main(["--board", str(missing),
                             "--output-dir", str(cli.output)]) == 2
    assert "board not found" in capsys.readouterr().err
    assert not cli.output.exists()


def test_help_states_the_table_and_both_flags(cli, capsys):
    with pytest.raises(SystemExit) as exited:
        _load_cli().main(["--help"])
    assert exited.value.code == 0
    out = capsys.readouterr().out
    assert EXIT_TABLE_HELP in out
    for code in "0123":
        assert re.search(r"^  %s  " % code, out, re.M)
    assert "--require-drc" in out and "--allow-drc-fail" in out


# --- 3. tests/regenerate_wirebond_demo.py ------------------------------------

def _regen_exit(driver, monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exited:
        driver.main()
    return exited.value.code


NO_DRC_LINE = "No assembly DRC ran (--use-existing-hyp runs none); exit 0"


@pytest.mark.parametrize("flags", [[], ["--require-drc"],
                                   ["--allow-drc-fail"]])
def test_the_existing_hyp_path_runs_no_drc_and_says_so(tmp_path, monkeypatch,
                                                        capsys, flags):
    driver, runs, _written_paths = _regen_driver(
        monkeypatch, _Board([_die("U1")]))
    argv = _regen_argv(tmp_path, tmp_path / "out") + flags
    assert _regen_exit(driver, monkeypatch, argv) == 0
    assert len(runs) == 1
    out = capsys.readouterr().out
    assert out.splitlines()[-1] == NO_DRC_LINE
    assert "assembly DRC:" not in out


def test_the_existing_hyp_path_passes_a_worker_failure_through(
        tmp_path, monkeypatch, capsys):
    driver, _runs, _written_paths = _regen_driver(
        monkeypatch, _Board([_die("U1")]))
    monkeypatch.setattr(driver, "subprocess", SimpleNamespace(
        run=lambda cmd, **kw: SimpleNamespace(returncode=2, stdout="",
                                              stderr="boom")))
    argv = _regen_argv(tmp_path, tmp_path / "out")
    assert _regen_exit(driver, monkeypatch, argv) == 2
    assert NO_DRC_LINE not in capsys.readouterr().out


@pytest.fixture
def regen(tmp_path, monkeypatch):
    """The default (run_export) path, run_export returning ``result``."""
    driver, _runs, _written_paths = _regen_driver(monkeypatch, _Board([]))
    exports = []

    def main(result, *flags):
        def run_export(_board, options, _plugin_dir, on_log=None):
            exports.append(options)
            return result

        monkeypatch.setattr(driver, "run_export", run_export)
        argv = ["regenerate_wirebond_demo.py",
                "--board", str(tmp_path / "demo.kicad_pcb"),
                "--output-dir", str(tmp_path / "out"), *flags]
        return _regen_exit(driver, monkeypatch, argv)

    return SimpleNamespace(main=main, exports=exports)


FAILED = NOT_PASSED["failed or refused (run_drc exit 1)"]


@pytest.mark.parametrize("result, flags, code, last", [
    (PASSED, (), 0, "assembly DRC: PASSED"),
    (FAILED, (), 3, _fail_line("failed or refused (run_drc exit 1)")),
    (FAILED, ("--require-drc",), 3,
     _fail_line("failed or refused (run_drc exit 1)")),
    (FAILED, ("--allow-drc-fail",), 0,
     _allowed_line("failed or refused (run_drc exit 1)")),
    (NOT_PASSED["NOT RUN"], ("--allow-drc-fail",), 0,
     _allowed_line("NOT RUN")),
    (ExportResult(exit_code=1), (), 1, "assembly DRC: NOT RUN"),
], ids=["passed", "failed", "failed-require", "failed-allowed",
        "not-run-allowed", "not-written"])
def test_the_default_path_exits_with_the_same_table(regen, capsys, result,
                                                    flags, code, last):
    assert regen.main(result, *flags) == code
    out = capsys.readouterr().out
    assert len(_verdicts(out)) == 1
    assert out.splitlines()[-1] == last


def test_the_default_path_writes_a_raised_stage_to_stderr(regen, capsys):
    assert regen.main(NOT_PASSED[RAISED]) == 3
    captured = capsys.readouterr()
    assert captured.err == TRACEBACK
    assert captured.out.splitlines()[-1] == _fail_line(RAISED)


def test_the_regen_flags_together_are_a_usage_error(regen, capsys):
    assert regen.main(PASSED, "--require-drc", "--allow-drc-fail") == 2
    assert "not allowed with argument" in capsys.readouterr().err
    assert regen.exports == []
