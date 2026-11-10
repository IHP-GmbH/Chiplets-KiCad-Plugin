# SPDX-License-Identifier: GPL-3.0-or-later
"""The finalizer sizes each die from its declared outline, or refuses.

``--die-outline-layers`` and ``--die-outline-exempt`` must between them name
every die of the .chiplet exactly once. A declared die's width/height become
its outline's size, written exactly; an exempt die keeps what the .chiplet
says and is logged by name. In the complete GDS the declared outline, placed
with the die, is the die's boundary; an exempt die keeps the placed cell bbox.

Every refusal is decided before anything is written: update_chiplet_file
raises before touching the .chiplet, and the worker refuses before the
interposer GDS, its sidecars or the .chiplet exist. The CLI test proves the
second on a pre-populated output directory, byte for byte. The worker runs
use the vendored interposer LYP and read the mount plane from the fixture PDK
root, so they need no PDK checkout.
"""
import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

db = pytest.importorskip("klayout.db")
yaml = pytest.importorskip("yaml")

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402

from .conftest import FIXTURE_PDK_ROOT  # noqa: E402


@pytest.fixture(autouse=True)
def _mount_plane(attach_frame_pdk):
    """Each finalize here reads the plane from the fixture PDK root."""


# Layers no PDK in the flow uses, so a shape found on them in the complete
# GDS can only be the die's own.
OUTLINE = (1234, 5)
BODY = (1235, 0)
OTHER = (1236, 0)


def _die_gds(path, outline_box_um, body_box_um=(0.0, 0.0, 300.0, 200.0),
             top_name="DIE", dbu=0.001, outline_dbu=None):
    """A die GDS: its body on BODY and its outline on OUTLINE, both in TOP.

    The body is larger than the outline, so the cell bbox and the outline
    are different answers.
    """
    layout = db.Layout()
    layout.dbu = dbu
    top = layout.create_cell(top_name)
    top.shapes(layout.layer(*BODY)).insert(db.DBox(*body_box_um))
    if outline_dbu is not None:
        top.shapes(layout.layer(*OUTLINE)).insert(db.Box(*outline_dbu))
    elif outline_box_um is not None:
        top.shapes(layout.layer(*OUTLINE)).insert(db.DBox(*outline_box_um))
    path.parent.mkdir(parents=True, exist_ok=True)
    layout.write(str(path))
    return str(path)


def _chiplet(path, dies):
    """Write a .chiplet with an interposer and ``dies`` [(id, extra dict)]."""
    components = [{
        "id": "interposer", "type": "interposer",
        "dimensions": {"width": 2000.0, "height": 1000.0, "thickness": 100.0},
        "position": {"x": 0.0, "y": 0.0, "z": 0.0},
    }]
    for ref, extra in dies:
        component = {
            "id": ref, "type": "die",
            "dimensions": {"width": 100.0, "height": 100.0, "thickness": 0.0},
            "position": {"x": 10.0, "y": 10.0, "z": 0.0},
        }
        component.update(extra)
        components.append(component)
    path.write_text(yaml.safe_dump({
        "format_version": "1.0", "assembly": {"name": "t"},
        "components": components}, sort_keys=False), encoding="utf-8")
    return str(path)


def _update(chiplet, **kwargs):
    kwargs.setdefault("bbox", (0.0, 0.0, 2000.0, 1000.0))
    return h.update_chiplet_file(chiplet, "/nonexistent_interposer.gds",
                                 **kwargs)


def _die(chiplet, ref):
    data = yaml.safe_load(Path(chiplet).read_text(encoding="utf-8"))
    return next(c for c in data["components"] if c["id"] == ref)


def _refused(chiplet, **kwargs):
    """The refusal lines; the .chiplet must be untouched."""
    before = Path(chiplet).read_bytes()
    with pytest.raises(h.DieOutlineRefused) as info:
        _update(chiplet, **kwargs)
    assert Path(chiplet).read_bytes() == before
    return info.value.lines()


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def test_declared_die_is_sized_and_exempt_die_keeps_its_size(tmp_path,
                                                             capsys):
    u1 = _die_gds(tmp_path / "u1.gds", (-389.0, -1298.798, 389.0, 1361.202),
                  body_box_um=(-400.0, -1300.0, 400.0, 1400.0))
    u2 = _die_gds(tmp_path / "u2.gds", (0.0, 0.0, 50.0, 50.0),
                  top_name="U2TOP")
    chiplet = _chiplet(tmp_path / "t.chiplet", [
        ("U1", {"layout": u1}), ("U2", {"layout": u2})])
    assert _update(chiplet, die_outline_layers={"U1": OUTLINE},
                   die_outline_exempt=["U2"]) is True
    one, two = _die(chiplet, "U1"), _die(chiplet, "U2")
    assert (one["dimensions"]["width"], one["dimensions"]["height"]) == (
        778.0, 2660.0)
    assert one["top_cell"] == "DIE"
    # Exempt: the .chiplet's own size stays, not the 50 x 50 outline nor the
    # 300 x 200 cell bbox. Its GDS is still read for top_cell.
    assert (two["dimensions"]["width"], two["dimensions"]["height"]) == (
        100.0, 100.0)
    assert two["top_cell"] == "U2TOP"
    out = capsys.readouterr().out
    assert ("U2: exempt from the die outline read; keeps dimensions 100.0 x "
            "100.0 um") in out
    assert "U1: dimensions 778.000 x 2660.000 um (die outline 1234/5" in out


