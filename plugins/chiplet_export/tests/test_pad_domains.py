# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract/API doubles, not a claim of native pcbnew or PCell execution."""

import copy
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))
from chiplet_export.pad_domains import (  # noqa: E402
    PROPERTY, PadDomainError, decode_domains, footprint_pin_list,
    archived_die_dimensions, validate_export_pad_sources,
)


PCBNEW = SimpleNamespace(
    PCB_IU_PER_MM=1_000_000, F_Cu=0, In1_Cu=2, B_Cu=31, PAD_ATTRIB_SMD=1,
    FP_EXCLUDE_FROM_BOM=2, F_CrtYd=45, B_CrtYd=46,
    PADSTACK=SimpleNamespace(MODE_NORMAL=0), PAD_SHAPE_CIRCLE=0,
)


def document(dbu=0.001, opening=35, enclosure=7.5):
    return {
        "format": "chiplet_kicad.pad_domains", "version": 1, "component": "U1",
        "die": {"frame": "die_local_gds_y_up", "pins_sha256": "a" * 64,
                "pin_list": {"version": 1, "chiplet_name": "DIE", "dbu_um": dbu,
                             "die_bbox_dbu": {"x_min": -20 / dbu, "x_max": 120 / dbu,
                                              "y_min": -40 / dbu, "y_max": 60 / dbu},
                             "pad_layer": "134/0", "pins": [
                    {"pad_index": 8, "name": "VDD", "type": "power_in",
                     "center_x_dbu": 10 / dbu, "center_y_dbu": 20 / dbu,
                     "width_dbu": 65.8 / dbu, "height_dbu": 70 / dbu,
                     "polygon_points_dbu": [[0, 0], [1, 0], [1, 1]],
                     "future_geometry": {"x": 123}},
                    {"pad_index": 2, "name": "VDD", "type": "passive",
                     "center_x_dbu": 80 / dbu, "center_y_dbu": 20 / dbu,
                     "width_dbu": 65.8 / dbu, "height_dbu": 65.8 / dbu},
                ]}},
        "interconnect": {"method": "attachment", "manifest_sha256": "b" * 64,
                         "fab_anchor": "ihp_cupillar_anchor", "body_diameter_um": 44,
                         "passiv_opening_um": opening, "tm2_enclosure_um": enclosure},
        "carrier": {"frame": "die_local_gds_y_up", "shape": "circle",
                    "diameter_um": opening + 2 * enclosure, "layer": "TopMetal2",
                    "kicad_layer": "F.Cu", "tech_sha256": "c" * 64},
        "attachments": [{"pad_index": 8, "pad_number": "VDD"},
                        {"pad_index": 2, "pad_number": "VDD"}],
    }


def vector(x, y):
    return SimpleNamespace(x=x, y=y)


def footprint(doc=None, orientation="flip_chip"):
    doc = document() if doc is None else doc
    props = {"GDS_FILE": "original.gds", "CONNECTION": "attachment",
             "ORIENTATION": orientation, PROPERTY: json.dumps(doc)}
    diameter = doc["carrier"]["diameter_um"] * 1000
    dbu = doc["die"]["pin_list"]["dbu_um"]
    mx = -1 if orientation == "flip_chip" else 1
    pads = []
    for i, pin in enumerate(doc["die"]["pin_list"]["pins"]):
        pads.append(Mock(
            GetNumber=Mock(return_value=pin["name"]),
            GetName=Mock(return_value=pin["name"]),
            GetNetCode=Mock(return_value=i),  # Includes an unassigned physical pad.
            GetFPRelativePosition=Mock(return_value=vector(
                mx * pin["center_x_dbu"] * dbu * 1000,
                -pin["center_y_dbu"] * dbu * 1000)),
            GetSizeX=Mock(return_value=diameter), GetSizeY=Mock(return_value=diameter),
            GetShape=Mock(return_value=PCBNEW.PAD_SHAPE_CIRCLE),
            GetLayerSet=Mock(return_value=SimpleNamespace(Seq=lambda: [0])),
            GetAttribute=Mock(return_value=PCBNEW.PAD_ATTRIB_SMD),
            Padstack=Mock(return_value=SimpleNamespace(Mode=lambda: 0)),
            GetDrillSize=Mock(return_value=vector(0, 0)),
            GetOffset=Mock(return_value=vector(0, 0)),
        ))
    fp = Mock(
        GetReference=Mock(return_value="U1"),
        # Not excluded from the BOM. die_role reads this for the export
        # preflight and the writer; a bare Mock would not combine with a flag.
        GetAttributes=Mock(return_value=0),
        Pads=Mock(return_value=pads),
        GetOrientation=Mock(return_value=SimpleNamespace(AsDegrees=lambda: 90)),
        HasField=Mock(side_effect=lambda key: key in props),
        GetFieldText=Mock(side_effect=lambda key: props[key]),
        GetFields=Mock(side_effect=lambda: [Mock(
            GetName=Mock(return_value=key), GetText=Mock(return_value=value))
            for key, value in props.items()]),
    )
    return fp, props


