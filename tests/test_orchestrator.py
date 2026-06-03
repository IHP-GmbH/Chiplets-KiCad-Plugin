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
    DEFAULT_INTERPOSER_ADAPTER,
    DEFAULT_INTERCONNECT_ADAPTER,
    ExportOptions, ExportResult,
    build_adk_drc_argv, build_cli_args,
    load_interposer_adapter, load_interconnect_adapter,
    available_connection_types,
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
    # Top cell is always passed (default INTERPOSER).
    assert args[args.index("-c") + 1] == "INTERPOSER"
    # Other optional flags omitted. io_pads / pad_locations are injected by
    # run_export (board auto-extraction), not by the pure build_cli_args.
    for flag in ("-l", "--connection-type", "--io-pads",
                 "--cupillar-gds", "--pad-locations", "--annotate-boundaries"):
        assert flag not in args


def test_complete_gds_toggle(tmp_path):
    opts = _opts(tmp_path)
    opts.emit_complete_gds = True
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert "--with-chiplets" in args
    complete = os.path.join(str(tmp_path), "demo_complete.gds")
    assert args[args.index("--complete-output") + 1] == complete


def test_annotate_boundaries_off_by_default(tmp_path):
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, _opts(tmp_path))
    assert "--annotate-boundaries" not in args


def test_annotate_boundaries_toggle(tmp_path):
    opts = _opts(tmp_path)
    opts.annotate_boundaries = True
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert "--annotate-boundaries" in args


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


def test_top_cell_always_passed(tmp_path):
    opts = _opts(tmp_path)
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert args[args.index("-c") + 1] == "INTERPOSER"  # default top cell

    opts.top_cell = "ASSEMBLY_TOP"
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert args[args.index("-c") + 1] == "ASSEMBLY_TOP"


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


def test_cupillar_gds_passthrough(tmp_path):
    opts = _opts(tmp_path)
    opts.cupillar_gds = "/etc/cupillars.gds"
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert args[args.index("--cupillar-gds") + 1] == "/etc/cupillars.gds"


def test_pad_locations_passthrough(tmp_path):
    opts = _opts(tmp_path)
    opts.pad_locations = {"U1": "/tmp/U1_pins.json", "U2": "/tmp/U2_pins.json"}
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    spec = args[args.index("--pad-locations") + 1]
    assert "U1=/tmp/U1_pins.json" in spec
    assert "U2=/tmp/U2_pins.json" in spec


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
        cupillar_gds="/etc/cup.gds",
        worker_python_override="",  # not part of CLI args
    )
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    for needle in (
        "-o", "--update-chiplet-file", "--with-chiplets", "--complete-output",
        "-c", "ASSEMBLY_TOP", "--connection-type", "sbump_sac305",
        "-l", "/etc/x.lyp", "--io-pads", "/etc/io.json",
        "--cupillar-gds", "/etc/cup.gds",
    ):
        assert needle in args, "Missing flag/value: %s" % needle


def test_worker_python_override_not_in_cli(tmp_path):
    opts = _opts(tmp_path)
    opts.worker_python_override = "/usr/bin/python3.12"
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    # The override governs which interpreter executes the script; it is
    # never injected into the script's own argv.
    assert "/usr/bin/python3.12" not in args


# ---------------------------------------------------------------------------
# ExportOptions / ExportResult defaults for ADK assembly DRC
# ---------------------------------------------------------------------------

def test_export_options_default_assembly_drc_enabled():
    opts = ExportOptions()
    assert opts.emit_assembly_drc is True
    assert opts.interposer_adapter == ""


def test_export_result_default_assembly_drc_fields():
    res = ExportResult()
    assert res.assembly_drc_exit_code == -1
    assert res.assembly_drc_report_path == ""


# ---------------------------------------------------------------------------
# load_interposer_adapter
# ---------------------------------------------------------------------------

def _write_chiplet(tmp_path, body):
    path = tmp_path / "demo.chiplet"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_load_interposer_adapter_field_present_double_quoted(tmp_path):
    p = _write_chiplet(tmp_path,
                       'interposer:\n  adapter: "custom_adapter"\n')
    assert load_interposer_adapter(p) == "custom_adapter"


def test_load_interposer_adapter_field_present_single_quoted(tmp_path):
    p = _write_chiplet(tmp_path,
                       "interposer:\n  adapter: 'another_one'\n")
    assert load_interposer_adapter(p) == "another_one"


def test_load_interposer_adapter_field_present_unquoted(tmp_path):
    p = _write_chiplet(tmp_path,
                       "interposer:\n  adapter: bare_value\n")
    assert load_interposer_adapter(p) == "bare_value"


def test_load_interposer_adapter_missing_file_returns_default(tmp_path):
    assert (load_interposer_adapter(str(tmp_path / "nope.chiplet"))
            == DEFAULT_INTERPOSER_ADAPTER)


def test_load_interposer_adapter_missing_block_returns_default(tmp_path):
    p = _write_chiplet(tmp_path, "assembly:\n  name: x\n")
    assert load_interposer_adapter(p) == DEFAULT_INTERPOSER_ADAPTER


