# SPDX-License-Identifier: GPL-3.0-or-later
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

import datetime
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


def discover_interposer_lyp(board=None, start: Optional[str] = None) -> str:
    """Default interposer layer-properties file for the dialog's LYP field.

    Chain (first existing file wins): INTERPOSER_LYP environment variable
    -> INTERPOSER_LYP project text variable -> canonical
    ``libs.tech/klayout/tech/intm4tm2.lyp`` under the discovered interposer
    PDK root. The .lyp belongs to the interposer PDK, not the plugin, so
    there is no bundled fallback: when nothing resolves this returns ""
    (the dialog leaves the picker empty for the user, and the worker errors
    pointing at INTERPOSER_PDK_ROOT). Mirrors the worker's own default
    (hyp_to_gds._find_default_lyp).
    """
    from .discovery import _lookup_text_var

    def _existing(path) -> str:
        try:
            if path and Path(path).is_file():
                return str(Path(path).absolute())
        except OSError:
            pass
        return ""

    found = _existing(os.environ.get("INTERPOSER_LYP", ""))
    if found:
        return found
    found = _existing(_lookup_text_var(board, "INTERPOSER_LYP") or "")
    if found:
        return found
    root = discover_dependency_root("INTERPOSER_PDK_ROOT", board, start)
    if root:
        found = _existing(Path(root).joinpath(
            "libs.tech", "klayout", "tech", "intm4tm2.lyp"))
        if found:
            return found
    return ""


@dataclass
class ExportOptions:
    """User-visible options collected by the dialog."""

    output_dir: str = ""
    # The .chiplet and the interposer GDS are the pipeline's product, not
    # options: the .chiplet is unusable until --update-chiplet-file rewrites
    # it into the canonical frame, and its `layout:` field points at the
    # interposer GDS. hyp_to_gds writes that GDS on every run regardless, so
    # a toggle could only ever have thrown the result away. Neither has a
    # dialog control; emit_chiplet survives as a headless escape hatch.
    emit_chiplet: bool = True
    # Override the H-A clobber guard's hand-edit tripwire. When the canonical
    # .chiplet's exporter-owned content was hand-edited outside KiCad since the
    # last export, the re-export aborts (the edit would be silently regenerated
    # away); set force to overwrite it deliberately. Foreign blocks (flow:,
    # netlist:) are always preserved regardless of this flag. Headless escape
    # hatch, like emit_chiplet; no dialog control.
    force: bool = False
    emit_complete_gds: bool = False
    # Viewer-only: paint each chiplet boundary onto an annotation GDS layer
    # (no DRC rule reads it). Drives hyp_to_gds --annotate-boundaries. Off by
    # default so the production GDS carries no synthetic geometry.
    annotate_boundaries: bool = False
    top_cell: str = "INTERPOSER"
    connection_type: str = ""          # empty = no --connection-type
    lyp_override: str = ""             # empty = hyp_to_gds default (built-in IHP)
    io_pads_json: str = ""             # empty = auto-extract from board
    cmim_devices_json: str = ""        # empty = auto-extract from board
    # No-fill (keep-out) regions authored in KiCad. Empty = auto-extract from
    # the board's NoMetFiller / <metal>.nofill layers. Drives --nofill-regions.
    nofill_regions_json: str = ""
    # Metal density fill via the interposer PDK engine, stamped on the
    # interposer GDS (opt-in; needs the PDK fill work + the klayout binary).
    # fill_mode: "single-pass" (fast, all four metals) or "closure" (M4/M5
    # density-feedback loop, deck-verified, slower).
    insert_metal_fill: bool = False
    fill_mode: str = "single-pass"
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
    # {ref: thickness um} per-die physical thickness. A die not listed
    # keeps the format default of 0.0. Empty = auto-read from the board's
    # per-footprint DIE_THICKNESS_UM fields (run_export), mirroring
    # die_connections: the board stays the source of truth.
    die_thicknesses: Dict[str, float] = field(default_factory=dict)


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
    # Metal-fill read-back. The density report (per-metal coverage/state) lands
    # under reports/; the coarse coverage map (for the KiCad read-back layer)
    # stays next to the interposer GDS under layout/. Both empty when fill was
    # off or skipped.
    fill_density_report_path: str = ""
    fill_coverage_path: str = ""
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


def _strip_inline_comment(line: str) -> str:
    """Strip a trailing ``#`` comment, ignoring ``#`` inside a quoted scalar.

    The .chiplet mini-parsers must not truncate an adapter/connection id that
    legitimately contains ``#`` inside quotes (e.g. ``adapter: "a#b"``).
    """
    in_quote = None
    for i, ch in enumerate(line):
        if in_quote:
            if ch == in_quote:
                in_quote = None
        elif ch in ("'", '"'):
            in_quote = ch
        elif ch == "#":
            return line[:i].rstrip()
    return line


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
        line = _strip_inline_comment(line)
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
        line = _strip_inline_comment(line)
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


