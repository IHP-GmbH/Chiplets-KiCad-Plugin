# SPDX-License-Identifier: GPL-3.0-or-later
"""The worker's reader of interposer-pnr's routed geometry (--routed-geometry):
the document, checked by routed_document, as the board model HYPParser fills
from a .hyp."""

import sys
from typing import Dict, List, Tuple

try:
    from . import io_pads_manifest as _io_pads_manifest
    from . import routed_document as _routed_document
    from .hyp_model import (Device, Padstack, PerimeterSegment, Pin, TraceArc,
                            TraceSegment, Via)
except ImportError:  # Standalone worker script.
    import io_pads_manifest as _io_pads_manifest
    import routed_document as _routed_document
    from hyp_model import (Device, Padstack, PerimeterSegment, Pin, TraceArc,
                           TraceSegment, Via)


class RoutedGeometryParser:
    """HYPParser's model, read from interposer-pnr's routed geometry
    (--routed-geometry) instead of a .hyp.

    It fills the fields HYPParser.parse() fills, in the units and order the
    board path's .hyp gives them, so nothing downstream of the parse tells the
    two apart (tests/test_routed_model.py holds the two models equal on the
    typical_03 golden):

    - units METRIC (metres): the document's micrometres times 1e-6;
    - perimeter from ``outline``, closed, in outline order;
    - stackup top to bottom: ``stack[].metal`` reversed;
    - devices in document order, at ``origin`` (no pin centroid), with the
      layout the document binds after its sha256 check; ``layer`` is the top
      metal, where the board places every die footprint. ``mirrored`` is not
      a parser field: the composer reads flip from the .chiplet, which the
      export wrote from it;
    - nets sorted by ``board_name`` (the .hyp orders its NET blocks by the
      board's net name); within a net its segments in document order, then
      its drawn vias in document order;
    - one padstack per distinct (layers, cut, pad), first seen;
    - pins are refs only (die pads from ``pad_domains``, carriers from the
      --io-pads manifest), with no position: they feed the standalone-pad
      check and draw nothing.

    Every refusal (RoutedGeometryRefused) comes from parse(), before anything
    is drawn or written.
    """

    UM_TO_M = 1e-6

    def __init__(self, document_path: str, io_pads_path: str = ""):
        self.hyp_path = document_path  # the input the run log names
        self.io_pads_path = io_pads_path or ""
        self.segments: List[TraceSegment] = []
        self.arcs: List[TraceArc] = []  # a routed document carries none
        self.vias: List[Via] = []
        self.padstacks: Dict[int, Padstack] = {}
        self.devices: List[Device] = []
        self.pins: List[Pin] = []
        self.units: str = "METRIC"
        self.stackup_layers: List[str] = []
        self.perimeter_segments: List[PerimeterSegment] = []
        self.board_path: str = ""
        #: (net board name, drawn record) per via, in self.vias order: the
        #: cut arrays the via table cross-check holds against the engine's.
        self.drawn_vias: List[Tuple[str, Dict]] = []
        #: {ref: (x, y)} each device's origin in micrometres as the document
        #: writes it: what the .chiplet's die positions are finalized from.
        self.device_origins_um: Dict[str, Tuple[float, float]] = {}

    def parse(self) -> None:
        on_warn = lambda note: print(f"Warning: {note}", file=sys.stderr)  # noqa: E731
        doc = _routed_document.read_document(self.hyp_path, on_warn=on_warn)
        files = _routed_document.bound_files(doc, self.hyp_path)
        m = self.UM_TO_M
        metals = _routed_document.stack_metals(doc)
        self.stackup_layers = list(reversed(metals))
        outline = doc["outline"]
        for (x1, y1), (x2, y2) in zip(outline, outline[1:] + outline[:1]):
            self.perimeter_segments.append(
                PerimeterSegment(x1=x1 * m, y1=y1 * m, x2=x2 * m, y2=y2 * m))
        for device in doc["devices"]:
            x, y = device["origin"]
            self.device_origins_um[device["ref"]] = (float(x), float(y))
            self.devices.append(Device(
                ref=device["ref"], layer=metals[-1],
                gds_file=files[device["ref"]]["layout"],
                x=x * m, y=y * m, rotation=float(device["rotation_deg"])))
            for pad in (device["pad_domains"].get("attachments") or []):
                self.pins.append(Pin(x=None, y=None, padstack_index=None,
                                     ref="%s.%s" % (device["ref"], pad["pad_number"])))
        if self.io_pads_path:
            carriers = _io_pads_manifest.read(
                self.io_pads_path, on_warn=on_warn,
                frame=_io_pads_manifest.CANONICAL_FRAME)
            for pad in carriers:
                self.pins.append(Pin(x=None, y=None, padstack_index=None,
                                     ref=str(pad.get("ref", ""))))
        stacks: Dict[Tuple, int] = {}
        for net in sorted(doc["nets"], key=lambda n: n["board_name"]):
            name = net["board_name"]
            for seg in net["segments"]:
                self.segments.append(TraceSegment(
                    net_name=name, x1=seg["x0"] * m, y1=seg["y0"] * m,
                    x2=seg["x1"] * m, y2=seg["y1"] * m,
                    width=seg["width"] * m, layer=seg["layer"]))
            for drawn in net["drawn"]:
                layers = (drawn["top_layer"], drawn["bottom_layer"])
                key = (layers, drawn["cut"], tuple(drawn["pad"]))
                if key not in stacks:
                    stacks[key] = len(stacks)
                    self.padstacks[stacks[key]] = Padstack(
                        index=stacks[key], drill=drawn["cut"] * m,
                        layers=list(layers), pad_width=drawn["pad"][0] * m,
                        pad_height=drawn["pad"][1] * m)
                self.vias.append(Via(net_name=name, x=drawn["x"] * m,
                                     y=drawn["y"] * m,
                                     padstack_index=stacks[key]))
                self.drawn_vias.append((name, drawn))
