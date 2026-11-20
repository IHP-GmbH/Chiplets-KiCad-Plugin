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


def _import_action():
    """The action class, or None where pcbnew is not importable, or is a stub
    without ActionPlugin (the SWIG class is only exposed inside a running
    pcbnew process). Submodules that do not depend on pcbnew remain usable
    for tests and headless tooling."""
    try:
        from .chiplet_export_action import ChipletExportPlugin
    except (ImportError, AttributeError):
        return None
    return ChipletExportPlugin


if _loaded_by_pcbnew():
    ChipletExportPlugin = _import_action()
    if ChipletExportPlugin is not None:
        ChipletExportPlugin().register()
else:
    # Anywhere else the action, and with it pcbnew, is imported only when the
    # attribute is read (PEP 562). Importing a submodule such as
    # chiplet_export.routed must not load pcbnew where it is installed.
    def __getattr__(name):
        if name == "ChipletExportPlugin":
            value = _import_action()
            globals()[name] = value
            return value
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
