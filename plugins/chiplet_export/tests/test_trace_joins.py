# SPDX-License-Identifier: GPL-3.0-or-later
"""A trace that bends off the 45 degree grid, or runs into a curve, keeps its
outline: no copper past the edge, none missing inside it.

Two ways a routed trace came out of the GDS with ragged corners, each pinned
by a geometric check on the drawn copper, not by a picture:

- A bend whose runs are neither orthogonal nor exactly diagonal (any-angle
  routing, and every vertex of a discretized arc) was closed with the square
  of side 2*half centred on the vertex. Turned with the trace, a corner of
  that square stands up to (sqrt(2) - 1) * half past the edge: 0.83 um on a
  4 um trace, every few degrees along a curve.
- An arc's drawn ends were computed from its centre and radius, which KiCad
  rounds to the nanometre, so they could miss the record's own ends by a
  rounding step. The stitcher matches ends to the nanometre and then saw two
  paths, butt-ended at the joint and with no corner patch, which leaves a
  notch inside the nominal width where the curve meets its lead.
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
from hyp_to_gds import GDSGenerator, LayerMap, TraceArc, TraceSegment  # noqa: E402

LYP_PATH = Path(os.environ.get("INTERPOSER_LYP", _h._find_default_lyp()))
# Every case draws with the interposer PDK's .lyp, a sibling tree.
pytestmark = pytest.mark.needs_sibling_sources

WIDTH_UM = 4.0
HALF = WIDTH_UM / 2.0


@pytest.fixture(scope="module", autouse=True)
def _require_lyp():
    if not LYP_PATH.exists():
        pytest.skip(f"interposer LYP not found: {LYP_PATH}")


@pytest.fixture
def gen():
    return GDSGenerator(LayerMap(str(LYP_PATH)), "TOP", "METRIC", [], None)


def _distance_to_polyline(p, points):
    best = math.inf
    for a, b in zip(points, points[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, (
            (p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
        best = min(best, math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy))
    return best


def _copper(gen, paths):
    polys = []
    for points in paths:
        polys.extend(gen._trace_polygons(points, WIDTH_UM))
    return db.Region(polys).merged()


def _past_ends(points):
    """The centreline with each end run on by HALF, as square ends draw it."""
    def on(end, toward):
        dx, dy = end[0] - toward[0], end[1] - toward[1]
        length = math.hypot(dx, dy)
        return (end[0] + dx * HALF / length, end[1] + dy * HALF / length)
    return [on(points[0], points[1])] + points[1:-1] + [on(points[-1], points[-2])]


def _farthest_copper(gen, points):
    """How far from the centreline the drawn copper reaches, in um. The
    square ends reach w/2 along the run past each endpoint, so the distance
    is taken to the centreline run on by that much."""
    dbu = gen.layout.dbu
    line = _past_ends(points)
    return max(_distance_to_polyline((q.x * dbu, q.y * dbu), line)
               for poly in _copper(gen, [points]).each()
               for q in poly.each_point_hull())


def _covers(gen, region, x, y):
    dbu = gen.layout.dbu
    probe = db.Region(db.Box(int(round(x / dbu)), int(round(y / dbu)),
                             int(round(x / dbu)) + 1, int(round(y / dbu)) + 1))
    return (probe - region).is_empty()


def _turn(a, p, b):
    d1 = math.atan2(p[1] - a[1], p[0] - a[0])
    d2 = math.atan2(b[1] - p[1], b[0] - p[0])
    return abs((d2 - d1 + math.pi) % (2 * math.pi) - math.pi)


def _mitre(turn):
    """Where the two flanks of a bend meet, from the centreline vertex."""
    return HALF / math.cos(turn / 2.0)


# Slack for the 5 nm grid: a snapped vertex moves by up to half a step on
# each axis, and the run it sits on was walked by snapped deltas.
GRID_SLACK = 0.01

ANY_ANGLE_BENDS = [
    [(0.0, 0.0), (100.0, 0.0), (200.0, 30.0)],
    [(0.0, 0.0), (100.0, 37.0), (130.0, 120.0)],
    [(0.0, 0.0), (80.0, -20.0), (160.0, -20.0)],
    [(0.0, 0.0), (60.0, 60.0), (90.0, 140.0)],
]


@pytest.mark.parametrize("points", ANY_ANGLE_BENDS)
def test_an_any_angle_bend_stays_inside_its_mitre(gen, points):
    reach = _farthest_copper(gen, points)
    assert reach <= _mitre(_turn(*points)) + GRID_SLACK, (
        "copper %.3f um from the centreline at a bend whose flanks meet at "
        "%.3f um" % (reach, _mitre(_turn(*points))))


def _arc(start_deg, sweep_deg, r=50.0, chords=16):
    return [(r * math.cos(math.radians(start_deg + i * sweep_deg / chords)),
             r * math.sin(math.radians(start_deg + i * sweep_deg / chords)))
            for i in range(chords + 1)]


@pytest.mark.parametrize("start_deg", [0.0, 20.0, 45.0, 100.0])
def test_a_curve_has_no_sawtooth(gen, start_deg):
    points = _arc(start_deg, 90.0)
    turn = math.radians(90.0 / 16)
    assert _farthest_copper(gen, points) <= _mitre(turn) + GRID_SLACK


@pytest.mark.parametrize("points", ANY_ANGLE_BENDS)
def test_an_any_angle_bend_is_closed_on_the_outside(gen, points):
    """The negative control for the mitre: removing the square must not open
    the corner. The point just inside the nominal edge, on the outer side of
    the bisector, is copper."""
    a, p, b = points
    d1 = math.atan2(p[1] - a[1], p[0] - a[0])
    d2 = math.atan2(b[1] - p[1], b[0] - p[0])
    left = math.sin(d2 - d1) > 0
    bisector = (d1 + d2) / 2.0 if abs(d2 - d1) < math.pi else (d1 + d2) / 2.0 + math.pi
    out = bisector - math.pi / 2 if left else bisector + math.pi / 2
    region = _copper(gen, [points])
    x, y = p[0] + 0.98 * HALF * math.cos(out), p[1] + 0.98 * HALF * math.sin(out)
    assert _covers(gen, region, x, y)


def _sharpest_convex_corner(poly):
    pts = list(poly.each_point_hull())
    sharpest = 180.0
    for i in range(len(pts)):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
        v1 = (b.x - a.x, b.y - a.y)
        v2 = (c.x - b.x, c.y - b.y)
        cross = v1[0] * v2[1] - v1[1] * v2[0]
        if cross >= 0:
            continue
        turn = math.degrees(math.atan2(abs(cross), v1[0] * v2[0] + v1[1] * v2[1]))
        sharpest = min(sharpest, 180 - turn)
    return sharpest


@pytest.mark.parametrize("points", ANY_ANGLE_BENDS + [_arc(20.0, 90.0)])
def test_no_drawn_patch_has_an_acute_corner(gen, points):
    """The angle deck reads raw polygons; a wedge alone would be acute."""
    for poly in gen._trace_polygons(points, WIDTH_UM):
        assert _sharpest_convex_corner(poly) >= 80.0


def _m(nm):
    return float("%.10f" % (nm / 1e9))   # nm -> metres, as the writers print


def _route(pin, s, e, pout, centre, r, ccw):
    """A lead-in, a KiCad arc and a lead-out, recorded as the writers do.

    Integers in nm in KiCad's Y-down frame. The centre and radius are the
    nanometre-rounded ones KiCad keeps, which is what puts the arc's
    computed ends a rounding step off its recorded ends.
    """
    x1, x2 = (e, s) if ccw else (s, e)
    w = _m(4000)
    return [
        ("seg", TraceSegment("N", _m(pin[0]), _m(-pin[1]), _m(s[0]), _m(-s[1]),
                             w, "TopMetal2")),
        ("arc", TraceArc("N", _m(x1[0]), _m(-x1[1]), _m(x2[0]), _m(-x2[1]),
                         _m(centre[0]), _m(-centre[1]), _m(r), w, "TopMetal2")),
        ("seg", TraceSegment("N", _m(e[0]), _m(-e[1]), _m(pout[0]), _m(-pout[1]),
                             w, "TopMetal2")),
    ]


# Two tangent routes, found by sweeping random KiCad-like arcs: about one in
# twenty broke the chain before the arc's ends were pinned to the record.
ROUTES = {
    "45deg": dict(pin=(9806992, 9674028), s=(9886831, 9613813),
                  e=(9966982, 9593050), pout=(10066015, 9606926),
                  centre=(9951971, 9700182), r=108180, ccw=True),
    "200deg": dict(pin=(7103857, 9461846), s=(7030792, 9530121),
                   e=(6954341, 9412663), pout=(7046351, 9373495),
                   centre=(6982211, 9478132), r=71155, ccw=True),
}


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_a_curve_and_its_leads_are_one_trace(gen, name):
    assert len(gen._connect_traces_to_paths(_route(**ROUTES[name]))) == 1


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_a_curve_meets_its_leads_without_a_notch(gen, name):
    """Copper 1.9 um off the centreline, within the nominal 2 um, on both
    sides of each joint and 0.2 um along the lead either way of it."""
    route = ROUTES[name]
    region = _copper(gen, gen._connect_traces_to_paths(_route(**route)))
    missing = []
    for joint, lead in ((route["s"], route["pin"]), (route["e"], route["pout"])):
        jx, jy = joint[0] / 1000.0, -joint[1] / 1000.0
        lx, ly = lead[0] / 1000.0, -lead[1] / 1000.0
        length = math.hypot(jx - lx, jy - ly)
        ux, uy = (jx - lx) / length, (jy - ly) / length
        for side in (1, -1):
            for k in range(-20, 21):
                x = jx + side * 1.9 * -uy + k * 0.01 * ux
                y = jy + side * 1.9 * ux + k * 0.01 * uy
                if not _covers(gen, region, x, y):
                    missing.append((round(x, 3), round(y, 3)))
    assert not missing, "no copper at %d points inside the nominal width, e.g. %s" % (
        len(missing), missing[:3])
