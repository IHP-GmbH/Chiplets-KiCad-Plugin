# SPDX-License-Identifier: GPL-3.0-or-later
"""The exporter closes sub-minimum-space notches without shorting anything.

KiCad routes through the middle of a round pad and takes its first bend while
still inside it, so the trace leaves the pad almost tangentially and the wedge
between the trace edge and the pad arc measures under the layer's minimum
space. The carrier deck reports that as TM2.b/M{n}.b, and it is a real
manufacturability finding, not a rule artefact: it is a notch in one piece of
copper, narrower than the process can resolve.

The heal fills those notches. The two properties that matter are that it
closes them and that it never brings two separate nets closer than the
minimum space, let alone joins them. notch_check (gaps inside one polygon)
keeps it from closing the legal gap between two nets, which an oversize/
undersize closing would weld; the clearance cut keeps its over-filled boxes
off copper they do not belong to. The patches also have to be legal on their
own, because 3_1_offgrid.drc and 3_2_angle.drc read raw polygons.
"""
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
# Every case draws with the interposer PDK's .lyp, a sibling tree.
pytestmark = pytest.mark.needs_sibling_sources

TM2 = (134, 0)
TM2_SPACE_UM = GDSGenerator._DEFAULT_NOTCH_SPACE['TM2_b']


@pytest.fixture(scope="module", autouse=True)
def _require_lyp():
    if not LYP_PATH.exists():
        pytest.skip(f"interposer LYP not found: {LYP_PATH}")


@pytest.fixture
def gen():
    return GDSGenerator(LayerMap(str(LYP_PATH)), "TOP", "METRIC", [], None)


def _um(gen, v):
    return int(round(v / gen.layout.dbu))


def _layer(gen, spec=TM2):
    return gen.layout.layer(*spec)


def _insert(gen, region, spec=TM2, cell=None):
    (cell or gen.top_cell).shapes(_layer(gen, spec)).insert(region)


def _merged(gen, spec=TM2):
    return db.Region(gen.top_cell.begin_shapes_rec(_layer(gen, spec))).merged()


def _notch_count(gen, spec=TM2, space_um=TM2_SPACE_UM):
    return _merged(gen, spec).notch_check(
        space_um / gen.layout.dbu, False, db.Metrics.Euclidian).count()


def _gap_between_pieces(gen, spec=TM2):
    """Smallest gap between separate pieces of copper, in dbu, or None."""
    gaps = _merged(gen, spec).isolated_check(
        _um(gen, 10), False, db.Metrics.Euclidian)
    return min((e.distance() for e in gaps.each()), default=None)


def _heal_patches(gen, spec=TM2):
    cells = [c for c in gen.layout.each_cell()
             if c.name.endswith("_NOTCH_HEAL")]
    if not cells:
        return []
    return list(db.Region(cells[0].shapes(_layer(gen, spec))).merged().each())


def _landing(gen):
    """A 50 um round landing with a trace entering 0.72 um off its axis.

    The trace's top edge meets the arc at about 88 degrees, a wedge only the
    space rule sees, so the heal has to close it. This is the landing mouth
    of the mixed-die board where an uncut patch stopped 0.8 um from another
    net's via cover, moved to the origin.
    """
    u = lambda v: _um(gen, v)  # noqa: E731
    disc = db.Region(db.Polygon.ellipse(
        db.Box(-u(25), -u(25), u(25), u(25)), 256))
    trace = db.Region(db.Box(0, -u(2.72), u(35), -u(0.72)))
    return (disc + trace).merged()


def _slotted_pad(gen, slot_width_um):
    """A 20x20 um pad with a slot cut into it, i.e. one polygon with a notch."""
    u = lambda v: _um(gen, v)  # noqa: E731
    body = db.Region(db.Box(0, 0, u(20), u(20)))
    slot = db.Region(db.Box(u(10), u(5), u(10 + slot_width_um), u(20) + 1))
    return body - slot


def test_notch_narrower_than_the_rule_is_closed(gen):
    _insert(gen, _slotted_pad(gen, 1.0))
    assert _notch_count(gen) > 0, "fixture must start with a real notch"

    assert gen._heal_notches() == 0
    assert _notch_count(gen) == 0