@pytest.mark.parametrize("dbu", [0.001, 0.002])
@pytest.mark.parametrize("orientation,mx", [("flip_chip", -1), ("face_up", 1)])
def test_preserves_original_die_size_identity_and_nm_frame(dbu, orientation, mx):
    doc = document(dbu)
    original = copy.deepcopy(doc)
    fp, props = footprint(doc, orientation)
    fp.Pads.return_value.reverse()  # Never use API order as physical identity.
    result = footprint_pin_list(fp, PCBNEW)
    assert result["dbu_um"] == 0.001
    assert [p["pad_index"] for p in result["pins"]] == [8, 2]
    assert [p["name"] for p in result["pins"]] == ["VDD", "VDD"]
    assert result["pins"][0]["type"] == "power_in"
    assert result["pins"][0]["center_x_dbu"] == mx * 10000
    assert result["pins"][0]["center_y_dbu"] == 20000
    assert result["pins"][0]["width_dbu"] == pytest.approx(65800)
    assert result["pins"][0]["height_dbu"] == 70000
    assert result["pins"][0]["polygon_points_dbu"][1] == [mx * dbu / 0.001, 0]
    assert "future_geometry" not in result["pins"][0]
    assert json.loads(props[PROPERTY]) == original  # Immutable source inventory.
    fp.GetOrientation.assert_not_called()  # Rotation is applied downstream, once.
    for pad in fp.Pads.return_value:
        pad.GetFPRelativePosition.assert_called_once()


def test_second_attachment_geometry_is_not_a_coupon_constant():
    fp, _ = footprint(document(opening=45, enclosure=10))
    assert footprint_pin_list(fp, PCBNEW)["pins"][0]["width_dbu"] == pytest.approx(65800)


@pytest.mark.parametrize("edit", [
    lambda d: d.update(version=2),
    lambda d: d.update(version=True),
    lambda d: d.update(component="U2"),
    lambda d: d["die"].update(frame="board_local"),
    lambda d: d["die"].update(pins_sha256="bad"),
    lambda d: d["carrier"].update(shape="rect"),
    lambda d: d["carrier"].update(diameter_um=55),
    lambda d: d["carrier"].update(kicad_layer="TopMetal2"),
    lambda d: d["interconnect"].update(method="unknown"),
    lambda d: d["interconnect"].update(fab_anchor=""),
    lambda d: d["interconnect"].pop("fab_anchor"),
    lambda d: d["interconnect"].update(tm2_enclosure_um=float("nan")),
    lambda d: d["die"]["pin_list"].update(dbu_um=True),
    lambda d: d["die"]["pin_list"].pop("dbu_um"),
    lambda d: d["die"]["pin_list"].pop("die_bbox_dbu"),
    lambda d: d["die"]["pin_list"]["die_bbox_dbu"].update(x_max=-20000),
    lambda d: d["die"]["pin_list"]["die_bbox_dbu"].update(y_min=float("nan")),
    lambda d: d["die"]["pin_list"]["pins"][0].update(pad_index=2),
    lambda d: d["die"]["pin_list"]["pins"][0].update(width_dbu=-1),
    lambda d: d["attachments"].pop(),
    lambda d: d["attachments"][0].update(pad_number="OTHER"),
])
def test_tampered_or_unsupported_record_refuses(edit):
    doc = document()
    edit(doc)
    with pytest.raises(PadDomainError):
        decode_domains(json.dumps(doc), "U1", "attachment")


@pytest.mark.parametrize("text", ["", "{broken", "null", '{"version":1,"version":1}'])
def test_present_invalid_record_is_not_legacy(text):
    fp, props = footprint()
    props[PROPERTY] = text
    with pytest.raises(PadDomainError):
        footprint_pin_list(fp, PCBNEW)


