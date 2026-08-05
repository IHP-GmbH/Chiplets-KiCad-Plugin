# SPDX-License-Identifier: GPL-3.0-or-later
"""The exporter closes sub-minimum-space notches without shorting anything.

KiCad routes through the middle of a round pad and takes its first bend while
still inside it, so the trace leaves the pad almost tangentially and the wedge
between the trace edge and the pad arc measures under the layer's minimum
space. The carrier deck reports that as TM2.b/M{n}.b, and it is a real
manufacturability finding, not a rule artefact: it is a notch in one piece of
copper, narrower than the process can resolve.

The heal fills those notches. The two properties that matter are that it
closes them and that it can never bridge two separate nets, which is why it
uses notch_check (gaps inside one polygon) and not an oversize/undersize
closing (which would weld two nets sitting at exactly the minimum space).
The patches also have to be legal on their own, because 3_1_offgrid.drc and
3_2_angle.drc read raw polygons.
"""
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


def test_patches_stay_axis_aligned(gen):
    """Not a style rule. A 45 degree chamfer on a patch is legal in isolation
    and closes the same notches with less copper, but it puts vertices off the
    5 nm grid in the MERGED copper where it meets the diagonal traces, and the
    deck reports those: 4 offgrid and 4 Angle45 markers on the reference
    board, none at a drawn vertex, in flat, deep and tiling alike. Axis-
    aligned edges do not do that. If this test is ever relaxed, the offgrid
    and angle decks have to be re-run on a real board, not just this fixture.
    """
    _insert(gen, _slotted_pad(gen, 1.0))
    gen._heal_notches()
    heal = [c for c in gen.layout.each_cell()
            if c.name.endswith("_NOTCH_HEAL")][0]
    shapes = list(heal.shapes(_layer(gen)).each())
    assert shapes
    for shape in shapes:
        pts = list(shape.polygon.each_point_hull())
        assert len(pts) == 4, "a patch must stay a rectangle"
        for a, b in zip(pts, pts[1:] + pts[:1]):
            assert a.x == b.x or a.y == b.y, f"non-axis edge {a}-{b}"


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
