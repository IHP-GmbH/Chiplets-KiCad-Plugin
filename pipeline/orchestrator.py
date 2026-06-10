# SPDX-License-Identifier: GPL-2.0-or-later
"""
Pipeline orchestrator for the chiplet export plugin.

Wires up writers + discovery + runner into a single end-to-end export
call that the dialog drives. Keeping the orchestration in a dedicated
module (no wx, no pcbnew at module load) makes the CLI-args helper
unit-testable on host Python.

Public surface:
  - ExportOptions:  user-visible toggles collected by the dialog.
  - ExportResult:   outcome with exit code, cancelled flag, output paths.
  - build_cli_args: pure function, stdlib-only, returns the argv passed
                    to hyp_to_gds.py. The whole test_orchestrator.py
                    suite covers this function.
  - run_export:     end-to-end orchestrator. Imports pcbnew / writers
                    lazily so the test suite never reaches them.
"""

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


# Default ADK interposer adapter used when neither the dialog nor the
# .chiplet file declare one. Matches the only adapter shipped today
# (adk/pdk_adapters/interposer/intm4tm2.drc).
DEFAULT_INTERPOSER_ADAPTER = "intm4tm2"

# Interconnect axis adapter. Empty = no interconnect axis (behaviour identical
# to before this axis existed). Deliberately NOT defaulted to a cu-pillar
# adapter: a legacy .chiplet with no `interconnect:` block must never silently
# gain IXN pitch/spacing checks.
DEFAULT_INTERCONNECT_ADAPTER = ""

# Ecosystem dependency roots the export pipeline consumes. The marker subpath
# validates a candidate root (same shape as hyp_to_gds._PATH_VAR_MARKERS); the
# walk tries each candidate directory name (canonical ecosystem name first,
# then the upstream repository name so default GitHub clones resolve too). The
# dialog surfaces each root as a pre-filled, overridable picker; an override is
# handed to the worker subprocesses through the corresponding environment
# variable -- explicit selection IS the convention's env leg, so swapping in
# another PDK checkout (a vendor fork, a release tag) needs no code change.
DEPENDENCY_ROOT_MARKERS = {
    "INTERPOSER_PDK_ROOT": (("interposer", "OpenIntM4TM2"),
                            ("libs.tech", "klayout")),
    "INTERCONNECT_PDK_ROOT": (("interconnect_pdk",
                               "IHP-Interconnect-IntM4TM2"), ("manifest",)),
    "ADK_ROOT": (("adk", "ADK"), ("klayout", "drc")),
}


def discover_dependency_root(var_name: str, board=None,
                             start: Optional[str] = None) -> str:
    """Resolve an ecosystem dependency root for display in the dialog.

    Chain (first marker-validated hit wins): environment variable ->
    KiCad project text variable -> sibling-checkout walk up from this
    file. Returns "" when nothing resolves; the dialog then shows an
    empty picker and the run fails loud at the consuming step.
    """
    from .discovery import _lookup_text_var

    dirnames, marker = DEPENDENCY_ROOT_MARKERS[var_name]

    def _valid(root) -> bool:
        try:
            return bool(root) and Path(root).joinpath(*marker).exists()
        except OSError:
            return False

    env = os.environ.get(var_name, "")
    if _valid(env):
        return str(Path(env).absolute())
    text = _lookup_text_var(board, var_name) or ""
    if _valid(text):
        return str(Path(text).absolute())
    here = Path(start or __file__).resolve()
    for base in here.parents:
        for dirname in dirnames:
            cand = base / dirname
            if _valid(cand):
                return str(cand)
    return ""


