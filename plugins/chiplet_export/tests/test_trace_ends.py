# SPDX-License-Identifier: GPL-3.0-or-later
"""A trace's GDS copper runs w/2 past each of its two ends (square ends).

The board draws a track end as a round cap w/2 past the endpoint, and the
reserved copper a PDN stage writes is the rectangle w/2 past it. The exporter
used to stop the copper flush at the endpoint, so every free end came out w/2
short of both, which the deck sees once an end lands on something small.

Square, not round: a round cap is a polygonised arc and fails the angle rule
the fractured drawing exists for. The square contains KiCad's round cap and
reaches (sqrt(2) - 1) * w/2 further at its corners, so where that corner
would touch another net's trace the export refuses instead of merging them.
"""
import os
import sys
from pathlib import Path

import klayout.db as db
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import hyp_to_gds as _h  # noqa: E402
from hyp_to_gds import (GDSGenerator, LayerMap, TraceSegment,  # noqa: E402
                        TrackEndRefused, refuse_touching_nets)

LYP_PATH = Path(os.environ.get("INTERPOSER_LYP", _h._find_default_lyp()))
# Every case draws with the interposer PDK's .lyp, a sibling tree.
pytestmark = pytest.mark.needs_sibling_sources

DIRECTIONS = [(1, 0), (1, 1), (0, 1), (-1, 1),
              (-1, 0), (-1, -1), (0, -1), (1, -1)]
RUN_UM = 30.0


@pytest.fixture(scope="module", autouse=True)
def _require_lyp():
    if not LYP_PATH.exists():
        pytest.skip(f"interposer LYP not found: {LYP_PATH}")


def _generator(**kwargs):
    return GDSGenerator(LayerMap(str(LYP_PATH)), "TOP", "METRIC", [], None,
                        **kwargs)


@pytest.fixture
def gen():
    return _generator()


def _um(gen, v):
    return int(round(v / gen.layout.dbu))


def _merged(gen, points, width_um):
    return db.Region(gen._trace_polygons(points, width_um)).merged()


def _seg(net, x1, y1, x2, y2, w, layer="TopMetal1"):
    """A METRIC segment from micrometres (the generator reads metres)."""
    return TraceSegment(net, x1 * 1e-6, y1 * 1e-6, x2 * 1e-6, y2 * 1e-6,
                        w * 1e-6, layer)


def test_a_stripe_reaches_its_drawn_rectangle(gen):
    # The board's 20 um strap from 2585 to 3515 is the reserved rectangle
    # 2575 to 3525: the copper must be exactly that box.
    copper = _merged(gen, [(2585.0, 685.0), (3515.0, 685.0)], 20.0)
    assert copper.count() == 1
    assert copper.bbox() == db.Box(_um(gen, 2575.0), _um(gen, 675.0),
                                   _um(gen, 3525.0), _um(gen, 695.0))
    assert copper.area() == copper.bbox().area()


@pytest.mark.parametrize("d", DIRECTIONS)
def test_each_end_grows_by_half_along_its_run(gen, d):
    copper = _merged(gen, [(0.0, 0.0), (RUN_UM * d[0], RUN_UM * d[1])], 4.0)
    # Half of 4 um is 2000 nm; at 45 degrees the flank offset is
    # 2000 / sqrt(2) = 1414.2 nm, rounded up to the 5 nm grid.
    half, diag, run = 2000, 1415, _um(gen, RUN_UM)
    assert copper.count() == 1
    box = copper.bbox()
    if d[0] == 0 or d[1] == 0:
        # A run of `run` plus half at each end, 2 * half wide.
        assert sorted((box.width(), box.height())) == [2 * half, run + 2 * half]
        assert copper.area() == 2 * half * (run + 2 * half)
    else:
        # Each end moves diag along both axes and the flanks sit diag off
        # the centreline on both axes: run + 4 * diag square, a band
        # 2 * diag thick on each axis.
        assert box.width() == box.height() == run + 4 * diag
        assert copper.area() == 4 * diag * (run + 2 * diag)


@pytest.mark.parametrize("d", DIRECTIONS)
def test_an_end_stays_on_grid_and_on_the_angle_grid(gen, d):
    g = max(1, gen.grid_dbu)
    for poly in gen._trace_polygons([(0.0, 0.0), (RUN_UM * d[0], RUN_UM * d[1]),
                                     (RUN_UM * d[0] + 20.0, RUN_UM * d[1])], 1.64):
        pts = list(poly.each_point_hull())
        assert all(q.x % g == 0 and q.y % g == 0 for q in pts)
        for u, v in zip(pts, pts[1:] + pts[:1]):
            dx, dy = abs(v.x - u.x), abs(v.y - u.y)
            assert dx == 0 or dy == 0 or dx == dy


