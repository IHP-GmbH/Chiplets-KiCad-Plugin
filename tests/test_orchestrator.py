# SPDX-License-Identifier: GPL-2.0-or-later
"""
Unit tests for pipeline/orchestrator.py's pure helpers.

Stdlib + pytest only. ``run_export`` is exercised by the end-to-end
suites; here we cover the pure helpers (CLI builder, dependency-root
discovery, connection-type listing, worker env) since they are the
parts the dialog cannot test in isolation otherwise.
"""

import json
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
    DEPENDENCY_ROOT_MARKERS,
    ExportOptions, ExportResult, describe_assembly_drc,
    build_adk_drc_argv, build_cli_args, build_worker_env,
    load_interposer_adapter, load_interconnect_adapter,
    available_connection_types, discover_dependency_root,
    derive_interconnect_methods, write_ixn_methods_sidecar,
    _read_component_connections,
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


def test_describe_assembly_drc_verdicts():
    res = ExportResult()
    assert describe_assembly_drc(res) == "assembly DRC: NOT RUN"
    res.assembly_drc_exit_code = 0
    assert describe_assembly_drc(res) == "assembly DRC: PASSED"
    res.assembly_drc_exit_code = 3
    assert describe_assembly_drc(res) == "assembly DRC: FAILED (exit 3)"


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


# ---------------------------------------------------------------------------
# Per-method interconnect (derive from per-die connections + manifest)
# ---------------------------------------------------------------------------

MIXED_CHIPLET = """\
format_version: "1.0"
interconnect:
  adapter: "ihp_cupillar"
components:
- id: interposer
  type: interposer
- id: U1
  type: die
  connection: method_x
  io_pads:
  - id: J1
    connection: nested_must_not_count
- id: U2
  type: die
  connection: method_y
- id: U3
  type: die
  connection: method_x
- id: U4
  type: die
  connection: custom_stack_not_in_manifest
- id: U5
  type: die
"""


def _write_fake_interconnect_root(tmp_path):
    root = tmp_path / "interconnect_pdk_root"
    manifest_dir = root / "manifest"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "interconnect_methods.json").write_text(json.dumps({
        "methods": {
            "method_x": {
                "pitch_rules": {"IXN_spacing": 40.0, "IXN_pitch": 75.0},
                "fab_params": {"passiv_opening_um": 35.0},
            },
            "method_y": {
                "pitch_rules": {"IXN_spacing": 15.0, "IXN_pitch": 50.0},
                "fab_params": {"passiv_opening_um": 35.0},
            },
        },
    }), encoding="utf-8")
    return str(root)


def test_read_component_connections_column_zero_items(tmp_path):
    p = _write_chiplet(tmp_path, MIXED_CHIPLET)
    conns = _read_component_connections(p)
    # interposer and U5 have no connection; the nested io_pads entry's
    # connection must not leak into U1's.
    assert conns == [("U1", "method_x"), ("U2", "method_y"),
                     ("U3", "method_x"), ("U4", "custom_stack_not_in_manifest")]


def test_read_component_connections_indented_items(tmp_path):
    p = _write_chiplet(tmp_path, (
        "components:\n"
        "  - id: die_a\n"
        "    connection: m_a\n"
        "  - id: die_b\n"
        "    type: die\n"
        "    connection: 'm_b'\n"
        "other_block:\n"
        "  - id: not_a_component\n"
        "    connection: nope\n"
    ))
    assert _read_component_connections(p) == [("die_a", "m_a"),
                                              ("die_b", "m_b")]


def test_derive_interconnect_methods_groups_dies_per_method(tmp_path):
    chiplet = _write_chiplet(tmp_path, MIXED_CHIPLET)
    root = _write_fake_interconnect_root(tmp_path)
    methods = derive_interconnect_methods(chiplet, interconnect_root=root)
    assert sorted(methods) == ["method_x", "method_y"]
    assert methods["method_x"]["dies"] == ["U1", "U3"]
    assert methods["method_y"]["dies"] == ["U2"]
    assert methods["method_x"]["IXN_spacing"] == 40.0
    assert methods["method_x"]["IXN_pitch"] == 75.0
    assert methods["method_x"]["IXN_pad_size"] == 35.0
    # The unknown stack id is left to the assembly-global adapter.
    assert "custom_stack_not_in_manifest" not in methods


def test_derive_interconnect_methods_empty_without_manifest(tmp_path):
    chiplet = _write_chiplet(tmp_path, MIXED_CHIPLET)
    assert derive_interconnect_methods(
        chiplet, interconnect_root=str(tmp_path / "nowhere")) == {}


