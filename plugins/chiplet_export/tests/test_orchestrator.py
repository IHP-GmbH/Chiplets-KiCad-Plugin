# SPDX-License-Identifier: GPL-3.0-or-later
"""
Unit tests for pipeline/orchestrator.py's pure helpers.

Stdlib + pytest only. ``run_export`` is exercised by the end-to-end
suites; here we cover the pure helpers (CLI builder, dependency-root
discovery, connection-type listing, worker env) since they are the
parts the dialog cannot test in isolation otherwise.
"""

import errno
import json
import os
import shutil
import stat
import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_export.pipeline import orchestrator  # noqa: E402
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
from chiplet_export.writers.connection_stacks import (  # noqa: E402
    InterconnectManifestSnapshot,
)
import chiplet_export.writers.connection_stacks as connection_stacks  # noqa: E402

from .conftest import sibling_workspace  # noqa: E402

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


def test_chiplet_update_path_is_the_file_the_worker_finalizes(tmp_path):
    """run_export hands the worker a staging sibling, not the canonical file."""
    staging = os.path.join(str(tmp_path), ".demo.chiplet.staging")
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, _opts(tmp_path),
                          chiplet_update_path=staging)
    assert args[args.index("--update-chiplet-file") + 1] == staging
    assert os.path.join(str(tmp_path), "demo.chiplet") not in args


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


def test_die_outline_flags_passthrough_sorted(tmp_path):
    opts = _opts(tmp_path)
    opts.die_outline_layers = {"U2": (206, 0), "U1": (39, 4)}
    opts.die_outline_exempt = ["U4", "U3", "U4"]
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert "--die-outline-layers=U1=39/4,U2=206/0" in args
    assert "--die-outline-exempt=U3,U4" in args


def test_die_outline_flags_carry_a_dash_leading_ref_in_one_token(tmp_path):
    """"--die-outline-exempt -U1" would make argparse take -U1 for an option
    and refuse the flag as missing its value."""
    opts = _opts(tmp_path)
    opts.die_outline_layers = {"-U2": (39, 4)}
    opts.die_outline_exempt = ["-U1"]
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)
    assert "--die-outline-layers=-U2=39/4" in args
    assert "--die-outline-exempt=-U1" in args
    assert "-U1" not in args and "-U2=39/4" not in args


def test_die_outline_flags_omitted_when_empty(tmp_path):
    args = build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, _opts(tmp_path))
    assert not [a for a in args if a.startswith("--die-outline-")]


@pytest.mark.parametrize("ref", ["", " U2", "U2 ", " "])
@pytest.mark.parametrize("field", ["die_outline_layers", "die_outline_exempt"])
def test_die_outline_flags_reject_empty_or_padded_refs(tmp_path, ref, field):
    """The worker strips each token, so a padded ref would name a different
    die than the caller meant, and an empty one fails only after staging."""
    opts = _opts(tmp_path)
    setattr(opts, field, {ref: (39, 4)} if field == "die_outline_layers"
            else [ref])
    with pytest.raises(ValueError, match="empty, padded with whitespace"):
        build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)


@pytest.mark.parametrize("ref", ["U,1", "U=1"])
@pytest.mark.parametrize("field", ["die_outline_layers", "die_outline_exempt"])
def test_die_outline_flags_reject_delimiters_in_refs(tmp_path, ref, field):
    opts = _opts(tmp_path)
    setattr(opts, field, {ref: (39, 4)} if field == "die_outline_layers"
            else [ref])
    with pytest.raises(ValueError, match="contains ',' or '='"):
        build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)


@pytest.mark.parametrize("layer", [(39,), (39, 4, 0), (-1, 0), (65536, 0),
                                   (39.0, 4), (True, 0), "39/4"])
def test_die_outline_layers_reject_non_layer_values(tmp_path, layer):
    opts = _opts(tmp_path)
    opts.die_outline_layers = {"U1": layer}
    with pytest.raises(ValueError, match="not a \\(layer, datatype\\) pair"):
        build_cli_args(HYP_SCRIPT, HYP_INPUT, BOARD_NAME, opts)


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
    # A packaging check greps these lines: the refusal and the error fields
    # the headless exit reads must never change them.
    assert describe_assembly_drc(ExportResult(
        assembly_drc_exit_code=1, assembly_drc_refused=True)) == (
        "assembly DRC: FAILED (exit 1)")
    assert describe_assembly_drc(ExportResult(
        assembly_drc_exit_code=2)) == "assembly DRC: FAILED (exit 2)"
    assert describe_assembly_drc(ExportResult(
        assembly_drc_error="Traceback")) == "assembly DRC: NOT RUN"


def test_export_result_default_assembly_drc_fields():
    res = ExportResult()
    assert res.assembly_drc_exit_code == -1
    assert res.assembly_drc_report_path == ""
    assert res.assembly_drc_refused is False
    assert res.assembly_drc_error == ""


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


@pytest.mark.needs_sibling_sources
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


class _StubManifestReader(object):
    """Stands in for the interconnect PDK's own manifest reader.

    Injected rather than relying on a sibling PDK checkout, because these tests
    are about what the ORCHESTRATOR does with what a reader returns; what the
    PDK's reader accepts is the PDK's test. Running them against whichever copy
    happened to be on the machine tested neither: on a bare runner there was no
    reader at all, the loader returned {} and these assertions were being made
    against the hardcoded fallback list. Local runs were green for months and
    the first CI run on a lone checkout failed all four.
    """

    def __init__(self, require_version=True):
        self.require_version = require_version

    def load_manifest(self, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if self.require_version and "schema_version" not in data:
            raise ValueError("manifest declares no schema_version")
        return data


@pytest.fixture
def stub_reader(monkeypatch):
    """A reader that is always there, so the test measures the orchestrator."""
    stub = _StubManifestReader()
    monkeypatch.setattr(orchestrator, "_interconnect_reader", lambda: stub)
    return stub


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


def test_derive_interconnect_methods_groups_dies_per_method(tmp_path, stub_reader):
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
    """Create <base>/<dirname>/<marker...> for a dependency root; a marker
    with a suffix is a file (the interposer's stack descriptor)."""
    dirnames, marker = DEPENDENCY_ROOT_MARKERS[var_name]
    root = base / dirnames[name_index]
    path = root.joinpath(*marker)
    if path.suffix:
        path.parent.mkdir(parents=True)
        path.write_text("{}\n")
    else:
        path.mkdir(parents=True)
    return root


@pytest.mark.needs_sibling_sources
@needs_all_siblings
def test_discover_dependency_root_walk_finds_real_siblings(tmp_path,
                                                           monkeypatch):
    """With no env vars set, the sibling walk resolves every root of this
    workspace; each resolved path ends in one of the accepted dir names
    (canonical ecosystem name or upstream repository name)."""
    start = sibling_workspace(tmp_path, DEPENDENCY_ROOT_MARKERS,
                              *DEPENDENCY_ROOT_MARKERS)
    for var, (dirnames, marker) in DEPENDENCY_ROOT_MARKERS.items():
        monkeypatch.delenv(var, raising=False)
        root = discover_dependency_root(var, start=str(start))
        assert root, "walk did not resolve %s" % var
        assert Path(root).name in dirnames
        assert Path(root).joinpath(*marker).exists()


@pytest.mark.needs_sibling_sources
@needs_interconnect
def test_discover_dependency_root_env_wins_and_bogus_falls_through(
        tmp_path, monkeypatch):
    start = str(sibling_workspace(tmp_path, DEPENDENCY_ROOT_MARKERS,
                                  "INTERCONNECT_PDK_ROOT"))
    fake = _fake_pdk_tree(tmp_path, "INTERCONNECT_PDK_ROOT")
    monkeypatch.setenv("INTERCONNECT_PDK_ROOT", str(fake))
    assert discover_dependency_root("INTERCONNECT_PDK_ROOT") == str(fake)

    # Set-but-invalid (marker missing) falls through to the walk.
    monkeypatch.setenv("INTERCONNECT_PDK_ROOT", str(tmp_path / "nonexistent"))
    found = discover_dependency_root("INTERCONNECT_PDK_ROOT", start=start)
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


@pytest.mark.parametrize("leg", ["env", "text variable"])
def test_a_selected_die_pdk_is_shown_not_replaced_by_the_sibling(
        tmp_path, monkeypatch, leg):
    """A KLayout PDK tree that is not IntM4TM2 (the SG13G2 die PDK has
    libs.tech/klayout/ too) is not walked past to a valid sibling: the
    dialog shows the selection as given, and the worker refuses it."""
    die_pdk = tmp_path / "ihp-sg13g2"
    (die_pdk / "libs.tech" / "klayout" / "tech").mkdir(parents=True)
    sibling = _fake_pdk_tree(tmp_path / "ws", "INTERPOSER_PDK_ROOT")
    start = tmp_path / "ws" / "plugin" / "pipeline" / "orchestrator.py"
    start.parent.mkdir(parents=True)
    board = None
    if leg == "env":
        monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(die_pdk))
    else:
        monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
        board = _FakeBoard({"INTERPOSER_PDK_ROOT": str(die_pdk)})
    found = discover_dependency_root("INTERPOSER_PDK_ROOT", board=board,
                                     start=str(start))
    assert found == str(die_pdk)
    monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
    assert discover_dependency_root("INTERPOSER_PDK_ROOT",
                                    start=str(start)) == str(sibling)


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


def test_available_connection_types_explicit_root(tmp_path, stub_reader):
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


@pytest.mark.needs_sibling_sources
@needs_interconnect
def test_connection_labels_from_real_manifest():
    """The label is built from the manifest, and the id stays its prefix so
    the dropdown's type-to-select still works on method ids."""
    specs = connection_method_specs()
    label = format_connection_label("cupillar_opt1", specs["cupillar_opt1"])
    assert label == "cupillar_opt1 - 75um pitch, 44um dia"
    for method, spec in specs.items():
        assert format_connection_label(method, spec).startswith(method)


