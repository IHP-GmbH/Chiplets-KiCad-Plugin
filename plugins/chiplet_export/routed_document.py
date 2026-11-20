# SPDX-License-Identifier: GPL-3.0-or-later
"""The routed-geometry document's copper: its stack and its nets, checked.

Read by the export's preflight (routed/document.py) and by the worker
(hyp_to_gds.py --routed-geometry), so both refuse the same documents; the
worker imports it as a sibling script module, so it imports nothing from the
package. Standard library only.

Nothing in a net is ignored: a net or a record carrying a key this reader does
not read (an arc, say) is refused, naming the net and the key, as is one
missing a key. The only keys read and not drawn are the ones the schema
declares as optional labels: a net's ``net_class`` and ``owner`` and a via's
``array``. The board path draws nothing from them either (the .hyp carries no
net class). Coordinates and widths are micrometres in the interposer GDS frame.
"""

import hashlib
import math
import os

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


class DocumentRefused(Exception):
    """The document's stack or nets cannot be read; nothing is drawn."""


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
    if drawn["pitch"] is not None:
        _number(drawn["pitch"], what + ".pitch", positive=True)
    elif k != 1:
        raise DocumentRefused("%s draws %d cuts and states no pitch" % (what, k))


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
