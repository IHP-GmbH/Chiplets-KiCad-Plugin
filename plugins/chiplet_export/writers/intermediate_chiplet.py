# SPDX-License-Identifier: GPL-3.0-or-later
"""The intermediate .chiplet, written from plain facts.

One emitter for the two sources of those facts: a loaded KiCad board
(chiplet_writer.read_board_facts) and the routed documents
(routed.facts_from_documents). It imports no pcbnew, so the routed path can
write the same bytes without a board; test_byte_exact_writers.py holds the
board path's bytes to the C++ exporter.

Coordinates are integer internal units (nanometers), in the intermediate
frame: x from the left edge of the board's bounding box, y up from its
bottom edge. The emitter converts them as the C++ exporter does (iu2um).
"""

import os
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from .connection_stacks import emit_interconnect_block
from ._yaml import escape_yaml_dq, yaml_scalar

#: Internal units per micrometer, pcbnew.PCB_IU_PER_MM / 1000.
IU_PER_UM = 1000.0

#: The interposer's technology id, fixed by the C++ exporter.
INTERPOSER_TECH_ID = "intm4tm2"

#: The interposer adapter when the project names none.
DEFAULT_INTERPOSER_ADAPTER = "intm4tm2"

#: The PDK layer the worker draws every io_pad on, as both files name it.
IO_PAD_LAYER = "TopMetal2"

#: The io_pad usage classes, the closed vocabulary of both the .chiplet and
#: io_pads.json. The .chiplet reader throws on any other (IOPad.cpp
#: string_to_io_class) and the finalizer does not normalize them on the
#: no-sidecar path, so a typo is refused at export, as the C++ exporter does.
IO_CLASSES = ("wire_bond", "flipped_bump", "tsv_bump")


def iu_to_um(iu):
    """Internal units to micrometers. Mirrors iu2um() in the C++ exporter."""
    return iu / IU_PER_UM


@dataclass(frozen=True)
class FrameFacts:
    """The size of the board's bounding box, in internal units."""
    width_iu: int
    height_iu: int


@dataclass(frozen=True)
class CarrierFacts:
    """A carrier emitted under interposer.io_pads."""
    ident: str
    io_class: str
    net: str
    size_um: Tuple[float, float]
    x_iu: int
    y_iu: int


@dataclass(frozen=True)
class DieFacts:
    """A die component. ``orientation`` is the board's free text; the
    emitter refuses any value but "", face_up and flip_chip."""
    ref: str
    technology: Optional[str]
    layout: str
    orientation: str
    width_um: float
    height_um: float
    x_iu: int
    y_iu: int
    front: bool
    rotation_deg: float


@dataclass(frozen=True)
class ChipletFacts:
    """Everything the intermediate .chiplet says, in emission order."""
    board_name: str
    interposer_adapter: str
    interconnect_adapter: str
    tech_map: Mapping[str, str]
    frame: FrameFacts
    thickness_iu: int
    carriers: Tuple[CarrierFacts, ...]
    dies: Tuple[DieFacts, ...]


def assembly_name(facts, output_path):
    """The board name, else the output's base name, else a literal, so the
    reader's non-empty assembly.name rule holds."""
    name = facts.board_name
    if not name:
        name = os.path.splitext(os.path.basename(output_path))[0]
    return name or "assembly"