@pytest.mark.parametrize("box,exact,float_value", [
    ((0, 0, 123457, 2000), ("123.457", "2.0"), "123.45700000000001"),
    ((0, 0, 2000, 123457), ("2.0", "123.457"), "123.45700000000001"),
    # Off the origin, where subtracting the float corners gives
    # 0.19999999999999998 x 0.30000000000000004.
    ((100, 100, 300, 400), ("0.2", "0.3"), "0.19999999999999998"),
], ids=["width", "height", "off-origin"])
def test_size_is_written_exactly(tmp_path, box, exact, float_value):
    """The values written are the exact products of the integer DBU width
    and height and the dbu, never a float product or a float difference of
    the corners, which land a unit in the last place away for these boxes."""
    die = _die_gds(tmp_path / "u1.gds", None, outline_dbu=box)
    chiplet = _chiplet(tmp_path / "t.chiplet", [("U1", {"layout": die})])
    assert _update(chiplet, die_outline_layers={"U1": OUTLINE}) is True
    text = Path(chiplet).read_text(encoding="utf-8")
    assert "width: %s\n" % exact[0] in text
    assert "height: %s\n" % exact[1] in text
    assert float_value not in text
    dims = _die(chiplet, "U1")["dimensions"]
    assert (Decimal(repr(dims["width"])), Decimal(repr(dims["height"]))) == (
        Decimal(exact[0]), Decimal(exact[1]))


def test_two_dies_on_one_file_with_two_layers(tmp_path):
    """Two dies naming one GDS on different layers get one outline each."""
    shared = _die_gds(tmp_path / "shared.gds", (0.0, 0.0, 100.0, 100.0))
    layout = db.Layout()
    layout.read(shared)
    layout.top_cell().shapes(layout.layer(*OTHER)).insert(
        db.DBox(0.0, 0.0, 50.0, 60.0))
    layout.write(shared)
    chiplet = _chiplet(tmp_path / "t.chiplet", [
        ("U1", {"layout": shared}), ("U2", {"layout": shared})])
    assert _update(chiplet, die_outline_layers={"U1": OUTLINE,
                                                "U2": OTHER}) is True
    one, two = _die(chiplet, "U1"), _die(chiplet, "U2")
    assert (one["dimensions"]["width"], one["dimensions"]["height"]) == (
        100.0, 100.0)
    assert (two["dimensions"]["width"], two["dimensions"]["height"]) == (
        50.0, 60.0)


def test_one_file_under_two_names_is_opened_once(tmp_path, monkeypatch):
    """A symlink and the file it names, each declared on its own layer, are
    one open, and both sizes are right."""
    shared = _die_gds(tmp_path / "shared.gds", (0.0, 0.0, 100.0, 100.0))
    layout = db.Layout()
    layout.read(shared)
    layout.top_cell().shapes(layout.layer(*OTHER)).insert(
        db.DBox(0.0, 0.0, 50.0, 60.0))
    layout.write(shared)
    link = tmp_path / "link.gds"
    link.symlink_to(shared)
    opened = []
    real_open = h._die_outline._open

    def counting_open(path, dbu):
        opened.append(os.path.realpath(path))
        return real_open(path, dbu)

    monkeypatch.setattr(h._die_outline, "_open", counting_open)
    chiplet = _chiplet(tmp_path / "t.chiplet", [
        ("U1", {"layout": str(link)}), ("U2", {"layout": shared})])
    assert _update(chiplet, die_outline_layers={"U1": OTHER,
                                                "U2": OUTLINE}) is True
    assert (_die(chiplet, "U1")["dimensions"]["width"],
            _die(chiplet, "U2")["dimensions"]["width"]) == (50.0, 100.0)
    assert opened.count(os.path.realpath(shared)) == 1


def test_resolvable_path_variable_is_read_expanded(tmp_path, monkeypatch):
    _die_gds(tmp_path / "root" / "u1.gds", (0.0, 0.0, 70.0, 80.0))
    roots = {"DIE_OUTLINE_TEST_ROOT": str(tmp_path / "root"),
             "INTERPOSER_PDK_ROOT": str(FIXTURE_PDK_ROOT)}
    monkeypatch.setattr(h, "_discover_path_var", roots.get)
    chiplet = _chiplet(tmp_path / "t.chiplet", [
        ("U1", {"layout": "${DIE_OUTLINE_TEST_ROOT}/u1.gds"})])
    assert _update(chiplet, die_outline_layers={"U1": OUTLINE}) is True
    dims = _die(chiplet, "U1")["dimensions"]
    assert (dims["width"], dims["height"]) == (70.0, 80.0)


# ---------------------------------------------------------------------------
# Wire-bond pads written as dies (replaced by the interposer's io_pads)
# ---------------------------------------------------------------------------

_PAD = {"ref": "J1", "io_class": "wire_bond", "x_um": 100.0, "y_um": 200.0,
        "size_x_um": 100.0, "size_y_um": 100.0}