@pytest.mark.needs_sibling_sources
@needs_interconnect
def test_connection_specs_stack_height_is_summed():
    """height is the sum of the stack's layer heights -- the number that
    lifts the die (position.z), not a single layer."""
    specs = connection_method_specs()
    assert specs["cupillar_opt1"]["height"] == 44.0   # Cu 28 + SnAg 16
    assert specs["sbump_sac305"]["height"] == 80.0    # single ball
    assert specs["cupillar_opt1"]["spacing"] == 40.0
    assert specs["cupillar_opt1"]["opening"] == 35.0


@pytest.mark.needs_sibling_sources
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


def test_connection_label_survives_a_partial_manifest_entry(tmp_path, stub_reader):
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


def test_a_present_manifest_with_no_reader_is_refused(tmp_path, monkeypatch):
    """No reader plus a manifest sitting right there is not absence.

    This is the case a bare runner is in, and the one the first version of the
    rewrite got wrong: it returned {} with the comment "the PDK is not
    installed", but the file had already been found on disk two lines above. The
    caller then reads "no methods" and available_connection_types serves its
    hardcoded list, so the export succeeds against invented data while a real
    manifest lies unread beside it. Absence is handled earlier, by the root and
    the file check; reaching the reader means present, and present-and-unusable
    refuses.
    """
    from chiplet_export.pipeline.orchestrator import (
        InterconnectSourceRefused, _load_interconnect_methods)

    root = _write_fake_interconnect_root(tmp_path)
    monkeypatch.setattr(orchestrator, "_interconnect_reader", lambda: None)
    with pytest.raises(InterconnectSourceRefused) as excinfo:
        _load_interconnect_methods(interconnect_root=root)
    # The message has to say what to do, since the user's PDK may be fine and
    # only the python path wrong.
    assert "INTERCONNECT_PDK_ROOT" in str(excinfo.value)


def test_a_manifest_without_a_version_is_refused(tmp_path, stub_reader):
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


@pytest.mark.parametrize("manifest_text", [
    "{",
    json.dumps({"methods": {"method_x": {}}}),
    json.dumps({"schema_version": "1.0"}),
    json.dumps({"schema_version": "1.0", "methods": []}),
    json.dumps({"schema_version": "1.0", "methods": {}}),
], ids=[
    "malformed-json",
    "missing-version",
    "missing-methods",
    "methods-not-an-object",
    "empty-methods",
])
def test_run_export_preflights_present_manifest_before_any_output(
        tmp_path, monkeypatch, stub_reader, manifest_text):
    """PLUG-16's orchestration-boundary control.

    No .chiplet exists yet, so there cannot be a component ``connection:`` to
    trigger the old late manifest read. A present unusable source must still
    stop the run before imports, directories, workspaces, writers or workers.
    """
    root = tmp_path / "refused_interconnect"
    (root / "manifest").mkdir(parents=True)
    (root / "manifest" / "interconnect_methods.json").write_text(
        manifest_text, encoding="utf-8")
    output_dir = tmp_path / "must_not_exist"

    def forbid_workspace(*_args, **_kwargs):
        raise AssertionError("preflight refusal reached workspace creation")

    monkeypatch.setattr(orchestrator.tempfile, "mkdtemp", forbid_workspace)
    result = orchestrator.run_export(
        object(),
        ExportOptions(output_dir=str(output_dir),
                      interconnect_pdk_root=str(root)),
        "/unused/plugin",
    )

    assert result.exit_code == -1
    assert "Interconnect PDK source refused" in result.error
    assert not output_dir.exists()


@pytest.mark.parametrize("selection", ["option", "environment", "project"])
@pytest.mark.parametrize("source_shape", ["missing-root", "missing-manifest-dir", "empty-manifest-dir"])
@pytest.mark.parametrize("existing_output", [False, True])
def test_run_export_refuses_incomplete_root_before_effects(
        tmp_path, monkeypatch, selection, source_shape, existing_output):
    """An incomplete A must not fall through to a valid alternative B."""
    root_a = tmp_path / "incomplete_a"
    if source_shape == "missing-manifest-dir":
        root_a.mkdir()
    elif source_shape == "empty-manifest-dir":
        (root_a / "manifest").mkdir(parents=True)
    root_b = _write_fake_interconnect_root(tmp_path)
    root_b = Path(root_b).rename(tmp_path / "interconnect_pdk")
    # B is both a valid automatic alternative and, when overridden by an
    # option, the environment selection. None of these may erase selected A.
    monkeypatch.setattr(orchestrator, "__file__", str(
        tmp_path / "plugin" / "pipeline" / "orchestrator.py"))
    if selection == "project":
        monkeypatch.delenv("INTERCONNECT_PDK_ROOT", raising=False)
    else:
        monkeypatch.setenv(
            "INTERCONNECT_PDK_ROOT",
            str(root_b if selection == "option" else root_a))
    calls, _seams = _install_run_export_fakes(monkeypatch)
    output_dir = tmp_path / "out"
    sentinel = output_dir / "existing.txt"
    if existing_output:
        output_dir.mkdir()
        sentinel.write_bytes(b"preserve existing output")

    forbidden_calls = []

    def forbid_effect(*_args, **_kwargs):
        forbidden_calls.append(True)
        raise AssertionError("incomplete-root refusal reached a later stage")

    from chiplet_export.pipeline import discovery

    monkeypatch.setattr(discovery, "_lookup_text_var", lambda *_a: str(root_a))
    monkeypatch.setattr(orchestrator, "_interconnect_reader", forbid_effect)
    monkeypatch.setattr(orchestrator.tempfile, "mkdtemp", forbid_effect)
    monkeypatch.setattr(discovery, "find_worker_python", forbid_effect)
    options = ExportOptions(
        output_dir=str(output_dir),
        interconnect_pdk_root=str(root_a) if selection == "option" else "")

    result = orchestrator.run_export(object(), options, "/unused/plugin")

    assert result.exit_code == -1
    assert "Interconnect PDK source refused" in result.error
    assert str(root_a) in result.error
    assert "manifest/interconnect_methods.json" in result.error
    assert forbidden_calls == []
    assert calls == []
    if existing_output:
        assert sentinel.read_bytes() == b"preserve existing output"
        assert list(output_dir.iterdir()) == [sentinel]
    else:
        assert not output_dir.exists()


def test_source_preflight_preserves_genuine_absence(monkeypatch):
    absent = InterconnectManifestSnapshot(None)
    monkeypatch.setattr(
        orchestrator, "_load_interconnect_manifest", lambda *_a, **_kw: absent)
    assert orchestrator._preflight_interconnect_manifest() is absent


def test_derive_preflights_source_before_looking_for_connections(
        tmp_path, monkeypatch):
    """A direct caller cannot recover the old no-connections bypass."""
    chiplet = tmp_path / "no_connections.chiplet"
    chiplet.write_text(
        'format_version: "1.0"\ncomponents: []\n', encoding="utf-8")

    def refuse(*_args, **_kwargs):
        raise orchestrator.InterconnectSourceRefused("refused without methods")

    monkeypatch.setattr(orchestrator, "_load_interconnect_methods", refuse)
    with pytest.raises(orchestrator.InterconnectSourceRefused,
                       match="refused without methods"):
        derive_interconnect_methods(str(chiplet))


def test_preflight_empty_snapshot_suppresses_a_second_manifest_read(
        tmp_path, monkeypatch):
    """An empty snapshot means genuine absence, not 'please discover again'."""
    chiplet = tmp_path / "no_connections.chiplet"
    chiplet.write_text(
        'format_version: "1.0"\ncomponents: []\n', encoding="utf-8")

    def forbid_reload(*_args, **_kwargs):
        raise AssertionError("the preflight snapshot was loaded twice")

    monkeypatch.setattr(
        orchestrator, "_load_interconnect_methods", forbid_reload)
    assert derive_interconnect_methods(
        str(chiplet), methods_db={}) == {}