def _load_interconnect_methods(interconnect_root: str = "",
                               board=None) -> Dict[str, dict]:
    """``{method_id: entry}`` from the interconnect PDK's method manifest.

    The single manifest reader for this module: the connection-type list, the
    dialog's spec labels and the per-method DRC derivation all go through it,
    so they can never disagree about which methods exist. The root is the
    explicit ``interconnect_root`` or the discovery chain's result.

    Reads the JSON directly (stdlib only): no import of the PDK's reader
    module and no sys.path mutation inside KiCad's bundled Python. Returns
    {} when nothing is readable -- every caller degrades on an empty dict
    rather than propagating a filesystem error into the dialog.
    """
    import json

    root = interconnect_root or discover_dependency_root(
        "INTERCONNECT_PDK_ROOT", board=board)
    if not root:
        return {}
    manifest_path = Path(root) / "manifest" / "interconnect_methods.json"
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            methods = json.load(fh).get("methods", {})
    except Exception:
        return {}
    return methods if isinstance(methods, dict) else {}


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
    connections = _read_component_connections(chiplet_path)
    if not connections:
        return {}
    methods_db = _load_interconnect_methods(interconnect_root, board=board)
    if not methods_db:
        return {}

    derived: Dict[str, dict] = {}
    for component_id, connection in connections:
        method = methods_db.get(connection)
        if not method:
            continue
        try:
            spacing = float(method["pitch_rules"]["IXN_spacing"])
            pitch = float(method["pitch_rules"]["IXN_pitch"])
            pad_size = float(method["fab_params"]["passiv_opening_um"])
        except (KeyError, TypeError, ValueError):
            continue  # malformed manifest entry: leave it to the adapter
        if not (spacing > 0 and pitch > 0 and pad_size > 0):
            # The ixn_methods schema requires exclusiveMinimum 0; a non-positive
            # value would make run_drc reject the whole sidecar. Skip it -- the
            # assembly-global adapter still covers this method.
            continue
        entry = derived.setdefault(connection, {
            "dies": [],
            "IXN_spacing": spacing,
            "IXN_pitch": pitch,
            "IXN_pad_size": pad_size,
        })
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


def _intersect_methods_with_manifest(methods: Dict[str, dict],
                                     gds_path: str) -> Dict[str, dict]:
    """Drop derived dies absent from the GDS's boundary manifest.

    The assembly DRC deck hard-raises when an ixn_methods entry names a die
    instance the ``<gds-stem>.boundaries.json`` manifest does not declare. Keep
    only dies present in that manifest, and drop a method whose dies all
    disappear (the schema requires ``dies`` minItems 1; the assembly-global
    adapter still covers it). When the manifest is missing/unreadable the
    methods are returned unchanged (best-effort, matching prior behaviour).
    """
    import json

    if not methods:
        return methods
    manifest_path = os.path.join(
        os.path.dirname(gds_path) or ".",
        Path(gds_path).stem + ".boundaries.json",
    )
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return methods
    boundaries = data.get("boundaries") if isinstance(data, dict) else None
    if not isinstance(boundaries, list):
        return methods
    instances = {b.get("instance") for b in boundaries
                 if isinstance(b, dict) and b.get("instance")}

    filtered: Dict[str, dict] = {}
    for mid, entry in methods.items():
        dies = [d for d in entry.get("dies", []) if d in instances]
        if dies:
            filtered[mid] = {**entry, "dies": dies}
    return filtered


def available_connection_types(interconnect_root: str = "",
                               board=None) -> List[str]:
    """Connection-type choices for the export dialog dropdown.

    Always starts with "" (no --connection-type). Sourced from the manifest of
    the interconnect PDK at ``interconnect_root`` (or the discovered root when
    empty): every method in declaration order, including any vendor method --
    pointing the dialog at another PDK checkout repopulates the choices with
    that vendor's catalogue. Falls back to the built-in IHP set when no
    manifest is readable, so the dialog still opens.

    Returns bare method ids; :func:`format_connection_label` turns one into
    the string the dialog displays.
    """
    methods = list(_load_interconnect_methods(interconnect_root, board=board))
    if methods:
        return [""] + methods
    return ["", "cupillar_opt1", "cupillar_opt2", "cupillar_opt3", "sbump_sac305"]


def connection_method_specs(interconnect_root: str = "",
                            board=None) -> Dict[str, dict]:
    """Decision-relevant numbers per connection method, for the dialog labels.

    ``{method_id: {"pitch", "spacing", "opening", "diameter", "height",
    "vendor", "description"}}`` -- the fields a designer picks a method by,
    and (for pitch/spacing/opening) the very numbers the assembly DRC will
    check the design against. Each field is independently optional: a
    manifest entry missing one simply omits that key instead of dropping
    the whole method. {} when no manifest is readable, which makes the
    labels degrade to bare ids.
    """
    specs: Dict[str, dict] = {}
    for method_id, entry in _load_interconnect_methods(
            interconnect_root, board=board).items():
        if not isinstance(entry, dict):
            continue
        spec: dict = {}
        pitch_rules = entry.get("pitch_rules") or {}
        fab_params = entry.get("fab_params") or {}
        for key, source, name in (
                ("pitch", pitch_rules, "IXN_pitch"),
                ("spacing", pitch_rules, "IXN_spacing"),
                ("opening", fab_params, "passiv_opening_um"),
                ("diameter", entry, "body_diameter_um"),
        ):
            try:
                spec[key] = float(source[name])
            except (KeyError, TypeError, ValueError):
                pass
        layers = (entry.get("connection_stack") or {}).get("layers")
        if isinstance(layers, list):
            try:
                spec["height"] = sum(float(layer["height"]) for layer in layers)
                spec["layers"] = [
                    (str(layer.get("name", "")), float(layer["height"]))
                    for layer in layers
                ]
            except (KeyError, TypeError, ValueError):
                spec.pop("height", None)
                spec.pop("layers", None)
        for key in ("vendor", "description"):
            value = entry.get(key)
            if value:
                spec[key] = str(value)
        specs[method_id] = spec
    return specs