def _die_and_pad(tmp_path):
    """U1 with a GDS, and J1: a pad some other tool wrote as a die, with no
    layout and nothing to read."""
    u1 = _die_gds(tmp_path / "u1.gds", (0.0, 0.0, 100.0, 100.0))
    return _chiplet(tmp_path / "t.chiplet", [("U1", {"layout": u1}),
                                             ("J1", {})])


def test_die_an_io_pad_replaces_needs_no_flag(tmp_path):
    chiplet = _die_and_pad(tmp_path)
    assert _update(chiplet, io_pads=[dict(_PAD)],
                   die_outline_layers={"U1": OUTLINE}) is True
    ids = [c["id"] for c in yaml.safe_load(
        Path(chiplet).read_text(encoding="utf-8"))["components"]]
    assert "J1" not in ids and "U1" in ids


@pytest.mark.parametrize("flags", [
    {"die_outline_layers": {"U1": OUTLINE, "J1": OUTLINE}},
    {"die_outline_layers": {"U1": OUTLINE}, "die_outline_exempt": ["J1"]},
], ids=["declared", "exempt"])
def test_flag_naming_an_io_pad_refuses(tmp_path, flags):
    lines = _refused(_die_and_pad(tmp_path), io_pads=[dict(_PAD)], **flags)
    assert len(lines) == 1
    assert lines[0].startswith("Die outline refused: J1: --die-outline-")
    assert lines[0].endswith(
        "names it, but an --io-pads entry carries this id, so the .chiplet "
        "die component with it is a wire-bond pad that the interposer's "
        "io_pads replace, not a die")


def test_io_pad_left_unplaced_keeps_its_die_component(tmp_path, capsys):
    """The worker head decides on every --io-pads entry, placed or not, and
    hands the .chiplet update the same refs. An entry add_io_pads skipped is
    not in io_pads, so its die component stays; it is still not sized."""
    chiplet = _die_and_pad(tmp_path)
    assert _update(chiplet, io_pads=[], io_pad_refs=["J1"],
                   die_outline_layers={"U1": OUTLINE}) is True
    pad = _die(chiplet, "J1")
    assert (pad["dimensions"]["width"], pad["dimensions"]["height"]) == (
        100.0, 100.0)
    assert "top_cell" not in pad
    assert ("J1: not sized: an --io-pads entry carries this id, so it is a "
            "wire-bond pad, not a die" in capsys.readouterr().out)


# ---------------------------------------------------------------------------
# Refusals (update_chiplet_file raises before writing)
# ---------------------------------------------------------------------------

def _two_dies(tmp_path, u2_extra=None):
    u1 = _die_gds(tmp_path / "u1.gds", (0.0, 0.0, 100.0, 100.0))
    u2 = _die_gds(tmp_path / "u2.gds", (0.0, 0.0, 100.0, 100.0))
    return _chiplet(tmp_path / "t.chiplet", [
        ("U1", {"layout": u1}),
        ("U2", {"layout": u2} if u2_extra is None else u2_extra)])


def test_no_flags_with_dies_refuses_every_die(tmp_path):
    lines = _refused(_two_dies(tmp_path))
    assert [line.split(": ")[1] for line in lines] == ["U1", "U2"]
    for line in lines:
        assert line.split(": ", 2)[2].startswith(
            "no die outline layer was given for any die (neither "
            "--die-outline-layers nor --die-outline-exempt)")
        assert "DIE_OUTLINE_LAYER" in line and "39/4" in line


def test_undeclared_die_refuses(tmp_path):
    lines = _refused(_two_dies(tmp_path), die_outline_layers={"U1": OUTLINE})
    assert lines == [
        "Die outline refused: U2: neither --die-outline-layers nor "
        "--die-outline-exempt names this die. Set the footprint field "
        "DIE_OUTLINE_LAYER to the LAYER/DATATYPE its GDS draws the die "
        "outline on (for example 39/4 for SG13G2 dies) and export again"]


def test_die_in_both_flags_refuses(tmp_path):
    lines = _refused(_two_dies(tmp_path),
                     die_outline_layers={"U1": OUTLINE, "U2": OUTLINE},
                     die_outline_exempt=["U1"])
    assert lines == [
        "Die outline refused: U1: both --die-outline-layers and "
        "--die-outline-exempt name this die; it is either sized from its "
        "outline or exempt"]


def test_flagged_ref_that_is_not_a_die_refuses(tmp_path):
    lines = _refused(_two_dies(tmp_path),
                     die_outline_layers={"U1": OUTLINE, "U2": OUTLINE,
                                         "interposer": OUTLINE,
                                         "U9": OUTLINE},
                     die_outline_exempt=["U8"])
    assert lines == [
        "Die outline refused: interposer: --die-outline-layers names it, "
        "but the .chiplet component with this id is of type 'interposer', "
        "not a die",
        "Die outline refused: U9: --die-outline-layers names it, but the "
        ".chiplet has no die component with this id",
        "Die outline refused: U8: --die-outline-exempt names it, but the "
        ".chiplet has no die component with this id"]


