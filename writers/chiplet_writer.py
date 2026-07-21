# SPDX-License-Identifier: GPL-3.0-or-later
"""
Chiplet YAML writer.

Replicates kicad/pcbnew/exporters/export_chiplet.cpp in pure Python
via the pcbnew SWIG bindings. Output is the intermediate .chiplet
with the `_metadata.finalize_required: true` block; the canonical
file is produced downstream by hyp_to_gds.py --update-chiplet-file.

Frame and anchor semantics follow
chiplet-studio/docs/coord_frame_contract.md sections 1, 4.1, 4.4.
"""

import math
import os
import sys

import pcbnew

from .connection_stacks import (
    emit_connection_stacks_block,
    emit_interconnect_block,
    validate_interconnect_ids,
)
from ._yaml import escape_yaml_dq, yaml_scalar


def _iu_to_um(iu):
    """Internal units to micrometers. Mirrors iu2um() in the C++ exporter."""
    return iu / (pcbnew.PCB_IU_PER_MM / 1000.0)


def _field_text(footprint, name, default=""):
    """Return the text of a footprint field by name, or `default` if absent."""
    if footprint.HasField(name):
        return footprint.GetFieldText(name)
    return default


def _lookup_property(board, name):
    """Look `name` up in BOARD.GetProperties(), then PROJECT.GetTextVars().

    Both maps may be exposed by SWIG as dict-like or std::map-like objects;
    we try membership testing first and fall back to the std::map API.

    Some boards (e.g. legacy KiCad demos without a modern .kicad_pro) yield a
    PROJECT whose SWIG wrapper doesn't expose GetTextVars; we treat that as
    "no text variables" instead of raising.
    """
    def _try(container):
        if container is None:
            return None
        try:
            if name in container:
                return str(container[name])
        except (TypeError, KeyError):
            pass
        if hasattr(container, "count") and hasattr(container, "at"):
            try:
                if container.count(name):
                    return str(container.at(name))
            except Exception:
                pass
        return None

    value = _try(board.GetProperties())
    if value is not None:
        return value
    project = board.GetProject()
    if project is not None and hasattr(project, "GetTextVars"):
        try:
            text_vars = project.GetTextVars()
        except Exception:
            text_vars = None
        value = _try(text_vars)
        if value is not None:
            return value
    return ""


def _lookup_text_var(board, name):
    """Look `name` up in PROJECT.GetTextVars() ONLY (never BOARD.GetProperties()).

    The C++ exporter reads INTERPOSER_ADAPTER / INTERCONNECT_ADAPTER from the
    project text variables only; _lookup_property's GetProperties-first lookup
    would let a board property shadow the text variable and diverge from
    byte-exact parity. Returns "" when absent (caller applies any default).
    """
    project = board.GetProject()
    if project is None or not hasattr(project, "GetTextVars"):
        return ""
    try:
        text_vars = project.GetTextVars()
    except Exception:
        return ""
    if text_vars is None:
        return ""
    try:
        if name in text_vars:
            return str(text_vars[name])
    except (TypeError, KeyError):
        pass
    if hasattr(text_vars, "count") and hasattr(text_vars, "at"):
        try:
            if text_vars.count(name):
                return str(text_vars.at(name))
        except Exception:
            pass
    return ""


def write_io_pads_json(board, output_path):
    """Extract IO_CLASS footprints from `board` into an io_pads.json sidecar.

    Output matches gds_to_kicad/io_pads/kicad_pcb_to_iopads.py: positions in
    micrometers with Y negated (KiCad Y-down -> GDS Y-up), consumed by
    hyp_to_gds.py --io-pads to render the TopMetal2 pad geometry and inject
    the pads under the interposer component (so the GDS bbox includes them).

    Returns the number of io_pads written (0 -> nothing written, no file).
    """
    import json

    pads_out = []
    for fp in list(board.Footprints()):
        io_class = _field_text(fp, "IO_CLASS")
        if not io_class:
            continue
        size_str = _field_text(fp, "IO_PAD_SIZE_UM")
        size_x_um = 0.0
        size_y_um = 0.0
        if size_str:
            parts = size_str.split("x")
            if len(parts) == 2:
                try:
                    size_x_um = float(parts[0])
                    size_y_um = float(parts[1])
                except ValueError:
                    pass
        pads_list = list(fp.Pads())
        if (size_x_um <= 0.0 or size_y_um <= 0.0) and pads_list:
            size_x_um = _iu_to_um(pads_list[0].GetSizeX())
            size_y_um = _iu_to_um(pads_list[0].GetSizeY())
        net_name = pads_list[0].GetNetname() if pads_list else ""
        pos = fp.GetPosition()
        try:
            layer_name = board.GetLayerName(fp.GetLayer())
        except Exception:
            layer_name = "F.Cu"
        pads_out.append({
            "ref": fp.GetReference(),
            "io_class": io_class,
            "x_um": _iu_to_um(pos.x),
            "y_um": -_iu_to_um(pos.y),
            "size_x_um": size_x_um,
            "size_y_um": size_y_um,
            "net": net_name,
            "layer": layer_name,
        })
    if not pads_out:
        return 0
    with open(output_path, "w") as f:
        json.dump({"io_pads": pads_out}, f, indent=2)
    return len(pads_out)