@dataclass
class ExportOptions:
    """User-visible options collected by the dialog."""

    output_dir: str = ""
    emit_chiplet: bool = True
    emit_interposer_gds: bool = True
    emit_complete_gds: bool = False
    keep_intermediate_hyp: bool = False
    # Viewer-only: paint each chiplet boundary onto an annotation GDS layer
    # (no DRC rule reads it). Drives hyp_to_gds --annotate-boundaries. Off by
    # default so the production GDS carries no synthetic geometry.
    annotate_boundaries: bool = False
    top_cell: str = "INTERPOSER"
    connection_type: str = ""          # empty = no --connection-type
    lyp_override: str = ""             # empty = hyp_to_gds default (built-in IHP)
    io_pads_json: str = ""             # empty = auto-extract from board
    cupillar_gds: str = ""             # non-empty = pre-generated GDS override
    worker_python_override: str = ""   # empty = use discovery chain
    # Assembly DRC against the ADK deck. Runs after hyp_to_gds when a
    # complete.gds was emitted; can be disabled when the user only wants
    # the GDS output.
    emit_assembly_drc: bool = True
    # Explicit ecosystem dependency roots (PDK selection). Empty = the
    # consuming step resolves via the discovery chain (env -> project text
    # var -> sibling walk). A non-empty value is exported to the worker
    # subprocesses as the corresponding environment variable.
    interposer_pdk_root: str = ""
    interconnect_pdk_root: str = ""
    adk_root: str = ""
    # Explicit adapter override. Empty = read from .chiplet file's
    # `interposer.adapter` field, with DEFAULT_INTERPOSER_ADAPTER as the
    # final fallback.
    interposer_adapter: str = ""
    # Interconnect axis adapter override. Empty = read from the .chiplet file's
    # `interconnect.adapter` field; absent there too = no interconnect axis.
    interconnect_adapter: str = ""
    # {ref: pin_list_json} auto-extracted die bumps; drives Cu-pillar
    # generation when connection_type names a cupillar stack.
    pad_locations: Dict[str, str] = field(default_factory=dict)
    # {ref: method id} per-die connection overrides. A die not listed uses
    # connection_type. Empty = auto-read from the board's per-footprint
    # CONNECTION fields (run_export), so the board stays the source of
    # truth for per-die method selection.
    die_connections: Dict[str, str] = field(default_factory=dict)


@dataclass
class ExportResult:
    """Outcome of a single ``run_export`` call."""

    exit_code: int = -1
    cancelled: bool = False
    error: str = ""
    hyp_path: str = ""
    chiplet_path: str = ""
    interposer_gds_path: str = ""
    complete_gds_path: str = ""
    cupillar_drc_path: str = ""
    # ADK assembly DRC outcome. ``exit_code`` of -1 means the deck did
    # not run (disabled, no complete.gds, or runner not found).
    assembly_drc_exit_code: int = -1
    assembly_drc_report_path: str = ""


def describe_assembly_drc(result: ExportResult) -> str:
    """One-line verdict for the assembly DRC step of an export.

    ``ExportResult.exit_code`` deliberately does NOT fold in the DRC
    verdict: the exported artifacts are valid regardless of design-rule
    violations. Consumers must surface this string (or check
    ``assembly_drc_exit_code`` themselves) so a FAILED deck is never
    mistaken for a green run.
    """
    if result.assembly_drc_exit_code == -1:
        return "assembly DRC: NOT RUN"
    if result.assembly_drc_exit_code == 0:
        return "assembly DRC: PASSED"
    return "assembly DRC: FAILED (exit %d)" % result.assembly_drc_exit_code


