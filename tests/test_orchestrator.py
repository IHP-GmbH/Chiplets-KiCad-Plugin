# SPDX-License-Identifier: GPL-2.0-or-later
"""
Unit tests for pipeline/orchestrator.py::build_cli_args.

Stdlib + pytest only. The orchestrator's ``run_export`` is exercised
end-to-end in Gate 47.7 (functional verification); here we only cover
the pure CLI-builder helper since it is the part the dialog cannot
test in isolation otherwise.
"""

import os
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_kicad_plugin.pipeline.orchestrator import (  # noqa: E402
    ExportOptions, build_cli_args,
)


HYP_SCRIPT = "/plugin/hyp_to_gds.py"
HYP_INPUT = "/tmp/board.hyp"
BOARD_NAME = "demo"


def _opts(out_dir):
    return ExportOptions(output_dir=str(out_dir))


def test_defaults_emit_canonical_chiplet_and_interposer(tmp_path):
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, _opts(tmp_path))
    assert args[0] == HYP_SCRIPT
    assert args[1] == HYP_INPUT

    interposer = os.path.join(str(tmp_path), "demo_interposer.gds")
    assert "-o" in args and args[args.index("-o") + 1] == interposer

    chiplet = os.path.join(str(tmp_path), "demo.chiplet")
    upd = args.index("--update-chiplet-file")
    assert args[upd + 1] == chiplet

    # Complete-assembly flags MUST NOT appear by default.
    assert "--with-chiplets" not in args
    assert "--complete-output" not in args
    # Other optional flags omitted.
    for flag in ("-c", "-l", "--connection-type", "--io-pads"):
        assert flag not in args


def test_complete_gds_toggle(tmp_path):
    opts = _opts(tmp_path)
    opts.emit_complete_gds = True
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert "--with-chiplets" in args
    complete = os.path.join(str(tmp_path), "demo_complete.gds")
    assert args[args.index("--complete-output") + 1] == complete


def test_disable_interposer_drops_output_flag(tmp_path):
    opts = _opts(tmp_path)
    opts.emit_interposer_gds = False
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert "-o" not in args


def test_disable_chiplet_drops_update_flag(tmp_path):
    opts = _opts(tmp_path)
    opts.emit_chiplet = False
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert "--update-chiplet-file" not in args


def test_top_cell_only_passed_when_overridden(tmp_path):
    opts = _opts(tmp_path)
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert "-c" not in args  # default "TOP" matches hyp_to_gds default

    opts.top_cell = "INTERPOSER_TOP"
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert args[args.index("-c") + 1] == "INTERPOSER_TOP"


def test_connection_type_passthrough(tmp_path):
    opts = _opts(tmp_path)
    opts.connection_type = "cupillar_opt2"
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert args[args.index("--connection-type") + 1] == "cupillar_opt2"


def test_lyp_and_io_pads_paths(tmp_path):
    opts = _opts(tmp_path)
    opts.lyp_override = "/etc/custom.lyp"
    opts.io_pads_json = "/etc/io_pads.json"
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert args[args.index("-l") + 1] == "/etc/custom.lyp"
    assert args[args.index("--io-pads") + 1] == "/etc/io_pads.json"


def test_argv_starts_with_script_and_input(tmp_path):
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, _opts(tmp_path))
    # hyp_to_gds.py expects ``hyp_file`` as positional first argument.
    assert args[:2] == [HYP_SCRIPT, HYP_INPUT]


def test_all_options_at_once(tmp_path):
    opts = ExportOptions(
        output_dir=str(tmp_path),
        emit_chiplet=True,
        emit_interposer_gds=True,
        emit_complete_gds=True,
        keep_intermediate_hyp=False,
        top_cell="ASSEMBLY_TOP",
        connection_type="sbump_sac305",
        lyp_override="/etc/x.lyp",
        io_pads_json="/etc/io.json",
        worker_python_override="",  # not part of CLI args
    )
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    for needle in (
        "-o", "--update-chiplet-file", "--with-chiplets", "--complete-output",
        "-c", "ASSEMBLY_TOP", "--connection-type", "sbump_sac305",
        "-l", "/etc/x.lyp", "--io-pads", "/etc/io.json",
    ):
        assert needle in args, "Missing flag/value: %s" % needle


def test_worker_python_override_not_in_cli(tmp_path):
    opts = _opts(tmp_path)
    opts.worker_python_override = "/usr/bin/python3.12"
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    # The override governs which interpreter executes the script; it is
    # never injected into the script's own argv.
    assert "/usr/bin/python3.12" not in args
