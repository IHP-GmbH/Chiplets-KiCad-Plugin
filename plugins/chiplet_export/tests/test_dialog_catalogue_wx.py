# SPDX-License-Identifier: GPL-3.0-or-later
"""Native wx catalogue smoke; use scripts/run_dialog_catalogue_wx.py under xvfb.

No modal event loop or export is started. The recovery leg supplies a labelled
reader double: it verifies GUI lifecycle, not supported-reader conformance.
Missing GUI imports/display are reported as skips on ordinary host runs.
Direct pytest in a capable environment may register the parent package before
this module executes; the standalone bootstrap suppresses only that registration.
"""

import gc
import json
import os
import sys
import types
from pathlib import Path

import pytest

wx = pytest.importorskip(
    "wx", reason="native dialog smoke needs wxPython")
pcbnew = pytest.importorskip(
    "pcbnew", reason="native dialog smoke needs pcbnew (KiCad Python)")
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    pytest.skip("native dialog smoke requires DISPLAY (use xvfb-run)",
                allow_module_level=True)
if not hasattr(pcbnew, "BOARD"):
    pytest.skip("native dialog smoke requires real pcbnew.BOARD",
                allow_module_level=True)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

# The standalone bootstrap imports the package before pytest startup. A guard
# here is too late: pytest already imports its parent while resolving conftest.
from chiplet_export import dialog_chiplet_export as dialog_module
from chiplet_export.pipeline import orchestrator
from chiplet_export.writers import chiplet_writer


@pytest.fixture(scope="module")
def wx_app():
    # Function-scoped dialog finalizers run before this owner is released.
    # Do not leave a newly created native application for interpreter shutdown,
    # where wx/pcbnew module destruction order is unspecified. An application
    # supplied by another harness remains that harness's responsibility.
    app = wx.GetApp()
    owns_app = app is None
    if owns_app:
        app = wx.App(False)
    try:
        yield app
    finally:
        if owns_app:
            app.ProcessPendingEvents()
            wx.YieldIfNeeded()  # include idle-time deferred window destruction
            gc.collect()       # release test-owned widget reference cycles now
            app.Destroy()


@pytest.fixture
def refused_dialog(wx_app, monkeypatch, tmp_path):
    root = tmp_path / "selected-data-only-pdk"
    (root / "manifest").mkdir(parents=True)
    manifest_path = root / "manifest" / "interconnect_methods.json"
    manifest_path.write_text(json.dumps({
        "methods": {"vendor_smoke": {"pitch_rules": {"IXN_pitch": 123.0}}}
    }), encoding="utf-8")
    monkeypatch.setenv("INTERCONNECT_PDK_ROOT", str(root))
    monkeypatch.setattr(orchestrator, "_interconnect_reader", lambda: None)
    board = pcbnew.BOARD()
    board.SetFileName(str(tmp_path / "smoke.kicad_pcb"))
    dialog = dialog_module.ChipletExportDialog(None, board=board)
    try:
        yield dialog, board, root, manifest_path
    finally:
        # _on_close calls EndModal, which is inappropriate for this nonmodal
        # smoke. Perform its lifetime guard/timer cleanup then destroy widgets.
        dialog._closing = True
        dialog._conn_refresh_timer.Stop()
        dialog.Destroy()
        wx_app.ProcessPendingEvents()
        wx.YieldIfNeeded()  # Destroy() schedules native top-level deletion


def repair_with_reader_double(monkeypatch):
    """Repair import availability only; schema policy is tested elsewhere."""
    reads = []

    def load_manifest(path):
        reads.append(Path(path))
        return json.loads(Path(path).read_text(encoding="utf-8"))

    reader = types.SimpleNamespace(load_manifest=load_manifest)
    monkeypatch.setattr(orchestrator, "_interconnect_reader", lambda: reader)
    return reads


def test_native_constructor_refusal_leaves_source_and_close_usable(refused_dialog):
    dialog, board, root, _ = refused_dialog
    assert dialog._board is board
    assert dialog._interconnect_root_ctrl.GetPath() == str(root)
    diagnostic = dialog._log_ctrl.GetValue()
    assert str(root) in diagnostic
    assert "reader" in diagnostic
    assert "KiCad" in diagnostic
    assert not dialog._run_btn.IsEnabled()
    assert not dialog._conn_ctrl.IsEnabled()
    assert dialog._conn_ctrl.GetCount() == 0
    assert dialog._conn_choices == []  # no synthetic or fallback methods
    assert dialog._conn_specs == {}
    assert dialog._interconnect_root_ctrl.IsEnabled()
    assert dialog._interconnect_root_ctrl._text.IsEnabled()
    assert dialog._interconnect_root_ctrl._text.IsEditable()
    assert dialog._interconnect_root_ctrl._browse.IsEnabled()
    assert dialog._close_btn.IsEnabled()
    assert dialog._worker_thread is None


