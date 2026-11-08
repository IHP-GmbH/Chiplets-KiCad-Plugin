# SPDX-License-Identifier: GPL-3.0-or-later
"""The technology id the ``.chiplet`` writer gives a die's layer sheet.

A die names its technology only through its ``LYP_FILE``. A PDK's sheet is
named after its technology (``sg13g2.lyp``, ``intm4tm2.lyp``), so the file stem
is the id. A project keeps its own sheet per technology instead, as
``inputs/tech/<technology id>/layers.lyp`` (PROJECT_SHEET): every such sheet
has the same name, and the technology is the folder that holds it. The stem
there would name every project technology ``layers`` and fold them into one
entry.

Kept free of pcbnew so the rule is testable without KiCad. The C++ exporter
(kicad/pcbnew/exporters/export_chiplet.cpp) still takes the stem, so the two
differ on a board with a project sheet, and only there.
"""

import os

#: The fixed name of a project's layer sheet, which sits in its technology's
#: folder: ``inputs/tech/<technology id>/layers.lyp``. The one place the name
#: is spelled.
PROJECT_SHEET = "layers.lyp"


def technology_id(lyp_file):
    """The technology id for the sheet ``lyp_file``.

    The folder that holds a PROJECT_SHEET, otherwise the file stem; spaces and
    dots become underscores either way. A PROJECT_SHEET with no technology
    folder above it (bare, ``./``, or right under a ``${VAR}`` root) keeps the
    stem, ``layers``.
    """
    name = os.path.basename(lyp_file)
    tech = os.path.splitext(name)[0]
    if name == PROJECT_SHEET:
        folder = os.path.basename(os.path.dirname(lyp_file))
        if folder not in ("", ".", "..") and "${" not in folder:
            tech = folder
    return tech.replace(" ", "_").replace(".", "_")