@pytest.mark.parametrize("fault", ["shape", "size", "layer", "position", "offset", "count"])
def test_carrier_geometry_edits_refuse(fault):
    fp, _ = footprint()
    pad = fp.Pads.return_value[0]
    if fault == "shape":
        pad.GetShape.return_value = 1
    elif fault == "size":
        pad.GetSizeX.return_value = 65800
    elif fault == "layer":
        pad.GetLayerSet.return_value = SimpleNamespace(Seq=lambda: [0, 2])
    elif fault == "position":
        pad.GetFPRelativePosition.return_value = vector(0, 0)
    elif fault == "offset":
        pad.GetOffset.return_value = vector(1, 0)
    else:
        fp.Pads.return_value.pop()
    with pytest.raises(PadDomainError):
        footprint_pin_list(fp, PCBNEW)


def test_coincident_repeated_pin_names_are_ambiguous():
    doc = document()
    doc["die"]["pin_list"]["pins"][1]["center_x_dbu"] = 10000
    fp, _ = footprint(doc)
    with pytest.raises(PadDomainError, match="unique rigid"):
        footprint_pin_list(fp, PCBNEW)


@pytest.mark.parametrize("version", [1, "1.0", "1.0.1", "01.00.03"])
def test_supported_source_pin_list_versions(version):
    doc = document()
    doc["die"]["pin_list"]["version"] = version
    assert decode_domains(json.dumps(doc), "U1", "attachment")


@pytest.mark.parametrize("version", [True, 1.0, "1", "2.0", " 1.0", "1.0\n", "1.٠", None])
def test_unsupported_source_pin_list_versions(version):
    doc = document()
    doc["die"]["pin_list"]["version"] = version
    with pytest.raises(PadDomainError, match="PinList version"):
        decode_domains(json.dumps(doc), "U1", "attachment")


def test_newer_source_pin_list_minor_warns_and_accepts():
    doc = document()
    doc["die"]["pin_list"]["version"] = "1.2.3"
    with pytest.warns(UserWarning, match="newer PinList minor"):
        decode_domains(json.dumps(doc), "U1", "attachment")


def test_carrier_layer_uses_canonical_kicad_enum_not_fixed_numeric_id():
    doc = document()
    doc["carrier"]["kicad_layer"] = "In1.Cu"
    fp, _ = footprint(doc)
    for pad in fp.Pads.return_value:
        pad.GetLayerSet.return_value = SimpleNamespace(Seq=lambda: [PCBNEW.In1_Cu])
    assert footprint_pin_list(fp, PCBNEW)


def test_real_saved_driver_pin_list_remains_accepted():
    # A pins.json saved by a real die build. No tree carries one, so the case
    # is opt-in: the variable names the file.
    source = os.environ.get("CHIPLET_DRIVER_PIN_LIST")
    if not source:
        pytest.skip("CHIPLET_DRIVER_PIN_LIST unset")
    source = Path(source)
    doc = document()
    doc["die"]["pin_list"] = json.loads(source.read_text())
    doc["attachments"] = [{"pad_index": pin["pad_index"], "pad_number": pin["name"]}
                          for pin in doc["die"]["pin_list"]["pins"]]
    fp, props = footprint(doc)
    original = props[PROPERTY]
    result = footprint_pin_list(fp, PCBNEW)
    assert len(result["pins"]) == len(doc["attachments"])
    assert props[PROPERTY] == original


def test_unmarked_is_explicit_legacy_not_carrier_qualification():
    fp, props = footprint()
    del props[PROPERTY]
    assert footprint_pin_list(fp, PCBNEW) is None
    fp.Pads.assert_not_called()


@pytest.mark.parametrize("changed", [None, "tm2_enclosure_um", "body_diameter_um", "fab_anchor"])
def test_new_metadata_compares_to_selected_manifest(changed):
    fp, _ = footprint()
    source = {"fab_anchor": "ihp_cupillar_anchor", "body_diameter_um": 44,
              "fab_params": {"passiv_opening_um": 35, "tm2_enclosure_um": 7.5}}
    if changed == "tm2_enclosure_um":
        source["fab_params"][changed] = 9
    elif changed:
        source[changed] = "different" if changed == "fab_anchor" else 80
    if changed:
        with pytest.raises(PadDomainError, match="selected manifest"):
            footprint_pin_list(fp, PCBNEW, {"attachment": source})
    else:
        assert footprint_pin_list(fp, PCBNEW, {"attachment": source})
    with pytest.raises(PadDomainError, match="selected manifest method"):
        footprint_pin_list(fp, PCBNEW, {})