def test_duplicate_chiplet_die_ids_refuse(tmp_path):
    die = _die_gds(tmp_path / "u1.gds", (0.0, 0.0, 100.0, 100.0))
    chiplet = _chiplet(tmp_path / "t.chiplet", [
        ("U1", {"layout": die}), ("U1", {"layout": die})])
    assert _refused(chiplet, die_outline_layers={"U1": OUTLINE}) == [
        "Die outline refused: U1: 2 .chiplet die components carry this id; "
        "a per-die outline cannot tell them apart"]


def test_duplicate_hyp_device_refs_refuse(tmp_path):
    chiplet = _two_dies(tmp_path)
    dev = h.Device("U1", "TopMetal2", str(tmp_path / "u1.gds"))
    lines = _refused(chiplet, devices=[dev, dev],
                     die_outline_layers={"U1": OUTLINE, "U2": OUTLINE})
    assert lines == [
        "Die outline refused: U1: 2 HYP devices carry this reference; a "
        "per-die outline cannot tell them apart"]


def test_shared_hyp_reference_nothing_uses_is_left_alone(tmp_path):
    """Two footprints that are not dies may share a reference (REF**); only
    a reference a die or a flag uses must be unique among the HYP devices."""
    chiplet = _two_dies(tmp_path)
    stray = h.Device("REF**", "TopMetal2", str(tmp_path / "u1.gds"))
    assert _update(chiplet, devices=[stray, stray],
                   die_outline_layers={"U1": OUTLINE, "U2": OUTLINE}) is True
    plan = h.resolve_die_outlines(None, "", [stray, stray])
    assert (plan.outlines, plan.exempt) == ({}, {})
    with pytest.raises(h.DieOutlineRefused) as info:
        h.resolve_die_outlines(None, "", [stray, stray],
                               outline_exempt=["REF**"])
    assert info.value.lines() == [
        "Die outline refused: REF**: 2 HYP devices carry this reference; a "
        "per-die outline cannot tell them apart"]


def test_missing_gds_refuses_an_exempt_die_too(tmp_path):
    missing = str(tmp_path / "gone" / "u2.gds")
    lines = _refused(_two_dies(tmp_path, {"layout": missing}),
                     die_outline_layers={"U1": OUTLINE},
                     die_outline_exempt=["U2"])
    assert lines == ["Die outline refused: U2: GDS %s does not exist"
                     % os.path.realpath(missing)]


def test_unreadable_gds_refuses_an_exempt_die_too(tmp_path):
    garbage = tmp_path / "u2_garbage.gds"
    garbage.write_bytes(b"not a GDS stream\n" * 8)
    lines = _refused(_two_dies(tmp_path, {"layout": str(garbage)}),
                     die_outline_layers={"U1": OUTLINE},
                     die_outline_exempt=["U2"])
    assert len(lines) == 1
    assert lines[0].startswith("Die outline refused: U2: cannot read GDS %s: "
                               % os.path.realpath(str(garbage)))


@pytest.mark.parametrize("dbu,exempt", [(0.005, False), (0.0005, True)],
                         ids=["declared", "exempt"])
def test_die_gds_at_another_dbu_refuses(tmp_path, dbu, exempt):
    """The complete GDS imports a die into its 0.001 layout without
    rescaling; a die at another dbu would change the whole layout's scale."""
    other = _die_gds(tmp_path / "u2_dbu.gds", (0.0, 0.0, 100.0, 100.0),
                     dbu=dbu)
    lines = _refused(_two_dies(tmp_path, {"layout": other}),
                     die_outline_layers=(
                         {"U1": OUTLINE} if exempt
                         else {"U1": OUTLINE, "U2": OUTLINE}),
                     die_outline_exempt=["U2"] if exempt else None)
    assert lines == [
        "Die outline refused: U2: GDS %s has a dbu of %s um, not the 0.001 "
        "um of the assembly layout; a die is imported without rescaling, so "
        "it would change the scale of the complete GDS"
        % (os.path.realpath(other), Decimal(repr(dbu)))]


def test_die_without_any_gds_refuses(tmp_path):
    lines = _refused(_two_dies(tmp_path, {}),
                     die_outline_layers={"U1": OUTLINE},
                     die_outline_exempt=["U2"])
    assert lines == [
        "Die outline refused: U2: no die GDS to read: no HYP device with a "
        "GDS_FILE carries this reference and the .chiplet component has no "
        "layout"]


def test_unresolvable_path_variable_refuses(tmp_path):
    lines = _refused(
        _two_dies(tmp_path, {"layout": "${NO_SUCH_ROOT_FOR_TEST}/u2.gds"}),
        die_outline_layers={"U1": OUTLINE, "U2": OUTLINE})
    assert len(lines) == 1
    assert lines[0].startswith(
        "Die outline refused: U2: cannot resolve GDS path "
        "${NO_SUCH_ROOT_FOR_TEST}/u2.gds: ")


