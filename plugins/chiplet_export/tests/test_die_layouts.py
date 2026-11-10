# SPDX-License-Identifier: GPL-3.0-or-later
"""--die-layouts: a die's full layout in the .chiplet, its abstract in the GDS.

The sidecar names, per die, its full layout and the contract's top cell and
box. The finalizer writes that layout (relative to the .chiplet when the two
share a root, never copied) and that top cell into the die component, and a
metadata entry of exactly two keys naming the abstract the assembly places.
The complete GDS and its boundary keep the abstract. The .hyp is written
before the worker and takes nothing from the flag.

Both files must span the contract's box exactly, in 0.001 um database units,
and the full layout's named cell must be one of its top cells. Every refusal
names its die, leaves the .chiplet untouched and, in a worker run, comes
before anything is written.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

db = pytest.importorskip("klayout.db")
yaml = pytest.importorskip("yaml")

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
for entry in (PLUGIN_ROOT, PLUGIN_ROOT.parent):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import die_layouts as layouts  # noqa: E402
import hyp_to_gds as h  # noqa: E402
from chiplet_export.pipeline.orchestrator import (  # noqa: E402
    ExportOptions, build_cli_args,
)

from .test_die_outline_finalize import (  # noqa: E402
    BODY, OUTLINE, _LYP, _board, _chiplet, _die, _die_gds, _run_worker,
    _tree_state, _update,
)
from .test_export_exit import PASSED, cli  # noqa: E402,F401
from .test_run_export_canonical_preservation import _run  # noqa: E402
from .test_seal_ring import ARGV_BEFORE, _timeless_sha  # noqa: E402


@pytest.fixture(autouse=True)
def _mount_plane(attach_frame_pdk):
    """Each finalize here reads the plane from the fixture PDK root."""


REFUSING = "Refusing to export: no layout, sidecar or .chiplet was written."
# The die _board draws: its body spans the top cell, its outline lies inside.
BOX = [0, 0, 300, 200]


def _full(path, top="U1_FULL", box=(0.0, 0.0, 300.0, 200.0), others=(),
          child_of=None, dbu=0.001):
    """A full layout: ``top`` spanning ``box``, and one more top cell per
    name in ``others``. ``child_of`` puts ``top`` under that parent cell."""
    layout = db.Layout()
    layout.dbu = dbu
    cell = layout.create_cell(top)
    cell.shapes(layout.layer(*BODY)).insert(db.DBox(*box))
    for name in others:
        layout.create_cell(name).shapes(layout.layer(*BODY)).insert(
            db.DBox(0.0, 0.0, 10.0, 10.0))
    if child_of is not None:
        layout.create_cell(child_of).insert(
            db.CellInstArray(cell.cell_index(), db.Trans()))
    path.parent.mkdir(parents=True, exist_ok=True)
    layout.write(str(path))
    return str(path)


def _sidecar(path, entries):
    """Write ``entries`` as the sidecar; a str is written as it is."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(entries if isinstance(entries, str)
                    else json.dumps(entries), encoding="utf-8")
    return str(path)


def _entry(layout, top_cell="U1_FULL", bbox_um=None):
    return {"layout": layout, "top_cell": top_cell,
            "bbox_um": list(BOX) if bbox_um is None else bbox_um}


# ---------------------------------------------------------------------------
# The sidecar's grammar
# ---------------------------------------------------------------------------

def test_a_sidecar_is_read_into_exact_database_units(tmp_path):
    full = _full(tmp_path / "full" / "u1.gds")
    sidecar = _sidecar(tmp_path / "die_layouts.json", '{"U1": {"layout": %s, '
                       '"top_cell": "U1_FULL", "bbox_um": [-0.001, 0, '
                       '999.999, 1E+3]}}' % json.dumps(full))
    assert layouts.load(sidecar) == {"U1": layouts.DieLayout(
        "U1", full, "U1_FULL", (-1, 0, 999999, 1000000))}