def test_the_anchor_comes_from_the_selected_manifest_not_a_literal():
    # A solder-bump record decodes here; whether its landing can be drawn is
    # the worker's check against the manifest's fab_anchors table.
    doc = document(opening=60, enclosure=10)
    doc["interconnect"].update(fab_anchor="ihp_sbump_anchor", body_diameter_um=80)
    fp, props = footprint(doc)
    source = {"fab_anchor": "ihp_sbump_anchor", "body_diameter_um": 80,
              "fab_params": {"passiv_opening_um": 60, "tm2_enclosure_um": 10}}
    assert footprint_pin_list(fp, PCBNEW, {"attachment": source})
    source["fab_anchor"] = "ihp_cupillar_anchor"
    with pytest.raises(PadDomainError, match="fab_anchor differs from selected manifest"):
        footprint_pin_list(fp, PCBNEW, {"attachment": source})


@pytest.mark.parametrize("override", [{}, {"pad_locations": {"U1": "external.json"}},
                                     {"cupillar_gds": "external.gds"}])
def test_pipeline_refuses_before_any_output_even_with_override(tmp_path, monkeypatch, override):
    from chiplet_export.pipeline import orchestrator

    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    fp, props = footprint()
    props[PROPERTY] = "{broken"
    board = SimpleNamespace(Footprints=lambda: [fp])
    monkeypatch.setattr(orchestrator, "_preflight_interconnect_manifest",
                        lambda *a, **kw: SimpleNamespace(methods={}))
    out = tmp_path / "new-output"
    existing = tmp_path / "accepted.gds"
    existing.write_bytes(b"accepted-evidence")
    result = orchestrator.run_export(board, orchestrator.ExportOptions(
        output_dir=str(out), **override), "/unused/plugin")
    assert "Carrier pad domains refused" in result.error
    assert not out.exists()
    assert existing.read_bytes() == b"accepted-evidence"