def emit_intermediate_chiplet(facts, output_path, connection_stacks):
    """Write ``facts`` to ``output_path`` as an intermediate .chiplet.

    ``connection_stacks`` is the block connection_stacks.
    emit_connection_stacks_block wrote from the run's interconnect manifest.

    Returns True. Raises OSError if the path is not writable, and ValueError
    for a die ORIENTATION outside the frame contract (with the file already
    open, as the C++ exporter does).
    """
    board_name = assembly_name(facts, output_path)

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
        # DRC should resolve when this design is checked. Validated by the
        # fact source, so a malformed id fails before this file is opened.
        f.write("interposer:\n")
        f.write('  adapter: "%s"\n' % escape_yaml_dq(facts.interposer_adapter))
        f.write("\n")

        # Interconnect adapter (optional second axis); emitted only when
        # set, mirroring interposer.
        f.write(emit_interconnect_block(facts.interconnect_adapter))

        # Technologies. Sorted by id to match the C++ exporter, whose techMap
        # is a std::map (sorted iteration); a plain dict here would emit in
        # insertion order and break byte-exact parity for multi-PDK assemblies.
        f.write("technologies:\n")
        for tech_id, lyp_path in sorted(facts.tech_map.items()):
            f.write("  %s:\n" % yaml_scalar(tech_id))
            f.write('    description: "Imported from KiCad"\n')
            f.write('    layer_properties: "%s"\n' % escape_yaml_dq(lyp_path))
            f.write("    dbu: 0.001\n")
            f.write("\n")

        # Connection stacks (default bump library). Sourced from the
        # interconnect PDK manifest (single source of truth); byte-identical to
        # the prior hardcoded literal so the C++ byte-exact parity gate (47.7b)
        # stays green. export_chiplet.cpp still emits the same literal.
        f.write(connection_stacks)
        f.write("\n")

        # Components
        f.write("components:\n")

        # 1) Interposer (always first; placeholder position in intermediate frame).
        width_um = iu_to_um(facts.frame.width_iu)
        height_um = iu_to_um(facts.frame.height_iu)
        thickness_um = iu_to_um(facts.thickness_iu)

        f.write("  - id: interposer\n")
        f.write("    type: interposer\n")
        # See coord_frame_contract.md section 2.
        f.write("    anchor: bbox_center\n")
        if INTERPOSER_TECH_ID in facts.tech_map:
            f.write("    technology: %s\n" % yaml_scalar(INTERPOSER_TECH_ID))

        gds_name = "%s.gds" % board_name
        f.write('    layout: "%s"\n' % escape_yaml_dq(gds_name))
        f.write('    top_cell: "INTERPOSER"\n')
        f.write("    dimensions:\n")
        f.write("      width: %.6f\n" % width_um)
        f.write("      height: %.6f\n" % height_um)
        f.write("      thickness: %.6f\n" % thickness_um)
        # Position placeholder; hyp_to_gds.py --update-chiplet-file fills
        # the canonical GDS-bbox-corner value. See coord_frame_contract.md
        # section 4, item 1.
        f.write("    position:\n")
        f.write("      x: 0.0\n")
        f.write("      y: 0.0\n")
        f.write("      z: 0.0\n")

        # 1b) IO_CLASS footprints nested under interposer.io_pads.
        if facts.carriers:
            f.write("    io_pads:\n")
            for pad in facts.carriers:
                size_x_um, size_y_um = pad.size_um
                x_um = iu_to_um(pad.x_iu)
                y_um = iu_to_um(pad.y_iu)

                f.write("      - id: %s\n" % yaml_scalar(pad.ident))
                f.write("        io_class: %s\n" % yaml_scalar(pad.io_class))
                f.write('        net: "%s"\n' % escape_yaml_dq(pad.net))
                f.write(
                    "        position: { x: %.6f, y: %.6f }\n"
                    % (x_um, y_um)
                )
                f.write(
                    "        size: { x: %.3f, y: %.3f }\n"
                    % (size_x_um, size_y_um)
                )
                f.write("        layer: %s\n" % IO_PAD_LAYER)
        f.write("\n")

        # 2) Die components.
        for die in facts.dies:
            ref = die.ref
            orient = die.orientation
            # ORIENTATION is a free-text footprint field. The frame contract
            # defines only face_up (the default) and flip_chip; an empty field
            # means face_up. Any other token (a typo, or the non-canonical
            # "face_down") would otherwise fall through to a non-flip die that
            # is silently emitted un-mirrored and without its connection stack.
            # Fail loudly at export, as an IO_CLASS is (read_io_pads).
            if orient not in ("", "face_up", "flip_chip"):
                raise ValueError(
                    "Footprint %s has an unrecognized ORIENTATION '%s'; expected "
                    "face_up or flip_chip (use flip_chip, not face_down)."
                    % (ref, orient)
                )
            is_flip_chip = (orient == "flip_chip")

            f.write("  - id: %s\n" % yaml_scalar(ref))
            f.write("    type: die\n")
            # See coord_frame_contract.md section 2: dies use
            # gds_origin as the mesh anchor.
            f.write("    anchor: gds_origin\n")

            if die.technology is not None:
                f.write("    technology: %s\n" % yaml_scalar(die.technology))

            if is_flip_chip:
                f.write("    connection: cupillar_opt2\n")

            if die.layout:
                f.write('    layout: "%s"\n' % escape_yaml_dq(die.layout))

            if is_flip_chip:
                f.write("    orientation: flip_chip\n")

            f.write("    dimensions:\n")
            f.write("      width: %.6f\n" % die.width_um)
            f.write("      height: %.6f\n" % die.height_um)
            f.write("      thickness: 0.0\n")

            f.write("    position:\n")
            f.write("      x: %.6f\n" % iu_to_um(die.x_iu))
            f.write("      y: %.6f\n" % iu_to_um(die.y_iu))

            if is_flip_chip:
                # Z auto-calculated by Chiplet Studio from the connection
                # stack height.
                f.write("      z: 0.0\n")
            elif die.front:
                f.write("      z: %.6f\n" % thickness_um)
            else:
                f.write("      z: 0.0\n")

            f.write("    rotation:\n")
            f.write("      z: %.4f\n" % die.rotation_deg)
            f.write("\n")

    return True
