# SPDX-License-Identifier: GPL-3.0-or-later
"""
Tests for the <stem>.pillars.json pillar manifest emitted by hyp_to_gds.

The manifest carries the as-drawn Cu-pillar/bump centers (canonical
interposer GDS-bbox-corner frame — the same frame .chiplet die positions
and io_pads live in — y-up, micrometers, post collision auto-resolve) so
manifest-level checks can align .chiplet placements against exactly what
the GDS holds. Pinned contract:
  * written next to the assembly GDS whenever the bump-generation path runs,
  * a bump-path run placing zero bumps still writes an empty pillars array,
  * runs that never enter the bump path write nothing,
  * positions match the instances add_device_bumps drew — including bumps
    shifted by collision auto-resolve (flagged moved_by_auto_resolve) —
    rebased by the interposer top-cell bbox lower-left corner,
  * schema/version strings are exact-match pinned, output is deterministic
    (sorted by device_ref then pin_name, trailing newline).
"""

import json
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

import hyp_to_gds as h  # noqa: E402
from chiplet_export.tests.test_hyp_to_gds_decoupling import (  # noqa: E402
    MIXED_HYP, _write_pin_list)

# Bump generation asserts real sibling-PDK content (bump_mirror in the
# interposer PDK, method geometry in the interconnect manifest). On a lone
# checkout none of those roots resolve -- skip the module.
_MISSING_ROOTS = [
    var for var in ("INTERCONNECT_PDK_ROOT", "INTERPOSER_PDK_ROOT",
                    "GDS_TO_KICAD_ROOT")
    if h._discover_path_var(var) is None
]
pytestmark = pytest.mark.skipif(
    bool(_MISSING_ROOTS),
    reason="needs sibling ecosystem checkouts; unresolved: %s"
           % ", ".join(_MISSING_ROOTS))


def _write_pins(tmp_path, ref, coords_um):
    """Pin-list sidecar with explicit pad centers (um), one pin per center."""
    pins = []
    for i, (x_um, y_um) in enumerate(coords_um):
        pins.append({
            "name": "p%d" % i, "type": "passive", "pad_index": i,
            "center_x_dbu": x_um * 1000.0, "center_y_dbu": y_um * 1000.0,
            "width_dbu": 60000.0, "height_dbu": 60000.0,
        })
    p = tmp_path / ("%s_pins.json" % ref)
    p.write_text(json.dumps(
        {"version": 1, "chiplet_name": ref, "dbu_um": 0.001, "pins": pins}))
    return str(p)


def _convert(tmp_path, monkeypatch, pad_locations, name="mixed", **kwargs):
    monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
    monkeypatch.delenv("INTERCONNECT_PDK_ROOT", raising=False)
    hyp = tmp_path / ("%s.hyp" % name)
    hyp.write_text(MIXED_HYP)
    out = tmp_path / ("%s_interposer.gds" % name)
    ok = h.convert_hyp_to_gds(
        hyp_path=str(hyp), output_path=str(out),
        lyp_path=h._find_default_lyp(),
        pad_locations=pad_locations, **kwargs)
    assert ok is True
    return out


def _manifest(out):
    return json.loads(out.with_name(out.stem + ".pillars.json").read_text())


def _drawn_centers(gds_path, ref):
    """Sorted (x_um, y_um) of every pillar instance under CUPILLARS_<ref>,
    rebased into the canonical GDS-bbox-corner frame (the manifest frame):
    raw instance displacement minus the top-cell bbox lower-left corner."""
    from klayout import db
    layout = db.Layout()
    layout.read(str(gds_path))
    bbox = layout.top_cell().dbbox()
    cell = None
    for ci in range(layout.cells()):
        if layout.cell(ci).name == "CUPILLARS_%s" % ref:
            cell = layout.cell(ci)
            break
    assert cell is not None, "CUPILLARS_%s not in %s" % (ref, gds_path)
    return sorted((round(inst.dtrans.disp.x - bbox.left, 6),
                   round(inst.dtrans.disp.y - bbox.bottom, 6))
                  for inst in cell.each_inst())


# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

def test_manifest_constants_pinned():
    """Readers exact-match these strings; bump both sides together."""
    assert h.PILLAR_MANIFEST_SCHEMA == "adk-pillar-manifest"
    assert h.PILLAR_MANIFEST_VERSION == "1.0.0"