def test_an_empty_sidecar_names_no_die(tmp_path):
    assert layouts.load(_sidecar(tmp_path / "d.json", {})) == {}


@pytest.mark.parametrize("text,match", [
    ("{", r"^Die layout refused: .*d\.json: cannot read the --die-layouts "
          r"sidecar: "),
    ("[]", r"^Die layout refused: .*d\.json: the --die-layouts sidecar is "
           r"not a JSON object of die references$"),
    ('{"U1": {"layout": "/x", "top_cell": "T", "bbox_um": [0, 0, NaN, 1]}}',
     r"cannot read the --die-layouts sidecar: NaN is not a number"),
], ids=["not-json", "not-an-object", "nan"])
def test_an_unreadable_sidecar_refuses(tmp_path, text, match):
    with pytest.raises(layouts.DieLayoutRefused, match=match):
        layouts.load(_sidecar(tmp_path / "d.json", text))


def test_a_missing_sidecar_refuses(tmp_path):
    with pytest.raises(layouts.DieLayoutRefused,
                       match=r"absent\.json: cannot read the --die-layouts "
                             r"sidecar: .*No such file"):
        layouts.load(str(tmp_path / "absent.json"))


def _one(tmp_path, entry):
    """The refusal line for a sidecar holding U1: ``entry``."""
    sidecar = _sidecar(tmp_path / "d.json", {"U1": entry})
    with pytest.raises(layouts.DieLayoutRefused) as info:
        layouts.load(sidecar)
    (line,) = info.value.lines()
    return line.replace(sidecar, "SIDECAR")


@pytest.mark.parametrize("edit,match", [
    (lambda e: "U1.gds", r"its entry in SIDECAR is not an object with "
                         r"layout, top_cell, bbox_um$"),
    (lambda e: {k: v for k, v in e.items() if k != "top_cell"},
     r"its entry in SIDECAR has no top_cell$"),
    (lambda e: dict(e, dbu_um=0.001),
     r"its entry in SIDECAR has unknown keys dbu_um; it takes layout, "
     r"top_cell, bbox_um$"),
    (lambda e: dict(e, layout="full/u1.gds"),
     r"layout 'full/u1\.gds' in SIDECAR is a relative path; the full layout "
     r"is named absolute$"),
    (lambda e: dict(e, layout="${DIES}/u1.gds"),
     r"layout '\$\{DIES\}/u1\.gds' in SIDECAR is a relative path"),
    (lambda e: dict(e, layout=e["layout"] + ".absent"),
     r"layout .*u1\.gds\.absent does not exist$"),
    (lambda e: dict(e, layout=os.path.dirname(e["layout"])),
     r"layout .*full is not a file$"),
    (lambda e: dict(e, layout=7), r"SIDECAR: layout is not a path$"),
    (lambda e: dict(e, top_cell=""), r"SIDECAR: top_cell is not a cell name$"),
    (lambda e: dict(e, bbox_um=[0, 0, 300]),
     r"SIDECAR: bbox_um is not four numbers \[x_min, y_min, x_max, y_max\]$"),
    (lambda e: dict(e, bbox_um=[0, 0, "300", 200]),
     r"bbox_um is not four numbers"),
    (lambda e: dict(e, bbox_um=[0, 0, True, 200]),
     r"bbox_um is not four numbers"),
    (lambda e: dict(e, bbox_um={"x": 0}), r"bbox_um is not four numbers"),
    (lambda e: dict(e, bbox_um=[0, 0, 300.0005, 200]),
     r"SIDECAR: bbox_um value 300\.0005 is not a whole number of 0\.001 um "
     r"database units$"),
    (lambda e: dict(e, bbox_um=[300, 0, 300, 200]),
     r"SIDECAR: bbox_um \[300, 0, 300, 200\] um is not a box: x_min must be "
     r"below x_max and y_min below y_max$"),
    (lambda e: dict(e, bbox_um=[0, 200, 300, 0]), r"is not a box"),
], ids=["not-an-object", "missing-key", "unknown-key", "relative",
        "unexpanded-variable", "missing-file", "directory", "layout-type",
        "empty-top-cell", "three-numbers", "string", "bool", "object",
        "sub-dbu", "zero-width", "upside-down"])
