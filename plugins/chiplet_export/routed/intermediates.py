# SPDX-License-Identifier: GPL-3.0-or-later
"""The board path's pre-worker intermediates, computed from the routed documents.

What each one equals on the board path (pipeline/orchestrator.py):

- the intermediate .chiplet: write_chiplet(board), through the same emitter;
- the die pin lists: write_die_pin_lists(board), decoded from each device's
  pad-domain archive, the field the board's CHIPLET_PAD_DOMAINS holds;
- the die connections and thicknesses: read_die_connections and
  read_die_thicknesses, from the .chiplet the board was drawn from;
- the CMIM sidecar: none. The routed path draws no CMIM, and an explicit one is
  refused;
- the no-fill sidecar: none beyond the one the caller already passes.

Every input is checked, and every archive decoded, before the first file is
written. An I/O error while writing can still leave the .chiplet without its
pin lists; the caller writes into a fresh folder.
"""

import json
import math
import os
from dataclasses import dataclass
from typing import Dict, Optional

from .. import io_pads_manifest as _io_pads_manifest
from ..pad_domains import PadDomainError, archive_die_dimensions, archive_pin_list
from ..writers.connection_stacks import (
    emit_connection_stacks_block, validate_adapter_id, validate_interconnect_ids,
)
from ..writers.intermediate_chiplet import (
    DEFAULT_INTERPOSER_ADAPTER, INTERPOSER_TECH_ID, IO_CLASSES, IO_PAD_LAYER,
    IU_PER_UM, CarrierFacts, ChipletFacts, DieFacts, FrameFacts,
    emit_intermediate_chiplet,
)
from ..writers._units import _size_um, parse_thickness_um
from .document import RoutedRefused, check_chiplet_binding, read_document

#: The stroke interposer-pnr draws the board outline with (kicad_export.py,
#: the Edge.Cuts gr_rect). The board path's frame is the outline's bounding
#: box, which KiCad grows by half that stroke on every side; the intermediate
#: frame of this path is grown the same, so both write the same coordinates to
#: the nanometer.
BOARD_EDGE_STROKE_UM = 100.0

#: The frame of the routed io_pads manifest. Its origin is the document's
#: (the outline's lower-left corner, which check_document holds at 0, 0).
IO_PADS_FRAME = "gds_bbox_corner_um"


def _iu(value_um):
    """Micrometers to integer internal units (nm), as a board stores them."""
    return int(round(value_um * IU_PER_UM))


def normalize_180(degrees):
    """The angle in (-180, 180], as KiCad stores a footprint's orientation
    (EDA_ANGLE::Normalize180, through FOOTPRINT::SetOrientation)."""
    value = float(degrees)
    while value <= -180.0:
        value += 360.0
    while value > 180.0:
        value -= 360.0
    # -0.0 + 0.0 is 0.0: KiCad never stores a negative zero.
    return value + 0.0


def _components(chiplet):
    components = chiplet.get("components") if isinstance(chiplet, dict) else None
    if not isinstance(components, list):
        raise RoutedRefused("the .chiplet lists no components")
    return {c.get("id"): c for c in components if isinstance(c, dict)}


def _device_components(document, chiplet):
    """[(device, component)] in devices[] order. Every ref must be a component,
    and every die of the .chiplet a device."""
    components = _components(chiplet)
    pairs = []
    for device in document["devices"]:
        component = components.get(device["ref"])
        if component is None:
            raise RoutedRefused(
                "device %s is no component of the .chiplet" % device["ref"])
        pairs.append((device, component))
    placed = {device["ref"] for device in document["devices"]}
    missing = sorted(str(ident) for ident, component in components.items()
                     if component.get("type") == "die" and ident not in placed)
    if missing:
        raise RoutedRefused("die %s of the .chiplet has no device in the routed "
                            "geometry" % ", ".join(missing))
    return pairs


def _connection(component):
    value = component.get("connection")
    return value.strip() if isinstance(value, str) else ""


def _orientation(device):
    return "flip_chip" if device["mirrored"] else "face_up"


