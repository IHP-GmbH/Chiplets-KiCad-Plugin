# SPDX-License-Identifier: GPL-3.0-or-later
"""The routed-geometry document, checked: what the export's intermediates
and the worker's drawing read from it.

Read for the export's preflight (routed_inputs.py, which the CLI runs under
the worker interpreter) and by the worker (hyp_to_gds.py --routed-geometry),
so both refuse the same documents; the CLI imports it too, for the files the
document binds. Run as scripts, both import it as a sibling module, so besides
the standard library it imports only the vendored chiplet_format_io (for the
version policy, and only when a version is checked), the way hyp_to_gds
imports its siblings.

The document is interposer-pnr's: micrometres, Y up, origin at the outline's
lower-left corner. chiplet-spec reads it with ``load_routed_geometry``, which
needs jsonschema; KiCad's Python has none, so this reader stands in for it and
refuses everything that one refuses (tests/test_routed_reader_agrees_with_helper.py
holds the two together). It may refuse more, never less.

Nothing in a net is ignored: a net or a record carrying a key this reader does
not read (an arc, say) is refused, naming the net and the key, as is one
missing a key. The only keys read and not drawn are the ones the schema
declares as optional labels: a net's ``net_class`` and ``owner`` and a via's
``array``. The board path draws nothing from them either (the .hyp carries no
net class). Coordinates and widths are micrometres in the interposer GDS frame.
"""

import hashlib
import json
import math
import os
import re

SCHEMA = "routed-geometry"
SUPPORTED_VERSION = "1.0"
FRAME = "interposer_local_y_up_um"

#: The name the version policy gives the document in its messages.
ARTIFACT = "routed-geometry.json"

STACK_KEYS = frozenset({"metal", "cut_above"})
NET_KEYS = frozenset({"name", "board_name", "segments", "vias", "drawn"})
NET_LABELS = frozenset({"net_class", "owner"})
SEGMENT_KEYS = frozenset({"layer", "x0", "y0", "x1", "y1", "width"})
VIA_KEYS = frozenset({"via", "x", "y", "top_layer", "bottom_layer", "size"})
VIA_LABELS = frozenset({"array"})
DRAWN_KEYS = frozenset({"via", "x", "y", "top_layer", "bottom_layer", "cut",
                        "enclosure", "pitch", "k", "pad"})
#: What a drawn via repeats of its placed via.
_PLACED = ("via", "x", "y", "top_layer", "bottom_layer")
#: The inputs the document binds by path and sha256, and by sha256 alone.
INPUT_FILES = ("traces", "chiplet", "bumps")
INPUT_DIGESTS = ("routing_tech", "tech_package", "physical_stack")
#: A sha256 as the schema states one: 64 lower-case hex digits and nothing after.
_SHA256 = re.compile(r"[0-9a-f]{64}")


class DocumentRefused(Exception):
    """The document cannot be read for what it is read for; nothing is
    written or drawn."""


def _chiplet_format_io():
    try:
        from . import vendored
    except ImportError:
        import vendored
    return vendored.chiplet_format_io()


def _keys(record, allowed, what, labels=frozenset()):
    """``record`` has every key of ``allowed`` and nothing beyond it and
    ``labels``, whose values must be names."""
    if not isinstance(record, dict):
        raise DocumentRefused("%s is not an object" % what)
    for key in labels & set(record):
        _text(record[key], "%s.%s" % (what, key))
    extra = sorted(set(record) - allowed - labels)
    if extra:
        raise DocumentRefused("%s carries %s, which this reader does not read"
                              % (what, ", ".join(repr(k) for k in extra)))
    missing = sorted(allowed - set(record))
    if missing:
        raise DocumentRefused("%s has no %s"
                              % (what, ", ".join(repr(k) for k in missing)))


