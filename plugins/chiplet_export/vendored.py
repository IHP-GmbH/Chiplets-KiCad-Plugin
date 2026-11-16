# SPDX-License-Identifier: GPL-3.0-or-later
"""The vendored ``chiplet_format_io``, reached the one way the worker reaches it.

Lazy and path-robust so it resolves whether the worker runs as
``python hyp_to_gds.py`` or is imported. The package imports PyYAML, so only
the worker tier calls this; importing this module imports nothing. Fixes in
the reader belong upstream in chiplet-spec and come back as a re-vendor; do
not edit ``vendor/chiplet_format_io`` in place.
"""

import os
import sys


def chiplet_format_io():
    """The vendored ``chiplet_format_io`` module."""
    vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    import chiplet_format_io as cfio
    return cfio
