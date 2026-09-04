# SPDX-License-Identifier: GPL-3.0-or-later
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

from chiplet_export.pipeline.orchestrator import (  # noqa: E402
    DEFAULT_INTERPOSER_ADAPTER,
    DEFAULT_INTERCONNECT_ADAPTER,
    DEPENDENCY_ROOT_MARKERS,
    ExportOptions, ExportResult, describe_assembly_drc,
    adapter_id_rejection,
    build_adk_drc_argv, build_cli_args, build_worker_env,
    load_interposer_adapter, load_interconnect_adapter,
    available_connection_types, discover_dependency_root,
    discover_interposer_lyp,
    connection_method_specs, format_connection_label,
    describe_connection_method, describe_die_thickness_gaps,
    describe_interposer_body_default,
    derive_interconnect_methods, write_ixn_methods_sidecar,
    _read_component_connections, _open_run_log,
)

# Sibling-dependent tests: the walk and the manifest listing assert against
# real ecosystem checkouts on disk. On a lone checkout (e.g. a bare CI
# runner) they skip; everything else here is tmp_path/monkeypatch based.
needs_interconnect = pytest.mark.skipif(
    not discover_dependency_root("INTERCONNECT_PDK_ROOT"),
    reason="interconnect PDK not discoverable (env var or sibling checkout)")
needs_all_siblings = pytest.mark.skipif(
    not all(discover_dependency_root(v) for v in DEPENDENCY_ROOT_MARKERS),
    reason="needs every ecosystem sibling checkout on disk")


HYP_SCRIPT = "/plugin/hyp_to_gds.py"
HYP_INPUT = "/tmp/board.hyp"
BOARD_NAME = "demo"


def _opts(out_dir):
    return ExportOptions(output_dir=str(out_dir))


def test_defaults_emit_canonical_chiplet_and_interposer(tmp_path):
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, _opts(tmp_path))
    assert args[0] == HYP_SCRIPT
    assert args[1] == HYP_INPUT

    interposer = os.path.join(str(tmp_path), "layout", "demo_interposer.gds")
    assert "-o" in args and args[args.index("-o") + 1] == interposer

    # The .chiplet stays at the output-dir root (not under layout/).
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
                 "--cupillar-gds", "--pad-locations", "--annotate-boundaries",
                 "--cmim-devices", "--insert-metal-fill", "--fill-mode",
                 "--nofill-regions"
                 ):
        assert flag not in args


def test_complete_gds_toggle(tmp_path):
    opts = _opts(tmp_path)
    opts.emit_complete_gds = True
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert "--with-chiplets" in args
    complete = os.path.join(str(tmp_path), "layout", "demo_complete.gds")
    assert args[args.index("--complete-output") + 1] == complete


def test_annotate_boundaries_off_by_default(tmp_path):
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, _opts(tmp_path))
    assert "--annotate-boundaries" not in args


def test_annotate_boundaries_toggle(tmp_path):
    opts = _opts(tmp_path)
    opts.annotate_boundaries = True
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert "--annotate-boundaries" in args


def test_metal_fill_defaults():
    opts = ExportOptions()
    assert opts.insert_metal_fill is False
    assert opts.fill_mode == "single-pass"
    assert opts.nofill_regions_json == ""


def test_metal_fill_off_by_default(tmp_path):
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, _opts(tmp_path))
    assert "--insert-metal-fill" not in args
    assert "--fill-mode" not in args


def test_metal_fill_single_pass_toggle(tmp_path):
    opts = _opts(tmp_path)
    opts.insert_metal_fill = True
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert "--insert-metal-fill" in args
    assert args[args.index("--fill-mode") + 1] == "single-pass"


def test_metal_fill_closure_mode(tmp_path):
    opts = _opts(tmp_path)
    opts.insert_metal_fill = True
    opts.fill_mode = "closure"
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert args[args.index("--fill-mode") + 1] == "closure"


def test_fill_mode_inert_without_insert(tmp_path):
    # A fill_mode set without insert_metal_fill must not leak the flag: the
    # worker would otherwise receive a mode with nothing to apply it to.
    opts = _opts(tmp_path)
    opts.fill_mode = "closure"
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert "--insert-metal-fill" not in args
    assert "--fill-mode" not in args


