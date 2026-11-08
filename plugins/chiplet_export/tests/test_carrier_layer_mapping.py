# SPDX-License-Identifier: GPL-3.0-or-later
"""Physical mapping admission with real LYP parsing and doubled KiCad/worker APIs."""

import json
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from .test_pad_domains import PCBNEW, PROPERTY, document, footprint
from .test_orchestrator import _install_run_export_fakes
from .test_layer_map_parity import REFERENCE_LYP, REFERENCE_SHA256
from chiplet_export.layer_map import (
    CarrierRootRefused, LayerMap, LayerMapError, resolve_lyp_path,
)
from chiplet_export.pad_domains import PadDomainError, validate_board_pad_domains
from chiplet_export.pipeline import orchestrator
from chiplet_export.writers.connection_stacks import InterconnectManifestSnapshot


def write_lyp(path, entries=None):
    if entries is None:
        entries = [("TopMetal2.drawing", "134/0@1"), ("Metal4.drawing", "50/0"),
                   ("Metal5.drawing", "67/0"), ("Contact.drawing", "134/0")]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<layer-properties>" + "".join(
        "<properties><name>%s</name><source>%s</source></properties>" % entry
        for entry in entries) + "</layer-properties>")
    return path


def methods_for(doc):
    ixn = doc["interconnect"]
    return {ixn["method"]: {
        "fab_anchor": ixn["fab_anchor"], "body_diameter_um": ixn["body_diameter_um"],
        "fab_params": {key: ixn[key] for key in ("passiv_opening_um", "tm2_enclosure_um")}}}


def board_for(fp, name="TopMetal2"):
    return SimpleNamespace(Footprints=lambda: [fp], GetLayerName=Mock(return_value=name),
                           GetFileName=lambda: "/project/design.kicad_pcb")


