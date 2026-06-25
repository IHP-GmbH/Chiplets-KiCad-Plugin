# SPDX-License-Identifier: GPL-3.0-or-later
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
except (ImportError, AttributeError):
    # pcbnew is not importable, or the host has a stub pcbnew without
    # ActionPlugin (the SWIG class is only exposed inside a running
    # pcbnew process). Submodules that do not depend on pcbnew remain
    # usable for tests and headless tooling.
    ChipletExportPlugin = None
else:
    ChipletExportPlugin().register()