def test_nofill_regions_flag(tmp_path):
    opts = _opts(tmp_path)
    opts.nofill_regions_json = "/tmp/demo_nofill.json"
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert args[args.index("--nofill-regions") + 1] == "/tmp/demo_nofill.json"


def test_interposer_output_always_passed(tmp_path):
    """-o is not optional: hyp_to_gds writes the interposer GDS on every run,
    so omitting the flag only sent it (and its sidecars) into the workspace
    tmpdir the orchestrator deletes -- leaving the .chiplet's `layout:`
    pointing at a path that no longer exists."""
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, _opts(tmp_path))
    expected = os.path.join(str(tmp_path), "layout", "demo_interposer.gds")
    assert args[args.index("-o") + 1] == expected


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


def test_die_connections_passthrough_sorted(tmp_path):
    opts = _opts(tmp_path)
    opts.die_connections = {"U2": "vendorx_microbump", "U1": "cupillar_opt1"}
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    spec = args[args.index("--die-connections") + 1]
    assert spec == "U1=cupillar_opt1,U2=vendorx_microbump"


def test_die_connections_omitted_when_empty(tmp_path):
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, _opts(tmp_path))
    assert "--die-connections" not in args


def test_die_thicknesses_passthrough_sorted(tmp_path):
    opts = _opts(tmp_path)
    opts.die_thicknesses = {"U2": 750.0, "U1": 250}
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    spec = args[args.index("--die-thicknesses") + 1]
    assert spec == "U1=250.0,U2=750.0"


def test_die_thicknesses_omitted_when_empty(tmp_path):
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, _opts(tmp_path))
    assert "--die-thicknesses" not in args


def test_argv_starts_with_script_and_input(tmp_path):
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, _opts(tmp_path))
    # hyp_to_gds.py expects ``hyp_file`` as positional first argument.
    assert args[:2] == [HYP_SCRIPT, HYP_INPUT]


