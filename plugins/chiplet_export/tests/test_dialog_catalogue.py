# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise real dialog methods with control doubles, without wx/display.

These are handling/lifecycle tests, not evidence that native widgets construct.
The wx shim is confined to loading a private test module and is then restored.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_export.pad_domains import PROPERTY
from chiplet_export.pipeline import orchestrator
from chiplet_export.writers.connection_stacks import InterconnectManifestSnapshot

from .test_die_outline_field import (
    FIELD, PCBNEW, _Board, _Footprint, _die, _marked,
)


class Control:
    def __init__(self, value="", choices=()):
        self.value = value
        self.choices = list(choices)
        self.selection = 0 if choices else -1
        self.enabled = True
        self.tooltip = ""

    def GetPath(self):
        return self.value

    def GetSelection(self):
        return self.selection

    def SetSelection(self, value):
        assert 0 <= value < len(self.choices)
        self.selection = value

    def Set(self, choices):
        self.choices = list(choices)

    def Enable(self, enabled):
        self.enabled = enabled

    def SetLabel(self, value):
        self.value = value

    def SetToolTip(self, value):
        self.tooltip = value

    def AppendText(self, value):
        self.value += value

    def SetValue(self, value):
        self.value = value

    def GetValue(self):
        return self.value


@pytest.fixture
def dialog_module(monkeypatch):
    # Explicit widget surface for the real constructor. Missing application
    # attributes and unsupported wx calls remain AttributeErrors, never mocks.
    class Window:
        def __init__(self, parent=None, **kwargs):
            self.parent = parent
            self.enabled = True
            self.tooltip = ""
            self.bindings = []

        def Bind(self, event, handler, source=None):
            self.bindings.append((event, handler, source))

        def Enable(self, enabled=True):
            self.enabled = bool(enabled)

        def Disable(self):
            self.Enable(False)

        def IsEnabled(self):
            return self.enabled

        def SetToolTip(self, value):
            self.tooltip = value

        def SetSizer(self, sizer):
            self.sizer = sizer

        def CentreOnParent(self):
            pass

    class Text(Window):
        def __init__(self, parent=None, value="", label="", **kwargs):
            super().__init__(parent, **kwargs)
            self.value = value or label
            self.editable = not bool(kwargs.get("style", 0) & wx.TE_READONLY)

        def GetValue(self):
            return self.value

        def SetValue(self, value):
            self.value = value

        def GetLabel(self):
            return self.value

        def SetLabel(self, value):
            self.value = value

        def AppendText(self, value):
            self.value += value

        def IsEditable(self):
            return self.editable

        def SetHint(self, value):
            self.hint = value

        def SetFont(self, font):
            pass

        def SetForegroundColour(self, colour):
            pass

        def SetMinSize(self, size):
            pass

    class Choice(Window):
        def __init__(self, parent=None, choices=(), **kwargs):
            super().__init__(parent, **kwargs)
            self.choices = list(choices)
            self.selection = -1

        def SetSelection(self, value):
            assert 0 <= value < len(self.choices)
            self.selection = value

        def GetSelection(self):
            return self.selection

        def Set(self, choices):
            self.choices = list(choices)
            self.selection = -1

        def GetCount(self):
            return len(self.choices)

        def GetString(self, index):
            assert 0 <= index < len(self.choices)
            return self.choices[index]

    class CheckBox(Text):
        def __init__(self, parent=None, **kwargs):
            super().__init__(parent, **kwargs)
            self.value = False

    class FilePicker(Text):
        def SetPath(self, path):
            self.value = path

        def GetPath(self):
            return self.value

    class Timer:
        def __init__(self, owner):
            self.owner = owner
            self.started = None

        def Start(self, milliseconds, oneShot=False):
            self.started = (milliseconds, oneShot)

        def Stop(self):
            self.started = None

    class Sizer:
        # Decorative layout calls do not decide source/refusal state.
        def __init__(self, *args, **kwargs):
            pass

        def Add(self, *args):
            pass

        def AddGrowableCol(self, *args):
            pass

        def AddStretchSpacer(self, *args):
            pass

        def SetSizeHints(self, window):
            pass

    class FontInfo:
        def __init__(self, size):
            self.size = size

        def Family(self, family):
            return self

    wx = types.SimpleNamespace(
        Dialog=Window, Panel=Window, Timer=Timer, TextCtrl=Text,
        StaticText=Text, Button=Text, CheckBox=CheckBox, Choice=Choice,
        FilePickerCtrl=FilePicker, BoxSizer=Sizer, StaticBoxSizer=Sizer,
        FlexGridSizer=Sizer, FontInfo=FontInfo, Font=lambda info: info,
        Size=lambda width, height: (width, height),
        SystemSettings=types.SimpleNamespace(GetColour=lambda role: role))
    for index, name in enumerate((
            "DEFAULT_DIALOG_STYLE", "RESIZE_BORDER", "VERTICAL", "HORIZONTAL",
            "EXPAND", "ALL", "RIGHT", "LEFT", "BOTTOM", "TOP",
            "ALIGN_CENTER_VERTICAL", "BU_EXACTFIT", "ST_ELLIPSIZE_END",
            "SYS_COLOUR_GRAYTEXT", "TE_MULTILINE", "TE_READONLY", "TE_DONTWRAP",
            "FONTFAMILY_TELETYPE")):
        setattr(wx, name, 1 << index)
    for name in ("EVT_TIMER", "EVT_BUTTON", "EVT_TEXT", "EVT_CHECKBOX",
                 "EVT_CHOICE", "EVT_CLOSE"):
        setattr(wx, name, object())
    path = Path(__file__).resolve().parents[1] / "dialog_chiplet_export.py"
    spec = importlib.util.spec_from_file_location(
        "chiplet_export._dialog_catalogue_test", path)
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as scope:
        scope.setitem(sys.modules, "wx", wx)
        spec.loader.exec_module(module)
    return module


