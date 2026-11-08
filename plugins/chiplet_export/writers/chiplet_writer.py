# SPDX-License-Identifier: GPL-3.0-or-later
"""
Chiplet YAML writer.

Replicates kicad/pcbnew/exporters/export_chiplet.cpp in pure Python
via the pcbnew SWIG bindings. Output is the intermediate .chiplet
with the `_metadata.finalize_required: true` block; the canonical
file is produced downstream by hyp_to_gds.py --update-chiplet-file.

Frame and anchor semantics follow
chiplet-system-spec/docs/coord_frame_contract.md sections 1 and 2 and the writer contract (section 4).
"""

import math
import os
import sys

import pcbnew

from .connection_stacks import (
    emit_connection_stacks_block,
    emit_interconnect_block,
    validate_adapter_id,
    validate_interconnect_ids,
)
from ._yaml import escape_yaml_dq, yaml_scalar
from ._technology import technology_id

# is_cmim, CMIM_MODEL and die_role live next to the DIE_OUTLINE_LAYER rule so
# the export preflight classifies footprints without importing this
# pcbnew-bound module; they stay importable from here.
if "." in (__package__ or ""):
    from ..die_outline import (  # noqa: F401
        CMIM, CMIM_MODEL, FIELD as DIE_OUTLINE_LAYER_FIELD, IO_PAD,
        NOT_EMITTED, OUTLINE_DIE, die_role, is_cmim,
    )
    from ..draft_net_names import BoardCarrier, planner_net, read_board_carriers
else:
    from die_outline import (  # noqa: F401
        CMIM, CMIM_MODEL, FIELD as DIE_OUTLINE_LAYER_FIELD, IO_PAD,
        NOT_EMITTED, OUTLINE_DIE, die_role, is_cmim,
    )
    from draft_net_names import BoardCarrier, planner_net, read_board_carriers


def _iu_to_um(iu):
    """Internal units to micrometers. Mirrors iu2um() in the C++ exporter."""
    return iu / (pcbnew.PCB_IU_PER_MM / 1000.0)


def _field_text(footprint, name, default=""):
    """Return the text of a footprint field by name, or `default` if absent."""
    if footprint.HasField(name):
        return footprint.GetFieldText(name)
    return default


#: The hidden property interposer-pnr ``export`` writes on every carrier
#: footprint it draws, sheet or draft. On a sheet board the reference is the
#: sheet's ``J<n>``, so the io_pad id rides here.
IO_PAD_ID = "IO_PAD_ID"


def io_pad_carriers(footprints):
    """Every IO_CLASS footprint, in order, BOM-excluded ones included.

    The io_pads.json sidecar emits this set, as gds_to_kicad's reader does;
    write_chiplet emits only the carriers on the BOM but takes their ids
    from this set, so the two files refuse the same boards.
    """
    return [fp for fp in footprints if _field_text(fp, "IO_CLASS")]


def board_carrier(fp):
    """A carrier footprint as interposer-pnr's shared reader takes it."""
    return BoardCarrier(fp.GetReference(), _field_text(fp, IO_PAD_ID),
                        str(fp.GetFPID().GetLibNickname()),
                        bool(fp.GetPath().AsString()))


def carrier_identities(carriers, report):
    """Each carrier's io_pad id and net as the draft writes them, in order.

    The id and the names the nets go back by are interposer-pnr's rules
    (vendor/round_trip_names.py: read_board_carriers, planner_net), from
    ``report`` (draft_net_names.read_export_report's, or None): ids follow
    the footprints, not their references, so two carriers sharing a
    reference keep their own. All pads must carry the same net (empty
    counts); a footprint without pads has no net. Raises, before either
    writer opens its output, CarrierIdError for a carrier id, ValueError for
    a carrier net either writer would refuse, and BoardReportError when the
    report is missing or does not describe the board.
    """
    carriers = list(carriers)
    ids, names = read_board_carriers([board_carrier(fp) for fp in carriers], report)
    named = []
    for fp, ident in zip(carriers, ids):
        ref = fp.GetReference()
        label = ref if ref == ident else "%s (io_pad %s)" % (ref, ident)
        nets = {pad.GetNetname() for pad in fp.Pads()}
        if len(nets) > 1:
            raise ValueError(
                "carrier %s: its pads carry different nets (%s); a carrier's pads carry one net"
                % (label, ", ".join(repr(net) for net in sorted(nets))))
        net = next(iter(nets), "")
        if names is not None:
            # By the reference the designer finds in KiCad, and the io_pad id
            # where it differs: two carriers can share a reference.
            net = planner_net(label, net, names)
        named.append((ident, net))
    return named