def test_all_options_at_once(tmp_path):
    opts = ExportOptions(
        output_dir=str(tmp_path),
        emit_chiplet=True,
        emit_complete_gds=True,
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


def test_cmim_devices_passthrough(tmp_path):
    opts = _opts(tmp_path)
    opts.cmim_devices_json = "/etc/cmim_devices.json"
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert args[args.index("--cmim-devices") + 1] == "/etc/cmim_devices.json"


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


@needs_interconnect
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
        # Required since the loader stopped reading this file with a raw
        # json.load and started going through the PDK reader, which applies
        # the shared version policy. A fixture with no schema_version is now
        # refused, and test_a_manifest_without_a_version_is_refused pins that.
        "schema_version": "1.0",
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

def _fake_pdk_tree(base, var_name, name_index=0):
    """Create <base>/<dirname>/<marker...> for a dependency root."""
    dirnames, marker = DEPENDENCY_ROOT_MARKERS[var_name]
    root = base / dirnames[name_index]
    root.joinpath(*marker).mkdir(parents=True)
    return root


@needs_all_siblings
def test_discover_dependency_root_walk_finds_real_siblings(monkeypatch):
    """With no env vars set, the sibling walk resolves every root of this
    workspace; each resolved path ends in one of the accepted dir names
    (canonical ecosystem name or upstream repository name)."""
    for var, (dirnames, marker) in DEPENDENCY_ROOT_MARKERS.items():
        monkeypatch.delenv(var, raising=False)
        root = discover_dependency_root(var)
        assert root, "walk did not resolve %s" % var
        assert Path(root).name in dirnames
        assert Path(root).joinpath(*marker).exists()


@needs_interconnect
def test_discover_dependency_root_env_wins_and_bogus_falls_through(
        tmp_path, monkeypatch):
    fake = _fake_pdk_tree(tmp_path, "INTERCONNECT_PDK_ROOT")
    monkeypatch.setenv("INTERCONNECT_PDK_ROOT", str(fake))
    assert discover_dependency_root("INTERCONNECT_PDK_ROOT") == str(fake)

    # Set-but-invalid (marker missing) falls through to the walk.
    monkeypatch.setenv("INTERCONNECT_PDK_ROOT", str(tmp_path / "nonexistent"))
    found = discover_dependency_root("INTERCONNECT_PDK_ROOT")
    assert found and found != str(fake)
    assert Path(found).name in DEPENDENCY_ROOT_MARKERS[
        "INTERCONNECT_PDK_ROOT"][0]


def test_discover_dependency_root_walk_accepts_repo_names(tmp_path,
                                                          monkeypatch):
    """A sibling named after the upstream repository (default GitHub clone
    dir, e.g. OpenIntM4TM2) resolves through the walk too."""
    for var in DEPENDENCY_ROOT_MARKERS:
        monkeypatch.delenv(var, raising=False)
    fake = _fake_pdk_tree(tmp_path / "ws", "INTERPOSER_PDK_ROOT",
                          name_index=1)
    start = tmp_path / "ws" / "plugin" / "pipeline" / "orchestrator.py"
    start.parent.mkdir(parents=True)
    found = discover_dependency_root("INTERPOSER_PDK_ROOT",
                                     start=str(start))
    assert found == str(fake)


# ---------------------------------------------------------------------------
# Interposer LYP default (pre-fills the dialog's LYP picker)
# ---------------------------------------------------------------------------

class _FakeProject:
    def __init__(self, text_vars):
        self._vars = text_vars

    def GetTextVars(self):
        return self._vars


class _FakeBoard:
    def __init__(self, text_vars):
        self._project = _FakeProject(text_vars)

    def GetProject(self):
        return self._project


def test_discover_interposer_lyp_env_wins(tmp_path, monkeypatch):
    lyp = tmp_path / "custom.lyp"
    lyp.write_text("<layer-properties/>")
    monkeypatch.setenv("INTERPOSER_LYP", str(lyp))
    assert discover_interposer_lyp() == str(lyp)


def test_discover_interposer_lyp_textvar_after_env(tmp_path, monkeypatch):
    monkeypatch.delenv("INTERPOSER_LYP", raising=False)
    lyp = tmp_path / "board.lyp"
    lyp.write_text("<layer-properties/>")
    board = _FakeBoard({"INTERPOSER_LYP": str(lyp)})
    assert discover_interposer_lyp(board=board) == str(lyp)


def test_discover_interposer_lyp_unresolved_textvar_falls_to_pdk(
        tmp_path, monkeypatch):
    """A ${VAR}-form text variable is not a file on disk; the canonical
    copy under the discovered PDK root wins instead."""
    monkeypatch.delenv("INTERPOSER_LYP", raising=False)
    monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
    pdk = _fake_pdk_tree(tmp_path / "ws", "INTERPOSER_PDK_ROOT")
    tech = pdk / "libs.tech" / "klayout" / "tech"
    tech.mkdir(parents=True, exist_ok=True)
    canonical = tech / "intm4tm2.lyp"
    canonical.write_text("<layer-properties/>")
    start = tmp_path / "ws" / "plugin" / "pipeline" / "orchestrator.py"
    start.parent.mkdir(parents=True)
    board = _FakeBoard({
        "INTERPOSER_LYP":
            "${INTERPOSER_PDK_ROOT}/libs.tech/klayout/tech/intm4tm2.lyp"})
    found = discover_interposer_lyp(board=board, start=str(start))
    assert found == str(canonical)


def test_discover_interposer_lyp_no_source_returns_empty(tmp_path, monkeypatch):
    """No env, no text var, no PDK checkout above the start: the .lyp belongs
    to the interposer PDK, so there is no bundled fallback; discovery returns
    "" and the dialog leaves the picker empty for the user to fill in."""
    monkeypatch.delenv("INTERPOSER_LYP", raising=False)
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(tmp_path / "nope"))
    start = tmp_path / "isolated" / "plugin" / "pipeline" / "orchestrator.py"
    start.parent.mkdir(parents=True)
    found = discover_interposer_lyp(start=str(start))
    assert found == ""


def test_available_connection_types_explicit_root(tmp_path):
    """An explicit root's manifest defines the dropdown, declaration order
    preserved -- pointing the dialog at a vendor checkout swaps the list."""
    root = tmp_path / "vendor_pdk"
    (root / "manifest").mkdir(parents=True)
    manifest = {"schema_version": "1.0",
                "methods": {"vendor_a": {}, "vendor_b": {}}}
    (root / "manifest" / "interconnect_methods.json").write_text(
        json.dumps(manifest))
    assert available_connection_types(str(root)) == ["", "vendor_a", "vendor_b"]


