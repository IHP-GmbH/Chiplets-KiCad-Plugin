# SPDX-License-Identifier: GPL-2.0-or-later
"""
ActionPlugin subclass for "Chiplet Export".

Run() launches the modal dialog that drives the export pipeline.
Dialog and writers are imported lazily so that an import error in a
later-gate module does not break plugin registration.
"""

import pcbnew


class ChipletExportPlugin(pcbnew.ActionPlugin):
    """Single-button chiplet export action for pcbnew."""

    def defaults(self):
        self.name = "Chiplet Export"
        self.category = "Export"
        self.description = (
            "Export a chiplet assembly: canonical .chiplet plus "
            "interposer and (optionally) complete-assembly GDS. "
            "Wraps the hyp_to_gds.py pipeline."
        )
        self.show_toolbar_button = False
        self.icon_file_name = ""

    def Run(self):
        from .dialog_chiplet_export import ChipletExportDialog
        import wx

        parent = None
        top_levels = wx.GetTopLevelWindows()
        if top_levels:
            parent = top_levels[0]

        dialog = ChipletExportDialog(parent)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