def _install_run_export_fakes(monkeypatch, writer_probe=None,
                              validation_probe=None, chiplet_body=None,
                              die_connections=None, layout_worker=None):
    """Install the smallest GUI/worker seam needed to reach assembly DRC.

    ``layout_worker``, when given, stands in for the hyp_to_gds subprocess: it
    receives the command and returns the runner's result. The default exits 0
    without touching the .chiplet it was handed.
    """
    seams = {
        "writer_manifests": [],
        "validation_manifests": [],
        "runner_envs": [],
    }
    writer = types.ModuleType("chiplet_export.writers.chiplet_writer")

    def write_chiplet(_board, path, interconnect_manifest=None, export_report=None):
        seams["writer_manifests"].append(interconnect_manifest)
        if writer_probe is not None:
            writer_probe(interconnect_manifest)
        Path(path).write_text(
            chiplet_body or 'format_version: "1.0"\ncomponents: []\n',
            encoding="utf-8")
        return True

    writer.write_chiplet = write_chiplet
    writer.preflight_carriers = lambda *_a, **_kw: None
    writer.write_io_pads_json = lambda *_a, **_kw: 0
    writer.write_cmim_devices_json = lambda *_a, **_kw: 0
    writer.write_nofill_regions_json = lambda *_a, **_kw: 0
    writer.write_die_pin_lists = lambda *_a, **_kw: {}
    writer.read_die_connections = lambda *_a, **_kw: dict(
        die_connections or {})
    writer.read_die_thicknesses = lambda *_a, **_kw: {}
    writer.list_die_refs = lambda *_a, **_kw: []
    writer._iu_to_um = float

    stacks = types.ModuleType("chiplet_export.writers.connection_stacks")

    def validate_interconnect_ids(*_args, **kwargs):
        seams["validation_manifests"].append(kwargs.get("manifest"))
        if validation_probe is not None:
            validation_probe(*_args, **kwargs)

    stacks.validate_interconnect_ids = validate_interconnect_ids
    stacks.validate_adapter_id = lambda *_a, **_kw: None
    # _load_interconnect_manifest imports the snapshot type lazily. Keep that
    # real pure-Python type available even while this harness replaces the
    # rest of the writer module surface.
    stacks.InterconnectManifestSnapshot = InterconnectManifestSnapshot

    hyperlynx = types.ModuleType("chiplet_export.writers.hyperlynx_writer")

    def write_hyperlynx(_board, path):
        Path(path).write_text("{VERSION=2.14}\n", encoding="utf-8")
        return True

    hyperlynx.write_hyperlynx = write_hyperlynx

    monkeypatch.setitem(
        sys.modules, "chiplet_export.writers.chiplet_writer", writer)
    monkeypatch.setitem(
        sys.modules, "chiplet_export.writers.connection_stacks", stacks)
    monkeypatch.setitem(
        sys.modules, "chiplet_export.writers.hyperlynx_writer", hyperlynx)

    from chiplet_export.pipeline import discovery, runner

    monkeypatch.setattr(
        discovery, "find_worker_python", lambda *_a, **_kw: "/worker/python")
    monkeypatch.setattr(
        discovery, "find_hyp_to_gds", lambda *_a, **_kw: "/worker/hyp_to_gds.py")
    monkeypatch.setattr(
        discovery, "find_adk_drc_runner", lambda *_a, **_kw: "/adk/run_drc.py")

    calls = []

    def run_async(command, **_kwargs):
        calls.append(command)
        seams["runner_envs"].append(_kwargs.get("env"))
        if "--complete-output" in command:
            output = command[command.index("--complete-output") + 1]
            Path(output).write_bytes(b"synthetic-gds")
        if layout_worker is not None and command[1] == "/worker/hyp_to_gds.py":
            return layout_worker(command)
        return types.SimpleNamespace(exit_code=0, cancelled=False)

    monkeypatch.setattr(runner, "run_async", run_async)
    monkeypatch.setattr(orchestrator, "_open_run_log", lambda *_a: (None, ""))
    monkeypatch.setattr(
        orchestrator, "_write_outputs_manifest", lambda *_a, **_kw: None)
    return calls, seams


class _RunExportBoard:
    def GetFileName(self):
        return "/project/demo.kicad_pcb"

    def Footprints(self):
        return []


def _drc_options(tmp_path):
    return ExportOptions(
        output_dir=str(tmp_path / "out"),
        emit_chiplet=False,
        emit_complete_gds=True,
        emit_assembly_drc=True,
    )


def test_run_export_reuses_one_manifest_snapshot_in_process(
        tmp_path, monkeypatch):
    calls, seams = _install_run_export_fakes(monkeypatch)
    manifest_a = {
        "schema_version": "1.0",
        "methods": {"method_a": {}},
    }
    snapshot = InterconnectManifestSnapshot(manifest_a)
    loads = []
    derived_methods = []

    def load_once(*_args, **_kwargs):
        loads.append(True)
        return snapshot

    def derive(*_args, **kwargs):
        derived_methods.append(kwargs["methods_db"])
        return {}

    monkeypatch.setattr(orchestrator, "_load_interconnect_manifest", load_once)
    monkeypatch.setattr(orchestrator, "derive_interconnect_methods", derive)

    result = orchestrator.run_export(
        _RunExportBoard(), _drc_options(tmp_path), "/unused/plugin")

    assert result.error == ""
    assert len(loads) == 1
    assert seams["writer_manifests"] == [snapshot]
    assert seams["writer_manifests"][0] is snapshot
    assert seams["validation_manifests"] == [snapshot]
    assert seams["validation_manifests"][0] is snapshot
    assert derived_methods == [manifest_a["methods"]]
    assert derived_methods[0] is snapshot.methods
    assert len(calls) == 2  # layout worker, then ADK DRC


def test_explicit_catalogue_a_cannot_mix_with_default_catalogue_b(
        tmp_path, monkeypatch):
    """Adversarial PLUG-12 seam: A owns every value in one export run."""
    method_a = {
        "description": "stack from A",
        "adapter": "adapter_a",
        "connection_stack": {"layers": [{
            "name": "A_layer", "material": "A_material",
            "height": 11.0, "diameter": 22.0,
        }]},
        "pitch_rules": {"IXN_spacing": 31.0, "IXN_pitch": 71.0},
        "fab_params": {"passiv_opening_um": 41.0},
    }
    method_b = {
        "description": "stack from B",
        "adapter": "adapter_b",
        "connection_stack": {"layers": [{
            "name": "B_layer", "material": "B_material",
            "height": 99.0, "diameter": 99.0,
        }]},
        "pitch_rules": {"IXN_spacing": 91.0, "IXN_pitch": 191.0},
        "fab_params": {"passiv_opening_um": 81.0},
    }
    manifest_a = {
        "schema_version": "1.0",
        "default_connection_library": ["shared_method"],
        "methods": {"shared_method": method_a},
    }
    manifest_b = {
        "schema_version": "1.0",
        "default_connection_library": ["shared_method"],
        "methods": {"shared_method": method_b},
    }
    root_a = tmp_path / "catalogue_a"
    (root_a / "manifest").mkdir(parents=True)
    manifest_path = root_a / "manifest" / "interconnect_methods.json"
    manifest_path.write_text(json.dumps(manifest_a), encoding="utf-8")

    class DualCatalogueReader:
        def __init__(self):
            self.load_paths = []
            self.default_reads = 0

        def load_manifest(self, path=None):
            if path is None:
                self.default_reads += 1
                return manifest_b
            self.load_paths.append(Path(path))
            assert Path(path) == manifest_path
            return manifest_a

        def _selected(self, manifest=None):
            if manifest is None:
                self.default_reads += 1
                return manifest_b
            assert manifest is manifest_a
            return manifest

        def list_methods(self, manifest=None):
            return list(self._selected(manifest)["methods"])

        def adapter_for(self, method_id, manifest=None):
            return self._selected(manifest)["methods"][method_id]["adapter"]

        def get_connection_library(self, manifest=None):
            selected = self._selected(manifest)
            return {
                method_id: {
                    "description": selected["methods"][method_id][
                        "description"],
                    "layers": selected["methods"][method_id][
                        "connection_stack"]["layers"],
                }
                for method_id in selected["default_connection_library"]
            }

    reader = DualCatalogueReader()
    monkeypatch.setattr(orchestrator, "_interconnect_reader", lambda: reader)
    monkeypatch.setattr(
        connection_stacks, "_manifest_reader", lambda: reader)

    emitted_blocks = []

    def writer_probe(snapshot):
        connection_stacks.validate_interconnect_ids(
            adapter="adapter_a", manifest=snapshot)
        emitted_blocks.append(
            connection_stacks.emit_connection_stacks_block(snapshot))

    def validation_probe(*_args, **kwargs):
        connection_stacks.validate_interconnect_ids(
            die_methods=kwargs["die_methods"], manifest=kwargs["manifest"])

    chiplet_body = (
        'format_version: "1.0"\n'
        "components:\n"
        "  - id: die_a\n"
        "    type: die\n"
        "    connection: shared_method\n"
    )
    calls, seams = _install_run_export_fakes(
        monkeypatch,
        writer_probe=writer_probe,
        validation_probe=validation_probe,
        chiplet_body=chiplet_body,
        die_connections={"die_a": "shared_method"},
    )
    options = _drc_options(tmp_path)
    options.interconnect_pdk_root = str(root_a)

    result = orchestrator.run_export(
        _RunExportBoard(), options, "/unused/plugin")

    assert result.error == ""
    assert reader.load_paths == [manifest_path]
    assert reader.default_reads == 0
    assert len(calls) == 2
    snapshot = seams["writer_manifests"][0]
    assert snapshot.manifest is manifest_a
    assert seams["validation_manifests"] == [snapshot]
    assert "stack from A" in emitted_blocks[0]
    assert "stack from B" not in emitted_blocks[0]
    assert seams["runner_envs"][0]["INTERCONNECT_PDK_ROOT"] == str(root_a)

    sidecar = (tmp_path / "out" / "layout"
               / "demo_complete.ixn_methods.json")
    methods = json.loads(sidecar.read_text(encoding="utf-8"))["methods"]
    assert methods["shared_method"]["IXN_spacing"] == 31.0
    assert methods["shared_method"]["IXN_pitch"] == 71.0
    assert methods["shared_method"]["IXN_pad_size"] == 41.0


@pytest.mark.parametrize("emit_chiplet", [False, True],
                         ids=["no-chiplet", "both-manifest-notes"])
