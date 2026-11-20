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

import math

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
    metals = set(stack_metals(document))
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
    return nets


def _layer(value, metals, what):
    _text(value, what)
    if value not in metals:
        raise DocumentRefused("%s is on %r, which the stack does not name"
                              % (what, value))
