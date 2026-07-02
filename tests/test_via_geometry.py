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

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402

# METRIC units in HYP are meters.
UM = 1e-6

LYP_PATH = Path(os.environ.get("INTERPOSER_LYP", h._find_default_lyp()))


@pytest.fixture(scope="session", autouse=True)
def _require_lyp():
    # The via-geometry tests build a LayerMap from the interposer .lyp; skip
    # when no PDK resolves (bare CI runner) instead of exiting 1 inside the
    # worker. The adk-tools gate has the PDK baked and runs them for real.
    if not LYP_PATH.exists():
        pytest.skip(f"interposer LYP not found: {LYP_PATH}")


def _make_generator(tech_json_path=None):
    return h.GDSGenerator(h.LayerMap(str(LYP_PATH)), "TOP", "METRIC", [],
                          tech_json_path)


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
