# SPDX-License-Identifier: GPL-3.0-or-later
"""
Single interactive window for the resizer passive elements plugin.
"""

import os
from pathlib import Path

import wx

from . import __version__, paths
from .apply_resize import apply_to_instance, generate_footprint_file, load_tech
from .board_reader import find_supported_footprints


class ResizerPassiveElementsDialog(wx.Dialog):

    def __init__(self, parent, board=None, title="resizer passive elements"):
        super().__init__(
            parent, title=title, size=(900, 900),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._board = board

        saved_root, saved_tech, saved_gen, saved_out = paths.load_path_overrides(board)
        self._initial_root = saved_root if saved_root and os.path.isdir(saved_root) else ""
        self._initial_tech = (saved_tech if saved_tech and os.path.isfile(saved_tech)
                               else paths.discover_tech_json_path(board))
        self._initial_gen = (saved_gen if saved_gen and os.path.isfile(saved_gen)
                              else paths.discover_footprint_gen_path(board))
        self._initial_out = saved_out or paths.discover_output_pretty_dir(board)

        self._build_ui()
        self.CentreOnParent()

    def _build_ui(self):
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(rows=4, cols=3, vgap=6, hgap=6)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(panel, label="OpenIntM4TM2 root folder:"),
                  0, wx.ALIGN_CENTER_VERTICAL)
        self._root_ctrl = wx.TextCtrl(panel, value=self._initial_root)
        self._root_ctrl.SetToolTip(
            "Optional: the OpenIntM4TM2 checkout folder (the one that "
            'contains "libs.tech"). Typing or picking it auto-fills the '
            "two fields below when it finds them there -- useful when the "
            "checkout isn't where auto-discovery expects it (e.g. an "
            "older Docker image that doesn't have it yet).")
        self._root_ctrl.Bind(wx.EVT_TEXT, self._on_root_changed)
        grid.Add(self._root_ctrl, 1, wx.EXPAND)
        root_browse = wx.Button(panel, label="...", style=wx.BU_EXACTFIT)
        root_browse.Bind(wx.EVT_BUTTON, self._on_browse_root)
        grid.Add(root_browse, 0)

        grid.Add(wx.StaticText(panel, label="intm4tm2_tech.json:"),
                  0, wx.ALIGN_CENTER_VERTICAL)
        self._tech_ctrl = wx.TextCtrl(panel, value=self._initial_tech)
        self._tech_ctrl.SetToolTip(
            "Path to intm4tm2_tech.json in the OpenIntM4TM2 checkout. "
            "Auto-filled by the root folder above (if found there) or by "
            "auto-discovery; also editable by hand or via the picker.")
        grid.Add(self._tech_ctrl, 1, wx.EXPAND)
        tech_browse = wx.Button(panel, label="...", style=wx.BU_EXACTFIT)
        tech_browse.Bind(wx.EVT_BUTTON, self._on_browse_tech)
        grid.Add(tech_browse, 0)

        grid.Add(wx.StaticText(panel, label="cmim_footprint_gen.py:"),
                  0, wx.ALIGN_CENTER_VERTICAL)
        self._gen_ctrl = wx.TextCtrl(panel, value=self._initial_gen)
        self._gen_ctrl.SetToolTip(
            "Path to libs.tech/kicad/scripts/cmim_footprint_gen.py in the "
            "OpenIntM4TM2 checkout (never modified, only imported). "
            "Auto-filled by the root folder above or by auto-discovery; "
            "also editable by hand or via the picker -- needed when "
            "neither of those auto-fills it.")
        grid.Add(self._gen_ctrl, 1, wx.EXPAND)
        gen_browse = wx.Button(panel, label="...", style=wx.BU_EXACTFIT)
        gen_browse.Bind(wx.EVT_BUTTON, self._on_browse_gen)
        grid.Add(gen_browse, 0)

        grid.Add(wx.StaticText(panel, label="Local output .pretty folder:"),
                  0, wx.ALIGN_CENTER_VERTICAL)
        self._out_ctrl = wx.TextCtrl(panel, value=self._initial_out)
        self._out_ctrl.SetToolTip(
            "LOCAL folder where generated .kicad_mod files are written. "
            "Never the shared OpenIntM4TM2 repository's .pretty -- not "
            "auto-filled from the root folder above.")
        grid.Add(self._out_ctrl, 1, wx.EXPAND)
        out_browse = wx.Button(panel, label="...", style=wx.BU_EXACTFIT)
        out_browse.Bind(wx.EVT_BUTTON, self._on_browse_out)
        grid.Add(out_browse, 0)

        outer.Add(grid, 0, wx.EXPAND | wx.ALL, 8)

        # --- single run button ---
        run_row = wx.BoxSizer(wx.HORIZONTAL)
        self._run_btn = wx.Button(panel, label="Run")
        self._run_btn.SetToolTip(
            "Runs Scan, Generate, Apply and Refresh in sequence, once "
            "each, using the fields above exactly as they are right now "
            "(not whatever they were the last time this was clicked).")
        run_row.Add(self._run_btn, 0, wx.ALL, 4)
        outer.Add(run_row, 0, wx.ALL, 8)

        self._run_btn.Bind(wx.EVT_BUTTON, self._on_run)

        # --- shared log --
        self._log_ctrl = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        mono = wx.Font(wx.FontInfo(10).Family(wx.FONTFAMILY_TELETYPE))
        self._log_ctrl.SetFont(mono)
        self._log_ctrl.SetMinSize(wx.Size(-1, 350))
        outer.Add(self._log_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        btns.AddStretchSpacer(1)
        close_btn = wx.Button(panel, label="Close")
        close_btn.Bind(wx.EVT_BUTTON, self._on_close)
        btns.Add(close_btn, 0, wx.ALL, 4)
        outer.Add(btns, 0, wx.EXPAND | wx.ALL, 4)

        self.Bind(wx.EVT_CLOSE, self._on_close)

        panel.SetSizer(outer)
        outer.SetSizeHints(self)
        self.SetSize(wx.Size(900, 900))
        self._panel = panel

    # ------------------------------------------------------------------
    # Path field accessors + log
    # ------------------------------------------------------------------

    def root_dir(self):
        return self._root_ctrl.GetValue().strip()

    def tech_json_path(self):
        return self._tech_ctrl.GetValue().strip()

    def gen_script_path(self):
        return self._gen_ctrl.GetValue().strip()

    def output_dir(self):
        return self._out_ctrl.GetValue().strip()

    def log(self, line):
        self._log_ctrl.AppendText(str(line) + "\n")

    def _header(self, title):
        if self._log_ctrl.GetValue():
            self.log("")
        self.log("=== {} ===".format(title))

    def _load_tech_or_log_error(self):
        tech_json_path = self.tech_json_path()
        gen_script_path = self.gen_script_path() or None
        try:
            return load_tech(tech_json_path, gen_script_path=gen_script_path)
        except Exception as exc:
            self.log("ERROR: could not load {}: {}".format(tech_json_path, exc))
            return None

    # ------------------------------------------------------------------
    # Root-folder auto-fill
    # ------------------------------------------------------------------

    def _on_root_changed(self, _event):
        tech_found, gen_found = paths.resolve_from_root(self.root_dir())
        if tech_found:
            self._tech_ctrl.SetValue(tech_found)
        if gen_found:
            self._gen_ctrl.SetValue(gen_found)

    def _on_scan(self, _event):
        # First line, always: lets a rebuilt/redeployed environment (e.g.
        # after a Docker image rebuild) be checked against a known-good
        # version instead of guessing whether the code actually changed.
        self.log("version:{}".format(__version__))
        self._header("Scan Elements")
        capacitors = find_supported_footprints(self._board, on_log=self.log)
        for cap in capacitors:
            w = "{:g}um".format(cap["w_um"]) if cap["w_um"] is not None else "?"
            l = "{:g}um".format(cap["l_um"]) if cap["l_um"] is not None else "?"
            c = ("{:.2f}fF".format(cap["capacitance_fF"])
                 if cap["capacitance_fF"] is not None else "?")
            self.log("{}: w={} l={} C={}".format(cap["reference"], w, l, c))
        self.log("Found {} cap_cmim capacitor(s) on the board.".format(
            len(capacitors)))

    def _on_generate(self, _event):
        self._header("Generate Footprint")
        tech = self._load_tech_or_log_error()
        if tech is None:
            return
        output_dir = self.output_dir()
        gen_script_path = self.gen_script_path() or None

        capacitors = find_supported_footprints(self._board, on_log=self.log)
        written, errors = 0, 0
        for cap in capacitors:
            path = generate_footprint_file(
                cap, tech, output_dir, on_log=self.log,
                gen_script_path=gen_script_path)
            if path is not None:
                written += 1
            else:
                errors += 1
        self.log("Generated {} file(s), {} error(s).".format(written, errors))

    def _on_apply(self, _event):
        self._header("Apply to Board")
        tech = self._load_tech_or_log_error()
        if tech is None:
            return
        output_dir = self.output_dir()
        gen_script_path = self.gen_script_path() or None

        capacitors = find_supported_footprints(self._board, on_log=self.log)
        total = len(capacitors)
        self.log("Found {} cap_cmim capacitor(s) on the board.".format(total))

        applied, errors = 0, 0
        for index, cap in enumerate(capacitors, start=1):
            self.log("[{}/{}] {}".format(index, total, cap["reference"]))

            mod_path = generate_footprint_file(
                cap, tech, output_dir, on_log=self.log,
                gen_script_path=gen_script_path)
            if mod_path is None:
                errors += 1
                continue

            ok = apply_to_instance(
                self._board, cap["footprint_obj"], mod_path, params=cap,
                on_log=self.log)
            if ok:
                applied += 1
            else:
                errors += 1

        if applied and self._board is not None:
            self._mark_modified()

        self.log("Done: {} applied, {} error(s).".format(applied, errors))

    def _on_refresh(self, _event):
        self._header("Refresh View")
        try:
            import pcbnew
            pcbnew.Refresh()
            self.log("View refreshed.")
        except Exception as exc:
            self.log("ERROR: {}".format(exc))

    def _on_run(self, _event):
        """ Run all four steps in sequence."""
        self._on_scan(None)
        self._on_generate(None)
        self._on_apply(None)
        self._on_refresh(None)

    def _mark_modified(self):
        try:
            import pcbnew
            pcbnew.Refresh()
        except Exception:
            pass
        for method_name in ("OnModify", "SetModified"):
            method = getattr(self._board, method_name, None)
            if callable(method):
                try:
                    method()
                    return
                except Exception:
                    continue
        self.log("Note: save the board manually (Ctrl+S) to keep the changes.")

    # ------------------------------------------------------------------
    # Path browse / close
    # ------------------------------------------------------------------

    def _on_browse_root(self, _event):
        start_dir = self.root_dir() or str(Path.home())
        dlg = wx.DirDialog(self, "Select the OpenIntM4TM2 root folder",
                            defaultPath=start_dir)
        try:
            if dlg.ShowModal() == wx.ID_OK:
                # SetValue fires EVT_TEXT, which runs the auto-fill.
                self._root_ctrl.SetValue(dlg.GetPath())
        finally:
            dlg.Destroy()

    def _on_browse_tech(self, _event):
        start_dir = os.path.dirname(self.tech_json_path()) or str(Path.home())
        dlg = wx.FileDialog(
            self, "Select intm4tm2_tech.json",
            defaultDir=start_dir,
            wildcard="JSON (*.json)|*.json|All files|*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if dlg.ShowModal() == wx.ID_OK:
                self._tech_ctrl.SetValue(dlg.GetPath())
        finally:
            dlg.Destroy()

    def _on_browse_gen(self, _event):
        start_dir = os.path.dirname(self.gen_script_path()) or str(Path.home())
        dlg = wx.FileDialog(
            self, "Select cmim_footprint_gen.py",
            defaultDir=start_dir,
            wildcard="Python (*.py)|*.py|All files|*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if dlg.ShowModal() == wx.ID_OK:
                self._gen_ctrl.SetValue(dlg.GetPath())
        finally:
            dlg.Destroy()

    def _on_browse_out(self, _event):
        start_dir = self.output_dir() or str(Path.home())
        dlg = wx.DirDialog(self, "Select the local output .pretty folder",
                            defaultPath=start_dir)
        try:
            if dlg.ShowModal() == wx.ID_OK:
                self._out_ctrl.SetValue(dlg.GetPath())
        finally:
            dlg.Destroy()

    def _on_close(self, _event):
        root = self.root_dir()
        tech = self.tech_json_path()
        gen = self.gen_script_path()
        out = self.output_dir()
        if (root, tech, gen, out) != (
                self._initial_root, self._initial_tech,
                self._initial_gen, self._initial_out):
            paths.save_path_overrides(self._board, root, tech, gen, out,
                                      on_log=self.log)
        self.EndModal(wx.ID_CLOSE)