def test_a_bad_entry_refuses(tmp_path, edit, match):
    full = _full(tmp_path / "full" / "u1.gds")
    line = _one(tmp_path, edit(_entry(full)))
    assert line.startswith("Die layout refused: U1: ")
    assert re.search(match, line), line


def test_a_key_given_twice_in_an_entry_refuses(tmp_path):
    full = _full(tmp_path / "full" / "u1.gds")
    sidecar = _sidecar(tmp_path / "d.json", '{"U1": {"layout": %s, '
                       '"top_cell": "A", "top_cell": "B", "bbox_um": '
                       '[0, 0, 300, 200]}}' % json.dumps(full))
    with pytest.raises(layouts.DieLayoutRefused,
                       match=r"^Die layout refused: U1: its entry in .*d\.json "
                             r"gives top_cell more than once$"):
        layouts.load(sidecar)


def test_a_die_named_twice_refuses_once(tmp_path):
    full = _full(tmp_path / "full" / "u1.gds")
    entry = json.dumps(_entry(full))
    sidecar = _sidecar(tmp_path / "d.json", '{"U1": %s, "U2": %s, "U1": %s, '
                       '"U1": %s}' % (entry, entry, entry, entry))
    with pytest.raises(layouts.DieLayoutRefused) as info:
        layouts.load(sidecar)
    assert info.value.lines() == [
        "Die layout refused: U1: %s names it 3 times; a die has one full "
        "layout" % sidecar]


def test_an_empty_reference_refuses(tmp_path):
    full = _full(tmp_path / "full" / "u1.gds")
    sidecar = _sidecar(tmp_path / "d.json", {"": _entry(full)})
    with pytest.raises(layouts.DieLayoutRefused,
                       match=r'^Die layout refused: "": .*d\.json has an '
                             r'empty die reference$'):
        layouts.load(sidecar)


def test_every_bad_entry_is_named_in_one_refusal(tmp_path):
    full = _full(tmp_path / "full" / "u1.gds")
    sidecar = _sidecar(tmp_path / "d.json", {
        "U1": dict(_entry(full), layout="u1.gds"),
        "U2": _entry(full),
        "U3": dict(_entry(full), bbox_um=[0, 0]),
    })
    with pytest.raises(layouts.DieLayoutRefused) as info:
        layouts.load(sidecar)
    assert [ref for ref, _cause in info.value.offenders] == ["U1", "U3"]


# ---------------------------------------------------------------------------
# The finalizer, called directly: the defence for a direct caller
# ---------------------------------------------------------------------------

def _case(tmp_path, entry_edit=None, **full_kwargs):
    """An abstract U1 (top cell DIE spanning BOX), its full layout, the
    .chiplet and the loaded sidecar."""
    abstract = _die_gds(tmp_path / "dies" / "u1.gds",
                        (40.0, 30.0, 240.0, 150.0))
    full = _full(tmp_path / "full" / "u1.gds", **full_kwargs)
    entry = _entry(full)
    if entry_edit is not None:
        entry = entry_edit(entry)
    (tmp_path / "out").mkdir()
    chiplet = _chiplet(tmp_path / "out" / "t.chiplet",
                       [("U1", {"layout": abstract})])
    sidecar = _sidecar(tmp_path / "die_layouts.json", {"U1": entry})
    return abstract, full, chiplet, layouts.load(sidecar)


def _finalize(chiplet, die_layouts):
    return _update(chiplet, die_outline_exempt=["U1"], die_layouts=die_layouts)


