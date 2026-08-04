# SPDX-License-Identifier: GPL-3.0-or-later
"""Board mutation: what survives replacing a placed footprint (needs pcbnew).

This is the only part of the plugin that touches the user's board, and every
loss it can cause is silent until much later: a dropped KIID path only shows
up at the next "Update PCB from Schematic", a dropped Sim.* model only when
someone runs a simulation. So the carry-over is pinned here against real
pcbnew rather than a stand-in.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

pcbnew = pytest.importorskip("pcbnew")

from resizer_passive_elements import apply_resize, paths  # noqa: E402

TECH_JSON = paths.discover_tech_json_path()
GEN_SCRIPT = paths.discover_footprint_gen_path()

pytestmark = pytest.mark.skipif(
    not (TECH_JSON and GEN_SCRIPT),
    reason="interposer PDK checkout not resolvable; cannot generate footprints",
)

CARRIED_OVER = {
    "Datasheet": "https://example.invalid/cap_cmim.pdf",
    "Sim.Device": "SUBCKT",
    "Sim.Library": "${INTERPOSER_PDK_ROOT}/libs.tech/ngspice/models/cornerCAP.lib",
    "Sim.Params": "w={w} l={l} m={m}",
    "Copyright": "Copyright 2026 IHP PDK Authors",
}


@pytest.fixture(scope="module")
def tech():
    return apply_resize.load_tech(TECH_JSON, GEN_SCRIPT)


@pytest.fixture
def generated(tmp_path, tech):
    """A freshly generated CMIM_100fF .kicad_mod to swap in."""
    return apply_resize.generate_footprint_file(
        {"reference": "C1", "model": "cap_cmim", "w_um": 8.11, "l_um": 8.11,
         "capacitance_fF": 100.0, "nominal_fF": 100.0},
        tech, str(tmp_path / "out.pretty"), gen_script_path=GEN_SCRIPT)


def _placed_instance(board, source_mod, extra_fields):
    """Load `source_mod` onto `board` and dress it like a schematic-driven part."""
    fp = pcbnew.FootprintLoad(str(Path(source_mod).parent),
                              Path(source_mod).stem)
    assert fp is not None
    fp.SetReference("C1")
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(20)))
    for name, value in extra_fields.items():
        fp.SetField(name, value)
    board.Add(fp)
    return fp


def _fields(footprint):
    return {f.GetName(): f for f in footprint.GetFields()}


def test_the_schematic_link_and_the_symbol_fields_survive(tmp_path, generated):
    board = pcbnew.BOARD()
    old = _placed_instance(board, generated, dict(
        CARRIED_OVER, Model="cap_cmim", w="8.11e-06", l="8.11e-06", m="1"))
    old.SetPath(pcbnew.KIID_PATH("/00000000-0000-0000-0000-000000000001"))
    old.SetLocked(True)
    old_path = old.GetPath().AsString()

    ok = apply_resize.apply_to_instance(
        board, old, generated,
        params={"model": "cap_cmim", "w_um": 8.11, "l_um": 8.11,
                "capacitance_fF": 99.95575, "nominal_fF": 100.0})

    assert ok
    new = list(board.Footprints())[0]
    # Without the path the next "Update PCB from Schematic" no longer
    # recognises this footprint as the symbol's and re-adds the symbol.
    assert new.GetPath().AsString() == old_path
    assert new.IsLocked()
    fields = _fields(new)
    for name, value in CARRIED_OVER.items():
        assert name in fields, "%s was dropped" % name
        assert fields[name].GetText() == value, name


def test_the_plugin_owns_its_own_fields(tmp_path, generated):
    board = pcbnew.BOARD()
    # Stale technology values on the instance being replaced: the plugin must
    # overwrite these, not carry them over.
    _placed_instance(board, generated, {
        "Model": "cap_cmim", "w": "9.99e-06", "l": "9.99e-06", "m": "7",
        "Capacitance": "999fF", "Nominal": "1pF"})
    old = list(board.Footprints())[0]

    assert apply_resize.apply_to_instance(
        board, old, generated,
        params={"model": "cap_cmim", "w_um": 8.11, "l_um": 8.11,
                "capacitance_fF": 99.95575, "nominal_fF": 100.0})

    fields = _fields(list(board.Footprints())[0])
    assert fields["w"].GetText() == "8.11e-06"
    assert fields["l"].GetText() == "8.11e-06"
    assert fields["m"].GetText() == "7"          # m is the user's, preserved
    assert fields["Capacitance"].GetText().startswith("99.95575")
    assert fields["Nominal"].GetText() == "100fF"


def test_a_stale_nominal_is_cleared_rather_than_left_lying(tmp_path, generated):
    board = pcbnew.BOARD()
    _placed_instance(board, generated, {"Model": "cap_cmim",
                                        "Nominal": "100fF"})
    old = list(board.Footprints())[0]

    # nominal_fF None is what _generate_cap_cmim_footprint reports once w/l no
    # longer match the label.
    assert apply_resize.apply_to_instance(
        board, old, generated,
        params={"model": "cap_cmim", "w_um": 10.0, "l_um": 10.0,
                "capacitance_fF": 151.6, "nominal_fF": None})

    assert _fields(list(board.Footprints())[0])["Nominal"].GetText() == ""


def test_a_field_the_user_had_visible_stays_visible(tmp_path, generated):
    board = pcbnew.BOARD()
    _placed_instance(board, generated, {"Model": "cap_cmim",
                                        "Note": "keep me visible"})
    old = list(board.Footprints())[0]
    _fields(old)["Note"].SetVisible(True)

    assert apply_resize.apply_to_instance(
        board, old, generated,
        params={"model": "cap_cmim", "w_um": 8.11, "l_um": 8.11})

    note = _fields(list(board.Footprints())[0])["Note"]
    assert note.GetText() == "keep me visible"
    assert note.IsVisible(), "a visible custom field came back hidden"


def test_nets_follow_the_pad_number_and_placement_is_kept(tmp_path, generated):
    board = pcbnew.BOARD()
    _placed_instance(board, generated, {"Model": "cap_cmim"})
    old = list(board.Footprints())[0]
    old.SetOrientationDegrees(90.0)
    position = old.GetPosition()
    for number, net_name in (("1", "VDD"), ("2", "GND")):
        net = pcbnew.NETINFO_ITEM(board, net_name)
        board.Add(net)
        for pad in old.Pads():
            if pad.GetNumber() == number:
                pad.SetNet(net)

    assert apply_resize.apply_to_instance(
        board, old, generated,
        params={"model": "cap_cmim", "w_um": 8.11, "l_um": 8.11})

    new = list(board.Footprints())[0]
    assert new.GetPosition() == position
    assert new.GetOrientationDegrees() == pytest.approx(90.0)
    nets = {pad.GetNumber(): pad.GetNetname() for pad in new.Pads()}
    assert nets == {"1": "VDD", "2": "GND"}