@pytest.fixture
def setup_export(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    fp, props = footprint()
    calls, seams = _install_run_export_fakes(monkeypatch, die_connections={"U1": "attachment"})
    snapshot = InterconnectManifestSnapshot({"methods": methods_for(document())})
    monkeypatch.setattr(orchestrator, "_preflight_interconnect_manifest", lambda *a, **kw: snapshot)
    options = orchestrator.ExportOptions(
        output_dir=str(tmp_path / "out"), emit_chiplet=False, emit_assembly_drc=False,
        lyp_override=str(write_lyp(tmp_path / "selected.lyp")))
    return board_for(fp), props, options, calls, seams


@pytest.mark.parametrize("name", ["Metal4", "Metal5", "unknown", "F.Cu", ""])
@pytest.mark.parametrize("existing", [False, True])
def test_alias_edit_with_unchanged_enum_refuses_before_writes(
        setup_export, tmp_path, monkeypatch, name, existing):
    board, props, options, calls, seams = setup_export
    board.GetLayerName.return_value = name
    # Physical pad enum and archived carrier metadata stay intact.
    assert board.Footprints()[0].Pads()[0].GetLayerSet().Seq() == [PCBNEW.F_Cu]
    assert json.loads(props[PROPERTY])["carrier"]["kicad_layer"] == "F.Cu"
    output = Path(options.output_dir)
    if existing:
        output.mkdir()
        for filename in ("design.hyp", "design.chiplet", "accepted.gds"):
            (output / filename).write_bytes(b"accepted evidence")
    before = {p.name: p.read_bytes() for p in output.glob("*")}
    mkdtemp = Mock(side_effect=AssertionError("preflight created a workspace"))
    monkeypatch.setattr(orchestrator.tempfile, "mkdtemp", mkdtemp)
    result = orchestrator.run_export(board, options, "/unused/plugin")
    assert "Carrier pad domains refused: U1 carrier layer" in result.error
    assert {p.name: p.read_bytes() for p in output.glob("*")} == before
    assert output.exists() == existing
    assert calls == [] and seams["writer_manifests"] == []
    mkdtemp.assert_not_called()
    board.GetLayerName.assert_called_once_with(PCBNEW.F_Cu)


@pytest.mark.parametrize("malformation", ["missing", "xml", "empty", "source", "conflict"])
def test_unusable_selected_lyp_refuses_without_output(setup_export, malformation):
    board, props, options, calls, seams = setup_export
    lyp = Path(options.lyp_override)
    if malformation == "missing":
        lyp.unlink()
    elif malformation == "xml":
        lyp.write_text("<broken")
    elif malformation == "empty":
        lyp.write_text("<layer-properties/>")
    else:
        write_lyp(lyp, [("TopMetal2.drawing", "134/0"),
                       ("TopMetal2.drawing", "bad" if malformation == "source" else "50/0")])
    result = orchestrator.run_export(board, options, "/unused/plugin")
    assert "Carrier pad domains refused: U1 carrier layer" in result.error
    assert not Path(options.output_dir).exists()
    assert calls == [] and seams["writer_manifests"] == []


@pytest.mark.parametrize("bad_source", [
    "", "<source/>", "<source></source>", "<source>  </source>",
    "<source>134/0/50</source>", "<source>134/0@bad</source>",
    "<source>134/0@</source>", "<source>134/0@1@2</source>",
    "<source>134/0@1/0</source>", "<source>134/0junk</source>",
    "<source>-134/0</source>", "<source>134/-1</source>",
])
@pytest.mark.parametrize("bad_first", [False, True])
def test_invalid_named_duplicate_refuses_in_either_order(
        setup_export, bad_source, bad_first):
    board, props, options, calls, seams = setup_export
    good = "<properties><name>TopMetal2.drawing</name><source>134/0</source></properties>"
    bad = "<properties><name>TopMetal2.drawing</name>%s</properties>" % bad_source
    entries = bad + good if bad_first else good + bad
    Path(options.lyp_override).write_text("<layer-properties>" + entries + "</layer-properties>")
    output = Path(options.output_dir)
    output.mkdir()
    accepted = output / "accepted.gds"
    accepted.write_bytes(b"accepted evidence")
    result = orchestrator.run_export(board, options, "/unused/plugin")
    assert "Invalid LYP source for 'TopMetal2:drawing'" in result.error
    assert list(output.iterdir()) == [accepted]
    assert accepted.read_bytes() == b"accepted evidence"
    assert calls == [] and seams["writer_manifests"] == []


@pytest.mark.parametrize("source", ["134", "134/0", "134@1", "134/0@1", "134/0@12", " 134/0@1 "])
def test_strict_lookup_accepts_supported_concrete_sources(tmp_path, source):
    lyp = write_lyp(tmp_path / "concrete.lyp", [("TopMetal2.drawing", source)])
    assert LayerMap(lyp, strict=True).get_layer("TopMetal2") == (134, 0)


@pytest.mark.parametrize("token,enum,name,opening,enclosure", [
    ("F.Cu", PCBNEW.F_Cu, "TopMetal2", 35, 7.5),
    ("In1.Cu", PCBNEW.In1_Cu, "TopMetal2", 45, 10),
    ("B.Cu", PCBNEW.B_Cu, "Contact", 35, 7.5),
])
def test_supported_slot_and_profile_use_resolved_physical_layer(
        tmp_path, monkeypatch, token, enum, name, opening, enclosure):
    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    doc = document(opening=opening, enclosure=enclosure)
    doc["carrier"]["kicad_layer"] = token
    fp, _ = footprint(doc)
    for pad in fp.Pads():
        pad.GetLayerSet.return_value = SimpleNamespace(Seq=lambda: [enum])
    board = board_for(fp, name)
    layer_map = LayerMap(write_lyp(tmp_path / "selected.lyp"), strict=True)
    load = Mock(return_value=layer_map)
    assert validate_board_pad_domains(board, methods_for(doc), load_layer_map=load) == {"U1": "attachment"}
    board.GetLayerName.assert_called_once_with(enum)
    load.assert_called_once_with()
    # This is a lookup/contract control, not new fabrication support for Metal5.
    assert layer_map.get_layer("Metal5") == (67, 0)


def test_marked_validation_requires_layer_source(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    fp, _ = footprint()
    with pytest.raises(PadDomainError, match="selected worker LYP"):
        validate_board_pad_domains(board_for(fp), methods_for(document()))


def test_board_layer_api_failure_refuses_before_writes(setup_export):
    board, props, options, calls, seams = setup_export
    board.GetLayerName.side_effect = RuntimeError("board layer unavailable")
    result = orchestrator.run_export(board, options, "/unused/plugin")
    assert "U1 carrier layer: board layer unavailable" in result.error
    assert not Path(options.output_dir).exists()
    assert calls == []


@pytest.mark.parametrize("explicit", [False, True])
def test_preflight_sends_exact_selected_lyp_to_worker(setup_export, tmp_path, monkeypatch, explicit):
    board, props, options, calls, seams = setup_export
    root = tmp_path / "selected-pdk"
    lyp = write_lyp(root / "libs.tech/klayout/tech/intm4tm2.lyp")
    lyp.write_bytes(REFERENCE_LYP.read_bytes())
    tooling = root / "libs.tech/klayout/python"
    tooling.mkdir()
    (tooling / "bump_mirror.py").write_text("# discovery marker only\n")
    (root / "libs.tech/intm4tm2_stack.json").write_text("{}\n")
    options.interposer_pdk_root = str(root)
    options.lyp_override = ("${INTERPOSER_PDK_ROOT}/libs.tech/klayout/tech/intm4tm2.lyp"
                            if explicit else "")
    original_option = options.lyp_override
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(tmp_path / "wrong-env"))
    # Picker-only preference must not silently replace the worker's selection.
    monkeypatch.setenv("INTERPOSER_LYP", str(tmp_path / "wrong-picker.lyp"))
    result = orchestrator.run_export(board, options, "/unused/plugin")
    assert result.error == ""
    assert len(calls) == 1
    assert calls[0][calls[0].index("-l") + 1] == str(lyp)
    assert hashlib.sha256(lyp.read_bytes()).hexdigest() == REFERENCE_SHA256
    assert seams["runner_envs"][0]["INTERPOSER_PDK_ROOT"] == str(root)
    assert options.lyp_override == original_option


def test_legacy_does_not_load_or_pin_carrier_mapping(setup_export, monkeypatch):
    board, props, options, calls, seams = setup_export
    del props[PROPERTY]
    options.lyp_override = ""
    board.GetLayerName.side_effect = AssertionError("legacy carrier qualification")
    result = orchestrator.run_export(board, options, "/unused/plugin")
    assert result.error == ""
    assert len(calls) == 1 and "-l" not in calls[0]
    board.GetLayerName.assert_not_called()


def test_lyp_variables_use_the_same_pinned_manifest_root_as_worker(
        setup_export, tmp_path, monkeypatch):
    board, props, options, calls, seams = setup_export
    selected = tmp_path / "board-selected-interconnect"
    (selected / "manifest").mkdir(parents=True)
    lyp = write_lyp(selected / "carrier.lyp")
    # A different discoverable sibling would win if preflight forgot the
    # manifest root already selected by the board's project settings.
    sibling = tmp_path / "interconnect_pdk"
    (sibling / "manifest").mkdir(parents=True)
    write_lyp(sibling / "carrier.lyp", [("Metal4.drawing", "50/0")])
    monkeypatch.delenv("INTERCONNECT_PDK_ROOT", raising=False)
    snapshot = InterconnectManifestSnapshot(
        {"methods": methods_for(document())}, root=str(selected))
    monkeypatch.setattr(orchestrator, "_preflight_interconnect_manifest", lambda *a, **kw: snapshot)
    options.lyp_override = "${INTERCONNECT_PDK_ROOT}/carrier.lyp"
    result = orchestrator.run_export(board, options, str(tmp_path / "plugin"))
    assert result.error == ""
    assert calls[0][calls[0].index("-l") + 1] == str(lyp)
    assert seams["runner_envs"][0]["INTERCONNECT_PDK_ROOT"] == str(selected)


def test_shared_reader_preserves_legacy_duplicate_policy(tmp_path):
    lyp = write_lyp(tmp_path / "duplicate.lyp", [
        ("TopMetal2.drawing", "134/0"), ("TopMetal2.drawing", "50/0")])
    assert LayerMap(lyp).get_layer("TopMetal2") == (50, 0)
    with pytest.raises(LayerMapError, match="Conflicting"):
        LayerMap(lyp, strict=True).get_layer("TopMetal2")


@pytest.mark.parametrize("name", ["${UNKNOWN}/file.lyp", "${INTERPOSER_PDK_ROOT/file.lyp"])
def test_bad_path_variables_are_loud(tmp_path, name):
    with pytest.raises(LayerMapError):
        resolve_lyp_path(name, env={}, start=tmp_path / "plugin/hyp_to_gds.py")


def test_default_resolution_keeps_worker_env_then_walk_rules(tmp_path):
    worker = tmp_path / "plugin/hyp_to_gds.py"
    sibling = tmp_path / "OpenIntM4TM2"
    lyp = write_lyp(sibling / "libs.tech/klayout/tech/intm4tm2.lyp")
    tooling = sibling / "libs.tech/klayout/python"
    tooling.mkdir()
    (tooling / "bump_mirror.py").write_text("# marker\n")
    (sibling / "libs.tech/intm4tm2_stack.json").write_text("{}\n")
    assert resolve_lyp_path(env={}, start=worker) == str(lyp)
    # A set root that is not an IntM4TM2 tree is refused, never walked past.
    with pytest.raises(CarrierRootRefused, match="^CARRIER_UNSUPPORTED_ROOT: "):
        resolve_lyp_path(env={"INTERPOSER_PDK_ROOT": str(tmp_path / "missing")}, start=worker)
