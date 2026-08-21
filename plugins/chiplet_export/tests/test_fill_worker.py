# SPDX-License-Identifier: GPL-3.0-or-later
"""Worker-side tests for metal density fill (hyp_to_gds).

Covers the plugin's own responsibilities -- reading the no-fill sidecar,
painting the keep-out datatypes, and measuring coarse coverage -- WITHOUT the
interposer PDK fill engine, which is exercised separately (it needs the klayout
binary and the PDK tree). The keep-out datatype contract (160/0 global,
<metal>/23 per metal) is what the PDK generators subtract, so getting it right
here is what makes a KiCad-authored keep-out actually honored.
"""
import json
import os
import shutil
import sys
from pathlib import Path

import klayout.db as db
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import hyp_to_gds as _h  # noqa: E402
from hyp_to_gds import (  # noqa: E402
    GDSGenerator, LayerMap, load_nofill_regions,
    _compute_fill_coverage, _filler_layers, _insert_metal_fill,
)

LYP_PATH = Path(os.environ.get("INTERPOSER_LYP", _h._find_default_lyp() or ""))


@pytest.fixture
def lyp():
    if not LYP_PATH.exists():
        pytest.skip(f"interposer LYP not found: {LYP_PATH}")
    return str(LYP_PATH)


def _gen(lyp_path):
    return GDSGenerator(LayerMap(lyp_path), "INTERPOSER", "METRIC", [], None)


# --------------------------------------------------------------------------
# load_nofill_regions
# --------------------------------------------------------------------------

def test_load_nofill_regions_roundtrip(tmp_path):
    p = tmp_path / "nofill.json"
    p.write_text(json.dumps({"version": 1, "regions": [
        {"role": "global", "polygon_um": [[0, 0], [10, 0], [10, 10], [0, 10]]},
    ]}))
    regions = load_nofill_regions(str(p))
    assert regions is not None and len(regions) == 1
    assert regions[0]["role"] == "global"


def test_load_nofill_regions_missing_is_none(tmp_path):
    # Requested-but-unreadable must be distinguishable from "no keep-outs".
    assert load_nofill_regions(str(tmp_path / "absent.json")) is None


def test_load_nofill_regions_empty_is_empty_list(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"version": 1, "regions": []}))
    assert load_nofill_regions(str(p)) == []


# --------------------------------------------------------------------------
# role -> keep-out layer mapping and painting
# --------------------------------------------------------------------------

def test_nofill_target_mapping(lyp):
    gen = _gen(lyp)
    assert gen._nofill_target("global") == (160, 0)
    for role, metal in (("M4", "Metal4"), ("M5", "Metal5"),
                        ("TM1", "TopMetal1"), ("TM2", "TopMetal2")):
        num, _dt = gen.layer_map.get_layer(metal)
        assert gen._nofill_target(role) == (num, 23), role
    assert gen._nofill_target("bogus") is None


def test_add_nofill_regions_paints_keepouts(lyp, tmp_path):
    gen = _gen(lyp)
    m4_num, _ = gen.layer_map.get_layer("Metal4")
    n = gen.add_nofill_regions([
        {"role": "global", "polygon_um": [[0, 0], [50, 0], [50, 50], [0, 50]]},
        {"role": "M4", "polygon_um": [[60, 0], [110, 0], [110, 50], [60, 50]]},
        {"role": "bogus", "polygon_um": [[0, 0], [1, 0], [1, 1], [0, 1]]},
        {"role": "global", "polygon_um": [[0, 0], [1, 1]]},  # <3 pts -> skipped
    ])
    assert n == 2
    out = tmp_path / "keepouts.gds"
    gen.write(str(out))
    ly = db.Layout()
    ly.read(str(out))
    top = ly.top_cell()
    assert ly.find_layer(160, 0) is not None
    assert top.shapes(ly.layer(160, 0)).size() == 1
    assert ly.find_layer(m4_num, 23) is not None
    assert top.shapes(ly.layer(m4_num, 23)).size() == 1


def test_no_keepout_layers_without_regions(lyp, tmp_path):
    gen = _gen(lyp)
    out = tmp_path / "clean.gds"
    gen.write(str(out))
    ly = db.Layout()
    ly.read(str(out))
    assert ly.find_layer(160, 0) is None