def _um(value) -> str:
    """Render a micrometer dimension without trailing zeros: 75.0 -> "75"."""
    return "%gum" % value


def format_connection_label(method_id: str, spec: Optional[dict] = None) -> str:
    """Short dropdown label: ``cupillar_opt1 - 75um pitch, 44um dia``.

    The id stays the label's prefix so the GTK dropdown's type-to-select
    still works on method ids. Falls back to the bare id when the manifest
    knows nothing about it -- the built-in fallback list, or a value the
    board carries that this PDK does not declare. Never invents numbers.
    """
    spec = spec or {}
    parts = []
    if "pitch" in spec:
        parts.append("%s pitch" % _um(spec["pitch"]))
    if "diameter" in spec:
        parts.append("%s dia" % _um(spec["diameter"]))
    if not parts:
        return method_id
    return "%s - %s" % (method_id, ", ".join(parts))


def describe_connection_method(method_id: str,
                               spec: Optional[dict] = None) -> str:
    """Full one-line spec sheet for the detail text under the dropdown.

    Everything the manifest knows that bears on the choice: the vendor's
    description, the pitch rules the assembly DRC enforces, the fab opening,
    the body diameter and the stack height broken down by layer.
    """
    spec = spec or {}
    if not spec:
        return method_id
    parts = []
    if "pitch" in spec and "spacing" in spec:
        parts.append("pitch %s / spacing %s"
                     % (_um(spec["pitch"]), _um(spec["spacing"])))
    elif "pitch" in spec:
        parts.append("pitch %s" % _um(spec["pitch"]))
    if "opening" in spec:
        parts.append("%s opening" % _um(spec["opening"]))
    if "diameter" in spec:
        parts.append("%s dia" % _um(spec["diameter"]))
    if "height" in spec:
        height = "%s tall" % _um(spec["height"])
        layers = spec.get("layers") or []
        if len(layers) > 1:
            height += " (%s)" % " + ".join(
                "%s %g" % (name, value) for name, value in layers)
        parts.append(height)
    text = ", ".join(parts)
    if spec.get("description"):
        text = "%s - %s" % (spec["description"], text) if text \
            else spec["description"]
    if spec.get("vendor"):
        text = "%s - %s" % (text, spec["vendor"]) if text else spec["vendor"]
    return text or method_id


# Plausible range for a silicon die body, in micrometers. Outside it the value
# is almost always a unit slip -- 0.75 typed in millimetres, 750000 in
# nanometres -- rather than a real part: ``parse_thickness_um`` accepts any
# positive float and nothing downstream questions the magnitude.
DIE_THICKNESS_PLAUSIBLE_UM = (50.0, 2000.0)


def describe_die_thickness_gaps(die_refs: List[str],
                                thicknesses: Dict[str, float]) -> List[str]:
    """Warning lines about per-die thickness, for the export log.

    Two things worth saying out loud, because neither is visible in the
    exported file:

      * A die with no ``DIE_THICKNESS_UM`` ships ``dimensions.thickness: 0.0``.
        The ADK's 3Dblox export rejects that outright, while Chiplet Studio
        and the thermal stackup each silently substitute a different default
        -- so the same assembly gets three different die bodies.
      * A thickness far outside the plausible range is a unit slip, not a
        part.

    Pure (no pcbnew) so the export path and the tests share one rule.
    """
    lines = []
    missing = [ref for ref in die_refs if ref not in thicknesses]
    if missing:
        lines.append(
            "WARNING: no DIE_THICKNESS_UM for %s -- these dies export with "
            "dimensions.thickness=0.0, which the ADK 3Dblox export rejects "
            "and which Chiplet Studio silently renders as a 200 um body. "
            "Set the die thickness (750 um for a standard SG13G2 die) in the "
            "export dialog or the footprint field."
            % ", ".join(missing))
    low, high = DIE_THICKNESS_PLAUSIBLE_UM
    odd = ["%s=%g" % (ref, thicknesses[ref]) for ref in sorted(thicknesses)
           if not (low <= thicknesses[ref] <= high)]
    if odd:
        lines.append(
            "WARNING: implausible die thickness %s -- the field is in "
            "micrometers (a 0.75 mm die is 750). Exporting as given."
            % ", ".join(odd))
    return lines


# An interposer's physical body (dimensions.thickness, from the KiCad board
# stackup) plausibly sits in this range (um): thinned Si interposers are a few
# hundred um, a full wafer ~750. The KiCad default FR-4 board (~1.6 mm) is the
# classic "no real interposer stackup" tell.
INTERPOSER_BODY_PLAUSIBLE_UM = (50.0, 1000.0)


def describe_interposer_body_default(thickness_um: Optional[float]) -> List[str]:
    """Warning lines about the interposer's physical body thickness.

    dimensions.thickness on the interposer is the physical silicon body, taken
    verbatim from the KiCad board stackup (GetBoardThickness). It is decoupled
    from the die-attachment surface (attachment_surface_z), so an implausible
    body no longer corrupts die z -- but a value near the ~1.6 mm FR-4 default
    means the board ships a placeholder stackup, not a real interposer one.

    Pure (no pcbnew) so the export path and the tests share one rule.
    """
    if thickness_um is None:
        return []
    low, high = INTERPOSER_BODY_PLAUSIBLE_UM
    if low <= thickness_um <= high:
        return []
    return [
        "WARNING: interposer physical body dimensions.thickness=%g um is "
        "outside the plausible %g-%g um range -- it comes from the KiCad board "
        "stackup, so a value near the 1.6 mm FR-4 default means the board has "
        "no real interposer stackup. The die-attachment surface "
        "(attachment_surface_z) is separate, so die z is unaffected; set a "
        "realistic interposer thickness in Board Setup to model the body."
        % (thickness_um, low, high)
    ]


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