def _check_carrier(pad, index):
    where = "io_pads[%d]" % index
    if not isinstance(pad, dict):
        raise RoutedRefused("%s must be an object" % where)
    for key in ("ref", "io_class", "net"):
        if not isinstance(pad.get(key), str) or not pad[key]:
            raise RoutedRefused("%s has no %s" % (where, key))
    for key in ("x_um", "y_um", "size_x_um", "size_y_um"):
        value = pad.get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise RoutedRefused("%s (%s) %s must be a finite number, not %r"
                                % (where, pad["ref"], key, value))
    # The board path refuses the same (io_pad_size_um); the .chiplet reader
    # throws on any other class.
    if pad["io_class"] not in IO_CLASSES:
        raise RoutedRefused("io_pad %s has io_class %r, not one of %s"
                            % (pad["ref"], pad["io_class"], ", ".join(IO_CLASSES)))
    # The emitter writes every carrier on IO_PAD_LAYER; another layer would be
    # written as that one without a word.
    if pad.get("layer", IO_PAD_LAYER) != IO_PAD_LAYER:
        raise RoutedRefused("io_pad %s is on %r; the .chiplet carries io_pads on %s"
                            % (pad["ref"], pad["layer"], IO_PAD_LAYER))


def _frame(document):
    """(x_lo, y_lo, width, height) of the intermediate frame, in iu."""
    half = _iu(BOARD_EDGE_STROKE_UM / 2.0)
    xs = [_iu(x) for x, _y in document["outline"]]
    ys = [_iu(y) for _x, y in document["outline"]]
    x_lo, y_lo = min(xs) - half, min(ys) - half
    return x_lo, y_lo, max(xs) + half - x_lo, max(ys) + half - y_lo


def facts_from_documents(document, chiplet, io_pads, *, board_name,
                         interconnect_manifest=None,
                         interposer_adapter=DEFAULT_INTERPOSER_ADAPTER,
                         interconnect_adapter="", interposer_lyp=""):
    """The ChipletFacts read_board_facts returns for the board these
    documents describe.

    ``document`` is a checked routed-geometry document, ``chiplet`` the
    parsed .chiplet it binds, ``io_pads`` the routed io_pads manifest's
    entries in its order. The adapters and the interposer layer sheet are the
    board's project text variables on the board path, so they are arguments
    here; a flow draft's project has none, hence the defaults. Raises
    RoutedRefused, or ValueError for an adapter id the board path refuses too.
    """
    validate_adapter_id(interconnect_adapter, "interconnect adapter")
    validate_interconnect_ids(adapter=interconnect_adapter,
                              manifest=interconnect_manifest)
    validate_adapter_id(interposer_adapter, "interposer adapter")

    x_lo, y_lo, width, height = _frame(document)

    dies = []
    for device, component in _device_components(document, chiplet):
        ref = device["ref"]
        try:
            width_um, height_um = archive_die_dimensions(
                device["pad_domains"], ref, _connection(component))
        except PadDomainError as exc:
            raise RoutedRefused("device %s: %s" % (ref, exc)) from exc
        x_um, y_um = device["origin"]
        dies.append(DieFacts(
            ref=ref,
            # The board's dies carry no LYP_FILE, so the board path names no
            # die technology; this path writes the same.
            technology=None,
            layout=device["layout"]["path"],
            orientation=_orientation(device),
            width_um=width_um, height_um=height_um,
            x_iu=_iu(x_um) - x_lo, y_iu=_iu(y_um) - y_lo,
            # interposer-pnr places every footprint on F.Cu; the flip is in
            # ORIENTATION and the pad x coordinates.
            front=True,
            rotation_deg=normalize_180(device["rotation_deg"])))

    carriers, seen = [], set()
    for index, pad in enumerate(io_pads):
        _check_carrier(pad, index)
        if pad["ref"] in seen:
            raise RoutedRefused("io_pad %s is listed twice" % pad["ref"])
        seen.add(pad["ref"])
        size = _size_um(pad["size_x_um"], pad["size_y_um"])
        if size is None:
            raise RoutedRefused("io_pad %s has no size above zero" % pad["ref"])
        carriers.append(CarrierFacts(
            ident=pad["ref"], io_class=pad["io_class"], net=pad["net"],
            size_um=size,
            x_iu=_iu(pad["x_um"]) - x_lo, y_iu=_iu(pad["y_um"]) - y_lo))

    return ChipletFacts(
        board_name=board_name,
        interposer_adapter=interposer_adapter,
        interconnect_adapter=interconnect_adapter,
        tech_map={INTERPOSER_TECH_ID: interposer_lyp} if interposer_lyp else {},
        frame=FrameFacts(width_iu=width, height_iu=height),
        thickness_iu=_iu(document["interposer"]["body_thickness_um"]),
        carriers=tuple(carriers),
        dies=tuple(dies))


