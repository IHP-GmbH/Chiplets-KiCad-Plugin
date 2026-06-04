# SPDX-License-Identifier: GPL-2.0-or-later
"""
Modal dialog driving the chiplet export pipeline.

Collects ExportOptions from the user, spawns the orchestrator on a
worker thread so the wxFrame stays responsive, and streams log lines
into a read-only text control via ``wx.CallAfter``.

The pipeline body lives in ``pipeline/orchestrator.py``; this file
contains only UI plumbing.
"""

import threading
from pathlib import Path

import wx

from .pipeline.orchestrator import (
    ExportOptions, ExportResult, run_export, available_connection_types,
    describe_assembly_drc,
)


# Sourced from the interconnect PDK manifest (falls back to the built-in IHP set
# if the PDK is not importable). Includes any vendor demo method, so a non-IHP
# bumping method is selectable from the dialog with no code change.
_CONNECTION_TYPE_CHOICES = available_connection_types()


class ChipletExportDialog(wx.Dialog):
    """Single-button chiplet export dialog."""

    def __init__(self, parent, board=None, plugin_dir=None):
        super().__init__(
            parent,
            title="Chiplet Export",
            size=(820, 640),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )

        self._board = board
        self._plugin_dir = (str(Path(plugin_dir).resolve())
                            if plugin_dir
                            else str(Path(__file__).resolve().parent))
        self._cancel_event = threading.Event()
        self._worker_thread = None

        self._build_ui()
        self.CentreOnParent()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self):
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        # Output directory
        out_box = wx.StaticBoxSizer(wx.HORIZONTAL, panel, "Output directory")
        self._out_dir_ctrl = wx.DirPickerCtrl(
            panel, path=self._default_out_dir())
        out_box.Add(self._out_dir_ctrl, 1, wx.EXPAND | wx.ALL, 4)
        outer.Add(out_box, 0, wx.EXPAND | wx.ALL, 8)

        # Output toggles
        outs_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Outputs")
        self._cb_chiplet = wx.CheckBox(panel, label="Canonical .chiplet")
        self._cb_chiplet.SetValue(True)
        self._cb_interposer = wx.CheckBox(panel, label="Interposer GDS")
        self._cb_interposer.SetValue(True)
        self._cb_complete = wx.CheckBox(
            panel, label="Complete assembly GDS (with chiplet instances)")
        self._cb_keep_hyp = wx.CheckBox(
            panel, label="Keep intermediate .hyp in output directory")
        self._cb_annotate = wx.CheckBox(
            panel, label="Annotate chiplet boundaries (viewer-only layer)")
        self._cb_annotate.SetToolTip(
            "Paint each chiplet's mechanical boundary and instance label onto "
            "an annotation GDS layer (1000/0) for eyeball inspection in "
            "KLayout. No DRC rule reads it; the assembly contract stays in the "
            "boundary manifest. Off by default.")
        for cb in (self._cb_chiplet, self._cb_interposer,
                   self._cb_complete, self._cb_keep_hyp, self._cb_annotate):
            outs_box.Add(cb, 0, wx.ALL, 2)
        outer.Add(outs_box, 0, wx.EXPAND | wx.ALL, 8)

        # Pipeline options
        opts_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Pipeline options")
        grid = wx.FlexGridSizer(rows=3, cols=2, vgap=4, hgap=8)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(panel, label="Top cell:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self._top_cell_ctrl = wx.TextCtrl(panel, value="INTERPOSER")
        grid.Add(self._top_cell_ctrl, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Connection stack:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self._conn_ctrl = wx.Choice(panel, choices=_CONNECTION_TYPE_CHOICES)
        self._conn_ctrl.SetSelection(0)
        grid.Add(self._conn_ctrl, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Interposer technology LYP:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self._lyp_ctrl = wx.FilePickerCtrl(
            panel,
            wildcard="Layer properties (*.lyp)|*.lyp|All files|*",
        )
        self._lyp_ctrl.SetToolTip(
            "Layer-properties (.lyp) of the interposer technology. Leave "
            "blank to use the built-in IHP interposer LYP. Select one only "
            "when the interposer uses a different technology / KiCad "
            "project template.")
        grid.Add(self._lyp_ctrl, 1, wx.EXPAND)

        opts_box.Add(grid, 0, wx.EXPAND | wx.ALL, 4)
        outer.Add(opts_box, 0, wx.EXPAND | wx.ALL, 8)

        # Worker python override
        worker_box = wx.StaticBoxSizer(
            wx.HORIZONTAL, panel,
            "Worker Python override (optional; .venv/bin/python3 auto-detected)",
        )
        self._worker_ctrl = wx.FilePickerCtrl(
            panel, wildcard="Python interpreter|*|All files|*")
        worker_box.Add(self._worker_ctrl, 1, wx.EXPAND | wx.ALL, 4)
        outer.Add(worker_box, 0, wx.EXPAND | wx.ALL, 8)

        # Log
        self._log_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        mono = wx.Font(wx.FontInfo(10).Family(wx.FONTFAMILY_TELETYPE))
        self._log_ctrl.SetFont(mono)
        outer.Add(self._log_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        # Status + buttons
        self._status = wx.StaticText(panel, label="Idle")
        outer.Add(self._status, 0, wx.LEFT | wx.RIGHT, 8)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self._run_btn = wx.Button(panel, label="Run")
        self._cancel_btn = wx.Button(panel, label="Cancel")
        self._close_btn = wx.Button(panel, label="Close")
        self._cancel_btn.Disable()
        btns.AddStretchSpacer(1)
        btns.Add(self._run_btn, 0, wx.ALL, 4)
        btns.Add(self._cancel_btn, 0, wx.ALL, 4)
        btns.Add(self._close_btn, 0, wx.ALL, 4)
        outer.Add(btns, 0, wx.EXPAND | wx.ALL, 4)

        self._run_btn.Bind(wx.EVT_BUTTON, self._on_run)
        self._cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel)
        self._close_btn.Bind(wx.EVT_BUTTON, self._on_close)
        self.Bind(wx.EVT_CLOSE, self._on_close)

        panel.SetSizer(outer)
        outer.SetSizeHints(self)

    def _default_out_dir(self):
        if self._board is not None:
            try:
                board_file = self._board.GetFileName()
                if board_file:
                    return str(Path(board_file).parent)
            except Exception:
                pass
        return str(Path.home())

    # ------------------------------------------------------------------
    # Options collection
    # ------------------------------------------------------------------

    def _collect_options(self):
        idx = self._conn_ctrl.GetSelection()
        if idx is None or idx < 0:
            conn = ""
        else:
            conn = _CONNECTION_TYPE_CHOICES[idx]
        return ExportOptions(
            output_dir=self._out_dir_ctrl.GetPath(),
            emit_chiplet=self._cb_chiplet.GetValue(),
            emit_interposer_gds=self._cb_interposer.GetValue(),
            emit_complete_gds=self._cb_complete.GetValue(),
            keep_intermediate_hyp=self._cb_keep_hyp.GetValue(),
            annotate_boundaries=self._cb_annotate.GetValue(),
            top_cell=self._top_cell_ctrl.GetValue() or "INTERPOSER",
            connection_type=conn,
            lyp_override=self._lyp_ctrl.GetPath() or "",
            worker_python_override=self._worker_ctrl.GetPath() or "",
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_run(self, _event):
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return

        options = self._collect_options()
        if not options.output_dir:
            wx.MessageBox(
                "Please select an output directory.",
                "Chiplet Export",
                wx.OK | wx.ICON_WARNING,
            )
            return
        if not (options.emit_chiplet or options.emit_interposer_gds
                or options.emit_complete_gds):
            wx.MessageBox(
                "Enable at least one output (canonical .chiplet, "
                "interposer GDS, or complete-assembly GDS).",
                "Chiplet Export",
                wx.OK | wx.ICON_WARNING,
            )
            return

        self._log_ctrl.SetValue("")
        self._cancel_event = threading.Event()
        self._set_running(True)

        def _worker():
            try:
                result = run_export(
                    self._board, options, self._plugin_dir,
                    on_log=self._append_log_safe,
                    cancel_event=self._cancel_event,
                )
            except Exception as exc:
                import traceback
                tb = traceback.format_exc()
                self._append_log_safe("FATAL: %s" % exc)
                for line in tb.rstrip().splitlines():
                    self._append_log_safe(line)
                result = ExportResult(
                    exit_code=-1,
                    error="Worker thread crashed: %s" % exc,
                )
            wx.CallAfter(self._on_done, result)

        self._worker_thread = threading.Thread(target=_worker, daemon=True)
        self._worker_thread.start()

    def _on_cancel(self, _event):
        self._cancel_event.set()
        self._set_status("Cancelling ...")
        self._cancel_btn.Disable()

    def _on_close(self, _event):
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._cancel_event.set()
        self.EndModal(wx.ID_CLOSE)

    def _on_done(self, result):
        self._set_running(False)
        if result.error:
            self._append_log("ERROR: " + result.error)
            self._set_status("Error: " + _short(result.error))
        elif result.cancelled:
            self._set_status("Cancelled")
        elif result.exit_code == 0:
            for label, path in (("chiplet", result.chiplet_path),
                                ("interposer GDS", result.interposer_gds_path),
                                ("complete GDS", result.complete_gds_path),
                                ("cu-pillar DRC report", result.cupillar_drc_path),
                                ("intermediate hyp", result.hyp_path)):
                if path:
                    self._append_log("Wrote %s: %s" % (label, path))
            verdict = describe_assembly_drc(result)
            self._append_log(verdict)
            if result.assembly_drc_report_path:
                self._append_log("  report: %s"
                                 % result.assembly_drc_report_path)
            if result.assembly_drc_exit_code > 0:
                self._set_status("Done - assembly DRC FAILED")
            else:
                self._set_status("Done (exit 0)")
        else:
            self._set_status("Failed (exit %d)" % result.exit_code)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _append_log_safe(self, line):
        wx.CallAfter(self._append_log, line)

    def _append_log(self, line):
        self._log_ctrl.AppendText(line + "\n")

    def _set_status(self, text):
        self._status.SetLabel(text)

    def _set_running(self, running):
        self._run_btn.Enable(not running)
        self._cancel_btn.Enable(running)
        if running:
            self._set_status("Running ...")


def _short(msg, limit=80):
    msg = msg.replace("\n", " ")
    return msg if len(msg) <= limit else msg[:limit - 3] + "..."
