# SPDX-License-Identifier: GPL-3.0-or-later
"""
Via geometry: SG13G2 PCell bootstrap + JSON-honoring rectangle fallback.

PCell half (anti-rot gate): whenever an SG13G2 PDK is discoverable
($PDK_ROOT or a sibling IHP-Open-PDK checkout) and the PCell python
deps are importable, the via_stack PCell path MUST come up. A silent
fall back to rectangles inside the adk-tools image -- where the PDK
slice is baked and PDK_ROOT is set -- is a regression, not a skip.

Fallback half: _create_simple_via must honor PDK_VIA_PARAMS (loaded
from interposer_tech_default.json, or the sg13g2 defaults) instead of
the historical hardcodes (0.45/1.2/2.0 cuts + 0.5 enclosure): n x n
arrays of PDK-sized cuts, landing pads sized array + 2*enclosure on
every metal of the span.

Runs on host (no pcbnew needed); requires the klayout module.
"""

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("klayout.db")
import klayout.db as db  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402

# METRIC units in HYP are meters.
UM = 1e-6

LYP_PATH = Path(os.environ.get("INTERPOSER_LYP", h._find_default_lyp()))
# Every case draws with the interposer PDK's .lyp, a sibling tree.
pytestmark = pytest.mark.needs_sibling_sources


@pytest.fixture(scope="session", autouse=True)
def _require_lyp():
    # The via-geometry tests build a LayerMap from the interposer .lyp; skip
    # when no PDK resolves (bare CI runner) instead of exiting 1 inside the
    # worker. The adk-tools gate has the PDK baked and runs them for real.
    if not LYP_PATH.exists():
        pytest.skip(f"interposer LYP not found: {LYP_PATH}")


def _make_generator(tech_json_path=None, units="METRIC", stackup=()):
    return h.GDSGenerator(h.LayerMap(str(LYP_PATH)), "TOP", units,
                          list(stackup), tech_json_path)


def _via_and_padstack(pad_um, layers):
    padstack = h.Padstack(index=1, drill=0.0, layers=list(layers),
                          pad_width=pad_um * UM, pad_height=pad_um * UM)
    via = h.Via(net_name="N1", x=0.0, y=0.0, padstack_index=1)
    return via, padstack


def _layer_boxes(gen, layer_name):
    idx = gen._get_gds_layer(layer_name)
    return [s.dbbox() for s in gen.routing_cell.shapes(idx).each()]


# ---------------------------------------------------------------------------
# Rectangle fallback honors PDK_VIA_PARAMS
# ---------------------------------------------------------------------------

def test_fallback_via_array_honors_tech_json(tmp_path):
    """Cut size, array count and pad enclosure all come from the JSON."""
    tech = tmp_path / "tech.json"
    tech.write_text(json.dumps({"rules": {
        "Vn_a": 0.3, "Vn_b": 0.3, "Vn_c1": 0.1,
        "TV1_a": 0.5, "TV1_b": 0.5, "TV1_d": 0.2,
        "TV2_a": 1.0, "TV2_b": 1.0, "TV2_d": 0.3,
    }}))
    gen = _make_generator(str(tech))
    via, padstack = _via_and_padstack(2.0, ("Metal4", "Metal5"))
    assert gen._create_simple_via(via, padstack) is True

    # n = ceil((2.0 - 2*0.1 + 0.3) / (0.3 + 0.3)) = 4 -> 16 cuts of 0.3
    cuts = _layer_boxes(gen, "Via4")
    assert len(cuts) == 16
    for cut in cuts:
        assert cut.width() == pytest.approx(0.3, abs=1e-6)
        assert cut.height() == pytest.approx(0.3, abs=1e-6)

    # extent = 4*0.3 + 3*0.3 = 2.1; pad side = extent + 2*enc = 2.3
    for metal in ("Metal4", "Metal5"):
        pads = _layer_boxes(gen, metal)
        assert len(pads) == 1
        assert pads[0].width() == pytest.approx(2.3, abs=1e-6)
        assert pads[0].height() == pytest.approx(2.3, abs=1e-6)