# --------------------------------------------------------------------------
# coarse coverage map
# --------------------------------------------------------------------------

def _synthetic_filled_gds(path, fill_frac=0.5):
    ly = db.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("INTERPOSER")
    top.shapes(ly.layer(235, 0)).insert(db.DBox(0, 0, 1000, 1000))  # prBoundary
    if fill_frac > 0:
        top.shapes(ly.layer(50, 22)).insert(
            db.DBox(0, 0, 1000 * fill_frac, 1000))                  # Metal4 fill
    ly.write(str(path))


def test_compute_fill_coverage_reports_cells(tmp_path):
    gds = tmp_path / "filled.gds"
    _synthetic_filled_gds(gds, fill_frac=0.5)
    cov = _compute_fill_coverage(str(gds), [(50, 22)], cell_um=200.0)
    assert cov["cell_um"] == 200.0
    assert cov["grid"], "expected non-empty coverage grid"
    # A cell fully inside the filled half must read ~1.0 coverage.
    assert max(c["coverage"] for c in cov["grid"]) > 0.99
    # Coordinates are micrometres in the GDS frame.
    assert all("x_um" in c and "coverage" in c for c in cov["grid"])


def test_compute_fill_coverage_empty_without_fill(tmp_path):
    gds = tmp_path / "unfilled.gds"
    _synthetic_filled_gds(gds, fill_frac=0.0)
    cov = _compute_fill_coverage(str(gds), [(50, 22)], cell_um=200.0)
    assert cov["grid"] == []


def test_filler_layers_from_lyp(lyp):
    layers = _filler_layers(LayerMap(lyp))
    # Four BEOL metals, all on the filler datatype 22.
    assert len(layers) == 4
    assert all(dt == 22 for (_num, dt) in layers)


# --------------------------------------------------------------------------
# end-to-end binding to the PDK fill engine (needs the klayout binary + the
# interposer PDK checkout that ships fill_closure.fill_stack). Tolerant-skips
# where either is absent (a bare CI runner, a PDK checkout predating the fill
# work), so the pure-logic tests above still run.
# --------------------------------------------------------------------------

def _fill_engine_available():
    mod = _h._import_fill_closure()
    return mod is not None and hasattr(mod, "fill_stack")


needs_fill_engine = pytest.mark.skipif(
    shutil.which("klayout") is None or not _fill_engine_available(),
    reason="needs the klayout binary and an interposer PDK with fill_stack")


@needs_fill_engine
def test_fill_stack_end_to_end(lyp, tmp_path):
    gds = tmp_path / "synthetic_interposer.gds"
    ly = db.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("INTERPOSER")
    top.shapes(ly.layer(235, 0)).insert(db.DBox(0, 0, 300, 300))     # prBoundary
    top.shapes(ly.layer(50, 0)).insert(db.DBox(200, 200, 260, 260))  # drawn Metal4
    top.shapes(ly.layer(160, 0)).insert(db.DBox(20, 20, 80, 80))     # global keep-out
    ly.write(str(gds))

    fl = _filler_layers(LayerMap(str(LYP_PATH)))
    result = _insert_metal_fill(str(gds), "INTERPOSER", "single-pass", fl)
    assert result["status"] == "ok"

    report = result["report"]
    assert isinstance(report, dict)
    for metal in ("M4", "M5", "TM1", "TM2"):
        assert metal in report and "coverage_pct" in report[metal]
    assert "converged" in report

    out = db.Layout()
    out.read(str(gds))
    t = out.top_cell()

    def reg(lnum, dt):
        li = out.find_layer(lnum, dt)
        return db.Region() if li is None else db.Region(t.begin_shapes_rec(li))

    keepout = reg(160, 0)
    drawn = reg(50, 0)
    for (lnum, dt) in fl:
        fill = reg(lnum, dt)
        assert not fill.is_empty(), "expected fill on %d/%d" % (lnum, dt)
        assert (fill & keepout).is_empty(), \
            "fill on %d/%d landed inside the 160/0 keep-out" % (lnum, dt)
        if lnum == 50:
            assert (fill & drawn).is_empty(), "Metal4 fill overlaps drawn metal"

    stem = gds.with_suffix("")
    assert Path(str(stem) + ".fill_density.json").exists()
    assert Path(str(stem) + ".fill_coverage.json").exists()
