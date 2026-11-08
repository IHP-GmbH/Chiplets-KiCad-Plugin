# SPDX-License-Identifier: GPL-3.0-or-later
"""Consume carrier-pad metadata without confusing it with die metal.

The original PinList is immutable provenance. Actual KiCad pads own net
membership; this reader checks their physical identity and produces the legacy
bump-generator input frame (footprint-local, mirrored once, nm, GDS Y up).
It never reads source paths or treats a recorded hash as verified file content.
"""

import json
import math
import re
import warnings

if __package__:
    from .board_snapshot import SnapshotError, _properties, _require_simple_pad
else:
    from board_snapshot import SnapshotError, _properties, _require_simple_pad


PROPERTY = "CHIPLET_PAD_DOMAINS"
POSITION_TOL_UM = 0.002  # 1 nm export/API quantization; not a design tolerance.


class PadDomainError(ValueError):
    """A marked footprint cannot be faithfully exported as carrier copper."""


def _object(value, label):
    if not isinstance(value, dict):
        raise PadDomainError("%s must be an object" % label)
    return value


def _number(value, label, positive=False):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or (positive and value <= 0):
        raise PadDomainError("%s must be a finite%s number" %
                             (label, " positive" if positive else ""))
    return value


def _sha(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PadDomainError("%s must be a SHA256 digest" % label)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PadDomainError("Duplicate metadata key %r" % key)
        result[key] = value
    return result


def _pin_list_version(value):
    # Same governed sidecar policy as chiplet-spec, with legacy integer 1.
    # Importing the reference reader here would require PyYAML in KiCad's GUI.
    if type(value) is int and value == 1:
        return
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)(?:\.[0-9]+)?", value) if isinstance(value, str) else None
    if match is None or match[1].lstrip("0") != "1":
        raise PadDomainError("Unsupported source PinList version %r" % value)
    if match[2].strip("0"):
        warnings.warn("Reading newer PinList minor version %s as 1.0" % value,
                      UserWarning, stacklevel=3)


def _copper_layer(token, pcbnew):
    # Derive the enum from KiCad's canonical name; numeric layer IDs are not
    # stable across KiCad generations. Board aliases such as TopMetal2 are a
    # separate technology mapping, not a replacement for this canonical name.
    try:
        return getattr(pcbnew, token.replace(".", "_"))
    except AttributeError as exc:
        raise PadDomainError("KiCad does not expose copper layer %s" % token) from exc


def _die_dimensions(pin_list):
    """Unrotated physical die extents; courtyard/text are not geometry sources."""
    dbu = _number(pin_list.get("dbu_um"), "PinList.dbu_um", positive=True)
    bbox = _object(pin_list.get("die_bbox_dbu"), "PinList.die_bbox_dbu")
    values = {key: _number(bbox.get(key), "die_bbox_dbu." + key)
              for key in ("x_min", "y_min", "x_max", "y_max")}
    return tuple(_number((values[hi] - values[lo]) * dbu, "die " + axis, positive=True)
                 for lo, hi, axis in (("x_min", "x_max", "width"),
                                      ("y_min", "y_max", "height")))