def test_automatic_empty_checkout_exports_with_visible_absence_warning(
        tmp_path, monkeypatch, emit_chiplet):
    """An unselected empty checkout is absence, not an incomplete selection.

    Workers/writers are seam doubles here; this proves orchestration reporting,
    not that independently discovering external processes see no other PDK.
    """
    monkeypatch.delenv("INTERCONNECT_PDK_ROOT", raising=False)
    monkeypatch.setattr(orchestrator, "__file__", str(
        tmp_path / "plugin" / "pipeline" / "orchestrator.py"))
    (tmp_path / "interconnect_pdk" / "manifest").mkdir(parents=True)
    # Restore real reporting after installing the worker/writer seam.
    outputs_manifest = orchestrator._write_outputs_manifest
    open_log = orchestrator._open_run_log
    calls, seams = _install_run_export_fakes(monkeypatch)
    monkeypatch.setattr(orchestrator, "_write_outputs_manifest", outputs_manifest)
    monkeypatch.setattr(orchestrator, "_open_run_log", open_log)
    logs = []
    options = _drc_options(tmp_path)
    options.emit_chiplet = emit_chiplet
    result = orchestrator.run_export(
        _RunExportBoard(), options, "/unused/plugin",
        on_log=logs.append)

    assert result.error == ""
    assert result.exit_code == 0
    assert len(calls) == 2
    assert seams["writer_manifests"][0].manifest is None
    assert seams["writer_manifests"][0].root == ""
    assert seams["validation_manifests"][0] is seams["writer_manifests"][0]
    warning = orchestrator.INTERCONNECT_ABSENCE_WARNING
    assert "Warning: " + warning in logs
    manifest = (tmp_path / "out" / "MANIFEST.md").read_text(encoding="utf-8")
    assert warning in manifest
    guard_lines = [line for line in logs
                   if "previous-export content guard" in line]
    if emit_chiplet:
        # The real orchestration must preserve BOTH independent notices when
        # composing one manifest; merging either signature alone loses one.
        assert len(guard_lines) == 1
        assert "first export" in guard_lines[0]
        assert "WITHOUT content-guard protection" in guard_lines[0]
        assert guard_lines[0] in manifest
        assert "## Previous-export content guard" in manifest
        assert "## Interconnect verification limits" in manifest
    else:
        assert guard_lines == []
        assert "## Previous-export content guard" not in manifest
    log_paths = list((tmp_path / "out" / "logs").glob("*.log"))
    assert len(log_paths) == 1
    assert warning in log_paths[0].read_text(encoding="utf-8")
    assert not (tmp_path / "out" / "layout" / "demo_complete.ixn_methods.json").exists()


def test_non_source_sidecar_error_remains_best_effort(tmp_path, monkeypatch):
    calls, _seams = _install_run_export_fakes(monkeypatch)
    logs = []
    monkeypatch.setattr(
        orchestrator, "_load_interconnect_manifest",
        lambda *_a, **_kw: InterconnectManifestSnapshot(None))

    def fail_enrichment(*_args, **_kwargs):
        raise OSError("sidecar scratch is unavailable")

    monkeypatch.setattr(
        orchestrator, "derive_interconnect_methods", fail_enrichment)
    result = orchestrator.run_export(
        _RunExportBoard(), _drc_options(tmp_path), "/unused/plugin",
        on_log=logs.append)

    assert result.error == ""
    assert result.assembly_drc_exit_code == 0
    assert len(calls) == 2
    assert any("per-method interconnect derivation failed" in line
               and "sidecar scratch is unavailable" in line for line in logs)


def test_a_refused_adapter_is_marked_refused_and_the_deck_never_runs(
        tmp_path, monkeypatch):
    """The export's own refusal sets 1, as run_drc's violations do; the flag
    is what lets the headless exit tell the two apart."""
    calls, _seams = _install_staged_finalize_fakes(monkeypatch)
    monkeypatch.setattr(orchestrator, "adapter_id_rejection",
                        lambda *_a, **_kw: "'/tmp/x.drc' is not an adapter id")
    logs = []
    result = orchestrator.run_export(
        _RunExportBoard(), _drc_options(tmp_path), "/unused/plugin",
        on_log=logs.append)

    assert result.error == ""
    assert result.assembly_drc_exit_code == 1
    assert result.assembly_drc_refused is True
    assert result.assembly_drc_error == ""
    assert len(calls) == 1  # the layout worker only
    assert ("Assembly DRC REFUSED: '/tmp/x.drc' is not an adapter id"
            in logs)


def test_a_missing_adk_runner_is_not_run_and_not_refused(tmp_path,
                                                         monkeypatch):
    calls, _seams = _install_staged_finalize_fakes(monkeypatch)
    from chiplet_export.pipeline import discovery

    def not_found(*_args, **_kwargs):
        raise discovery.AdkRunnerNotFoundError("no ADK checkout")

    monkeypatch.setattr(discovery, "find_adk_drc_runner", not_found)
    logs = []
    result = orchestrator.run_export(
        _RunExportBoard(), _drc_options(tmp_path), "/unused/plugin",
        on_log=logs.append)

    assert result.error == ""
    assert result.assembly_drc_exit_code == -1
    assert result.assembly_drc_refused is False
    assert result.assembly_drc_error == ""
    assert len(calls) == 1
    assert "Assembly DRC skipped: no ADK checkout" in logs


# --- staged finalize: the canonical .chiplet is replaced only on success ----
#
# The worker finalizes a staging sibling of the canonical .chiplet, and
# run_export moves it over the canonical file only when the worker exited 0 and
# was not cancelled. Every other outcome must leave an earlier export's file as
# the user had it. These drive the real run_export through the seam above; the
# layout worker is a double that does to its --update-chiplet-file argument
# what the finalizer does (drop the intermediate marker), unless a test hands
# it the real finalizer.

_PREVIOUS_CHIPLET = (
    'format_version: "1.0"\n'
    'assembly:\n'
    '  name: previous\n'
    'components: []\n'
)
_FINALIZE_MARKER = "_metadata:\n  finalize_required: true\n"
_STAGED_CHIPLET = (
    'format_version: "1.0"\n'
    + _FINALIZE_MARKER
    + 'assembly:\n'
    '  name: demo\n'
    'components: []\n'
)
_FINALIZED_CHIPLET = _STAGED_CHIPLET.replace(_FINALIZE_MARKER, "")


def _staged_finalize_options(tmp_path):
    return ExportOptions(output_dir=str(tmp_path / "out"), emit_chiplet=True,
                         emit_complete_gds=False)


def _install_staged_finalize_fakes(monkeypatch, **kwargs):
    """The run_export seam, with no interconnect PDK in the way."""
    installed = _install_run_export_fakes(monkeypatch, **kwargs)
    monkeypatch.setattr(
        orchestrator, "_load_interconnect_manifest",
        lambda *_a, **_kw: InterconnectManifestSnapshot(None))
    return installed


def _previous_export(tmp_path, mode=0o640):
    """An earlier export's canonical .chiplet, digest sidecar and mode."""
    from chiplet_export.pipeline import chiplet_merge
    out = tmp_path / "out"
    out.mkdir()
    canonical = out / "demo.chiplet"
    canonical.write_text(_PREVIOUS_CHIPLET, encoding="utf-8")
    chiplet_merge.record_exporter_content_digest(str(canonical))
    canonical.chmod(mode)
    sidecar = Path(chiplet_merge.digest_sidecar_path(str(canonical)))
    return canonical, sidecar


def _file_state(canonical, sidecar):
    return (canonical.read_bytes(), stat.S_IMODE(canonical.stat().st_mode),
            sidecar.read_bytes())


def _root_names(tmp_path):
    return {p.name for p in (tmp_path / "out").iterdir()}


def _layout_worker(exit_code=0, cancelled=False, finalize=True, seen=None):
    """A hyp_to_gds double that records what it was handed and when."""
    def worker(command):
        handed = Path(command[command.index("--update-chiplet-file") + 1])
        canonical = handed.parent / "demo.chiplet"
        if seen is not None:
            seen["handed"] = handed
            seen["canonical"] = (canonical.read_bytes()
                                 if canonical.exists() else None)
        if finalize:
            handed.write_text(
                handed.read_text(encoding="utf-8").replace(
                    _FINALIZE_MARKER, ""), encoding="utf-8")
        if seen is not None:
            seen["inode"] = handed.stat().st_ino
        return types.SimpleNamespace(exit_code=exit_code, cancelled=cancelled)
    return worker


def _retirement_notices(logs, unfinalized):
    return [line for line in logs if str(unfinalized) in line]


def _raise_in_the_drc_stage(monkeypatch, site, exc):
    """Make the DRC stage raise ``exc`` at ``site``, after the layout worker
    has run and the .chiplet has been replaced."""
    from chiplet_export.pipeline import discovery, runner

    if site == "runner-discovery":
        def find(*_args, **_kwargs):
            raise exc
        monkeypatch.setattr(discovery, "find_adk_drc_runner", find)
        return
    layout_run = runner.run_async

    def run_async(command, **kwargs):
        if command[1] == "/adk/run_drc.py":
            raise exc
        return layout_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_async", run_async)


@pytest.mark.parametrize("site, exc, line", [
    ("runner-discovery", RuntimeError("checkout is half cloned"),
     "RuntimeError: checkout is half cloned"),
    ("deck-launch", FileNotFoundError(errno.ENOENT,
                                      "No such file or directory",
                                      "/worker/python"),
     "FileNotFoundError: [Errno 2] No such file or directory: "
     "'/worker/python'"),
])
def test_a_drc_stage_that_raises_leaves_the_files_written(
        tmp_path, monkeypatch, site, exc, line):
    """The raise used to escape run_export: no result, no outputs manifest,
    and a caller saw a crash where the files were in fact written."""
    calls, _seams = _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=_layout_worker())
    _raise_in_the_drc_stage(monkeypatch, site, exc)
    manifests = []
    monkeypatch.setattr(orchestrator, "_write_outputs_manifest",
                        lambda *_a, **kw: manifests.append(kw))
    options = _staged_finalize_options(tmp_path)
    options.emit_complete_gds = True
    options.emit_assembly_drc = True
    logs = []
    result = orchestrator.run_export(
        _RunExportBoard(), options, "/unused/plugin", on_log=logs.append)

    canonical = tmp_path / "out" / "demo.chiplet"
    assert result.exit_code == 0 and result.error == ""
    assert result.chiplet_path == str(canonical)
    assert canonical.read_text(encoding="utf-8") == _FINALIZED_CHIPLET
    assert result.assembly_drc_exit_code == -1
    assert result.assembly_drc_report_path == ""
    assert result.assembly_drc_refused is False
    assert result.assembly_drc_error.startswith("Traceback")
    assert result.assembly_drc_error.rstrip().endswith(line)
    assert ("Assembly DRC stage raised %s; the files are written, the DRC "
            "did not complete" % line) in logs
    assert len(calls) == 1  # the layout worker; the deck never started
    assert [kw["chiplet_written"] for kw in manifests] == [True]