def preflight_carriers(board, report):
    """Refuse a board whose carriers both writers would refuse, by id or by
    net (see carrier_identities), before any output exists."""
    carrier_identities(io_pad_carriers(board.Footprints()), report)


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


def _expand_text_var(project, name):
    """Resolve one project text variable through pcbnew, or None.

    SWIG never wrapped PROJECT (it is absent from every KiCad `.i` file, in
    every version), so `GetProject()` hands Python an opaque object and calling
    `GetTextVars()` on it is impossible. The pointer is still a valid typed
    `PROJECT*` though, and SWIG passes it straight back into a wrapped C++
    function, so ExpandTextVars reaches the very map the C++ exporter reads.

    Project-only, like the C++ exporter and unlike BOARD::ResolveTextVar, which
    is board-properties-first. An unset name comes back as the literal token,
    and a value containing ${...} is not re-expanded, both matching the C++
    side. Returns None when the name is unset or pcbnew cannot be asked.
    """
    if project is None:
        return None
    token = "${%s}" % name
    try:
        value = str(pcbnew.ExpandTextVars(token, project))
    except (AttributeError, TypeError, NotImplementedError):
        return None
    return None if value == token else value


def _lookup_text_var(board, name):
    """Look `name` up in the PROJECT text variables ONLY (never BOARD.GetProperties()).

    The C++ exporter reads INTERPOSER_ADAPTER / INTERCONNECT_ADAPTER from the
    project text variables only; _lookup_property's GetProperties-first lookup
    would let a board property shadow the text variable and diverge from
    byte-exact parity. Returns "" when absent (caller applies any default).

    Two legs: the direct map, for a binding that exposes it (no shipped KiCad
    does, but a test double can and a future one might), then ExpandTextVars,
    which is what actually resolves against a real board.
    """
    project = board.GetProject()
    if project is None:
        return ""
    if not hasattr(project, "GetTextVars"):
        return _expand_text_var(project, name) or ""
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