def die_pin_lists(document, chiplet, method_specs=None):
    """{ref: bump sidecar} as write_die_pin_lists writes it, in devices[] order.

    ``method_specs`` is what the board preflight holds each archive against
    (connection_method_specs); given, an attachment that differs is refused.
    """
    result = {}
    for device, component in _device_components(document, chiplet):
        ref = device["ref"]
        try:
            result[ref] = archive_pin_list(device["pad_domains"], ref,
                                           _connection(component),
                                           _orientation(device), method_specs)
        except PadDomainError as exc:
            raise RoutedRefused("device %s: %s" % (ref, exc)) from exc
    return result


def die_connections(document, chiplet):
    """{ref: method} as read_die_connections returns it: every device whose
    component names a connection."""
    return {device["ref"]: _connection(component)
            for device, component in _device_components(document, chiplet)
            if _connection(component)}


def die_thicknesses(document, chiplet):
    """{ref: um} as read_die_thicknesses returns it.

    interposer-pnr writes DIE_THICKNESS_UM as ``"%g" % thickness`` and only
    when the thickness is above 0 (kicad_export.die_properties); the board
    path parses that text. The same text is parsed here.
    """
    result = {}
    for device, component in _device_components(document, chiplet):
        thickness = (component.get("dimensions") or {}).get("thickness")
        if type(thickness) not in (int, float) or not thickness > 0:
            continue
        value = parse_thickness_um("%g" % thickness)
        if value is not None:
            result[device["ref"]] = value
    return result


@dataclass(frozen=True)
class RoutedIntermediates:
    """What write_intermediates wrote: the board path's pre-worker
    intermediates, for a caller to hand to the workers in their place."""
    chiplet: str
    pin_lists: Dict[str, str]
    die_connections: Dict[str, str]
    die_thicknesses: Dict[str, float]
    io_pads: int
    cmim_devices: int = 0


def write_intermediates(document_path, chiplet_path, io_pads_path, *,
                        chiplet_output, pins_dir, board_name,
                        interconnect_manifest=None,
                        interposer_adapter=DEFAULT_INTERPOSER_ADAPTER,
                        interconnect_adapter="", interposer_lyp="",
                        cmim_devices_json=None, method_specs=None, on_warn=None):
    """Write the intermediate .chiplet to ``chiplet_output`` and one
    ``<ref>_pins.json`` per device into ``pins_dir``.

    Refuses (RoutedRefused), before any file is written: a document this
    reader does not read, a .chiplet other than the one it binds, a device
    that is no .chiplet component or whose archive does not decode, an io_pads
    manifest in another frame, a missing ``pins_dir``, and any CMIM sidecar.
    """
    if cmim_devices_json:
        raise RoutedRefused(
            "a CMIM sidecar (%s) was given, but the routed path draws no "
            "integrated capacitor; it is computed on the board path only"
            % cmim_devices_json)
    document = read_document(document_path, on_warn=on_warn)
    check_chiplet_binding(document, chiplet_path)
    from .. import vendored as _vendored
    cfio = _vendored.chiplet_format_io()
    try:
        chiplet = cfio.load(chiplet_path)
    except (OSError, cfio.ChipletFormatError) as exc:
        raise RoutedRefused("cannot read %s: %s" % (chiplet_path, exc)) from exc
    try:
        io_pads = _io_pads_manifest.read(io_pads_path, on_warn=on_warn,
                                         frame=IO_PADS_FRAME)
    except _io_pads_manifest.IoPadsRefused as exc:
        raise RoutedRefused(str(exc)) from exc

    facts = facts_from_documents(
        document, chiplet, io_pads, board_name=board_name,
        interconnect_manifest=interconnect_manifest,
        interposer_adapter=interposer_adapter,
        interconnect_adapter=interconnect_adapter, interposer_lyp=interposer_lyp)
    pin_lists = die_pin_lists(document, chiplet, method_specs)
    connections = die_connections(document, chiplet)
    thicknesses = die_thicknesses(document, chiplet)
    stacks = emit_connection_stacks_block(interconnect_manifest)
    if not os.path.isdir(pins_dir):
        raise RoutedRefused("the pin list folder %s does not exist" % pins_dir)

    emit_intermediate_chiplet(facts, chiplet_output, stacks)
    paths = {}
    for ref, sidecar in pin_lists.items():
        path = os.path.join(pins_dir, "%s_pins.json" % ref)
        with open(path, "w") as f:
            json.dump(sidecar, f, indent=2)
        paths[ref] = path
    return RoutedIntermediates(
        chiplet=chiplet_output, pin_lists=paths, die_connections=connections,
        die_thicknesses=thicknesses, io_pads=len(io_pads))
