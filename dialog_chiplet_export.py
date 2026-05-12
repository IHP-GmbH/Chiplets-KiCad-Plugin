# SPDX-License-Identifier: GPL-2.0-or-later
"""
Modal dialog driving the chiplet export pipeline.

Iter 1 scope (lands in Gate 47.6):
  - Output directory selection
  - Output toggles (.hyp / interposer GDS / canonical .chiplet /
    complete .gds / I/O pad sidecar JSON)
  - Pipeline options (top cell, connection stack, LYP override)
  - Live log streaming from the hyp_to_gds.py worker subprocess
  - Worker venv discovery diagnostics

This file currently provides a stub dialog that lets the user verify
the plugin loads and is wired into pcbnew.
"""

import wx


class ChipletExportDialog(wx.Dialog):
    """Placeholder dialog. Full UI lands in Gate 47.6."""

    def __init__(self, parent):
        super().__init__(
            parent,
            title="Chiplet Export",
            size=(440, 220),
            style=wx.DEFAULT_DIALOG_STYLE,
        )

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        msg = wx.StaticText(
            panel,
            label=(
                "Chiplet Export plugin loaded.\n\n"
                "Implementation in progress (TaskList #47, Gate 47.6).\n"
                "Until then, use the legacy File > Export > Chiplet... "
                "action and run hyp_to_gds.py manually."
            ),
        )
        sizer.Add(msg, 1, wx.ALL | wx.EXPAND, 16)

        button_sizer = self.CreateButtonSizer(wx.CLOSE)
        if button_sizer is not None:
            sizer.Add(button_sizer, 0, wx.ALL | wx.EXPAND, 8)

        panel.SetSizer(sizer)
        self.Bind(wx.EVT_BUTTON, self._on_close, id=wx.ID_CLOSE)

    def _on_close(self, _event):
        self.EndModal(wx.ID_CLOSE)
