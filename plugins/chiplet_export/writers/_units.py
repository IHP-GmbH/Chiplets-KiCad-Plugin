# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit parsing shared by the board path and the routed path, without pcbnew.

chiplet_writer re-exports both names, so its callers keep importing them
from there.
"""

import math


def _size_um(x, y):
    """(x, y) to the nanometer, or None unless both are finite and above zero."""
    size = (round(x, 3), round(y, 3))
    return size if all(math.isfinite(v) and v > 0.0 for v in size) else None


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