def test_native_constructor_recovers_candidate_inspection_error(
        wx_app, monkeypatch, tmp_path):
    root = tmp_path / "interconnect_pdk"
    manifest_path = root / "manifest" / "interconnect_methods.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("INTERCONNECT_PDK_ROOT", raising=False)
    monkeypatch.setattr(orchestrator, "__file__", str(
        tmp_path / "plugin" / "pipeline" / "orchestrator.py"))
    is_file = Path.is_file

    def denied(path):
        if path == manifest_path:
            raise PermissionError("controlled candidate inspection refusal")
        return is_file(path)

    monkeypatch.setattr(Path, "is_file", denied)
    board = pcbnew.BOARD()
    board.SetFileName(str(tmp_path / "inspection.kicad_pcb"))
    dialog = dialog_module.ChipletExportDialog(None, board=board)
    try:
        assert dialog._interconnect_root_ctrl.GetPath() == str(root)
        assert "cannot inspect" in dialog._catalogue_error
        assert str(manifest_path) in dialog._log_ctrl.GetValue()
        assert not dialog._run_btn.IsEnabled()
        assert dialog._interconnect_root_ctrl._text.IsEditable()
        assert dialog._interconnect_root_ctrl._browse.IsEnabled()
        assert dialog._close_btn.IsEnabled()
        monkeypatch.setattr(Path, "is_file", is_file)
        manifest_path.write_text(json.dumps({
            "methods": {"recovered": {}}
        }), encoding="utf-8")
        repair_with_reader_double(monkeypatch)
        dialog._on_conn_refresh_timer(None)
        assert not dialog._catalogue_error
        assert dialog._run_btn.IsEnabled()
    finally:
        dialog._closing = True
        dialog._conn_refresh_timer.Stop()
        dialog.Destroy()
        wx_app.ProcessPendingEvents()
        wx.YieldIfNeeded()


def test_native_refresh_recovers_with_reader_double(refused_dialog, monkeypatch):
    dialog, _, root, manifest_path = refused_dialog
    reads = repair_with_reader_double(monkeypatch)
    # Execute the real timer callback against native controls without waiting.
    dialog._on_conn_refresh_timer(None)
    assert reads and all(path == manifest_path for path in reads)
    assert dialog._interconnect_root_ctrl.GetPath() == str(root)
    assert not dialog._catalogue_error
    assert dialog._conn_choices == ["", "vendor_smoke"]
    assert dialog._conn_ctrl.GetCount() == 2
    assert "vendor_smoke" in dialog._conn_ctrl.GetString(1)
    assert dialog._conn_specs["vendor_smoke"]["pitch"] == 123.0
    assert dialog._conn_ctrl.IsEnabled()
    assert dialog._run_btn.IsEnabled()
    assert dialog._status.GetLabel() == "Idle"
    assert "loaded from the resolved source" in dialog._log_ctrl.GetValue()


def test_native_run_before_debounce_refuses_without_side_effects(
        refused_dialog, monkeypatch, tmp_path):
    dialog, board, root, _ = refused_dialog
    repair_with_reader_double(monkeypatch)
    dialog._on_conn_refresh_timer(None)
    assert dialog._run_btn.IsEnabled()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("side effect")
        pytest.fail("refused GUI source reached board writes or export")

    monkeypatch.setattr(chiplet_writer, "write_die_connections", forbidden)
    monkeypatch.setattr(chiplet_writer, "write_die_thicknesses", forbidden)
    monkeypatch.setattr(chiplet_writer, "write_die_outline_layers", forbidden)
    monkeypatch.setattr(dialog_module, "run_export", forbidden)
    monkeypatch.setattr(dialog, "_collect_options", forbidden)
    incomplete = tmp_path / "incomplete-selected-pdk"
    incomplete.mkdir()
    # ChangeValue edits the real text control without dispatching EVT_TEXT;
    # explicitly schedule the real debounce, then click Run before yielding.
    dialog._interconnect_root_ctrl._text.ChangeValue(str(incomplete))
    dialog._schedule_connection_refresh()
    assert dialog._conn_refresh_timer.IsRunning()
    dialog._on_run(None)
    assert calls == []
    assert dialog._worker_thread is None
    assert not dialog._run_btn.IsEnabled()
    assert not dialog._conn_ctrl.IsEnabled()
    assert str(incomplete) in dialog._catalogue_error
    assert "interconnect_methods.json" in dialog._catalogue_error
    assert dialog._board is board
    assert board.GetFileName() == str(tmp_path / "smoke.kicad_pcb")
    # Stop the pending event before checking recovery against the original root.
    dialog._conn_refresh_timer.Stop()
    dialog._interconnect_root_ctrl._text.ChangeValue(str(root))
    dialog._on_conn_refresh_timer(None)
    assert dialog._run_btn.IsEnabled()