def test_available_connection_types_bad_root_falls_back(tmp_path):
    types = available_connection_types(str(tmp_path / "not_a_pdk"))
    assert types == ["", "cupillar_opt1", "cupillar_opt2",
                     "cupillar_opt3", "sbump_sac305"]


# ---------------------------------------------------------------------------
# Connection-method labels (what the dialog shows next to each method id)
# ---------------------------------------------------------------------------


@needs_interconnect
def test_connection_labels_from_real_manifest():
    """The label is built from the manifest, and the id stays its prefix so
    the dropdown's type-to-select still works on method ids."""
    specs = connection_method_specs()
    label = format_connection_label("cupillar_opt1", specs["cupillar_opt1"])
    assert label == "cupillar_opt1 - 75um pitch, 44um dia"
    for method, spec in specs.items():
        assert format_connection_label(method, spec).startswith(method)


@needs_interconnect
def test_connection_specs_stack_height_is_summed():
    """height is the sum of the stack's layer heights -- the number that
    lifts the die (position.z), not a single layer."""
    specs = connection_method_specs()
    assert specs["cupillar_opt1"]["height"] == 44.0   # Cu 28 + SnAg 16
    assert specs["sbump_sac305"]["height"] == 80.0    # single ball
    assert specs["cupillar_opt1"]["spacing"] == 40.0
    assert specs["cupillar_opt1"]["opening"] == 35.0


@needs_interconnect
def test_describe_connection_method_carries_the_drc_numbers():
    text = describe_connection_method("cupillar_opt1",
                                      connection_method_specs()["cupillar_opt1"])
    for fragment in ("pitch 75um", "spacing 40um", "35um opening",
                     "44um dia", "44um tall", "PacTech"):
        assert fragment in text


def test_connection_label_without_manifest_is_the_bare_id():
    """No manifest, no numbers: the fallback list and any board value this
    PDK does not declare must never be labelled with invented specs."""
    assert format_connection_label("cupillar_opt1", None) == "cupillar_opt1"
    assert format_connection_label("legacy_stack", {}) == "legacy_stack"
    assert describe_connection_method("legacy_stack", {}) == "legacy_stack"


def test_connection_label_survives_a_partial_manifest_entry(tmp_path):
    """A vendor entry missing pitch_rules degrades field by field instead of
    dropping the method from the dialog."""
    root = tmp_path / "vendor_pdk"
    (root / "manifest").mkdir(parents=True)
    (root / "manifest" / "interconnect_methods.json").write_text(json.dumps(
        {"schema_version": "1.0",
         "methods": {"vendor_a": {"body_diameter_um": 30}}}))
    specs = connection_method_specs(str(root))
    assert specs["vendor_a"] == {"diameter": 30.0}
    assert format_connection_label("vendor_a", specs["vendor_a"]) == \
        "vendor_a - 30um dia"


def test_connection_specs_bad_root_is_empty(tmp_path):
    assert connection_method_specs(str(tmp_path / "not_a_pdk")) == {}


# ---------------------------------------------------------------------------
# Die-thickness warnings
# ---------------------------------------------------------------------------


def test_die_thickness_gap_named_for_every_unset_die():
    lines = describe_die_thickness_gaps(["U1", "U2", "U3"], {"U2": 750.0})
    assert len(lines) == 1
    assert "U1, U3" in lines[0]
    assert "U2" not in lines[0].split("--")[0]


def test_die_thickness_no_warning_when_all_set():
    assert describe_die_thickness_gaps(["U1"], {"U1": 750.0}) == []


def test_die_thickness_implausible_magnitude_warns():
    """0.75 is a millimetre value and 750000 a nanometre one; the field is
    micrometers and parse_thickness_um accepts both without complaint."""
    lines = describe_die_thickness_gaps(["U1", "U2"],
                                        {"U1": 0.75, "U2": 750000.0})
    assert len(lines) == 1
    assert "U1=0.75" in lines[0] and "U2=750000" in lines[0]


def test_die_thickness_no_dies_no_warnings():
    assert describe_die_thickness_gaps([], {}) == []


def test_interposer_body_plausible_no_warning():
    # A thinned Si interposer (~300 um) or a full wafer (~750) is plausible.
    assert describe_interposer_body_default(300.0) == []
    assert describe_interposer_body_default(750.0) == []


