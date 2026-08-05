# SPDX-License-Identifier: GPL-3.0-or-later
"""A trace bend stops where the trace does: no copper past the flank.

The exporter draws a trace as one quad per segment plus one patch per interior
vertex, because a single snapped outline cannot be both on the 5 nm grid and
exactly 45 degrees. The patch used to be the square of side 2*half centred on
the vertex: it closes the corner, but at a 45 degree bend it reaches the corner
of its own bounding box while the two flanks meet earlier, leaving
(2 - sqrt(2)) * width / 2 of copper past the flank. 1.17 um on a 4 um trace,
with the tip 2.83 um from the vertex where the flanks meet at 2.17 and the
round join KiCad renders is at 2.00.

That overshoot trips no rule, which is how it survived: it is a convex 90
degree tip, so neither the angle rule nor the notch rule sees it. It is still
real copper pointing at whatever the trace passes.

The patch is now the square cut back to the flanks. The shape matters as much
as the size, and the two ways of getting it wrong are both pinned below:

- the bare wedge between the flanks has a corner equal to the turn angle, 45
  degrees at a 45 degree bend, and 3_2_angle.drc checks RAW polygons, so a
  wedge drawn on its own is a violation even though the copper around it is a
  straight trace (measured: 88 such polygons on TopMetal2, 8 on Metal4, 3 on
  Metal5 for this board);
- a 45 degree chamfer cuts ACROSS the flanks instead of stopping on them, which
  adds an intersection vertex to the merged outline and puts it off the 5 nm
  grid. Cutting along the flank adds no vertex at all.
"""
import math
import os
import sys
from pathlib import Path

import klayout.db as db
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import hyp_to_gds as _h  # noqa: E402
from hyp_to_gds import GDSGenerator, LayerMap, MANUFACTURING_GRID_NM  # noqa: E402

LYP_PATH = Path(os.environ.get("INTERPOSER_LYP", _h._find_default_lyp()))

WIDTH_UM = 4.0
DIRECTIONS = [(1, 0), (1, 1), (0, 1), (-1, 1),
              (-1, 0), (-1, -1), (0, -1), (1, -1)]


@pytest.fixture(scope="module", autouse=True)
def _require_lyp():
    if not LYP_PATH.exists():
        pytest.skip(f"interposer LYP not found: {LYP_PATH}")


@pytest.fixture
def gen():
    return GDSGenerator(LayerMap(str(LYP_PATH)), "TOP", "METRIC", [], None)


def _um(gen, v):
    return int(round(v / gen.layout.dbu))


def _drawn(gen, points, width_um=WIDTH_UM):
    return gen._trace_polygons(points, width_um)


def _merged(gen, points, width_um=WIDTH_UM):
    return db.Region(_drawn(gen, points, width_um)).merged()


def _square_only(gen, points, width_um=WIDTH_UM):
    """The same trace with every corner closed by the old square patch."""
    saved = GDSGenerator._corner_patch
    GDSGenerator._corner_patch = lambda self, a, p, b, half, diag: db.Polygon(
        db.Box(p.x - half, p.y - half, p.x + half, p.y + half))
    try:
        return db.Region(gen._trace_polygons(points, width_um)).merged()
    finally:
        GDSGenerator._corner_patch = saved


def _sharpest_convex_corner(poly):
    """Smallest convex interior angle of a polygon, in degrees.

    KLayout hulls run clockwise, so a convex corner turns right (cross < 0).
    """
    pts = list(poly.each_point_hull())
    sharpest = 180.0
    for i in range(len(pts)):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
        v1 = (b.x - a.x, b.y - a.y)
        v2 = (c.x - b.x, c.y - b.y)
        cross = v1[0] * v2[1] - v1[1] * v2[0]
        if cross >= 0:
            continue
        turn = math.degrees(math.atan2(abs(cross),
                                       v1[0] * v2[0] + v1[1] * v2[1]))
        sharpest = min(sharpest, 180 - turn)
    return sharpest