def populate_controls(dialog, root="/selected/pdk"):
    dialog._board = None
    dialog._closing = False
    dialog._worker_thread = None
    dialog._catalogue_error = ""
    dialog._catalogue_reported_error = ""
    dialog._catalogue_warning = ""
    dialog._catalogue_reported_warning = ""
    dialog._conn_availability = {}
    # Already asked for this worker and these roots: no query unless a test
    # changes one of them.
    dialog._availability_key = ("/worker/python", "/interposer/pdk", root)
    dialog._availability_report = ""
    dialog._availability_reported = ""
    dialog._shown_state = ("", False)
    dialog._plugin_dir = "/plugin"
    dialog._interconnect_root_ctrl = Control(root)
    dialog._interposer_root_ctrl = Control("/interposer/pdk")
    dialog._worker_ctrl = Control("/worker/python")
    dialog._conn_choices = ["", "original"]
    dialog._conn_specs = {"original": {}}
    dialog._conn_labels = ["none", "original"]
    dialog._conn_ctrl = Control(choices=dialog._conn_labels)
    dialog._conn_ctrl.SetSelection(1)
    dialog._conn_detail = Control()
    dialog._die_conn_ctrls = {"U1": Control(choices=["default", "original"])}
    dialog._die_conn_ctrls["U1"].SetSelection(1)
    dialog._die_conn_items = {"U1": ["", "original"]}
    dialog._die_thick_ctrls = {}
    dialog._die_outline_ctrls = {}
    dialog._run_btn = Control()
    dialog._cancel_btn = Control()
    dialog._status = Control("Idle")
    dialog._log_ctrl = Control()


@pytest.fixture
def dialog(dialog_module, monkeypatch):
    instance = object.__new__(dialog_module.ChipletExportDialog)
    populate_controls(instance)
    # Isolate the two catalogue-read failure sites unless a test opts into the
    # actual shared preflight below.
    monkeypatch.setattr(dialog_module, "_preflight_interconnect_manifest",
                        lambda *args, **kwargs: InterconnectManifestSnapshot(
                            {"methods": {"original": {}}}, root="/selected/pdk"))
    monkeypatch.setattr(dialog_module, "available_connection_types",
                        lambda *args, **kwargs: ["", "original"])
    monkeypatch.setattr(dialog_module, "connection_method_specs",
                        lambda *args, **kwargs: {"original": {}})
    # The availability query runs only where a test runs it (_answer_inline).
    monkeypatch.setattr(dialog_module, "_in_background",
                        lambda target, *args: None)
    return instance



@pytest.mark.parametrize("failure_site", ["available_connection_types",
                                         "connection_method_specs"])
def test_refused_refresh_preserves_atomic_catalogue_and_blocks_run(
        dialog, dialog_module, monkeypatch, failure_site):
    before = (dialog._conn_choices, dialog._conn_specs, dialog._conn_labels)
    calls = []

    def refused(root, board=None):
        calls.append(root)
        raise orchestrator.InterconnectSourceRefused("reader unavailable")

    monkeypatch.setattr(dialog_module, "available_connection_types",
                        lambda *args, **kwargs: ["", "replacement"])
    monkeypatch.setattr(dialog_module, failure_site, refused)
    assert dialog._refresh_connection_choices() is False
    assert all(a is b for a, b in zip(before, (
        dialog._conn_choices, dialog._conn_specs, dialog._conn_labels)))
    assert calls == ["/selected/pdk"]
    assert not dialog._run_btn.enabled
    assert not dialog._conn_ctrl.enabled
    assert not dialog._die_conn_ctrls["U1"].enabled
    assert dialog._interconnect_root_ctrl.enabled
    assert dialog._interconnect_root_ctrl.value == "/selected/pdk"
    assert "/selected/pdk" in dialog._log_ctrl.value
    assert "KiCad's Python" in dialog._log_ctrl.value
    dialog._set_running(False)
    assert not dialog._run_btn.enabled


