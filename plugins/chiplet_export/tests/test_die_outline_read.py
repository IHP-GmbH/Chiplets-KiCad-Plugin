# SPDX-License-Identifier: GPL-3.0-or-later
"""The die outline read (die_outline_gds) on synthetic GDS files.

One declared layer of a die's own GDS, collected through the hierarchy under
its one top cell and merged, must be exactly one axis-aligned rectangle with a
nonzero width and height. Everything else refuses with a cause naming the
layer and the file, and no bounding box is ever substituted. The flag parsers
are strict in the same way: a malformed token is an error naming it.
"""
import os
from decimal import Decimal

import pytest

db = pytest.importorskip("klayout.db")

import die_outline_gds as d  # noqa: E402

OUTLINE = (39, 4)


def _gds(path, build, dbu=0.001):
    """Write a GDS whose top cell TOP is filled by build(layout, top)."""
    layout = db.Layout()
    layout.dbu = dbu
    top = layout.create_cell("TOP")
    build(layout, top)
    layout.write(str(path))
    return str(path)


def _read(path, layer=OUTLINE):
    return d.DieGdsReader().outline(path, layer)


def _refusal(path, layer=OUTLINE):
    with pytest.raises(d.DieGdsError) as info:
        _read(path, layer)
    return str(info.value)


def _bow_tie(*corners):
    """A polygon over the four corners in this order, crossing itself: its two
    lobes wind opposite ways, so its own area() is 0."""
    return db.Polygon([db.Point(*corner) for corner in corners], raw=True)


# ---------------------------------------------------------------------------
# What is accepted
# ---------------------------------------------------------------------------

def test_one_top_cell_box(tmp_path):
    path = _gds(tmp_path / "die.gds", lambda ly, top: top.shapes(
        ly.layer(*OUTLINE)).insert(db.Box(-389000, -1298798, 389000,
                                          1361202)))
    outline = _read(path)
    assert outline.box_dbu == (-389000, -1298798, 389000, 1361202)
    assert (outline.width_um, outline.height_um) == (Decimal("778.000"),
                                                     Decimal("2660.000"))
    assert outline.gds.top_cell == "TOP"
    assert outline.gds.dbu == Decimal("0.001")


def test_outline_in_a_child_cell_only(tmp_path):
    """The outline is read through the hierarchy, in the top cell's frame:
    nothing on the layer in TOP itself, one box in a child placed with an
    offset (the child's own coordinates are not the answer)."""
    def build(ly, top):
        child = ly.create_cell("SEAL")
        child.shapes(ly.layer(*OUTLINE)).insert(db.Box(0, 0, 1000, 2000))
        top.insert(db.CellInstArray(child.cell_index(),
                                    db.Trans(db.Vector(500, 300))))
        top.shapes(ly.layer(134, 0)).insert(db.Box(-5000, -5000, 5000, 5000))
    outline = _read(_gds(tmp_path / "die.gds", build))
    assert outline.box_dbu == (500, 300, 1500, 2300)
    assert outline.gds.top_cell == "TOP"


def test_abutting_pieces_merge_into_one_rectangle(tmp_path):
    def build(ly, top):
        shapes = top.shapes(ly.layer(*OUTLINE))
        shapes.insert(db.Box(0, 0, 1000, 1000))
        shapes.insert(db.Box(1000, 0, 3000, 1000))
        shapes.insert(db.Box(500, 200, 900, 800))   # inside, overlapping
    assert _read(_gds(tmp_path / "die.gds", build)).box_dbu == (0, 0, 3000,
                                                                1000)


def test_texts_do_not_count_but_do_not_disturb(tmp_path):
    def build(ly, top):
        shapes = top.shapes(ly.layer(*OUTLINE))
        shapes.insert(db.Box(0, 0, 1000, 1000))
        shapes.insert(db.Text("SEAL", db.Trans(db.Vector(50000, 50000))))
    assert _read(_gds(tmp_path / "die.gds", build)).box_dbu == (0, 0, 1000,
                                                                1000)


def test_a_shape_of_zero_area_beside_the_outline_does_not_count(tmp_path):
    # It adds no area to the merge; 0.30.5 and 0.30.12 both drop it.
    def build(ly, top):
        shapes = top.shapes(ly.layer(*OUTLINE))
        shapes.insert(db.Box(0, 0, 778000, 1000000))
        shapes.insert(db.Box(900000, 0, 900000, 1000))
    assert _read(_gds(tmp_path / "die.gds", build)).box_dbu == (0, 0, 778000,
                                                                1000000)


