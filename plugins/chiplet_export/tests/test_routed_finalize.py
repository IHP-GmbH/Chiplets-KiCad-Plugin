# SPDX-License-Identifier: GPL-3.0-or-later
"""The routed path's frame: io pads in gds_bbox_corner_um, the bbox guard,
and the .chiplet finalized without a shift.

On the routed path the worker writes the io pads and die origins into the
.chiplet as the routed geometry gives them, in the frame whose origin is the
outline's lower-left. That is the GDS bbox's lower-left only while nothing
composed lies outside it, so the worker checks it exactly (0 dbu) before the
GDS is written and again after the fill, refusing with
[interposer-bbox-off-origin], naming both lower-left corners and the shape
farthest out. A refused run leaves no GDS, boundaries or pillars file.

The worker runs in process on the routed_t03 fixture, without fill or
pillars unless a case needs them. The finalized dies and pads are compared
with the board path's golden .chiplet (fixtures/routed_t03/inputs/
board-path.chiplet).
"""

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

db = pytest.importorskip("klayout.db")
yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
PLUGINS = REPO.parent
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))

import hyp_to_gds as _h  # noqa: E402

from .routed_t03_stage import BOARD_HYP, CHIPLET, INPUTS, IO_PADS, stage  # noqa: E402

LYP_PATH = Path(_h._find_default_lyp())
BOARD_CHIPLET = INPUTS / "board-path.chiplet"
NAME = "cf_typical_03_from_schematic"
OFF_ORIGIN = "[interposer-bbox-off-origin]"


@pytest.fixture(autouse=True)
def _lyp():
    if not LYP_PATH.is_file():
        pytest.skip("interposer LYP not found: %s" % LYP_PATH)


def _io_pads(tmp_path, edit=None):
    data = json.loads(IO_PADS.read_text())
    if edit is not None:
        edit(data)
    path = tmp_path / "io_pads.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def _run(tmp_path, *, io_pads=IO_PADS, chiplet=None, **kwargs):
    """The worker on the routed geometry; (ok, layout folder, stderr)."""
    document = stage(tmp_path)
    layout = tmp_path / "layout"
    layout.mkdir()
    ok = _h.convert_hyp_to_gds(
        "", str(layout / ("%s_interposer.gds" % NAME)), str(LYP_PATH),
        routed_geometry_path=str(document), io_pads_json=str(io_pads),
        chiplet_file_path=chiplet, die_outline_exempt=["U1", "U2"], **kwargs)
    return ok, layout


def _left_out(layout):
    return sorted(p.name for p in layout.iterdir())


def test_a_board_frame_manifest_is_refused_before_anything_is_written(
        tmp_path, capsys):
    pads = _io_pads(tmp_path, lambda d: d.update(frame="board_absolute_ynegated_um"))
    ok, layout = _run(tmp_path, io_pads=pads)
    err = capsys.readouterr().err
    assert ok is False
    assert "board_absolute_ynegated_um" in err and "gds_bbox_corner_um" in err
    assert _left_out(layout) == []


def _pad_past_left_edge(data):
    """J_U1_ext[0]'s left edge 10 um left of the outline."""
    pad = data["io_pads"][0]
    pad["x_um"] = -10.0 + pad["size_x_um"] / 2.0


def test_a_pad_past_the_left_edge_is_refused_and_nothing_is_left(tmp_path, capsys):
    ok, layout = _run(tmp_path, io_pads=_io_pads(tmp_path, _pad_past_left_edge))
    err = capsys.readouterr().err
    assert ok is False
    assert OFF_ORIGIN in err
    assert "bbox lower-left is (-10000, 0) dbu" in err
    assert "outline's lower-left (0, 0) dbu" in err
    assert "in cell WB_PAD_80x80" in err
    assert _left_out(layout) == []


def _shift_outline(axis):
    """The parsed outline moved 1 dbu off the origin on ``axis``. The
    document check refuses such an outline when it reads it
    (test_the_document_refuses_an_outline_off_the_origin); this plants it
    after the parse, so the guard is what refuses."""
    parse = _h.RoutedGeometryParser.parse

    def shifted(self):
        parse(self)
        d = 1e-9  # 1 dbu (1 nm) in metres
        for seg in self.perimeter_segments:
            if axis == "x":
                seg.x1 = d * -1 if seg.x1 == 0 else seg.x1
                seg.x2 = d * -1 if seg.x2 == 0 else seg.x2
            else:
                seg.y1 = d * -1 if seg.y1 == 0 else seg.y1
                seg.y2 = d * -1 if seg.y2 == 0 else seg.y2
    return shifted


@pytest.mark.parametrize("axis, corner", [("x", "(-1, 0)"), ("y", "(0, -1)")])
def test_an_outline_one_dbu_off_the_origin_is_refused(
        tmp_path, capsys, monkeypatch, axis, corner):
    monkeypatch.setattr(_h.RoutedGeometryParser, "parse", _shift_outline(axis))
    ok, layout = _run(tmp_path)
    err = capsys.readouterr().err
    assert ok is False
    assert OFF_ORIGIN in err
    assert "bbox lower-left is %s dbu" % corner in err
    assert "outline's lower-left %s dbu" % corner in err
    assert _left_out(layout) == []