def test_notch_wider_than_the_rule_is_left_alone(gen):
    _insert(gen, _slotted_pad(gen, TM2_SPACE_UM * 2))
    before = _merged(gen).area()

    assert gen._heal_notches() == 0
    assert _merged(gen).area() == before, "a legal slot must survive the heal"


def test_two_nets_at_exactly_the_minimum_space_are_not_bridged(gen):
    """The property an oversize/undersize closing would violate.

    Two pieces of copper at exactly the minimum space are legal and belong to
    different nets. A closing by half the rule on each side welds them into
    one; notch_check never sees between them. The slotted pad shares the
    layer so the heal is genuinely running on this geometry.
    """
    u = lambda v: _um(gen, v)  # noqa: E731
    _insert(gen, _slotted_pad(gen, 1.0))
    _insert(gen, db.Region(db.Box(u(20 + TM2_SPACE_UM), 0,
                                  u(40 + TM2_SPACE_UM), u(20))))
    assert _merged(gen).count() == 2

    gen._heal_notches()

    healed = _merged(gen)
    assert healed.count() == 2, "the heal welded two separate nets"
    gap = healed.space_check(u(TM2_SPACE_UM), False, db.Metrics.Euclidian)
    assert gap.count() == 0, "the heal pushed two nets below the minimum space"


def test_patches_are_on_grid_and_only_0_45_90(gen):
    """3_1_offgrid.drc and 3_2_angle.drc read raw polygons, so each patch has
    to be legal by itself, whatever the shape it is closing against."""
    _insert(gen, _slotted_pad(gen, 1.0))
    gen._heal_notches()

    heal_cells = [c for c in gen.layout.each_cell()
                  if c.name.endswith("_NOTCH_HEAL")]
    assert len(heal_cells) == 1, "the heal must land in its own named cell"
    grid = gen.grid_dbu
    shapes = list(heal_cells[0].shapes(_layer(gen)).each())
    assert shapes, "expected at least one patch"
    for shape in shapes:
        poly = shape.polygon
        points = list(poly.each_point_hull())
        for p in points:
            assert p.x % grid == 0 and p.y % grid == 0, f"offgrid vertex {p}"
        for a, b in zip(points, points[1:] + points[:1]):
            dx, dy = abs(b.x - a.x), abs(b.y - a.y)
            assert dx == 0 or dy == 0 or dx == dy, f"edge {a}-{b} is not 0/45/90"


def test_foreign_geometry_is_not_reshaped(gen):
    """An imported die is delivered as-is; the same rule the grid pass follows."""
    u = lambda v: _um(gen, v)  # noqa: E731
    die = gen.layout.create_cell("IMPORTED_DIE")
    gen.top_cell.insert(db.CellInstArray(die.cell_index(), db.Trans()))
    _insert(gen, _slotted_pad(gen, 1.0), cell=die)
    gen._foreign_cells = {die.cell_index()}
    before = die.bbox_per_layer(_layer(gen)).area()

    assert gen._heal_notches() == 0, "a foreign notch is not ours to close"
    assert die.bbox_per_layer(_layer(gen)).area() == before
    assert not [c for c in gen.layout.each_cell()
                if c.name.endswith("_NOTCH_HEAL")]


def _assert_rectilinear_on_grid(gen, patches):
    grid = gen.grid_dbu
    for poly in patches:
        pts = list(poly.each_point_hull())
        for a, b in zip(pts, pts[1:] + pts[:1]):
            assert a.x == b.x or a.y == b.y, f"non-axis edge {a}-{b}"
            assert a.x % grid == 0 and a.y % grid == 0, f"offgrid vertex {a}"