def test_an_interior_vertex_is_unchanged(gen):
    # The same bends with the end runs ten times longer: around the bends,
    # far from either end, the copper is the same.
    short = [(0.0, 0.0), (40.0, 0.0), (60.0, 20.0), (60.0, 60.0)]
    long = [(-360.0, 0.0), (40.0, 0.0), (60.0, 20.0), (60.0, 420.0)]
    window = db.Region(db.Box(_um(gen, 10.0), _um(gen, -10.0),
                              _um(gen, 70.0), _um(gen, 50.0)))
    assert ((_merged(gen, short, 4.0) & window)
            ^ (_merged(gen, long, 4.0) & window)).is_empty()


def test_a_width_change_is_closed(gen):
    # A 6 um run meeting a 2 um one: each is a path end of its own width, so
    # the wide run's cap covers the step a flush end left.
    wide = _merged(gen, [(0.0, 0.0), (0.0, 20.0)], 6.0)
    narrow = _merged(gen, [(0.0, 20.0), (0.0, 40.0)], 2.0)
    step = db.Region(db.Box(_um(gen, -3.0), _um(gen, 20.0),
                            _um(gen, 3.0), _um(gen, 23.0)))
    assert (step - (wide + narrow).merged()).is_empty()


def test_the_path_form_carries_the_extension():
    gen = _generator(grid_nm=0)
    assert gen.add_path([(0.0, 0.0), (10.0, 0.0)], 2.0, "TopMetal1")
    layer = gen._get_gds_layer("TopMetal1")
    (shape,) = list(gen.routing_cell.shapes(layer).each())
    assert shape.is_path()
    assert shape.dpath.bbox() == db.DBox(-1.0, -1.0, 11.0, 1.0)


# A 20 um stripe ending at x = 100, and a 2 um trace of another net whose own
# square end has its corner at (108.5, 8.5). The round caps are 2.43 um apart,
# clear of the 1.64 um TopMetal1 space, so the board passes; the square end
# reaches (110, 10) and would merge the two nets.
STRIPE = _seg("VDD", 0.0, 0.0, 100.0, 0.0, 20.0)


def _refusal(segments, **kwargs):
    gen = _generator(**kwargs)
    gen.add_segments_as_paths(segments)
    with pytest.raises(TrackEndRefused) as exc:
        refuse_touching_nets(gen.trace_copper, gen.layout.dbu)
    return str(exc.value).splitlines()


def test_a_square_end_that_reaches_another_net_is_refused():
    near = _seg("SIG", 109.5, 9.5, 130.0, 9.5, 2.0)
    first, place, advice = _refusal([STRIPE, near])
    assert first == "1 place(s) where two nets' traces touch"
    # The two square corners overlap between (108.5, 8.5) and (110, 10).
    assert place == ("TopMetal1: SIG meets VDD at "
                     "(108.500, 8.500)-(110.000, 10.000) um")
    assert advice.startswith("widen the clearance there")


def test_the_path_form_is_checked_too():
    near = _seg("SIG", 109.5, 9.5, 130.0, 9.5, 2.0)
    assert _refusal([STRIPE, near], grid_nm=0)[1].startswith(
        "TopMetal1: SIG meets VDD")


def test_a_square_end_that_only_abuts_another_net_is_refused():
    # The stripe's square end stops at x = 110; a trace starting square on
    # that edge shares it, and shared edges merge in the GDS.
    abut = _seg("SIG", 111.0, 0.0, 130.0, 0.0, 2.0)
    # Only an edge is shared, so the place is the 1 nm sliver across it.
    assert _refusal([STRIPE, abut])[1] == (
        "TopMetal1: SIG meets VDD at (109.999, -1.001)-(110.000, 1.001) um")


def test_a_square_end_clear_of_other_nets_is_drawn():
    gen = _generator()
    clear = _seg("SIG", 112.5, 9.5, 130.0, 9.5, 2.0)
    # Its own width, so a path of its own whose end cap lands on the stripe.
    same_net = _seg("VDD", 100.0, 0.0, 100.0, 40.0, 10.0)
    assert gen.add_segments_as_paths([STRIPE, clear, same_net]) == 3
    refuse_touching_nets(gen.trace_copper, gen.layout.dbu)