def test_writer_emits_original_die_geometry_not_carrier_size(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    from chiplet_export.writers import chiplet_writer
    monkeypatch.setattr(chiplet_writer, "pcbnew", PCBNEW)
    fp, _ = footprint()
    result = chiplet_writer.write_die_pin_lists(SimpleNamespace(Footprints=lambda: [fp]), tmp_path)
    pins = json.loads(Path(result["U1"]).read_text())["pins"]
    assert pins[0]["width_dbu"] == pytest.approx(65800)
    assert pins[0]["pad_index"] == 8


def test_unmarked_writer_preserves_legacy_pad_semantics(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    from chiplet_export.writers import chiplet_writer
    monkeypatch.setattr(chiplet_writer, "pcbnew", PCBNEW)
    fp, props = footprint()
    del props[PROPERTY]
    fp.Pads.return_value[0].GetSizeX.return_value = 65800
    result = chiplet_writer.write_die_pin_lists(SimpleNamespace(Footprints=lambda: [fp]), tmp_path)
    pins = json.loads(Path(result["U1"]).read_text())["pins"]
    assert pins[0]["width_dbu"] == 65800
    assert pins[0]["pad_index"] == 0  # Historical enumeration, not reinterpreted.
    assert "no carrier-domain qualification" in capsys.readouterr().err


@pytest.mark.parametrize("override", [
    {"die_connections": {"U1": "other_attachment"}},
    {"die_connections": {"U1": ""}},
    {"die_connections": {"U2": "attachment"}, "connection_type": "other_attachment"},
    {"die_connections": {"U2": "attachment"}},
    {"pad_locations": {"U1": "wrong_centers.json"}},
    {"pad_locations": {"U2": "unrelated_centers.json"}},
    {"cupillar_gds": "unverified_pillars.gds"},
])
def test_valid_domains_cannot_be_bypassed_by_export_options(tmp_path, monkeypatch, override):
    from chiplet_export.pipeline import orchestrator

    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    fp, _props = footprint()
    source = {"fab_anchor": "ihp_cupillar_anchor", "body_diameter_um": 44,
              "fab_params": {"passiv_opening_um": 35, "tm2_enclosure_um": 7.5}}
    monkeypatch.setattr(orchestrator, "_preflight_interconnect_manifest",
                        lambda *a, **kw: SimpleNamespace(methods={"attachment": source}, root=""))
    board = SimpleNamespace(Footprints=lambda: [fp], GetLayerName=lambda _: "TopMetal2")
    lyp = tmp_path / "carrier.lyp"
    lyp.write_text("<layer-properties><properties><name>TopMetal2.drawing</name>"
                   "<source>134/0</source></properties></layer-properties>")
    out = tmp_path / "new-output"
    accepted = tmp_path / "accepted.gds"
    accepted.write_bytes(b"unchanged-evidence")
    result = orchestrator.run_export(board, orchestrator.ExportOptions(
        output_dir=str(out), lyp_override=str(lyp), **override), "/unused/plugin")
    assert "Carrier pad domains refused" in result.error
    assert "regenerate" in result.error
    assert not out.exists()
    assert accepted.read_bytes() == b"unchanged-evidence"


@pytest.mark.parametrize("overrides,default", [
    ({}, "different_but_not_selected"),
    ({"U1": "attachment"}, "different_but_not_selected"),
    ({"U2": "other_attachment"}, "attachment"),
])
def test_identical_effective_method_override_is_allowed(overrides, default):
    validate_export_pad_sources(
        {"U1": "attachment"}, method_overrides=overrides,
        default_method=default, pad_locations={}, cupillar_gds="")


def test_legacy_only_export_source_overrides_remain_allowed():
    validate_export_pad_sources(
        {}, method_overrides={"U1": "other_attachment"}, default_method="fallback",
        pad_locations={"U1": "manual.json"}, cupillar_gds="manual.gds")


@pytest.mark.parametrize("dbu", [0.001, 0.002])
def test_archived_die_bbox_is_authoritative_not_courtyard(dbu):
    fp, _props = footprint(document(dbu=dbu))
    for name in ("GetBoundingBox", "GetCourtyard", "BuildCourtyardCaches"):
        getattr(fp, name).side_effect = AssertionError("editable artwork consulted")
    assert archived_die_dimensions(fp) == (140, 100)


def test_marked_writer_uses_unrotated_archived_dimensions(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    from chiplet_export.writers import chiplet_writer
    monkeypatch.setattr(chiplet_writer, "pcbnew", PCBNEW)
    monkeypatch.setattr(chiplet_writer, "validate_interconnect_ids", lambda **kw: None)
    monkeypatch.setattr(chiplet_writer, "emit_connection_stacks_block", lambda *a: "connection_stacks: {}\n")
    fp, _props = footprint()
    fp.GetAttributes.return_value = 0
    fp.GetPosition.return_value = vector(1_000_000, 2_000_000)
    fp.GetLayer.return_value = PCBNEW.F_Cu
    for name in ("GetBoundingBox", "GetCourtyard", "BuildCourtyardCaches"):
        getattr(fp, name).side_effect = AssertionError("editable artwork consulted")
    board = Mock(
        Footprints=Mock(return_value=[fp]), GetProperties=Mock(return_value={}),
        GetProject=Mock(return_value=None), GetFileName=Mock(return_value="design.kicad_pcb"),
        GetBoardEdgesBoundingBox=Mock(return_value=Mock(
            IsValid=Mock(return_value=True), GetWidth=Mock(return_value=5_000_000),
            GetHeight=Mock(return_value=5_000_000), GetX=Mock(return_value=0),
            GetBottom=Mock(return_value=5_000_000))),
        GetDesignSettings=Mock(return_value=SimpleNamespace(GetBoardThickness=lambda: 13830)),
    )
    output = tmp_path / "out.chiplet"
    assert chiplet_writer.write_chiplet(board, output)
    die_text = output.read_text().split("  - id: U1\n", 1)[1]
    assert "width: 140.000000" in die_text
    assert "height: 100.000000" in die_text
    assert "z: 90.0000" in die_text
    assert "x: 1000.000000" in die_text
    assert "y: 3000.000000" in die_text

    # A standalone writer call is guarded too, before opening/replacing the
    # target; the orchestration preflight is not the only protection.
    doc = json.loads(_props[PROPERTY])
    del doc["die"]["pin_list"]["die_bbox_dbu"]
    _props[PROPERTY] = json.dumps(doc)
    accepted_bytes = output.read_bytes()
    with pytest.raises(PadDomainError, match="die_bbox_dbu"):
        chiplet_writer.write_chiplet(board, output)
    assert output.read_bytes() == accepted_bytes


def test_unmarked_dimensions_keep_legacy_courtyard_path():
    fp, props = footprint()
    del props[PROPERTY]
    assert archived_die_dimensions(fp) is None