def test_patches_stay_axis_aligned(gen):
    """Not a style rule. A 45 degree chamfer on a patch is legal in isolation
    and closes the same notches with less copper, but it puts vertices off the
    5 nm grid in the MERGED copper where it meets the diagonal traces, and the
    deck reports those: 4 offgrid and 4 Angle45 markers on the reference
    board, none at a drawn vertex, in flat, deep and tiling alike. Axis-
    aligned edges do not do that. If this test is ever relaxed, the offgrid
    and angle decks have to be re-run on a real board, not just this fixture.

    A patch cut back from other copper is an L or a similar rectilinear
    shape rather than a rectangle; what it may never have is an edge off
    the axes or a vertex off the grid.
    """
    _insert(gen, _slotted_pad(gen, 1.0))
    gen._heal_notches()
    patches = _heal_patches(gen)
    assert patches
    _assert_rectilinear_on_grid(gen, patches)


def test_patch_keeps_the_space_from_another_nets_via_cover(gen):
    """The uncut box over-fills the landing mouth and stops 0.8 um short of a
    via cover that is itself 2.15 um from the landing, legal copper. Cut back
    by the cover's box grown by the space, the patch becomes an L that still
    closes the wedge."""
    u = lambda v: _um(gen, v)  # noqa: E731
    _insert(gen, _landing(gen))
    _insert(gen, db.Region(db.Box(u(27), u(2.8), u(29), u(4.8))))
    assert _notch_count(gen) > 0, "fixture must start with a real notch"
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM), "fixture must be legal"

    assert gen._heal_notches() == 0
    assert _notch_count(gen) == 0
    assert _merged(gen).count() == 2
    assert _gap_between_pieces(gen) == u(TM2_SPACE_UM), \
        "the cut stops at the space rule, not at the heal margin"
    patches = _heal_patches(gen)
    assert any(p.num_points_hull() > 4 for p in patches), "expected an L"
    _assert_rectilinear_on_grid(gen, patches)


def test_patch_never_welds_a_legal_via_cover(gen):
    """A cover 2.04 um from the landing is legal, and the uncut box runs
    into it. Filled as it stands, the patch joins the two nets into one
    polygon: a short no deck reports, since there is no gap left to
    measure."""
    u = lambda v: _um(gen, v)  # noqa: E731
    _insert(gen, _landing(gen))
    _insert(gen, db.Region(db.Box(u(27.2), u(1.32), u(29.2), u(3.32))))
    assert _merged(gen).count() == 2
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM), "fixture must be legal"

    assert gen._heal_notches() == 0
    assert _merged(gen).count() == 2, "the heal welded two nets"
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM)


def test_a_notch_the_cut_cannot_close_is_reported_open(gen, capsys):
    """A via cover sits diagonally off the landing mouth, 2.02 um away. The
    cut box around it reaches 2 um in x and y, which covers the wedge, so
    nothing that closes the notch survives the cut. The notch stays open, the
    heal says where, and the carrier DRC reports it: an honest failure
    instead of a patch welding the cover, which is what the uncut box does
    here."""
    u = lambda v: _um(gen, v)  # noqa: E731
    _insert(gen, _landing(gen))
    _insert(gen, db.Region(db.Box(u(26.97), u(1.62), u(28.97), u(3.62))))
    wedge = _merged(gen).notch_check(
        u(TM2_SPACE_UM), False, db.Metrics.Euclidian).bbox()
    assert not wedge.empty(), "fixture must start with a real notch"
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM), "fixture must be legal"

    assert gen._heal_notches() == 1
    out = capsys.readouterr()
    assert "1 region(s) unclosed" in out.out
    where = re.search(r"unclosed notch near \(([-\d.]+), ([-\d.]+)\) um",
                      out.out)
    assert where, out.out
    assert wedge.contains(db.Point(u(float(where[1])), u(float(where[2])))), \
        "the reported notch must be the drawn one"
    assert "still open after the heal" in out.err
    assert _merged(gen).count() == 2
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM)


