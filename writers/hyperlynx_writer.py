# SPDX-License-Identifier: GPL-2.0-or-later
#
# Derivative work of kicad/pcbnew/exporters/export_hyperlynx.cpp,
# Copyright (C) 2019 CERN and Copyright The KiCad Developers
# (see AUTHORS.txt in upstream KiCad). Distributed under the GNU
# General Public License version 2 or later.
"""
Hyperlynx .hyp writer (metric / METERS variant).

Replicates kicad/pcbnew/exporters/export_hyperlynx.cpp in pure
Python via the pcbnew SWIG bindings. The metric patch (units in
meters, GDS_FILE field on devices) is preserved so the output
remains a drop-in for hyp_to_gds.py.

Output structure mirrors the C++ exporter:

  {VERSION=2.14}
  {UNITS=METRIC LENGTH}
  {BOARD "<filename>"  (PERIMETER_SEGMENT ...) }
  {STACKUP (SIGNAL ...) (DIELECTRIC ...) }
  {DEVICES (? REF=... L=... X=... Y=... R=... [GDS_FILE=...]) }
  {PADSTACK=<id>, <drill> ("layer", shape, sx, sy, angle, M) ... }
  {NET="<name>" (PIN ...|VIA ...|SEG ...|ARC ...|{POLYGON ...}) }
"""

import sys

import pcbnew


# C++ PAD_SHAPE enum -> Hyperlynx shape id (line 159-187 in
# export_hyperlynx.cpp).  Anything not in this map falls back to
# oval (shape id 0) with a warning, matching the C++ default branch.
_SHAPE_MAP = {
    pcbnew.PAD_SHAPE_CIRCLE: 0,
    pcbnew.PAD_SHAPE_OVAL: 0,
    pcbnew.PAD_SHAPE_ROUNDRECT: 2,
    pcbnew.PAD_SHAPE_RECT: 1,
}


def _iu_to_hyp(iu):
    """KiCad internal units (nm) to Hyperlynx METERS.

    Mirrors iu2hyp() in the C++ exporter (line 50-54).  PCB_IU_PER_MM
    is exposed by SWIG; dividing by an extra factor of 1000 converts
    millimetres to metres.
    """
    return iu / (pcbnew.PCB_IU_PER_MM * 1000.0)


class _PadStack:
    """Pad/via shape descriptor with equality semantics for dedup.

    Mirrors HYPERLYNX_PAD_STACK (lines 59-125, 220-250).  Two stacks
    compare equal when shape, type, sizes, layers, and angle match
    (plus drill if both are through).  The exporter deduplicates
    stacks via this comparison so the resulting .hyp emits one
    {PADSTACK=...} block per unique geometry.
    """

    __slots__ = ("board", "id", "drill", "shape", "sx", "sy",
                 "angle", "layers", "type")

    def __init__(self):
        self.board = None
        self.id = 0
        self.drill = 0
        self.shape = None
        self.sx = 0
        self.sy = 0
        self.angle = 0.0
        self.layers = None
        self.type = None

    @classmethod
    def from_pad(cls, board, pad):
        ps = cls()
        ps.board = board
        # PADSTACK::ALL_LAYERS is defined as F_Cu (padstack.h:145),
        # so layer-aware getters with F_Cu match the C++ behaviour.
        ps.sx = pad.GetSizeX()
        ps.sy = pad.GetSizeY()
        angle = 180.0 - pad.GetOrientation().AsDegrees()
        if angle < 0.0:
            angle += 360.0
        ps.angle = angle
        ps.layers = pad.GetLayerSet()
        ps.drill = pad.GetDrillSize().x
        ps.shape = pad.GetShape(pcbnew.F_Cu)
        # Hardcoded in C++ (line 234); the exporter treats all pads
        # as through-hole for stack comparison purposes.
        ps.type = pcbnew.PAD_ATTRIB_PTH
        return ps

    @classmethod
    def from_via(cls, board, via):
        ps = cls()
        ps.board = board
        ps.sx = via.GetWidth(pcbnew.F_Cu)
        ps.sy = ps.sx
        ps.angle = 0.0
        ps.layers = via.GetLayerSet()
        ps.drill = via.GetDrillValue()
        ps.shape = pcbnew.PAD_SHAPE_CIRCLE
        ps.type = pcbnew.PAD_ATTRIB_PTH
        return ps

    def is_through(self):
        return self.type in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH)

    def _layers_key(self):
        """Stable tuple of sorted layer IDs (LSET equality fallback)."""
        try:
            return tuple(sorted(int(l) for l in self.layers.Seq()))
        except Exception:
            return None

    def __eq__(self, other):
        if not isinstance(other, _PadStack):
            return NotImplemented
        if self.shape != other.shape:
            return False
        if self.type != other.type:
            return False
        if self.is_through() and other.is_through() and self.drill != other.drill:
            return False
        if self.sx != other.sx:
            return False
        if self.sy != other.sy:
            return False
        if self._layers_key() != other._layers_key():
            return False
        if self.angle != other.angle:
            return False
        return True