# ---------------------------------------------------------------------------
# Bump-path runs write the manifest; other runs do not
# ---------------------------------------------------------------------------

def test_bump_path_writes_manifest_with_header(tmp_path, monkeypatch):
    out = _convert(
        tmp_path, monkeypatch,
        {"U1": _write_pin_list(tmp_path, "U1"),
         "U2": _write_pin_list(tmp_path, "U2")},
        connection_type="cupillar_opt1")
    m = _manifest(out)
    assert m["schema"] == "adk-pillar-manifest"
    assert m["version"] == "1.0.0"
    assert m["generator"] == "hyp_to_gds.py"
    assert m["assembly_gds"] == out.name
    assert m["units"] == "um"
    assert len(m["pillars"]) == 4  # 2 devices x 2 pins
    for p in m["pillars"]:
        assert p["method"] == "cupillar_opt1"
        assert p["diameter_um"] == 44
        assert p["moved_by_auto_resolve"] is False
        assert "auto_resolve_shift_um" not in p  # only moved bumps carry it
    # U1 at (200, -500) with pins at x=+-100, in the canonical frame:
    # the board outline spans (0, -1000)..(2000, 0), so the bbox corner
    # (0, -1000) rebases the drawn centers to y=+500. Absolute sanity.
    u1 = [(p["x_um"], p["y_um"]) for p in m["pillars"]
          if p["device_ref"] == "U1"]
    assert sorted(u1) == [(100.0, 500.0), (300.0, 500.0)]


def test_no_bump_path_writes_no_manifest(tmp_path, monkeypatch):
    """A conversion without any connection stack never emits the sidecar."""
    out = _convert(tmp_path, monkeypatch, None, name="plain")
    assert not out.with_name(out.stem + ".pillars.json").exists()


def test_bump_path_with_zero_bumps_writes_empty_array(tmp_path, monkeypatch):
    """The bump path ran (a method resolved) but the only device is unknown
    to the HYP, so nothing is placed: manifest present, pillars empty."""
    out = _convert(
        tmp_path, monkeypatch,
        {"U9": _write_pins(tmp_path, "U9", [(-100.0, 0.0)])},
        name="empty", connection_type="cupillar_opt1")
    m = _manifest(out)
    assert m["pillars"] == []
    assert m["schema"] == "adk-pillar-manifest"


def test_unresolvable_methods_still_write_empty_manifest(tmp_path, monkeypatch):
    """Connections were requested but NO method resolves body geometry in
    the interconnect manifest: the bump path was still entered, so the
    manifest must exist with an empty pillars array. Consumers rely on the
    distinction between "requested but nothing drawn" (empty array) and
    "bump path never ran" (no sidecar at all)."""
    out = _convert(
        tmp_path, monkeypatch,
        {"U1": _write_pin_list(tmp_path, "U1")},
        name="unresolved", connection_type="no_such_method")
    m = _manifest(out)
    assert m["pillars"] == []
    assert m["schema"] == "adk-pillar-manifest"


# ---------------------------------------------------------------------------
# Positions are the as-drawn ones (GDS instances), method travels per die
# ---------------------------------------------------------------------------

def test_positions_match_drawn_instances(tmp_path, monkeypatch):
    """Manifest x/y equal the pillar instance displacements in the GDS,
    per device, in the same frame."""
    out = _convert(
        tmp_path, monkeypatch,
        {"U1": _write_pin_list(tmp_path, "U1"),
         "U2": _write_pin_list(tmp_path, "U2")},
        connection_type="cupillar_opt1",
        die_connections={"U2": "vendorx_microbump"})
    m = _manifest(out)
    for ref in ("U1", "U2"):
        recorded = sorted((p["x_um"], p["y_um"]) for p in m["pillars"]
                          if p["device_ref"] == ref)
        assert recorded == _drawn_centers(out, ref)
    # Per-die method + its body diameter travel into the records.
    methods = {p["device_ref"]: (p["method"], p["diameter_um"])
               for p in m["pillars"]}
    assert methods["U1"] == ("cupillar_opt1", 44)
    assert methods["U2"] == ("vendorx_microbump", 40)