def test_the_die_names_its_full_layout_and_the_abstract(tmp_path, capsys):
    _abstract, _full_path, chiplet, die_layouts = _case(
        tmp_path, others=("SPARE",))
    assert _finalize(chiplet, die_layouts) is True
    die = _die(chiplet, "U1")
    assert die["layout"] == "../full/u1.gds"
    assert die["top_cell"] == "U1_FULL"
    assert die["metadata"] == {"assembly_view": "abstract",
                               "assembly_view_layout": "../dies/u1.gds"}
    assert list(die["metadata"]) == ["assembly_view", "assembly_view_layout"]
    assert not (tmp_path / "out" / "chiplets").exists()
    assert ("U1: layout ../full/u1.gds (full layout, top cell U1_FULL); the "
            "assembly view shows the abstract ../dies/u1.gds"
            in capsys.readouterr().out)


def test_a_metadata_map_already_there_keeps_its_other_keys(tmp_path):
    _abstract, _full_path, chiplet, die_layouts = _case(tmp_path)
    data = yaml.safe_load(Path(chiplet).read_text(encoding="utf-8"))
    data["components"][1]["metadata"] = {"owner": "lab",
                                         "assembly_view": "full"}
    Path(chiplet).write_text(yaml.safe_dump(data, sort_keys=False),
                             encoding="utf-8")
    assert _finalize(chiplet, die_layouts) is True
    assert _die(chiplet, "U1")["metadata"] == {
        "owner": "lab", "assembly_view": "abstract",
        "assembly_view_layout": "../dies/u1.gds"}


def test_without_the_flag_the_die_keeps_its_abstract(tmp_path):
    """None and an empty sidecar write the same bytes, and neither adds
    metadata or changes the abstract's layout and top cell."""
    abstract, _full_path, chiplet, _die_layouts = _case(tmp_path)
    original = Path(chiplet).read_bytes()
    assert _finalize(chiplet, None) is True
    without = Path(chiplet).read_bytes()
    Path(chiplet).write_bytes(original)
    assert _finalize(chiplet, {}) is True
    assert Path(chiplet).read_bytes() == without
    die = _die(chiplet, "U1")
    assert "metadata" not in die
    assert (die["layout"], die["top_cell"]) == (abstract, "DIE")


def _refused(chiplet, die_layouts, match):
    before = Path(chiplet).read_bytes()
    with pytest.raises(h.DieLayoutRefused, match=match) as info:
        _finalize(chiplet, die_layouts)
    assert Path(chiplet).read_bytes() == before
    return info.value


def test_a_reference_that_is_no_die_refuses(tmp_path):
    _abstract, full, chiplet, _die_layouts = _case(tmp_path)
    entry = layouts.DieLayout("U9", full, "U1_FULL", (0, 0, 300000, 200000))
    _refused(chiplet, {"U9": entry},
             r"^Die layout refused: U9: --die-layouts names it, but the "
             r"\.chiplet has no die component with this id$")


def test_the_interposer_is_no_die(tmp_path):
    _abstract, full, chiplet, _die_layouts = _case(tmp_path)
    entry = layouts.DieLayout("interposer", full, "U1_FULL",
                              (0, 0, 300000, 200000))
    _refused(chiplet, {"interposer": entry},
             r"the \.chiplet component with this id is of type "
             r"'interposer', not a die$")


def test_a_wire_bond_pad_is_no_die(tmp_path):
    abstract, full, _unused, _die_layouts = _case(tmp_path)
    chiplet = _chiplet(tmp_path / "out" / "pads.chiplet",
                       [("U1", {"layout": abstract}), ("J1", {})])
    entry = layouts.DieLayout("J1", full, "U1_FULL", (0, 0, 300000, 200000))
    before = Path(chiplet).read_bytes()
    with pytest.raises(h.DieLayoutRefused,
                       match=r"J1: --die-layouts names it, but an --io-pads "
                             r"entry carries this id"):
        _update(chiplet, die_outline_exempt=["U1"], io_pad_refs=["J1"],
                die_layouts={"J1": entry})
    assert Path(chiplet).read_bytes() == before