def test_every_offender_is_named_in_one_refusal(tmp_path):
    body_only = _die_gds(tmp_path / "u1_body.gds", None)
    chiplet = _two_dies(tmp_path, {"layout": str(tmp_path / "nope.gds")})
    data = yaml.safe_load(Path(chiplet).read_text(encoding="utf-8"))
    data["components"][1]["layout"] = body_only
    Path(chiplet).write_text(yaml.safe_dump(data, sort_keys=False),
                             encoding="utf-8")
    lines = _refused(chiplet, die_outline_layers={"U1": OUTLINE, "U7": BODY},
                     die_outline_exempt=["U2"])
    assert [line.split(": ")[1] for line in lines] == ["U7", "U1", "U2"]
    assert ("die outline layer 1234/5 is not in %s"
            % os.path.realpath(body_only) in lines[1])
    assert "does not exist" in lines[2]


def test_gds_top_cell_is_never_a_silent_none(tmp_path, capsys):
    with pytest.raises(h.DieGdsError, match="does not exist"):
        h._read_gds_top_cell(str(tmp_path / "gone.gds"))
    # The interposer GDS is written before the .chiplet update, so its top
    # cell failing is reported rather than refused.
    die = _die_gds(tmp_path / "u1.gds", (0.0, 0.0, 100.0, 100.0))
    chiplet = _chiplet(tmp_path / "t.chiplet", [("U1", {"layout": die})])
    assert _update(chiplet, die_outline_layers={"U1": OUTLINE}) is True
    assert ("Warning: interposer top_cell not written: GDS "
            "%s does not exist" % os.path.realpath("/nonexistent_interposer.gds")
            in capsys.readouterr().err)


# ---------------------------------------------------------------------------
# The worker: the boundary, and nothing written on a refusal
# ---------------------------------------------------------------------------

# The vendored IntM4TM2 layer table (test_layer_map_parity.py pins it to the
# PDK's), so the worker runs below need no PDK checkout.
_LYP = str(Path(__file__).resolve().parent / "fixtures"
           / "intm4tm2_reference.lyp")

_HYP = """\
{{VERSION=2.14}}
{{UNITS=METRIC LENGTH}}

{{BOARD "synthetic"
  (PERIMETER_SEGMENT X1=0.000000 Y1=0.000000 X2=0.002000 Y2=0.000000)
  (PERIMETER_SEGMENT X1=0.002000 Y1=0.000000 X2=0.002000 Y2=-0.001000)
  (PERIMETER_SEGMENT X1=0.002000 Y1=-0.001000 X2=0.000000 Y2=-0.001000)
  (PERIMETER_SEGMENT X1=0.000000 Y1=-0.001000 X2=0.000000 Y2=0.000000)
}}

{{STACKUP
  (SIGNAL T=3.5e-05 P=0 C=1.724e-08 L="TopMetal2" M=COPPER)
}}

{{DEVICES
{devices}}}

{{NET="n1"
  (SEG X1=0.000100 Y1=-0.000100 X2=0.001900 Y2=-0.000100 W=0.0000040000 L="TopMetal2")
}}
"""

_DEVICE = ('  (? REF="{ref}" L="TopMetal2" X={x:.6f} Y=-0.000500 '
           'R={rotation:.2f} GDS_FILE="{gds}")\n')


def _hyp(tmp_path, devices):
    """A HYP placing ``devices`` [(ref, gds, rotation, x in metres)]."""
    hyp = tmp_path / "board.hyp"
    hyp.write_text(_HYP.format(devices="".join(
        _DEVICE.format(ref=ref, gds=gds, rotation=rotation, x=x)
        for ref, gds, rotation, x in devices)))
    return str(hyp)


def _board(tmp_path, rotation=0.0, flip=False, dbu=0.001, chiplet=True):
    """HYP, die GDS and .chiplet (None when ``chiplet`` is False) for one die
    U1; returns their paths."""
    die = _die_gds(tmp_path / "dies" / "u1.gds", (40.0, 30.0, 240.0, 150.0),
                   dbu=dbu)
    hyp = _hyp(tmp_path, [("U1", die, rotation, 0.001)])
    out = tmp_path / "out"
    (out / "layout").mkdir(parents=True, exist_ok=True)
    extra = {"layout": die}
    if flip:
        extra["orientation"] = "flip_chip"
    path = _chiplet(out / "demo.chiplet", [("U1", extra)]) if chiplet else None
    return hyp, die, out, path


def _convert(hyp, out, chiplet=None, **kwargs):
    """convert_hyp_to_gds with the complete GDS, in this process."""
    return h.convert_hyp_to_gds(
        hyp_path=hyp, output_path=str(out / "layout" / "demo_interposer.gds"),
        lyp_path=_LYP, with_chiplets=True,
        complete_output_path=str(out / "layout" / "demo_complete.gds"),
        chiplet_file_path=chiplet, **kwargs)


def _boundary(out, ref="U1"):
    manifest = json.loads((out / "layout" / "demo_complete.boundaries.json")
                          .read_text(encoding="utf-8"))
    return next(r for r in manifest["boundaries"] if r["instance"] == ref)


def _box_of(record):
    xs = [p[0] for p in record["polygon_dbu"]]
    ys = [p[1] for p in record["polygon_dbu"]]
    return min(xs), min(ys), max(xs), max(ys)