def test_fallback_uses_pdk_defaults_not_legacy_hardcodes():
    """Without a tech JSON the sg13g2 defaults apply (0.19 Vn cuts),
    not the historical 0.45/0.5-enclosure rectangles."""
    gen = _make_generator(None)
    via, padstack = _via_and_padstack(1.0, ("Metal4", "Metal5"))
    assert gen._create_simple_via(via, padstack) is True

    # Vn: size .19 sep .22 enc .05 -> n = ceil((1.0-0.1+0.22)/0.41) = 3
    cuts = _layer_boxes(gen, "Via4")
    assert len(cuts) == 9
    for cut in cuts:
        assert cut.width() == pytest.approx(0.19, abs=1e-6)

    # extent = 3*0.19 + 2*0.22 = 1.01; pad side = 1.01 + 2*0.05 = 1.11
    pads = _layer_boxes(gen, "Metal4")
    assert len(pads) == 1
    assert pads[0].width() == pytest.approx(1.11, abs=1e-6)


def test_fallback_spans_draw_intermediate_landing_pads():
    """A Metal5..TopMetal2 padstack gets TopVia1+TopVia2 arrays and a
    TopMetal1 landing pad even though TopMetal1 is not listed."""
    gen = _make_generator(None)
    via, padstack = _via_and_padstack(5.0, ("Metal5", "TopMetal2"))
    assert gen._create_simple_via(via, padstack) is True

    assert _layer_boxes(gen, "TopVia1"), "TopVia1 array missing"
    assert _layer_boxes(gen, "TopVia2"), "TopVia2 array missing"
    assert len(_layer_boxes(gen, "TopMetal1")) == 1, \
        "intermediate TopMetal1 landing pad missing (floating cuts)"


# ---------------------------------------------------------------------------
# SG13G2 PCell bootstrap
# ---------------------------------------------------------------------------

def _sg13g2_klayout_dir():
    root = h._discover_path_var("PDK_ROOT")
    if not root:
        return None
    cand = Path(root).joinpath(*h.GDSGenerator._SG13G2_KLAYOUT_SUBPATH)
    return cand if cand.is_dir() else None