class _HyperlynxExporter:
    """Stateful Hyperlynx writer, mirroring HYPERLYNX_EXPORTER."""

    def __init__(self, board, output_path):
        self.board = board
        self.output_path = output_path
        self.pad_stacks = []
        self.poly_id = 1
        self.f = None

    def write(self):
        with open(self.output_path, "w", encoding="utf-8") as f:
            self.f = f
            if not self._generate_headers():
                return False
            if not self._write_board_info():
                return False
            self._write_stackup_info()
            self._write_devices()
            self._write_pad_stacks()
            self._write_nets()
        return True

    def _print(self, level, text):
        """Indented write.

        Matches OUTPUTFORMATTER::Print at richio.cpp:465-491 which
        prefixes the formatted output with NESTWIDTH=2 spaces per
        nestLevel.
        """
        self.f.write("  " * level + text)

    def _add_pad_stack(self, stack):
        """Insert or return existing equal stack. Assigns id on insert."""
        for p in self.pad_stacks:
            if p == stack:
                return p
        stack.id = len(self.pad_stacks)
        self.pad_stacks.append(stack)
        return stack

    def _format_pad_shape(self, stack):
        if stack.shape in _SHAPE_MAP:
            shape_id = _SHAPE_MAP[stack.shape]
        else:
            sys.stderr.write(
                "Warning: Pad shape not supported by the Hyperlynx "
                "exporter (supported: circle, oval, rectangle, "
                "rounded rectangle); exported as oval.\n"
            )
            shape_id = 0
        return "%d, %.9f, %.9f, %.1f, M" % (
            shape_id,
            _iu_to_hyp(stack.sx),
            _iu_to_hyp(stack.sy),
            stack.angle,
        )

    def _generate_headers(self):
        self._print(0, "{VERSION=2.14}\n")
        self._print(0, "{UNITS=METRIC LENGTH}\n\n")
        return True

    def _write_board_info(self):
        outlines = pcbnew.SHAPE_POLY_SET()
        self._print(0, '{BOARD "%s"\n' % self.board.GetFileName())
        if not self.board.GetBoardPolygonOutlines(outlines):
            sys.stderr.write(
                "Error: Board outline is malformed. Run DRC for a "
                "full analysis.\n"
            )
            self._print(0, "}\n\n")
            return False
        for o in range(outlines.OutlineCount()):
            outl = outlines.COutline(o)
            for i in range(outl.SegmentCount()):
                seg = outl.CSegment(i)
                self._print(
                    1,
                    "(PERIMETER_SEGMENT X1=%.9f Y1=%.9f "
                    "X2=%.9f Y2=%.9f)\n" % (
                        _iu_to_hyp(seg.A.x),
                        _iu_to_hyp(-seg.A.y),
                        _iu_to_hyp(seg.B.x),
                        _iu_to_hyp(-seg.B.y),
                    ),
                )
        self._print(0, "}\n\n")
        return True

    def _write_stackup_info(self):
        stackup = self.board.GetDesignSettings().GetStackupDescriptor()
        self._print(0, "{STACKUP\n")
        layer_name = ""  # last copper layer name; dielectrics inherit it
        for item in stackup.GetList():
            t = item.GetType()
            if t == pcbnew.BS_ITEM_TYPE_COPPER:
                layer_name = self.board.GetLayerName(item.GetBrdLayerId())
                plating_thickness = 0
                resistivity = 1.724e-8  # Good for copper
                self._print(
                    1,
                    '(SIGNAL T=%g P=%g C=%g L="%.20s" M=COPPER)\n' % (
                        _iu_to_hyp(item.GetThickness(0)),
                        _iu_to_hyp(plating_thickness),
                        resistivity,
                        layer_name,
                    ),
                )
            elif t == pcbnew.BS_ITEM_TYPE_DIELECTRIC:
                count = item.GetSublayersCount()
                if count < 2:
                    self._print(
                        1,
                        '(DIELECTRIC T=%g C=%g L="DE_%.17s" M="%.20s")\n' % (
                            _iu_to_hyp(item.GetThickness(0)),
                            item.GetEpsilonR(0),
                            layer_name,
                            item.GetMaterial(0),
                        ),
                    )
                else:
                    for idx in range(count):
                        self._print(
                            1,
                            '(DIELECTRIC T=%g C=%g L="DE%d_%.16s" '
                            'M="%.20s")\n' % (
                                _iu_to_hyp(item.GetThickness(idx)),
                                item.GetEpsilonR(idx),
                                idx,
                                layer_name,
                                item.GetMaterial(idx),
                            ),
                        )
        self._print(0, "}\n\n")
        return True

    def _write_devices(self):
        self._print(0, "{DEVICES\n")
        for footprint in list(self.board.Footprints()):
            ref = footprint.GetReference()
            if not ref:
                ref = "EMPTY"
            layer_name = self.board.GetLayerName(footprint.GetLayer())
            gds_path = ""
            if footprint.HasField("GDS_FILE"):
                gds_path = footprint.GetFieldText("GDS_FILE")
            pos = footprint.GetPosition()
            x = _iu_to_hyp(pos.x)
            y = _iu_to_hyp(-pos.y)  # negate Y to match wire coord system
            rot = footprint.GetOrientation().AsDegrees()
            if gds_path:
                self._print(
                    1,
                    '(? REF="%s" L="%s" X=%.9f Y=%.9f R=%.2f '
                    'GDS_FILE="%s")\n' % (
                        ref, layer_name, x, y, rot, gds_path,
                    ),
                )
            else:
                self._print(
                    1,
                    '(? REF="%s" L="%s" X=%.9f Y=%.9f R=%.2f)\n' % (
                        ref, layer_name, x, y, rot,
                    ),
                )
        self._print(0, "}\n\n")
        return True

    def _write_pad_stacks(self):
        for footprint in list(self.board.Footprints()):
            for pad in list(footprint.Pads()):
                self._add_pad_stack(_PadStack.from_pad(self.board, pad))
        for track in list(self.board.Tracks()):
            if track.Type() == pcbnew.PCB_VIA_T:
                via = track.Cast()
                self._add_pad_stack(_PadStack.from_via(self.board, via))
        for stack in self.pad_stacks:
            self._write_single_pad_stack(stack)
        return True

    def _write_single_pad_stack(self, stack):
        cu_count = self.board.GetCopperLayerCount()
        allowed_cu = pcbnew.LSET.AllCuMask(cu_count)
        out_layers = [l for l in stack.layers.CuStack()
                      if allowed_cu.Contains(l)]
        if not out_layers:
            return
        self._print(
            0,
            "{PADSTACK=%d, %.9f\n" % (
                stack.id, _iu_to_hyp(stack.drill),
            ),
        )
        shape_str = self._format_pad_shape(stack)
        for layer in out_layers:
            self._print(
                1,
                '("%s", %s)\n' % (
                    self.board.GetLayerName(layer), shape_str,
                ),
            )
        self._print(0, "}\n\n")

    def _has_copper_layer(self, item):
        for _ in item.GetLayerSet().CuStack():
            return True
        return False

    def _collect_net_objects(self, netcode):
        rv = []

        def check(item):
            if not self._has_copper_layer(item):
                return False
            nc = item.GetNetCode()
            if nc == netcode or (netcode < 0 and nc <= 0):
                return True
            return False

        for footprint in list(self.board.Footprints()):
            for pad in list(footprint.Pads()):
                if check(pad):
                    rv.append(pad)
        for track in list(self.board.Tracks()):
            if check(track):
                rv.append(track)
        for zone in list(self.board.Zones()):
            if check(zone):
                rv.append(zone)
        return rv

    def _write_net_objects(self, items):
        for item in items:
            t = item.Type()
            casted = item.Cast() if hasattr(item, "Cast") else item
            if t == pcbnew.PCB_PAD_T:
                self._write_pin(casted)
            elif t == pcbnew.PCB_VIA_T:
                self._write_via(casted)
            elif t == pcbnew.PCB_ARC_T:
                self._write_arc(casted)
            elif t == pcbnew.PCB_TRACE_T:
                self._write_seg(casted)
            elif t == pcbnew.PCB_ZONE_T:
                self._write_zone(casted)
        return True

    def _write_pin(self, pad):
        stack = self._add_pad_stack(_PadStack.from_pad(self.board, pad))
        parent = pad.GetParentFootprint()
        ref = parent.GetReference() if parent is not None else ""
        if not ref:
            ref = "EMPTY"
        pad_name = pad.GetNumber() or "1"
        pos = pad.GetPosition()
        self._print(
            1,
            '(PIN X=%.10f Y=%.10f R="%s.%s" P=%d)\n' % (
                _iu_to_hyp(pos.x),
                _iu_to_hyp(-pos.y),
                ref, pad_name, stack.id,
            ),
        )

    def _write_via(self, via):
        stack = self._add_pad_stack(_PadStack.from_via(self.board, via))
        pos = via.GetPosition()
        self._print(
            1,
            "(VIA X=%.10f Y=%.10f P=%d)\n" % (
                _iu_to_hyp(pos.x), _iu_to_hyp(-pos.y), stack.id,
            ),
        )

    def _write_seg(self, track):
        layer_name = self.board.GetLayerName(track.GetLayer())
        s = track.GetStart()
        e = track.GetEnd()
        self._print(
            1,
            '(SEG X1=%.10f Y1=%.10f X2=%.10f Y2=%.10f W=%.10f '
            'L="%s")\n' % (
                _iu_to_hyp(s.x), _iu_to_hyp(-s.y),
                _iu_to_hyp(e.x), _iu_to_hyp(-e.y),
                _iu_to_hyp(track.GetWidth()),
                layer_name,
            ),
        )

    def _write_arc(self, arc):
        layer_name = self.board.GetLayerName(arc.GetLayer())
        start = arc.GetStart()
        end = arc.GetEnd()
        if arc.IsCCW():
            start, end = end, start
        center = arc.GetCenter()
        self._print(
            1,
            '(ARC X1=%.10f Y1=%.10f X2=%.10f Y2=%.10f XC=%.10f '
            'YC=%.10f R=%.10f W=%.10f L="%s")\n' % (
                _iu_to_hyp(start.x), _iu_to_hyp(-start.y),
                _iu_to_hyp(end.x), _iu_to_hyp(-end.y),
                _iu_to_hyp(center.x), _iu_to_hyp(-center.y),
                _iu_to_hyp(arc.GetRadius()),
                _iu_to_hyp(arc.GetWidth()),
                layer_name,
            ),
        )

    def _write_zone(self, zone):
        for layer in zone.GetLayerSet().Seq():
            if not zone.HasFilledPolysForLayer(layer):
                continue
            layer_name = self.board.GetLayerName(layer)
            fill = zone.GetFilledPolysList(layer).CloneDropTriangulation()
            try:
                fill.Simplify()
            except TypeError:
                # Older binding variant required a strategy argument.
                fill.Simplify(pcbnew.SHAPE_POLY_SET.PM_FAST)
            for i in range(fill.OutlineCount()):
                outl = fill.COutline(i)
                p0 = outl.CPoint(0)
                self._print(
                    1,
                    '{POLYGON T=POUR L="%s" ID=%d X=%.10f Y=%.10f\n' % (
                        layer_name, self.poly_id,
                        _iu_to_hyp(p0.x), _iu_to_hyp(-p0.y),
                    ),
                )
                for v in range(outl.PointCount()):
                    pt = outl.CPoint(v)
                    self._print(
                        2,
                        "(LINE X=%.10f Y=%.10f)\n" % (
                            _iu_to_hyp(pt.x), _iu_to_hyp(-pt.y),
                        ),
                    )
                self._print(
                    2,
                    "(LINE X=%.10f Y=%.10f)\n" % (
                        _iu_to_hyp(p0.x), _iu_to_hyp(-p0.y),
                    ),
                )
                self._print(1, "}\n")
                for h in range(fill.HoleCount(i)):
                    hole = fill.CHole(i, h)
                    ph0 = hole.CPoint(0)
                    self._print(
                        1,
                        "{POLYVOID ID=%d X=%.10f Y=%.10f\n" % (
                            self.poly_id,
                            _iu_to_hyp(ph0.x), _iu_to_hyp(-ph0.y),
                        ),
                    )
                    for v in range(hole.PointCount()):
                        pt = hole.CPoint(v)
                        self._print(
                            2,
                            "(LINE X=%.10f Y=%.10f)\n" % (
                                _iu_to_hyp(pt.x), _iu_to_hyp(-pt.y),
                            ),
                        )
                    self._print(
                        2,
                        "(LINE X=%.10f Y=%.10f)\n" % (
                            _iu_to_hyp(ph0.x), _iu_to_hyp(-ph0.y),
                        ),
                    )
                    self._print(1, "}\n")
                self.poly_id += 1

    def _write_nets(self):
        self.poly_id = 1
        # NETINFO_LIST::iterator walks m_netNames (a std::map keyed
        # by wxString net name), so the byte-exact reference order
        # is alphabetic by name — NOT numeric by netcode.
        nets_by_name = self.board.GetNetInfo().NetsByName()
        for net_name_key in nets_by_name:
            net_info = nets_by_name[net_name_key]
            netcode = net_info.GetNetCode()
            net_name = net_info.GetNetname()
            if netcode <= 0 or not net_name:
                continue
            net_objects = self._collect_net_objects(netcode)
            if net_objects:
                self._print(0, '{NET="%s"\n' % net_name)
                self._write_net_objects(net_objects)
                self._print(0, "}\n\n")
        # Items not attached to any defined net land in EmptyNet<N>
        # one-per-NET blocks, matching the C++ behaviour (line 647).
        null_objects = self._collect_net_objects(-1)
        for idx, item in enumerate(null_objects):
            self._print(0, '{NET="EmptyNet%d"\n' % idx)
            self._write_net_objects([item])
            self._print(0, "}\n\n")
        return True


def write_hyperlynx(board, output_path):
    """Write `board` to `output_path` as a metric Hyperlynx .hyp file.

    Args:
        board:       pcbnew.BOARD instance.
        output_path: Filesystem path for the .hyp output.

    Returns:
        True on success, False on board-outline error (mirrors the
        C++ return path).
    """
    return _HyperlynxExporter(board, output_path).write()