@pytest.mark.parametrize("axis", ["x", "y"])
def test_the_document_refuses_an_outline_off_the_origin(tmp_path, capsys, axis):
    i = 0 if axis == "x" else 1

    def edit(document):
        for point in document["outline"]:
            if point[i] == 0:
                point[i] = -0.001
    layout = tmp_path / "layout"
    layout.mkdir()
    ok = _h.convert_hyp_to_gds("", str(layout / "i.gds"), str(LYP_PATH),
                               routed_geometry_path=str(stage(tmp_path, edit)))
    assert ok is False
    assert "outline's lower-left" in capsys.readouterr().err
    assert _left_out(layout) == []


def test_a_fill_shape_past_the_left_edge_is_refused_and_the_gds_deleted(
        tmp_path, capsys, monkeypatch):
    def fill(path, cell, mode, layers):
        layout = db.Layout()
        layout.read(path)
        top = layout.cell(cell)
        index = layout.layer(*layers[0]) if layers else layout.layer(126, 22)
        top.shapes(index).insert(db.Box(-5000, 100000, -1000, 104000))
        layout.write(path)
        return {"status": "ok", "report": {}}
    monkeypatch.setattr(_h, "_insert_metal_fill", fill)
    ok, layout = _run(tmp_path, insert_metal_fill=True)
    err = capsys.readouterr().err
    assert ok is False
    assert OFF_ORIGIN in err and "bbox lower-left is (-5000, 0) dbu" in err
    assert "in cell INTERPOSER" in err
    assert _left_out(layout) == []


def _canon(path):
    """A digest of the composed interposer: every shape, flattened into the
    top cell, as integer-dbu polygons per layer, sorted."""
    layout = db.Layout()
    layout.read(str(path))
    top = layout.top_cell()
    records = []
    for index in layout.layer_indexes():
        info = layout.get_info(index)
        region = db.Region(top.begin_shapes_rec(index)).merged()
        for polygon in region.each():
            records.append("%d/%d %s" % (info.layer, info.datatype, polygon))
    return hashlib.sha256("\n".join(sorted(records)).encode()).hexdigest()


def test_a_pad_one_grid_step_inside_passes_the_guard_and_changes_the_canon(tmp_path):
    """The guard passes this, and so would an XOR within the step; the canon
    does not. One manufacturing grid step (5 dbu), not 1 dbu: the worker
    snaps pad positions to the grid, so a 1 dbu move is not drawn at all."""
    ok, layout = _run(tmp_path / "as-written")
    assert ok is True

    def shift(data):
        data["io_pads"][0]["x_um"] += _h.MANUFACTURING_GRID_NM / 1000.0
    moved = tmp_path / "moved"
    moved.mkdir()
    ok_moved, layout_moved = _run(moved, io_pads=_io_pads(moved, shift))
    assert ok_moved is True
    gds = "%s_interposer.gds" % NAME
    assert _canon(layout / gds) != _canon(layout_moved / gds)


def _snap_line(line):
    """Whether ``line`` is a snapped-pad line, by the template's fixed head."""
    return line.startswith(_h.SNAPPED_PAD_LINE.split("{", 1)[0])


def test_a_pad_off_the_grid_is_snapped_and_the_snap_is_named(tmp_path, capsys):
    """The routed path snaps an off-grid pad as the board path does (one
    add_io_pads for both), and says so: one line per moved pad, its ref and
    both positions in dbu. An on-grid run prints no such line."""
    ok, _layout = _run(tmp_path / "as-written")
    assert ok is True
    assert not any(_snap_line(line) for line in capsys.readouterr().out.splitlines())

    def nudge(data):
        data["io_pads"][0]["x_um"] += 0.001
    moved = tmp_path / "moved"
    moved.mkdir()
    ok_moved, _layout = _run(moved, io_pads=_io_pads(moved, nudge))
    assert ok_moved is True
    expected = _h.SNAPPED_PAD_LINE.format(ref="J_U1_ext[0]", grid=5,
                                          was=(140001, 625000), now=(140000, 625000))
    lines = capsys.readouterr().out.splitlines()
    assert [line for line in lines if line == expected] == [expected]
    assert sum(_snap_line(line) for line in lines) == 1


