# SPDX-License-Identifier: GPL-2.0-or-later
"""
Chiplet Export plugin for KiCad pcbnew.

Single-action workflow: produces canonical .chiplet, interposer GDS,
optionally a complete-assembly GDS, and intermediate .hyp from a
loaded board, by wrapping the hyp_to_gds.py pipeline.

Registered automatically when pcbnew scans the scripting/plugins
directory.
"""

try:
    from .chiplet_export_action import ChipletExportPlugin
except ImportError:
    # pcbnew is not importable outside KiCad (unit tests, CI, headless
    # tooling). Submodules that do not depend on pcbnew remain usable.
    ChipletExportPlugin = None
else:
    ChipletExportPlugin().register()