def test_a_wedge_the_heal_itself_opens_is_reported_too(gen, capsys):
    """The drawn notch at the pad closes, and the patch that closes it opens
    a smaller wedge further up the 45 degree trace, next to a parallel trace
    of another net. The bbox of a diagonal piece is far larger than the
    piece, so its cut blocks the patch that wedge needs, and the wedge is
    reported like a drawn notch. Those cut boxes are off grid before
    snapping, which is what the last line checks."""
    u = lambda v: _um(gen, v)  # noqa: E731
    pad = db.Region(db.Box(0, 0, u(20), u(20)))
    trace = db.Region(db.Path([db.Point(u(10), u(20)),
                               db.Point(u(30), u(40))], u(2)))
    other = db.Region(db.Path([db.Point(u(19), u(23)),
                               db.Point(u(25), u(29))], u(2)))
    _insert(gen, pad + trace)
    _insert(gen, other)
    drawn = _merged(gen).notch_check(
        u(TM2_SPACE_UM), False, db.Metrics.Euclidian).bbox()
    assert not drawn.empty(), "fixture must start with a real notch"
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM), "fixture must be legal"

    assert gen._heal_notches() == 1
    out = capsys.readouterr()
    assert "1 region(s) unclosed" in out.out
    where = re.search(r"unclosed notch near \(([-\d.]+), ([-\d.]+)\) um",
                      out.out)
    assert where, out.out
    assert not drawn.contains(db.Point(u(float(where[1])),
                                       u(float(where[2])))), \
        "the open wedge must be the heal's, not the drawn one"
    assert "still open after the heal" in out.err
    still = _merged(gen).notch_check(
        u(TM2_SPACE_UM), False, db.Metrics.Euclidian)
    assert not still.is_empty()
    assert not any(p.bbox().overlaps(drawn) for p in still.each()), \
        "the drawn notch must be closed"
    assert _merged(gen).count() == 2
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM)
    _assert_rectilinear_on_grid(gen, _heal_patches(gen))


def test_the_cut_leaves_no_sliver_under_the_width_rule(gen):
    """A cover 2.0 um above the slotted pad, starting 10.3 um in, cuts the
    patch down to the slot and a 0.3 x 2.0 um finger of its overhang, which
    would stand on the pad edge narrower than the width rule, reported by
    no heal line. Opening each kept patch by half the space removes it."""
    u = lambda v: _um(gen, v)  # noqa: E731
    width = lambda: _merged(gen).width_check(  # noqa: E731
        u(TM2_SPACE_UM), False, db.Metrics.Euclidian)
    _insert(gen, _slotted_pad(gen, 1.0))
    _insert(gen, db.Region(db.Box(u(10.3), u(22), u(30), u(30))))
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM), "fixture must be legal"
    assert width().is_empty()

    assert gen._heal_notches() == 0
    assert _merged(gen).count() == 2
    assert width().is_empty()
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM)


def test_the_cut_uses_the_space_rule_not_the_heal_margin(gen):
    """One number serves the cut, the recount and the carrier deck: the
    space rule, not the heal's 10 percent fill margin. A cover 2.1 um from
    the uncut patch is already clear of it, so the patch stays whole."""
    u = lambda v: _um(gen, v)  # noqa: E731
    _insert(gen, _landing(gen))
    _insert(gen, db.Region(db.Box(u(27), u(4.1), u(29), u(6.1))))

    assert gen._heal_notches() == 0
    assert _notch_count(gen) == 0
    assert [p.num_points_hull() for p in _heal_patches(gen)] == [4]
    assert _gap_between_pieces(gen) == u(2.1)


def test_patches_of_one_pass_keep_the_space_between_them(gen):
    """Two pads 2.5 um apart, each with a slot near the facing edge. Each
    patch spills 0.5 um past its own pad and keeps the space from the other
    pad, but cut only against the drawn copper the two would end 1.5 um
    apart. Each patch placed joins the copper the next one is cut against."""
    u = lambda v: _um(gen, v)  # noqa: E731
    def slotted(x0, slot_x):
        body = db.Region(db.Box(u(x0), 0, u(x0 + 20), u(20)))
        return body - db.Region(db.Box(u(slot_x), u(5),
                                       u(slot_x + 1), u(20) + 1))
    _insert(gen, slotted(0, 17.5))
    _insert(gen, slotted(22.5, 24))
    assert _notch_count(gen) > 0
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM)

    assert gen._heal_notches() == 0
    assert _notch_count(gen) == 0
    assert _merged(gen).count() == 2
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM)


