# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading the routed-geometry document, for the fields this package uses.

The document is interposer-pnr's: micrometers, Y up, origin at the outline's
lower-left corner. This reader checks only what the intermediates read from it
(the outline, the body thickness, the devices and the .chiplet binding); the
nets and vias are the GDS row's.
"""

import hashlib
import json
import math

from .. import vendored as _vendored

SCHEMA = "routed-geometry"
SUPPORTED_VERSION = "1.0"
FRAME = "interposer_local_y_up_um"

#: The name the version policy gives the document in its messages.
ARTIFACT = "routed-geometry.json"


class RoutedRefused(Exception):
    """The routed inputs cannot give the export's intermediates; nothing is
    written.

    Not a ``ValueError``: the decoders below raise those for their own
    causes, and a caller must not mistake one for the other.
    """

    def lines(self):
        return ["Routed intermediates refused: %s" % self]


def _finite(value, label, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (positive and value <= 0)):
        raise RoutedRefused("%s must be a finite number%s, not %r"
                            % (label, " above 0" if positive else "", value))
    return float(value)


def _point(value, label):
    if not isinstance(value, list) or len(value) != 2:
        raise RoutedRefused("%s must be an [x, y] pair, not %r" % (label, value))
    return (_finite(value[0], label + "[0]"), _finite(value[1], label + "[1]"))


def _device(device, index):
    where = "devices[%d]" % index
    if not isinstance(device, dict):
        raise RoutedRefused("%s must be an object" % where)
    ref = device.get("ref")
    if not isinstance(ref, str) or not ref:
        raise RoutedRefused("%s has no ref" % where)
    where = "device %s" % ref
    layout = device.get("layout")
    if (not isinstance(layout, dict) or not isinstance(layout.get("path"), str)
            or not layout["path"]):
        raise RoutedRefused("%s has no layout.path" % where)
    _point(device.get("origin"), where + " origin")
    _finite(device.get("rotation_deg"), where + " rotation_deg")
    if type(device.get("mirrored")) is not bool:
        raise RoutedRefused("%s mirrored must be true or false" % where)
    if device.get("pad_domains") is None:
        raise RoutedRefused(
            "%s has no pad_domains; the die pin list and its dimensions come "
            "from that archive only" % where)


def check_document(document, *, on_warn=None):
    """Refuse ``document`` unless it holds what the intermediates read.

    The version goes through chiplet_format_io's shared policy: a higher
    minor of major 1 is read with a warning, any other major is refused.
    """
    if not isinstance(document, dict):
        raise RoutedRefused("%s is not a JSON object" % ARTIFACT)
    if document.get("schema") != SCHEMA:
        raise RoutedRefused("%s has schema %r, not %r"
                            % (ARTIFACT, document.get("schema"), SCHEMA))
    cfio = _vendored.chiplet_format_io()
    try:
        cfio.check_contract_version(document.get("version"), SUPPORTED_VERSION,
                                    name=ARTIFACT, on_warn=on_warn)
    except cfio.ContractVersionError as exc:
        raise RoutedRefused(str(exc)) from exc
    if document.get("frame") != FRAME:
        raise RoutedRefused("%s is in frame %r; this reader reads only %r"
                            % (ARTIFACT, document.get("frame"), FRAME))
    outline = document.get("outline")
    if not isinstance(outline, list) or len(outline) < 3:
        raise RoutedRefused("%s outline needs at least three points" % ARTIFACT)
    for i, point in enumerate(outline):
        _point(point, "outline[%d]" % i)
    interposer = document.get("interposer")
    if not isinstance(interposer, dict):
        raise RoutedRefused("%s has no interposer object" % ARTIFACT)
    _finite(interposer.get("body_thickness_um"), "interposer.body_thickness_um",
            positive=True)
    devices = document.get("devices")
    if not isinstance(devices, list) or not devices:
        raise RoutedRefused("%s lists no devices" % ARTIFACT)
    seen = set()
    for i, device in enumerate(devices):
        _device(device, i)
        if device["ref"] in seen:
            raise RoutedRefused("device %s is listed twice" % device["ref"])
        seen.add(device["ref"])
    return document


def read_document(path, *, on_warn=None):
    """The routed-geometry document at ``path``, checked; RoutedRefused
    otherwise."""
    try:
        with open(path, encoding="utf-8") as f:
            document = json.load(f)
    except (OSError, ValueError) as exc:
        raise RoutedRefused("cannot read %s: %s" % (path, exc)) from exc
    return check_document(document, on_warn=on_warn)


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def check_chiplet_binding(document, chiplet_path):
    """Refuse a .chiplet other than the one the document was written from
    (``inputs.chiplet.sha256``). No path is followed from the document."""
    bound = ((document.get("inputs") or {}).get("chiplet") or {}).get("sha256")
    if not isinstance(bound, str) or not bound:
        raise RoutedRefused("%s names no inputs.chiplet.sha256" % ARTIFACT)
    actual = sha256_file(chiplet_path)
    if actual != bound:
        raise RoutedRefused(
            "%s is not the .chiplet the routed geometry was written from "
            "(sha256 %s, the document binds %s)" % (chiplet_path, actual, bound))