def test_the_board_path_does_not_name_its_snaps(tmp_path, capsys):
    """Board output is unchanged: the worker on the sealed board-path .hyp
    snaps the same off-grid pad and prints no snap line."""
    def board_frame(data):
        data["frame"] = "board_absolute_ynegated_um"
        for pad in data["io_pads"]:
            pad["y_um"] = -pad["y_um"]
        data["io_pads"][0]["x_um"] += 0.001
    pads = _io_pads(tmp_path, board_frame)
    hyp = stage(tmp_path).parent / "board-path.hyp"
    hyp.write_bytes(BOARD_HYP.read_bytes())
    out = tmp_path / "layout" / ("%s_interposer.gds" % NAME)
    out.parent.mkdir()
    ok = _h.convert_hyp_to_gds(str(hyp), str(out), str(LYP_PATH),
                               io_pads_json=str(pads),
                               die_outline_exempt=["U1", "U2"])
    captured = capsys.readouterr()
    assert ok is True, captured.err
    assert "Placed 6 wire_bond pads" in captured.out
    assert not any(_snap_line(line) for line in captured.out.splitlines())


def _intermediate_chiplet(tmp_path):
    from chiplet_export.routed import write_intermediates
    from chiplet_export.writers.connection_stacks import (
        InterconnectManifestSnapshot, _manifest_reader,
    )
    try:
        manifest = _manifest_reader().load_manifest()
    except ImportError:
        pytest.skip("interconnect_pdk manifest not discoverable on this checkout")
    pins = tmp_path / "pins"
    pins.mkdir()
    result = write_intermediates(
        str(stage(tmp_path / "intermediates")), str(CHIPLET), str(IO_PADS),
        chiplet_output=str(tmp_path / ("%s.chiplet" % NAME)), pins_dir=str(pins),
        board_name=NAME, interconnect_manifest=InterconnectManifestSnapshot(manifest),
        interposer_adapter="intm4tm2", interconnect_adapter="ihp_cupillar")
    return result.chiplet


def _components(path):
    return {c["id"]: c for c in yaml.safe_load(Path(path).read_text())["components"]}


def test_the_finalized_dies_and_pads_are_the_board_paths(tmp_path):
    chiplet = _intermediate_chiplet(tmp_path)
    before = _components(chiplet)
    ok, _layout = _run(tmp_path, chiplet=chiplet)
    assert ok is True
    finalized, golden = _components(chiplet), _components(BOARD_CHIPLET)
    for ref in ("U1", "U2"):
        assert finalized[ref]["anchor"] == golden[ref]["anchor"] == "gds_origin"
        got = (finalized[ref]["position"]["x"], finalized[ref]["position"]["y"])
        want = (golden[ref]["position"]["x"], golden[ref]["position"]["y"])
        assert got == want
        # The intermediate does not already hold the answer.
        assert (before[ref]["position"]["x"], before[ref]["position"]["y"]) != want
    assert finalized["interposer"]["io_pads"] == golden["interposer"]["io_pads"]


def test_the_finalized_pads_keep_their_own_layer(tmp_path):
    def relayer(data):
        for pad in data["io_pads"]:
            pad["layer"] = "TopMetal1"
    chiplet = _intermediate_chiplet(tmp_path)
    ok, _layout = _run(tmp_path, chiplet=chiplet, io_pads=_io_pads(tmp_path, relayer))
    assert ok is True
    pads = _components(chiplet)["interposer"]["io_pads"]
    assert {pad["layer"] for pad in pads} == {"TopMetal1"}


def test_a_direct_finalize_off_the_origin_is_refused_untouched(tmp_path, capsys):
    chiplet = Path(_intermediate_chiplet(tmp_path))
    before = chiplet.read_bytes()
    ok = _h.update_chiplet_file(str(chiplet), str(tmp_path / "absent.gds"),
                                bbox=(-0.001, 0.0, 2600.0, 1400.0),
                                device_origins_um={"U1": (1180.0, 380.0)})
    assert ok is False
    assert OFF_ORIGIN in capsys.readouterr().err
    assert chiplet.read_bytes() == before


def test_the_board_path_still_shifts_by_the_bbox(tmp_path):
    """Without device origins update_chiplet_file is the board path's: the
    pads are shifted by the bbox lower-left and drawn on TopMetal2."""
    chiplet = Path(_intermediate_chiplet(tmp_path))
    model = _h.RoutedGeometryParser(str(stage(tmp_path / "model")))
    model.parse()
    pads = copy.deepcopy(json.loads(IO_PADS.read_text())["io_pads"])
    for pad in pads:
        pad["layer"] = "TopMetal1"
    ok = _h.update_chiplet_file(str(chiplet), str(tmp_path / "absent.gds"),
                                bbox=(-5.0, 0.0, 2605.0, 1400.0), io_pads=pads,
                                devices=model.devices,
                                die_outline_exempt=["U1", "U2"])
    assert ok is True
    written = _components(chiplet)["interposer"]["io_pads"]
    assert written[0]["position"]["x"] == pads[0]["x_um"] + 5.0
    assert {pad["layer"] for pad in written} == {"TopMetal2"}
    u1 = _components(chiplet)["U1"]["position"]
    assert (u1["x"], u1["y"]) == pytest.approx((1180.0 + 5.0, 380.0), abs=1e-6)