def _number(value, what, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or (positive and value <= 0):
        raise DocumentRefused("%s is not a %snumber: %r"
                              % (what, "positive " if positive else "", value))
    return float(value)


def _text(value, what):
    if not isinstance(value, str) or not value:
        raise DocumentRefused("%s is not a name: %r" % (what, value))
    return value


def _finite(value, label, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (positive and value <= 0)):
        raise DocumentRefused("%s must be a finite number%s, not %r"
                            % (label, " above 0" if positive else "", value))
    return float(value)


def _point(value, label):
    if not isinstance(value, list) or len(value) != 2:
        raise DocumentRefused("%s must be an [x, y] pair, not %r" % (label, value))
    return (_finite(value[0], label + "[0]"), _finite(value[1], label + "[1]"))


def _digest(entry, what):
    """``entry`` is an object whose ``sha256`` is a digest."""
    if not isinstance(entry, dict):
        raise DocumentRefused("%s is not an object" % what)
    sha = entry.get("sha256")
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        raise DocumentRefused("%s.sha256 is not a sha256: %r" % (what, sha))


def _file(entry, what):
    """``entry`` names a file by a path and its sha256."""
    _digest(entry, what)
    _text(entry.get("path"), what + ".path")


def _inputs(document):
    """The files and digests the document says it was routed from."""
    inputs = document.get("inputs")
    if not isinstance(inputs, dict):
        raise DocumentRefused("%s has no inputs object" % ARTIFACT)
    for key in INPUT_FILES + INPUT_DIGESTS + ("pins",):
        if key not in inputs:
            raise DocumentRefused("%s inputs has no %r" % (ARTIFACT, key))
    for key in INPUT_FILES:
        _file(inputs[key], "inputs." + key)
    if "form" in inputs:
        _file(inputs["form"], "inputs.form")
    for key in INPUT_DIGESTS:
        _digest(inputs[key], "inputs." + key)
    if not isinstance(inputs["pins"], dict):
        raise DocumentRefused("%s inputs.pins is not an object" % ARTIFACT)
    for ref, entry in inputs["pins"].items():
        _file(entry, "inputs.pins.%s" % ref)


def _producer(document):
    producer = document.get("producer")
    if not isinstance(producer, dict):
        raise DocumentRefused("%s has no producer object" % ARTIFACT)
    _text(producer.get("tool"), "producer.tool")
    _text(producer.get("version"), "producer.version")


def _device(device, index):
    where = "devices[%d]" % index
    if not isinstance(device, dict):
        raise DocumentRefused("%s must be an object" % where)
    ref = device.get("ref")
    if not isinstance(ref, str) or not ref:
        raise DocumentRefused("%s has no ref" % where)
    where = "device %s" % ref
    _file(device.get("layout"), where + " layout")
    _point(device.get("origin"), where + " origin")
    _finite(device.get("rotation_deg"), where + " rotation_deg")
    if type(device.get("mirrored")) is not bool:
        raise DocumentRefused("%s mirrored must be true or false" % where)
    if device.get("pad_domains") is None:
        raise DocumentRefused(
            "%s has no pad_domains; the die pin list and its dimensions come "
            "from that archive only" % where)
    if not isinstance(device["pad_domains"], dict):
        raise DocumentRefused("%s pad_domains is not an object" % where)


def check_document(document, *, on_warn=None):
    """Refuse ``document`` unless it holds what the intermediates read.

    The version goes through chiplet_format_io's shared policy: a higher
    minor of major 1 is read with a warning, any other major is refused.
    """
    if not isinstance(document, dict):
        raise DocumentRefused("%s is not a JSON object" % ARTIFACT)
    if document.get("schema") != SCHEMA:
        raise DocumentRefused("%s has schema %r, not %r"
                            % (ARTIFACT, document.get("schema"), SCHEMA))
    cfio = _chiplet_format_io()
    try:
        cfio.check_contract_version(document.get("version"), SUPPORTED_VERSION,
                                    name=ARTIFACT, on_warn=on_warn)
    except cfio.ContractVersionError as exc:
        raise DocumentRefused(str(exc)) from exc
    if document.get("frame") != FRAME:
        raise DocumentRefused("%s is in frame %r; this reader reads only %r"
                            % (ARTIFACT, document.get("frame"), FRAME))
    _finite(document.get("dbu_um"), "dbu_um", positive=True)
    outline = document.get("outline")
    if not isinstance(outline, list) or len(outline) < 4:
        raise DocumentRefused("%s outline needs at least four points" % ARTIFACT)
    points = [_point(point, "outline[%d]" % i) for i, point in enumerate(outline)]
    # The io_pads manifest's gds_bbox_corner_um frame and this one share an
    # origin only because this frame puts it at the outline's lower-left.
    corner = (min(x for x, _y in points), min(y for _x, y in points))
    if any(round(value * 1000.0) != 0 for value in corner):
        raise DocumentRefused(
            "%s outline's lower-left is (%g, %g); frame %s has its origin there"
            % (ARTIFACT, corner[0], corner[1], FRAME))
    interposer = document.get("interposer")
    if not isinstance(interposer, dict):
        raise DocumentRefused("%s has no interposer object" % ARTIFACT)
    _finite(interposer.get("body_thickness_um"), "interposer.body_thickness_um",
            positive=True)
    devices = document.get("devices")
    if not isinstance(devices, list) or not devices:
        raise DocumentRefused("%s lists no devices" % ARTIFACT)
    seen = set()
    for i, device in enumerate(devices):
        _device(device, i)
        if device["ref"] in seen:
            raise DocumentRefused("device %s is listed twice" % device["ref"])
        seen.add(device["ref"])
    check_nets(document)
    _inputs(document)
    _producer(document)
    return document


def read_document(path, *, on_warn=None):
    """The routed-geometry document at ``path``, checked; DocumentRefused
    otherwise."""
    try:
        with open(path, encoding="utf-8") as f:
            document = json.load(f)
    except (OSError, ValueError) as exc:
        raise DocumentRefused("cannot read %s: %s" % (path, exc)) from exc
    return check_document(document, on_warn=on_warn)


def stack_metals(document):
    """The stack's metal names, bottom to top, checked."""
    stack = document.get("stack")
    if not isinstance(stack, list) or not stack:
        raise DocumentRefused("%s names no stack" % ARTIFACT)
    metals = []
    for i, entry in enumerate(stack):
        what = "stack[%d]" % i
        _keys(entry, STACK_KEYS, what)
        metal = _text(entry["metal"], what + ".metal")
        if metal in metals:
            raise DocumentRefused("%s names %s twice" % (what, metal))
        cut = entry["cut_above"]
        if (cut is None) != (i == len(stack) - 1):
            raise DocumentRefused(
                "%s.cut_above must be null on the top metal and a cut below it"
                % what)
        if cut is not None:
            _text(cut, what + ".cut_above")
        metals.append(metal)
    return metals


def check_nets(document):
    """The nets, each checked against the stack; DocumentRefused otherwise."""
    order = stack_metals(document)
    metals = set(order)
    nets = document.get("nets")
    if not isinstance(nets, list):
        raise DocumentRefused("%s has no nets list" % ARTIFACT)
    names = set()
    for i, net in enumerate(nets):
        label = "nets[%d]" % i
        if isinstance(net, dict) and isinstance(net.get("name"), str):
            label = "net %s" % net["name"]
        _keys(net, NET_KEYS, label, NET_LABELS)
        name = _text(net["name"], label + ".name")
        _text(net["board_name"], label + ".board_name")
        if name in names:
            raise DocumentRefused("%s is listed twice" % label)
        names.add(name)
        for key in ("segments", "vias", "drawn"):
            if not isinstance(net[key], list):
                raise DocumentRefused("%s.%s is not a list" % (label, key))
        for j, seg in enumerate(net["segments"]):
            what = "%s segment %d" % (label, j)
            _keys(seg, SEGMENT_KEYS, what)
            _layer(seg["layer"], metals, what)
            for key in ("x0", "y0", "x1", "y1"):
                _number(seg[key], "%s.%s" % (what, key))
            _number(seg["width"], what + ".width", positive=True)
        if len(net["vias"]) != len(net["drawn"]):
            raise DocumentRefused("%s places %d vias and draws %d"
                                  % (label, len(net["vias"]), len(net["drawn"])))
        for j, (via, drawn) in enumerate(zip(net["vias"], net["drawn"])):
            what = "%s via %d" % (label, j)
            _keys(via, VIA_KEYS, what, VIA_LABELS)
            _keys(drawn, DRAWN_KEYS, what + " (drawn)")
            for key in _PLACED:
                if via[key] != drawn[key]:
                    raise DocumentRefused("%s: placed %s %r, drawn %r"
                                          % (what, key, via[key], drawn[key]))
            _text(via["via"], what + ".via")
            _number(via["x"], what + ".x")
            _number(via["y"], what + ".y")
            _layer(via["top_layer"], metals, what + ".top_layer")
            _layer(via["bottom_layer"], metals, what + ".bottom_layer")
            if order.index(via["top_layer"]) != order.index(via["bottom_layer"]) + 1:
                raise DocumentRefused(
                    "%s joins %s and %s, which are not adjacent in the stack"
                    % (what, via["bottom_layer"], via["top_layer"]))
            if via["size"] is not None:
                _number(via["size"], what + ".size", positive=True)
            _drawn_values(drawn, what + " (drawn)")
    return nets


def _drawn_values(drawn, what):
    """The cut array a drawn via states: pad, cut, enclosure, pitch, k."""
    pad = drawn["pad"]
    if not isinstance(pad, list) or len(pad) != 2:
        raise DocumentRefused("%s.pad is not a [width, height] pair: %r"
                              % (what, pad))
    for i, value in enumerate(pad):
        _number(value, "%s.pad[%d]" % (what, i), positive=True)
    _number(drawn["cut"], what + ".cut", positive=True)
    if _number(drawn["enclosure"], what + ".enclosure") < 0:
        raise DocumentRefused("%s.enclosure is negative: %r"
                              % (what, drawn["enclosure"]))
    k = drawn["k"]
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise DocumentRefused("%s.k is not a count of at least 1: %r" % (what, k))
    if k == 1:
        if drawn["pitch"] is not None:
            raise DocumentRefused("%s draws one cut and states a pitch: %r"
                                  % (what, drawn["pitch"]))
    elif drawn["pitch"] is None:
        raise DocumentRefused("%s draws %d cuts and states no pitch" % (what, k))
    else:
        _number(drawn["pitch"], what + ".pitch", positive=True)


def _layer(value, metals, what):
    _text(value, what)
    if value not in metals:
        raise DocumentRefused("%s is on %r, which the stack does not name"
                              % (what, value))


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def bound_files(document, document_path):
    """{ref: {"layout": path, "pins": path}}: the die layout and pin list the
    document binds for each device, each checked against the sha256 it binds.

    A relative path is resolved against the document's folder (the flow
    writes the document beside the board, so a layout path is the board's
    GDS_FILE text); an absolute one is taken as it is. Refuses
    (DocumentRefused) a file that cannot be read or whose bytes are not the
    ones bound, and a device whose pin list the document does not bind.
    """
    folder = os.path.dirname(os.path.abspath(document_path))
    pins = (document.get("inputs") or {}).get("pins") or {}
    devices = document.get("devices")
    if not isinstance(devices, list) or not devices:
        raise DocumentRefused("%s lists no devices" % ARTIFACT)
    files = {}
    for device in devices:
        ref = device.get("ref") if isinstance(device, dict) else None
        _text(ref, "a device ref")
        if not isinstance(pins.get(ref), dict):
            raise DocumentRefused("%s binds no pin list for %s (inputs.pins)"
                                  % (ARTIFACT, ref))
        files[ref] = {}
        for kind, entry, label in (("layout", device.get("layout"), "layout"),
                                   ("pins", pins[ref], "pin list")):
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) \
                    or not isinstance(entry.get("sha256"), str):
                raise DocumentRefused("%s names no %s path and sha256 for %s"
                                      % (ARTIFACT, label, ref))
            path = os.path.join(folder, entry["path"])
            try:
                actual = sha256_file(path)
            except OSError as exc:
                raise DocumentRefused("cannot read the %s of %s, %s: %s"
                                      % (label, ref, path, exc)) from exc
            if actual != entry["sha256"]:
                raise DocumentRefused(
                    "the %s of %s, %s, is not the file the routed geometry "
                    "binds (sha256 %s, the document binds %s)"
                    % (label, ref, path, actual, entry["sha256"]))
            files[ref][kind] = os.path.normpath(path)
    return files


def check_chiplet_binding(document, chiplet_path):
    """Refuse a .chiplet other than the one the document was written from
    (``inputs.chiplet.sha256``). No path is followed from the document."""
    bound = ((document.get("inputs") or {}).get("chiplet") or {}).get("sha256")
    if not isinstance(bound, str) or not bound:
        raise DocumentRefused("%s names no inputs.chiplet.sha256" % ARTIFACT)
    actual = sha256_file(chiplet_path)
    if actual != bound:
        raise DocumentRefused(
            "%s is not the .chiplet the routed geometry was written from "
            "(sha256 %s, the document binds %s)" % (chiplet_path, actual, bound))