def _placed_bbox_dbu(gds_path, layer):
    layout = db.Layout()
    layout.read(gds_path)
    index = layout.find_layer(*layer)
    box = db.Region(layout.top_cell().begin_shapes_rec(index)).bbox()
    return box.left, box.bottom, box.right, box.top


@pytest.mark.parametrize("flip,rotation,exempt", [
    (False, 0.0, False),
    (False, 90.0, False),
    (True, 0.0, False),
    (True, 90.0, False),
    (False, 90.0, True),
    (True, 90.0, True),
])
def test_boundary_is_the_placed_outline(tmp_path, flip, rotation, exempt):
    """The boundary manifest's box for U1 is where U1's outline shapes land
    in the complete GDS (mirror, rotation and offset included), and not the
    placed body, which is larger. An exempt die keeps the placed cell bbox,
    which here is the body."""
    hyp, _unused, out, chiplet = _board(tmp_path, rotation, flip)
    complete = str(out / "layout" / "demo_complete.gds")
    ok = h.convert_hyp_to_gds(
        hyp_path=hyp, output_path=str(out / "layout" / "demo_interposer.gds"),
        lyp_path=_LYP, with_chiplets=True, complete_output_path=complete,
        chiplet_file_path=chiplet,
        die_outline_layers=None if exempt else {"U1": OUTLINE},
        die_outline_exempt=["U1"] if exempt else None)
    assert ok is True
    manifest = json.loads((out / "layout" / "demo_complete.boundaries.json")
                          .read_text(encoding="utf-8"))
    record = next(r for r in manifest["boundaries"] if r["instance"] == "U1")
    assert record["transform"]["mirror_x"] is flip
    xs = [p[0] for p in record["polygon_dbu"]]
    ys = [p[1] for p in record["polygon_dbu"]]
    boundary = (min(xs), min(ys), max(xs), max(ys))
    outline = _placed_bbox_dbu(complete, OUTLINE)
    body = _placed_bbox_dbu(complete, BODY)
    assert outline != body
    assert boundary == (body if exempt else outline)


def _tree_state(root):
    """Every directory and every file's sha256 under ``root``."""
    state = {}
    for base, dirs, files in os.walk(root):
        rel = os.path.relpath(base, root)
        state[rel + "/"] = None
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as handle:
                state[os.path.join(rel, name)] = hashlib.sha256(
                    handle.read()).hexdigest()
    return state


def _run_worker(tmp_path, hyp, out, chiplet, flags):
    command = [
        sys.executable, str(PLUGIN_ROOT / "hyp_to_gds.py"), hyp,
        "-o", str(out / "layout" / "demo_interposer.gds"), "-l", _LYP,
        "--with-chiplets",
        "--complete-output", str(out / "layout" / "demo_complete.gds"),
        "--update-chiplet-file", chiplet,
    ] + flags
    return subprocess.run(command, cwd=str(tmp_path), capture_output=True,
                          text=True, timeout=300)


@pytest.mark.parametrize("flags,cause", [
    ([], "no die outline layer was given for any die"),
    (["--die-outline-layers", "U1=7/7"], "die outline layer 7/7 is not in"),
], ids=["no-flags", "layer-absent"])
def test_refusal_leaves_the_output_dir_byte_identical(tmp_path, flags, cause):
    hyp, _unused, out, chiplet = _board(tmp_path)
    # A previous export's outputs, which a refused run must not touch.
    for name, text in [
            ("layout/demo_interposer.gds", "old interposer"),
            ("layout/demo_interposer.boundaries.json", "{}"),
            ("layout/demo_interposer.pillars.json", "{}"),
            ("layout/demo_complete.gds", "old complete"),
            ("layout/demo_complete.boundaries.json", "{}"),
            ("chiplets/U1/u1.gds", "old bundle"),
            ("MANIFEST.md", "old manifest")]:
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_text(text)
    before = _tree_state(str(tmp_path))
    result = _run_worker(tmp_path, hyp, out, chiplet, flags)
    assert result.returncode == 1, result.stderr
    assert _tree_state(str(tmp_path)) == before
    assert "Die outline refused: U1: " + cause in result.stderr
    assert ("Refusing to export: no layout, sidecar or .chiplet was written."
            in result.stderr)


def test_declared_run_does_write(tmp_path):
    """Control for the test above: the same comparison sees the writes of
    a run that is not refused."""
    hyp, _unused, out, chiplet = _board(tmp_path)
    before = _tree_state(str(tmp_path))
    result = _run_worker(tmp_path, hyp, out, chiplet,
                         ["--die-outline-layers", "U1=%d/%d" % OUTLINE])
    assert result.returncode == 0, result.stderr
    after = _tree_state(str(tmp_path))
    assert after != before
    assert after["out/demo.chiplet"] != before["out/demo.chiplet"]
    assert "out/layout/demo_complete.boundaries.json" in after
    assert (_die(chiplet, "U1")["dimensions"]["width"],
            _die(chiplet, "U1")["dimensions"]["height"]) == (200.0, 120.0)


