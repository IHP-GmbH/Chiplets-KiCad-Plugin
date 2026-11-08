# SPDX-License-Identifier: GPL-3.0-or-later
"""A curved trace is drawn the way it was routed, whatever its sweep.

A .hyp ARC record names its two ends, centre and radius, and no direction
flag, so it looks as if a reader can only guess, and the reader guessed the
minor arc. The direction is in the record anyway, by convention: both .hyp
writers (export_hyperlynx.cpp and writers/hyperlynx_writer.py) swap the ends
of an arc KiCad calls counter-clockwise and then negate Y. KiCad's IsCCW is
taken on its raw Y-down coordinates, so after the swap every arc runs
clockwise there, and the Y negation turns that into counter-clockwise from
(X1, Y1) to (X2, Y2) in file coordinates, which is the HyperLynx convention.

A reader that takes the minor arc agrees with that for any sweep under 180
degrees and draws the complement of anything larger: a 270 degree arc came
out as the 90 degree arc between the same ends, on the other side of the
centre.

The cases below build each record the way the writers do, from a KiCad arc
given by start, mid and end, and check the drawn points geometrically: the
curve passes through KiCad's mid point and sweeps KiCad's angle.
"""
import math
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import hyp_to_gds as _h  # noqa: E402
from hyp_to_gds import GDSGenerator, LayerMap, TraceArc  # noqa: E402

LYP_PATH = Path(os.environ.get("INTERPOSER_LYP", _h._find_default_lyp()))
# The generator is built from the interposer PDK's .lyp, a sibling tree.
pytestmark = pytest.mark.needs_sibling_sources

R_UM = 50.0
UM = 1e-6  # metres per micrometre: the METRIC .hyp unit is the metre


@pytest.fixture(scope="module", autouse=True)
def _require_lyp():
    if not LYP_PATH.exists():
        pytest.skip(f"interposer LYP not found: {LYP_PATH}")


@pytest.fixture
def metric():
    return GDSGenerator(LayerMap(str(LYP_PATH)), "TOP", "METRIC", [], None)


def _kicad_arc(start_deg, sweep_deg, cx=200.0, cy=-120.0, r=R_UM):
    """(start, mid, end) of a KiCad arc in um, Y down, swept by sweep_deg.

    Positive sweep is counter-clockwise on the raw coordinates, which is
    what KiCad's IsCCW tests (the cross product of end - start and
    mid - start is negative for it).
    """
    def at(deg):
        a = math.radians(deg)
        return (cx + r * math.cos(a), cy + r * math.sin(a))
    return (at(start_deg), at(start_deg + sweep_deg / 2.0),
            at(start_deg + sweep_deg), (cx, cy))


def _is_ccw(start, mid, end):
    se = (end[0] - start[0], end[1] - start[1])
    sm = (mid[0] - start[0], mid[1] - start[1])
    return se[0] * sm[1] - se[1] * sm[0] < 0


def _metric_record(start, mid, end, centre, r=R_UM):
    """The ARC record both writers emit for this KiCad arc, METRIC units."""
    if _is_ccw(start, mid, end):
        start, end = end, start
    return TraceArc(net_name="N", x1=start[0] * UM, y1=-start[1] * UM,
                    x2=end[0] * UM, y2=-end[1] * UM,
                    xc=centre[0] * UM, yc=-centre[1] * UM,
                    radius=r * UM, width=4 * UM, layer="TopMetal2")