def _missing_pcell_deps():
    missing = []
    for mod in ("tkinter", "psutil"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    return missing


_PDK_KLAYOUT = _sg13g2_klayout_dir()
_MISSING_DEPS = _missing_pcell_deps()


@pytest.mark.skipif(_PDK_KLAYOUT is None,
                    reason="No SG13G2 PDK discoverable (set PDK_ROOT or "
                           "keep a sibling IHP-Open-PDK checkout)")
@pytest.mark.skipif(bool(_MISSING_DEPS),
                    reason="PCell python deps missing: %s" % _MISSING_DEPS)
def test_pcells_bootstrap_with_pdk_present():
    """With a discoverable PDK the PCell path MUST be active (the
    adk-tools image bakes the PDK slice; falling back silently there
    is the regression this test exists to catch)."""
    gen = _make_generator(None)
    assert gen._pcells_available, \
        "SG13G2 PCell bootstrap failed although %s exists" % _PDK_KLAYOUT

    # Field acceptance: a real via_stack instantiates and draws geometry.
    cell = gen.layout.create_cell("via_stack", "SG13_dev", {
        "b_layer": "Metal4",
        "t_layer": "Metal5",
        "vn_columns": 2,
        "vn_rows": 2,
    })
    assert cell is not None
    assert not cell.dbbox().empty(), "via_stack PCell produced no geometry"


def test_bootstrap_quiet_false_without_pdk(monkeypatch):
    """No PDK discoverable -> False, no exception, no noise."""
    gen = _make_generator(None)
    monkeypatch.setattr(h, "_discover_path_var", lambda name: None)
    assert gen._bootstrap_sg13g2_pcells() is False


# ---------------------------------------------------------------------------
# Via count at the landing: integer dbu, not a float ceil
# ---------------------------------------------------------------------------

# The span as the HYP's STACKUP gives it in the flow; the PCell path orders
# the via layers by it.
_TOP_SPAN = ("TopMetal2", "TopMetal1")

# HYP length unit per UNITS value, in um.
_UNIT_UM = {"METRIC": h.GDSGenerator.METER_TO_UM,
            "ENGLISH": h.GDSGenerator.INCH_TO_UM}


def _tv2_landing_um(gen, k):
    """The landing a k x k TopVia2 array draws: the top of k's interval."""
    p = gen.PDK_VIA_PARAMS["TV2"]
    return k * (p["size"] + p["sep"]) - p["sep"] + 2 * p["enc"]


def _cut_boxes(gen, layer_name):
    """Every shape of the layer under the top cell, PCell instances included."""
    idx = gen._get_gds_layer(layer_name)
    return list(db.Region(gen.top_cell.begin_shapes_rec(idx)).each())


def _via_generator(pcells, units="METRIC"):
    gen = _make_generator(None, units, _TOP_SPAN)
    if pcells and not gen._pcells_available:
        pytest.skip("SG13_dev via_stack PCell not available")
    gen._pcells_available = pcells
    return gen


@pytest.mark.parametrize("units", ["METRIC", "ENGLISH"])
@pytest.mark.parametrize("k", [1, 3, 10])
def test_via_count_at_landing_and_one_dbu_above(units, k):
    """A pad equal to the k x k landing gives k; one dbu more gives k+1,
    whichever unit the HYP length arrives in."""
    gen = _make_generator(None, units)
    landing = _tv2_landing_um(gen, k)
    for pad_um, want in ((landing, k), (landing + gen.layout.dbu, k + 1)):
        target = gen._to_um(pad_um / _UNIT_UM[units])
        assert gen._calculate_via_array(target, "TV2") == want


@pytest.mark.parametrize("pcells", [False, True])
def test_landing_pad_draws_its_own_array(pcells):
    """A 3x3 landing draws 9 TopVia2 cuts on either via path."""
    gen = _via_generator(pcells)
    via, padstack = _via_and_padstack(_tv2_landing_um(gen, 3), _TOP_SPAN)
    assert gen.add_via(via, padstack) is True
    assert len(_cut_boxes(gen, "TopVia2")) == 9


# A dieless HYP with a TopMetal2-TopMetal1 padstack of a 1.9 um pad and a
# 0.9 um drill, METRIC: the one-cut via KiCad writes for a TopVia2.
_SIGNAL_VIA_HYP = (
    "{VERSION=2.14}\n{UNITS=METRIC LENGTH}\n\n"
    '{BOARD "board.kicad_pcb"\n'
    "  (PERIMETER_SEGMENT X1=0.000100000 Y1=0.000000000 X2=0.000000000 Y2=0.000000000)\n"
    "  (PERIMETER_SEGMENT X1=0.000000000 Y1=0.000000000 X2=0.000000000 Y2=0.000100000)\n"
    "  (PERIMETER_SEGMENT X1=0.000000000 Y1=0.000100000 X2=0.000100000 Y2=0.000100000)\n"
    "  (PERIMETER_SEGMENT X1=0.000100000 Y1=0.000100000 X2=0.000100000 Y2=0.000000000)\n"
    "}\n\n"
    "{PADSTACK=2, 0.000000900\n"
    '  ("TopMetal2", 0, 0.000001900, 0.000001900, 0.0, M)\n'
    '  ("TopMetal1", 0, 0.000001900, 0.000001900, 0.0, M)\n'
    "}\n\n"
    '{NET="N1"\n'
    "  (VIA X=0.0000200000 Y=0.0000200000 P=2)\n"
    "  (VIA X=0.0000400000 Y=0.0000200000 P=2)\n"
    "  (VIA X=0.0000600000 Y=0.0000409280 P=2)\n"
    "}\n"
)


@pytest.mark.parametrize("pcells", [False, True])
def test_signal_via_hyp_draws_one_cut_per_via(tmp_path, pcells):
    """Each 1.9 um one-cut via stays one 0.9 um TopVia2 cut."""
    hyp = tmp_path / "signal_vias.hyp"
    hyp.write_text(_SIGNAL_VIA_HYP)
    parser = h.HYPParser(str(hyp))
    parser.parse()
    assert len(parser.vias) == 3
    gen = _via_generator(pcells, parser.units)
    for via in parser.vias:
        assert gen.add_via(via, parser.padstacks[via.padstack_index]) is True
    cuts = _cut_boxes(gen, "TopVia2")
    assert len(cuts) == 3
    assert {c.bbox().width() for c in cuts} == {round(0.9 / gen.layout.dbu)}