def test_self_intersecting_polygons_count_by_their_merged_area(tmp_path):
    """Two bow-ties report no area each and merge to a clean square."""
    def build(ly, top):
        shapes = top.shapes(ly.layer(*OUTLINE))
        shapes.insert(_bow_tie((0, 0), (1000, 1000), (1000, 0), (0, 1000)))
        shapes.insert(_bow_tie((0, 0), (1000, 0), (0, 1000), (1000, 1000)))
    assert _read(_gds(tmp_path / "die.gds", build)).box_dbu == (0, 0, 1000,
                                                                1000)


# ---------------------------------------------------------------------------
# What refuses (never a bbox)
# ---------------------------------------------------------------------------

def test_two_separate_shapes_refuse(tmp_path):
    def build(ly, top):
        shapes = top.shapes(ly.layer(*OUTLINE))
        shapes.insert(db.Box(0, 0, 1000, 1000))
        shapes.insert(db.Box(2000, 0, 3000, 1000))
    cause = _refusal(_gds(tmp_path / "die.gds", build))
    assert "merges to 2 separate shapes" in cause
    assert "No bounding box is substituted" in cause


def test_corner_touching_boxes_refuse(tmp_path):
    """Two boxes that meet at one corner merge into one polygon, which is not
    a rectangle. Their bbox would be, and must not be used."""
    def build(ly, top):
        shapes = top.shapes(ly.layer(*OUTLINE))
        shapes.insert(db.Box(0, 0, 1000, 1000))
        shapes.insert(db.Box(1000, 1000, 2000, 2000))
    cause = _refusal(_gds(tmp_path / "die.gds", build))
    assert "merges to one shape that is not a rectangle" in cause
    assert "No bounding box is substituted" in cause


@pytest.mark.parametrize("shape,detail", [
    (db.Polygon([db.Point(0, 0), db.Point(2000, 0), db.Point(2000, 1000),
                 db.Point(1000, 1000), db.Point(1000, 2000),
                 db.Point(0, 2000)]), "6 vertices, 0 holes"),
    (db.Polygon([db.Point(0, 0), db.Point(2000, 0), db.Point(0, 2000)]),
     "not Manhattan"),
])
def test_one_shape_that_is_not_a_rectangle_refuses(tmp_path, shape, detail):
    path = _gds(tmp_path / "die.gds",
                lambda ly, top: top.shapes(ly.layer(*OUTLINE)).insert(shape))
    cause = _refusal(path)
    assert "not a rectangle" in cause and detail in cause


def test_a_ring_refuses(tmp_path):
    ring = db.Polygon(db.Box(0, 0, 3000, 3000))
    ring.insert_hole(db.Box(1000, 1000, 2000, 2000))
    path = _gds(tmp_path / "die.gds",
                lambda ly, top: top.shapes(ly.layer(*OUTLINE)).insert(ring))
    assert "4 vertices, 1 holes" in _refusal(path)


@pytest.mark.parametrize("shape", [
    db.Box(0, 0, 0, 1000),
    db.Path([db.Point(0, 0), db.Point(0, 1000)], 0),
], ids=["zero-width-box", "zero-width-path"])
def test_zero_width_refuses(tmp_path, shape):
    path = _gds(tmp_path / "die.gds",
                lambda ly, top: top.shapes(ly.layer(*OUTLINE)).insert(shape))
    # One cause on 0.30.5, which keeps such a shape as a zero-width box, and on
    # 0.30.12, which drops it.
    assert "carries only shapes of zero area (0 x 1000 DBU" in _refusal(path)


def test_several_shapes_of_zero_area_refuse(tmp_path):
    def build(ly, top):
        shapes = top.shapes(ly.layer(*OUTLINE))
        shapes.insert(db.Box(0, 0, 0, 1000))
        shapes.insert(db.Path([db.Point(5000, 0), db.Point(5000, 1000),
                               db.Point(6000, 1000)], 0))
    path = _gds(tmp_path / "die.gds", build)
    assert "carries only shapes of zero area (6000 x 1000 DBU" in _refusal(path)


def test_a_bow_tie_refuses_as_not_a_rectangle(tmp_path):
    """It reports no area of its own; its merge has area and is not a box."""
    shape = _bow_tie((0, 0), (1000, 1000), (1000, 0), (0, 1000))
    path = _gds(tmp_path / "die.gds",
                lambda ly, top: top.shapes(ly.layer(*OUTLINE)).insert(shape))
    cause = _refusal(path)
    assert "not a rectangle" in cause and "zero area" not in cause


def test_layer_absent_refuses(tmp_path):
    path = _gds(tmp_path / "die.gds", lambda ly, top: top.shapes(
        ly.layer(134, 0)).insert(db.Box(0, 0, 1000, 1000)))
    cause = _refusal(path)
    assert "die outline layer 39/4 is not in" in cause
    assert path in cause


