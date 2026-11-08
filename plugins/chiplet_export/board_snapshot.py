# SPDX-License-Identifier: GPL-3.0-or-later
"""Read a pcbnew board without exporting, saving, or interpreting die metadata.

Positions are absolute KiCad coordinates in millimetres (Y down). Pad numbers
are author strings, not physical-pad identities. Repeated numbers and unassigned
pads remain separate observations; matching them to source pins belongs to the
consumer.
"""

import json
import math


class SnapshotError(ValueError):
    """The board cannot be represented by this snapshot version."""


def _finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise SnapshotError("Non-finite board geometry")
    return value


def _ordered(records):
    # Presentation order only. Neither the index nor this ordering is identity.
    return sorted(records, key=lambda row: json.dumps(row, sort_keys=True,
                                                     allow_nan=False))


def _properties(footprint):
    properties = {}
    for field in footprint.GetFields():
        name = str(field.GetName())
        if name in properties:
            raise SnapshotError("Duplicate footprint property %r on %s" %
                                (name, footprint.GetReference()))
        # GetText, not GetShownText: preserve variables and whitespace verbatim.
        properties[name] = str(field.GetText())
    return properties


def _require_simple_pad(pad, reference, number, pcbnew):
    """Refuse physical parameters that observation v1 cannot describe.

    In the SWIG API, no-argument shape/size/offset accessors read F.Cu. Only
    NORMAL padstacks guarantee that these describe every copper layer.
    Missing inspection APIs cannot establish that the pad is supported.
    """
    context = "%s pad %r" % (reference, number)

    def read(label, getter):
        try:
            return getter()
        except Exception as exc:
            raise SnapshotError("%s: cannot inspect %s (%s: %s)" %
                                (context, label, type(exc).__name__, exc)) from exc

    smd = read("pcbnew.PAD_ATTRIB_SMD", lambda: pcbnew.PAD_ATTRIB_SMD)
    if read("GetAttribute()", lambda: pad.GetAttribute()) != smd:
        raise SnapshotError("%s: only SMD pads are supported" % context)
    normal = read("pcbnew.PADSTACK.MODE_NORMAL", lambda: pcbnew.PADSTACK.MODE_NORMAL)
    if read("Padstack().Mode()", lambda: pad.Padstack().Mode()) != normal:
        raise SnapshotError("%s: only uniform NORMAL padstacks are supported" % context)

    def xy(method):
        value = getattr(pad, method)()
        return _finite(value.x), _finite(value.y)

    if read("GetDrillSize()", lambda: xy("GetDrillSize")) != (0.0, 0.0):
        raise SnapshotError("%s: drilled pads are unsupported" % context)
    if read("GetOffset()", lambda: xy("GetOffset")) != (0.0, 0.0):
        raise SnapshotError("%s: copper offset is unsupported" % context)


def observe_board(board):
    """Return raw, JSON-compatible board facts; never change the board.

    pcbnew and the existing writer's unit conversion are imported lazily, so
    importing this module or asking the CLI for help does not need KiCad.
    """
    import pcbnew

    if __package__:
        from .writers.chiplet_writer import _iu_to_um
    else:
        from writers.chiplet_writer import _iu_to_um

    def mm(iu):
        return _finite(_iu_to_um(iu) / 1000.0)

    def point(vector):
        return [mm(vector.x), mm(vector.y)]

    def layer(layer_id):
        return str(board.GetLayerName(layer_id))

    nets = [{"code": int(code), "name": str(net.GetNetname())}
            for code, net in board.GetNetsByNetcode().items()]
    nets.sort(key=lambda row: row["code"])
    net_names = {row["code"]: row["name"] for row in nets}

    def pad_record(pad, reference):
        number = str(pad.GetNumber())
        _require_simple_pad(pad, reference, number, pcbnew)
        shapes = {pcbnew.PAD_SHAPE_CIRCLE: "circle",
                  pcbnew.PAD_SHAPE_RECT: "rect",
                  pcbnew.PAD_SHAPE_OVAL: "oval"}
        kind = pad.GetShape()
        if kind not in shapes:
            raise SnapshotError("Unsupported pad shape %s on pad %r" %
                                (kind, pad.GetNumber()))
        code, name = int(pad.GetNetCode()), str(pad.GetNetname())
        if code not in net_names or net_names[code] != name:
            raise SnapshotError("Pad %r has inconsistent net code/name" %
                                pad.GetNumber())
        return {
            "number": number,
            # LoadBoard invents UUIDs for pads whose source has none. They
            # cannot be distinguished through these APIs; never publish an
            # invented value as an authored or stable pad identifier.
            "uuid": None,
            "at_mm": point(pad.GetPosition()),
            "size_mm": point(pad.GetSize()),
            "rotation_deg": _finite(pad.GetOrientation().AsDegrees()),
            "shape": shapes[kind],
            "layers": sorted(layer(lid) for lid in pad.GetLayerSet().Seq()),
            "net_code": code,
            "net_name": name,
        }

    footprints = []
    board_footprints = list(board.GetFootprints())
    for footprint in board_footprints:
        footprints.append({
            "reference": str(footprint.GetReference()),
            "uuid": str(footprint.m_Uuid.AsString()),
            "at_mm": point(footprint.GetPosition()),
            "rotation_deg": _finite(footprint.GetOrientation().AsDegrees()),
            "layer": layer(footprint.GetLayer()),
            "properties": _properties(footprint),
            "pads": _ordered([pad_record(pad, footprint.GetReference())
                              for pad in footprint.Pads()]),
        })

    def outline_record(item):
        if item.GetClass() != "PCB_SHAPE":
            raise SnapshotError("Unsupported Edge.Cuts item %s" % item.GetClass())
        shapes = {pcbnew.SHAPE_T_SEGMENT: "segment",
                  pcbnew.SHAPE_T_RECT: "rect",
                  pcbnew.SHAPE_T_ARC: "arc",
                  pcbnew.SHAPE_T_CIRCLE: "circle"}
        kind = item.GetShape()
        if kind not in shapes:
            raise SnapshotError("Unsupported Edge.Cuts shape %s" % kind)
        record = {"shape": shapes[kind], "width_mm": mm(item.GetWidth())}
        if kind == pcbnew.SHAPE_T_CIRCLE:
            record.update(center_mm=point(item.GetCenter()),
                          radius_mm=mm(item.GetRadius()))
        else:
            record.update(start_mm=point(item.GetStart()),
                          end_mm=point(item.GetEnd()))
            if kind == pcbnew.SHAPE_T_ARC:
                record["mid_mm"] = point(item.GetArcMid())
        return record

    drawings = list(board.GetDrawings())
    for footprint in board_footprints:
        drawings.extend(footprint.GraphicalItems())
    outline = [outline_record(item) for item in drawings
               if item.GetLayer() == pcbnew.Edge_Cuts]
    return {
        "format": "chiplet_kicad.board_observation",
        "version": 1,
        "units": "mm",
        "frame": "kicad_y_down",
        "source": {},
        "nets": nets,
        "outline": _ordered(outline),
        "footprints": _ordered(footprints),
    }
