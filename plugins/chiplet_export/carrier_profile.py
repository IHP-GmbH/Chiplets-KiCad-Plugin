# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate manifest geometry against the existing CuPillarPad adapter.

The interconnect manifest owns the opening and metal enclosure. The PDK
adapter currently overrides the opening for table-known body diameters; a
profile it cannot faithfully draw must be refused, not silently substituted.
This module has no KLayout or pcbnew dependency.
"""

import math


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


def resolve_carrier_profile(method, fab_params, body_diameter_um, pdk_table):
    """Return explicit (opening, enclosure) in um, or refuse an unsafe profile.

    ``pdk_table`` is the loaded generator's own table, not a copied PDK rule.
    No table/default value substitutes for a missing manifest parameter.
    """
    if not isinstance(fab_params, dict):
        raise ValueError("%s: fab_params must be a mapping" % method)
    opening = _positive_number(
        fab_params.get("passiv_opening_um"),
        "%s: fab_params.passiv_opening_um" % method)
    enclosure = _positive_number(
        fab_params.get("tm2_enclosure_um"),
        "%s: fab_params.tm2_enclosure_um" % method)
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
