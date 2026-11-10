# SPDX-License-Identifier: GPL-3.0-or-later
"""Two dies that share a cell name must each keep their own geometry.

Every die is read into the one output layout, so the cell-conflict policy
decides what happens when two independently authored dies agree on a cell name
(a standard-cell library, a "SEAL", a "TOP"). KLayout's default is AddToCell:
the incoming shapes are added INTO the existing cell, so the two dies end up
sharing one merged cell and EACH acquires the other's geometry. Neither still
matches the file its designer delivered.

The defect was first reported as "one die is wrong", and that is a property of
the comparison, not of the merge: compared by area, a die whose shared box
contains the other's looks innocent because the union is unchanged. So these
tests give the two dies DISJOINT shared geometry and assert on shape counts as
well as area. With overlapping boxes the second die passes under AddToCell and
the suite would read as if only half the defect existed.

Nothing else catches it. The assembly DRC checks the assembly, not whether each
die still equals its own GDS, and the shipped example cannot exercise it at all
because both of its dies name the same GDS file, which the template cache reads
once.

These tests fail if the RenameCell option in add_device is removed.
"""
import os
import sys
from pathlib import Path

import pytest

import klayout.db as db

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402
from hyp_to_gds import Device, GDSGenerator, LayerMap  # noqa: E402

LYP_PATH = Path(os.environ.get("INTERPOSER_LYP", h._find_default_lyp()))
# Every case draws with the interposer PDK's .lyp, a sibling tree.
pytestmark = pytest.mark.needs_sibling_sources

SHARED = "SHARED_LIB_CELL"
LAYER = (134, 0)  # TopMetal2, a layer the dies legitimately draw on

# Disjoint on purpose. See the module docstring: overlapping shared boxes make
# the containing die's area comparison pass under the broken policy.
A_SHARED = db.DBox(0, 0, 10, 10)
B_SHARED = db.DBox(500, 500, 520, 520)


@pytest.fixture(scope="module", autouse=True)
def _require_lyp():
    if not LYP_PATH.exists():
        pytest.skip(f"interposer LYP not found: {LYP_PATH}")


def _write_die(path, top_name, shared_box, own_um, *, second_layer=False):
    """A die whose top cell holds its own box and instantiates SHARED."""
    ly = db.Layout()
    ly.dbu = 0.001
    li = ly.layer(*LAYER)
    shared = ly.create_cell(SHARED)
    shared.shapes(li).insert(shared_box)
    top = ly.create_cell(top_name)
    top.shapes(li).insert(db.DBox(1000, 1000, 1000 + own_um, 1000 + own_um))
    top.insert(db.DCellInstArray(shared.cell_index(), db.DTrans()))
    if second_layer:
        top.shapes(ly.layer(135, 1)).insert(db.DBox(10, 20, 10 + own_um, 30))
    ly.write(str(path))
    return path


def _area_um2(layout, cell, layer_index):
    region = db.Region(cell.begin_shapes_rec(layer_index))
    return region.area() * layout.dbu * layout.dbu


def _shape_count(cell, layer_index):
    n = 0
    it = cell.begin_shapes_rec(layer_index)
    while not it.at_end():
        n += 1
        it.next()
    return n


def _source(path):
    """(shape count, area) of the die as its designer delivered it."""
    ly = db.Layout()
    ly.read(str(path))
    li = ly.layer(*LAYER)
    return _shape_count(ly.top_cell(), li), _area_um2(ly, ly.top_cell(), li)


def _import_both(tmp_path, flip_chip=False):
    a = _write_die(tmp_path / "vendor_a.gds", "DIE_A", A_SHARED, 100.0)
    b = _write_die(tmp_path / "vendor_b.gds", "DIE_B", B_SHARED, 200.0)

    gen = GDSGenerator(LayerMap(str(LYP_PATH)), "TOP", "METRIC", [], None)
    assert gen.add_device(Device(ref="U1", layer="TopMetal2",
                                 gds_file=str(a), x=0.0, y=0.0),
                          flip_chip=flip_chip)
    assert gen.add_device(Device(ref="U2", layer="TopMetal2",
                                 gds_file=str(b), x=0.005, y=0.0),
                          flip_chip=flip_chip)
    return gen, a, b


def _assert_die_matches_its_file(gen, template_key, path):
    layout = gen.layout
    li = layout.layer(*LAYER)
    template = gen._imported_templates[str(path.resolve())]
    want_count, want_area = _source(path)

    assert _shape_count(template, li) == want_count, (
        f"{template_key}'s imported hierarchy carries "
        f"{_shape_count(template, li)} shapes where its own GDS has "
        f"{want_count}; the other die's same-named cell was merged into it")
    assert _area_um2(layout, template, li) == pytest.approx(want_area)


def test_the_first_die_keeps_its_own_geometry(tmp_path):
    gen, a, _ = _import_both(tmp_path)
    _assert_die_matches_its_file(gen, "vendor_a", a)


def test_the_second_die_keeps_its_own_geometry(tmp_path):
    """Also a real detector, given the disjoint fixture. It was not always.

    With the shared boxes overlapping, this one passed under AddToCell and made
    the defect look one-sided.
    """
    gen, _, b = _import_both(tmp_path)
    _assert_die_matches_its_file(gen, "vendor_b", b)