def _read_adapter_from_block(chiplet_path: str, block_name: str,
                             default: str) -> str:
    """Read ``<block_name>:\\n  adapter: <value>`` from a ``.chiplet`` YAML.

    A minimal hand-rolled parser (KiCad's bundled Python lacks PyYAML). The
    block header counts only at column 0; quoted (single/double) and unquoted
    values are accepted; ``#`` comments (line and inline) are stripped. Returns
    ``default`` when the file is missing/unreadable or the block/field is absent
    or empty.
    """
    try:
        with open(chiplet_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return default

    in_block = False
    for raw in lines:
        line = raw.rstrip("\n")
        if "#" in line:
            line = line[: line.index("#")].rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if indent == 0:
            in_block = (stripped == "%s:" % block_name)
            continue
        if not in_block:
            continue
        if stripped.startswith("adapter:"):
            value = stripped[len("adapter:"):].strip()
            if (len(value) >= 2
                    and value[0] in ("'", '"')
                    and value[-1] == value[0]):
                value = value[1:-1]
            return value or default
    return default


def load_interposer_adapter(chiplet_path: str) -> str:
    """Return the interposer adapter declared in a ``.chiplet`` YAML file.

    Reads the top-level ``interposer.adapter`` field. Falls back to
    :data:`DEFAULT_INTERPOSER_ADAPTER` when the file is missing, unreadable,
    or does not declare the field::

        interposer:
          adapter: "intm4tm2"
    """
    return _read_adapter_from_block(
        chiplet_path, "interposer", DEFAULT_INTERPOSER_ADAPTER)


def load_interconnect_adapter(chiplet_path: str) -> str:
    """Return the interconnect adapter declared in a ``.chiplet`` YAML file.

    Reads the top-level ``interconnect.adapter`` field. Returns
    :data:`DEFAULT_INTERCONNECT_ADAPTER` (``""`` -- no interconnect axis) when
    the file/block/field is absent, so a legacy design never silently gains the
    IXN pitch/spacing checks::

        interconnect:
          adapter: "ihp_cupillar"
    """
    return _read_adapter_from_block(
        chiplet_path, "interconnect", DEFAULT_INTERCONNECT_ADAPTER)


def _read_component_connections(chiplet_path: str) -> List[tuple]:
    """``[(component_id, connection_id), ...]`` from a ``.chiplet`` YAML.

    Minimal hand-rolled parser (KiCad's bundled Python lacks PyYAML),
    sibling of :func:`_read_adapter_from_block`. Reads the top-level
    ``components:`` list and collects each item's ``id`` and ``connection``
    fields; items without a connection are skipped. Handles both list
    styles the suite emits (items at column 0 or indented under the key);
    nested lists (``io_pads:``) are excluded by indent.
    """
    try:
        with open(chiplet_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []

    def _value(text):
        value = text.strip()
        if (len(value) >= 2 and value[0] in ("'", '"')
                and value[-1] == value[0]):
            value = value[1:-1]
        return value

    entries = []
    state = {"id": "", "conn": ""}

    def _flush():
        if state["id"] and state["conn"]:
            entries.append((state["id"], state["conn"]))
        state["id"] = ""
        state["conn"] = ""

    in_components = False
    item_indent = None
    for raw in lines:
        line = raw.rstrip("\n")
        if "#" in line:
            line = line[: line.index("#")].rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if indent == 0 and not stripped.startswith("- "):
            _flush()
            in_components = (stripped == "components:")
            item_indent = None
            continue
        if not in_components:
            continue
        if stripped.startswith("- "):
            if item_indent is None:
                item_indent = indent
            if indent == item_indent:
                _flush()
                rest = stripped[2:].strip()
                if rest.startswith("id:"):
                    state["id"] = _value(rest[len("id:"):])
            continue
        if item_indent is None or indent != item_indent + 2:
            continue  # nested list field (io_pads entries etc.)
        if stripped.startswith("connection:"):
            state["conn"] = _value(stripped[len("connection:"):])
        elif stripped.startswith("id:") and not state["id"]:
            state["id"] = _value(stripped[len("id:"):])
    _flush()
    return entries


def derive_interconnect_methods(chiplet_path: str,
                                interconnect_root: str = "",
                                board=None) -> Dict[str, dict]:
    """Per-method IXN parameters derived from the ``.chiplet`` + manifest.

    The per-die ``connection:`` ids are the interconnect method ids for
    manifest-era assemblies; the interconnect PDK manifest is the single
    source of truth for each method's pitch rules. Returns
    ``{method_id: {"dies": [ids...], "IXN_spacing": f, "IXN_pitch": f,
    "IXN_pad_size": f}}`` for the methods the manifest knows; connection
    ids the manifest does not know are skipped (custom/legacy stacks --
    the assembly-global adapter covers them). Empty dict when nothing
    derivable (no connections, no manifest): the DRC then runs exactly as
    before this refinement existed.
    """
    import json

    connections = _read_component_connections(chiplet_path)
    if not connections:
        return {}
    root = interconnect_root or discover_dependency_root(
        "INTERCONNECT_PDK_ROOT", board=board)
    if not root:
        return {}
    manifest_path = Path(root) / "manifest" / "interconnect_methods.json"
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            methods_db = json.load(fh).get("methods", {})
    except Exception:
        return {}

    derived: Dict[str, dict] = {}
    for component_id, connection in connections:
        method = methods_db.get(connection)
        if not method:
            continue
        try:
            entry = derived.setdefault(connection, {
                "dies": [],
                "IXN_spacing": float(method["pitch_rules"]["IXN_spacing"]),
                "IXN_pitch": float(method["pitch_rules"]["IXN_pitch"]),
                "IXN_pad_size": float(method["fab_params"]["passiv_opening_um"]),
            })
        except (KeyError, TypeError, ValueError):
            continue  # malformed manifest entry: leave it to the adapter
        if component_id not in entry["dies"]:
            entry["dies"].append(component_id)
    return derived


def write_ixn_methods_sidecar(methods: Dict[str, dict], gds_path: str,
                              chiplet_path: str = "") -> str:
    """Write ``<gds-stem>.ixn_methods.json`` next to the GDS.

    Sibling of the producer's ``<gds-stem>.boundaries.json``: the ADK deck
    consumes both to scope the IXN checks per method. Returns the sidecar
    path, or "" when ``methods`` is empty (nothing written).
    """
    import json

    if not methods:
        return ""
    sidecar = os.path.join(
        os.path.dirname(gds_path) or ".",
        Path(gds_path).stem + ".ixn_methods.json",
    )
    payload = {
        "schema": "adk-ixn-methods",
        "version": "1.0.0",
        "generator": "chiplet_kicad_plugin/orchestrator",
        "assembly_gds": os.path.basename(gds_path),
        "source_chiplet": os.path.basename(chiplet_path) if chiplet_path else "",
        "methods": {mid: methods[mid] for mid in sorted(methods)},
    }
    with open(sidecar, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
        fh.write("\n")
    return sidecar


def available_connection_types(interconnect_root: str = "",
                               board=None) -> List[str]:
    """Connection-type choices for the export dialog dropdown.

    Always starts with "" (no --connection-type). Sourced from the manifest of
    the interconnect PDK at ``interconnect_root`` (or the discovered root when
    empty): every method in declaration order, including any vendor method --
    pointing the dialog at another PDK checkout repopulates the choices with
    that vendor's catalogue. Falls back to the built-in IHP set when no
    manifest is readable, so the dialog still opens.

    Reads the manifest JSON directly (stdlib only): no import of the PDK's
    reader module and no sys.path mutation inside KiCad's bundled Python.
    """
    import json

    root = interconnect_root or discover_dependency_root(
        "INTERCONNECT_PDK_ROOT", board=board)
    if root:
        manifest = Path(root) / "manifest" / "interconnect_methods.json"
        try:
            with open(manifest, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            methods = list(data["methods"].keys())
            if methods:
                return [""] + methods
        except Exception:
            pass
    return ["", "cupillar_opt1", "cupillar_opt2", "cupillar_opt3", "sbump_sac305"]


def build_worker_env(options: ExportOptions,
                     base_env: Optional[Dict[str, str]] = None
                     ) -> Optional[Dict[str, str]]:
    """Subprocess environment with the dialog's PDK-root overrides applied.

    Returns None (inherit the parent environment untouched) when no root
    override is set, keeping the default behaviour identical to before the
    explicit roots existed. Never mutates ``os.environ``.
    """
    overrides = {
        "INTERPOSER_PDK_ROOT": options.interposer_pdk_root,
        "INTERCONNECT_PDK_ROOT": options.interconnect_pdk_root,
        "ADK_ROOT": options.adk_root,
    }
    set_vars = {name: value for name, value in overrides.items() if value}
    if not set_vars:
        return None
    env = dict(base_env if base_env is not None else os.environ)
    env.update(set_vars)
    return env


def build_adk_drc_argv(adk_runner_path: str,
                       gds_path: str,
                       interposer_adapter: str,
                       report_path: Optional[str] = None,
                       run_dir: Optional[str] = None,
                       topcell: Optional[str] = None,
                       threads: Optional[int] = None,
                       run_mode: Optional[str] = None,
                       interconnect_adapter: str = "",
                       interconnect_methods: str = "") -> List[str]:
    """Construct argv for the ADK ``run_drc.py`` subprocess.

    The returned list begins with ``adk_runner_path`` and the required
    ``--path`` / ``--interposer-adapter`` flags; the remaining flags are
    appended only when the caller provides a value. Output paths are
    passed through unchanged (callers are expected to pre-resolve them).
    """
    args: List[str] = [
        adk_runner_path,
        "--path", gds_path,
        "--interposer-adapter", interposer_adapter,
    ]
    if report_path:
        args += ["--report", report_path]
    if run_dir:
        args += ["--run_dir", run_dir]
    if topcell:
        args += ["--topcell", topcell]
    if threads is not None:
        args += ["--threads", str(threads)]
    if run_mode:
        args += ["--run_mode", run_mode]
    if interconnect_adapter:
        args += ["--interconnect-adapter", interconnect_adapter]
    if interconnect_methods:
        args += ["--interconnect-methods", interconnect_methods]
    return args


def build_cli_args(hyp_to_gds_path: str,
                   hyp_path: str,
                   board_name: str,
                   options: ExportOptions) -> List[str]:
    """Construct argv for the hyp_to_gds.py subprocess.

    The list starts with the script path and the positional hyp input,
    then appends flags driven by `options`. Output paths are absolute
    (rooted at ``options.output_dir``).
    """
    out_dir = options.output_dir
    args: List[str] = [hyp_to_gds_path, hyp_path]

    if options.emit_interposer_gds:
        args += ["-o", os.path.join(out_dir, "%s_interposer.gds" % board_name)]

    if options.top_cell:
        args += ["-c", options.top_cell]

    if options.lyp_override:
        args += ["-l", options.lyp_override]

    if options.emit_complete_gds:
        args += [
            "--with-chiplets",
            "--complete-output",
            os.path.join(out_dir, "%s_complete.gds" % board_name),
        ]

    # Viewer-only boundary annotation (no DRC rule reads the layer). Harmless
    # on the interposer GDS -- no chiplets means nothing is painted.
    if options.annotate_boundaries:
        args += ["--annotate-boundaries"]

    if options.emit_chiplet:
        args += [
            "--update-chiplet-file",
            os.path.join(out_dir, "%s.chiplet" % board_name),
        ]

    if options.connection_type:
        args += ["--connection-type", options.connection_type]

    if options.die_connections:
        spec = ",".join("%s=%s" % (ref, m)
                        for ref, m in sorted(options.die_connections.items()))
        args += ["--die-connections", spec]

    if options.io_pads_json:
        args += ["--io-pads", options.io_pads_json]

    if options.cupillar_gds:
        args += ["--cupillar-gds", options.cupillar_gds]

    if options.pad_locations:
        spec = ",".join("%s=%s" % (ref, p)
                        for ref, p in sorted(options.pad_locations.items()))
        args += ["--pad-locations", spec]

    return args


def run_export(board, options, plugin_dir,
               on_log=None, cancel_event=None) -> ExportResult:
    """End-to-end export driven by the dialog's Run button.

    Sequence:
      1. Resolve worker python and hyp_to_gds.py (early failure).
      2. Create a workspace tmpdir.
      3. Write Hyperlynx and intermediate .chiplet via the Python ports.
      4. Stage the intermediate .chiplet into ``options.output_dir``
         so ``--update-chiplet-file`` can rewrite it in place.
      5. Invoke hyp_to_gds.py via the async runner; stream log lines
         to ``on_log``.
      6. Optionally copy the intermediate .hyp to the output directory.
      7. Always remove the tmpdir.

    Returns:
        ExportResult.  If ``error`` is non-empty the call failed before
        any subprocess ran and ``exit_code`` is -1.
    """
    from .discovery import (
        find_worker_python, find_hyp_to_gds, find_adk_drc_runner,
        WorkerPythonNotFoundError, HypToGdsNotFoundError,
        AdkRunnerNotFoundError,
    )
    import dataclasses

    from .runner import run_async
    from ..writers.chiplet_writer import (
        write_chiplet, write_io_pads_json, write_die_pin_lists,
        read_die_connections,
    )
    from ..writers.connection_stacks import validate_interconnect_ids
    from ..writers.hyperlynx_writer import write_hyperlynx

    def _log(line):
        if on_log is not None:
            try:
                on_log(line)
            except Exception:
                pass

    if not options.output_dir:
        return ExportResult(error="Output directory is empty.")
    try:
        Path(options.output_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return ExportResult(error="Cannot create output directory: %s" % exc)

    board_file = ""
    try:
        board_file = board.GetFileName() or ""
    except Exception:
        pass
    board_name = Path(board_file).stem or "board"

    try:
        worker_py = (options.worker_python_override
                     or find_worker_python(plugin_dir, board=board))
    except WorkerPythonNotFoundError as exc:
        return ExportResult(error=str(exc))

    try:
        hyp_to_gds = find_hyp_to_gds(plugin_dir)
    except HypToGdsNotFoundError as exc:
        return ExportResult(error=str(exc))

    tmpdir = tempfile.mkdtemp(prefix="chiplet_export_")
    _log("Workspace: %s" % tmpdir)
    try:
        hyp_path = os.path.join(tmpdir, "%s.hyp" % board_name)
        chiplet_intermediate = os.path.join(tmpdir, "%s.chiplet" % board_name)

        _log("Writing Hyperlynx ...")
        try:
            hyp_ok = write_hyperlynx(board, hyp_path)
        except Exception as exc:
            import traceback
            return ExportResult(
                error="Hyperlynx writer crashed: %s\n%s"
                      % (exc, traceback.format_exc()),
            )
        if not hyp_ok:
            return ExportResult(
                error=(
                    "Hyperlynx writer aborted (most commonly: the "
                    "board has no closed Edge.Cuts outline). Add a "
                    "board outline, then retry."
                ),
            )
        _log("Writing intermediate .chiplet ...")
        try:
            chiplet_ok = write_chiplet(board, chiplet_intermediate)
        except Exception as exc:
            import traceback
            return ExportResult(
                error="Chiplet writer crashed: %s\n%s"
                      % (exc, traceback.format_exc()),
            )
        if not chiplet_ok:
            return ExportResult(
                error="Chiplet writer aborted (could not open the "
                      "intermediate .chiplet for writing).",
            )

        chiplet_final = os.path.join(options.output_dir,
                                     "%s.chiplet" % board_name)
        if options.emit_chiplet:
            shutil.copy2(chiplet_intermediate, chiplet_final)

        # Auto-extract io_pads from the board so hyp_to_gds renders the pad
        # geometry (and the interposer GDS bbox includes them). A non-empty
        # options.io_pads_json acts as an explicit override.
        effective_io_pads = options.io_pads_json
        if not effective_io_pads:
            io_pads_auto = os.path.join(tmpdir, "%s_io_pads.json" % board_name)
            try:
                n_io = write_io_pads_json(board, io_pads_auto)
            except Exception as exc:
                n_io = 0
                _log("Warning: io_pads auto-extraction failed: %s" % exc)
            if n_io:
                effective_io_pads = io_pads_auto
                _log("Auto-extracted %d io_pad(s) from board" % n_io)

        # Auto-extract die footprint pads so the Cu-pillar generator places
        # DRC-validated pillars under each flip-chip die (acts only when
        # connection_type names a cupillar stack). A user-supplied
        # cupillar_gds is a pre-generated override and disables auto-extract.
        effective_pad_locs = options.pad_locations
        if not effective_pad_locs and not options.cupillar_gds:
            try:
                effective_pad_locs = write_die_pin_lists(board, tmpdir)
            except Exception as exc:
                effective_pad_locs = {}
                _log("Warning: die pad extraction failed: %s" % exc)
            if effective_pad_locs:
                _log("Auto-extracted die bumps for cu-pillars: %s"
                     % ", ".join(sorted(effective_pad_locs)))

        # Per-die connection methods: an explicit options map wins;
        # otherwise the board's per-footprint CONNECTION fields are the
        # source of truth. Dies without an entry use connection_type.
        effective_die_conns = options.die_connections
        if not effective_die_conns:
            try:
                effective_die_conns = read_die_connections(board)
            except Exception as exc:
                effective_die_conns = {}
                _log("Warning: per-die connection read failed: %s" % exc)
            if effective_die_conns:
                _log("Per-die connections from board fields: %s"
                     % ", ".join("%s=%s" % (r, m) for r, m
                                 in sorted(effective_die_conns.items())))

        # Unknown per-die method ids fail the export here (manifest is the
        # source of truth) instead of degrading later in the worker or DRC.
        validate_interconnect_ids(die_methods=effective_die_conns.values())

        effective_options = dataclasses.replace(
            options, io_pads_json=effective_io_pads,
            pad_locations=effective_pad_locs,
            die_connections=effective_die_conns)
        cli = build_cli_args(hyp_to_gds, hyp_path, board_name, effective_options)
        command = [worker_py] + cli

        # PDK-root overrides travel as environment variables (the discovery
        # convention's explicit-selection leg). Logged so the run records
        # which checkouts produced the artifacts.
        worker_env = build_worker_env(options)
        for var, value in (("INTERPOSER_PDK_ROOT", options.interposer_pdk_root),
                           ("INTERCONNECT_PDK_ROOT", options.interconnect_pdk_root),
                           ("ADK_ROOT", options.adk_root)):
            if value:
                _log("Using %s=%s" % (var, value))
        _log("$ " + " ".join(command))

        run = run_async(
            command,
            on_stdout=_log,
            on_stderr=lambda s: _log("[stderr] " + s),
            cancel_event=cancel_event,
            env=worker_env,
        )

        hyp_kept = ""
        if options.keep_intermediate_hyp:
            hyp_kept = os.path.join(options.output_dir, "%s.hyp" % board_name)
            try:
                shutil.copy2(hyp_path, hyp_kept)
                _log("Kept intermediate .hyp at %s" % hyp_kept)
            except OSError as exc:
                _log("Warning: could not copy intermediate .hyp: %s" % exc)
                hyp_kept = ""

        # The worker writes <board>_cupillar_drc.json next to the interposer
        # GDS when a cupillar stack drives pillar generation. Surface it only
        # if it was actually produced this run.
        drc_report = os.path.join(options.output_dir,
                                  "%s_cupillar_drc.json" % board_name)
        if not os.path.exists(drc_report):
            drc_report = ""

        # ADK assembly DRC over the complete.gds. The chiplet boundaries come
        # from the <complete>.boundaries.json manifest that hyp_to_gds wrote
        # next to the GDS (auto-discovered by run_drc.py); they are not a GDS
        # layer. Runs only when there is a complete.gds to check and the user
        # did not opt out via emit_assembly_drc=False.
        assembly_drc_exit = -1
        assembly_drc_report = ""
        complete_gds_abs = os.path.join(
            options.output_dir, "%s_complete.gds" % board_name,
        )
        should_run_drc = (
            options.emit_complete_gds
            and options.emit_assembly_drc
            and run.exit_code == 0
            and not run.cancelled
            and os.path.exists(complete_gds_abs)
        )
        if should_run_drc:
            try:
                adk_runner = find_adk_drc_runner(
                    plugin_dir, board=board, root_override=options.adk_root)
            except AdkRunnerNotFoundError as exc:
                _log("Assembly DRC skipped: %s" % exc)
                adk_runner = ""
            if adk_runner:
                effective_adapter = (
                    options.interposer_adapter
                    or load_interposer_adapter(chiplet_final)
                )
                effective_interconnect = (
                    options.interconnect_adapter
                    or load_interconnect_adapter(chiplet_final)
                )
                # Per-method IXN refinement: derive {method -> dies} from the
                # .chiplet's per-die connections + the interconnect PDK
                # manifest, written as a sidecar next to the complete GDS
                # (sibling of the boundaries manifest). Best-effort: any
                # failure leaves the assembly-global adapter behavior.
                ixn_methods_sidecar = ""
                try:
                    derived_methods = derive_interconnect_methods(
                        chiplet_final,
                        interconnect_root=options.interconnect_pdk_root,
                        board=board,
                    )
                    ixn_methods_sidecar = write_ixn_methods_sidecar(
                        derived_methods, complete_gds_abs, chiplet_final,
                    )
                    if ixn_methods_sidecar:
                        _log("Interconnect methods sidecar: %s (%s)" % (
                            ixn_methods_sidecar,
                            ", ".join(sorted(derived_methods)),
                        ))
                except Exception as exc:
                    _log("Warning: per-method interconnect derivation "
                         "failed: %s" % exc)
                drc_run_dir = os.path.join(
                    options.output_dir, "assembly_drc",
                )
                assembly_drc_report_target = os.path.join(
                    options.output_dir,
                    "%s_assembly_drc.lyrdb" % board_name,
                )
                adk_cli = build_adk_drc_argv(
                    adk_runner,
                    gds_path=complete_gds_abs,
                    interposer_adapter=effective_adapter,
                    report_path=assembly_drc_report_target,
                    run_dir=drc_run_dir,
                    topcell=options.top_cell or None,
                    interconnect_adapter=effective_interconnect,
                    interconnect_methods=ixn_methods_sidecar,
                )
                adk_command = [worker_py] + adk_cli
                _log("$ " + " ".join(adk_command))
                adk_run = run_async(
                    adk_command,
                    on_stdout=_log,
                    on_stderr=lambda s: _log("[stderr] " + s),
                    cancel_event=cancel_event,
                    env=worker_env,
                )
                assembly_drc_exit = adk_run.exit_code
                if os.path.exists(assembly_drc_report_target):
                    assembly_drc_report = assembly_drc_report_target

        return ExportResult(
            exit_code=run.exit_code,
            cancelled=run.cancelled,
            hyp_path=hyp_kept,
            chiplet_path=(chiplet_final if options.emit_chiplet else ""),
            interposer_gds_path=(
                os.path.join(options.output_dir,
                             "%s_interposer.gds" % board_name)
                if options.emit_interposer_gds else ""
            ),
            complete_gds_path=(
                complete_gds_abs if options.emit_complete_gds else ""
            ),
            cupillar_drc_path=drc_report,
            assembly_drc_exit_code=assembly_drc_exit,
            assembly_drc_report_path=assembly_drc_report,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