def write_die_pin_lists(board, out_dir):
    """Extract die footprint pads into per-die pin_list JSON sidecars.

    A die footprint carries a GDS_FILE field; its pads ARE the chiplet's
    bump locations. Output matches the gds_to_kicad pin_list schema
    (footprint-local coordinates in DBU = nm, Y negated for GDS Y-up),
    consumed downstream by the Cu-pillar generator (bump_mirror) to place
    DRC-validated pillars under each flip-chip die.

    Returns a dict {device_ref: json_path}. Empty if no die footprints.
    """
    import json

    result = {}
    for fp in list(board.Footprints()):
        if not _field_text(fp, "GDS_FILE"):
            continue
        ref = fp.GetReference()
        pins = []
        fp_pos = fp.GetPosition()
        for idx, pad in enumerate(list(fp.Pads())):
            try:
                local = pad.GetFPRelativePosition()
                lx, ly = local.x, local.y
            except Exception:
                pos = pad.GetPosition()
                lx, ly = pos.x - fp_pos.x, pos.y - fp_pos.y
            pins.append({
                "name": pad.GetName() or ("pad%d" % idx),
                "type": "passive",
                "pad_index": idx,
                "center_x_dbu": float(lx),
                "center_y_dbu": float(-ly),
                "width_dbu": float(pad.GetSizeX()),
                "height_dbu": float(pad.GetSizeY()),
            })
        if not pins:
            continue
        path = os.path.join(out_dir, "%s_pins.json" % ref)
        with open(path, "w") as f:
            json.dump({"version": 1, "chiplet_name": ref,
                       "dbu_um": 0.001, "pins": pins}, f, indent=2)
        result[ref] = path
    return result


# Footprint field naming the die's interconnect method (a manifest method
# id, e.g. "cupillar_opt2"). Lives on the footprint -- same family as
# GDS_FILE / ORIENTATION -- so per-die method selection persists in the
# board and survives re-export.
CONNECTION_FIELD = "CONNECTION"


def list_die_refs(board):
    """Sorted refs of the board's die footprints (GDS_FILE field present)."""
    return sorted(fp.GetReference() for fp in list(board.Footprints())
                  if _field_text(fp, "GDS_FILE"))


def read_die_connections(board):
    """Per-die interconnect methods from the board's footprint fields.

    Returns {ref: method id} for every die footprint (GDS_FILE present)
    whose CONNECTION field is non-empty. Dies without the field fall back
    to the export's assembly-global connection type downstream.
    """
    result = {}
    for fp in list(board.Footprints()):
        if not _field_text(fp, "GDS_FILE"):
            continue
        method = _field_text(fp, CONNECTION_FIELD).strip()
        if method:
            result[fp.GetReference()] = method
    return result


def _get_field(footprint, name):
    """Footprint field object by name, or None (the SWIG bindings expose
    GetFields() but not GetFieldByName)."""
    for field in footprint.GetFields():
        if field.GetName() == name:
            return field
    return None