def test_a_full_layout_at_another_dbu_refuses(tmp_path):
    _abstract, full, chiplet, die_layouts = _case(tmp_path, dbu=0.005)
    _refused(chiplet, die_layouts,
             r"^Die layout refused: U1: the full layout %s has a dbu of "
             r"0\.005 um, not the 0\.001 um the contract box is compared in$"
             % os.path.realpath(full))


def test_a_full_layout_without_the_contract_cell_refuses(tmp_path):
    _abstract, full, chiplet, die_layouts = _case(
        tmp_path, top="OTHER", others=("SPARE",))
    _refused(chiplet, die_layouts,
             r"^Die layout refused: U1: the full layout %s has no cell U1_FULL "
             r"\(the contract's top_cell\); its top cells are OTHER, SPARE$"
             % os.path.realpath(full))


def test_a_contract_cell_that_is_not_a_top_cell_refuses(tmp_path):
    """The named cell is there, but another cell instantiates it."""
    _abstract, full, chiplet, die_layouts = _case(tmp_path, child_of="WRAP")
    _refused(chiplet, die_layouts,
             r"the full layout %s has the contract's top cell U1_FULL, but "
             r"another cell instantiates it, so it is not a top cell$"
             % os.path.realpath(full))


@pytest.mark.parametrize("full_box,bbox_um,full_says,abstract_says", [
    ((0.0, 0.0, 300.0, 199.999), BOX, "[0, 0, 300, 199.999] um, which differs",
     "[0, 0, 300, 200] um, which matches"),
    ((0.0, 0.0, 300.0, 200.5), [0, 0, 300, 200.5],
     "[0, 0, 300, 200.5] um, which matches",
     "[0, 0, 300, 200] um, which differs"),
    ((10.0, 0.0, 310.0, 200.0), [10, 0, 310, 200],
     "[10, 0, 310, 200] um, which matches",
     "[0, 0, 300, 200] um, which differs"),
], ids=["full-differs", "abstract-differs", "abstract-shifted"])
def test_a_box_that_matches_one_file_only_refuses(tmp_path, full_box,
                                                  bbox_um, full_says,
                                                  abstract_says):
    """Each line names both files, both boxes and which one differs."""
    abstract, full, chiplet, die_layouts = _case(
        tmp_path, entry_edit=lambda e: dict(e, bbox_um=bbox_um),
        box=full_box)
    refusal = _refused(chiplet, die_layouts, r"^Die layout refused: U1: the "
                       r"contract's bbox_um ")
    (line,) = refusal.lines()
    assert ("the full layout %s (top cell U1_FULL) spans %s"
            % (os.path.realpath(full), full_says)) in line
    assert ("the abstract %s (top cell DIE) spans %s"
            % (os.path.realpath(abstract), abstract_says)) in line
    assert "exactly in 0.001 um database units" in line


def test_a_full_layout_gone_after_the_load_refuses(tmp_path):
    _abstract, full, chiplet, die_layouts = _case(tmp_path)
    os.remove(full)
    _refused(chiplet, die_layouts,
             r"^Die layout refused: U1: full layout: GDS %s does not exist$"
             % os.path.realpath(full))


def test_every_die_is_named_in_one_refusal(tmp_path):
    _abstract, full, chiplet, _die_layouts = _case(tmp_path, dbu=0.005)
    entry = layouts.DieLayout("U1", full, "U1_FULL", (0, 0, 300000, 200000))
    refusal = _refused(chiplet, {"U9": entry, "U1": entry}, r"U9")
    assert [ref for ref, _cause in refusal.offenders] == ["U9", "U1"]