def decode_domains(text, reference, method):
    """Validate the versioned contract; return the original parsed document."""
    try:
        doc = _object(json.loads(text, object_pairs_hook=_unique_object), PROPERTY)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PadDomainError("Invalid %s JSON" % PROPERTY) from exc
    if (doc.get("format") != "chiplet_kicad.pad_domains"
            or type(doc.get("version")) is not int or doc["version"] != 1):
        raise PadDomainError("Unsupported carrier pad-domain format/version")
    if doc.get("component") != reference:
        raise PadDomainError("Carrier pad-domain component differs from reference")
    die = _object(doc.get("die"), "die")
    carrier = _object(doc.get("carrier"), "carrier")
    attachment = _object(doc.get("interconnect"), "interconnect")
    for domain in (die, carrier):
        if domain.get("frame") != "die_local_gds_y_up":
            raise PadDomainError("Unsupported pad-domain coordinate frame")
    _sha(die.get("pins_sha256"), "die.pins_sha256")
    _sha(carrier.get("tech_sha256"), "carrier.tech_sha256")
    _sha(attachment.get("manifest_sha256"), "interconnect.manifest_sha256")
    if not method or attachment.get("method") != method:
        raise PadDomainError("Carrier interconnect method differs from CONNECTION")
    if attachment.get("fab_anchor") != "ihp_cupillar_anchor":
        raise PadDomainError("Unsupported fabrication anchor; this adapter supports ihp_cupillar_anchor")
    for key in ("body_diameter_um", "passiv_opening_um", "tm2_enclosure_um"):
        _number(attachment.get(key), "interconnect." + key, positive=True)
    diameter = _number(carrier.get("diameter_um"), "carrier.diameter_um", True)
    expected = attachment["passiv_opening_um"] + 2 * attachment["tm2_enclosure_um"]
    if not math.isfinite(expected) or not math.isclose(diameter, expected, rel_tol=0, abs_tol=1e-9):
        raise PadDomainError("Carrier diameter differs from the explicit fabrication profile")
    layer = carrier.get("kicad_layer")
    # The existing IHP CuPillarPad fabrication adapter draws TopMetal2. This is
    # a declared supported profile, not a generic process/layer translation.
    if (carrier.get("shape") != "circle" or carrier.get("layer") != "TopMetal2"
            or not isinstance(layer, str)
            or re.fullmatch(r"(?:F|B|In[1-9][0-9]*)\.Cu", layer) is None):
        raise PadDomainError("Unsupported carrier shape/layer profile")
    pin_list = _object(die.get("pin_list"), "die.pin_list")
    _pin_list_version(pin_list.get("version"))
    _number(pin_list.get("dbu_um"), "PinList.dbu_um", positive=True)
    _die_dimensions(pin_list)
    pins = pin_list.get("pins")
    if not isinstance(pins, list) or not pins:
        raise PadDomainError("Source PinList must contain physical pins")
    by_index = {}
    for pin in pins:
        pin = _object(pin, "pin")
        index = pin.get("pad_index")
        if type(index) is not int or index < 0 or index in by_index:
            raise PadDomainError("Source physical pad_index must be unique nonnegative integers")
        if not isinstance(pin.get("name"), str):
            raise PadDomainError("Source pin name must be a string")
        for key in ("center_x_dbu", "center_y_dbu", "width_dbu", "height_dbu"):
            _number(pin.get(key), "pin." + key, positive=key in ("width_dbu", "height_dbu"))
        if pin.get("polygon_points_dbu") is not None:
            polygon = pin["polygon_points_dbu"]
            if not isinstance(polygon, list) or len(polygon) < 3:
                raise PadDomainError("Source pin polygon requires at least three points")
            for point in polygon:
                if not isinstance(point, list) or len(point) != 2:
                    raise PadDomainError("Source polygon point must have two coordinates")
                for value in point:
                    _number(value, "polygon coordinate")
        by_index[index] = pin
    bindings = doc.get("attachments")
    if not isinstance(bindings, list) or len(bindings) != len(pins):
        raise PadDomainError("Attachments must cover every source physical pin")
    seen = set()
    for binding in bindings:
        binding = _object(binding, "attachment")
        index = binding.get("pad_index")
        if (type(index) is not int or index not in by_index or index in seen
                or binding.get("pad_number") != by_index[index]["name"]):
            raise PadDomainError("Attachments are not a name-preserving physical-pin bijection")
        seen.add(index)
    return doc


def archived_die_dimensions(footprint):
    """Marked die dimensions in um, or None for the historical courtyard path.

    No layout source is opened and no editable artwork is consulted. The
    immutable export receipt remains responsible for detecting record changes.
    """
    try:
        props = _properties(footprint)
    except SnapshotError as exc:
        raise PadDomainError(str(exc)) from exc
    if PROPERTY not in props:
        return None
    doc = decode_domains(props[PROPERTY], footprint.GetReference(), props.get("CONNECTION"))
    return _die_dimensions(doc["die"]["pin_list"])