def _style_managed_field(field):
    """Style a machine-managed footprint field: F.Fab, hidden, small text.

    KiCad creates new fields visible on F.SilkS at 1.27 mm -- on a
    um-scale interposer board that draws method ids across the whole
    layout. Returns True when anything was adjusted (idempotent).
    """
    changed = False
    if field.GetLayer() != pcbnew.F_Fab:
        field.SetLayer(pcbnew.F_Fab)
        changed = True
    if field.IsVisible():
        field.SetVisible(False)
        changed = True
    small = pcbnew.FromMM(0.2)
    size = field.GetTextSize()
    if size.x != small or size.y != small:
        field.SetTextSize(pcbnew.VECTOR2I(small, small))
        changed = True
    thin = pcbnew.FromMM(0.05)
    if field.GetTextThickness() != thin:
        field.SetTextThickness(thin)
        changed = True
    return changed


def write_die_connections(board, mapping):
    """Persist per-die interconnect methods to footprint CONNECTION fields.

    `mapping` is {ref: method id}; an empty value clears the override (the
    die falls back to the assembly default). Only die footprints (GDS_FILE
    present) are touched, and only when the field text or its presentation
    actually changes (the field is kept hidden on F.Fab at small size --
    see _style_managed_field). Returns the refs that were modified; the
    caller owns saving the board.
    """
    changed = []
    for fp in list(board.Footprints()):
        if not _field_text(fp, "GDS_FILE"):
            continue
        ref = fp.GetReference()
        if ref not in mapping:
            continue
        new = (mapping[ref] or "").strip()
        text_changed = _field_text(fp, CONNECTION_FIELD).strip() != new
        if text_changed:
            fp.SetField(CONNECTION_FIELD, new)
        field = _get_field(fp, CONNECTION_FIELD)
        restyled = _style_managed_field(field) if field is not None else False
        if text_changed or restyled:
            changed.append(ref)
    return changed


# Footprint field carrying the die's physical thickness in micrometers
# (the body z-extent written to .chiplet dimensions.thickness; interconnect
# stack heights are a separate axis -- see connection_stacks). Same family
# as CONNECTION: the board is the source of truth, presentation is
# machine-managed.
DIE_THICKNESS_FIELD = "DIE_THICKNESS_UM"