def test_layer_with_texts_only_refuses(tmp_path):
    path = _gds(tmp_path / "die.gds", lambda ly, top: top.shapes(
        ly.layer(*OUTLINE)).insert(db.Text("SEAL", db.Trans())))
    assert "carries no shape under top cell TOP" in _refusal(path)


def test_missing_gds_refuses(tmp_path):
    path = os.path.realpath(str(tmp_path / "nope.gds"))
    assert _refusal(path) == "GDS %s does not exist" % path


def test_unreadable_gds_refuses(tmp_path):
    path = tmp_path / "garbage.gds"
    path.write_bytes(b"this is not a GDS stream\n" * 8)
    assert _refusal(str(path)).startswith(
        "cannot read GDS %s: " % os.path.realpath(str(path)))


def test_directory_refuses(tmp_path):
    assert _refusal(str(tmp_path)) == ("GDS %s is not a file"
                                       % os.path.realpath(str(tmp_path)))


@pytest.mark.parametrize("dbu", [0.005, 0.0005, 1e-6])
def test_dbu_other_than_the_assemblys_refuses(tmp_path, dbu):
    """The complete GDS imports a die into its 0.001 layout without
    rescaling, so a die at another dbu is refused rather than sized, for an
    outline read and for a top cell read (an exempt die) alike."""
    path = _gds(tmp_path / "die.gds", lambda ly, top: top.shapes(
        ly.layer(*OUTLINE)).insert(db.Box(0, 0, 200000, 1000)), dbu=dbu)
    cause = "GDS %s has a dbu of %s um, not the 0.001 um of the assembly" % (
        os.path.realpath(path), Decimal(repr(dbu)))
    assert _refusal(path).startswith(cause)
    with pytest.raises(d.DieGdsError, match="has a dbu of"):
        d.DieGdsReader().gds(path)
    # The interposer's own top cell is read at whatever dbu it has.
    assert d.read_top_cell(path) == "TOP"


def test_multiple_top_cells_refuse(tmp_path):
    """add_device would take the first parentless cell; the outline read
    will not guess which one is the die."""
    def build(ly, top):
        top.shapes(ly.layer(*OUTLINE)).insert(db.Box(0, 0, 1000, 1000))
        other = ly.create_cell("ORPHAN")
        other.shapes(ly.layer(*OUTLINE)).insert(db.Box(0, 0, 1000, 1000))
    path = _gds(tmp_path / "die.gds", build)
    cause = _refusal(path)
    assert "has 2 top cells (" in cause
    assert "TOP" in cause and "ORPHAN" in cause
    assert "exactly one top cell is required" in cause
    with pytest.raises(d.DieGdsError, match="2 top cells"):
        d.read_top_cell(path)


def test_size_the_chiplet_cannot_carry_exactly_refuses(tmp_path):
    """Unreachable at the assembly dbu (integer DBU times 0.001 has three
    decimals), so shown on a reader that accepts any dbu: the guard does not
    depend on the dbu rule to hold."""
    path = _gds(tmp_path / "die.gds", lambda ly, top: top.shapes(
        ly.layer(*OUTLINE)).insert(db.Box(0, 0, 1000001, 1000)), dbu=1e-7)
    with pytest.raises(d.DieGdsError) as info:
        d.DieGdsReader(dbu=None).outline(path, OUTLINE)
    assert "cannot carry exactly in 6 decimal places" in str(info.value)


# ---------------------------------------------------------------------------
# Exact width/height (integer DBU times the layout's dbu, as Decimals)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("box,exact_w,exact_h", [
    ((0, 0, 123457, 1000), "123.457", "1"),
    ((0, 0, 1000, 123457), "1", "123.457"),
    # Off the origin: the corners' float difference is 0.19999999999999998
    # x 0.30000000000000004; the integer width and height are exact.
    ((100, 100, 300, 400), "0.2", "0.3"),
])
def test_size_is_the_exact_decimal_product(tmp_path, box, exact_w, exact_h):
    path = _gds(tmp_path / "die.gds", lambda ly, top: top.shapes(
        ly.layer(*OUTLINE)).insert(db.Box(*box)))
    outline = _read(path)
    assert (outline.width_um, outline.height_um) == (Decimal(exact_w),
                                                     Decimal(exact_h))
    for size, exact in ((outline.width_um, exact_w),
                        (outline.height_um, exact_h)):
        assert Decimal(repr(d.exact_float(size))) == Decimal(exact)


def test_float_arithmetic_is_not_exact_for_these_boxes():
    """Why the sizes above are Decimal products: the float ones drift."""
    assert repr(123457 * 0.001) == "123.45700000000001"
    assert repr(300 * 0.001 - 100 * 0.001) == "0.19999999999999998"
    assert repr(400 * 0.001 - 100 * 0.001) == "0.30000000000000004"