def test_load_interposer_adapter_block_without_adapter_returns_default(
        tmp_path):
    p = _write_chiplet(tmp_path, "interposer:\n  other_key: foo\n")
    assert load_interposer_adapter(p) == DEFAULT_INTERPOSER_ADAPTER


def test_load_interposer_adapter_inline_comment_stripped(tmp_path):
    p = _write_chiplet(tmp_path,
                       'interposer:\n  adapter: "x"  # trailing\n')
    assert load_interposer_adapter(p) == "x"


def test_load_interposer_adapter_ignores_indented_lookalike(tmp_path):
    # `interposer:` only counts at column 0; a key with the same name
    # nested under another block must not enter the parser state.
    body = (
        "components:\n"
        "  - id: foo\n"
        "    interposer: not-a-block\n"
        "    adapter: trap_value\n"
    )
    p = _write_chiplet(tmp_path, body)
    assert load_interposer_adapter(p) == DEFAULT_INTERPOSER_ADAPTER


def test_load_interposer_adapter_block_followed_by_other_top_level(tmp_path):
    body = (
        'interposer:\n'
        '  adapter: "winner"\n'
        'components:\n'
        '  - id: a\n'
    )
    p = _write_chiplet(tmp_path, body)
    assert load_interposer_adapter(p) == "winner"


def test_load_interposer_adapter_empty_value_returns_default(tmp_path):
    p = _write_chiplet(tmp_path, 'interposer:\n  adapter: ""\n')
    assert load_interposer_adapter(p) == DEFAULT_INTERPOSER_ADAPTER


# ---------------------------------------------------------------------------
# build_adk_drc_argv
# ---------------------------------------------------------------------------

ADK_RUNNER = "/adk/klayout/drc/run_drc.py"
GDS = "/tmp/complete.gds"
ADAPTER = "intm4tm2"


def test_build_adk_drc_argv_required_only():
    args = build_adk_drc_argv(ADK_RUNNER, GDS, ADAPTER)
    assert args == [
        ADK_RUNNER,
        "--path", GDS,
        "--interposer-adapter", ADAPTER,
    ]


def test_build_adk_drc_argv_with_report_and_rundir(tmp_path):
    args = build_adk_drc_argv(
        ADK_RUNNER, GDS, ADAPTER,
        report_path=str(tmp_path / "out.lyrdb"),
        run_dir=str(tmp_path / "drc_dir"),
    )
    assert "--report" in args
    assert args[args.index("--report") + 1] == str(tmp_path / "out.lyrdb")
    assert "--run_dir" in args
    assert args[args.index("--run_dir") + 1] == str(tmp_path / "drc_dir")


def test_build_adk_drc_argv_with_all_optionals(tmp_path):
    args = build_adk_drc_argv(
        ADK_RUNNER, GDS, ADAPTER,
        report_path=str(tmp_path / "r.lyrdb"),
        run_dir=str(tmp_path / "d"),
        topcell="INTERPOSER",
        threads=8,
        run_mode="deep",
    )
    assert args[args.index("--topcell") + 1] == "INTERPOSER"
    assert args[args.index("--threads") + 1] == "8"
    assert args[args.index("--run_mode") + 1] == "deep"


def test_build_adk_drc_argv_threads_zero_passes_through():
    # 0 is a legitimate caller-specified value (forces single-thread);
    # the builder must not treat it as "unset".
    args = build_adk_drc_argv(ADK_RUNNER, GDS, ADAPTER, threads=0)
    assert args[args.index("--threads") + 1] == "0"


# ---------------------------------------------------------------------------
# Interconnect axis (orthogonal to the interposer adapter; opt-in)
# ---------------------------------------------------------------------------

def test_export_options_default_interconnect_adapter_empty():
    assert ExportOptions().interconnect_adapter == ""
    assert DEFAULT_INTERCONNECT_ADAPTER == ""


def test_load_interconnect_adapter_present(tmp_path):
    p = _write_chiplet(tmp_path, 'interconnect:\n  adapter: "ihp_cupillar"\n')
    assert load_interconnect_adapter(p) == "ihp_cupillar"


def test_load_interconnect_adapter_missing_block_returns_empty(tmp_path):
    p = _write_chiplet(tmp_path, "interposer:\n  adapter: x\n")
    assert load_interconnect_adapter(p) == ""


def test_load_interconnect_adapter_missing_file_returns_empty(tmp_path):
    assert load_interconnect_adapter(str(tmp_path / "nope.chiplet")) == ""


def test_build_adk_drc_argv_omits_interconnect_by_default():
    args = build_adk_drc_argv(ADK_RUNNER, GDS, ADAPTER)
    assert "--interconnect-adapter" not in args


def test_build_adk_drc_argv_adds_interconnect_when_set():
    args = build_adk_drc_argv(ADK_RUNNER, GDS, ADAPTER,
                              interconnect_adapter="ihp_cupillar")
    assert args[args.index("--interconnect-adapter") + 1] == "ihp_cupillar"


def test_available_connection_types_from_manifest():
    types = available_connection_types()
    assert types[0] == ""  # always first: empty = no --connection-type flag
    for method in ("cupillar_opt2", "sbump_sac305", "vendorx_microbump"):
        assert method in types