def _corner_of(gen, first, second, width_um=WIDTH_UM):
    """The patch for a bend from direction `first` into direction `second`."""
    a = db.Point(0, 0)
    p = db.Point(_um(gen, first[0] * 60.0), _um(gen, first[1] * 60.0))
    b = db.Point(p.x + _um(gen, second[0] * 60.0),
                 p.y + _um(gen, second[1] * 60.0))
    half = _um(gen, width_um / 2.0)
    diag = int(math.ceil(half / math.sqrt(2) / gen.grid_dbu)) * gen.grid_dbu
    return gen._corner_patch(a, p, b, half, diag), p, half


def _is_square_at(patch, p, half):
    return patch == db.Polygon(db.Box(p.x - half, p.y - half,
                                      p.x + half, p.y + half))


def test_45_bend_loses_exactly_the_overshoot(gen):
    """(2 - sqrt(2))^2 * w^2 / 8 per bend, to within the grid."""
    points = [(0.0, 0.0), (100.0, 0.0), (150.0, 50.0)]
    mitred, square = _merged(gen, points), _square_only(gen, points)

    removed = (square - mitred).area() * gen.layout.dbu ** 2
    predicted = (2 - math.sqrt(2)) ** 2 * WIDTH_UM ** 2 / 8
    assert removed == pytest.approx(predicted, abs=0.01)
    assert (mitred - square).is_empty(), \
        "a 45 degree bend must only lose copper, never gain it"


def test_the_outer_corner_lands_where_the_flanks_meet(gen):
    """The physical statement behind the area figure, pinned to a coordinate.

    The outer corner of the bend must be 2*diag - half along the incoming run
    from the vertex (diag being the on-grid 45 degree offset, ceil(half/
    sqrt(2))), not a further (2 - sqrt(2))*half out at the square's corner.
    """
    bend = (100.0, 0.0)
    mitred = _merged(gen, [(0.0, 0.0), bend, (150.0, 50.0)])
    half = WIDTH_UM / 2.0
    grid_um = MANUFACTURING_GRID_NM * 0.001
    diag = math.ceil(half / math.sqrt(2) / grid_um) * grid_um

    corners = {(p.x, p.y) for poly in mitred.each()
               for p in poly.each_point_hull()}
    assert (_um(gen, bend[0] + 2 * diag - half),
            _um(gen, bend[1] - half)) in corners, \
        "the outer corner is not where the flanks meet"
    assert (_um(gen, bend[0] + half), _um(gen, bend[1] - half)) \
        not in corners, "the square patch's corner is still copper"


def test_no_drawn_polygon_has_an_acute_corner(gen):
    """The rule that broke the first attempt. 3_2_angle.drc runs its acute
    check on the RAW polygons, so a patch shaped like the open wedge is a
    violation at every 45 degree bend even though the merged copper is a
    straight trace. Every drawn polygon has to be legal by itself."""
    for i, first in enumerate(DIRECTIONS):
        for step in (1, -1, 2, -2, 3, -3, 4):
            second = DIRECTIONS[(i + step) % len(DIRECTIONS)]
            patch, _, _ = _corner_of(gen, first, second)
            assert _sharpest_convex_corner(patch) >= 87.0, \
                f"acute patch on the turn {first} -> {second}"


def test_drawn_patches_stay_on_grid_and_0_45_90(gen):
    """3_1_offgrid.drc and 3_2_angle.drc read the raw polygons too."""
    points = [(0.0, 0.0), (50.0, 0.0), (100.0, 50.0), (100.0, 150.0),
              (50.0, 200.0), (0.0, 200.0)]
    grid = gen.grid_dbu

    for poly in _drawn(gen, points):
        pts = list(poly.each_point_hull())
        for p in pts:
            assert p.x % grid == 0 and p.y % grid == 0, f"offgrid vertex {p}"
        for a, b in zip(pts, pts[1:] + pts[:1]):
            dx, dy = abs(b.x - a.x), abs(b.y - a.y)
            assert dx == 0 or dy == 0 or dx == dy, f"edge {a}-{b} is not 0/45/90"


def test_orthogonal_90_bend_is_untouched(gen):
    """Between two axis-aligned runs the square already IS the mitre."""
    points = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]

    assert (_merged(gen, points) ^ _square_only(gen, points)).is_empty()