def parse_thickness_um(raw):
    """Parse a DIE_THICKNESS_UM field value. Returns a positive float in
    micrometers, or None when the text is empty or not a positive finite
    number (callers decide whether that warrants a warning)."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0.0:
        return None
    return value


def read_die_thicknesses(board):
    """Per-die physical thickness (um) from the board's footprint fields.

    Returns {ref: thickness_um} for every die footprint (GDS_FILE present)
    whose DIE_THICKNESS_UM field parses as a positive finite number. A
    non-empty value that does not parse warns to stderr and is skipped --
    the die then keeps the format default of 0.0 downstream.
    """
    result = {}
    for fp in list(board.Footprints()):
        if not _field_text(fp, "GDS_FILE"):
            continue
        raw = _field_text(fp, DIE_THICKNESS_FIELD).strip()
        if not raw:
            continue
        ref = fp.GetReference()
        value = parse_thickness_um(raw)
        if value is None:
            print("WARNING: %s: ignoring %s '%s' (expected a positive "
                  "number of micrometers)" % (ref, DIE_THICKNESS_FIELD, raw),
                  file=sys.stderr)
            continue
        result[ref] = value
    return result


def write_die_thicknesses(board, mapping):
    """Persist per-die thickness (um) to footprint DIE_THICKNESS_UM fields.

    `mapping` is {ref: text}; an empty value clears the field (the die
    falls back to the format default of 0.0). Only die footprints
    (GDS_FILE present) are touched, and only when the field text or its
    presentation actually changes (kept hidden on F.Fab at small size --
    see _style_managed_field). Returns the refs that were modified; the
    caller owns saving the board.
    """
    changed = []
    for fp in list(board.Footprints()):
        if not _field_text(fp, "GDS_FILE"):
            continue
        ref = fp.GetReference()
        if ref not in mapping:
            continue
        new = (mapping[ref] or "").strip()
        text_changed = _field_text(fp, DIE_THICKNESS_FIELD).strip() != new
        if text_changed:
            fp.SetField(DIE_THICKNESS_FIELD, new)
        field = _get_field(fp, DIE_THICKNESS_FIELD)
        restyled = _style_managed_field(field) if field is not None else False
        if text_changed or restyled:
            changed.append(ref)
    return changed


def write_chiplet(board, output_path):
    """Write `board` to `output_path` as an intermediate .chiplet file.

    Args:
        board:        pcbnew.BOARD instance.
        output_path:  Filesystem path for the YAML output.

    Returns:
        True on success.

    Raises:
        OSError if the output path is not writable.
    """
    # --- Data gathering ---

    interposer_lyp = _lookup_property(board, "INTERPOSER_LYP")
    interposer_tech_id = "intm4tm2"
    tech_map = {}
    if interposer_lyp:
        tech_map[interposer_tech_id] = interposer_lyp
    else:
        sys.stderr.write(
            "Warning: INTERPOSER_LYP path not found. "
            "Set it in Board Setup > Text Variables.\n"
        )

    # Interconnect adapter (optional second axis), read from text var
    # INTERCONNECT_ADAPTER. Validated against the interconnect PDK manifest
    # before any output is written, so a typo fails the export here instead
    # of surfacing later in studio or the assembly DRC.
    # Text-vars only, mirroring export_chiplet.cpp (GetTextVars()); a board
    # property must not shadow this or byte-exact parity breaks.
    interconnect_adapter = _lookup_text_var(board, "INTERCONNECT_ADAPTER")
    validate_interconnect_ids(adapter=interconnect_adapter)

    component_techs = {}
    components = []
    io_pads = []

    for footprint in list(board.Footprints()):
        if footprint.GetAttributes() & pcbnew.FP_EXCLUDE_FROM_BOM:
            continue

        io_class = _field_text(footprint, "IO_CLASS")
        if io_class:
            # The reader only accepts these classes (IOPad.cpp
            # string_to_io_class throws otherwise) and the finalizer does not
            # normalize them on the no-sidecar path, so a typo would ship a
            # .chiplet that fails at load. Fail loudly at export, mirroring the
            # C++ exporter.
            if io_class not in ("wire_bond", "flipped_bump", "tsv_bump"):
                raise ValueError(
                    "Footprint %s has an unrecognized IO_CLASS '%s'; expected "
                    "wire_bond, flipped_bump, or tsv_bump."
                    % (footprint.GetReference(), io_class)
                )
            io_pads.append(footprint)
            continue

        gds_file = _field_text(footprint, "GDS_FILE")
        lyp_file = _field_text(footprint, "LYP_FILE")

        if not gds_file:
            sys.stderr.write(
                "Warning: Footprint %s missing GDS_FILE field.\n"
                % footprint.GetReference()
            )

        if lyp_file:
            tech_id = os.path.splitext(os.path.basename(lyp_file))[0]
            tech_id = tech_id.replace(" ", "_").replace(".", "_")
            # A die LYP whose basename collides with the interposer's hardcoded
            # technology id ('intm4tm2') but points at a different file would
            # silently clobber the interposer entry. Disambiguate the die with
            # its reference so the interposer keeps its own layer_properties.
            # (Two dies that share a basename keep dedup'ing to one entry.)
            if (tech_id == interposer_tech_id
                    and interposer_tech_id in tech_map
                    and tech_map[interposer_tech_id] != lyp_file):
                tech_id = tech_id + "_" + footprint.GetReference()
            tech_map[tech_id] = lyp_file
            component_techs[id(footprint)] = tech_id

        components.append(footprint)

    # --- YAML emission ---

    board_filename = board.GetFileName()
    board_name = os.path.splitext(os.path.basename(board_filename))[0]
    if not board_name:
        # Unsaved / in-memory board: fall back to the output basename, then a
        # literal, so the reader's non-empty assembly.name rule is satisfied.
        board_name = os.path.splitext(os.path.basename(output_path))[0]
    if not board_name:
        board_name = "assembly"

    with open(output_path, "w", encoding="utf-8") as f:
        # Header
        f.write('format_version: "1.0"\n\n')

        # Intermediate-frame marker. Readers MUST refuse files where
        # finalize_required is true (see ChipletFormat::load).
        f.write("_metadata:\n")
        f.write("  frame: pcb-bbox-corner\n")
        f.write("  finalize_required: true\n")
        f.write('  finalizer: "hyp_to_gds.py --update-chiplet-file"\n')
        f.write("\n")

        # Assembly
        f.write("assembly:\n")
        f.write('  name: "%s"\n' % escape_yaml_dq(board_name))
        f.write('  units: "um"\n')
        f.write("\n")

        # Interposer adapter. Declares which ADK PDK adapter the assembly
        # DRC should resolve when this design is checked. Override via
        # Board Setup > Text Variables > INTERPOSER_ADAPTER.
        # Text-vars only, mirroring export_chiplet.cpp (GetTextVars()).
        interposer_adapter = (
            _lookup_text_var(board, "INTERPOSER_ADAPTER")
            or "intm4tm2"
        )
        f.write("interposer:\n")
        f.write('  adapter: "%s"\n' % escape_yaml_dq(interposer_adapter))
        f.write("\n")

        # Interconnect adapter (optional second axis); emitted only when
        # set, mirroring interposer. Validated during data gathering.
        f.write(emit_interconnect_block(interconnect_adapter))

        # Technologies. Sorted by id to match the C++ exporter, whose techMap
        # is a std::map (sorted iteration); a plain dict here would emit in
        # insertion order and break byte-exact parity for multi-PDK assemblies.
        f.write("technologies:\n")
        for tech_id, lyp_path in sorted(tech_map.items()):
            f.write("  %s:\n" % yaml_scalar(tech_id))
            f.write('    description: "Imported from KiCad"\n')
            f.write('    layer_properties: "%s"\n' % escape_yaml_dq(lyp_path))
            f.write("    dbu: 0.001\n")
            f.write("\n")

        # Connection stacks (default bump library). Sourced from the
        # interconnect PDK manifest (single source of truth); byte-identical to
        # the prior hardcoded literal so the C++ byte-exact parity gate (47.7b)
        # stays green. export_chiplet.cpp still emits the same literal.
        f.write(emit_connection_stacks_block())
        f.write("\n")

        # Components
        f.write("components:\n")

        # 1) Interposer (always first; placeholder position in intermediate frame).
        board_bbox = board.GetBoardEdgesBoundingBox()
        if (not board_bbox.IsValid()
                or board_bbox.GetWidth() == 0
                or board_bbox.GetHeight() == 0):
            # Fallback for designs without Edge.Cuts shapes (e.g. nm-scale
            # GDS-derived interposers): derive bbox from the union of
            # footprints / tracks / zones.
            board_bbox = board.ComputeBoundingBox(False)

        width_um = _iu_to_um(board_bbox.GetWidth())
        height_um = _iu_to_um(board_bbox.GetHeight())
        thickness_um = _iu_to_um(board.GetDesignSettings().GetBoardThickness())

        interposer_x_min = board_bbox.GetX()
        interposer_y_max = board_bbox.GetBottom()  # Y grows down in KiCad

        f.write("  - id: interposer\n")
        f.write("    type: interposer\n")
        # See coord_frame_contract.md section 2.
        f.write("    anchor: bbox_center\n")
        if interposer_tech_id in tech_map:
            f.write("    technology: %s\n" % yaml_scalar(interposer_tech_id))

        gds_name = "%s.gds" % board_name
        f.write('    layout: "%s"\n' % escape_yaml_dq(gds_name))
        f.write('    top_cell: "INTERPOSER"\n')
        f.write("    dimensions:\n")
        f.write("      width: %.6f\n" % width_um)
        f.write("      height: %.6f\n" % height_um)
        f.write("      thickness: %.6f\n" % thickness_um)
        # Position placeholder; hyp_to_gds.py --update-chiplet-file fills
        # the canonical GDS-bbox-corner value. See coord_frame_contract.md
        # section 4.1.
        f.write("    position:\n")
        f.write("      x: 0.0\n")
        f.write("      y: 0.0\n")
        f.write("      z: 0.0\n")

        # 1b) IO_CLASS footprints nested under interposer.io_pads.
        if io_pads:
            f.write("    io_pads:\n")
            for fp in io_pads:
                ref = fp.GetReference()
                io_class = _field_text(fp, "IO_CLASS")
                size_str = _field_text(fp, "IO_PAD_SIZE_UM")

                size_x_um = 0.0
                size_y_um = 0.0
                if size_str:
                    parts = size_str.split("x")
                    if len(parts) == 2:
                        try:
                            size_x_um = float(parts[0])
                            size_y_um = float(parts[1])
                        except ValueError:
                            pass

                pads_list = list(fp.Pads())
                if (size_x_um <= 0.0 or size_y_um <= 0.0) and pads_list:
                    pad = pads_list[0]
                    size_x_um = _iu_to_um(pad.GetSizeX())
                    size_y_um = _iu_to_um(pad.GetSizeY())

                net_name = ""
                if pads_list:
                    net_name = pads_list[0].GetNetname()

                pos = fp.GetPosition()
                x_um = _iu_to_um(pos.x - interposer_x_min)
                y_um = _iu_to_um(interposer_y_max - pos.y)

                f.write("      - id: %s\n" % yaml_scalar(ref))
                f.write("        io_class: %s\n" % yaml_scalar(io_class))
                f.write('        net: "%s"\n' % escape_yaml_dq(net_name))
                f.write(
                    "        position: { x: %.6f, y: %.6f }\n"
                    % (x_um, y_um)
                )
                f.write(
                    "        size: { x: %.3f, y: %.3f }\n"
                    % (size_x_um, size_y_um)
                )
                f.write("        layer: TopMetal2\n")
        f.write("\n")

        # 2) Die components.
        for footprint in components:
            ref = footprint.GetReference()
            gds = _field_text(footprint, "GDS_FILE")
            orient = _field_text(footprint, "ORIENTATION")
            # ORIENTATION is a free-text footprint field. The frame contract
            # defines only face_up (the default) and flip_chip; an empty field
            # means face_up. Any other token (a typo, or the non-canonical
            # "face_down") would otherwise fall through to a non-flip die that
            # is silently emitted un-mirrored and without its connection stack.
            # Fail loudly at export, mirroring the IO_CLASS check above.
            if orient not in ("", "face_up", "flip_chip"):
                raise ValueError(
                    "Footprint %s has an unrecognized ORIENTATION '%s'; expected "
                    "face_up or flip_chip (use flip_chip, not face_down)."
                    % (ref, orient)
                )
            is_flip_chip = (orient == "flip_chip")

            f.write("  - id: %s\n" % yaml_scalar(ref))
            f.write("    type: die\n")
            # See coord_frame_contract.md sections 2 and 4.4: dies use
            # gds_origin as the mesh anchor.
            f.write("    anchor: gds_origin\n")

            if id(footprint) in component_techs:
                f.write("    technology: %s\n" % yaml_scalar(component_techs[id(footprint)]))

            if is_flip_chip:
                f.write("    connection: cupillar_opt2\n")

            if gds:
                f.write('    layout: "%s"\n' % escape_yaml_dq(gds))

            if is_flip_chip:
                f.write("    orientation: flip_chip\n")

            fp_bbox = footprint.GetBoundingBox()
            courtyard_layer = (
                pcbnew.B_CrtYd if footprint.GetLayer() == pcbnew.B_Cu
                else pcbnew.F_CrtYd
            )
            # Courtyard caches are built lazily: a headless pcbnew.LoadBoard()
            # leaves them empty while the live GUI editor keeps them warm, so die
            # width/height silently depended on GUI-vs-headless run context
            # (GetBoundingBox fallback vs courtyard.BBox()). Force the build so
            # both paths deterministically take the courtyard branch.
            try:
                footprint.BuildCourtyardCaches()
            except Exception:
                pass
            courtyard = footprint.GetCourtyard(courtyard_layer)
            if not courtyard.IsEmpty():
                fp_bbox = courtyard.BBox()

            comp_width = _iu_to_um(fp_bbox.GetWidth())
            comp_height = _iu_to_um(fp_bbox.GetHeight())

            f.write("    dimensions:\n")
            f.write("      width: %.6f\n" % comp_width)
            f.write("      height: %.6f\n" % comp_height)
            f.write("      thickness: 0.0\n")

            pos = footprint.GetPosition()
            x_out = _iu_to_um(pos.x - interposer_x_min)
            y_out = _iu_to_um(interposer_y_max - pos.y)

            f.write("    position:\n")
            f.write("      x: %.6f\n" % x_out)
            f.write("      y: %.6f\n" % y_out)

            if is_flip_chip:
                # Z auto-calculated by Chiplet Studio from the connection
                # stack height.
                f.write("      z: 0.0\n")
            elif footprint.GetLayer() == pcbnew.F_Cu:
                f.write("      z: %.6f\n" % thickness_um)
            else:
                f.write("      z: 0.0\n")

            f.write("    rotation:\n")
            f.write(
                "      z: %.4f\n"
                % footprint.GetOrientation().AsDegrees()
            )
            f.write("\n")

    return True