def test_an_interrupt_in_the_drc_stage_still_propagates(tmp_path,
                                                        monkeypatch):
    _install_staged_finalize_fakes(monkeypatch)
    _raise_in_the_drc_stage(monkeypatch, "deck-launch", KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_export(
            _RunExportBoard(), _drc_options(tmp_path), "/unused/plugin")


@pytest.mark.parametrize("finalized", [False, True],
                         ids=["fails-before-finalizing",
                              "fails-after-finalizing"])
def test_failed_worker_leaves_the_existing_canonical_chiplet_as_it_was(
        tmp_path, monkeypatch, finalized):
    """A failed re-export must not cost the user the .chiplet they had.

    The worker can fail after its own write has landed (an orientation error,
    no devices loaded, the complete GDS write), so both cases are covered, and
    the notice must not claim which one happened.
    """
    canonical, sidecar = _previous_export(tmp_path)
    before = _file_state(canonical, sidecar)
    seen = {}
    _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=_layout_worker(exit_code=1, finalize=finalized,
                                     seen=seen))
    logs = []
    result = orchestrator.run_export(
        _RunExportBoard(), _staged_finalize_options(tmp_path),
        "/unused/plugin", on_log=logs.append)

    assert result.exit_code == 1
    assert result.chiplet_path == ""
    assert seen["canonical"] == before[0], (
        "the canonical file was rewritten before the worker even finished")
    assert _file_state(canonical, sidecar) == before
    unfinalized = canonical.with_name("demo.chiplet.unfinalized")
    assert unfinalized.read_text(encoding="utf-8") == (
        _FINALIZED_CHIPLET if finalized else _STAGED_CHIPLET)
    assert _root_names(tmp_path) == {
        canonical.name, sidecar.name, unfinalized.name, "demo.hyp", "layout"}
    notices = _retirement_notices(logs, unfinalized)
    assert len(notices) == 1
    assert "%s was left unchanged" % canonical in notices[0]
    assert "intermediate" not in notices[0]
    # The worker's own errors name the file it was handed, so the notice has
    # to name it too, or the two cannot be connected.
    assert str(seen["handed"]) in notices[0]


def test_failed_worker_never_creates_a_canonical_chiplet(tmp_path,
                                                         monkeypatch):
    """No .chiplet before a failed first export means none after, and none
    in between: a reader watching the directory never sees one appear."""
    seen = {}
    _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=_layout_worker(exit_code=1, seen=seen))
    logs = []
    result = orchestrator.run_export(
        _RunExportBoard(), _staged_finalize_options(tmp_path),
        "/unused/plugin", on_log=logs.append)

    out = tmp_path / "out"
    canonical = out / "demo.chiplet"
    assert result.chiplet_path == ""
    assert seen["canonical"] is None, (
        "a .chiplet existed while the worker ran, before anything succeeded")
    assert not canonical.exists()
    assert _root_names(tmp_path) == {
        "demo.chiplet.unfinalized", "demo.hyp", "layout"}
    notices = _retirement_notices(logs, out / "demo.chiplet.unfinalized")
    assert len(notices) == 1
    assert "no %s was created" % canonical in notices[0]


def test_successful_worker_replaces_the_canonical_with_the_finalized_file(
        tmp_path, monkeypatch):
    """The worker finalizes a sibling; success renames that very file over the
    canonical one, which keeps its mode, and only then records the digest."""
    from chiplet_export.pipeline import chiplet_merge
    canonical, sidecar = _previous_export(tmp_path, mode=0o640)
    previous_sidecar = sidecar.read_bytes()
    seen = {}
    _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=_layout_worker(seen=seen))
    logs = []
    result = orchestrator.run_export(
        _RunExportBoard(), _staged_finalize_options(tmp_path),
        "/unused/plugin", on_log=logs.append)

    handed = seen["handed"]
    assert handed.parent == canonical.parent
    assert handed != canonical
    assert not handed.name.endswith(".chiplet")
    assert seen["canonical"] == _PREVIOUS_CHIPLET.encode("utf-8")

    assert result.error == ""
    assert result.exit_code == 0
    assert result.chiplet_path == str(canonical)
    assert canonical.read_text(encoding="utf-8") == _FINALIZED_CHIPLET
    # Renamed, not copied: a reader never sees a half-written canonical file.
    assert canonical.stat().st_ino == seen["inode"]
    assert stat.S_IMODE(canonical.stat().st_mode) == 0o640
    assert _root_names(tmp_path) == {
        canonical.name, sidecar.name, "demo.hyp", "layout"}
    assert sidecar.read_bytes() != previous_sidecar
    assert sidecar.read_text(encoding="utf-8").strip() == (
        chiplet_merge.exporter_content_digest(str(canonical)))
    # The worker's output names only the staging path, which is gone now; one
    # line has to say the canonical file was replaced by it.
    assert "Replaced %s with the finalized .chiplet (staged as %s)." % (
        canonical, handed) in logs
    # Promoted, so there was nothing to retire and nothing to warn about.
    assert not [line for line in logs if "demo.chiplet.unfinalized" in line]


@pytest.mark.parametrize("failure", ["argv-refusal", "worker-cannot-start"])
def test_exception_after_staging_leaves_the_canonical_and_keeps_the_staged_file(
        tmp_path, monkeypatch, failure):
    """Raising between staging and the worker's result is still a failure.

    build_cli_args refuses a ref it cannot encode, and Popen raises when the
    worker python does not exist. Neither may leave the canonical file holding
    an intermediate, nor leave the staging file behind under its temporary
    name.
    """
    canonical, sidecar = _previous_export(tmp_path)
    before = _file_state(canonical, sidecar)
    if failure == "argv-refusal":
        _install_staged_finalize_fakes(
            monkeypatch, chiplet_body=_STAGED_CHIPLET,
            die_connections={"U,1": "method"})
        expected = ValueError
    else:
        def cannot_start(command):
            raise FileNotFoundError(2, "No such file or directory", command[0])
        _install_staged_finalize_fakes(
            monkeypatch, chiplet_body=_STAGED_CHIPLET,
            layout_worker=cannot_start)
        expected = FileNotFoundError
    logs = []
    with pytest.raises(expected):
        orchestrator.run_export(
            _RunExportBoard(), _staged_finalize_options(tmp_path),
            "/unused/plugin", on_log=logs.append)

    assert _file_state(canonical, sidecar) == before
    unfinalized = canonical.with_name("demo.chiplet.unfinalized")
    assert unfinalized.read_text(encoding="utf-8") == _STAGED_CHIPLET
    assert _root_names(tmp_path) == {
        canonical.name, sidecar.name, unfinalized.name, "demo.hyp", "layout"}
    assert len(_retirement_notices(logs, unfinalized)) == 1


def test_finalized_canonical_keeps_relative_layout_references(
        tmp_path, monkeypatch, attach_frame_pdk):
    """The real finalizer writes layout: references relative to the directory
    of the file it finalizes. That is only right for the canonical file if the
    staging file sits in the same directory, which this proves by moving the
    exported set and resolving every reference from its new place."""
    db = pytest.importorskip("klayout.db")
    yaml = pytest.importorskip("yaml")
    import hyp_to_gds

    def write_gds(path, top_name):
        layout = db.Layout()
        top = layout.create_cell(top_name)
        top.shapes(layout.layer(8, 0)).insert(db.Box(0, 0, 1000, 1000))
        path.parent.mkdir(parents=True, exist_ok=True)
        layout.write(str(path))

    # The die GDS lives outside the output dir and its board-relative path
    # does not resolve from there, so the finalizer bundles it next to the file
    # it finalizes.
    die_gds = tmp_path / "board" / "dies" / "die.gds"
    write_gds(die_gds, "DIE")
    staged = (
        'format_version: "1.0"\n'
        + _FINALIZE_MARKER
        + 'assembly:\n'
        '  name: demo\n'
        'components:\n'
        '  - id: interposer\n'
        '    type: interposer\n'
        '    dimensions: {width: 1.0, height: 1.0, thickness: 100.0}\n'
        '    position: {x: 0.0, y: 0.0, z: 0.0}\n'
        '  - id: U1\n'
        '    type: die\n'
        '    layout: ../dies/die.gds\n'
        '    dimensions: {width: 1.0, height: 1.0, thickness: 0.0}\n'
        '    position: {x: 0.0, y: 0.0, z: 0.0}\n'
    )
    seen = {}

    def real_finalizer(command):
        seen["handed"] = Path(command[command.index("--update-chiplet-file")
                                      + 1])
        interposer_gds = Path(command[command.index("-o") + 1])
        write_gds(interposer_gds, "INTERPOSER")
        # write_gds draws the die as one 1 x 1 um rectangle on 8/0, which
        # is therefore its outline layer.
        ok = hyp_to_gds.update_chiplet_file(
            str(seen["handed"]), str(interposer_gds),
            devices=[hyp_to_gds.Device("U1", "TopMetal2", str(die_gds))],
            die_outline_layers={"U1": (8, 0)})
        return types.SimpleNamespace(exit_code=0 if ok else 1,
                                     cancelled=False)

    _install_staged_finalize_fakes(monkeypatch, chiplet_body=staged,
                                   layout_worker=real_finalizer)
    result = orchestrator.run_export(
        _RunExportBoard(), _staged_finalize_options(tmp_path),
        "/unused/plugin")

    out = tmp_path / "out"
    assert result.error == "" and result.exit_code == 0
    assert seen["handed"].parent == out
    assert seen["handed"] != out / "demo.chiplet"

    moved = tmp_path / "moved"
    shutil.move(str(out), str(moved))
    document = yaml.safe_load((moved / "demo.chiplet").read_text(
        encoding="utf-8"))
    layouts = {c["id"]: c["layout"] for c in document["components"]}
    assert layouts["interposer"] == os.path.join("layout",
                                                 "demo_interposer.gds")
    for ref, reference in sorted(layouts.items()):
        assert not os.path.isabs(reference), ref
        assert (moved / reference).is_file(), (
            "%s: %s does not resolve from the moved output" % (ref, reference))
    assert (moved / layouts["U1"]).read_bytes() == die_gds.read_bytes()