def test_a_patch_that_would_join_two_nets_is_dropped(gen, capsys):
    """Two pads at the minimum space, each with a slot opening toward the
    other. Their patch boxes fuse into one that carries notch edges of both
    pads, so neither counts as other copper and the cut removes nothing.
    Placed, that box would weld the two nets; it is dropped instead, and the
    notches it would have closed are reported."""
    u = lambda v: _um(gen, v)  # noqa: E731
    a = db.Region(db.Box(0, 0, u(20), u(20))) - db.Region(
        db.Box(u(15), u(9.5), u(20) + 1, u(10.5)))
    b = db.Region(db.Box(u(22), 0, u(42), u(20))) - db.Region(
        db.Box(u(22) - 1, u(9.5), u(27), u(10.5)))
    _insert(gen, a)
    _insert(gen, b)
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM), "fixture must be legal"

    assert gen._heal_notches() == 1
    assert _merged(gen).count() == 2, "the heal welded two nets"
    assert "1 region(s) unclosed" in capsys.readouterr().out


def test_a_patch_owns_what_it_covers_not_what_its_bbox_covers(gen):
    """A's L-shaped slot and its straight slot give two patch boxes that
    overlap into one patch, and the bbox of that patch spans B's slot
    without reaching it. B is still other copper: counted as owned, it would
    give the patch a second owner and get it rejected, or, without that
    rule, not cut it, leaving it 1 um short of B. One pass, because in a
    second one B's slot is already closed and the patch has one owner
    either way."""
    u = lambda v: _um(gen, v)  # noqa: E731
    box = lambda x1, y1, x2, y2: db.Region(  # noqa: E731
        db.Box(u(x1), u(y1), u(x2), u(y2)))
    a = (box(0, 0, 7, 40) + box(0, 33, 40, 40) + box(20, -20, 27, 40)
         - box(3, 3, 4, 37) - box(3, 36, 37, 37) - box(23, -17, 24, 17))
    b = box(29, -19, 45, 0) - box(31, -10, 37, -9)
    _insert(gen, a)
    _insert(gen, b)
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM), "fixture must be legal"
    gen.NOTCH_HEAL_MAX_ITERS = 1

    assert gen._heal_notches() == 0
    assert _merged(gen).count() == 2
    assert _gap_between_pieces(gen) >= u(TM2_SPACE_UM)


def test_a_patch_with_two_owners_is_rejected(gen):
    """A touches the patch with its slot and B with the mouth of its own, so
    both own it and neither is cut against. C, a third net, cuts the part
    that meets B, and what is left touches A alone, 0.5 um from B: nothing
    checked that space. A patch with two owners is rejected before the cut.
    No fixture tried made _notch_patches produce one, so the patch is
    handed to _clear_other_copper directly."""
    u = lambda v: _um(gen, v)  # noqa: E731
    box = lambda x1, y1, x2, y2: db.Region(  # noqa: E731
        db.Box(u(x1), u(y1), u(x2), u(y2)))
    a = box(0, 0, 20, 20) - db.Region(
        db.Box(u(15), u(9.5), u(20) + 1, u(10.5)))
    b = box(22, 12.5, 40, 30) - db.Region(
        db.Box(u(25), u(12.5) - 1, u(26), u(20)))
    c = box(23.5, 5, 28, 10.5)
    copper = (a + b + c).merged()
    space = u(TM2_SPACE_UM)
    assert copper.count() == 3
    assert copper.isolated_check(
        space, False, db.Metrics.Euclidian).is_empty(), "fixture must be legal"
    notches = copper.notch_check(
        space * gen.NOTCH_HEAL_MARGIN, False, db.Metrics.Euclidian)

    kept = gen._clear_other_copper(box(13, 7.5, 27, 12.5), copper,
                                   notches, space)
    assert kept.is_empty()