def _distance_to_polyline(p, points):
    best = math.inf
    for a, b in zip(points, points[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, (
            (p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
        best = min(best, math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy))
    return best


def _swept(points, centre):
    """Total unsigned angle the polyline turns through around the centre."""
    total = 0.0
    for a, b in zip(points, points[1:]):
        step = (math.atan2(b[1] - centre[1], b[0] - centre[0])
                - math.atan2(a[1] - centre[1], a[0] - centre[0]))
        step = (step + math.pi) % (2 * math.pi) - math.pi
        total += step
    return abs(math.degrees(total))


SWEEPS = [90.0, 179.0, 180.0, 181.0, 270.0, 300.0, 345.0]
STARTS = [0.0, 37.0, 135.0, 250.0]


@pytest.mark.parametrize("direction", [1, -1], ids=["ccw", "cw"])
@pytest.mark.parametrize("sweep", SWEEPS)
@pytest.mark.parametrize("start_deg", STARTS)
def test_the_drawn_arc_passes_through_kicads_mid_point(metric, start_deg,
                                                       sweep, direction):
    start, mid, end, centre = _kicad_arc(start_deg, direction * sweep)
    points = metric._arc_to_points(_metric_record(start, mid, end, centre))
    # The METRIC file already carries -Y, which the reader keeps.
    mid_gds = (mid[0], -mid[1])
    sagitta = R_UM * (1 - math.cos(math.pi / 16))
    assert _distance_to_polyline(mid_gds, points) <= sagitta + 1e-6, (
        "a %g degree arc was drawn on the wrong side of its centre" % sweep)


@pytest.mark.parametrize("direction", [1, -1], ids=["ccw", "cw"])
@pytest.mark.parametrize("sweep", SWEEPS)
def test_the_drawn_arc_sweeps_kicads_angle(metric, sweep, direction):
    start, mid, end, centre = _kicad_arc(37.0, direction * sweep)
    points = metric._arc_to_points(_metric_record(start, mid, end, centre))
    assert _swept(points, (centre[0], -centre[1])) == pytest.approx(sweep, abs=1e-6)


@pytest.mark.parametrize("sweep", [90.0, 270.0])
def test_the_drawn_arc_runs_from_the_first_end_to_the_second(metric, sweep):
    start, mid, end, centre = _kicad_arc(10.0, sweep)
    arc = _metric_record(start, mid, end, centre)
    points = metric._arc_to_points(arc)
    assert math.dist(points[0], (arc.x1 / UM, arc.y1 / UM)) < 1e-6
    assert math.dist(points[-1], (arc.x2 / UM, arc.y2 / UM)) < 1e-6


def test_a_long_arc_is_as_finely_drawn_as_a_short_one(metric):
    start, mid, end, centre = _kicad_arc(0.0, 300.0)
    points = metric._arc_to_points(_metric_record(start, mid, end, centre))
    # 16 chords per half turn, the resolution a semicircle always had.
    assert len(points) - 1 >= math.ceil(16 * 300.0 / 180.0)


def test_an_english_record_is_counter_clockwise_in_its_own_frame():
    """An ENGLISH .hyp is read with Y negated; the convention holds before that.

    The record runs counter-clockwise in its file coordinates; the reader's
    mirror then turns it clockwise in the GDS. The mid point of a 270 degree
    arc from (1, 0) to (0, -1) about the origin is (-1/sqrt 2, 1/sqrt 2) in
    the file, so (-1/sqrt 2, -1/sqrt 2) after the mirror.
    """
    english = GDSGenerator(LayerMap(str(LYP_PATH)), "TOP", "ENGLISH", [], None)
    inch = 1.0 / english.INCH_TO_UM
    r = R_UM * inch
    arc = TraceArc(net_name="N", x1=r, y1=0.0, x2=0.0, y2=-r, xc=0.0, yc=0.0,
                   radius=r, width=4 * inch, layer="TopMetal2")
    points = english._arc_to_points(arc)
    mid = (-R_UM / math.sqrt(2), -R_UM / math.sqrt(2))
    sagitta = R_UM * (1 - math.cos(math.pi / 16))
    assert _distance_to_polyline(mid, points) <= sagitta + 1e-6
    assert _swept(points, (0.0, 0.0)) == pytest.approx(270.0, abs=1e-6)


def _minor_arc_points(gen, arc, num_points=16):
    """The reader's previous discretization, which took the minor arc."""
    x1, y1 = gen._to_um(arc.x1), gen._to_um_y(arc.y1)
    x2, y2 = gen._to_um(arc.x2), gen._to_um_y(arc.y2)
    xc, yc = gen._to_um(arc.xc), gen._to_um_y(arc.yc)
    radius = gen._to_um(arc.radius)
    angle1 = math.atan2(y1 - yc, x1 - xc)
    diff = math.atan2(y2 - yc, x2 - xc) - angle1
    if diff > math.pi:
        diff -= 2 * math.pi
    elif diff < -math.pi:
        diff += 2 * math.pi
    return [(xc + radius * math.cos(angle1 + i / num_points * diff),
             yc + radius * math.sin(angle1 + i / num_points * diff))
            for i in range(num_points + 1)]


@pytest.mark.parametrize("direction", [1, -1], ids=["ccw", "cw"])
@pytest.mark.parametrize("sweep", [30.0, 90.0, 179.0])
@pytest.mark.parametrize("start_deg", STARTS)
def test_an_arc_under_180_degrees_keeps_its_points(metric, start_deg, sweep,
                                                   direction):
    """The minor arc was already right; its points must not move a bit, but
    for the two ends, which are the record's own (see test_trace_joins.py)."""
    start, mid, end, centre = _kicad_arc(start_deg, direction * sweep)
    arc = _metric_record(start, mid, end, centre)
    points = metric._arc_to_points(arc)
    assert points[1:-1] == _minor_arc_points(metric, arc)[1:-1]
    assert points[0] == (metric._to_um(arc.x1), metric._to_um_y(arc.y1))
    assert points[-1] == (metric._to_um(arc.x2), metric._to_um_y(arc.y2))
