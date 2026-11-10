# SPDX-License-Identifier: GPL-3.0-or-later
"""
Chiplet Export plugin for KiCad pcbnew.

Single-action workflow: produces canonical .chiplet, interposer GDS,
the driving Hyperlynx .hyp, and optionally a complete-assembly GDS from
a loaded board, by wrapping the hyp_to_gds.py pipeline.

Registered automatically when pcbnew scans the scripting/plugins
directory.
"""

import sys


def _loaded_by_pcbnew():
    """True while pcbnew's plugin loader imports this package.

    Only a running pcbnew calls pcbnew.LoadPlugins. Registering an action
    anywhere else (tests, headless tooling) has no KiCad program behind it:
    the first registration trips an assert and the second aborts the process.
    """
    frame = sys._getframe(1)
    while frame is not None:
        if (frame.f_code.co_name in ("LoadPlugins", "LoadPluginModule")
                and frame.f_globals.get("__name__") == "pcbnew"):
            return True
        frame = frame.f_back
    return False


try:
    from .chiplet_export_action import ChipletExportPlugin
except (ImportError, AttributeError):
    # pcbnew is not importable, or the host has a stub pcbnew without
    # ActionPlugin (the SWIG class is only exposed inside a running
    # pcbnew process). Submodules that do not depend on pcbnew remain
    # usable for tests and headless tooling.
    ChipletExportPlugin = None
else:
    if _loaded_by_pcbnew():
        ChipletExportPlugin().register()