def test_the_recount_counts_at_the_space_rule(gen, capsys):
    """The heal fills to 2.2 um so its result clears the rule, but only a gap
    under 2.0 um is a violation. With no pass allowed, a 1.0 um and a 2.1 um
    slot both stay as drawn, and only the first is reported."""
    u = lambda v: _um(gen, v)  # noqa: E731
    _insert(gen, _slotted_pad(gen, 1.0))
    _insert(gen, _slotted_pad(gen, 2.1).moved(u(40), 0))
    gen.NOTCH_HEAL_MAX_ITERS = 0

    assert gen._heal_notches() == 1
    assert "0 patch(es), 1 region(s) unclosed" in capsys.readouterr().out


def test_wire_bond_pad_notches_are_left_to_the_pad_rules(gen):
    """Widening a trace where it crosses a bond pad marker lengthens the edge
    Pad.fR measures the 7 um exit band from, so the band that has to stay
    covered grows and the heal trades a notch for a bigger exit violation.
    Measured on the reference board: healing there moved Pad.fR_TM2 from 27
    markers to 30 and the uncovered area from 450 to 466 um2. That geometry is
    the pad cell's, so the heal stays out."""
    _insert(gen, _slotted_pad(gen, 1.0))
    _insert(gen, db.Region(db.Box(0, 0, _um(gen, 20), _um(gen, 20))),
            spec=GDSGenerator.NOTCH_HEAL_KEEPOUT)
    assert _notch_count(gen) > 0

    residual = gen._heal_notches()

    assert not [c for c in gen.layout.each_cell()
                if c.name.endswith("_NOTCH_HEAL")], "heal entered a bond pad"
    assert _notch_count(gen) > 0, "fixture must still have its notch"
    assert residual == 0, "a keepout notch is out of scope, not a failure"


def test_keepout_does_not_shield_cu_pillar_pads(gen):
    """Only the wire-bond datatype is excluded. Cu-pillar pads carry the
    :pillar datatype and are where most of the real notches are."""
    assert GDSGenerator.NOTCH_HEAL_KEEPOUT == (41, 0)
    _insert(gen, _slotted_pad(gen, 1.0))
    _insert(gen, db.Region(db.Box(0, 0, _um(gen, 20), _um(gen, 20))),
            spec=(41, 35))

    assert gen._heal_notches() == 0
    assert _notch_count(gen) == 0


def test_clean_layout_creates_no_heal_cell(gen):
    _insert(gen, db.Region(db.Box(0, 0, _um(gen, 20), _um(gen, 20))))

    assert gen._heal_notches() == 0
    assert not [c for c in gen.layout.each_cell()
                if c.name.endswith("_NOTCH_HEAL")]


def test_space_rules_come_from_the_tech_json(tmp_path):
    """The heal must fill to the deck's numbers, not to a private copy."""
    tech = tmp_path / "tech.json"
    tech.write_text('{"rules": {"Mn_b": 0.5, "TM1_b": 1.0, "TM2_b": 3.0}}')

    assert GDSGenerator._load_notch_space(str(tech)) == {
        'Mn_b': 0.5, 'TM1_b': 1.0, 'TM2_b': 3.0}


def test_missing_tech_json_falls_back_instead_of_aborting(tmp_path):
    """A heal that cannot read its rules leaves the layout as it was; the
    carrier DRC still reports whatever is there, so this is not worth an
    abort."""
    assert GDSGenerator._load_notch_space(None) == \
        GDSGenerator._DEFAULT_NOTCH_SPACE
    assert GDSGenerator._load_notch_space(str(tmp_path / "absent.json")) == \
        GDSGenerator._DEFAULT_NOTCH_SPACE


def test_every_healed_layer_maps_to_a_known_rule_key():
    """A layer added to NOTCH_HEAL_LAYERS without its rule value would raise a
    KeyError mid-write; catch it here instead."""
    for spec, key in GDSGenerator.NOTCH_HEAL_LAYERS.items():
        assert key in GDSGenerator._DEFAULT_NOTCH_SPACE, spec


def test_heal_grid_matches_the_manufacturing_grid(gen):
    assert gen.grid_dbu == int(round(MANUFACTURING_GRID_NM * 0.001
                                     / gen.layout.dbu))