def _reject_delimiter_chars(mapping: Dict[str, str], what: str,
                            check_values: bool = True) -> None:
    """Raise if a key (or value, when ``check_values``) contains ',' or '=',
    which the REF=VALUE,... CLI encoding (split on ',' then '=') cannot
    represent unambiguously.

    ``check_values`` is False for pad_locations, whose values are
    plugin-generated file paths rooted at $TMPDIR that may legitimately
    contain those characters; only the ref keys are validated there.
    """
    for key, value in mapping.items():
        tokens = [(key, "ref")]
        if check_values:
            tokens.append((str(value), "value"))
        for token, role in tokens:
            if "," in token or "=" in token:
                raise ValueError(
                    "%s %s %r contains ',' or '=', which would corrupt the "
                    "REF=VALUE,... encoding passed to hyp_to_gds."
                    % (what, role, token))


# The GDS layouts and their derived sidecars (the .boundaries.json the worker
# writes next to each GDS, the .ixn_methods.json the assembly DRC reads, and the
# cu-pillar DRC json the worker drops next to the interposer GDS) live in a
# `layout/` subdir of the output directory. The .chiplet stays at the output-dir
# root and references the interposer GDS by a `layout/<file>` relative path:
# hyp_to_gds --update-chiplet-file emits it automatically via relative_to(), and
# Chiplet Studio's loader resolves it against the .chiplet's own directory.
# Routing both the writer argv and the DRC input through the same helper keeps
# those two paths identical so they can never drift.
LAYOUT_SUBDIR = "layout"


def layout_dir(output_dir: str) -> str:
    """Subdir of ``output_dir`` holding the GDS layouts and their sidecars."""
    return os.path.join(output_dir, LAYOUT_SUBDIR)


def layout_path(output_dir: str, filename: str) -> str:
    """Path to ``filename`` inside the output dir's ``layout/`` subdir."""
    return os.path.join(layout_dir(output_dir), filename)


def build_cli_args(hyp_to_gds_path: str,
                   hyp_path: str,
                   board_name: str,
                   options: ExportOptions) -> List[str]:
    """Construct argv for the hyp_to_gds.py subprocess.

    The list starts with the script path and the positional hyp input,
    then appends flags driven by `options`. Output paths are absolute: the
    GDS layouts go under ``options.output_dir``'s ``layout/`` subdir, the
    .chiplet stays at the output-dir root.
    """
    out_dir = options.output_dir
    args: List[str] = [hyp_to_gds_path, hyp_path]

    # Always: hyp_to_gds writes the interposer GDS unconditionally, and
    # without -o it lands next to the .hyp -- which lives in the workspace
    # tmpdir this module deletes on the way out, taking the boundary and
    # pillar sidecars with it and leaving the .chiplet's `layout:` pointing
    # at a path that no longer exists.
    args += ["-o", layout_path(out_dir, "%s_interposer.gds" % board_name)]

    if options.top_cell:
        args += ["-c", options.top_cell]

    if options.lyp_override:
        args += ["-l", options.lyp_override]

    if options.emit_complete_gds:
        args += [
            "--with-chiplets",
            "--complete-output",
            layout_path(out_dir, "%s_complete.gds" % board_name),
        ]

    # Viewer-only boundary annotation (no DRC rule reads the layer). Harmless
    # on the interposer GDS -- no chiplets means nothing is painted.
    if options.annotate_boundaries:
        args += ["--annotate-boundaries"]

    # Metal density fill on the interposer GDS (PDK engine). --fill-mode is
    # inert without --insert-metal-fill, but pass it together for a legible argv.
    if options.insert_metal_fill:
        args += ["--insert-metal-fill", "--fill-mode", options.fill_mode]

    # No-fill keep-outs authored in KiCad, carried by sidecar (not the .hyp).
    if options.nofill_regions_json:
        args += ["--nofill-regions", options.nofill_regions_json]

    if options.emit_chiplet:
        args += [
            "--update-chiplet-file",
            os.path.join(out_dir, "%s.chiplet" % board_name),
        ]

    if options.connection_type:
        args += ["--connection-type", options.connection_type]

    if options.die_connections:
        _reject_delimiter_chars(options.die_connections, "die connection")
        spec = ",".join("%s=%s" % (ref, m)
                        for ref, m in sorted(options.die_connections.items()))
        args += ["--die-connections", spec]

    if options.die_thicknesses:
        _reject_delimiter_chars(options.die_thicknesses, "die thickness")
        spec = ",".join("%s=%s" % (ref, repr(float(t)))
                        for ref, t in sorted(options.die_thicknesses.items()))
        args += ["--die-thicknesses", spec]

    if options.io_pads_json:
        args += ["--io-pads", options.io_pads_json]

    if options.cmim_devices_json:
        args += ["--cmim-devices", options.cmim_devices_json]

    if options.cupillar_gds:
        args += ["--cupillar-gds", options.cupillar_gds]

    if options.pad_locations:
        _reject_delimiter_chars(options.pad_locations, "pad location",
                                check_values=False)
        spec = ",".join("%s=%s" % (ref, p)
                        for ref, p in sorted(options.pad_locations.items()))
        args += ["--pad-locations", spec]

    return args


