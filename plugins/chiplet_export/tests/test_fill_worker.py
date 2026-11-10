# SPDX-License-Identifier: GPL-3.0-or-later
"""Worker-side tests for metal density fill (hyp_to_gds).

Covers the plugin's own responsibilities -- reading the no-fill sidecar,
painting the keep-out datatypes, and measuring coarse coverage -- WITHOUT the
interposer PDK fill engine, which is exercised separately (it needs the klayout
binary and the PDK tree). The keep-out datatype contract (160/0 global,
<metal>/23 per metal) is what the PDK generators subtract, so getting it right
here is what makes a KiCad-authored keep-out actually honored.
"""
import hashlib
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

@pytest.mark.needs_sibling_sources
def test_nofill_target_mapping(lyp):
    gen = _gen(lyp)
    assert gen._nofill_target("global") == (160, 0)
    for role, metal in (("M4", "Metal4"), ("M5", "Metal5"),
                        ("TM1", "TopMetal1"), ("TM2", "TopMetal2")):
        num, _dt = gen.layer_map.get_layer(metal)
        assert gen._nofill_target(role) == (num, 23), role
    assert gen._nofill_target("bogus") is None


@pytest.mark.needs_sibling_sources
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


@pytest.mark.needs_sibling_sources
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


COVERAGE_SHA256 = (
    "210d946990969e4ef38d8a1d850966ff363c24f49fce351e04c8204d9d7e6491")


def _stamp_lattice_fill(design, output):
    """A stand-in engine fill: lattices that overlap across metals, straddle
    the 200 um cells, sit partly in a placed subcell, plus one 45-degree shape
    across a cell corner, on a prBoundary whose last row and column are short.
    """
    ly = db.Layout()
    ly.read(str(design))
    top = ly.top_cell()
    sub = ly.create_cell("FILL_TILE")
    for x in range(0, 40, 9):
        sub.shapes(ly.layer(67, 22)).insert(db.DBox(x, 0, x + 5, 5))
    for (lnum, pitch, size, dx) in ((50, 7.0, 4.0, 0.0), (67, 7.0, 4.0, 2.5),
                                    (126, 23.0, 15.0, 1.0)):
        shapes = top.shapes(ly.layer(lnum, 22))
        y = 72.0
        while y < 1040.0:
            x = -128.0 + dx
            while x < 925.0:
                shapes.insert(db.DBox(x, y, x + size, y + size))
                x += pitch
            y += pitch * 1.5
    for i in range(6):
        top.insert(db.DCellInstArray(sub.cell_index(),
                                     db.DTrans(db.DVector(50 + 180 * i, 265.0))))
    top.shapes(ly.layer(134, 22)).insert(db.DPolygon([
        db.DPoint(250, 450), db.DPoint(290, 410), db.DPoint(330, 450),
        db.DPoint(290, 490)]))
    opts = db.SaveLayoutOptions()
    opts.gds2_write_timestamps = False
    ly.write(str(output), opts)


def test_the_coverage_map_and_the_filled_gds_are_pinned(tmp_path, monkeypatch):
    # The map must stay byte for byte what the whole-fill merge produced; the
    # digest is that version's output on this fill. The GDS is the engine's,
    # untouched by the map.
    gds = tmp_path / "interposer.gds"
    ly = db.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("INTERPOSER")
    top.shapes(ly.layer(235, 0)).insert(db.DBox(-130, 70, 930.5, 1047.3))
    ly.write(str(gds))
    written = {}

    class _Engine:
        @staticmethod
        def fill_stack(design, output, **_kw):
            _stamp_lattice_fill(design, output)
            written["gds"] = Path(output).read_bytes()
            return {"converged": True}

    monkeypatch.setattr(_h.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(_h, "_import_fill_closure", lambda: _Engine)
    layers = [(50, 22), (67, 22), (126, 22), (134, 22)]
    result = _insert_metal_fill(str(gds), "INTERPOSER", "closure", layers,
                                log=lambda *_: None)

    assert result["status"] == "ok"
    assert gds.read_bytes() == written["gds"]
    cov = (tmp_path / "interposer.fill_coverage.json").read_bytes()
    assert len(result["coverage"]["grid"]) == 30
    assert hashlib.sha256(cov).hexdigest() == COVERAGE_SHA256


@pytest.mark.needs_sibling_sources
def test_filler_layers_from_lyp(lyp):
    layers = _filler_layers(LayerMap(lyp))
    # Four BEOL metals, all on the filler datatype 22.
    assert len(layers) == 4
    assert all(dt == 22 for (_num, dt) in layers)


# --------------------------------------------------------------------------
# a fill engine that stops
# --------------------------------------------------------------------------

def test_a_fill_engine_that_stops_is_an_error_not_a_traceback(
        tmp_path, monkeypatch, capsys):
    gds = tmp_path / "interposer.gds"
    gds.write_bytes(b"unfilled")

    class _Engine:
        @staticmethod
        def fill_stack(design, output, **_kw):
            raise RuntimeError(
                "fill_metal_run.drc: interposer_filler_m45: EdgeSeal ring has "
                "no interior\nERROR: in Executable::execute\n  backtrace")

    monkeypatch.setattr(_h.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(_h, "_import_fill_closure", lambda: _Engine)
    result = _insert_metal_fill(str(gds), "INTERPOSER", "single-pass", [])

    assert result == {"status": "error", "report": None, "coverage": None}
    err = capsys.readouterr().err.splitlines()
    assert err[0] == ("ERROR: metal fill stopped: fill_metal_run.drc: "
                      "interposer_filler_m45: EdgeSeal ring has no interior")
    assert err[1:] == ["  ERROR: in Executable::execute", "    backtrace"]
    assert gds.read_bytes() == b"unfilled"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["interposer.gds"]


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


@pytest.mark.klayout_binary
@pytest.mark.needs_sibling_sources
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