@pytest.mark.parametrize("ref,stem", [("U1", "vendor_a"), ("U2", "vendor_b")])
def test_the_flip_chip_wrapper_matches_its_own_file(tmp_path, ref, stem):
    """The flip-chip path, which is the one the real dies take.

    The tests above read `_imported_templates`, the cell-level view. What the
    complete GDS actually carries for a flip-chip die is the wrapper that
    `_place_die_flipped` FLATTENS out of that template, at the moment the die
    is read. So the first die is flattened before the second is read and its
    wrapper escapes the merge, while the second die's wrapper pulls in both
    dies' shapes. Only the second die shows the defect in this view, which is
    how it was first reported from a real export.

    Without this case the suite covers only the non-flip path, where both
    wrappers instantiate the shared template and the defect is symmetric.
    Mirroring preserves count and area, so both are safe to compare.

    Be clear about which half detects anything: with the fix removed, only the
    U2 parametrisation fails. U1 passes in both worlds and is here as a
    control, to show that the fix does not disturb the die that was already
    correct.
    """
    gen, a, b = _import_both(tmp_path, flip_chip=True)
    layout = gen.layout
    li = layout.layer(*LAYER)
    source_path = a if stem == "vendor_a" else b

    wrapper = layout.cell(f"{ref}_{stem}_flipped")
    assert wrapper is not None, "the flip-chip wrapper was not created"
    want_count, want_area = _source(source_path)

    assert _shape_count(wrapper, li) == want_count, (
        f"{ref}'s flattened wrapper carries {_shape_count(wrapper, li)} shapes "
        f"where its own GDS has {want_count}; it was flattened out of a "
        f"template the other die had already been merged into")
    assert _area_um2(layout, wrapper, li) == pytest.approx(want_area)


def test_the_shared_cell_is_present_twice_rather_than_merged(tmp_path):
    """Two independent dies that agree on a name cost two cells, not one.

    Stated as its own assertion because it is the price of the fix: the
    complete GDS carries a duplicate. A future change that de-duplicates by
    name would reintroduce the corruption, and this is what would catch it.
    """
    gen, _, _ = _import_both(tmp_path)
    shared = [c.name for c in gen.layout.each_cell()
              if c.name == SHARED or c.name.startswith(SHARED + "$")]
    assert len(shared) == 2, (
        f"expected each die to keep its own {SHARED}, found {shared}")


@pytest.mark.parametrize("flip_chip", [False, True])
def test_same_filename_in_different_directories_keeps_both_dies(tmp_path,
                                                               flip_chip):
    """Ready dies both use layout.gds; a filename is not a source identity.

    Assert actual wrapper geometry, then serialize and reload it. This must
    fail even if a broken implementation hides the cache behind another API.
    Both files also share their top and subcell names, exercising RenameCell.
    """
    a_dir, b_dir = tmp_path / "driver", tmp_path / "echo"
    a_dir.mkdir()
    b_dir.mkdir()
    a = _write_die(a_dir / "layout.gds", "DIE", A_SHARED, 100.0,
                   second_layer=True)
    b = _write_die(b_dir / "layout.gds", "DIE", B_SHARED, 200.0,
                   second_layer=True)
    before = a.read_bytes(), b.read_bytes()
    gen = GDSGenerator(LayerMap(str(LYP_PATH)), "TOP", "METRIC", [], None)
    for ref, path, x in [("U1", a, 0.0), ("U2", b, 0.005)]:
        assert gen.add_device(Device(ref, "TopMetal2", str(path), x=x),
                              flip_chip=flip_chip)

    out = tmp_path / "complete.gds"
    gen.cleanup_orphan_top_cells()
    gen.write(str(out))
    result = db.Layout()
    result.read(str(out))
    suffix = "_flipped" if flip_chip else ""
    for ref, path in [("U1", a), ("U2", b)]:
        wrapper = result.cell(f"{ref}_layout{suffix}")
        assert wrapper is not None
        src = db.Layout()
        src.read(str(path))
        pairs = {(info.layer, info.datatype)
                 for layout in (src, result) for info in layout.layer_infos()}
        for pair in pairs:
            src_index = src.layer(*pair)
            result_index = result.layer(*pair)
            expected = db.Region(src.top_cell().begin_shapes_rec(src_index))
            if flip_chip:
                expected.transform(db.Trans(db.Trans.M90))  # mirror X about Y
            actual = db.Region(wrapper.begin_shapes_rec(result_index))
            assert (actual ^ expected).is_empty(), (ref, pair)
            assert _shape_count(wrapper, result_index) == _shape_count(
                src.top_cell(), src_index), (ref, pair)
    assert (a.read_bytes(), b.read_bytes()) == before


@pytest.mark.parametrize("flip_chip", [False, True])
def test_two_path_spellings_of_one_source_reuse_one_template(tmp_path,
                                                            flip_chip):
    """Normalize path spelling without giving up intentional template reuse."""
    path = _write_die(tmp_path / "layout.gds", "DIE", A_SHARED, 100.0)
    (tmp_path / "subdir").mkdir()
    alias = tmp_path / "subdir" / ".." / "layout.gds"
    gen = GDSGenerator(LayerMap(str(LYP_PATH)), "TOP", "METRIC", [], None)
    assert gen.add_device(Device("U1", "TopMetal2", str(path)),
                          flip_chip=flip_chip)
    assert gen.add_device(Device("U2", "TopMetal2", str(alias), x=0.005),
                          flip_chip=flip_chip)
    templates = [cell for cell in gen.layout.each_cell()
                 if cell.name == "DIE" or cell.name.startswith("DIE$")]
    assert len(templates) == 1, "one physical source was unnecessarily re-imported"
    suffix = "_flipped" if flip_chip else ""
    li = gen.layout.layer(*LAYER)
    a = db.Region(gen.layout.cell("U1_layout" + suffix).begin_shapes_rec(li))
    b = db.Region(gen.layout.cell("U2_layout" + suffix).begin_shapes_rec(li))
    assert (a ^ b).is_empty()
    assert a.area() > 0