def test_a_shared_full_layout_is_opened_once(tmp_path, monkeypatch):
    abstract, full, _unused, _die_layouts = _case(tmp_path)
    chiplet = _chiplet(tmp_path / "out" / "two.chiplet", [
        ("U1", {"layout": abstract}), ("U2", {"layout": abstract})])
    entry = layouts.DieLayout("U1", full, "U1_FULL", (0, 0, 300000, 200000))
    opened = []
    real = h._die_outline._open_tops
    monkeypatch.setattr(h._die_outline, "_open_tops",
                        lambda path: opened.append(path) or real(path))
    assert _update(chiplet, die_outline_exempt=["U1", "U2"],
                   die_layouts={"U1": entry, "U2": entry}) is True
    assert opened == [os.path.realpath(full)]


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

def _worker_case(tmp_path, entry_edit=None, **full_kwargs):
    hyp, die, out, chiplet = _board(tmp_path)
    full = _full(tmp_path / "full" / "u1.gds", **full_kwargs)
    entry = _entry(full)
    if entry_edit is not None:
        entry = entry_edit(entry)
    sidecar = _sidecar(tmp_path / "die_layouts.json", {"U1": entry})
    return hyp, die, out, chiplet, sidecar


def _cells(path):
    layout = db.Layout()
    layout.read(path)
    return {cell.name for cell in layout.each_cell()}


def test_worker_writes_the_full_layout_and_composes_the_abstract(tmp_path):
    """The flag moves the .chiplet's layout, top_cell and metadata only: the
    boundary manifest is the one a run without it writes, and the complete
    GDS composes the abstract, not the full layout."""
    flags = ["--die-outline-layers", "U1=%d/%d" % OUTLINE]
    plain = tmp_path / "plain"
    plain.mkdir()
    hyp0, _die0, out0, chiplet0 = _board(plain)
    assert _run_worker(plain, hyp0, out0, chiplet0, flags).returncode == 0

    hyp, _die_path, out, chiplet, sidecar = _worker_case(
        tmp_path, others=("SPARE",))
    result = _run_worker(tmp_path, hyp, out, chiplet,
                         flags + ["--die-layouts", sidecar])
    assert result.returncode == 0, result.stderr
    assert ("Die layouts from %s:\n  U1: %s (top cell U1_FULL); the assembly "
            "places %s" % (sidecar, tmp_path / "full" / "u1.gds",
                           os.path.realpath(_die_path))) in result.stdout
    die = _die(chiplet, "U1")
    assert (die["layout"], die["top_cell"]) == ("../full/u1.gds", "U1_FULL")
    assert die["metadata"] == {"assembly_view": "abstract",
                               "assembly_view_layout": "../dies/u1.gds"}
    moved = ("layout", "top_cell", "metadata")
    assert ({k: v for k, v in die.items() if k not in moved}
            == {k: v for k, v in _die(chiplet0, "U1").items()
                if k not in moved})
    # The two GDS differ only in their BGNLIB/BGNSTR dates, so the manifest
    # differs only in the sha256 it records of the complete GDS.
    for name in ("demo_complete.gds", "demo_interposer.gds"):
        assert (_timeless_sha(out / "layout" / name)
                == _timeless_sha(out0 / "layout" / name)), name

    def boundaries(root):
        manifest = json.loads((root / "layout" / "demo_complete.boundaries"
                               ".json").read_text(encoding="utf-8"))
        manifest.pop("assembly_gds_sha256")
        return manifest

    assert boundaries(out) == boundaries(out0)
    cells = _cells(str(out / "layout" / "demo_complete.gds"))
    assert "DIE" in cells
    assert not cells & {"U1_FULL", "SPARE"}
    assert not (out / "chiplets").exists()