def _open_run_log(output_dir, board_name):
    """Open a timestamped, per-run log file under ``<output_dir>/logs/``.

    Returns ``(file_handle, path)`` on success, or ``(None, "")`` if the
    directory or file cannot be created. The caller tees every export log
    line into the handle so each run leaves a permanent record next to its
    products (the dialog's log widget is cleared on every run). Best-effort
    by contract: a log-file failure must never abort the export.

    Two runs of the same board within the same second never clobber each
    other: the file is opened exclusively and a ``_1``, ``_2`` ... counter
    is appended on collision (the common no-collision case keeps the clean
    ``<timestamp>_<board>.log`` name).
    """
    try:
        logs_dir = os.path.join(output_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base = "%s_%s" % (stamp, board_name)
        for suffix in [""] + ["_%d" % i for i in range(1, 100)]:
            path = os.path.join(logs_dir, "%s%s.log" % (base, suffix))
            try:
                return open(path, "x", encoding="utf-8"), path
            except FileExistsError:
                continue
        # Pathological: 100 same-second collisions. Fall back to a
        # truncating open rather than failing the export's logging.
        path = os.path.join(logs_dir, "%s.log" % base)
        return open(path, "w", encoding="utf-8"), path
    except OSError:
        return None, ""


def _write_outputs_manifest(output_dir, board_name):
    """Write a human-readable MANIFEST.md describing the export products.

    Best-effort: a manifest write failure must never affect the export. The
    .chiplet sits at the output-dir root; the GDS layouts and their sidecars
    live under layout/ (the .chiplet references the interposer GDS by a
    ``layout/<file>`` path, and the assembly DRC discovers each sidecar next
    to its GDS); the DRC reports live under reports/. Each entry is tagged
    present/absent against what this run actually produced.
    """
    def present(rel):
        return os.path.exists(os.path.join(output_dir, rel))

    root = [
        ("%s.chiplet" % board_name,
         "assembly description; open this in Chiplet Studio"),
    ]
    layout = [
        ("layout/%s_interposer.gds" % board_name,
         "interposer layout (referenced by the .chiplet)"),
        ("layout/%s_interposer.boundaries.json" % board_name,
         "chiplet-boundary manifest for the interposer GDS"),
        ("layout/%s_interposer.pillars.json" % board_name,
         "cu-pillar manifest for the interposer GDS (per-pillar geometry "
         "and connection method; present when a cu-pillar stack is used)"),
        ("layout/%s_interposer.fill_coverage.json" % board_name,
         "coarse metal-fill coverage map for the KiCad read-back layer "
         "(present when metal fill is inserted)"),
        ("layout/%s_complete.gds" % board_name,
         "full assembly layout (interposer + dies)"),
        ("layout/%s_complete.boundaries.json" % board_name,
         "chiplet-boundary manifest for the complete GDS"),
        ("layout/%s_complete.pillars.json" % board_name,
         "cu-pillar manifest for the complete GDS"),
        ("layout/%s_complete.ixn_methods.json" % board_name,
         "per-method interconnect scoping sidecar for the assembly DRC"),
    ]
    reports = [
        ("reports/%s_assembly_drc.lyrdb" % board_name,
         "ADK assembly DRC results (open in KLayout)"),
        ("reports/%s_cupillar_drc.json" % board_name,
         "cu-pillar connection DRC summary"),
        ("reports/%s_fill_density.json" % board_name,
         "metal-fill density report (per-metal coverage and deck state; "
         "present when metal fill is inserted)"),
    ]

    def section(items):
        return ["- [%s] `%s` - %s"
                % ("x" if present(rel) else " ", rel, role)
                for rel, role in items]

    lines = ["# Output products", ""]
    lines.append("Generated by the Chiplet Studio export pipeline for "
                 "`%s`." % board_name)
    lines += ["", "## Root", ""]
    lines += section(root)
    lines += ["", "## layout/ (GDS layouts and their sidecars)", ""]
    lines.append("The .chiplet references the interposer GDS by a "
                 "`layout/<file>` path, resolved against the .chiplet's own "
                 "directory. The boundary and interconnect-method sidecars "
                 "travel with their GDS so the assembly DRC finds them as "
                 "siblings.")
    lines.append("")
    lines += section(layout)
    lines += ["", "## reports/ (DRC output; safe to archive or delete)", ""]
    lines += section(reports)
    lines += ["", "## logs/", "", "Per-run export logs (git-ignored)."]
    try:
        with open(os.path.join(output_dir, "MANIFEST.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


def run_export(board, options, plugin_dir,
               on_log=None, cancel_event=None) -> ExportResult:
    """End-to-end export driven by the dialog's Run button.

    Sequence:
      1. Resolve worker python and hyp_to_gds.py (early failure).
      2. Create a workspace tmpdir.
      3. Write Hyperlynx and intermediate .chiplet via the Python ports.
      4. Stage the intermediate .chiplet and the driving .hyp into
         ``options.output_dir`` (the .hyp is a first-class output that
         downstream tools consume), so ``--update-chiplet-file`` can
         rewrite the .chiplet in place.
      5. Invoke hyp_to_gds.py via the async runner; stream log lines
         to ``on_log``.
      6. Always remove the tmpdir.

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
    from . import chiplet_merge
    from ..writers.chiplet_writer import (
        write_chiplet, write_io_pads_json,
        write_cmim_devices_json,
        write_nofill_regions_json,
        write_die_pin_lists,
        read_die_connections, read_die_thicknesses, list_die_refs,
        _iu_to_um,
    )
    from ..writers.connection_stacks import validate_interconnect_ids
    from ..writers.hyperlynx_writer import write_hyperlynx

    if not options.output_dir:
        return ExportResult(error="Output directory is empty.")
    try:
        Path(options.output_dir).mkdir(parents=True, exist_ok=True)
        # The worker (KLayout layout.write + the boundaries sidecar) does not
        # create parent dirs, so the layout/ subdir must exist before it runs.
        Path(layout_dir(options.output_dir)).mkdir(parents=True, exist_ok=True)
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

    log_fh = None
    log_path = ""

    def _log(line):
        # Tee to the per-run log file (best-effort) and the dialog callback.
        if log_fh is not None:
            try:
                log_fh.write(line + "\n")
                log_fh.flush()
            except Exception:
                pass
        if on_log is not None:
            try:
                on_log(line)
            except Exception:
                pass

    tmpdir = tempfile.mkdtemp(prefix="chiplet_export_")
    try:
        log_fh, log_path = _open_run_log(options.output_dir, board_name)
        if log_path:
            _log("Run log: %s" % log_path)
        _log("Workspace: %s" % tmpdir)
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
        # Staged into the output dir only AFTER validate_interconnect_ids
        # passes (below), so a validation failure never leaves a stale,
        # non-re-anchored .chiplet behind.

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

        # Auto-extract no-fill (keep-out) regions from the board's dedicated
        # NoMetFiller / <metal>.nofill layers so the worker carves them out of
        # the metal fill. A non-empty options.nofill_regions_json overrides.
        effective_nofill = options.nofill_regions_json
        if not effective_nofill:
            nofill_auto = os.path.join(tmpdir,
                                       "%s_nofill_regions.json" % board_name)
            try:
                n_nofill = write_nofill_regions_json(board, nofill_auto)
            except Exception as exc:
                n_nofill = 0
                _log("Warning: no-fill region extraction failed: %s" % exc)
            if n_nofill:
                effective_nofill = nofill_auto
                _log("Auto-extracted %d no-fill region(s) from board" % n_nofill)

        # Auto-extract cap_cmim device metadata from the board so hyp_to_gds can
        # instantiate the real IntM4TM2 cmim PCell. A non-empty
        # options.cmim_devices_json acts as an explicit override.
        effective_cmim_devices = options.cmim_devices_json
        if not effective_cmim_devices:
            cmim_auto = os.path.join(tmpdir, "%s_cmim_devices.json" % board_name)
            cmim_skipped = []
            try:
                n_cmim = write_cmim_devices_json(board, cmim_auto,
                                                 skipped=cmim_skipped)
            except Exception as exc:
                n_cmim = 0
                _log("Warning: CMIM auto-extraction failed: %s" % exc)

            if cmim_skipped:
                # Symmetric with the worker's "requested but not placed" guard:
                # a cap_cmim on the board that cannot even be described would
                # otherwise vanish from every artifact on a run that exits 0.
                return ExportResult(
                    error=(
                        "%d cap_cmim footprint(s) could not be exported: %s.\n"
                        "Check their w/l/m fields: a bare number is read as "
                        "metres (how the symbol library stores it) and a "
                        "'um'/'u' suffix as micrometres (how the footprint "
                        "library does). The per-device cause is on the run "
                        "log."
                        % (len(cmim_skipped), ", ".join(cmim_skipped))
                    ),
                )

            if n_cmim:
                effective_cmim_devices = cmim_auto
                _log("Auto-extracted %d cmim device(s) from board" % n_cmim)

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

        # Per-die physical thickness: an explicit options map wins;
        # otherwise the board's per-footprint DIE_THICKNESS_UM fields are
        # the source of truth. Dies without an entry keep the format
        # default of 0.0.
        effective_die_thicks = options.die_thicknesses
        if not effective_die_thicks:
            try:
                effective_die_thicks = read_die_thicknesses(board)
            except Exception as exc:
                effective_die_thicks = {}
                _log("Warning: per-die thickness read failed: %s" % exc)
            if effective_die_thicks:
                _log("Per-die thickness (um) from board fields: %s"
                     % ", ".join("%s=%s" % (r, t) for r, t
                                 in sorted(effective_die_thicks.items())))

        # The inverse of the line above: a die with no thickness is not a
        # neutral default, it is a 0.0 that breaks one consumer and gets
        # silently invented by two others. Say so on every run.
        try:
            for line in describe_die_thickness_gaps(list_die_refs(board),
                                                    effective_die_thicks):
                _log(line)
        except Exception as exc:
            _log("Warning: die thickness check failed: %s" % exc)

        # The interposer's physical body (dimensions.thickness) now survives to
        # the .chiplet verbatim from the board stackup; flag an implausible
        # value (e.g. the KiCad FR-4 default), which means no real interposer
        # stackup was set. Cosmetic: die z uses attachment_surface_z, not this.
        try:
            body_iu = board.GetDesignSettings().GetBoardThickness()
            for line in describe_interposer_body_default(_iu_to_um(body_iu)):
                _log(line)
        except Exception as exc:
            _log("Warning: interposer body thickness check failed: %s" % exc)

        # Unknown per-die method ids fail the export here (manifest is the
        # source of truth) instead of degrading later in the worker or DRC.
        validate_interconnect_ids(die_methods=effective_die_conns.values())

        # All pre-worker validation passed: stage the intermediate into the
        # output dir now (hyp_to_gds --update-chiplet-file rewrites it in
        # place). Deferred from above so a validation failure leaves no
        # partial artifact.
        #
        # H-A clobber guard. write_chiplet regenerated the intermediate from
        # board state only, so a straight copy2 over the canonical file would
        # silently destroy any human/Studio-authored top-level block (flow:,
        # netlist:) and any hand-edited position. Guard the door before the
        # copy: (1) if the canonical file's exporter-owned content was
        # hand-edited outside KiCad since the last export, abort rather than
        # regenerate it away (unless forced) -- the copy alone could not be
        # skipped safely because the worker rewrites the file in place via
        # --update-chiplet-file; (2) carry the foreign blocks over into the
        # staged intermediate so flow: stays EMBEDDED (FlowEngine reads it only
        # from the embedded block) and survives the finalizer's round-trip.
        if options.emit_chiplet:
            if chiplet_merge.foreign_hand_edit_detected(chiplet_final) \
                    and not options.force:
                return ExportResult(
                    error=(
                        "The canonical .chiplet has hand edits to "
                        "exporter-owned content (e.g. a position) made outside "
                        "KiCad since the last export; re-exporting would "
                        "silently regenerate them away. Re-apply the change in "
                        "KiCad, or re-run with force=True to overwrite. "
                        "(flow:/netlist: blocks are preserved either way.)\n"
                        "  File: %s" % chiplet_final
                    ),
                )
            try:
                carried = chiplet_merge.carry_over_foreign_blocks(
                    chiplet_final, chiplet_intermediate)
            except Exception as exc:
                # A malformed canonical file must not crash the export; the
                # worst case degrades to the pre-guard behaviour (no carry-over),
                # loudly, instead of silently.
                carried = []
                _log("Warning: could not preserve foreign .chiplet blocks: %s"
                     % exc)
            if carried:
                _log("Preserved hand-authored .chiplet block(s) across "
                     "re-export: %s" % ", ".join(carried))
            shutil.copy2(chiplet_intermediate, chiplet_final)

        # The Hyperlynx netlist that drives hyp_to_gds is a first-class
        # output: stage it next to the .chiplet so downstream tools can
        # consume the exact .hyp the layout was generated from. Staged here,
        # after validation, so a validation failure leaves no partial
        # artifact (matching the .chiplet above).
        hyp_final = os.path.join(options.output_dir, "%s.hyp" % board_name)
        try:
            shutil.copy2(hyp_path, hyp_final)
            _log("Wrote Hyperlynx: %s" % hyp_final)
        except OSError as exc:
            _log("Warning: could not copy .hyp to output: %s" % exc)
            hyp_final = ""

        effective_options = dataclasses.replace(
            options,
            io_pads_json=effective_io_pads,
            cmim_devices_json=effective_cmim_devices,
            nofill_regions_json=effective_nofill,
            pad_locations=effective_pad_locs,
            die_connections=effective_die_conns,
            die_thicknesses=effective_die_thicks)

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

        # The staged .chiplet is the intermediate until the worker finalizes
        # it in place. If the worker failed or was cancelled it never did, so
        # what sits in the output dir carries `_metadata.finalize_required:
        # true` -- a file Chiplet Studio refuses to load, in the exact place
        # (and under the exact name) where a good one belongs. Retire it under
        # a name that cannot be mistaken for a product.
        chiplet_ok = options.emit_chiplet and run.exit_code == 0 \
            and not run.cancelled
        if options.emit_chiplet and not chiplet_ok:
            unfinalized = chiplet_final + ".unfinalized"
            try:
                os.replace(chiplet_final, unfinalized)
                _log("Export did not finalize the .chiplet; the intermediate "
                     "is kept as %s (Chiplet Studio cannot load it)."
                     % unfinalized)
            except OSError as exc:
                _log("Warning: could not retire the unfinalized .chiplet: %s"
                     % exc)

        # H-A clobber guard, second half: record the finalized file's
        # exporter-content digest so the next re-export can tell a genuine
        # hand edit (position touched outside KiCad) from a foreign-block edit
        # (a pasted flow:) and from an unchanged file. Best-effort: a sidecar
        # failure must never fail an otherwise good export.
        if chiplet_ok:
            try:
                chiplet_merge.record_exporter_content_digest(chiplet_final)
            except Exception as exc:
                _log("Warning: could not record .chiplet content digest: %s"
                     % exc)

        # The worker writes <board>_cupillar_drc.json next to the interposer
        # GDS (now under layout/) when a cupillar stack drives pillar
        # generation. Surface it only if it was actually produced this run,
        # and tuck it under reports/ so the output-dir root keeps just the
        # .chiplet and the MANIFEST.
        drc_report = ""
        cupillar_flat = layout_path(options.output_dir,
                                    "%s_cupillar_drc.json" % board_name)
        if os.path.exists(cupillar_flat):
            reports_dir = os.path.join(options.output_dir, "reports")
            os.makedirs(reports_dir, exist_ok=True)
            cupillar_dst = os.path.join(reports_dir,
                                        "%s_cupillar_drc.json" % board_name)
            try:
                shutil.move(cupillar_flat, cupillar_dst)
                drc_report = cupillar_dst
            except OSError as exc:
                _log("Warning: could not move cu-pillar DRC into reports/: %s"
                     % exc)
                drc_report = cupillar_flat

        # Metal-fill read-back sidecars (written by the worker next to the
        # interposer GDS under layout/). The density report is a verdict, so it
        # joins reports/; the coarse coverage map is layout-derived geometry the
        # KiCad read-back layer consumes, so it stays in layout/.
        fill_density_report = ""
        fill_coverage = ""
        if options.insert_metal_fill:
            density_flat = layout_path(
                options.output_dir,
                "%s_interposer.fill_density.json" % board_name)
            if os.path.exists(density_flat):
                reports_dir = os.path.join(options.output_dir, "reports")
                os.makedirs(reports_dir, exist_ok=True)
                density_dst = os.path.join(
                    reports_dir, "%s_fill_density.json" % board_name)
                try:
                    shutil.move(density_flat, density_dst)
                    fill_density_report = density_dst
                except OSError as exc:
                    _log("Warning: could not move fill density report into "
                         "reports/: %s" % exc)
                    fill_density_report = density_flat
            coverage_flat = layout_path(
                options.output_dir,
                "%s_interposer.fill_coverage.json" % board_name)
            if os.path.exists(coverage_flat):
                fill_coverage = coverage_flat

        # ADK assembly DRC over the complete.gds. The chiplet boundaries come
        # from the <complete>.boundaries.json manifest that hyp_to_gds wrote
        # next to the GDS (auto-discovered by run_drc.py); they are not a GDS
        # layer. Runs only when there is a complete.gds to check and the user
        # did not opt out via emit_assembly_drc=False.
        assembly_drc_exit = -1
        assembly_drc_report = ""
        drc_cancelled = False
        complete_gds_abs = layout_path(
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
                # The DRC's interposer/interconnect adapters and per-method IXN
                # scoping come from the .chiplet's declared fields. chiplet_final
                # exists only when emit_chiplet is set; otherwise read the
                # intermediate (still in tmpdir, carrying the same adapters and
                # per-die connections) so the DRC honours the design's real
                # adapters instead of silently defaulting to intm4tm2.
                chiplet_for_drc = (
                    chiplet_final
                    if options.emit_chiplet and os.path.exists(chiplet_final)
                    else chiplet_intermediate
                )
                effective_adapter = (
                    options.interposer_adapter
                    or load_interposer_adapter(chiplet_for_drc)
                )
                effective_interconnect = (
                    options.interconnect_adapter
                    or load_interconnect_adapter(chiplet_for_drc)
                )
                # Per-method IXN refinement: derive {method -> dies} from the
                # .chiplet's per-die connections + the interconnect PDK
                # manifest, written as a sidecar next to the complete GDS
                # (sibling of the boundaries manifest). Best-effort: any
                # failure leaves the assembly-global adapter behavior.
                ixn_methods_sidecar = ""
                try:
                    derived_methods = derive_interconnect_methods(
                        chiplet_for_drc,
                        interconnect_root=options.interconnect_pdk_root,
                        board=board,
                    )
                    # Drop dies the boundary manifest does not declare, or the
                    # assembly DRC deck hard-raises on the sidecar.
                    derived_methods = _intersect_methods_with_manifest(
                        derived_methods, complete_gds_abs)
                    ixn_methods_sidecar = write_ixn_methods_sidecar(
                        derived_methods, complete_gds_abs, chiplet_for_drc,
                    )
                    if ixn_methods_sidecar:
                        _log("Interconnect methods sidecar: %s (%s)" % (
                            ixn_methods_sidecar,
                            ", ".join(sorted(derived_methods)),
                        ))
                except Exception as exc:
                    _log("Warning: per-method interconnect derivation "
                         "failed: %s" % exc)
                # DRC report + its scratch run dir live under reports/ so they
                # stay out of the layout/ subdir and the output-dir root. The
                # report's parent must exist before KLayout writes it (run_drc
                # only mkdirs its own run_dir), so create reports/ here.
                reports_dir = os.path.join(options.output_dir, "reports")
                os.makedirs(reports_dir, exist_ok=True)
                drc_run_dir = os.path.join(reports_dir, "assembly_drc")
                assembly_drc_report_target = os.path.join(
                    reports_dir,
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
                if adk_run.cancelled:
                    # A cancel during the DRC must not be reported as a DRC
                    # failure: leave it NOT RUN (-1) and surface the cancel.
                    drc_cancelled = True
                else:
                    assembly_drc_exit = adk_run.exit_code
                    if os.path.exists(assembly_drc_report_target):
                        assembly_drc_report = assembly_drc_report_target

        _write_outputs_manifest(options.output_dir, board_name)
        return ExportResult(
            exit_code=run.exit_code,
            cancelled=run.cancelled or drc_cancelled,
            hyp_path=hyp_final,
            # Only a finalized file gets reported: a headless caller handed a
            # path to an intermediate would ship it straight to a consumer
            # that rejects it.
            chiplet_path=(chiplet_final if chiplet_ok else ""),
            interposer_gds_path=layout_path(
                options.output_dir, "%s_interposer.gds" % board_name),
            complete_gds_path=(
                complete_gds_abs if options.emit_complete_gds else ""
            ),
            cupillar_drc_path=drc_report,
            fill_density_report_path=fill_density_report,
            fill_coverage_path=fill_coverage,
            assembly_drc_exit_code=assembly_drc_exit,
            assembly_drc_report_path=assembly_drc_report,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if log_fh is not None:
            try:
                log_fh.close()
            except Exception:
                pass
