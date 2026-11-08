# SPDX-License-Identifier: GPL-3.0-or-later
"""
ActionPlugin: "resizer passive elements".

Single entry point under Tools > External Plugins. Run() only opens the
ResizerPassiveElementsDialog (dialog_log.py) -- it never scans, generates or applies
anything by itself. That window's single "Run" button chains Scan,
Generate, Apply and Refresh, and only runs when clicked, reading
whatever is currently in the path fields at that moment.
"""

import pcbnew


class ResizerPassiveElementsPlugin(pcbnew.ActionPlugin):

    def defaults(self):
        self.name = "resizer passive elements"
        self.category = "Chiplet / resizer passive elements"
        self.description = (
            "Tools to resize cap_cmim footprints: Scan Elements, "
            "Generate Footprint, Apply to Board and Refresh View "
            "(plus a Run All shortcut), all in one window."
        )
        self.show_toolbar_button = False
        self.icon_file_name = ""

    def Run(self):
        import wx

        from .dialog_log import ResizerPassiveElementsDialog

        try:
            board = pcbnew.GetBoard()
        except Exception:
            board = None

        parent = None
        top_levels = wx.GetTopLevelWindows()
        if top_levels:
            parent = top_levels[0]

        dialog = ResizerPassiveElementsDialog(parent, board=board, title="resizer passive elements")
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