def test_the_export_refuses_and_writes_no_gds(tmp_path, capsys):
    # The same pair on a board: the conversion names both ends and stops.
    hyp = tmp_path / "near_ends.hyp"
    seg = ('  (SEG X1=%.6f Y1=%.6f X2=%.6f Y2=%.6f W=%.6f L="TopMetal1")')
    hyp.write_text("\n".join([
        "{VERSION=2.14}",
        "{UNITS=METRIC LENGTH}",
        '{BOARD "near_ends"',
        "  (PERIMETER_SEGMENT X1=0.000000 Y1=0.000000 X2=0.001000 Y2=0.000000)",
        "  (PERIMETER_SEGMENT X1=0.001000 Y1=0.000000 X2=0.001000 Y2=-0.001000)",
        "  (PERIMETER_SEGMENT X1=0.001000 Y1=-0.001000 X2=0.000000 Y2=-0.001000)",
        "  (PERIMETER_SEGMENT X1=0.000000 Y1=-0.001000 X2=0.000000 Y2=0.000000)",
        "}",
        "{STACKUP",
        '  (SIGNAL T=3.5e-05 P=0 C=1.724e-08 L="TopMetal1" M=COPPER)',
        "}",
        '{NET="VDD"',
        seg % (0.0001, -0.0005, 0.0002, -0.0005, 0.00002),
        "}",
        '{NET="SIG"',
        seg % (0.0002095, -0.0004905, 0.00023, -0.0004905, 0.000002),
        "}",
    ]) + "\n")
    out = tmp_path / "near_ends.gds"

    ok = _h.convert_hyp_to_gds(hyp_path=str(hyp), output_path=str(out),
                               lyp_path=str(LYP_PATH))

    assert ok is False
    assert not out.exists()
    err = capsys.readouterr().err
    assert "ERROR: track ends refused: 1 place(s) where two nets' traces touch" in err
    assert "TopMetal1: SIG meets VDD" in err


def _deck_markers(polys, g):
    """What the carrier's offgrid and 0/45/90 checks see, in both passes.

    The deck judges every drawn polygon on its own and then the merged
    outline, where crossing edges add vertices. Returns the off-grid vertices
    and the off-angle edges of both, in database units.
    """
    offgrid, angle = [], []
    for poly in list(polys) + list(db.Region(polys).merged().each()):
        pts = list(poly.each_point_hull())
        offgrid += [(q.x, q.y) for q in pts if q.x % g or q.y % g]
        for u, v in zip(pts, pts[1:] + pts[:1]):
            dx, dy = abs(v.x - u.x), abs(v.y - u.y)
            if not (dx == 0 or dy == 0 or dx == dy):
                angle.append(((u.x, u.y), (v.x, v.y)))
    return offgrid, angle


# A junction from a routed board: one trace leaves a pad at 45 degrees and
# turns east, and a second one starts on that corner and leaves at 45 degrees.
# The corner is off the 5 nm grid, so the two traces, each snapped from its own
# start, reach it one grid step apart (132228.430 and 132228.435 um).
JOINT = [
    _seg("gnd", 131754.121, -68079.316, 132228.433, -67605.004, 4.0, "TopMetal2"),
    _seg("gnd", 132307.579, -67605.004, 132228.433, -67605.004, 4.0, "TopMetal2"),
    _seg("gnd", 132230.396, -67603.041, 132230.396, -66767.969, 4.0, "TopMetal2"),
    _seg("gnd", 132228.433, -67605.004, 132230.396, -67603.041, 4.0, "TopMetal2"),
]


def test_an_end_on_a_corner_of_its_own_net_passes_the_deck(gen):
    # A square end there would stick out of the corner along the incoming
    # run, and its edge would cross that run's flank one step off: a merged
    # vertex at half a grid step and a 3 nm tilted edge.
    assert gen.add_segments_as_paths(JOINT) == 2
    copper = gen.trace_copper[("TopMetal2", "gnd")]
    assert _deck_markers(copper, gen.grid_dbu) == ([], [])


def test_the_path_form_keeps_a_joint_end_flush():
    gen = _generator(grid_nm=0)
    gen.add_path([(0.0, 0.0), (10.0, 0.0)], 2.0, "TopMetal1",
                 free=(False, True))
    layer = gen._get_gds_layer("TopMetal1")
    (shape,) = list(gen.routing_cell.shapes(layer).each())
    assert shape.dpath.bbox() == db.DBox(0.0, -1.0, 11.0, 1.0)