@pytest.mark.parametrize("exit_code", [0, -15],
                         ids=["cancelled-after-exit-0",
                              "cancelled-while-running"])
def test_cancelled_worker_leaves_the_existing_canonical_chiplet_as_it_was(
        tmp_path, monkeypatch, exit_code):
    """A cancel is a failure for the .chiplet, even when the worker had already
    written the file it was handed and exited 0 before the cancel was seen."""
    canonical, sidecar = _previous_export(tmp_path)
    before = _file_state(canonical, sidecar)
    outputs_manifest = orchestrator._write_outputs_manifest
    _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=_layout_worker(exit_code=exit_code, cancelled=True))
    monkeypatch.setattr(orchestrator, "_write_outputs_manifest",
                        outputs_manifest)
    result = orchestrator.run_export(
        _RunExportBoard(), _staged_finalize_options(tmp_path),
        "/unused/plugin")

    assert result.cancelled
    assert result.chiplet_path == ""
    assert _file_state(canonical, sidecar) == before
    unfinalized = canonical.with_name("demo.chiplet.unfinalized")
    assert unfinalized.read_text(encoding="utf-8") == _FINALIZED_CHIPLET
    assert _root_names(tmp_path) == {
        canonical.name, sidecar.name, unfinalized.name, "demo.hyp", "layout",
        "MANIFEST.md"}
    # Retired by the run itself, before its MANIFEST was written, not by the
    # clean-up that only an exception should reach.
    manifest = (tmp_path / "out" / "MANIFEST.md").read_text(encoding="utf-8")
    assert "- [x] `demo.chiplet.unfinalized`" in manifest


@pytest.mark.parametrize("worker_exit", [1, 0], ids=["failed", "succeeded"])
def test_manifest_lists_only_a_chiplet_this_run_wrote(tmp_path, monkeypatch,
                                                      worker_exit):
    """An earlier export's .chiplet on disk is not this run's product."""
    _previous_export(tmp_path)
    outputs_manifest = orchestrator._write_outputs_manifest
    _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=_layout_worker(exit_code=worker_exit))
    monkeypatch.setattr(orchestrator, "_write_outputs_manifest",
                        outputs_manifest)
    orchestrator.run_export(
        _RunExportBoard(), _staged_finalize_options(tmp_path),
        "/unused/plugin")

    manifest = (tmp_path / "out" / "MANIFEST.md").read_text(encoding="utf-8")
    entry = [line for line in manifest.splitlines()
             if "`demo.chiplet`" in line]
    assert len(entry) == 1
    not_regenerated = ("This run did not replace the .chiplet, so none of "
                       "its content was regenerated.")
    if worker_exit:
        assert entry[0].startswith("- [ ] "), entry[0]
        assert "earlier export" in entry[0]
        assert "- [x] `demo.chiplet.unfinalized`" in manifest
        assert not_regenerated in manifest
    else:
        assert entry[0].startswith("- [x] "), entry[0]
        assert "earlier export" not in entry[0]
        assert "demo.chiplet.unfinalized" not in manifest
        assert not_regenerated not in manifest


def test_unreplaceable_canonical_is_an_error_and_the_drc_never_reads_it(
        tmp_path, monkeypatch):
    """The worker succeeded but its file could not take the canonical name.

    The canonical file on disk is then an earlier export's, with that export's
    adapters. The assembly DRC must not run against it, and the run must not
    report success.
    """
    canonical, sidecar = _previous_export(tmp_path)
    before = _file_state(canonical, sidecar)
    calls, _seams = _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=_layout_worker())
    real_replace = os.replace

    def refuse_promotion(src, dst, *args, **kwargs):
        if os.fspath(dst) == str(canonical):
            raise PermissionError(13, "Permission denied", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(orchestrator.os, "replace", refuse_promotion)
    options = _staged_finalize_options(tmp_path)
    options.emit_complete_gds = True
    options.emit_assembly_drc = True
    result = orchestrator.run_export(_RunExportBoard(), options,
                                     "/unused/plugin")

    assert str(canonical) in result.error
    assert result.chiplet_path == ""
    assert len(calls) == 1, "the assembly DRC ran: %r" % (calls[1:],)
    assert result.assembly_drc_exit_code == -1
    assert _file_state(canonical, sidecar) == before
    unfinalized = canonical.with_name("demo.chiplet.unfinalized")
    assert unfinalized.read_text(encoding="utf-8") == _FINALIZED_CHIPLET


def _manifest_text(tmp_path):
    return (tmp_path / "out" / "MANIFEST.md").read_text(encoding="utf-8")


def test_a_failed_forced_export_does_not_claim_it_regenerated_anything(
        tmp_path, monkeypatch):
    """The guard's note on a forced run is written before the worker runs, so
    it says exporter-owned content WILL BE regenerated. A run that then fails
    regenerated nothing, and the MANIFEST has to say so next to the note."""
    canonical, sidecar = _previous_export(tmp_path)
    # Exporter-owned content edited after the digest was recorded.
    canonical.write_text(_PREVIOUS_CHIPLET.replace("previous", "hand-edited"),
                         encoding="utf-8")
    before = _file_state(canonical, sidecar)
    outputs_manifest = orchestrator._write_outputs_manifest
    _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=_layout_worker(exit_code=1))
    monkeypatch.setattr(orchestrator, "_write_outputs_manifest",
                        outputs_manifest)
    options = _staged_finalize_options(tmp_path)
    options.force = True
    result = orchestrator.run_export(_RunExportBoard(), options,
                                     "/unused/plugin")

    assert result.exit_code == 1
    assert _file_state(canonical, sidecar) == before
    manifest = _manifest_text(tmp_path)
    guard = manifest[manifest.index("## Previous-export content guard"):]
    assert "WILL BE regenerated by this export" in guard
    assert ("This run did not replace the .chiplet, so none of its content "
            "was regenerated.") in guard


def test_manifest_without_emit_chiplet_credits_no_chiplet_file(tmp_path,
                                                               monkeypatch):
    """With emit_chiplet off the run writes no .chiplet at all, so neither an
    earlier export's canonical file nor an .unfinalized file an earlier failed
    run left is this run's product."""
    canonical, _sidecar = _previous_export(tmp_path)
    stale = canonical.with_name("demo.chiplet.unfinalized")
    stale.write_text("left by an earlier failed export\n", encoding="utf-8")
    outputs_manifest = orchestrator._write_outputs_manifest
    _install_staged_finalize_fakes(monkeypatch, chiplet_body=_STAGED_CHIPLET)
    monkeypatch.setattr(orchestrator, "_write_outputs_manifest",
                        outputs_manifest)
    options = _staged_finalize_options(tmp_path)
    options.emit_chiplet = False
    result = orchestrator.run_export(_RunExportBoard(), options,
                                     "/unused/plugin")

    assert result.error == "" and result.exit_code == 0
    manifest = _manifest_text(tmp_path)
    entry = [line for line in manifest.splitlines()
             if "`demo.chiplet`" in line]
    assert len(entry) == 1
    assert entry[0].startswith("- [ ] "), entry[0]
    assert "earlier export" in entry[0]
    assert "demo.chiplet.unfinalized" not in manifest
    assert "Previous-export content guard" not in manifest


