# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise main() output with board loading and pipeline outcome isolated."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_export.pipeline.orchestrator import ExportResult  # noqa: E402

CLI_SOURCE = PLUGIN_ROOT / "chiplet_export_cli.py"


@pytest.mark.parametrize("error_input, expected", [
    ("export_report", "error:               refused; the export report is the file given with --export-report"),
    ("", "error:               refused"),
])
def test_cli_identifies_the_input_named_by_the_pipeline(
        tmp_path, monkeypatch, capsys, error_input, expected):
    spec = importlib.util.spec_from_file_location("_cli_main_under_test", CLI_SOURCE)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setitem(sys.modules, "pcbnew", types.SimpleNamespace(LoadBoard=lambda _: object()))
    result = ExportResult(error="refused", error_input=error_input)
    monkeypatch.setattr(cli, "run_export", lambda *_args, **_kwargs: result)
    board = tmp_path / "board.kicad_pcb"
    board.touch()
    assert cli.main(["--board", str(board), "--output-dir", str(tmp_path / "out")]) == 1
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("error:"))
    assert line == expected