def test_exact_float_carries_six_decimals_and_no_more():
    assert d.exact_float(Decimal("123.457")) == 123.457
    assert d.exact_float(Decimal("1.000001")) == 1.000001
    for value in ("1.0000001", "0.0000001"):
        with pytest.raises(ValueError):
            d.exact_float(Decimal(value))


# ---------------------------------------------------------------------------
# One read per file and layer per run
# ---------------------------------------------------------------------------

def test_reader_opens_each_file_once(tmp_path, monkeypatch):
    path = _gds(tmp_path / "die.gds", lambda ly, top: top.shapes(
        ly.layer(*OUTLINE)).insert(db.Box(0, 0, 1000, 1000)))
    missing = str(tmp_path / "missing.gds")
    opened = []
    real_open = d._open

    def counting_open(real, dbu):
        opened.append(real)
        return real_open(real, dbu)

    monkeypatch.setattr(d, "_open", counting_open)
    reader = d.DieGdsReader()
    reader.load(path, [OUTLINE])
    first = reader.outline(path, OUTLINE)
    assert reader.outline(str(tmp_path / "." / "die.gds"), OUTLINE) is first
    assert reader.gds(path).top_cell == "TOP"
    for _ in range(2):
        with pytest.raises(d.DieGdsError, match="does not exist"):
            reader.gds(missing)
    assert opened == [os.path.realpath(path), os.path.realpath(missing)]


def test_one_file_under_two_names_is_one_read(tmp_path, monkeypatch):
    """A symlink and the file it names share one read, and a cause names the
    resolved file, whichever spelling asked first."""
    real = _gds(tmp_path / "die.gds", lambda ly, top: top.shapes(
        ly.layer(*OUTLINE)).insert(db.Box(0, 0, 1000, 2000)))
    link = tmp_path / "link.gds"
    link.symlink_to(real)
    bad = tmp_path / "bad.gds"
    bad.write_bytes(b"not a GDS stream\n" * 8)
    bad_link = tmp_path / "bad_link.gds"
    bad_link.symlink_to(bad)
    opened = []
    real_open = d._open

    def counting_open(path, dbu):
        opened.append(os.path.realpath(path))
        return real_open(path, dbu)

    monkeypatch.setattr(d, "_open", counting_open)
    reader = d.DieGdsReader()
    first = reader.outline(str(link), OUTLINE)
    assert first.box_dbu == (0, 0, 1000, 2000)
    assert first.gds.path == os.path.realpath(real)
    assert reader.outline(real, OUTLINE) is first
    for spelling in (str(bad_link), str(bad)):
        with pytest.raises(d.DieGdsError) as info:
            reader.gds(spelling)
        assert str(info.value).startswith(
            "cannot read GDS %s: " % os.path.realpath(str(bad)))
    assert opened.count(os.path.realpath(real)) == 1
    assert opened.count(os.path.realpath(str(bad))) == 1


# ---------------------------------------------------------------------------
# Flag grammar
# ---------------------------------------------------------------------------

def test_parse_outline_layers():
    assert d.parse_outline_layers("U1=39/4, U2=206/0") == {
        "U1": (39, 4), "U2": (206, 0)}
    assert d.parse_outline_layers("U1=65535/65535") == {"U1": (65535, 65535)}


@pytest.mark.parametrize("spec,named", [
    ("", "''"),
    ("U1=39/4,", "''"),
    ("U1", "'U1'"),
    ("=39/4", "'=39/4'"),
    ("U1=39", "'39'"),
    ("U1=39/4/0", "'39/4/0'"),
    ("U1=-1/0", "'-1/0'"),
    ("U1=65536/0", "'65536/0'"),
    ("U1=39 /4", "'39 /4'"),
    ("U1=39/4,U1=40/0", "'U1' twice"),
])
def test_parse_outline_layers_names_the_bad_token(spec, named):
    with pytest.raises(ValueError, match="--die-outline-layers") as info:
        d.parse_outline_layers(spec)
    assert named in str(info.value)


def test_parse_outline_exempt():
    assert d.parse_outline_exempt("U3, U4") == ["U3", "U4"]


@pytest.mark.parametrize("spec,named", [
    ("", "''"),
    ("U3,,U4", "''"),
    ("U3=39/4", "'U3=39/4'"),
    ("U3,U3", "'U3' twice"),
])
def test_parse_outline_exempt_names_the_bad_token(spec, named):
    with pytest.raises(ValueError, match="--die-outline-exempt") as info:
        d.parse_outline_exempt(spec)
    assert named in str(info.value)