@pytest.mark.parametrize("entry_edit,full_kwargs,line", [
    (lambda e: dict(e, bbox_um=[0, 0, 300, 201]), {},
     "Die layout refused: U1: the contract's bbox_um [0, 0, 300, 201] um"),
    (lambda e: dict(e, top_cell="NOPE"), {},
     "Die layout refused: U1: the full layout"),
    (lambda e: dict(e, layout="full/u1.gds"), {},
     "Die layout refused: U1: layout 'full/u1.gds' in"),
], ids=["box", "top-cell", "relative"])
def test_worker_refusal_writes_nothing(tmp_path, entry_edit, full_kwargs,
                                       line):
    hyp, _die_path, out, chiplet, sidecar = _worker_case(
        tmp_path, entry_edit, **full_kwargs)
    before = _tree_state(str(tmp_path))
    result = _run_worker(tmp_path, hyp, out, chiplet,
                         ["--die-outline-layers", "U1=%d/%d" % OUTLINE,
                          "--die-layouts", sidecar])
    assert result.returncode == 1, result.stderr
    assert _tree_state(str(tmp_path)) == before
    assert line in result.stderr
    assert REFUSING in result.stderr


def test_worker_refuses_a_reference_that_is_no_die(tmp_path):
    hyp, _die_path, out, chiplet, _sidecar_path = _worker_case(tmp_path)
    sidecar = _sidecar(tmp_path / "other.json", {
        "U7": _entry(str(tmp_path / "full" / "u1.gds"))})
    before = _tree_state(str(tmp_path))
    result = _run_worker(tmp_path, hyp, out, chiplet,
                         ["--die-outline-layers", "U1=%d/%d" % OUTLINE,
                          "--die-layouts", sidecar])
    assert result.returncode == 1, result.stderr
    assert _tree_state(str(tmp_path)) == before
    assert ("Die layout refused: U7: --die-layouts names it, but the "
            ".chiplet has no die component with this id") in result.stderr


def test_the_flag_without_a_chiplet_refuses(tmp_path):
    # Carried on with the flag ignored, the run would skip the frame check.
    hyp, _die_path, out, _chiplet, sidecar = _worker_case(tmp_path)
    before = _tree_state(str(tmp_path))
    result = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "hyp_to_gds.py"), hyp,
         "-o", str(out / "layout" / "demo_interposer.gds"), "-l", _LYP,
         "--die-layouts", sidecar],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=300)
    assert result.returncode == 1, result.stderr
    assert _tree_state(str(tmp_path)) == before
    assert ("Error: --die-layouts needs --update-chiplet-file: the full "
            "layouts go into the .chiplet, and without one their frame check "
            "would not run; nothing was written." in result.stderr)


def test_an_empty_path_is_a_usage_error(tmp_path, monkeypatch, capsys):
    hyp, _die_path, out, chiplet, _sidecar_path = _worker_case(tmp_path)
    before = _tree_state(str(tmp_path))
    monkeypatch.setattr(sys, "argv", [
        "hyp_to_gds.py", hyp, "-l", _LYP, "-o", str(out / "layout" / "x.gds"),
        "--update-chiplet-file", chiplet, "--die-layouts="])
    assert h.main() == 1
    assert ("Error: --die-layouts was given an empty path."
            in capsys.readouterr().err)
    assert _tree_state(str(tmp_path)) == before


def test_entry_point_reports_a_raised_refusal(monkeypatch, capsys):
    def refuse():
        raise h.DieLayoutRefused([("U1", "a cause")])
    monkeypatch.setattr(h, "main", refuse)
    assert h._main_reporting_refusals() == 1
    assert "Die layout refused: U1: a cause" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# argv, CLI and the .hyp
# ---------------------------------------------------------------------------

def test_the_argv_without_the_flag_is_the_argv_from_before():
    off = build_cli_args("hyp_to_gds.py", "in.hyp", "demo",
                         ExportOptions(output_dir="/out"))
    on = build_cli_args("hyp_to_gds.py", "in.hyp", "demo",
                        ExportOptions(output_dir="/out",
                                      die_layouts_json="/run/die_layouts.json"))
    assert off == ARGV_BEFORE
    assert on == off + ["--die-layouts", "/run/die_layouts.json"]


