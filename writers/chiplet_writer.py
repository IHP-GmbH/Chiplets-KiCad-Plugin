# SPDX-License-Identifier: GPL-2.0-or-later
"""
Chiplet YAML writer.

Replicates kicad/pcbnew/exporters/export_chiplet.cpp in pure Python
via the pcbnew SWIG bindings. Output is the intermediate .chiplet
with the `_metadata.finalize_required: true` block; the canonical
file is produced downstream by hyp_to_gds.py --update-chiplet-file.

Frame and anchor semantics follow
chiplet-studio/docs/coord_frame_contract.md sections 1, 4.1, 4.4.
"""

import os
import sys

import pcbnew


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
    interposer_tech_id = "interposer_tech"
    tech_map = {}
    if interposer_lyp:
        tech_map[interposer_tech_id] = interposer_lyp
    else:
        sys.stderr.write(
            "Warning: INTERPOSER_LYP path not found. "
            "Set it in Board Setup > Text Variables.\n"
        )

    component_techs = {}
    components = []
    io_pads = []

    for footprint in list(board.Footprints()):
        if footprint.GetAttributes() & pcbnew.FP_EXCLUDE_FROM_BOM:
            continue

        io_class = _field_text(footprint, "IO_CLASS")
        if io_class:
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
            tech_map[tech_id] = lyp_file
            component_techs[id(footprint)] = tech_id

        components.append(footprint)

    # --- YAML emission ---

    board_filename = board.GetFileName()
    board_name = os.path.splitext(os.path.basename(board_filename))[0]

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
        f.write('  name: "%s"\n' % board_name)
        f.write('  units: "um"\n')
        f.write("\n")

        # Interposer adapter. Declares which ADK PDK adapter the assembly
        # DRC should resolve when this design is checked. Override via
        # Board Setup > Text Variables > INTERPOSER_ADAPTER.
        interposer_adapter = (
            _lookup_property(board, "INTERPOSER_ADAPTER")
            or "ihp_sg13g2_interposer"
        )
        f.write("interposer:\n")
        f.write('  adapter: "%s"\n' % interposer_adapter)
        f.write("\n")

        # Technologies
        f.write("technologies:\n")
        for tech_id, lyp_path in tech_map.items():
            f.write("  %s:\n" % tech_id)
            f.write('    description: "Imported from KiCad"\n')
            f.write('    layer_properties: "%s"\n' % lyp_path)
            f.write("    dbu: 0.001\n")
            f.write("\n")

        # Connection stacks (default bump library) -- verbatim from C++.
        f.write("connection_stacks:\n")
        f.write("  cupillar_opt1:\n")
        f.write('    description: "PacTech Cu Pillar, Table 6.1 Option 1 (35um opening)"\n')
        f.write("    layers:\n")
        f.write("      - {name: CuPillar, material: Cu, height: 28.0, diameter: 44.0}\n")
        f.write("      - {name: SnAgCap, material: SnAg, height: 16.0, diameter: 44.0}\n")
        f.write("  cupillar_opt2:\n")
        f.write('    description: "PacTech Cu Pillar, Table 6.1 Option 2 (40um opening)"\n')
        f.write("    layers:\n")
        f.write("      - {name: CuPillar, material: Cu, height: 32.0, diameter: 49.0}\n")
        f.write("      - {name: SnAgCap, material: SnAg, height: 16.0, diameter: 49.0}\n")
        f.write("  cupillar_opt3:\n")
        f.write('    description: "PacTech Cu Pillar, Table 6.1 Option 3 (45um opening)"\n')
        f.write("    layers:\n")
        f.write("      - {name: CuPillar, material: Cu, height: 42.0, diameter: 54.0}\n")
        f.write("      - {name: SnAgCap, material: SnAg, height: 19.0, diameter: 54.0}\n")
        f.write("  sbump_sac305:\n")
        f.write('    description: "PacTech SAC305 solder bump (80um ball)"\n')
        f.write("    layers:\n")
        f.write("      - {name: SolderBall, material: SAC305, height: 80.0, diameter: 80.0}\n")
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
            f.write("    technology: %s\n" % interposer_tech_id)

        gds_name = "%s.gds" % board_name
        f.write('    layout: "%s"\n' % gds_name)
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

                f.write("      - id: %s\n" % ref)
                f.write("        io_class: %s\n" % io_class)
                f.write('        net: "%s"\n' % net_name)
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
            is_flip_chip = (orient == "flip_chip")

            f.write("  - id: %s\n" % ref)
            f.write("    type: die\n")
            # See coord_frame_contract.md sections 2 and 4.4: dies use
            # gds_origin as the mesh anchor.
            f.write("    anchor: gds_origin\n")

            if id(footprint) in component_techs:
                f.write("    technology: %s\n" % component_techs[id(footprint)])

            if is_flip_chip:
                f.write("    connection: cupillar_opt2\n")

            if gds:
                f.write('    layout: "%s"\n' % gds)

            if is_flip_chip:
                f.write("    orientation: flip_chip\n")

            fp_bbox = footprint.GetBoundingBox()
            courtyard_layer = (
                pcbnew.B_CrtYd if footprint.GetLayer() == pcbnew.B_Cu
                else pcbnew.F_CrtYd
            )
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