def test_auto_resolved_bump_recorded_at_moved_position(tmp_path, monkeypatch):
    """Two U1 pads 60 um apart violate the opt1 separation (75 um):
    auto-resolve pushes them to +-37.5 um around the midpoint. The manifest
    must carry the moved (as-drawn) centers and flag them."""
    out = _convert(
        tmp_path, monkeypatch,
        {"U1": _write_pins(tmp_path, "U1", [(-30.0, 0.0), (30.0, 0.0)])},
        name="moved", connection_type="cupillar_opt1")
    m = _manifest(out)
    assert len(m["pillars"]) == 2
    recorded = sorted((p["x_um"], p["y_um"]) for p in m["pillars"])
    # As-drawn == manifest, and NOT the pre-resolve pad positions
    # (canonical frame: outline bbox corner (0, -1000) rebases y to +500).
    assert recorded == _drawn_centers(out, "U1")
    assert recorded != [(170.0, 500.0), (230.0, 500.0)]
    assert recorded == [(162.5, 500.0), (237.5, 500.0)]
    assert all(p["moved_by_auto_resolve"] is True for p in m["pillars"])
    # Moved bumps record the shift magnitude (+-30 -> +-37.5 = 7.5 um each)
    # so consumers can bound the expected deviation, not just excuse it.
    for p in m["pillars"]:
        assert p["auto_resolve_shift_um"] == pytest.approx(7.5, abs=1e-6)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_pillars_sorted_by_device_then_pin(tmp_path, monkeypatch):
    """Ordering is (device_ref, pin_name), independent of input dict order."""
    out = _convert(
        tmp_path, monkeypatch,
        {"U2": _write_pin_list(tmp_path, "U2"),   # U2 first on purpose
         "U1": _write_pin_list(tmp_path, "U1")},
        connection_type="cupillar_opt1")
    m = _manifest(out)
    keys = [(p["device_ref"], p["pin_name"]) for p in m["pillars"]]
    assert keys == sorted(keys)
    assert keys[0][0] == "U1"


def test_complete_manifest_reuses_interposer_frame_origin(tmp_path):
    """The frame origin is captured at the first write (the interposer GDS,
    before chiplet artwork is merged) and reused for the complete-GDS
    manifest: merged instances that grow the top-cell bbox toward the lower
    left must NOT shift manifest positions. Both sidecars carry identical
    pillars; assembly_gds names each GDS."""
    gen = h.GDSGenerator(
        h.LayerMap(h._find_default_lyp()), "TOP", "METRIC", [], None)

    def _pad(ref, x, y):
        return {"ref": ref, "io_class": "wire_bond", "x_um": float(x),
                "y_um": float(y), "size_x_um": 100.0, "size_y_um": 100.0,
                "net": "N"}

    io1 = tmp_path / "io1.json"
    io1.write_text(json.dumps({"io_pads": [_pad("J1", 0, 0)]}))
    gen.add_io_pads(str(io1))
    gen.record_pillars([{
        "device_ref": "U1", "pin_name": "p0", "method": "cupillar_opt1",
        "x_um": 100.0, "y_um": 50.0, "diameter_um": 44,
        "moved_by_auto_resolve": False,
    }])
    interposer = tmp_path / "frame_interposer.gds"
    gen.write(str(interposer))
    m1 = _manifest(interposer)
    # J1 spans (-50,-50)..(50,50): that corner rebases (100,50) to (150,100).
    assert (m1["pillars"][0]["x_um"], m1["pillars"][0]["y_um"]) == (150.0,
                                                                    100.0)
    # Grow the bbox toward the lower left, as merged chiplet artwork could.
    io2 = tmp_path / "io2.json"
    io2.write_text(json.dumps({"io_pads": [_pad("J2", -500, -500)]}))
    gen.add_io_pads(str(io2))
    complete = tmp_path / "frame_complete.gds"
    gen.write(str(complete))
    m2 = _manifest(complete)
    assert m1["assembly_gds"] == "frame_interposer.gds"
    assert m2["assembly_gds"] == "frame_complete.gds"
    assert m2["pillars"] == m1["pillars"]  # origin reused, positions stable


def test_manifest_bytes_deterministic(tmp_path, monkeypatch):
    """Two identical runs produce byte-identical manifests (with a trailing
    newline)."""
    texts = []
    for sub in ("a", "b"):
        d = tmp_path / sub
        d.mkdir()
        out = _convert(
            d, monkeypatch,
            {"U1": _write_pin_list(d, "U1"),
             "U2": _write_pin_list(d, "U2")},
            connection_type="cupillar_opt1")
        texts.append(out.with_name(out.stem + ".pillars.json").read_text())
    assert texts[0] == texts[1]
    assert texts[0].endswith("\n")