@pytest.mark.parametrize("flag,value,message", [
    ("--die-outline-layers", "U1=39",
     "Invalid die outline layer '39' for 'U1' in --die-outline-layers"),
    ("--die-outline-layers", "", "Invalid --die-outline-layers token ''"),
    ("--die-outline-exempt", "U1=39/4",
     "Invalid --die-outline-exempt token 'U1=39/4'"),
])
def test_cli_parse_error_names_the_token(tmp_path, monkeypatch, capsys, flag,
                                         value, message):
    hyp, _unused, out, chiplet = _board(tmp_path)
    before = _tree_state(str(tmp_path))
    monkeypatch.setattr(sys, "argv", [
        "hyp_to_gds.py", hyp, "-l", _LYP, "-o",
        str(out / "layout" / "demo_interposer.gds"),
        "--update-chiplet-file", chiplet, flag + "=" + value])
    assert h.main() == 1
    assert "Error: " + message in capsys.readouterr().err
    assert _tree_state(str(tmp_path)) == before


def test_entry_point_reports_a_raised_refusal(monkeypatch, capsys):
    def refuse():
        raise h.DieOutlineRefused([("U1", "a cause")])
    monkeypatch.setattr(h, "main", refuse)
    assert h._main_reporting_refusals() == 1
    assert "Die outline refused: U1: a cause" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# More worker paths: rotation, no .chiplet, reads, dbu, placement, io pads
# ---------------------------------------------------------------------------

def _placed_outline_hull(gds_path, layer):
    """The hull points, in DBU, of the one merged shape on ``layer``."""
    layout = db.Layout()
    layout.read(gds_path)
    region = db.Region(layout.top_cell().begin_shapes_rec(
        layout.find_layer(*layer))).merged()
    (polygon,) = list(region.each())
    return sorted((p.x, p.y) for p in polygon.each_point_hull())


@pytest.mark.parametrize("flip", [False, True], ids=["face-up", "flip-chip"])
def test_boundary_is_the_rotated_outline_at_45_degrees(tmp_path, flip):
    """At a rotation that is not a right angle the boundary is the outline
    where its four corners land, for a flip-chip die as for a face-up one,
    and not the larger axis-aligned box around it."""
    hyp, _unused, out, chiplet = _board(tmp_path, 45.0, flip)
    assert _convert(hyp, out, chiplet,
                    die_outline_layers={"U1": OUTLINE}) is True
    boundary = sorted(tuple(p) for p in _boundary(out)["polygon_dbu"])
    placed = _placed_outline_hull(str(out / "layout" / "demo_complete.gds"),
                                  OUTLINE)
    assert len(boundary) == len(placed) == 4
    for (bx, by), (px, py) in zip(boundary, placed):
        # The placed shapes are snapped to whole DBU; the boundary is not.
        assert abs(bx - px) <= 1 and abs(by - py) <= 1


def test_without_a_chiplet_a_declared_die_gets_its_outline(tmp_path):
    hyp, _unused, out, _none = _board(tmp_path, 90.0, chiplet=False)
    assert _convert(hyp, out, die_outline_layers={"U1": OUTLINE}) is True
    assert _box_of(_boundary(out)) == _placed_bbox_dbu(
        str(out / "layout" / "demo_complete.gds"), OUTLINE)


def test_without_a_chiplet_an_exempt_die_is_named(tmp_path, capsys):
    hyp, _unused, out, _none = _board(tmp_path, chiplet=False)
    assert _convert(hyp, out, die_outline_exempt=["U1"]) is True
    stdout = capsys.readouterr().out
    assert "U1: exempt (--die-outline-exempt)" in stdout
    assert "U1: boundary is the placed cell bbox (exempt)" in stdout
    assert _box_of(_boundary(out)) == _placed_bbox_dbu(
        str(out / "layout" / "demo_complete.gds"), BODY)


def test_without_a_chiplet_an_unknown_reference_refuses(tmp_path, capsys):
    hyp, _unused, out, _none = _board(tmp_path, chiplet=False)
    before = _tree_state(str(tmp_path))
    assert _convert(hyp, out, die_outline_layers={"U1": OUTLINE,
                                                  "U9": OUTLINE}) is False
    assert ("Die outline refused: U9: --die-outline-layers names it, but no "
            "HYP device carries this reference" in capsys.readouterr().err)
    assert _tree_state(str(tmp_path)) == before


def test_outline_flags_without_either_mode_are_reported_ignored(tmp_path,
                                                               capsys):
    hyp, _unused, out, _none = _board(tmp_path, chiplet=False)
    assert h.convert_hyp_to_gds(
        hyp_path=hyp, output_path=str(out / "layout" / "demo_interposer.gds"),
        lyp_path=_LYP, die_outline_layers={"U1": OUTLINE}) is True
    assert ("Warning: --die-outline-layers and --die-outline-exempt are "
            "ignored without --update-chiplet-file or --with-chiplets."
            in capsys.readouterr().err)