def test_interposer_body_fr4_default_warns():
    # The KiCad default FR-4 board (~1.6 mm) means no real interposer stackup.
    lines = describe_interposer_body_default(1600.0)
    assert len(lines) == 1
    assert "1600" in lines[0] and "attachment_surface_z" in lines[0]


def test_interposer_body_none_no_warning():
    assert describe_interposer_body_default(None) == []


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


# ---------------------------------------------------------------------------
# Per-run log file (run_export tees every log line here; the end-to-end
# suites exercise the tee, these cover the pure file-opening helper)
# ---------------------------------------------------------------------------

def test_open_run_log_creates_timestamped_file(tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    fh, path = _open_run_log(str(out), "demo_board")
    try:
        assert fh is not None
        assert path
        p = Path(path)
        # Lands in <output_dir>/logs/ as <YYYYmmdd_HHMMSS>_<board>.log
        assert p.parent == out / "logs"
        assert p.name.endswith("_demo_board.log")
        stamp = p.name[: -len("_demo_board.log")]
        assert len(stamp) == len("YYYYmmdd_HHMMSS")
        assert stamp[8] == "_" and stamp.replace("_", "").isdigit()
        # The handle is writable and flushes to the file on disk.
        fh.write("hello\n")
        fh.flush()
        assert "hello" in p.read_text(encoding="utf-8")
    finally:
        if fh is not None:
            fh.close()


def test_open_run_log_best_effort_when_dir_unusable(tmp_path):
    # output_dir is a regular file, so logs/ cannot be created; the helper
    # degrades to (None, "") instead of raising (export must not abort).
    bad = tmp_path / "a_file"
    bad.write_text("x", encoding="utf-8")
    fh, path = _open_run_log(str(bad), "demo")
    assert fh is None
    assert path == ""


def test_open_run_log_no_clobber_same_second(tmp_path):
    # Two back-to-back runs of the same board (same wall-clock second) must
    # land in distinct files; neither truncates the other's record.
    out = tmp_path / "outputs"
    out.mkdir()
    fh1, p1 = _open_run_log(str(out), "demo")
    fh2, p2 = _open_run_log(str(out), "demo")
    try:
        assert fh1 is not None and fh2 is not None
        assert p1 != p2
        fh1.write("first\n")
        fh1.flush()
        fh2.write("second\n")
        fh2.flush()
        assert Path(p1).read_text(encoding="utf-8") == "first\n"
        assert Path(p2).read_text(encoding="utf-8") == "second\n"
    finally:
        for fh in (fh1, fh2):
            if fh is not None:
                fh.close()


# ---------------------------------------------------------------------------
# adapter_id_rejection: the DRC consumption gate.
#
# The adapter names a .drc that the assembly deck reads into the source it
# evaluates, so it is an id and never a path. The point of these tests is the
# CONVERGENCE: a CLI override wins over the document, so a gate on either leg
# alone is bypassable through the other.
# ---------------------------------------------------------------------------

# Shaped like the values that actually reach the DRC: a planted deck beside a
# downloaded project, an escape out of the vetted adapter directory, an
# absolute path, and an unexpanded substitution.
HOSTILE_ADAPTERS = [
    "rules/evil.drc",
    "../interconnect/ihp_cupillar",
    "/tmp/evil.drc",
    "${INTERCONNECT_PDK_ROOT}/x.drc",
]


def test_adapter_rejection_passes_clean_ids():
    opts = ExportOptions()
    assert adapter_id_rejection(
        "intm4tm2", "ihp_cupillar", opts, "/w/b.chiplet") == ""


def test_adapter_rejection_allows_empty_interconnect():
    """No interconnect axis is the common case and must not be a refusal."""
    opts = ExportOptions()
    assert adapter_id_rejection("intm4tm2", "", opts, "/w/b.chiplet") == ""


@pytest.mark.parametrize("value", HOSTILE_ADAPTERS)
def test_adapter_rejection_catches_the_document_interconnect_leg(value):
    """The leg that is live today: interconnect is not an exporter-owned
    top-level key, so a foreign block is carried into the freshly generated
    document and read back from it."""
    opts = ExportOptions()
    msg = adapter_id_rejection("intm4tm2", value, opts, "/w/b.chiplet")
    assert msg
    assert repr(value) in msg
    # Names the document, so the user knows to edit a file and not a flag.
    assert "interconnect.adapter in /w/b.chiplet" in msg


@pytest.mark.parametrize("value", HOSTILE_ADAPTERS)
def test_adapter_rejection_catches_the_document_interposer_leg(value):
    opts = ExportOptions()
    msg = adapter_id_rejection(value, "", opts, "/w/b.chiplet")
    assert msg
    assert "interposer.adapter in /w/b.chiplet" in msg


@pytest.mark.parametrize("value", HOSTILE_ADAPTERS)
def test_adapter_rejection_catches_the_cli_override_leg(value):
    """A gate on the document leg alone would sit behind this: the override
    wins through the `or`, so it never reads the document at all."""
    opts = ExportOptions(interposer_adapter=value)
    msg = adapter_id_rejection(value, "", opts, "/w/b.chiplet")
    assert msg
    # Blames the flag, not the file, because the file is not what is wrong.
    assert "--interposer-adapter override" in msg
    assert "/w/b.chiplet" not in msg


@pytest.mark.parametrize("value", HOSTILE_ADAPTERS)
def test_adapter_rejection_catches_the_interconnect_override_leg(value):
    opts = ExportOptions(interconnect_adapter=value)
    msg = adapter_id_rejection("intm4tm2", value, opts, "/w/b.chiplet")
    assert msg
    assert "--interconnect-adapter override" in msg


def test_a_clean_override_over_a_hostile_document_is_accepted():
    """The override wins, so the document value never reaches the DRC and is
    not what we are judging. Pins that we gate the EFFECTIVE value rather
    than every value we happened to read."""
    opts = ExportOptions(interconnect_adapter="ihp_cupillar")
    assert adapter_id_rejection(
        "intm4tm2", "ihp_cupillar", opts, "/w/b.chiplet") == ""


def test_interposer_is_reported_before_interconnect():
    """Deterministic message when both are bad, so the log is stable."""
    opts = ExportOptions()
    msg = adapter_id_rejection("/a.drc", "/b.drc", opts, "/w/b.chiplet")
    assert "'/a.drc'" in msg and "'/b.drc'" not in msg


def test_a_refused_adapter_is_not_reportable_as_a_skip():
    """A refusal must not share an exit path with the benign "klayout absent"
    skip. That skip leaves the DRC NOT RUN (-1); the refusal uses 1, so the
    verdict line says FAILED and cannot be read as an environment note."""
    refused = describe_assembly_drc(ExportResult(assembly_drc_exit_code=1))
    skipped = describe_assembly_drc(ExportResult(assembly_drc_exit_code=-1))
    passed = describe_assembly_drc(ExportResult(assembly_drc_exit_code=0))
    assert refused == "assembly DRC: FAILED (exit 1)"
    assert skipped == "assembly DRC: NOT RUN"
    assert refused != skipped and refused != passed


def test_a_manifest_without_a_version_is_refused(tmp_path):
    """PLUG-12's break-it-once: the gate is what the raw json.load skipped.

    The loader used to read this file directly under a bare except that
    returned {}, so a refused major, a corrupt file and an absent PDK were the
    same answer, and the export finished reporting success with no interconnect
    methods at all.
    """
    from chiplet_export.pipeline.orchestrator import (
        InterconnectSourceRefused, _load_interconnect_methods)

    root = tmp_path / "versionless"
    (root / "manifest").mkdir(parents=True)
    (root / "manifest" / "interconnect_methods.json").write_text(
        json.dumps({"methods": {"m": {}}}), encoding="utf-8")
    with pytest.raises(InterconnectSourceRefused):
        _load_interconnect_methods(str(root))


def test_an_absent_interconnect_root_still_degrades_quietly(tmp_path):
    """Absent is not broken. The plugin has to run without the PDK."""
    from chiplet_export.pipeline.orchestrator import _load_interconnect_methods
    assert _load_interconnect_methods(str(tmp_path / "nope")) == {}


def test_a_wrong_root_does_not_silently_serve_the_real_manifest(tmp_path):
    """The reader's load_manifest falls back to its OWN discovery when the path
    it is handed does not exist, so delegating the existence check would make a
    wrong root fail OPEN against a manifest nobody asked for."""
    from chiplet_export.pipeline.orchestrator import _load_interconnect_methods
    empty = tmp_path / "empty_root"
    (empty / "manifest").mkdir(parents=True)
    assert _load_interconnect_methods(str(empty)) == {}
