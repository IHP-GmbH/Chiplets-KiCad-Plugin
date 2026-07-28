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
    connection_method_specs, describe_assembly_drc, describe_connection_method,
    discover_dependency_root, discover_interposer_lyp, format_connection_label,
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


def _muted(parent, text="", shrinkable=False):
    """Grey explanatory label; ``shrinkable`` keeps it out of the width budget.

    A sizer takes a StaticText's full text width as its minimum and
    ``outer.SetSizeHints`` turns the widest child into the dialog's enforced
    minimum width -- and ``wx.ST_ELLIPSIZE_END`` alone does not change that
    best size on wxGTK, it only decides how the text is clipped once the
    control is already too narrow. So a long sentence needs both the style
    and an explicit minimum of its own, or it silently widens the dialog by
    a few hundred pixels. Column headers pass ``shrinkable=False``: they are
    short, and their column is not growable, so their width is exactly what
    should reserve space.
    """
    label = wx.StaticText(parent, label=text, style=wx.ST_ELLIPSIZE_END)
    label.SetForegroundColour(
        wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
    if shrinkable:
        label.SetMinSize(wx.Size(1, -1))
    return label


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

        # Outputs. The .chiplet and the interposer GDS are stated, not
        # offered: the .chiplet is the export's product and the GDS is the
        # layout it references, and hyp_to_gds writes both on every run
        # anyway -- a toggle could only have discarded them. The two real
        # options are the extra artifacts.
        outs_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Outputs")
        always = _muted(
            panel,
            "Always written: <board>.chiplet (open this in Chiplet Studio) "
            "and layout/<board>_interposer.gds",
            shrinkable=True)
        always.SetToolTip(
            "The .chiplet is the assembly description Chiplet Studio loads; "
            "its layout: field points at the interposer GDS, so the two "
            "travel together. Both are produced on every run.")
        outs_box.Add(always, 0, wx.ALL, 2)
        self._cb_complete = wx.CheckBox(
            panel, label="Complete assembly GDS (with chiplet instances)")
        self._cb_complete.SetToolTip(
            "Interposer plus every placed die flattened into one GDS. "
            "Required for the ADK assembly DRC, which runs right after the "
            "export when this is on.")
        self._cb_annotate = wx.CheckBox(
            panel, label="Annotate chiplet boundaries (viewer-only layer)")
        self._cb_annotate.SetToolTip(
            "Paint each chiplet's mechanical boundary and instance label onto "
            "an annotation GDS layer (1000/0) for eyeball inspection in "
            "KLayout. No DRC rule reads it; the assembly contract stays in the "
            "boundary manifest. Off by default.")
        for cb in (self._cb_complete, self._cb_annotate):
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
        grid = wx.FlexGridSizer(rows=4, cols=2, vgap=4, hgap=8)
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
        #
        # _conn_choices holds bare method ids and _conn_labels the strings
        # shown; the two are index-parallel, so every read-back path keeps
        # returning the id the CLI and the CONNECTION field expect. Same
        # shape the per-die rows already use with _die_conn_items.
        self._conn_choices = []
        self._conn_labels = []
        self._conn_specs = {}
        self._load_connection_catalogue()
        self._conn_ctrl = wx.Choice(panel, choices=self._conn_labels)
        self._conn_ctrl.SetSelection(0)
        self._conn_ctrl.Bind(wx.EVT_CHOICE, self._on_conn_selected)
        grid.Add(self._conn_ctrl, 1, wx.EXPAND)

        # The numbers behind the selected method: pitch and spacing are what
        # the assembly DRC will check this design against, so they belong on
        # screen and not only in the PDK manifest. One line for the whole
        # dialog -- one per die row would crowd out the log on a 2+ die board.
        grid.Add(wx.StaticText(panel, label=""), 0)
        self._conn_detail = _muted(panel, shrinkable=True)
        grid.Add(self._conn_detail, 1, wx.EXPAND)

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

        # Per-die connection method and physical thickness: one row per die
        # footprint (GDS_FILE field). Initialized from each footprint's
        # CONNECTION / DIE_THICKNESS_UM fields and written back on Run, so
        # the board stays the source of truth for per-die selection. A die
        # on "(use default)" follows the assembly-wide stack above; mixed
        # selections give each die its own method's 3D bodies, connection
        # stack and DRC numbers. An empty thickness keeps the .chiplet
        # format default of 0.0.
        self._die_conn_ctrls = {}
        self._die_conn_items = {}
        self._die_thick_ctrls = {}
        die_refs = self._die_refs()
        if die_refs:
            die_box = wx.StaticBoxSizer(
                wx.VERTICAL, panel,
                "Per-die settings (saved to the footprint's CONNECTION / "
                "DIE_THICKNESS_UM fields)")
            die_grid = wx.FlexGridSizer(rows=len(die_refs) + 1, cols=4,
                                        vgap=2, hgap=8)
            die_grid.AddGrowableCol(1, 1)
            # Column headers: two unrelated quantities share each row, and
            # without them "thickness" next to a connection dropdown reads as
            # the thickness OF that connection.
            for header in ("Die", "Interconnect method", "",
                           "Die thickness (um)"):
                die_grid.Add(_muted(panel, header), 0,
                             wx.ALIGN_CENTER_VERTICAL)
            board_conns = self._board_die_connections()
            board_thicks = self._board_die_thicknesses()
            for ref in die_refs:
                die_grid.Add(wx.StaticText(panel, label="%s:" % ref),
                             0, wx.ALIGN_CENTER_VERTICAL)
                ctrl = wx.Choice(panel)
                ctrl.Bind(wx.EVT_CHOICE,
                          lambda _e, r=ref: self._refresh_die_tooltip(r))
                self._die_conn_ctrls[ref] = ctrl
                self._set_die_choice_items(ref, board_conns.get(ref, ""))
                die_grid.Add(ctrl, 1, wx.EXPAND)
                die_grid.Add(wx.StaticText(panel, label="die Si thickness:"),
                             0, wx.ALIGN_CENTER_VERTICAL)
                thick = wx.TextCtrl(panel, size=wx.Size(90, -1))
                thick.SetValue(board_thicks.get(ref, ""))
                # A hint, not a value: GetValue() stays empty, so an
                # untouched field is never stamped onto the footprint of a
                # board that never declared a thickness.
                thick.SetHint("750")
                thick.SetToolTip(
                    "Physical thickness of the silicon die body in "
                    "micrometers, written to dimensions.thickness. 750 is a "
                    "standard SG13G2 die. Empty exports 0.0, which the ADK "
                    "3Dblox export rejects and which Chiplet Studio renders "
                    "as a 200 um body. Interconnect stack heights are a "
                    "separate axis -- do not add them here.")
                self._die_thick_ctrls[ref] = thick
                die_grid.Add(thick, 0)
            die_box.Add(die_grid, 0, wx.EXPAND | wx.ALL, 4)
            die_box.Add(
                _muted(panel,
                       "Interconnect stack heights (24-80 um) come from the "
                       "interconnect PDK manifest and are not editable here; "
                       "the thickness column is the silicon die body.",
                       shrinkable=True),
                0, wx.EXPAND | wx.ALL, 4)
            outer.Add(die_box, 0, wx.EXPAND | wx.ALL, 8)

        # Worker Python: one line, pre-filled with what discovery resolved, so
        # it reports provenance instead of sitting there as an empty box. Left
        # blank the export follows the discovery chain; typing a path here is
        # the only override that takes effect without restarting KiCad (the
        # env var and the project text variable both need one).
        worker_row = wx.BoxSizer(wx.HORIZONTAL)
        worker_row.Add(wx.StaticText(panel, label="Worker Python:"),
                       0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._worker_ctrl = wx.TextCtrl(panel)
        self._worker_ctrl.SetHint(self._worker_python_hint())
        worker_tip = (
            "Interpreter that runs hyp_to_gds and the ADK DRC; it needs the "
            "klayout and PyYAML modules, which KiCad's bundled Python lacks. "
            "Leave empty to follow the discovery chain "
            "($KICAD_CHIPLET_PYTHON, the plugin's .venv, the project text "
            "variable, then a probed python3 on PATH). Set it when this "
            "checkout has no .venv or the auto-detected one is incomplete.")
        self._worker_ctrl.SetToolTip(worker_tip)
        worker_browse = wx.Button(panel, label="Browse...",
                                  style=wx.BU_EXACTFIT)
        worker_browse.Bind(wx.EVT_BUTTON, self._on_browse_worker)
        worker_row.Add(self._worker_ctrl, 1, wx.EXPAND)
        worker_row.Add(worker_browse, 0, wx.LEFT, 4)
        outer.Add(worker_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)

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

        # EVT_CHOICE does not fire for the SetSelection calls above, so the
        # detail line and tooltips need one explicit pass at build time.
        self._on_conn_selected()

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

    def _worker_python_hint(self):
        """Placeholder text for the worker-Python field.

        Shows the interpreter discovery already resolved so the empty field
        reads as provenance rather than as a blank required box. Deliberately
        uses the probe-free preview: the full chain's PATH leg spawns an
        import probe with a multi-second timeout, and this runs on the UI
        thread while the dialog is being built.
        """
        try:
            from .pipeline.discovery import preview_worker_python
            path, source = preview_worker_python(self._plugin_dir,
                                                 board=self._board)
        except Exception:
            path, source = "", ""
        if path:
            return "%s  (auto: %s)" % (path, source)
        return "(auto-detected at Run)"

    def _on_browse_worker(self, _event):
        dlg = wx.FileDialog(
            self, "Select the worker Python interpreter",
            defaultDir=os.path.dirname(self._worker_ctrl.GetValue()) or "/usr/bin",
            wildcard="Python interpreter|*|All files|*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if dlg.ShowModal() == wx.ID_OK:
                self._worker_ctrl.SetValue(dlg.GetPath())
        finally:
            dlg.Destroy()

    # ------------------------------------------------------------------
    # Connection-method catalogue (ids, display labels, spec sheets)
    # ------------------------------------------------------------------

    def _load_connection_catalogue(self):
        """(Re)read method ids and their specs from the interconnect PDK.

        Fills the index-parallel ``_conn_choices`` (ids) / ``_conn_labels``
        (display) pair plus ``_conn_specs``. Every read-back path indexes the
        id list, so what reaches the CLI and the CONNECTION fields is always
        the bare method id -- the labels are presentation only.
        """
        root = self._interconnect_root_ctrl.GetPath()
        self._conn_choices = available_connection_types(root, board=self._board)
        self._conn_specs = connection_method_specs(root, board=self._board)
        self._conn_labels = [
            "(none - keep each die's CONNECTION field)" if not method
            else self._conn_label_for(method)
            for method in self._conn_choices
        ]

    def _conn_label_for(self, method_id):
        """Display label for a method id (bare id when the manifest is mute)."""
        return format_connection_label(method_id,
                                       self._conn_specs.get(method_id))

    def _conn_detail_for(self, method_id):
        """Spec-sheet line for a method id, for the detail text and tooltips."""
        if not method_id:
            return ("No assembly-wide default: each die keeps the method in "
                    "its own CONNECTION field.")
        return describe_connection_method(method_id,
                                          self._conn_specs.get(method_id))

    def _on_conn_selected(self, _event=None):
        """Refresh the detail line + tooltip under the assembly-wide choice.

        Also called explicitly after every programmatic ``SetSelection``:
        wx does not raise EVT_CHOICE for those.
        """
        idx = self._conn_ctrl.GetSelection()
        method = (self._conn_choices[idx]
                  if idx is not None and 0 <= idx < len(self._conn_choices)
                  else "")
        detail = self._conn_detail_for(method)
        self._conn_detail.SetLabel(detail)
        self._conn_ctrl.SetToolTip(detail)

    def _refresh_die_tooltip(self, ref):
        """Put the selected method's spec sheet on one die row's tooltip."""
        ctrl = self._die_conn_ctrls.get(ref)
        if ctrl is None:
            return
        method = self._die_conn_value(ref)
        ctrl.SetToolTip(
            self._conn_detail_for(method) if method
            else "Follows the assembly-wide connection stack selected above.")

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

    def _board_die_thicknesses(self):
        """{ref: display text} persisted in DIE_THICKNESS_UM fields.

        Values come back as floats from the reader; render them without
        trailing zeros so the field shows what the user typed ("750", not
        "750.000000").
        """
        if self._board is None:
            return {}
        try:
            from .writers.chiplet_writer import read_die_thicknesses
            return {ref: ("%.6f" % v).rstrip("0").rstrip(".")
                    for ref, v in read_die_thicknesses(self._board).items()}
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
        ctrl.Set(["(use default)"]
                 + [self._conn_label_for(m) for m in methods])
        try:
            ctrl.SetSelection(self._die_conn_items[ref].index(current))
        except ValueError:
            ctrl.SetSelection(0)
        self._refresh_die_tooltip(ref)

    def _die_conn_value(self, ref):
        """Currently selected method for `ref` ("" = use default)."""
        ctrl = self._die_conn_ctrls[ref]
        items = self._die_conn_items[ref]
        idx = ctrl.GetSelection()
        if idx is None or not (0 <= idx < len(items)):
            return ""
        return items[idx]

    def _die_thickness_text(self, ref):
        """Raw thickness field text for `ref` ("" = keep format default)."""
        return self._die_thick_ctrls[ref].GetValue().strip()

    def _die_thickness_errors(self):
        """Refs whose thickness text is non-empty but not a positive number."""
        from .writers.chiplet_writer import parse_thickness_um
        return [ref for ref in sorted(self._die_thick_ctrls)
                if self._die_thickness_text(ref)
                and parse_thickness_um(self._die_thickness_text(ref)) is None]

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

        A selection the new manifest does not declare is kept as an extra
        item rather than dropped: the per-die rows have always behaved that
        way, and silently resetting an assembly-wide choice because the user
        was mid-way through typing a PDK path is worse than showing a method
        the current checkout cannot describe.
        """
        if self._closing or not hasattr(self, "_conn_ctrl"):
            return  # dialog closing/destroyed, or still under construction
        current = ""
        idx = self._conn_ctrl.GetSelection()
        if idx is not None and 0 <= idx < len(self._conn_choices):
            current = self._conn_choices[idx]
        self._load_connection_catalogue()
        if current and current not in self._conn_choices:
            self._conn_choices.append(current)
            self._conn_labels.append(self._conn_label_for(current))
        self._conn_ctrl.Set(self._conn_labels)
        try:
            self._conn_ctrl.SetSelection(self._conn_choices.index(current))
        except ValueError:
            self._conn_ctrl.SetSelection(0)
        self._on_conn_selected()

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
        from .writers.chiplet_writer import parse_thickness_um
        die_thicks = {ref: parse_thickness_um(self._die_thickness_text(ref))
                      for ref in self._die_thick_ctrls}
        die_thicks = {ref: t for ref, t in die_thicks.items()
                      if t is not None}
        return ExportOptions(
            output_dir=self._out_dir_ctrl.GetPath(),
            emit_complete_gds=self._cb_complete.GetValue(),
            annotate_boundaries=self._cb_annotate.GetValue(),
            top_cell=self._top_cell_ctrl.GetValue() or "INTERPOSER",
            connection_type=conn,
            die_connections=die_conns,
            die_thicknesses=die_thicks,
            lyp_override=self._lyp_ctrl.GetPath() or "",
            worker_python_override=self._worker_ctrl.GetValue().strip(),
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
        # An override that is not runnable must fail here, with a message,
        # rather than deep in the subprocess launch as a raw OSError.
        if options.worker_python_override:
            from .pipeline.discovery import _is_executable
            if not _is_executable(Path(options.worker_python_override)):
                wx.MessageBox(
                    "Worker Python is not an executable file:\n%s\n\nClear "
                    "the field to use the auto-detected interpreter."
                    % options.worker_python_override,
                    "Chiplet Export",
                    wx.OK | wx.ICON_WARNING,
                )
                return
        bad_thicks = self._die_thickness_errors()
        if bad_thicks:
            wx.MessageBox(
                "Invalid die thickness for %s: expected a positive number "
                "of micrometers (750 for a standard SG13G2 die), or empty "
                "to export 0.0."
                % ", ".join(bad_thicks),
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

        # Same for the per-die thickness fields (validated above; empty
        # clears the field so the die keeps the format default).
        if self._die_thick_ctrls and self._board is not None:
            try:
                from .writers.chiplet_writer import write_die_thicknesses
                changed = write_die_thicknesses(
                    self._board,
                    {ref: self._die_thickness_text(ref)
                     for ref in self._die_thick_ctrls})
                if changed:
                    self._append_log_safe(
                        "Updated DIE_THICKNESS_UM field on: %s (save the "
                        "board to keep it)" % ", ".join(changed))
            except Exception as exc:
                self._append_log_safe(
                    "Warning: could not write DIE_THICKNESS_UM fields: %s"
                    % exc)

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