def test_valid_invalid_valid_recovers_selections_without_enabling_busy_run(
        dialog, dialog_module, monkeypatch):
    assert dialog._refresh_connection_choices() is True

    def specs(root, board=None):
        if root == "/broken/pdk":
            raise orchestrator.InterconnectSourceRefused("invalid manifest")
        return {"original": {}}

    monkeypatch.setattr(dialog_module, "connection_method_specs", specs)
    dialog._interconnect_root_ctrl.value = "/broken/pdk"
    dialog._on_conn_refresh_timer(None)
    assert not dialog._run_btn.enabled
    log = dialog._log_ctrl.value
    dialog._on_conn_refresh_timer(None)
    assert dialog._log_ctrl.value == log  # same refusal is not repeated
    dialog._interconnect_root_ctrl.value = "/repaired/pdk"
    dialog._worker_thread = types.SimpleNamespace(is_alive=lambda: True)
    assert dialog._refresh_connection_choices() is True
    assert not dialog._run_btn.enabled
    assert dialog._conn_ctrl.enabled
    assert dialog._die_conn_ctrls["U1"].enabled
    assert dialog._conn_choices[dialog._conn_ctrl.GetSelection()] == "original"
    assert dialog._die_conn_value("U1") == "original"
    assert not dialog._catalogue_error
    dialog._worker_thread = None
    dialog._set_running(False)
    assert dialog._run_btn.enabled


@pytest.fixture
def constructor_source(dialog_module, monkeypatch, tmp_path):
    root = tmp_path / "selected-pdk"
    root.mkdir()
    monkeypatch.setenv("INTERCONNECT_PDK_ROOT", str(root))
    # Only unrelated discovery/preview is replaced. No application attributes,
    # layout methods, source resolution or catalogue state are fabricated.
    monkeypatch.setattr(dialog_module, "discover_dependency_root",
                        lambda *args, **kwargs: "")
    monkeypatch.setattr(dialog_module, "discover_interposer_lyp",
                        lambda *args, **kwargs: "")
    monkeypatch.setattr(dialog_module.ChipletExportDialog, "_worker_python_hint",
                        lambda self: "controlled preview; no interpreter probe")
    monkeypatch.setattr(dialog_module, "_preflight_interconnect_manifest",
                        orchestrator._preflight_interconnect_manifest)
    monkeypatch.setattr(dialog_module, "_in_background",
                        lambda target, *args: None)
    return root


def install_constructor_reader_double(root, monkeypatch):
    """Provide JSON source data; this is not a supported-reader/version test."""
    manifest = root / "manifest" / "interconnect_methods.json"
    manifest.parent.mkdir(exist_ok=True)
    manifest.write_text(json.dumps({
        "schema_version": "1.0",
        "methods": {"recovered": {"pitch_rules": {"IXN_pitch": 123.0}}},
    }), encoding="utf-8")
    reads = []

    def load_manifest(path):
        reads.append(Path(path))
        return json.loads(Path(path).read_text(encoding="utf-8"))

    monkeypatch.setattr(orchestrator, "_interconnect_reader",
                        lambda: types.SimpleNamespace(load_manifest=load_manifest))
    return reads, manifest


def assert_constructor_source_editable(instance, root):
    assert instance._interconnect_root_ctrl.GetPath() == str(root)
    assert instance._interconnect_root_ctrl.IsEnabled()
    assert instance._interconnect_root_ctrl._text.IsEnabled()
    assert instance._interconnect_root_ctrl._text.IsEditable()
    assert instance._interconnect_root_ctrl._browse.IsEnabled()
    assert instance._close_btn.IsEnabled()
    assert instance._worker_thread is None