def write_io_pads_json(board, output_path, export_report=None):
    """Extract IO_CLASS footprints from `board` into an io_pads.json sidecar.

    Output matches gds_to_kicad/io_pads/kicad_pcb_to_iopads.py: positions in
    micrometers with Y negated (KiCad Y-down -> GDS Y-up), consumed by
    hyp_to_gds.py --io-pads to render the TopMetal2 pad geometry and inject
    the pads under the interposer component (so the GDS bbox includes them).
    Each entry's ``ref`` is the io_pad id (carrier_identities), not necessarily the
    footprint reference, and its ``net`` as carrier_identities names it from
    ``export_report``.

    Returns the number of io_pads written (0 -> nothing written, no file).
    """
    import json

    pads_out = []
    carriers = io_pad_carriers(board.Footprints())
    for fp, (ident, net_name) in zip(carriers, carrier_identities(carriers, export_report)):
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
            size_x_um = _iu_to_um(pads_list[0].GetSizeX())
            size_y_um = _iu_to_um(pads_list[0].GetSizeY())
        pos = fp.GetPosition()
        try:
            layer_name = board.GetLayerName(fp.GetLayer())
        except Exception:
            layer_name = "F.Cu"
        pads_out.append({
            "ref": ident,
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


CMIM_SIDECAR_VERSION = 2


def parse_length_um(text):
    """'w'/'l' field text -> micrometres, or None if unparseable.

    Two formats genuinely occur on a board and are only distinguishable by
    the unit suffix, so both must be accepted:

    - '8.11um' / '8.11u': what the PDK's own cmim_footprint_gen.py stamps
      into every intm4tm2.pretty footprint (already micrometres).
    - '8.11e-6' (bare, no suffix): how cap_cmim.kicad_sym stores w/l, in
      METRES, which is what lands on the footprint instance after
      "Update PCB from Schematic".

    Mirrors resizer_passive_elements/board_reader.py:_parse_um. Keep the two
    in step: a board round-tripped through that plugin ends up in the bare
    metres form, a freshly placed library part in the 'um' form, and this
    exporter has to read both.
    """
    if text is None:
        return None
    token = text.strip().lower()
    if not token:
        return None
    if token.endswith("um"):
        token, scale = token[:-2], 1.0
    elif token.endswith("u"):
        token, scale = token[:-1], 1.0
    else:
        scale = 1e6  # bare number: metres -> micrometres
    try:
        return float(token) * scale
    except ValueError:
        return None


def write_cmim_devices_json(board, output_path, skipped=None):
    """Extract cap_cmim footprints from `board` into a sidecar JSON.

    Metadata extraction only; the GDS geometry is generated later by
    hyp_to_gds.py from the IntM4TM2 `cmim` PCell. Dimensions are normalised
    to micrometres here (schema `w_um`/`l_um`, version 2) so the worker
    never has to guess which of the two board conventions a field used.
    Positions follow write_io_pads_json: micrometres, Y negated.

    `skipped`, when given, collects the refs of cap_cmim footprints that could
    not be described. The caller is expected to fail the export over them: a
    device dropped here never reaches the worker, so the worker's own
    "requested but not placed" guard cannot see it, and the run would ship an
    interposer missing a capacitor the board says is there.

    Returns the number of CMIM devices written (0 -> no file).
    """
    import json

    def _skip(ref, reason):
        print("Warning: skipping CMIM %s: %s" % (ref, reason),
              file=sys.stderr)
        if skipped is not None:
            skipped.append(str(ref))

    devices = []
    for fp in list(board.Footprints()):
        if not is_cmim(fp):
            continue

        ref = fp.GetReference()
        pos = fp.GetPosition()

        w_text = _field_text(fp, "w").strip()
        l_text = _field_text(fp, "l").strip()
        m_text = _field_text(fp, "m", "1").strip() or "1"

        missing = [n for n, t in (("w", w_text), ("l", l_text)) if not t]
        if missing:
            _skip(ref, "missing %s field(s)" % ", ".join(missing))
            continue

        w_um = parse_length_um(w_text)
        l_um = parse_length_um(l_text)
        bad = []
        if w_um is None:
            bad.append('w="%s"' % w_text)
        if l_um is None:
            bad.append('l="%s"' % l_text)
        if bad:
            _skip(ref, "unparseable %s" % ", ".join(bad))
            continue

        try:
            m = int(float(m_text))
        except ValueError:
            _skip(ref, 'unparseable m="%s"' % m_text)
            continue

        if w_um <= 0.0 or l_um <= 0.0 or m <= 0:
            _skip(ref, "non-positive parameter(s) (w=%g um, l=%g um, m=%d)"
                  % (w_um, l_um, m))
            continue

        # Y is negated for the GDS frame, so the rotation sense flips with it.
        # A rotated rectangular cap_cmim is real: the PDK's generator emits
        # CMIM_<w>x<l>um parts, and placing one unrotated draws it 90 degrees
        # off in the fabrication GDS.
        try:
            rotation_deg = -float(fp.GetOrientationDegrees()) % 360.0
        except Exception:
            rotation_deg = 0.0

        devices.append({
            "ref": ref,
            "x_um": _iu_to_um(pos.x),
            "y_um": -_iu_to_um(pos.y),
            "w_um": w_um,
            "l_um": l_um,
            "m": m,
            "rotation_deg": rotation_deg,
        })

    if not devices:
        return 0

    with open(output_path, "w") as f:
        json.dump({"version": CMIM_SIDECAR_VERSION,
                   "cmim_devices": devices}, f, indent=2)

    return len(devices)


# No-fill (keep-out) authoring layers. The user draws graphic polygons or
# rule-area/keepout zones on these dedicated NON-COPPER board layers to carve
# out where metal density fill must not go; the worker paints them onto the
# interposer GDS keep-out datatypes so the PDK fill generators skip them.
#
# Never author no-fill on a copper layer: a polygon on copper exports through
# the .hyp as real drawn metal (it would become fill's opposite -- metal, not a
# keep-out). Regions therefore travel by this sidecar, never through the .hyp.
#
# role -> GDS meaning is resolved worker-side (global -> NoMetFiller 160/0, all
# metals; per-metal -> <metal>/23), keeping the GDS layer contract in one place
# (hyp_to_gds.GDSGenerator).
NOFILL_LAYER_ROLES = {
    "NoMetFiller": "global",
    "M4.nofill": "M4",
    "M5.nofill": "M5",
    "TM1.nofill": "TM1",
    "TM2.nofill": "TM2",
}
NOFILL_SIDECAR_VERSION = 1


def _iter_polyset_rings_um(poly_set):
    """Yield each outline of a SHAPE_POLY_SET as a list of [x_um, y_um].

    Y is negated for the GDS frame (KiCad Y-down -> GDS Y-up), same convention
    as write_io_pads_json. Holes are ignored: a keep-out is a solid region, and
    an inner hole would only shrink the area the fill must avoid.
    """
    for i in range(poly_set.OutlineCount()):
        outl = poly_set.COutline(i)
        ring = [[_iu_to_um(outl.CPoint(v).x), -_iu_to_um(outl.CPoint(v).y)]
                for v in range(outl.PointCount())]
        if len(ring) >= 3:
            yield ring


def write_nofill_regions_json(board, output_path):
    """Extract no-fill (keep-out) regions from the dedicated board layers.

    Reads graphic polygons/rectangles (PCB_SHAPE) and rule-area/keepout zones
    drawn on the NOFILL_LAYER_ROLES layers into a sidecar JSON consumed by
    hyp_to_gds.py --nofill-regions. Coordinates are micrometres with Y negated,
    matching write_io_pads_json. The sidecar (not the .hyp) is the transport:
    the .hyp carries only net-attached copper, so a keep-out could not ride it.

    Returns the number of regions written (0 -> nothing written, no file).
    """
    import json

    def _role(item):
        try:
            return NOFILL_LAYER_ROLES.get(board.GetLayerName(item.GetLayer()))
        except Exception:
            return None

    regions = []

    def _emit(role, rings):
        for ring in rings:
            regions.append({"role": role, "polygon_um": ring})

    for item in list(board.GetDrawings()):
        if not isinstance(item, pcbnew.PCB_SHAPE):
            continue
        role = _role(item)
        if role is None:
            continue
        try:
            shape = item.GetShape()
        except Exception:
            continue
        if shape == pcbnew.SHAPE_T_POLY:
            _emit(role, _iter_polyset_rings_um(item.GetPolyShape()))
        elif shape == pcbnew.SHAPE_T_RECT:
            a, b = item.GetStart(), item.GetEnd()
            _emit(role, [[
                [_iu_to_um(a.x), -_iu_to_um(a.y)],
                [_iu_to_um(b.x), -_iu_to_um(a.y)],
                [_iu_to_um(b.x), -_iu_to_um(b.y)],
                [_iu_to_um(a.x), -_iu_to_um(b.y)],
            ]])
        # segment/arc/circle are not closed keep-out areas; skip them.

    for zone in list(board.Zones()):
        role = _role(zone)
        if role is None:
            continue
        try:
            outline = zone.Outline()
        except Exception:
            continue
        _emit(role, _iter_polyset_rings_um(outline))

    if not regions:
        return 0

    with open(output_path, "w") as f:
        json.dump({"version": NOFILL_SIDECAR_VERSION, "regions": regions}, f,
                  indent=2)

    return len(regions)


def write_die_pin_lists(board, out_dir):
    """Extract die footprint pads into per-die pin_list JSON sidecars.

    A marked footprint's electrical pads are carrier landings, not die metal.
    Validate their attachment record and preserve the original die dimensions.
    Unmarked historical footprints retain the legacy pad-derived semantics.
    Output matches the gds_to_kicad pin_list schema
    (footprint-local coordinates in DBU = nm, Y negated for GDS Y-up),
    consumed downstream by the Cu-pillar generator (bump_mirror) to place
    DRC-validated pillars under each flip-chip die.

    Returns a dict {device_ref: json_path}. Empty if no die footprints.
    """
    import json
    if "." in __package__:
        from ..pad_domains import footprint_pin_list
    else:
        from pad_domains import footprint_pin_list

    result = {}
    for fp in list(board.Footprints()):
        if not _field_text(fp, "GDS_FILE"):
            continue
        ref = fp.GetReference()
        domain_pins = footprint_pin_list(fp, pcbnew)
        if domain_pins is not None:
            path = os.path.join(out_dir, "%s_pins.json" % ref)
            with open(path, "w") as f:
                json.dump(domain_pins, f, indent=2)
            result[ref] = path
            continue
        print("Legacy die pad extraction for %s: no carrier-domain qualification; "
              "regenerate the board for domain-aware export." % ref,
              file=sys.stderr)
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


def write_die_outline_layers(board, mapping):
    """Persist per-die outline layers to footprint DIE_OUTLINE_LAYER fields.

    `mapping` is {ref: "L/D" text}. Only the dies sized from their GDS
    (die_role OUTLINE_DIE) are written: a marked die keeps its archived
    PinList size and never gets the field, and a footprint the .chiplet does
    not export as a die is left alone. An empty text is skipped, never
    written, since a die without the field refuses the export. The text is
    written as given (stripped); the export preflight is what validates it.
    The field is kept hidden on F.Fab at small size (_style_managed_field).
    Returns the refs that were modified; the caller owns saving the board.
    """
    changed = []
    for fp in list(board.Footprints()):
        ref = fp.GetReference()
        if ref not in mapping:
            continue
        new = (mapping[ref] or "").strip()
        if not new or die_role(fp, pcbnew) != OUTLINE_DIE:
            continue
        text_changed = _field_text(fp, DIE_OUTLINE_LAYER_FIELD) != new
        if text_changed:
            fp.SetField(DIE_OUTLINE_LAYER_FIELD, new)
        field = _get_field(fp, DIE_OUTLINE_LAYER_FIELD)
        restyled = _style_managed_field(field) if field is not None else False
        if text_changed or restyled:
            changed.append(ref)
    return changed


def write_chiplet(board, output_path, interconnect_manifest=None, export_report=None):
    """Write `board` to `output_path` as an intermediate .chiplet file.

    Args:
        board:        pcbnew.BOARD instance.
        output_path:  Filesystem path for the YAML output.
        interconnect_manifest: Optional authoritative manifest snapshot from
            the export preflight.  ``None`` preserves the standalone writer's
            historical discovery behavior.
        export_report: The export report's net names
            (draft_net_names.read_export_report), or None; see
            carrier_identities.

    Returns:
        True on success.

    Raises:
        OSError if the output path is not writable.
    """
    # --- Data gathering ---
    if "." in __package__:
        from ..pad_domains import archived_die_dimensions
    else:
        from pad_domains import archived_die_dimensions

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
    # Shape first, then membership. validate_interconnect_ids degrades to a
    # warning when the interconnect PDK is not discoverable, which would let a
    # path-shaped value through unchecked on a host without the PDK; the regex
    # gate never degrades.
    validate_adapter_id(interconnect_adapter,
                        "INTERCONNECT_ADAPTER text variable")
    validate_interconnect_ids(
        adapter=interconnect_adapter, manifest=interconnect_manifest)

    # Interposer adapter, emitted in the interposer: block further down. Read
    # and validated HERE, during data gathering, and not at the emit site: the
    # emit site runs inside the open output file, so failing there would leave
    # a partial .chiplet on disk.
    # Text-vars only, mirroring export_chiplet.cpp (GetTextVars()).
    interposer_adapter = (
        _lookup_text_var(board, "INTERPOSER_ADAPTER")
        or "intm4tm2"
    )
    validate_adapter_id(interposer_adapter, "INTERPOSER_ADAPTER text variable")

    component_techs = {}
    # Which die last named each technology id, for the two-sheets warning.
    tech_named_by = {}
    component_dimensions = {}
    components = []
    io_pads = []

    footprints = list(board.Footprints())
    for footprint in footprints:
        # The same classification the export preflight runs (die_role), so
        # the two agree on which footprints are dies.
        role = die_role(footprint, pcbnew)
        if role == NOT_EMITTED:
            continue

        if role == IO_PAD:
            io_class = _field_text(footprint, "IO_CLASS")
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

        if role == CMIM:
            # An interposer-integrated MIM capacitor is not a die: its geometry
            # is drawn into the interposer GDS from the IntM4TM2 PCell
            # (write_cmim_devices_json -> hyp_to_gds --cmim-devices), so it has
            # no layout of its own and no die thickness. Emitting it as a
            # component would ship dimensions.thickness 0.0, which the ADK
            # 3Dblox export rejects outright (chiplet2dbx _require_dimension).
            continue

        gds_file = _field_text(footprint, "GDS_FILE")
        lyp_file = _field_text(footprint, "LYP_FILE")

        if not gds_file:
            sys.stderr.write(
                "Warning: Footprint %s missing GDS_FILE field.\n"
                % footprint.GetReference()
            )

        if lyp_file:
            tech_id = technology_id(lyp_file)
            # A die LYP whose id collides with the interposer's hardcoded
            # technology id ('intm4tm2') but points at a different file would
            # silently clobber the interposer entry. Disambiguate the die with
            # its reference so the interposer keeps its own layer_properties.
            if (tech_id == interposer_tech_id
                    and interposer_tech_id in tech_map
                    and tech_map[interposer_tech_id] != lyp_file):
                tech_id = tech_id + "_" + footprint.GetReference()
            # Two dies that share an id share one entry. When their sheets are
            # different files, the last one wins for both, as in the C++
            # exporter; say so rather than merge them silently.
            if tech_id in tech_named_by and tech_map[tech_id] != lyp_file:
                sys.stderr.write(
                    "Warning: footprints %s and %s name technology '%s' with "
                    "two different layer sheets (%s, %s); the .chiplet keeps "
                    "%s for both.\n"
                    % (tech_named_by[tech_id], footprint.GetReference(),
                       tech_id, tech_map[tech_id], lyp_file, lyp_file))
            tech_map[tech_id] = lyp_file
            tech_named_by[tech_id] = footprint.GetReference()
            component_techs[id(footprint)] = tech_id

        # Marked footprints carry their actual die boundary in the immutable
        # source PinList. Validate it before opening the output, and never let
        # editable courtyard/text geometry redefine that physical die.
        dimensions = archived_die_dimensions(footprint)
        if dimensions is not None:
            component_dimensions[id(footprint)] = dimensions
        components.append(footprint)

    # Before opening the output, so a refused carrier leaves no file behind.
    # Over the sidecar's carrier set, not only the emitted io_pads.
    carriers = io_pad_carriers(footprints)
    # Keyed on the wrappers in `footprints`, which io_pads holds too: rebuild
    # neither list from fresh board lookups, or id() stops matching.
    named_by_fp = dict(zip(map(id, carriers), carrier_identities(carriers, export_report)))

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
        # Board Setup > Text Variables > INTERPOSER_ADAPTER. Read and
        # validated during data gathering above, so a malformed id fails the
        # export before this file was ever opened.
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
        f.write(emit_connection_stacks_block(interconnect_manifest))
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
        # section 4, item 1.
        f.write("    position:\n")
        f.write("      x: 0.0\n")
        f.write("      y: 0.0\n")
        f.write("      z: 0.0\n")

        # 1b) IO_CLASS footprints nested under interposer.io_pads.
        if io_pads:
            f.write("    io_pads:\n")
            for fp in io_pads:
                ident, net_name = named_by_fp[id(fp)]
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


                pos = fp.GetPosition()
                x_um = _iu_to_um(pos.x - interposer_x_min)
                y_um = _iu_to_um(interposer_y_max - pos.y)

                f.write("      - id: %s\n" % yaml_scalar(ident))
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
            # See coord_frame_contract.md section 2: dies use
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

            if id(footprint) in component_dimensions:
                comp_width, comp_height = component_dimensions[id(footprint)]
            else:
                fp_bbox = footprint.GetBoundingBox()
                courtyard_layer = (
                    pcbnew.B_CrtYd if footprint.GetLayer() == pcbnew.B_Cu
                    else pcbnew.F_CrtYd
                )
                # Preserve the historical path for unmarked footprints. Build
                # the lazy cache so GUI/headless legacy exports agree.
                try:
                    footprint.BuildCourtyardCaches()
                except Exception as exc:
                    print("WARNING: %s: BuildCourtyardCaches failed (%s); die "
                          "width/height fall back to the footprint bounding box"
                          % (ref, exc), file=sys.stderr)
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
