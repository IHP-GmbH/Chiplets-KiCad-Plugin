# SPDX-License-Identifier: GPL-3.0-or-later
"""
resizer passive elements plugin for KiCad pcbnew.

Registers a single pcbnew.ActionPlugin under Tools > External Plugins
("resizer passive elements", category "Chiplet / resizer passive elements") that opens one window
with a single "Run" button (chains Scan, Generate, Apply, Refresh) plus
the shared log. See README.md for install/usage and ARCHITECTURE.md for
the design.

The import is wrapped in try/except (ImportError, AttributeError),
mirroring the sibling chiplet_export plugin's __init__.py, so this package stays
importable on a host without a real pcbnew (tests, headless tooling) --
ActionPlugin only exists inside a running pcbnew process.
"""

# Bumped on every code change. Printed as the first line of "Scan
# Elements"' log (dialog_log.py) so a rebuilt/redeployed Docker image
# can be confirmed to actually be running the current code, rather than
# a stale cached copy -- Python caches imported modules, and reloading
# the plugin (or even a fresh KiCad process against an unchanged mount)
# does not guarantee the .py files on disk actually changed underneath it.
__version__ = "0.15.1"

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
    from .action_resizer_passive_elements import ResizerPassiveElementsPlugin
except (ImportError, AttributeError):
    ResizerPassiveElementsPlugin = None
else:
    if _loaded_by_pcbnew():
        ResizerPassiveElementsPlugin().register()
