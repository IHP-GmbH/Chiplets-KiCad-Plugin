# SPDX-License-Identifier: GPL-3.0-or-later
"""The exported carrier has to be on the process's manufacturing grid.

Two rules of the interposer PDK decide whether the drawn geometry is legal at
all, before any width or spacing question: 3_1_offgrid.drc (every vertex on a
5 nm grid) and 3_2_angle.drc (only 0/45/90 degree edges on the metals). Both
read the raw, as-drawn polygons. These tests hold the exporter to them without
needing the decks to run, and pin MANUFACTURING_GRID_NM to the value the deck
actually defines so the two cannot drift apart.
"""
import math
import os
import re
import sys
from pathlib import Path

import klayout.db as db
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import hyp_to_gds as _h  # noqa: E402
from hyp_to_gds import GDSGenerator, LayerMap, MANUFACTURING_GRID_NM  # noqa: E402

LYP_PATH = Path(os.environ.get("INTERPOSER_LYP", _h._find_default_lyp()))


@pytest.fixture(scope="module", autouse=True)
def _require_lyp():
    if not LYP_PATH.exists():
        pytest.skip(f"interposer LYP not found: {LYP_PATH}")


def _make_generator(grid_nm=MANUFACTURING_GRID_NM) -> GDSGenerator:
    return GDSGenerator(LayerMap(str(LYP_PATH)), "TOP", "METRIC", [], None,
                        grid_nm=grid_nm)


def _all_vertices(cell, layout):
    for li in layout.layer_indexes():
        for sh in cell.shapes(li).each():
            poly = sh.polygon
            if poly is None:
                continue
            yield from poly.each_point_hull()
            for h in range(poly.holes()):
                yield from poly.each_point_hole(h)


def _all_edges(cell, layout):
    for li in layout.layer_indexes():
        for sh in cell.shapes(li).each():
            poly = sh.polygon
            if poly is None:
                continue
            for edge in poly.each_edge():
                yield edge


def test_grid_constant_matches_the_pdk_deck():
    """MANUFACTURING_GRID_NM is the deck's GRID, not a number we picked."""
    deck = (Path(os.environ.get("PDK_ROOT", "")) / "..").resolve()
    candidates = []
    lyp_root = LYP_PATH.resolve()
    for parent in lyp_root.parents:
        cand = parent / "tech" / "drc" / "rule_decks" / "3_1_offgrid.drc"
        if cand.exists():
            candidates.append(cand)
        cand = parent / "drc" / "rule_decks" / "3_1_offgrid.drc"
        if cand.exists():
            candidates.append(cand)
    if not candidates:
        pytest.skip("interposer offgrid deck not reachable from the LYP")
    text = candidates[0].read_text()
    m = re.search(r"^\s*GRID\s*=\s*([0-9.]+)\.nm", text, re.M)
    assert m, f"no GRID definition found in {candidates[0]}"
    assert float(m.group(1)) == float(MANUFACTURING_GRID_NM)


def test_orthogonal_trace_is_on_grid():
    gen = _make_generator()
    # Deliberately off-grid endpoints (1 nm and 3 nm residues).
    gen.add_path([(0.000001, 0.000003), (50.000002, 0.000003)], 4.0, "TopMetal2")
    for p in _all_vertices(gen.routing_cell, gen.layout):
        assert p.x % MANUFACTURING_GRID_NM == 0
        assert p.y % MANUFACTURING_GRID_NM == 0


def test_diagonal_trace_stays_exactly_45_degrees():
    """A vertex-wise snap breaks diagonal runs; the exporter must not."""
    gen = _make_generator()
    # An exact 45 run whose endpoints round in opposite directions.
    gen.add_path([(0.000003, 0.000003), (30.000002, 30.000002)], 4.0, "TopMetal2")
    edges = list(_all_edges(gen.routing_cell, gen.layout))
    assert edges
    for edge in edges:
        dx, dy = edge.dx(), edge.dy()
        assert dx % MANUFACTURING_GRID_NM == 0
        assert dy % MANUFACTURING_GRID_NM == 0
        assert dx == 0 or dy == 0 or abs(dx) == abs(dy), (
            f"edge {edge} is neither orthogonal nor exactly 45 degrees")


def test_diagonal_trace_is_not_narrower_than_nominal():
    """Rounding the diagonal offset down would draw under the minimum width."""
    for width_um in (2.0, 4.0, 6.0):
        gen = _make_generator()
        gen.add_path([(0.0, 0.0), (40.0, 40.0)], width_um, "TopMetal2")
        region = db.Region()
        for li in gen.layout.layer_indexes():
            region.insert(gen.routing_cell.shapes(li))
        region.merge()
        # Width across a 45 degree run: the perpendicular distance between the
        # two long edges. Measured as the smallest width the region reports.
        narrow = region.width_check(int(round(width_um / gen.layout.dbu)))
        assert narrow.is_empty(), (
            f"a {width_um} um diagonal trace measures under its own width")