def test_cli_passes_the_sidecar_absolute(cli, tmp_path, monkeypatch):
    (tmp_path / "die_layouts.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(PASSED, "--die-layouts", "die_layouts.json") == 0
    assert cli.exports[-1].die_layouts_json == str(
        tmp_path / "die_layouts.json")
    assert cli.main(PASSED) == 0
    assert cli.exports[-1].die_layouts_json == ""


def test_cli_refuses_a_missing_sidecar(cli, tmp_path, capsys):
    missing = str(tmp_path / "absent.json")
    assert cli.main(PASSED, "--die-layouts", missing) == 2
    assert cli.exports == []
    assert ("chiplet-export: die layouts sidecar not found: %s" % missing
            in capsys.readouterr().err)


#: Stands in for hyp_to_gds.py in run_export: records the argv it was given
#: and the sha256 of the .hyp it was handed, then finalizes as the fake in
#: test_run_export_canonical_preservation does.
_RECORDING_WORKER = r'''
import hashlib, json, sys

args = sys.argv[1:]
with open(args[0], "rb") as fh:
    hyp_sha = hashlib.sha256(fh.read()).hexdigest()
with open({record!r}, "w", encoding="utf-8") as fh:
    json.dump({{"argv": args, "hyp_sha": hyp_sha}}, fh)
path = args[args.index("--update-chiplet-file") + 1]
with open(path, "r", encoding="utf-8") as fh:
    text = fh.read()
with open(path, "w", encoding="utf-8") as fh:
    fh.write(text.replace("_metadata:\n  finalize_required: true\n", ""))
'''

#: run_export's options with the sidecar set, and a .hyp writer that writes
#: what it was given, so a flag that reached it would change the bytes.
_SETUP = r'''
import functools
discovery.find_hyp_to_gds = lambda *_a, **_kw: {worker!r}
if {sidecar!r}:
    orc.ExportOptions = functools.partial(orc.ExportOptions,
                                          die_layouts_json={sidecar!r})
def _echo_hyperlynx(board, output_path):
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write("{{VERSION=2.0}}\n" + repr(sorted(vars(board))) + "\n")
    return True
hyperlynx_writer.write_hyperlynx = _echo_hyperlynx
'''


def _export(tmp_path, name, sidecar):
    """run_export under the strict pcbnew double; (probe, record, .hyp)."""
    base = tmp_path / name
    base.mkdir()
    record = base / "worker.json"
    worker = base / "worker.py"
    worker.write_text(_RECORDING_WORKER.format(record=str(record)),
                      encoding="utf-8")
    probe, _final = _run(base, extra_setup=_SETUP.format(
        worker=str(worker), sidecar=sidecar))
    hyp = (base / "out" / "board.hyp").read_bytes()
    return probe, json.loads(record.read_text(encoding="utf-8")), hyp, base


def test_the_hyp_does_not_depend_on_the_flag(tmp_path):
    """run_export writes the .hyp before the worker and hands the writer
    nothing from the options; the worker's argv gains --die-layouts FILE and
    nothing else. The writer is a double here (run_export needs pcbnew), and
    the signature test in test_run_export_canonical_preservation keeps it the
    real writer's shape."""
    sidecar = str(tmp_path / "die_layouts.json")
    probe_off, off, hyp_off, base_off = _export(tmp_path, "off", "")
    probe_on, on, hyp_on, base_on = _export(tmp_path, "on", sidecar)
    assert probe_off["error"] == probe_on["error"] == ""
    assert hyp_on == hyp_off
    assert on["hyp_sha"] == off["hyp_sha"] == hashlib.sha256(
        hyp_off).hexdigest()

    def same(record, base):
        return [arg.replace(str(base), "BASE") for arg in record["argv"][1:]]

    tail = same(on, base_on)
    assert "--die-layouts" not in same(off, base_off)
    at = tail.index("--die-layouts")
    assert tail[at:at + 2] == ["--die-layouts", sidecar]
    assert tail[:at] + tail[at + 2:] == same(off, base_off)

