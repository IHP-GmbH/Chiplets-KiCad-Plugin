# SPDX-License-Identifier: GPL-3.0-or-later
"""Sibling checkouts, for both plugin suites.

A case whose verdict reads a sibling repository's tree carries the
``needs_sibling_sources`` marker. Its door is the variable that names that
sibling. When the variable is unset and ``HCDP_ROOT`` is set, the sibling is
``$HCDP_ROOT/<id>``, the layout a tiered runner exports; this runs before any
test module is imported, so module-level lookups see it too.

Under such a runner (``HCDP_CI`` set) the parent-directory walk is refused:
from an exported tree it finds nothing, or a checkout nobody pinned. The
cases that assert the walk itself walk a workspace that links the siblings
the variables name (``sibling_workspace`` in the chiplet_export tests).
"""

import os
from pathlib import Path

# variable -> sibling id under HCDP_ROOT
SIBLINGS = {
    "INTERPOSER_PDK_ROOT": "interposer",
    "INTERCONNECT_PDK_ROOT": "interconnect_pdk",
    "CHIPLET_SPEC_ROOT": "chiplet-spec",
    "GDS_TO_KICAD_ROOT": "gds_to_kicad",
    "ADK_ROOT": "adk",
}


def _resolve_siblings():
    root = os.environ.get("HCDP_ROOT")
    if not root:
        return
    for var, sibling in SIBLINGS.items():
        candidate = Path(root) / sibling
        if not os.environ.get(var) and candidate.is_dir():
            os.environ[var] = str(candidate)


_resolve_siblings()