def test_instance_origins_and_pad_geometry_are_snapped(tmp_path):
    import json
    gen = _make_generator()
    pads = {"io_pads": [
        {"ref": "J1", "io_class": "wire_bond",
         "x_um": 100.000002, "y_um": 50.000003,
         "size_x_um": 100.0, "size_y_um": 100.0, "net": "VDD"},
    ]}
    pads_json = tmp_path / "io_pads.json"
    pads_json.write_text(json.dumps(pads))
    placed = gen.add_io_pads(str(pads_json))
    out = tmp_path / "carrier.gds"
    gen.write(str(out))

    layout = db.Layout()
    layout.read(str(out))
    for cell in layout.each_cell():
        for inst in cell.each_inst():
            disp = inst.trans.disp
            assert disp.x % MANUFACTURING_GRID_NM == 0
            assert disp.y % MANUFACTURING_GRID_NM == 0
        for p in _all_vertices(cell, layout):
            assert p.x % MANUFACTURING_GRID_NM == 0
            assert p.y % MANUFACTURING_GRID_NM == 0
    # The sidecar records what was drawn, not what the board carried.
    assert placed[0]["x_um"] == pytest.approx(100.0, abs=1e-9)
    assert placed[0]["y_um"] == pytest.approx(50.0, abs=1e-9)


def test_wire_bond_pad_carries_its_opening_and_recognition():
    """dfpad is what exempts a 100 um pad from the metal-slit rule; passiv is
    the opening a bond wire lands in. Without them the pad is a slab.

    The pad comes from the PDK's bondpad PyCell, so the enclosure is measured,
    not asserted equal: the PyCell draws the octagon one grid step taller than
    wide (rady + 0.005), which makes the vertical enclosure 2.105 um where the
    horizontal one is exactly Pas.c. What must hold is that it never falls
    below Pas.c anywhere, diagonal facets included, which the bbox cannot see.
    """
    gen = _make_generator()
    cell = gen._create_wire_bond_pad_cell(100.0, 100.0)
    seen = {}
    for li in gen.layout.layer_indexes():
        info = gen.layout.get_info(li)
        for sh in cell.shapes(li).each():
            seen[(info.layer, info.datatype)] = sh.bbox()
    assert (134, 0) in seen, "no TopMetal2 pad metal"
    assert (41, 0) in seen, "no dfpad recognition"
    assert (9, 0) in seen, "no passivation opening"
    metal = seen[(134, 0)]
    dfpad = seen[(41, 0)]
    passiv = seen[(9, 0)]
    assert dfpad == metal, "dfpad must not extend past TopMetal2 (Pad.i)"
    enc = gen.IO_PAD_PASSIV_ENCLOSURE_UM
    for span, name in ((metal.width() - passiv.width(), "x"),
                       (metal.height() - passiv.height(), "y")):
        assert span / 2 * gen.layout.dbu >= enc - 1e-9, \
            f"passivation enclosure in {name} is under Pas.c"

    metal_region = db.Region(cell.shapes(gen.layout.layer(134, 0)))
    passiv_region = db.Region(cell.shapes(gen.layout.layer(9, 0)))
    assert metal_region.enclosing_check(
        passiv_region, int(round(enc / gen.layout.dbu)), False,
        db.Metrics.Euclidian).is_empty(), \
        "the pad metal encloses its own opening by less than Pas.c"


def test_wire_bond_pad_matches_the_requested_pycell_shape():
    """The pad is the PDK's now, not three boxes drawn here, and it is the
    shape we asked the PyCell for. The count matters because the PyCell's
    'circle' branch emits TWO polygons per layer, the second one at full pad
    size on the passivation layer, which would wipe out the metal enclosure.
    """
    gen = _make_generator()
    cell = gen._create_wire_bond_pad_cell(100.0, 100.0)
    vertices = {'square': 4, 'octagon': 8}[gen.IO_PAD_SHAPE]

    for layer in ((134, 0), (41, 0), (9, 0)):
        shapes = list(cell.shapes(gen.layout.layer(*layer)).each())
        assert len(shapes) == 1, f"expected one polygon on {layer}"
        assert shapes[0].polygon.num_points() == vertices, \
            f"{layer} is not a {gen.IO_PAD_SHAPE}"


def test_wire_bond_pad_shape_is_one_the_pycell_draws_correctly():
    """'circle' is broken in the PyCell (measured: 116 enclosure violations,
    minimum enclosure 0.5 nm). Nothing rejects it there: the branch is
    if octagon / else if square / else circle, so any typo lands in it."""
    assert GDSGenerator.IO_PAD_SHAPE in ('square', 'octagon')


def test_wire_bond_pad_vertices_are_on_grid_and_0_45_90():
    """The PyCell has its own grid; it has to agree with the deck's."""
    gen = _make_generator()
    cell = gen._create_wire_bond_pad_cell(100.0, 100.0)

    for li in gen.layout.layer_indexes():
        for sh in cell.shapes(li).each():
            pts = list(sh.polygon.each_point_hull())
            for p in pts:
                assert p.x % MANUFACTURING_GRID_NM == 0
                assert p.y % MANUFACTURING_GRID_NM == 0
            for a, b in zip(pts, pts[1:] + pts[:1]):
                dx, dy = abs(b.x - a.x), abs(b.y - a.y)
                assert dx == 0 or dy == 0 or dx == dy


def test_wire_bond_pad_too_small_for_the_pycell_is_refused():
    """The PyCell resets hwquota to 1.0 with only a print below 10 um, which
    would draw a square while the .chiplet still recorded the rectangle."""
    gen = _make_generator()

    with pytest.raises(ValueError, match="bondpad PyCell"):
        gen._create_wire_bond_pad_cell(100.0, 5.0)


def test_grid_can_be_disabled_for_debugging():
    """grid_nm=0 keeps the historical path geometry, unsnapped."""
    gen = _make_generator(grid_nm=0)
    gen.add_path([(0.000001, 0.000003), (50.000002, 0.000003)], 4.0, "TopMetal2")
    kinds = [sh.is_path() for li in gen.layout.layer_indexes()
             for sh in gen.routing_cell.shapes(li).each()]
    assert kinds and all(kinds), "with snapping off the trace stays a path"
