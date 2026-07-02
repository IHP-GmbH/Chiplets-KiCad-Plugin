# SPDX-License-Identifier: GPL-3.0-or-later
"""
Modal dialog driving the chiplet export pipeline.

Collects ExportOptions from the user, spawns the orchestrator on a
worker thread so the wxFrame stays responsive, and streams log lines
into a read-only text control via ``wx.CallAfter``.

The pipeline body lives in ``pipeline/orchestrator.py``; this file
contains only UI plumbing.
"""

import os
import threading
from pathlib import Path

import wx

from .pipeline.orchestrator import (
    ExportOptions, ExportResult, run_export, available_connection_types,
    describe_assembly_drc, discover_dependency_root, discover_interposer_lyp,
)


class _DirBrowseDialog(wx.Dialog):
    """Directory chooser built on wx.GenericDirCtrl.

    Deliberately NOT the native GTK folder chooser: its places dropdown
    ("File System", "Other Locations", network mounts) enumerates eagerly
    and can hang the UI on dead automounts or a missing desktop portal.
    The generic tree expands lazily -- only what the user clicks is read --
    and carries a New-folder button so the target directory can be created
    right here.
    """

    def __init__(self, parent, start_path=""):
        super().__init__(
            parent,
            title="Select directory",
            size=(560, 520),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        outer = wx.BoxSizer(wx.VERTICAL)

        self._tree = wx.GenericDirCtrl(self, style=wx.DIRCTRL_DIR_ONLY)
        start = start_path or str(Path.home())
        if Path(start).is_dir():
            self._tree.SetPath(start)
        outer.Add(self._tree, 1, wx.EXPAND | wx.ALL, 8)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        new_btn = wx.Button(self, label="New folder...")
        new_btn.Bind(wx.EVT_BUTTON, self._on_new_folder)
        btns.Add(new_btn, 0, wx.ALL, 4)
        btns.AddStretchSpacer(1)
        std = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        btns.Add(std, 0, wx.ALL, 4)
        outer.Add(btns, 0, wx.EXPAND | wx.ALL, 4)

        self.SetSizer(outer)

    def GetPath(self):
        return self._tree.GetPath()

    def _on_new_folder(self, _event):
        base = self._tree.GetPath() or str(Path.home())
        name = wx.GetTextFromUser(
            "Name of the new folder under:\n%s" % base,
            "New folder", "", self)
        if not name:
            return
        target = os.path.join(base, name)
        try:
            os.makedirs(target, exist_ok=False)
        except OSError as exc:
            wx.MessageBox("Could not create folder:\n%s" % exc,
                          "New folder", wx.OK | wx.ICON_ERROR, self)
            return
        self._tree.ReCreateTree()
        self._tree.SetPath(target)


class _DirField(wx.Panel):
    """Text field + Browse button for picking a directory.

    Replaces wx.DirPickerCtrl, whose GTK places dropdown froze the dialog
    (see _DirBrowseDialog). The path is plain editable text; Browse opens
    the lazy generic tree. ``on_change`` fires on every path change (typed
    or browsed) so dependent widgets can refresh.
    """

    def __init__(self, parent, path="", on_change=None):
        super().__init__(parent)
        self._on_change = on_change
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        self._text = wx.TextCtrl(self, value=path)
        self._browse = wx.Button(self, label="Browse...",
                                 style=wx.BU_EXACTFIT)
        sizer.Add(self._text, 1, wx.EXPAND | wx.RIGHT, 4)
        sizer.Add(self._browse, 0)
        self.SetSizer(sizer)

        self._browse.Bind(wx.EVT_BUTTON, self._on_browse)
        if on_change is not None:
            self._text.Bind(wx.EVT_TEXT, lambda _e: on_change())

    def GetPath(self):
        return self._text.GetValue().strip()

    def SetPath(self, path):
        self._text.SetValue(path or "")

    def SetToolTip(self, tip):
        self._text.SetToolTip(tip)
        super().SetToolTip(tip)

    def _on_browse(self, _event):
        dlg = _DirBrowseDialog(self, start_path=self.GetPath())
        try:
            if dlg.ShowModal() == wx.ID_OK and dlg.GetPath():
                self._text.SetValue(dlg.GetPath())
        finally:
            dlg.Destroy()


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
        # Set when the dialog is closing so a late wx.CallAfter from the worker
        # thread no-ops instead of touching a window that is being destroyed.
        self._closing = False
        # Debounce timer for the connection-choices refresh (F40): created
        # before _build_ui so the interconnect field's on_change can use it.
        self._conn_refresh_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_conn_refresh_timer,
                  self._conn_refresh_timer)

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
        self._out_dir_ctrl = _DirField(panel, path=self._default_out_dir())
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
        self._cb_annotate = wx.CheckBox(
            panel, label="Annotate chiplet boundaries (viewer-only layer)")
        self._cb_annotate.SetToolTip(
            "Paint each chiplet's mechanical boundary and instance label onto "
            "an annotation GDS layer (1000/0) for eyeball inspection in "
            "KLayout. No DRC rule reads it; the assembly contract stays in the "
            "boundary manifest. Off by default.")
        for cb in (self._cb_chiplet, self._cb_interposer,
                   self._cb_complete, self._cb_annotate):
            outs_box.Add(cb, 0, wx.ALL, 2)
        outer.Add(outs_box, 0, wx.EXPAND | wx.ALL, 8)

        # PDK roots: pre-filled with the discovery chain's result (env var ->
        # project text var -> sibling-checkout walk) so the provenance of
        # every dependency is visible; editable to point the pipeline at any
        # other checkout (vendor fork, release tag). An override travels to
        # the worker as the matching environment variable.
        pdk_box = wx.StaticBoxSizer(
            wx.VERTICAL, panel,
            "PDK roots (auto-discovered; edit to use another checkout)",
        )
        pdk_grid = wx.FlexGridSizer(rows=3, cols=2, vgap=4, hgap=8)
        pdk_grid.AddGrowableCol(1, 1)

        pdk_grid.Add(wx.StaticText(panel, label="Interposer PDK:"),
                     0, wx.ALIGN_CENTER_VERTICAL)
        self._interposer_root_ctrl = _DirField(
            panel,
            path=discover_dependency_root("INTERPOSER_PDK_ROOT", self._board))
        self._interposer_root_ctrl.SetToolTip(
            "Interposer PDK checkout. Supplies the fab pad openings and the "
            "Cu-pillar placement tooling (bump_mirror). Resolved via "
            "$INTERPOSER_PDK_ROOT, the project text variable, or a sibling "
            "checkout; override to export against a different interposer PDK.")
        pdk_grid.Add(self._interposer_root_ctrl, 1, wx.EXPAND)

        pdk_grid.Add(wx.StaticText(panel, label="Interconnect PDK:"),
                     0, wx.ALIGN_CENTER_VERTICAL)
        self._interconnect_root_ctrl = _DirField(
            panel,
            path=discover_dependency_root("INTERCONNECT_PDK_ROOT", self._board),
            on_change=self._schedule_connection_refresh)
        self._interconnect_root_ctrl.SetToolTip(
            "Interconnect PDK checkout. Its manifest defines the connection "
            "stacks below (changing this re-reads the list) plus the 3D "
            "bodies and pitch rules. Resolved via $INTERCONNECT_PDK_ROOT, "
            "the project text variable, or a sibling checkout.")
        pdk_grid.Add(self._interconnect_root_ctrl, 1, wx.EXPAND)

        pdk_grid.Add(wx.StaticText(panel, label="ADK:"),
                     0, wx.ALIGN_CENTER_VERTICAL)
        self._adk_root_ctrl = _DirField(
            panel,
            path=discover_dependency_root("ADK_ROOT", self._board))
        self._adk_root_ctrl.SetToolTip(
            "Assembly Design Kit checkout. Runs the assembly DRC "
            "(klayout/drc/run_drc.py) over the complete GDS with the "
            "interposer + interconnect adapters. Resolved via $ADK_ROOT, "
            "the project text variable, or a sibling checkout.")
        pdk_grid.Add(self._adk_root_ctrl, 1, wx.EXPAND)

        pdk_box.Add(pdk_grid, 0, wx.EXPAND | wx.ALL, 4)
        outer.Add(pdk_box, 0, wx.EXPAND | wx.ALL, 8)

        # Pipeline options
        opts_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Pipeline options")
        grid = wx.FlexGridSizer(rows=3, cols=2, vgap=4, hgap=8)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(panel, label="Top cell:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self._top_cell_ctrl = wx.TextCtrl(panel, value="INTERPOSER")
        grid.Add(self._top_cell_ctrl, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Connection stack (default):"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        # Sourced from the selected interconnect PDK's manifest (vendor
        # methods included); built-in IHP fallback keeps the dialog usable
        # when no PDK is on disk. Per-die overrides below win over this
        # assembly-wide default.
        self._conn_choices = available_connection_types(
            self._interconnect_root_ctrl.GetPath(), board=self._board)
        self._conn_ctrl = wx.Choice(panel, choices=self._conn_choices)
        self._conn_ctrl.SetSelection(0)
        grid.Add(self._conn_ctrl, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Interposer technology LYP:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self._lyp_ctrl = wx.FilePickerCtrl(
            panel,
            wildcard="Layer properties (*.lyp)|*.lyp|All files|*",
        )
        default_lyp = discover_interposer_lyp(board=self._board)
        if default_lyp:
            self._lyp_ctrl.SetPath(default_lyp)
        self._lyp_ctrl.SetToolTip(
            "Layer-properties (.lyp) of the INTERPOSER technology, "
            "pre-filled with the discovered default. Do not point it at "
            "the interconnect .lyp (bump layers only) -- that one is "
            "consumed automatically via the .chiplet. Replace it only "
            "when the interposer uses a different technology; blank "
            "means it is resolved from the interposer PDK "
            "(INTERPOSER_PDK_ROOT) at export, or the export errors asking "
            "you to set it.")
        grid.Add(self._lyp_ctrl, 1, wx.EXPAND)

        opts_box.Add(grid, 0, wx.EXPAND | wx.ALL, 4)
        outer.Add(opts_box, 0, wx.EXPAND | wx.ALL, 8)

        # Per-die connection method: one row per die footprint (GDS_FILE
        # field). Initialized from each footprint's CONNECTION field and
        # written back on Run, so the board stays the source of truth for
        # per-die method selection. A die on "(use default)" follows the
        # assembly-wide stack above; mixed selections give each die its own
        # method's 3D bodies, connection stack and DRC numbers.
        self._die_conn_ctrls = {}
        self._die_conn_items = {}
        die_refs = self._die_refs()
        if die_refs:
            die_box = wx.StaticBoxSizer(
                wx.VERTICAL, panel,
                "Per-die connection (overrides the default; saved to the "
                "footprint's CONNECTION field)")
            die_grid = wx.FlexGridSizer(rows=len(die_refs), cols=2,
                                        vgap=2, hgap=8)
            die_grid.AddGrowableCol(1, 1)
            board_conns = self._board_die_connections()
            for ref in die_refs:
                die_grid.Add(wx.StaticText(panel, label="%s:" % ref),
                             0, wx.ALIGN_CENTER_VERTICAL)
                ctrl = wx.Choice(panel)
                self._die_conn_ctrls[ref] = ctrl
                self._set_die_choice_items(ref, board_conns.get(ref, ""))
                die_grid.Add(ctrl, 1, wx.EXPAND)
            die_box.Add(die_grid, 0, wx.EXPAND | wx.ALL, 4)
            outer.Add(die_box, 0, wx.EXPAND | wx.ALL, 8)

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
                    board_dir = Path(board_file).parent
                    # Project-template layout: a board under a kicad/ source
                    # dir that has a sibling outputs/ dir (the adk-new-project
                    # scaffold always creates both) defaults its export to
                    # that outputs/, so the split works with no manual path
                    # edit. Requiring the sibling outputs/ to exist keeps a
                    # coincidentally-named kicad/ folder on the historic
                    # behavior (write next to the board); the name match is
                    # case-insensitive for hand-placed projects.
                    outputs_dir = board_dir.parent / "outputs"
                    if board_dir.name.lower() == "kicad" and outputs_dir.is_dir():
                        return str(outputs_dir)
                    return str(board_dir)
            except Exception:
                pass
        return str(Path.home())

    # ------------------------------------------------------------------
    # Per-die connection rows
    # ------------------------------------------------------------------

    def _die_refs(self):
        """Sorted refs of the board's die footprints (GDS_FILE field)."""
        if self._board is None:
            return []
        try:
            from .writers.chiplet_writer import list_die_refs
            return list_die_refs(self._board)
        except Exception:
            return []

    def _board_die_connections(self):
        """{ref: method} persisted in the footprints' CONNECTION fields."""
        if self._board is None:
            return {}
        try:
            from .writers.chiplet_writer import read_die_connections
            return read_die_connections(self._board)
        except Exception:
            return {}

    def _set_die_choice_items(self, ref, current):
        """(Re)populate one die's method choice, selecting `current`.

        Items are the manifest methods plus, when the board carries a value
        this manifest does not know (e.g. a different PDK root), that value
        itself -- an existing board selection is never dropped silently.
        """
        methods = [c for c in self._conn_choices if c]
        if current and current not in methods:
            methods.append(current)
        self._die_conn_items[ref] = [""] + methods
        ctrl = self._die_conn_ctrls[ref]
        ctrl.Set(["(use default)"] + methods)
        try:
            ctrl.SetSelection(self._die_conn_items[ref].index(current))
        except ValueError:
            ctrl.SetSelection(0)

    def _die_conn_value(self, ref):
        """Currently selected method for `ref` ("" = use default)."""
        ctrl = self._die_conn_ctrls[ref]
        items = self._die_conn_items[ref]
        idx = ctrl.GetSelection()
        if idx is None or not (0 <= idx < len(items)):
            return ""
        return items[idx]

    # ------------------------------------------------------------------
    # Options collection
    # ------------------------------------------------------------------

    _CONN_REFRESH_DEBOUNCE_MS = 400

    def _schedule_connection_refresh(self, _event=None):
        """Debounce the connection-choices refresh.

        A manifest re-read plus a filesystem walk on every keystroke stutters
        the GUI; restart a one-shot timer so the refresh fires once typing
        pauses (or the path is browsed).
        """
        self._conn_refresh_timer.Start(self._CONN_REFRESH_DEBOUNCE_MS,
                                       oneShot=True)

    def _on_conn_refresh_timer(self, _event):
        self._refresh_connection_choices()

    def _refresh_connection_choices(self, _event=None):
        """Re-read the connection stacks from the selected interconnect PDK.

        Preserves the current selection when the new manifest still offers
        it; otherwise resets to "" (no --connection-type).
        """
        if self._closing or not hasattr(self, "_conn_ctrl"):
            return  # dialog closing/destroyed, or still under construction
        current = ""
        idx = self._conn_ctrl.GetSelection()
        if idx is not None and 0 <= idx < len(self._conn_choices):
            current = self._conn_choices[idx]
        self._conn_choices = available_connection_types(
            self._interconnect_root_ctrl.GetPath(), board=self._board)
        self._conn_ctrl.Set(self._conn_choices)
        try:
            self._conn_ctrl.SetSelection(self._conn_choices.index(current))
        except ValueError:
            self._conn_ctrl.SetSelection(0)

        # The per-die rows offer the same manifest's methods; each keeps
        # its current selection when the new manifest still has it.
        for ref in getattr(self, "_die_conn_ctrls", {}):
            self._set_die_choice_items(ref, self._die_conn_value(ref))

    def _collect_options(self):
        idx = self._conn_ctrl.GetSelection()
        if idx is None or idx < 0:
            conn = ""
        else:
            conn = self._conn_choices[idx]
        die_conns = {ref: self._die_conn_value(ref)
                     for ref in self._die_conn_ctrls}
        die_conns = {ref: m for ref, m in die_conns.items() if m}
        return ExportOptions(
            output_dir=self._out_dir_ctrl.GetPath(),
            emit_chiplet=self._cb_chiplet.GetValue(),
            emit_interposer_gds=self._cb_interposer.GetValue(),
            emit_complete_gds=self._cb_complete.GetValue(),
            annotate_boundaries=self._cb_annotate.GetValue(),
            top_cell=self._top_cell_ctrl.GetValue() or "INTERPOSER",
            connection_type=conn,
            die_connections=die_conns,
            lyp_override=self._lyp_ctrl.GetPath() or "",
            worker_python_override=self._worker_ctrl.GetPath() or "",
            interposer_pdk_root=self._interposer_root_ctrl.GetPath() or "",
            interconnect_pdk_root=self._interconnect_root_ctrl.GetPath() or "",
            adk_root=self._adk_root_ctrl.GetPath() or "",
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

        # Persist the per-die selections to the footprints' CONNECTION
        # fields (including cleared overrides) so the board and this export
        # agree; the user saves the board to keep them.
        if self._die_conn_ctrls and self._board is not None:
            try:
                from .writers.chiplet_writer import write_die_connections
                changed = write_die_connections(
                    self._board,
                    {ref: self._die_conn_value(ref)
                     for ref in self._die_conn_ctrls})
                if changed:
                    self._append_log_safe(
                        "Updated CONNECTION field on: %s (save the board "
                        "to keep it)" % ", ".join(changed))
            except Exception as exc:
                self._append_log_safe(
                    "Warning: could not write CONNECTION fields: %s" % exc)

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
        # Mark closing first so any in-flight wx.CallAfter from the worker
        # (streamed log lines, _on_done) becomes a no-op instead of touching a
        # window EndModal/Destroy is about to free.
        self._closing = True
        self._conn_refresh_timer.Stop()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._cancel_event.set()
        self.EndModal(wx.ID_CLOSE)

    def _on_done(self, result):
        if self._closing:
            return  # dialog is closing; the window may already be gone
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
                                ("Hyperlynx (.hyp)", result.hyp_path)):
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
        if self._closing:
            return  # dialog is closing; the log control may already be gone
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