@pytest.mark.parametrize("source", ["missing_reader", "missing_manifest"])
def test_real_constructor_with_control_doubles_refuses_and_recovers(
        dialog_module, constructor_source, monkeypatch, source):
    root = constructor_source
    if source == "missing_reader":
        manifest = root / "manifest" / "interconnect_methods.json"
        manifest.parent.mkdir()
        manifest.write_text('{"schema_version":"1.0","methods":{"original":{}}}',
                            encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_interconnect_reader", lambda: None)
    # This executes the product __init__, _build_ui and real _DirField. In
    # particular, removing the final state application leaves Run enabled;
    # moving it before control creation raises AttributeError in this test.
    instance = dialog_module.ChipletExportDialog(None, board=None)
    assert_constructor_source_editable(instance, root)
    assert not instance._run_btn.IsEnabled()
    assert not instance._conn_ctrl.IsEnabled()
    assert instance._conn_ctrl.GetCount() == 0
    assert instance._conn_choices == [] and instance._conn_specs == {}
    assert str(root) in instance._log_ctrl.GetValue()
    assert ("reader" if source == "missing_reader" else "does not contain") in (
        instance._log_ctrl.GetValue())

    reads, manifest = install_constructor_reader_double(root, monkeypatch)
    instance._on_conn_refresh_timer(None)
    assert reads and all(path == manifest for path in reads)
    assert_constructor_source_editable(instance, root)
    assert not instance._catalogue_error
    assert instance._run_btn.IsEnabled() and instance._conn_ctrl.IsEnabled()
    assert instance._conn_choices == ["", "recovered"]
    instance._conn_ctrl.SetSelection(1)
    instance._on_conn_selected()
    assert instance._conn_choices[instance._conn_ctrl.GetSelection()] == "recovered"
    assert instance._conn_specs["recovered"]["pitch"] == 123.0


def test_real_constructor_with_control_doubles_starts_with_valid_catalogue(
        dialog_module, constructor_source, monkeypatch):
    reads, manifest = install_constructor_reader_double(constructor_source, monkeypatch)
    instance = dialog_module.ChipletExportDialog(None, board=None)
    assert_constructor_source_editable(instance, constructor_source)
    assert reads and all(path == manifest for path in reads)
    # A changed interposer root or worker re-reads, and so asks the worker again.
    assert (instance._interposer_root_ctrl._on_change
            == instance._schedule_connection_refresh)
    assert instance._schedule_connection_refresh in [
        handler for _event, handler, _source in instance._worker_ctrl.bindings]
    assert instance._catalogue_error == ""
    assert instance._log_ctrl.GetValue() == ""
    assert instance._run_btn.IsEnabled() and instance._conn_ctrl.IsEnabled()
    assert instance._conn_choices == ["", "recovered"]
    assert instance._conn_ctrl.GetCount() == 2
    instance._conn_ctrl.SetSelection(1)
    instance._on_conn_selected()
    assert instance._conn_choices[instance._conn_ctrl.GetSelection()] == "recovered"


@pytest.mark.parametrize("source", ["missing_manifest", "missing_reader"])
def test_run_preflights_actual_selected_source_before_options_or_side_effects(
        dialog, dialog_module, monkeypatch, tmp_path, source):
    root = tmp_path / "selected"
    root.mkdir()
    if source == "missing_reader":
        (root / "manifest").mkdir()
        (root / "manifest" / "interconnect_methods.json").write_text("{}")
    monkeypatch.setattr(orchestrator, "_interconnect_reader", lambda: None)
    monkeypatch.setattr(dialog_module, "_preflight_interconnect_manifest",
                        orchestrator._preflight_interconnect_manifest)
    dialog._interconnect_root_ctrl.value = str(root)

    def unexpected(*args, **kwargs):
        pytest.fail("refused source reached options, catalogue fallback or export")

    monkeypatch.setattr(orchestrator, "discover_dependency_root", unexpected)
    monkeypatch.setattr(dialog, "_collect_options", unexpected)
    monkeypatch.setattr(dialog_module, "available_connection_types", unexpected)
    monkeypatch.setattr(dialog_module, "connection_method_specs", unexpected)
    monkeypatch.setattr(dialog_module, "run_export", unexpected)
    # No timer invocation: the Run handler must validate the freshly edited root.
    dialog._on_run(None)
    assert not dialog._run_btn.enabled
    assert dialog._worker_thread is None
    assert str(root) in dialog._catalogue_error


@pytest.mark.parametrize("previous_refusal", [False, True])
def test_genuine_no_pdk_absence_remains_usable_with_warning(
        dialog, dialog_module, monkeypatch, previous_refusal):
    if previous_refusal:
        dialog._catalogue_error = "Previously selected source could not be read"
        dialog._apply_catalogue_state()
        assert not dialog._run_btn.enabled
    dialog._interconnect_root_ctrl.value = ""
    monkeypatch.setattr(orchestrator, "discover_interconnect_root",
                        lambda *args, **kwargs: "")
    monkeypatch.setattr(dialog_module, "_preflight_interconnect_manifest",
                        orchestrator._preflight_interconnect_manifest)
    monkeypatch.setattr(dialog_module, "available_connection_types",
                        orchestrator.available_connection_types)
    monkeypatch.setattr(dialog_module, "connection_method_specs",
                        orchestrator.connection_method_specs)
    assert dialog._refresh_connection_choices() is True
    assert dialog._run_btn.enabled
    assert not dialog._catalogue_error
    assert dialog._catalogue_warning == orchestrator.INTERCONNECT_ABSENCE_WARNING
    assert orchestrator.INTERCONNECT_ABSENCE_WARNING in dialog._log_ctrl.value
    assert orchestrator.INTERCONNECT_ABSENCE_WARNING in dialog._conn_detail.value
    assert "without interconnect PDK" in dialog._status.value
    assert "catalogue loaded from the resolved source" not in dialog._log_ctrl.value
    log = dialog._log_ctrl.value
    assert dialog._refresh_connection_choices() is True
    assert dialog._log_ctrl.value == log


def test_absence_warning_clears_when_a_manifest_becomes_available(
        dialog, dialog_module, monkeypatch):
    monkeypatch.setattr(dialog_module, "_preflight_interconnect_manifest",
                        lambda *args, **kwargs: InterconnectManifestSnapshot(None))
    assert dialog._refresh_connection_choices() is True
    assert dialog._catalogue_warning
    monkeypatch.setattr(dialog_module, "_preflight_interconnect_manifest",
                        lambda *args, **kwargs: InterconnectManifestSnapshot(
                            {"methods": {"original": {}}}, root="/selected/pdk"))
    assert dialog._refresh_connection_choices() is True
    assert not dialog._catalogue_warning
    assert not dialog._catalogue_reported_warning
    assert dialog._status.value == "Idle"
    assert "catalogue loaded from the resolved source" in dialog._log_ctrl.value
    assert orchestrator.INTERCONNECT_ABSENCE_WARNING not in dialog._conn_detail.value


def test_no_pdk_warning_survives_run_log_reset(dialog, dialog_module, monkeypatch):
    monkeypatch.setattr(dialog_module, "_preflight_interconnect_manifest",
                        lambda *args, **kwargs: InterconnectManifestSnapshot(None))
    monkeypatch.setattr(dialog, "_collect_options", lambda: types.SimpleNamespace(
        output_dir="/output", worker_python_override=""))
    monkeypatch.setattr(dialog, "_die_thickness_errors", lambda: [])
    started = []
    monkeypatch.setattr(dialog_module.threading, "Thread", lambda **kwargs:
                        types.SimpleNamespace(start=lambda: started.append(True)))
    dialog._on_run(None)
    assert started == [True]
    assert dialog._log_ctrl.value == "WARNING: " + orchestrator.INTERCONNECT_ABSENCE_WARNING + "\n"


def test_unexpected_error_is_not_relabelled_as_source_refusal(
        dialog, dialog_module, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("programming defect")

    monkeypatch.setattr(dialog_module, "connection_method_specs", broken)
    with pytest.raises(RuntimeError, match="programming defect"):
        dialog._refresh_connection_choices()


# --- the per-die outline layer column ----------------------------------------

@pytest.fixture
def outline_wx(dialog_module, monkeypatch):
    """The wx names the outline column and its Run refusals add to the surface
    above: the 3D pane a board with dies brings along, the refusal MessageBox,
    and CallAfter for the log lines Run queues. Every StaticText is recorded,
    so a test can read the notes a row shows instead of a text control."""
    wx = dialog_module.wx
    boxes, later, labels = [], [], []

    class Pane(wx.Panel):
        def __init__(self, parent=None, label="", style=0):
            super().__init__(parent)
            self.pane = wx.Panel(self)

        def Collapse(self, collapse=True):
            self.collapsed = collapse

        def GetPane(self):
            return self.pane

    class Label(wx.StaticText):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            labels.append(self)

    for name, value in (
            ("CollapsiblePane", Pane), ("StaticText", Label),
            ("MessageBox", lambda message, *args: boxes.append(message)),
            ("CallAfter", lambda fn, *args: later.append((fn, args))),
            ("CP_DEFAULT_STYLE", 1 << 20), ("CP_NO_TLW_RESIZE", 1 << 21),
            ("OK", 1 << 22), ("ICON_WARNING", 1 << 23),
            ("EVT_COLLAPSIBLEPANE_CHANGED", object())):
        monkeypatch.setattr(wx, name, value, raising=False)
    # die_outline imports pcbnew only when a board has footprints.
    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    return types.SimpleNamespace(boxes=boxes, later=later, labels=labels)


def install_writer_double(monkeypatch):
    """The pcbnew-bound writer module as the dialog imports it: the reads the
    constructor makes, the thickness grammar Run uses, and write-backs that
    only record."""
    writes = []
    writer = types.ModuleType("chiplet_export.writers.chiplet_writer")
    writer.list_die_refs = lambda board: sorted(
        fp.GetReference() for fp in board.Footprints()
        if fp.HasField("GDS_FILE") and fp.GetFieldText("GDS_FILE"))
    writer.read_die_connections = lambda board: {}
    writer.read_die_thicknesses = lambda board: {}

    def parse_thickness_um(text):
        try:
            value = float(text)
        except ValueError:
            return None
        return value if value > 0 else None

    writer.parse_thickness_um = parse_thickness_um
    for name in ("write_die_connections", "write_die_thicknesses",
                 "write_die_outline_layers"):
        def record(board, mapping, name=name):
            writes.append((name, dict(mapping)))
            return sorted(mapping)
        setattr(writer, name, record)
    monkeypatch.setitem(sys.modules, writer.__name__, writer)
    return writes


def outline_run_controls(dialog, board, rows):
    """What a Run click reads beyond populate_controls, so the real
    _collect_options builds the options."""
    dialog._board = board
    dialog._plugin_dir = "/plugin"
    dialog._die_outline_ctrls = {ref: Control(text)
                                 for ref, text in rows.items()}
    dialog._out_dir_ctrl = Control("/output")
    for name in ("_cb_complete", "_cb_annotate", "_cb_fill"):
        setattr(dialog, name, Control(False))
    dialog._fill_mode = Control()
    dialog._top_cell_ctrl = Control("INTERPOSER")
    for name in ("_lyp_ctrl", "_export_report_ctrl", "_worker_ctrl", "_interposer_root_ctrl",
                 "_adk_root_ctrl"):
        setattr(dialog, name, Control(""))


def forbid_run_side_effects(dialog, dialog_module, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a refused outline layer reached the run")

    monkeypatch.setattr(dialog, "_set_running", forbidden)
    monkeypatch.setattr(dialog_module, "run_export", forbidden)
    monkeypatch.setattr(dialog_module.threading, "Thread", forbidden)


NO_ROW_LAYER = "the export dialog gives it no outline layer. Set %s" % FIELD
ROW_LAYER = "the export dialog gives it the outline layer %r, which is not "


def _outline_refusal_case(case):
    """(board, rows, fragments the one MessageBox must carry)."""
    if case == "empty row":
        return ([_die("U1"), _die("U2", None)], {"U1": "39/4", "U2": ""},
                ["Die outline layer refused:", "U2: " + NO_ROW_LAYER])
    if case == "malformed row":
        return ([_die("U1", "39.0/4")], {"U1": "39.0/4"},
                ["U1: " + ROW_LAYER % "39.0/4", "(39/4 for SG13G2 dies)"])
    if case == "every bad row at once":
        return ([_die("U1", None), _die("U2", "a/b"), _die("U3")],
                {"U1": "", "U2": "a/b", "U3": "39/4"},
                ["U1: " + NO_ROW_LAYER, "U2: " + ROW_LAYER % "a/b"])
    if case == "a bad row and parts without a row":
        # One box for both: the rows are not checked on their own first. The
        # parts share one cause, stated once for both.
        return ([_die("U1", "39/4"), _Footprint("J1", gds=False),
                 _Footprint("J2", gds=False)], {"U1": "a/b"},
                ["U1: " + ROW_LAYER % "a/b",
                 "J1, J2: no GDS_FILE field", "on its symbol in the schematic"])
    if case == "preflight refusal":
        # J1 has no GDS_FILE, so it has no row; the preflight still sees it.
        return ([_die("U1"), _Footprint("J1", gds=False)], {"U1": "39/4"},
                ["Die outline layer refused:", "J1: no GDS_FILE field",
                 "Exclude from bill of materials"])
    assert case == "carrier refusal"
    return ([_die("U1"), _marked("U2", duplicate=PROPERTY)], {"U1": "39/4"},
            ["Carrier pad domains refused:", "Duplicate footprint property"])


@pytest.mark.parametrize("case", [
    "empty row", "malformed row", "every bad row at once",
    "a bad row and parts without a row", "preflight refusal",
    "carrier refusal"])
def test_a_refused_outline_layer_is_one_message_box_with_no_side_effects(
        dialog, dialog_module, outline_wx, monkeypatch, case):
    writes = install_writer_double(monkeypatch)
    footprints, rows, fragments = _outline_refusal_case(case)
    outline_run_controls(dialog, _Board(footprints), rows)
    forbid_run_side_effects(dialog, dialog_module, monkeypatch)
    dialog._on_run(None)
    assert len(outline_wx.boxes) == 1, outline_wx.boxes
    for fragment in fragments:
        assert fragment in outline_wx.boxes[0], (fragment, outline_wx.boxes)
    if case == "every bad row at once":
        assert "U3" not in outline_wx.boxes[0]
    assert outline_wx.boxes[0].count("no GDS_FILE field") <= 1
    assert writes == []  # not even the CONNECTION write-back
    assert outline_wx.later == []
    assert dialog._worker_thread is None
    assert dialog._log_ctrl.value == "" and dialog._status.value == "Idle"


def test_collect_options_passes_every_outline_row_as_typed(
        dialog, outline_wx, monkeypatch):
    install_writer_double(monkeypatch)
    outline_run_controls(dialog, None, {"U1": " 39/4 ", "U2": "39.0/4",
                                        "U3": ""})
    options = dialog._collect_options()
    assert options.die_outline_layers == {"U1": "39/4", "U2": "39.0/4",
                                          "U3": ""}


def test_run_writes_the_outline_rows_back_last_and_exports_them(
        dialog, dialog_module, outline_wx, monkeypatch):
    """The row replaces the board's malformed field: it is what the preflight
    checks, what is written back (after CONNECTION and thickness) and what
    run_export receives."""
    writes = install_writer_double(monkeypatch)
    board = _Board([_die("U1", "39.0/4"), _marked("U2")])
    outline_run_controls(dialog, board, {"U1": " 39/4 "})
    dialog._die_thick_ctrls = {"U1": Control("750")}
    threads, exported = [], []
    monkeypatch.setattr(
        dialog_module.threading, "Thread",
        lambda target, daemon: threads.append(target) or types.SimpleNamespace(
            start=lambda: None))
    monkeypatch.setattr(
        dialog_module, "run_export",
        lambda board, options, plugin_dir, **kwargs: exported.append(options)
        or orchestrator.ExportResult(error="stopped by the test"))
    dialog._on_run(None)
    assert outline_wx.boxes == []
    assert [name for name, _mapping in writes] == [
        "write_die_connections", "write_die_thicknesses",
        "write_die_outline_layers"]
    assert writes[-1][1] == {"U1": "39/4"}
    assert ((dialog._append_log,
             ("Updated DIE_OUTLINE_LAYER field on: U1 (save the board to "
              "keep it)",)) in outline_wx.later)
    threads[0]()
    assert exported[0].die_outline_layers == {"U1": "39/4"}


def test_the_outline_column_shows_the_board_text_raw(
        dialog_module, constructor_source, outline_wx, monkeypatch):
    """A malformed field is shown as the board has it, never as an empty row
    that Run would write back over it; Run then refuses it by name."""
    install_constructor_reader_double(constructor_source, monkeypatch)
    writes = install_writer_double(monkeypatch)
    u1 = _die("U1", "39.0/4")
    board = _Board([u1, _die("U3", None), _marked("U2"),
                    _Footprint("P1", fields={"IO_CLASS": "wire_bond"})])
    instance = dialog_module.ChipletExportDialog(None, board=board)
    ctrls = instance._die_outline_ctrls
    assert sorted(ctrls) == ["U1", "U3"]
    assert ctrls["U1"].GetValue() == "39.0/4"
    assert ctrls["U3"].GetValue() == ""
    assert {ctrl.hint for ctrl in ctrls.values()} == {"layer/datatype"}
    notes = {label.GetLabel(): label.tooltip for label in outline_wx.labels}
    assert "Outline layer (L/D)" in notes
    assert "CHIPLET_PAD_DOMAINS" in notes["exempt: archived PinList size"]
    assert "IO pad" in notes["not exported as a die"]

    forbid_run_side_effects(instance, dialog_module, monkeypatch)
    instance._on_run(None)
    assert len(outline_wx.boxes) == 1
    assert "U3: " + NO_ROW_LAYER in outline_wx.boxes[0]
    assert "U1: " + ROW_LAYER % "39.0/4" in outline_wx.boxes[0]
    assert writes == []
    assert u1.GetFieldText(FIELD) == "39.0/4"


def test_a_board_the_column_cannot_read_is_not_shown_as_empty_rows(
        dialog_module, constructor_source, outline_wx, monkeypatch):
    """A failed read keeps its rows and names itself; Run reads the board
    again and refuses with the cause, before any write-back."""
    install_constructor_reader_double(constructor_source, monkeypatch)
    writes = install_writer_double(monkeypatch)
    board = _Board([_die("U1"), _marked("U2", duplicate=PROPERTY)])
    instance = dialog_module.ChipletExportDialog(None, board=board)
    assert instance._die_outline_ctrls == {}
    unread = [label for label in outline_wx.labels
              if label.GetLabel() == "board not read"]
    assert len(unread) == 2
    assert "Duplicate footprint property" in unread[0].tooltip

    forbid_run_side_effects(instance, dialog_module, monkeypatch)
    instance._on_run(None)
    assert len(outline_wx.boxes) == 1
    assert outline_wx.boxes[0].startswith("Carrier pad domains refused:")
    assert writes == []


@pytest.mark.parametrize("drc, error, status", [
    (0, "", "Done (exit 0)"),
    (1, "", "Done - assembly DRC FAILED"),
    (-1, "Traceback (most recent call last):\nOSError: boom\n",
     "Done - assembly DRC did not complete"),
], ids=["passed", "failed", "stage-raised"])
def test_done_status_names_a_drc_that_did_not_complete(dialog, drc, error,
                                                       status):
    """A DRC stage that raised after the files were written used to reach the
    worker's crash handler. Caught, it must not read as a clean Done."""
    dialog._on_done(orchestrator.ExportResult(
        exit_code=0, assembly_drc_exit_code=drc, assembly_drc_error=error))
    assert dialog._status.value == status
    if error:
        assert "OSError: boom\n" in dialog._log_ctrl.value
        assert "assembly DRC: NOT RUN\n" in dialog._log_ctrl.value


@pytest.mark.parametrize("error_input, expected", [
    ("export_report", "ERROR: refused; the export report is the file in 'Interposer export report'\n"),
    ("", "ERROR: refused\n"),
])
def test_dialog_identifies_the_input_named_by_the_pipeline(dialog, error_input, expected):
    result = orchestrator.ExportResult(error="refused", error_input=error_input)
    dialog._on_done(result)
    assert dialog._log_ctrl.value == expected
    assert dialog._status.value.startswith("Error: ")


def _answer_inline(dialog_module, monkeypatch, answer):
    """Run the availability query inline; ``answer`` is its result or raises.

    Returns the queries made and ``deliver``, which hands the UI thread the
    answers posted so far, as wx.CallAfter would once the dialog is idle.
    """
    queries, posted = [], []

    def deliver():
        while posted:
            fn, args = posted.pop(0)
            fn(*args)

    def query(worker, plugin_dir, options):
        queries.append((worker, plugin_dir, options.interposer_pdk_root,
                        options.interconnect_pdk_root))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(dialog_module, "query_connection_availability", query)
    monkeypatch.setattr(dialog_module, "_in_background",
                        lambda target, *args: target(*args))
    monkeypatch.setattr(dialog_module.wx, "CallAfter",
                        lambda fn, *args: posted.append((fn, args)),
                        raising=False)
    return queries, deliver


BUMP = ({"original": None, "bump": "bump: no pad cell"},
        "INTERCONNECT_PDK_ROOT=/resolved/pdk /worker/python "
        "/plugin/connection_methods.py")


def _offer_an_undrawable_method(dialog, dialog_module, monkeypatch):
    queries, deliver = _answer_inline(dialog_module, monkeypatch, BUMP)
    monkeypatch.setattr(dialog_module, "available_connection_types",
                        lambda *args, **kwargs: ["", "original", "bump"])
    monkeypatch.setattr(dialog_module, "connection_method_specs",
                        lambda *args, **kwargs: {"original": {}, "bump": {}})
    # The root the preflight resolved, not the text in the field.
    monkeypatch.setattr(dialog_module, "_preflight_interconnect_manifest",
                        lambda *args, **kwargs: InterconnectManifestSnapshot(
                            {"methods": {"original": {}, "bump": {}}},
                            root="/resolved/pdk"))
    assert dialog._refresh_connection_choices() is True
    assert not any(label.endswith("(unavailable)")
                   for label in dialog._conn_ctrl.choices)
    deliver()
    return queries, deliver


def test_a_method_the_worker_refuses_is_labelled_with_its_reason(
        dialog, dialog_module, monkeypatch):
    queries, _deliver = _offer_an_undrawable_method(
        dialog, dialog_module, monkeypatch)
    # The worker, both roots and the plugin are the ones run_export uses.
    assert queries == [("/worker/python", "/plugin", "/interposer/pdk",
                        "/resolved/pdk")]
    assert dialog._conn_ctrl.choices[2].endswith("(unavailable)")
    assert not dialog._conn_ctrl.choices[1].endswith("(unavailable)")
    # The per-die rows, filled before the answer came, are relabelled too.
    assert dialog._die_conn_ctrls["U1"].choices[2].endswith("(unavailable)")
    assert dialog._conn_choices[dialog._conn_ctrl.GetSelection()] == "original"
    assert ("$ INTERCONNECT_PDK_ROOT=/resolved/pdk /worker/python "
            "/plugin/connection_methods.py") in dialog._log_ctrl.value
    assert "Unavailable: bump: no pad cell" in dialog._log_ctrl.value
    dialog._conn_ctrl.SetSelection(2)
    dialog._on_conn_choice(None)
    assert dialog._conn_detail.value == "Unavailable: bump: no pad cell"


def test_run_stays_disabled_while_a_die_uses_an_undrawable_method(
        dialog, dialog_module, monkeypatch):
    _offer_an_undrawable_method(dialog, dialog_module, monkeypatch)
    assert dialog._run_btn.enabled
    dialog._die_conn_ctrls["U1"].SetSelection(2)
    dialog._on_die_conn_selected("U1")
    assert not dialog._run_btn.enabled
    assert "U1 uses bump" in dialog._status.value
    assert dialog._die_conn_ctrls["U1"].tooltip == "Unavailable: bump: no pad cell"
    dialog._set_running(False)
    assert not dialog._run_btn.enabled

    # A die on the default follows the assembly-wide choice.
    dialog._die_conn_ctrls["U1"].SetSelection(0)
    dialog._on_die_conn_selected("U1")
    assert dialog._run_btn.enabled and dialog._status.value == "Idle"
    dialog._conn_ctrl.SetSelection(2)
    dialog._on_conn_choice(None)
    assert not dialog._run_btn.enabled
    dialog._conn_ctrl.SetSelection(1)
    dialog._on_conn_choice(None)
    assert dialog._run_btn.enabled and dialog._status.value == "Idle"


def test_each_worker_and_root_pair_is_asked_once(
        dialog, dialog_module, monkeypatch):
    queries, deliver = _offer_an_undrawable_method(
        dialog, dialog_module, monkeypatch)
    log = dialog._log_ctrl.value
    assert dialog._refresh_connection_choices() is True
    deliver()
    assert len(queries) == 1 and dialog._log_ctrl.value == log
    dialog._interposer_root_ctrl.value = "/other/interposer"
    assert dialog._refresh_connection_choices() is True
    deliver()
    assert queries[-1][2] == "/other/interposer" and len(queries) == 2
    # The new pair's answer is logged once, under the old one.
    assert dialog._log_ctrl.value.startswith(log)
    assert dialog._log_ctrl.value.count("Unavailable: bump: no pad cell") == 2


def test_an_answer_for_an_older_pair_is_dropped(
        dialog, dialog_module, monkeypatch):
    _offer_an_undrawable_method(dialog, dialog_module, monkeypatch)
    old_key = dialog._availability_key
    dialog._availability_key = ("/new/worker", "", "")
    dialog._conn_availability = {}
    dialog._apply_availability(old_key, BUMP)
    assert dialog._conn_availability == {}
    dialog._closing = True
    dialog._apply_availability(dialog._availability_key, BUMP)
    assert dialog._conn_availability == {}


def test_an_availability_query_that_fails_marks_nothing(
        dialog, dialog_module, monkeypatch):
    _queries, deliver = _answer_inline(
        dialog_module, monkeypatch,
        dialog_module.ConnectionAvailabilityError("worker exited 1: boom"))
    dialog._interposer_root_ctrl.value = "/other/interposer"
    assert dialog._refresh_connection_choices() is True
    deliver()
    assert not any(label.endswith("(unavailable)")
                   for label in dialog._conn_ctrl.choices)
    assert dialog._run_btn.enabled
    assert ("WARNING: method availability unknown, checked again at Run: "
            "worker exited 1: boom") in dialog._log_ctrl.value


def test_without_a_worker_nothing_is_asked(dialog, dialog_module, monkeypatch):
    """No worker typed and none found: run_export resolves one at Run."""
    queries, deliver = _answer_inline(dialog_module, monkeypatch, BUMP)
    discovery = importlib.import_module(
        dialog_module.__package__ + ".pipeline.discovery")
    monkeypatch.setattr(discovery, "preview_worker_python",
                        lambda *args, **kwargs: ("", "not found"))
    dialog._worker_ctrl.value = ""
    dialog._interposer_root_ctrl.value = "/other/interposer"
    assert dialog._refresh_connection_choices() is True
    deliver()
    assert queries == []
    assert dialog._run_btn.enabled