def _check_method_source(attachment, method_specs):
    method = attachment["method"]
    source = _object(method_specs.get(method), "selected manifest method " + method)
    fab = _object(source.get("fab_params"), "selected manifest fab_params")
    expected = {"fab_anchor": source.get("fab_anchor"),
                "body_diameter_um": source.get("body_diameter_um"),
                "passiv_opening_um": fab.get("passiv_opening_um"),
                "tm2_enclosure_um": fab.get("tm2_enclosure_um")}
    for key, value in expected.items():
        if key != "fab_anchor":
            _number(value, "selected manifest " + key, positive=True)
        if attachment[key] != value:
            raise PadDomainError("Carrier metadata %s differs from selected manifest; "
                                 "regenerate the board" % key)


def _pads_by_name(footprint, pcbnew):
    """Read each SWIG position once; never scan pads of unrelated terminals."""
    result = {}
    for pad in footprint.Pads():
        local = pad.GetFPRelativePosition()
        px = _number(local.x / (pcbnew.PCB_IU_PER_MM / 1000.0), "pad local X")
        py = _number(-local.y / (pcbnew.PCB_IU_PER_MM / 1000.0), "pad local Y")
        result.setdefault(str(pad.GetNumber()), []).append((pad, px, py))
    return result


def footprint_pin_list(footprint, pcbnew, method_specs=None):
    """Return a checked bump sidecar, or None for an unmarked legacy footprint."""
    try:
        props = _properties(footprint)
        if PROPERTY not in props:
            return None
        ref = footprint.GetReference()
        if not props.get("GDS_FILE"):
            raise PadDomainError("Carrier pad-domain footprint requires GDS_FILE")
        doc = decode_domains(props[PROPERTY], ref, props.get("CONNECTION"))
        if method_specs is not None:
            _check_method_source(doc["interconnect"], method_specs)
        orientation = props.get("ORIENTATION")
        if orientation not in ("flip_chip", "face_up"):
            raise PadDomainError("Carrier footprint requires canonical ORIENTATION")
        mx = -1 if orientation == "flip_chip" else 1
        source = doc["die"]["pin_list"]
        dbu = source["dbu_um"]
        copper_layer = _copper_layer(doc["carrier"]["kicad_layer"], pcbnew)
        by_name = _pads_by_name(footprint, pcbnew)
        if sum(map(len, by_name.values())) != len(source["pins"]):
            raise PadDomainError("Carrier pad count differs from original PinList")
        pins = []
        for pin in source["pins"]:
            x, y = mx * pin["center_x_dbu"] * dbu, pin["center_y_dbu"] * dbu
            candidates = by_name.get(pin["name"], [])
            matched = [row for row in candidates
                       if abs(row[1] - x) <= POSITION_TOL_UM
                       and abs(row[2] - y) <= POSITION_TOL_UM]
            if len(matched) != 1:
                raise PadDomainError("Physical pin %s/%s has no unique rigid carrier pad" %
                                     (ref, pin["pad_index"]))
            pad = matched[0][0]
            _require_simple_pad(pad, ref, pin["name"], pcbnew)
            diameter_iu = doc["carrier"]["diameter_um"] * (pcbnew.PCB_IU_PER_MM / 1000.0)
            if (pad.GetShape() != pcbnew.PAD_SHAPE_CIRCLE
                    or set(pad.GetLayerSet().Seq()) != {copper_layer}
                    or abs(pad.GetSizeX() - diameter_iu) > 1.0
                    or abs(pad.GetSizeY() - diameter_iu) > 1.0):
                raise PadDomainError("Physical pin %s/%s no longer has its carrier landing geometry" %
                                     (ref, pin["pad_index"]))
            candidates.remove(matched[0])
            # The bump reader uses fixed nm, not PinList.dbu_um. Convert only
            # known geometry fields; do not copy unknown geometry in a new frame.
            converted = {key: pin[key] for key in ("name", "type", "side", "pad_index")
                         if key in pin}
            converted.setdefault("type", "passive")
            scale = dbu / 0.001
            for key in ("center_x_dbu", "center_y_dbu", "width_dbu", "height_dbu"):
                converted[key] = pin[key] * scale * (mx if key == "center_x_dbu" else 1)
                _number(converted[key], "normalized " + key)
            if pin.get("polygon_points_dbu") is not None:
                converted["polygon_points_dbu"] = [[mx * a * scale, b * scale]
                                                   for a, b in pin["polygon_points_dbu"]]
                for point in converted["polygon_points_dbu"]:
                    for value in point:
                        _number(value, "normalized polygon coordinate")
            pins.append(converted)
        return {"version": 1, "chiplet_name": ref, "dbu_um": 0.001, "pins": pins}
    except PadDomainError:
        raise
    except (SnapshotError, AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise PadDomainError("Cannot inspect carrier pad-domain geometry: %s" % exc) from exc


def validate_board_pad_domains(board, method_specs, *, load_layer_map=None):
    """Validate marked footprints and return their authoritative method map.

    An unmarked board remains legacy and is not declared carrier-qualified.
    Detecting wholesale metadata removal requires the upstream export receipt.
    Load the selected worker LYP lazily: unmarked exports do not acquire a new
    dependency, while marked exports cannot skip the physical layer check.
    """
    from .layer_map import LayerMapError

    methods = {}
    layer_map = None
    for footprint in board.Footprints():
        import pcbnew
        if footprint_pin_list(footprint, pcbnew, method_specs) is not None:
            reference = footprint.GetReference()
            if reference in methods:
                raise PadDomainError("Duplicate carrier-domain footprint reference %s" % reference)
            try:
                if layer_map is None:
                    if load_layer_map is None:
                        raise PadDomainError("Carrier validation requires the selected worker LYP")
                    layer_map = load_layer_map()
                domains = decode_domains(
                    _properties(footprint)[PROPERTY], reference,
                    footprint.GetFieldText("CONNECTION"))
                carrier = domains["carrier"]
                token = carrier["kicad_layer"]
                actual_name = board.GetLayerName(_copper_layer(token, pcbnew))
                if not isinstance(actual_name, str) or not actual_name:
                    raise PadDomainError("Cannot inspect the current board layer name for " + token)
                actual = layer_map.get_layer(actual_name)
                expected = layer_map.get_layer(carrier["layer"])
                if actual != expected:
                    raise PadDomainError(
                        "%s board layer %r maps to %s, but carrier.layer %r maps to %s"
                        % (token, actual_name, actual, carrier["layer"], expected))
            except (LayerMapError, KeyError, AttributeError, TypeError, OSError, RuntimeError,
                    PadDomainError) as exc:
                raise PadDomainError("%s carrier layer: %s" % (reference, exc)) from exc
            methods[reference] = footprint.GetFieldText("CONNECTION")
    return methods


def validate_export_pad_sources(methods, *, method_overrides, default_method,
                                pad_locations, cupillar_gds):
    """Refuse overrides that would make the worker ignore validated geometry.

    A nonempty method override map replaces (not supplements) the board map.
    Any explicit pin-location map disables automatic extraction for the whole
    board. These are the existing worker/orchestrator semantics, not a merge.
    """
    if not methods:
        return  # Historical boards keep their explicit-override semantics.
    if pad_locations or cupillar_gds:
        raise PadDomainError(
            "Carrier-domain footprints require board-derived pin locations; "
            "external pad_locations or cupillar_gds bypass their validated "
            "geometry. Remove the override, or regenerate the board from the "
            "intended attachment inputs before exporting.")
    if method_overrides:
        for reference, expected in methods.items():
            selected = method_overrides.get(reference, default_method)
            if selected != expected:
                raise PadDomainError(
                    "%s: selected export method %r differs from validated "
                    "carrier method %r. Remove the method override, or "
                    "regenerate the board with the intended attachment method."
                    % (reference, selected, expected))
