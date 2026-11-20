# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate manifest geometry against the interposer's landing pad cells.

The interconnect manifest owns the opening, the metal enclosure and, through
its fab_anchors table, the UBM opening layers an anchor needs. The Cu-pillar
pad cell overrides the opening for Table 6.1 body diameters; a profile a pad
cell cannot faithfully draw must be refused, not silently substituted.
This module has no KLayout or pcbnew dependency.
"""

import math
import re


def _positive_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a finite positive number" % label)
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError("%s must be a finite positive number" % label) from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError("%s must be a finite positive number" % label)
    return result


#: A method's two pad parameters: the name each has from interconnect
#: methods 1.2 on, the 1.x name every 1.x registry keeps beside it, and the
#: words a refusal uses for it.
PAD_PARAMETERS = (("passivation_opening_um", "passiv_opening_um", "passivation opening"),
                  ("pad_metal_enclosure_um", "tm2_enclosure_um", "pad metal enclosure"))


#: The manifest a refusal names: its file name, as interconnect_pdk's reader
#: names a manifest handed to it as a dict, so both give one text.
MANIFEST_WHERE = "interconnect_methods.json"


class FabParamsRefused(Exception):
    """A method's pad parameters have no single reading, or the reader that
    would read them is older than interconnect methods 1.2.

    Deliberately not a KeyError, TypeError or ValueError: the handlers around
    a manifest read take those to mean "malformed entry, skip it".
    """


#: The oldest interconnect methods version whose reader this plugin uses: 1.2
#: names the pad parameters by their new names and refuses two values.
READER_MIN_VERSION = (1, 2)


def check_reader(reader):
    """Refuse interconnect_pdk's reader when it reads interconnect methods
    below 1.2 (its ``SUPPORTED_SCHEMA_VERSION``), with FabParamsRefused."""
    value = getattr(reader, "SUPPORTED_SCHEMA_VERSION", None)
    parts = value.split(".") if isinstance(value, str) else ()
    version = (tuple(int(part) for part in parts)
               if len(parts) == 2 and all(part.isdigit() for part in parts) else None)
    if version is None or version < READER_MIN_VERSION:
        raise FabParamsRefused(
            "interconnect_pdk at %s reads interconnect methods up to version %s. This "
            "plugin needs version 1.2 or later, because 1.2 names the pad parameters "
            "passivation_opening_um and pad_metal_enclosure_um. Update interconnect_pdk."
            % (getattr(reader, "__file__", None) or "an unknown path",
               value if isinstance(value, str) and value else "unknown"))


def method_fab_params(method, method_specs, *, where=MANIFEST_WHERE, reader=None):
    """``method``'s fab_params, its pad parameters under the 1.2 names.

    With ``reader`` (interconnect_pdk's interconnect_manifest) the reader
    owns the rule: it must read interconnect methods 1.2 or later
    (check_reader), and its ``fab_params`` dict, or its own refusal with its
    text, is the answer. Without one (the board path reads the method from
    the board) chiplet-spec 1.25.0's rule (technology_package.method_fab_params)
    is applied here, with its two texts. ``where`` names the manifest in
    those. A method that is not an object, or has no fab_params object, reads
    as one without pad parameters (MISSING). Raises KeyError for an unknown
    method, as the reader does, and FabParamsRefused.
    """
    spec = method_specs[method]
    if reader is not None:
        check_reader(reader)
        try:
            return dict(reader.fab_params(method, {"methods": method_specs}, where=where))
        except reader.FabParamsRefused as exc:
            raise FabParamsRefused(str(exc)) from exc
    fab_params = spec.get("fab_params") if isinstance(spec, dict) else None
    params = dict(fab_params) if isinstance(fab_params, dict) else {}
    for new, old, words in PAD_PARAMETERS:
        if old not in params:
            if new in params:
                raise FabParamsRefused(
                    "PACKAGE_METHODS_FAB_PARAMS_MISSING: %s: method %s has no "
                    "fab_params.%s. Interconnect methods 1.x keeps the old name beside "
                    "%s, so that readers older than 1.2 still find it. Add it with the "
                    "same value." % (where, method, old, new))
            raise FabParamsRefused(
                "PACKAGE_METHODS_FAB_PARAMS_MISSING: %s: method %s has no %s: "
                "fab_params.%s and %s are both missing. Add both, with the same value."
                % (where, method, words, old, new))
        if new in params and params[new] != params[old]:
            raise FabParamsRefused(
                "PACKAGE_METHODS_FAB_PARAMS_DISAGREE: %s: method %s gives %s %s and %s "
                "%s. Since interconnect methods 1.2 these are two names for one value, "
                "so the file has no single reading. Make them equal."
                % (where, method, old, params[old], new, params[new]))
        params[new] = params[old]
    return params


def resolve_carrier_profile(method, fab_params, body_diameter_um, pdk_table):
    """Return explicit (opening, enclosure) in um, or refuse an unsafe profile.

    ``pdk_table`` is the loaded generator's own table, not a copied PDK rule,
    and empty for a pad cell whose opening always comes from the method.
    No table/default value substitutes for a missing manifest parameter.
    ``fab_params`` is method_fab_params' dict: the 1.2 names are read.
    """
    if not isinstance(fab_params, dict):
        raise ValueError("%s: fab_params must be a mapping" % method)
    opening = _positive_number(
        fab_params.get("passivation_opening_um"),
        "%s: fab_params.passivation_opening_um" % method)
    enclosure = _positive_number(
        fab_params.get("pad_metal_enclosure_um"),
        "%s: fab_params.pad_metal_enclosure_um" % method)
    body = _positive_number(body_diameter_um, "%s: body diameter" % method)
    option = pdk_table.get(body)
    if option is not None and opening != option["passiv_opening"]:
        raise ValueError(
            "%s: manifest opening %s um differs from the PDK generator's "
            "table opening %s um for body %s um; this adapter cannot honor "
            "that profile" % (method, opening, option["passiv_opening"], body))
    if not math.isfinite(opening + 2 * enclosure):
        raise ValueError("%s: carrier landing diameter is not finite" % method)
    return opening, enclosure


def check_anchor_drawable(method, anchor, fab_anchors, drawn_openings):
    """Refuse a method whose fabrication anchor the pad generator cannot draw.

    ``fab_anchors`` is the manifest's table; ``drawn_openings`` is the set of
    (layer, datatype) UBM openings the loaded pad generator draws. The anchor
    is drawable when the openings it declares are exactly those.
    """
    if not isinstance(anchor, str) or not anchor:
        raise ValueError("%s: fab_anchor must be a nonempty string" % method)
    entry = fab_anchors.get(anchor) if isinstance(fab_anchors, dict) else None
    layers = entry.get("layers") if isinstance(entry, dict) else None
    if not isinstance(layers, dict) or not layers:
        raise ValueError("%s: the manifest declares no UBM layers for fab anchor %s"
                         % (method, anchor))
    declared = set()
    # The schema's pattern, checked here: load_manifest gates only the version.
    for spec in layers.values():
        match = re.fullmatch(r"([0-9]+)/([0-9]+)", spec) if isinstance(spec, str) else None
        if match is None:
            raise ValueError("%s: fab anchor %s has a malformed layer %r"
                             % (method, anchor, spec))
        declared.add((int(match[1]), int(match[2])))
    drawn = set(drawn_openings)
    if declared != drawn:
        raise ValueError(
            "%s: fab anchor %s opens %s, but the pad generator draws %s; this "
            "adapter cannot draw that landing"
            % (method, anchor, _layer_list(declared), _layer_list(drawn)))


def _layer_list(pairs):
    return ", ".join("%d/%d" % pair for pair in sorted(pairs)) or "nothing"
