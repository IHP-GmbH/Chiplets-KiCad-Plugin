# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the cap_cmim path: board extraction, sidecar, PCell placement.

Two board conventions for the w/l fields genuinely occur and both must land
on the same geometry (see chiplet_writer.parse_length_um):

- ``57.68um`` / ``57.68u`` -- stamped by the PDK's own cmim_footprint_gen.py
  into every intm4tm2.pretty footprint.
- ``5.768e-5`` -- the cap_cmim symbol's metres, copied onto the footprint by
  "Update PCB from Schematic".

The placement assertions pin the frame: the footprint position is the centre
of the MIM plate, while the PCell draws that plate from its own origin, so the
instance sits at the plate's lower-left corner.
"""
import json
import os
import sys
from pathlib import Path

import klayout.db as db
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import hyp_to_gds as _h  # noqa: E402
from hyp_to_gds import (  # noqa: E402
    GDSGenerator,
    LayerMap,
    _cmim_length_um,
    _parse_length_um,
    load_cmim_devices,
)

LYP_PATH = Path(os.environ.get("INTERPOSER_LYP", _h._find_default_lyp()))

# 5 pF part from intm4tm2.pretty: a 57.68 um plate, with the Metal5 bottom
# plate overhanging by Mim_c = 0.6 um on every side.
W_UM = 57.68
PLATE_MARGIN_UM = 0.6
BBOX_UM = W_UM + 2 * PLATE_MARGIN_UM

needs_pcells = pytest.mark.skipif(
    _h._discover_path_var("INTERPOSER_PDK_ROOT") is None,
    reason="interposer PDK not resolvable; cannot create IntM4TM2 PCells",
)


@pytest.fixture(scope="session", autouse=True)
def _require_lyp():
    if not LYP_PATH.exists():
        pytest.skip("interposer LYP not found: %s" % LYP_PATH)


def _make_generator() -> GDSGenerator:
    return GDSGenerator(LayerMap(str(LYP_PATH)), "TOP", "METRIC", [], None)


def _sidecar(tmp_path, entry, **top):
    payload = {"version": 2, "cmim_devices": [dict(ref="C1", **entry)]}
    payload.update(top)
    path = tmp_path / "cmim_devices.json"
    path.write_text(json.dumps(payload))
    return path


# ---------------------------------------------------------------------------
# Unit parsing (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (57.68, 57.68),            # v2 sidecar: already micrometers
    ("57.68um", 57.68),        # PDK footprint library
    ("57.68u", 57.68),         # short suffix, as the PCell itself accepts
    ("  8.11UM ", 8.11),       # tolerant of case and padding
    ("5.768e-5", 57.68),       # symbol library: bare number means metres
    ("0", 0.0),
])
def test_parse_length_um_accepts_every_board_convention(value, expected):
    assert _parse_length_um(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["", "   ", "abc", "57,68um", None, True])
def test_parse_length_um_rejects_junk(value):
    assert _parse_length_um(value) is None


def test_cmim_length_um_prefers_the_v2_key():
    assert _cmim_length_um({"w_um": 12.5, "w": "999um"}, "w") == 12.5


def test_cmim_length_um_falls_back_to_the_legacy_key():
    assert _cmim_length_um({"w": "12.5um"}, "w") == 12.5


def test_cmim_length_um_raises_on_missing_and_unparseable():
    with pytest.raises(KeyError):
        _cmim_length_um({"l_um": 1.0}, "w")
    with pytest.raises(ValueError):
        _cmim_length_um({"w_um": "wide"}, "w")


# ---------------------------------------------------------------------------
# Sidecar loading
# ---------------------------------------------------------------------------


def test_load_cmim_devices_reads_the_entries(tmp_path):
    path = _sidecar(tmp_path, {"x_um": 0.0, "y_um": 0.0, "w_um": 1.0,
                               "l_um": 1.0, "m": 1})
    assert [d["ref"] for d in load_cmim_devices(str(path))] == ["C1"]


def test_an_unreadable_sidecar_is_not_an_empty_one(tmp_path):
    # None means "asked for a sidecar and could not read it", which the caller
    # must fail on. [] means "read it, this board declares no capacitors".
    assert load_cmim_devices(str(tmp_path / "nope.json")) is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert load_cmim_devices(str(broken)) is None
    listy = tmp_path / "list.json"
    listy.write_text(json.dumps([1, 2]))
    assert load_cmim_devices(str(listy)) is None
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"version": 2, "cmim_devices": []}))
    assert load_cmim_devices(str(empty)) == []


def test_a_legacy_key_means_metres_whether_quoted_or_not():
    # Under the v1 `w` key the board convention applies to numbers too, or the
    # same value would differ by 1e6 on nothing but JSON quoting.
    assert _cmim_length_um({"w": 5.768e-5}, "w") == pytest.approx(57.68)
    assert _cmim_length_um({"w": "5.768e-5"}, "w") == pytest.approx(57.68)
    # Under the v2 key it is micrometres, quoted or not.
    assert _cmim_length_um({"w_um": 57.68}, "w") == pytest.approx(57.68)
    assert _cmim_length_um({"w_um": "57.68um"}, "w") == pytest.approx(57.68)


# ---------------------------------------------------------------------------
# PCell placement
# ---------------------------------------------------------------------------


@needs_pcells
@pytest.mark.parametrize("entry", [
    {"w_um": W_UM, "l_um": W_UM, "m": 1},
    {"w": "57.68um", "l": "57.68um", "m": "1"},
    {"w": "57.68u", "l": "57.68u", "m": "1"},
    {"w": "5.768e-5", "l": "5.768e-5", "m": "1"},
], ids=["v2_um", "library_um_suffix", "short_u_suffix", "symbol_metres"])
def test_every_field_convention_lands_on_the_same_plate(tmp_path, entry):
    gen = _make_generator()
    devices = load_cmim_devices(
        str(_sidecar(tmp_path, dict(x_um=1000.0, y_um=2000.0, **entry))))

    placed, unplaced = gen.add_cmim_devices(devices)

    assert (placed, unplaced) == (1, [])
    bbox = gen.layout.cell("CMIM_DEVICES").dbbox()
    # The footprint position is the plate centre, not the cell origin.
    assert (bbox.left + bbox.right) / 2 == pytest.approx(1000.0, abs=1e-6)
    assert (bbox.bottom + bbox.top) / 2 == pytest.approx(2000.0, abs=1e-6)
    assert bbox.width() == pytest.approx(BBOX_UM, abs=1e-6)
    assert bbox.height() == pytest.approx(BBOX_UM, abs=1e-6)


@needs_pcells
def test_instance_origin_is_snapped_to_the_technology_grid(tmp_path):
    gen = _make_generator()
    grid = gen._intm4tm2_tech()["grid_um"]
    assert grid > 0
    devices = load_cmim_devices(str(_sidecar(
        tmp_path,
        {"x_um": 1000.0013, "y_um": 2000.0, "w_um": W_UM, "l_um": W_UM,
         "m": 1})))

    assert gen.add_cmim_devices(devices)[0] == 1

    left = gen.layout.cell("CMIM_DEVICES").dbbox().left + PLATE_MARGIN_UM
    assert round(left / grid) * grid == pytest.approx(left, abs=1e-9)


@needs_pcells
@pytest.mark.parametrize("entry", [
    {"x_um": 0.0, "y_um": 0.0, "w_um": "wide", "l_um": W_UM},
    {"x_um": 0.0, "y_um": 0.0, "w_um": -1.0, "l_um": W_UM},
    {"x_um": 0.0, "y_um": 0.0, "w_um": 0.0, "l_um": W_UM},
    {"x_um": 0.0, "y_um": 0.0, "l_um": W_UM},
    {"x_um": 0.0, "y_um": 0.0, "w_um": W_UM, "l_um": W_UM, "m": 0},
], ids=["unparseable", "negative", "zero", "missing", "zero_multiplier"])
def test_unplaceable_devices_are_reported_not_swallowed(tmp_path, entry):
    gen = _make_generator()
    devices = load_cmim_devices(str(_sidecar(tmp_path, entry)))

    placed, unplaced = gen.add_cmim_devices(devices)

    # The caller fails the export on a non-empty unplaced list; a silent skip
    # would ship an interposer GDS missing a fabricated device.
    assert (placed, unplaced) == (0, ["C1"])


@needs_pcells
@pytest.mark.parametrize("w_um,l_um,why", [
    (100.0, 100.0, "15 pF, above the 8 pF device maximum"),
    (93.0, 93.0, "13 pF, just above the maximum"),
    (1.0, 1.0, "below cmim_minLW = 1.14 um"),
    (2000.0, 2000.0, "above cmim_maxLW = 1000 um"),
    (8.11e6, 8.11e6, "the metres/micrometres mix-up"),
    (float("inf"), 10.0, "not a finite number"),
], ids=["over_cmax", "just_over_cmax", "under_minlw", "over_maxlw",
        "metres_typo", "infinite"])
def test_a_plate_the_pcell_would_refuse_never_reaches_it(tmp_path, w_um,
                                                         l_um, why):
    """The PCell does not return None when it refuses its parameters.

    It hands back a nameless, empty cell, which the GDS writer emits as an
    empty STRNAME record and makes the whole file unreadable; and a plate
    large enough to pass its own coercion sends its via loop, which is
    O(w*l), into an unbounded allocation. Both are refused before the call.
    """
    gen = _make_generator()
    devices = load_cmim_devices(str(_sidecar(
        tmp_path, {"x_um": 0.0, "y_um": 0.0, "w_um": w_um, "l_um": l_um,
                   "m": 1})))

    assert gen.add_cmim_devices(devices) == (0, ["C1"]), why
    assert gen.layout.cell("CMIM_DEVICES") is None
    assert all(c.name for c in gen.layout.each_cell())


@needs_pcells
def test_an_in_spec_rectangle_is_not_rejected_by_the_bound(tmp_path):
    # The bound is on the capacitance, not on the side: 100 x 50 um is
    # 7512 fF, inside the 8 pF maximum, and must still be placed.
    gen = _make_generator()
    devices = load_cmim_devices(str(_sidecar(
        tmp_path, {"x_um": 0.0, "y_um": 0.0, "w_um": 100.0, "l_um": 50.0,
                   "m": 1})))

    assert gen.add_cmim_devices(devices) == (1, [])


@needs_pcells
def test_the_written_gds_reads_back(tmp_path):
    """An empty-named cell makes KLayout refuse the whole file."""
    gen = _make_generator()
    good = load_cmim_devices(str(_sidecar(
        tmp_path, {"x_um": 0.0, "y_um": 0.0, "w_um": W_UM, "l_um": W_UM,
                   "m": 1})))
    assert gen.add_cmim_devices(good) == (1, [])

    out = tmp_path / "interposer.gds"
    gen.write(str(out))

    db.Layout().read(str(out))


@needs_pcells
def test_a_rotated_rectangular_device_is_placed_rotated(tmp_path):
    gen = _make_generator()
    upright = load_cmim_devices(str(_sidecar(
        tmp_path, {"x_um": 1000.0, "y_um": 2000.0, "w_um": 20.0,
                   "l_um": 5.0, "m": 1})))
    assert gen.add_cmim_devices(upright) == (1, [])
    flat = gen.layout.cell("CMIM_DEVICES").dbbox()

    turned = _make_generator()
    rotated = load_cmim_devices(str(_sidecar(
        tmp_path, {"x_um": 1000.0, "y_um": 2000.0, "w_um": 20.0, "l_um": 5.0,
                   "m": 1, "rotation_deg": 90.0})))
    assert turned.add_cmim_devices(rotated) == (1, [])
    stood = turned.layout.cell("CMIM_DEVICES").dbbox()

    # Same plate, swapped extents, still centred on the footprint position.
    assert stood.width() == pytest.approx(flat.height(), abs=1e-6)
    assert stood.height() == pytest.approx(flat.width(), abs=1e-6)
    assert (stood.left + stood.right) / 2 == pytest.approx(1000.0, abs=1e-6)
    assert (stood.bottom + stood.top) / 2 == pytest.approx(2000.0, abs=1e-6)


@needs_pcells
def test_no_group_cell_is_left_behind_when_nothing_is_placed(tmp_path):
    gen = _make_generator()
    devices = load_cmim_devices(str(_sidecar(
        tmp_path, {"x_um": 0.0, "y_um": 0.0, "w_um": -1.0, "l_um": 1.0})))

    gen.add_cmim_devices(devices)

    assert gen.layout.cell("CMIM_DEVICES") is None


def test_add_cmim_devices_is_a_no_op_without_devices():
    assert _make_generator().add_cmim_devices([]) == (0, [])


@needs_pcells
def test_sg13g2_via_pcells_survive_the_intm4tm2_rebind(tmp_path):
    """Binding the layout to intm4tm2 drops the SG13_dev library proxies.

    The via cache therefore holds cell indices, not db.Cell handles: with
    handles, write() died in Cell.flatten with "Object has been destroyed
    already" and the export produced no GDS at all.
    """
    gen = _make_generator()
    if not gen._pcells_available:
        pytest.skip("SG13G2 PCells unavailable; no via cells to invalidate")
    padstack = _h.Padstack(index=0, drill=0.0, layers=["Metal5", "Metal4"],
                           pad_width=5e-6, pad_height=5e-6)
    created = gen._get_or_create_via_pcell(padstack)
    assert created is not None
    via_cell, group_cell = created
    group_cell.insert(db.DCellInstArray(via_cell, db.DTrans()))

    devices = load_cmim_devices(str(_sidecar(
        tmp_path, {"x_um": 1000.0, "y_um": 2000.0, "w_um": W_UM,
                   "l_um": W_UM, "m": 1})))
    assert gen.add_cmim_devices(devices) == (1, [])

    out = tmp_path / "interposer.gds"
    gen.write(str(out))

    written = db.Layout()
    written.read(str(out))
    names = [c.name for c in written.each_cell()]
    assert any(n.startswith("VIA_") for n in names), names
    assert "CMIM_DEVICES" in names

    # The names surviving is not the point: a rebind that kept the cells but
    # dropped their shapes would pass a name-only assertion. Pin the geometry
    # against the same run without any CMIM.
    def via_shapes(layout):
        cell = next(c for c in layout.each_cell()
                    if c.name.startswith("VIA_"))
        return sum(cell.shapes(i).size() for i in layout.layer_indexes())

    control = _make_generator()
    created = control._get_or_create_via_pcell(padstack)
    control_cell, control_group = created
    control_group.insert(db.DCellInstArray(control_cell, db.DTrans()))
    control_out = tmp_path / "control.gds"
    control.write(str(control_out))
    control_layout = db.Layout()
    control_layout.read(str(control_out))

    assert via_shapes(written) == via_shapes(control_layout) > 0