def test_head_reads_the_hyp_gds_file_before_the_chiplet_layout(tmp_path):
    """The exporter writes the board-relative layout (../chiplets/u1.gds),
    which does not resolve from the output directory. The head reads the
    HYP device's GDS_FILE, the file add_device places."""
    hyp, _unused, out, chiplet = _board(tmp_path)
    data = yaml.safe_load(Path(chiplet).read_text(encoding="utf-8"))
    data["components"][1]["layout"] = "../chiplets/u1.gds"
    Path(chiplet).write_text(yaml.safe_dump(data, sort_keys=False),
                             encoding="utf-8")
    assert not (tmp_path / "chiplets" / "u1.gds").exists()
    assert _convert(hyp, out, chiplet,
                    die_outline_layers={"U1": OUTLINE}) is True
    dims = _die(chiplet, "U1")["dimensions"]
    assert (dims["width"], dims["height"]) == (200.0, 120.0)


def test_worker_opens_each_die_gds_once(tmp_path, monkeypatch):
    """The head, the .chiplet update and the boundary share one read."""
    hyp, die, out, chiplet = _board(tmp_path)
    opened = []
    real_open = h._die_outline._open

    def counting_open(path, dbu):
        opened.append(os.path.realpath(path))
        return real_open(path, dbu)

    monkeypatch.setattr(h._die_outline, "_open", counting_open)
    assert _convert(hyp, out, chiplet,
                    die_outline_layers={"U1": OUTLINE}) is True
    assert opened.count(os.path.realpath(die)) == 1


def test_die_at_another_dbu_refuses_before_anything_is_written(tmp_path,
                                                                capsys):
    hyp, die, out, chiplet = _board(tmp_path, dbu=0.005)
    before = _tree_state(str(tmp_path))
    assert _convert(hyp, out, chiplet,
                    die_outline_layers={"U1": OUTLINE}) is False
    assert ("Die outline refused: U1: GDS %s has a dbu of 0.005 um"
            % os.path.realpath(die) in capsys.readouterr().err)
    assert _tree_state(str(tmp_path)) == before


def test_device_at_another_dbu_never_rescales_the_complete_gds(tmp_path,
                                                               capsys):
    """A device the outline read does not cover (no .chiplet, nobody names
    it) is still imported into the 0.001 layout. At another dbu the import
    would rescale everything already drawn, so no complete GDS is written."""
    hyp, _unused, out, _none = _board(tmp_path, dbu=0.005, chiplet=False)
    assert _convert(hyp, out) is False
    err = capsys.readouterr().err
    assert "ERROR: U1: GDS " in err and "has a dbu of 0.005 um" in err
    assert not (out / "layout" / "demo_complete.gds").exists()


def test_die_that_cannot_be_placed_fails_the_run(tmp_path, monkeypatch,
                                                 capsys):
    u1 = _die_gds(tmp_path / "dies" / "u1.gds", (40.0, 30.0, 240.0, 150.0))
    u2 = _die_gds(tmp_path / "dies" / "u2.gds", (40.0, 30.0, 240.0, 150.0))
    hyp = _hyp(tmp_path, [("U1", u1, 0.0, 0.0006), ("U2", u2, 0.0, 0.0014)])
    out = tmp_path / "out"
    (out / "layout").mkdir(parents=True)
    real_add = h.GDSGenerator.add_device

    def add_device(self, device, **kwargs):
        if device.ref == "U2":
            return False
        return real_add(self, device, **kwargs)

    monkeypatch.setattr(h.GDSGenerator, "add_device", add_device)
    assert _convert(hyp, out, die_outline_layers={"U1": OUTLINE,
                                                  "U2": OUTLINE}) is False
    assert "die(s) U2 could not be placed" in capsys.readouterr().err
    assert not (out / "layout" / "demo_complete.gds").exists()


def test_worker_io_pad_entry_it_skips_keeps_its_die_unsized(tmp_path,
                                                            capsys):
    """The head and the .chiplet update decide on the same --io-pads refs.
    J1's entry is too small to place, so add_io_pads skips it and the dedup
    keeps the J1 component, which is still a pad and not sized."""
    hyp, die, out, chiplet = _board(tmp_path)
    _chiplet(Path(chiplet), [("U1", {"layout": die}), ("J1", {})])
    pads = tmp_path / "io_pads.json"
    pads.write_text(json.dumps({"io_pads": [
        dict(_PAD, size_x_um=1.0, size_y_um=1.0)]}))
    assert _convert(hyp, out, chiplet, io_pads_json=str(pads),
                    die_outline_layers={"U1": OUTLINE}) is True
    assert ("J1: not sized: an --io-pads entry carries this id"
            in capsys.readouterr().out)
    assert _die(chiplet, "J1")["dimensions"]["width"] == 100.0
    assert _die(chiplet, "U1")["dimensions"]["width"] == 200.0


def test_cli_takes_a_reference_that_starts_with_a_dash(tmp_path, monkeypatch,
                                                       capsys):
    """The --flag=VALUE form build_cli_args emits reaches the worker as the
    value, not as an option."""
    hyp, _unused, out, chiplet = _board(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "hyp_to_gds.py", hyp, "-l", _LYP, "-o",
        str(out / "layout" / "demo_interposer.gds"),
        "--update-chiplet-file", chiplet,
        "--die-outline-layers=U1=%d/%d" % OUTLINE,
        "--die-outline-exempt=-U1"])
    assert h.main() == 1
    assert ("Die outline refused: -U1: --die-outline-exempt names it, but "
            "the .chiplet has no die component with this id"
            in capsys.readouterr().err)