def test_diagonal_90_bend_reaches_further_than_the_square(gen):
    """The one case where the patch grows. Between two diagonal runs the
    flanks meet at 2*diag from the vertex, outside the square, so the square
    ends in a blunt stub short of the corner. The patch closes it to a point,
    which is 90 degrees and therefore legal."""
    points = [(0.0, 0.0), (60.0, 60.0), (0.0, 120.0)]
    mitred, square = _merged(gen, points), _square_only(gen, points)

    assert not (mitred - square).is_empty(), "expected the mitre to reach out"
    assert mitred.area() < square.area(), "and still to be the smaller shape"
    for poly in mitred.each():
        assert _sharpest_convex_corner(poly) >= 87.0


def test_corner_is_never_opened(gen):
    """The patch exists to close the wedge between two quads. One that fell
    short would show up as a notch or as a severed trace."""
    points = [(0.0, 0.0), (50.0, 0.0), (100.0, 50.0), (150.0, 50.0),
              (200.0, 100.0), (250.0, 100.0), (300.0, 150.0)]
    region = _merged(gen, points)

    assert region.count() == 1, "the trace came apart at a corner"
    assert region.notch_check(_um(gen, 2.0), False,
                              db.Metrics.Euclidian).count() == 0


def test_every_45_degree_turn_only_removes_copper(gen):
    """All sixteen 45 degree bends. A sign error in the outer-flank choice
    would cut into the inside of one of them, which shows up as added copper
    or as a severed trace."""
    for i, first in enumerate(DIRECTIONS):
        for step in (1, -1):
            second = DIRECTIONS[(i + step) % len(DIRECTIONS)]
            points = [(0.0, 0.0), (first[0] * 60.0, first[1] * 60.0),
                      (first[0] * 60.0 + second[0] * 60.0,
                       first[1] * 60.0 + second[1] * 60.0)]
            mitred, square = _merged(gen, points), _square_only(gen, points)
            assert (mitred - square).is_empty(), \
                f"mitre added copper on the turn {first} -> {second}"
            assert mitred.count() == 1, \
                f"trace came apart on the turn {first} -> {second}"
            assert mitred.notch_check(_um(gen, 2.0), False,
                                      db.Metrics.Euclidian).count() == 0


def test_turn_sharper_than_90_degrees_keeps_the_square(gen):
    """Not an oversight. Past 90 degrees the flanks meet in a spike that grows
    without bound as the turn tightens, and its tip is acute, which the angle
    deck reports. The square never leaves the trace's own width. The wedge such
    a turn leaves cannot be closed by any polygon that is neither acute nor
    rounded; KiCad rounds it, which is off-grid for us.
    """
    patch, p, half = _corner_of(gen, (1, 0), (-1, 1))    # 135 degree turn
    assert _is_square_at(patch, p, half)


def test_reversal_keeps_the_square(gen):
    patch, p, half = _corner_of(gen, (1, 0), (-1, 0))
    assert _is_square_at(patch, p, half)


def test_segment_off_the_45_grid_keeps_the_square(gen):
    """A run that is neither orthogonal nor exactly diagonal only gets an
    approximated flank, so a cut along that flank would land on a line the
    quad does not actually have."""
    a = db.Point(0, 0)
    p = db.Point(_um(gen, 100.0), 0)
    b = db.Point(_um(gen, 130.0), _um(gen, 50.0))        # 30 degrees, not 45
    half = _um(gen, WIDTH_UM / 2.0)
    diag = int(math.ceil(half / math.sqrt(2) / gen.grid_dbu)) * gen.grid_dbu

    assert _is_square_at(gen._corner_patch(a, p, b, half, diag), p, half)


def test_collinear_vertex_keeps_the_square(gen):
    """Two runs in the same direction already share their end edge, so the
    square lies inside the run and changes nothing."""
    patch, p, half = _corner_of(gen, (1, 0), (1, 0))
    assert _is_square_at(patch, p, half)


def test_grid_constant_is_the_one_the_patch_uses(gen):
    assert gen.grid_dbu == int(round(MANUFACTURING_GRID_NM * 0.001
                                     / gen.layout.dbu))