def test_drc_without_emit_chiplet_reads_this_runs_adapters(tmp_path,
                                                           monkeypatch):
    """With emit_chiplet off the DRC reads the intermediate this run wrote. A
    .chiplet an earlier export left in the output dir declares that export's
    adapters, which need not be this layout's."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "demo.chiplet").write_text(
        'format_version: "1.0"\ninterposer:\n  adapter: stale\n'
        'components: []\n', encoding="utf-8")
    calls, _seams = _install_staged_finalize_fakes(
        monkeypatch,
        chiplet_body=('format_version: "1.0"\ninterposer:\n  adapter: fresh\n'
                      'components: []\n'))
    result = orchestrator.run_export(
        _RunExportBoard(), _drc_options(tmp_path), "/unused/plugin")

    assert result.error == ""
    assert len(calls) == 2
    drc = calls[1]
    assert drc[drc.index("--interposer-adapter") + 1] == "fresh"


@pytest.mark.parametrize("change", ["edited", "created"])
def test_a_chiplet_changed_during_the_run_is_not_replaced(tmp_path,
                                                          monkeypatch, change):
    """The canonical file stays loadable while the worker runs, which can take
    minutes. A save made in that time is newer than anything this run read, so
    replacing the file would discard it, and the digest recorded next would
    hide that it ever existed."""
    out = tmp_path / "out"
    sidecar = out / "demo.chiplet.exportcontent.sha256"
    if change == "edited":
        canonical, sidecar = _previous_export(tmp_path)
    else:
        canonical = out / "demo.chiplet"
    sidecar_before = sidecar.read_bytes() if sidecar.exists() else None
    saved = _PREVIOUS_CHIPLET + "flow:\n  steps: [saved during the export]\n"
    finalize = _layout_worker()

    def save_meanwhile(command):
        canonical.write_text(saved, encoding="utf-8")
        return finalize(command)

    calls, _seams = _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=save_meanwhile)
    options = _staged_finalize_options(tmp_path)
    options.emit_complete_gds = True
    options.emit_assembly_drc = True
    result = orchestrator.run_export(_RunExportBoard(), options,
                                     "/unused/plugin")

    assert str(canonical) in result.error
    assert "changed on disk" in result.error
    assert result.exit_code == 0
    assert result.chiplet_path == ""
    assert canonical.read_text(encoding="utf-8") == saved
    assert canonical.with_name("demo.chiplet.unfinalized").read_text(
        encoding="utf-8") == _FINALIZED_CHIPLET
    assert (sidecar.read_bytes() if sidecar.exists() else None) == (
        sidecar_before)
    assert len(calls) == 1, "the assembly DRC ran: %r" % (calls[1:],)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root can write a file whatever its mode")
@pytest.mark.parametrize("force", [False, True])
def test_a_read_only_chiplet_is_refused_before_the_worker_runs(
        tmp_path, monkeypatch, force):
    """A rename needs only the directory to be writable, so the replace would
    go through over a .chiplet the user made read-only, and leave the new file
    read-only too, still looking protected. Refused up front instead."""
    canonical, sidecar = _previous_export(tmp_path, mode=0o444)
    before = _file_state(canonical, sidecar)
    # The staged file would carry the 0444 too, which only the real worker's
    # atomic write gets past; this double exits 0 without writing it.
    calls, _seams = _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=_layout_worker(finalize=False))
    options = _staged_finalize_options(tmp_path)
    options.force = force
    result = orchestrator.run_export(_RunExportBoard(), options,
                                     "/unused/plugin")

    assert str(canonical) in result.error
    assert "not writable" in result.error
    assert "unchanged" in result.error
    assert calls == []
    assert _file_state(canonical, sidecar) == before
    assert _root_names(tmp_path) == {canonical.name, sidecar.name, "layout"}


def test_a_staging_failure_is_an_error_and_leaves_nothing_behind(
        tmp_path, monkeypatch):
    """Staging is a write in the output dir and can fail like one. That is an
    ExportResult error naming the .chiplet, not an exception out of
    run_export, and the partly written staging file is removed: nothing else
    would ever retire it."""
    canonical, sidecar = _previous_export(tmp_path)
    before = _file_state(canonical, sidecar)
    calls, _seams = _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=_layout_worker())

    def disk_full(_src, dst, *_args, **_kwargs):
        dst.write(b"partial")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(orchestrator.shutil, "copyfileobj", disk_full)
    result = orchestrator.run_export(
        _RunExportBoard(), _staged_finalize_options(tmp_path),
        "/unused/plugin")

    assert "Cannot stage the .chiplet next to %s" % canonical in result.error
    assert "No space left on device" in result.error
    assert calls == []
    assert _file_state(canonical, sidecar) == before
    assert _root_names(tmp_path) == {canonical.name, sidecar.name, "layout"}


def test_a_first_export_stages_a_hidden_file_with_the_umask_mode(
        tmp_path, monkeypatch):
    """With no canonical file there is no mode to carry over, so the staging
    file, and with it the new .chiplet, gets what any new file in the output
    dir would. Its leading dot keeps it out of a plain directory listing."""
    seen = {}
    _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=_layout_worker(seen=seen))
    logs = []
    previous_umask = os.umask(0o022)
    try:
        result = orchestrator.run_export(
            _RunExportBoard(), _staged_finalize_options(tmp_path),
            "/unused/plugin", on_log=logs.append)
    finally:
        os.umask(previous_umask)

    canonical = tmp_path / "out" / "demo.chiplet"
    handed = seen["handed"]
    assert result.chiplet_path == str(canonical)
    assert handed == canonical.with_name(".demo.chiplet.staging")
    assert stat.S_IMODE(canonical.stat().st_mode) == 0o644
    assert "Wrote the finalized .chiplet to %s (staged as %s)." % (
        canonical, handed) in logs


def test_a_staged_file_that_cannot_be_retired_is_named_where_it_stays(
        tmp_path, monkeypatch):
    """When the rename to .unfinalized fails the staged file keeps its hidden
    name. The notice still has to say the export failed and the canonical file
    is unchanged, and say where the file is; and the MANIFEST must not list an
    .unfinalized file an earlier run left as this run's."""
    canonical, sidecar = _previous_export(tmp_path)
    before = _file_state(canonical, sidecar)
    stale = canonical.with_name("demo.chiplet.unfinalized")
    stale.write_text("left by an earlier failed export\n", encoding="utf-8")
    seen = {}
    outputs_manifest = orchestrator._write_outputs_manifest
    _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=_layout_worker(exit_code=1, seen=seen))
    monkeypatch.setattr(orchestrator, "_write_outputs_manifest",
                        outputs_manifest)
    real_replace = os.replace

    def refuse_retirement(src, dst, *args, **kwargs):
        if os.fspath(dst) == str(stale):
            raise PermissionError(13, "Permission denied", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(orchestrator.os, "replace", refuse_retirement)
    logs = []
    result = orchestrator.run_export(
        _RunExportBoard(), _staged_finalize_options(tmp_path),
        "/unused/plugin", on_log=logs.append)

    handed = seen["handed"]
    assert result.exit_code == 1
    assert _file_state(canonical, sidecar) == before
    assert handed.read_text(encoding="utf-8") == _FINALIZED_CHIPLET
    assert stale.read_text(encoding="utf-8") == (
        "left by an earlier failed export\n")
    notices = [line for line in logs if "remains at %s" % handed in line]
    assert len(notices) == 1
    assert "The export failed or was cancelled" in notices[0]
    assert "%s was left unchanged" % canonical in notices[0]
    assert "demo.chiplet.unfinalized" not in _manifest_text(tmp_path)


def test_a_staging_file_an_interrupted_export_left_is_removed_and_named(
        tmp_path, monkeypatch):
    """A run retires its own staging file, so one already there was left by a
    run that was killed before it could. It is the exporter's own artefact,
    never a product, and the name is fixed, so this run removes it, says so,
    and stages at the same deterministic path."""
    canonical, _sidecar = _previous_export(tmp_path)
    leftover = canonical.with_name(".demo.chiplet.staging")
    leftover.write_text("left by a killed export\n", encoding="utf-8")
    seen = {}
    _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET,
        layout_worker=_layout_worker(seen=seen))
    logs = []
    result = orchestrator.run_export(
        _RunExportBoard(), _staged_finalize_options(tmp_path),
        "/unused/plugin", on_log=logs.append)

    assert result.chiplet_path == str(canonical)
    assert seen["handed"] == leftover
    assert not leftover.exists()
    assert canonical.read_text(encoding="utf-8") == _FINALIZED_CHIPLET
    assert ("Removed %s, a staging file left by an export that did not "
            "finish." % leftover) in logs


def test_the_staging_path_is_fixed_and_keeps_the_canonical_spelling():
    """The worker's argv and the run log name the staging file, so it must not
    change from run to run, and a relative output dir stays relative."""
    assert orchestrator._staging_path("out/demo.chiplet") == (
        os.path.join("out", ".demo.chiplet.staging"))
    assert orchestrator._staging_path("/abs/out/demo.chiplet") == (
        "/abs/out/.demo.chiplet.staging")


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


@pytest.mark.parametrize('override', [False, True])
def test_export_report_is_read_once_and_shared_without_inverting_an_override(tmp_path, monkeypatch, override):
    _calls, _seams = _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET, layout_worker=_layout_worker())
    from chiplet_export import draft_net_names
    writer = sys.modules['chiplet_export.writers.chiplet_writer']
    reads, consumers = [], []
    original_read = draft_net_names.read_export_report
    original_write = writer.write_chiplet

    def read(path):
        value = original_read(path)
        reads.append(value)
        return value

    def write(board, path, **kwargs):
        consumers.append(kwargs['export_report'])
        return original_write(board, path, **kwargs)

    def pads(board, path, **kwargs):
        consumers.append(kwargs['export_report'])
        return 0

    monkeypatch.setattr(draft_net_names, 'read_export_report', read)
    writer.write_chiplet, writer.write_io_pads_json = write, pads
    writer.preflight_carriers = lambda board, names: consumers.append(names)
    report = tmp_path / 'export.json'
    report.write_text(json.dumps({'format': 'interposer_pnr.export', 'version': 1,
        'round_trip': {'version': 3, 'net_names': {'planned': 'SHEET'}, 'sheet_nets': {}}}))
    options = _staged_finalize_options(tmp_path)
    options.export_report = str(report)
    supplied = tmp_path / 'authored.json'
    authored = b'{"io_pads":[{"net":"planned"}]}\n'
    if override:
        supplied.write_bytes(authored)
        options.io_pads_json = str(supplied)
    result = orchestrator.run_export(_RunExportBoard(), options, '/unused/plugin')
    assert result.error == ''
    assert len(reads) == 1
    assert len(consumers) == (2 if override else 3)
    assert all(value is reads[0] for value in consumers)
    assert reads[0].names.sheet_to_planner == {'SHEET': 'planned'}
    if override:
        assert supplied.read_bytes() == authored
        command = _calls[0]
        assert command[command.index('--io-pads') + 1] == str(supplied)


def test_bad_export_report_is_refused_before_output_creation(tmp_path, monkeypatch):
    calls, _seams = _install_staged_finalize_fakes(monkeypatch)
    options = _staged_finalize_options(tmp_path)
    report = tmp_path / 'invalid.json'
    report.write_text('[]')
    options.export_report = str(report)
    result = orchestrator.run_export(_RunExportBoard(), options, '/unused/plugin')
    assert result.error.startswith('Export report refused: export report ')
    assert 'not an interposer_pnr.export report, version 1' in result.error
    assert result.error_input == 'export_report'
    assert not Path(options.output_dir).exists()
    assert calls == []