def test_write_ixn_methods_sidecar_next_to_gds(tmp_path):
    methods = {
        "method_x": {"dies": ["U1"], "IXN_spacing": 40.0,
                     "IXN_pitch": 75.0, "IXN_pad_size": 35.0},
    }
    gds = tmp_path / "demo_complete.gds"
    sidecar = write_ixn_methods_sidecar(methods, str(gds), "demo.chiplet")
    assert sidecar == str(tmp_path / "demo_complete.ixn_methods.json")
    data = json.loads(Path(sidecar).read_text(encoding="utf-8"))
    assert data["schema"] == "adk-ixn-methods"
    assert data["assembly_gds"] == "demo_complete.gds"
    assert data["source_chiplet"] == "demo.chiplet"
    assert data["methods"]["method_x"]["dies"] == ["U1"]
    # Empty input writes nothing.
    assert write_ixn_methods_sidecar({}, str(gds)) == ""


def test_build_adk_drc_argv_adds_interconnect_methods_when_set():
    args = build_adk_drc_argv(ADK_RUNNER, GDS, ADAPTER,
                              interconnect_methods="/tmp/x.ixn_methods.json")
    assert args[args.index("--interconnect-methods") + 1] == "/tmp/x.ixn_methods.json"
    args = build_adk_drc_argv(ADK_RUNNER, GDS, ADAPTER)
    assert "--interconnect-methods" not in args


# ---------------------------------------------------------------------------
# Explicit PDK roots (dialog pickers; the GUI face of the env-var leg)
# ---------------------------------------------------------------------------

def _fake_pdk_tree(base, var_name):
    """Create <base>/<dirname>/<marker...> for a dependency root."""
    dirname, marker = DEPENDENCY_ROOT_MARKERS[var_name]
    root = base / dirname
    root.joinpath(*marker).mkdir(parents=True)
    return root


def test_discover_dependency_root_walk_finds_real_siblings(monkeypatch):
    """With no env vars set, the sibling walk resolves every root of this
    workspace, and each resolved path ends in the conventional dir name."""
    for var, (dirname, marker) in DEPENDENCY_ROOT_MARKERS.items():
        monkeypatch.delenv(var, raising=False)
        root = discover_dependency_root(var)
        assert root, "walk did not resolve %s" % var
        assert Path(root).name == dirname
        assert Path(root).joinpath(*marker).exists()


def test_discover_dependency_root_env_wins_and_bogus_falls_through(
        tmp_path, monkeypatch):
    fake = _fake_pdk_tree(tmp_path, "INTERCONNECT_PDK_ROOT")
    monkeypatch.setenv("INTERCONNECT_PDK_ROOT", str(fake))
    assert discover_dependency_root("INTERCONNECT_PDK_ROOT") == str(fake)

    # Set-but-invalid (marker missing) falls through to the walk.
    monkeypatch.setenv("INTERCONNECT_PDK_ROOT", str(tmp_path / "nonexistent"))
    found = discover_dependency_root("INTERCONNECT_PDK_ROOT")
    assert found and found != str(fake)
    assert Path(found).name == "interconnect_pdk"


def test_available_connection_types_explicit_root(tmp_path):
    """An explicit root's manifest defines the dropdown, declaration order
    preserved -- pointing the dialog at a vendor checkout swaps the list."""
    root = tmp_path / "vendor_pdk"
    (root / "manifest").mkdir(parents=True)
    manifest = {"methods": {"vendor_a": {}, "vendor_b": {}}}
    (root / "manifest" / "interconnect_methods.json").write_text(
        json.dumps(manifest))
    assert available_connection_types(str(root)) == ["", "vendor_a", "vendor_b"]


def test_available_connection_types_bad_root_falls_back(tmp_path):
    types = available_connection_types(str(tmp_path / "not_a_pdk"))
    assert types == ["", "cupillar_opt1", "cupillar_opt2",
                     "cupillar_opt3", "sbump_sac305"]


def test_build_worker_env_none_when_no_overrides():
    assert build_worker_env(ExportOptions()) is None


def test_build_worker_env_sets_only_given_roots():
    opts = ExportOptions(interconnect_pdk_root="/x/interconnect_pdk",
                         adk_root="/y/adk")
    env = build_worker_env(opts, base_env={"PATH": "/usr/bin"})
    assert env["INTERCONNECT_PDK_ROOT"] == "/x/interconnect_pdk"
    assert env["ADK_ROOT"] == "/y/adk"
    assert "INTERPOSER_PDK_ROOT" not in env
    assert env["PATH"] == "/usr/bin"  # base preserved


def test_build_worker_env_does_not_mutate_os_environ():
    opts = ExportOptions(interposer_pdk_root="/z/interposer")
    before = dict(os.environ)
    env = build_worker_env(opts)
    assert env["INTERPOSER_PDK_ROOT"] == "/z/interposer"
    assert dict(os.environ) == before