def test_a_carrier_refusal_comes_before_output_creation(tmp_path, monkeypatch):
    calls, _seams = _install_staged_finalize_fakes(monkeypatch)
    writer = sys.modules['chiplet_export.writers.chiplet_writer']

    def refuse(_board, _report):
        raise ValueError("carrier J1: its pads carry different nets ('', 'N'); a carrier's pads carry one net")

    writer.preflight_carriers = refuse
    options = _staged_finalize_options(tmp_path)
    result = orchestrator.run_export(_RunExportBoard(), options, '/unused/plugin')
    assert result.error == "IO pad carriers refused: carrier J1: its pads carry different nets ('', 'N'); a carrier's pads carry one net"
    assert result.error_input == ''
    assert not Path(options.output_dir).exists()
    assert calls == []


def test_a_board_report_refusal_is_distinguished_before_output_creation(tmp_path, monkeypatch):
    from chiplet_export.draft_net_names import BoardReportError

    calls, _seams = _install_staged_finalize_fakes(monkeypatch)
    writer = sys.modules['chiplet_export.writers.chiplet_writer']

    def refuse(_board, _report):
        raise BoardReportError("report does not describe the board")

    writer.preflight_carriers = refuse
    options = _staged_finalize_options(tmp_path)
    result = orchestrator.run_export(_RunExportBoard(), options, '/unused/plugin')
    assert result.error == 'IO pad carriers refused: report does not describe the board'
    assert result.error_input == 'export_report'
    assert not Path(options.output_dir).exists()
    assert calls == []


# ---------------------------------------------------------------------------
# Connection-method availability: the worker's connection_methods.py report
# ---------------------------------------------------------------------------

def _methods_command(tmp_path, body):
    """A plugin dir whose connection_methods.py runs ``body``."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "connection_methods.py").write_text(
        "import json, os, sys\n" + body, encoding="utf-8")
    return str(plugin)


def _report(*methods):
    return "print(json.dumps(%r))\n" % {
        "format": "chiplet_export.connection_methods", "version": 1,
        "methods": list(methods)}


def test_the_availability_query_reads_the_report_with_the_export_roots(tmp_path):
    plugin = _methods_command(tmp_path, (
        "env = os.environ\n"
        "print(json.dumps({'format': 'chiplet_export.connection_methods',"
        " 'version': 1, 'methods': ["
        "{'id': 'pillar', 'available': True, 'reason': None},"
        "{'id': 'bump', 'available': False, 'reason': '%s|%s' % ("
        "env['INTERPOSER_PDK_ROOT'], env['INTERCONNECT_PDK_ROOT'])}]}))\n"))
    availability, command = orchestrator.query_connection_availability(
        sys.executable, plugin, ExportOptions(
            interposer_pdk_root="/interposer", interconnect_pdk_root="/ic"))
    assert command == [sys.executable,
                       str(Path(plugin) / "connection_methods.py")]
    assert availability == {"pillar": None, "bump": "/interposer|/ic"}
    assert list(availability) == ["pillar", "bump"]


@pytest.mark.parametrize("body,message", [
    ("sys.stderr.write('Error: torn manifest\\n'); sys.exit(5)\n",
     "exited 5: Error: torn manifest"),
    ("print('not json')\n", "printed no JSON report"),
    ("print('[]')\n", "printed an unrecognized report"),
    ("print(json.dumps({'format': 'other', 'version': 1, 'methods': []}))\n",
     "printed an unrecognized report"),
    (_report({"id": "bump", "available": False, "reason": None}),
     "malformed method entry"),
    (_report({"id": "bump", "available": True, "reason": "why"}),
     "malformed method entry"),
    (_report({"id": "bump", "available": False, "reason": ""}),
     "malformed method entry"),
    (_report({"id": 7, "available": True, "reason": None}),
     "malformed method entry"),
    (_report("bump"), "malformed method entry"),
])
def test_an_availability_report_that_cannot_be_trusted_is_an_error(
        tmp_path, body, message):
    plugin = _methods_command(tmp_path, body)
    with pytest.raises(orchestrator.ConnectionAvailabilityError, match=message):
        orchestrator.query_connection_availability(
            sys.executable, plugin, ExportOptions())


def test_an_availability_worker_that_cannot_start_is_an_error(tmp_path):
    with pytest.raises(orchestrator.ConnectionAvailabilityError,
                       match="no-such-python"):
        orchestrator.query_connection_availability(
            str(tmp_path / "no-such-python"), str(tmp_path), ExportOptions())


def test_refused_landings_resolve_each_die_as_the_worker_does():
    availability = {"pillar": None, "bump": "bump: no pad cell"}
    assert orchestrator.refused_landings(
        availability, ["U2", "U1", "U3", "U4"],
        {"U1": "pillar", "U3": "", "U4": "unknown"}, "bump") == [
            ("U2", "bump", "bump: no pad cell")]
    assert orchestrator.refused_landings(
        availability, ["U1"], {"U1": "bump"}, "") == [
            ("U1", "bump", "bump: no pad cell")]
    assert orchestrator.refused_landings(availability, ["U1"], {}, "") == []


def _landing_fakes(monkeypatch):
    """The run_export fakes, with die U1's pins extracted and a manifest."""
    calls, _seams = _install_run_export_fakes(monkeypatch)
    writer = sys.modules["chiplet_export.writers.chiplet_writer"]
    writer.write_die_pin_lists = lambda *_a, **_kw: {"U1": "/pins/U1.json"}
    snapshot = InterconnectManifestSnapshot(
        {"schema_version": "1.0",
         "methods": {"pillar": {}, "bump": {}, "sbump_sac305": {}}},
        root="/selected/ic")
    monkeypatch.setattr(orchestrator, "_load_interconnect_manifest",
                        lambda *_a, **_kw: snapshot)
    return calls


def _landing_run(tmp_path, monkeypatch, answer, **options):
    """run_export through _landing_fakes, the query recorded."""
    calls = _landing_fakes(monkeypatch)
    queries = []

    def query(worker_py, plugin_dir, query_options):
        queries.append((worker_py, plugin_dir,
                        query_options.interconnect_pdk_root))
        return answer, [worker_py, plugin_dir + "/connection_methods.py"]

    monkeypatch.setattr(orchestrator, "query_connection_availability", query)
    run_options = _drc_options(tmp_path)
    for name, value in options.items():
        setattr(run_options, name, value)
    log = []
    result = orchestrator.run_export(_RunExportBoard(), run_options,
                                     "/plugin", on_log=log.append)
    return result, calls, queries, log


@pytest.mark.parametrize("options", [
    {"connection_type": "bump"},
    {"connection_type": "pillar", "die_connections": {"U1": "bump"}},
])
def test_run_export_refuses_an_undrawable_landing_before_staging(
        tmp_path, monkeypatch, options):
    result, calls, queries, log = _landing_run(
        tmp_path, monkeypatch,
        {"pillar": None, "bump": "bump: no pad cell"}, **options)
    assert result.error == ("Cannot draw the landing of U1 (bump): "
                            "bump: no pad cell")
    assert queries == [("/worker/python", "/plugin", "/selected/ic")]
    assert "$ /worker/python /plugin/connection_methods.py" in log
    assert calls == []
    # Nothing staged: no .chiplet, no copied .hyp, only the empty layout dir.
    assert [p.name for p in (tmp_path / "out").rglob("*") if p.is_file()] == []


@pytest.mark.parametrize("options,asked", [
    ({"connection_type": "pillar"}, True),
    ({"connection_type": ""}, False),
    # The worker merges a given cupillar_gds and draws no pads, even for
    # pin lists passed alongside it.
    ({"connection_type": "bump", "cupillar_gds": "/given/pillars.gds",
      "pad_locations": {"U1": "/pins/U1.json"}}, False),
])
def test_run_export_asks_only_for_the_landings_it_draws(
        tmp_path, monkeypatch, options, asked):
    result, calls, queries, _log = _landing_run(
        tmp_path, monkeypatch,
        {"pillar": None, "bump": "bump: no pad cell"}, **options)
    assert not result.error.startswith("Cannot draw")
    assert bool(queries) is asked
    assert calls  # the layout worker ran


def test_run_export_refuses_when_availability_cannot_be_read(
        tmp_path, monkeypatch):
    calls = _landing_fakes(monkeypatch)

    def failed(*_a, **_kw):
        raise orchestrator.ConnectionAvailabilityError("worker exited 1: boom")

    monkeypatch.setattr(orchestrator, "query_connection_availability", failed)
    options = _drc_options(tmp_path)
    options.connection_type = "pillar"
    result = orchestrator.run_export(_RunExportBoard(), options, "/plugin")
    assert result.error == ("Cannot check the connection methods: "
                            "worker exited 1: boom")
    assert calls == []


def test_the_cli_connection_flag_reaches_the_landing_refusal(
        tmp_path, monkeypatch, capsys):
    """chiplet-export --connection is free text; run_export refuses it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_cli_landing_under_test", PLUGIN_ROOT / "chiplet_export_cli.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    calls = _landing_fakes(monkeypatch)
    monkeypatch.setitem(sys.modules, "pcbnew", types.SimpleNamespace(
        LoadBoard=lambda _path: _RunExportBoard()))
    monkeypatch.setattr(
        orchestrator, "query_connection_availability",
        lambda *_a, **_kw: ({"sbump_sac305": "sbump_sac305: no pad cell"},
                            ["/worker/python", "connection_methods.py"]))
    board = tmp_path / "board.kicad_pcb"
    board.touch()
    assert cli.main(["--board", str(board), "--output-dir",
                     str(tmp_path / "out"), "--connection",
                     "sbump_sac305"]) == 1
    out = capsys.readouterr().out
    assert ("error:               Cannot draw the landing of U1 "
            "(sbump_sac305): sbump_sac305: no pad cell") in out
    assert calls == []
