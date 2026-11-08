# SPDX-License-Identifier: GPL-3.0-or-later
"""
The seal ring on the board outline (--seal-ring), and what the files look
like after it.

The ring is the interposer PDK's sealring PCell, placed with the outline as
its l x w. Around it: the outline has to be one on-grid rectangle at least as
large as the PCell accepts; the no-fill keep-outs are cut back to the ring's
39/4; the first write stops before any byte when the export's own fabricated
geometry sits in the ring band or closer to the ring than its space rule; the
boundary sidecar binds the digest of the final bytes, after the metal fill;
and the fill engine is the fill_closure.py of the resolved PDK, nothing else.

Every logic case runs on a fake PCell library registered in this process,
and on the vendored interposer LYP, so none of them needs the interposer PDK.
One case draws the real PCell and skips, saying why, when the PDK is absent.
With the flag off, the worker argv and the GDS bytes are the ones the export
produced before the seal ring existed.
"""
import hashlib
import json
import re
import struct
import sys
import types
from pathlib import Path

import pytest

db = pytest.importorskip("klayout.db")

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
for entry in (PLUGIN_ROOT, PLUGIN_ROOT.parent):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import hyp_to_gds as h  # noqa: E402
from chiplet_export.pipeline.orchestrator import (  # noqa: E402
    ExportOptions, build_cli_args,
)

LYP = str(Path(__file__).resolve().parent / "fixtures"
          / "intm4tm2_reference.lyp")

# --------------------------------------------------------------------------
# a fake sealring PCell library
# --------------------------------------------------------------------------

FAKE_LIBRARY = "SealRingTestLib"
FAKE_MINIMUM_UM = 114.25
# The fake ring follows the PDK's placement: EdgeSeal 30 um inside the
# outline, 3.5 um wide, the metals on the same footprint, and a Passiv ring
# 1 um outside it. Its interior therefore starts 33.5 um in.
EDGESEAL_INSET_NM = 30000
RING_WIDTH_NM = 3500
INTERIOR_INSET_UM = 33.5
RING_LAYERS = ((39, 0), (50, 0), (67, 0), (126, 0), (134, 0))


def _outline_nm(value):
    match = re.fullmatch(r"([0-9]+)(?:\.([0-9]{1,3}))?u", value)
    if match is None:
        return None
    return int(match.group(1)) * 1000 + int((match.group(2) or "0")
                                            .ljust(3, "0"))


class _FakeSealRing(db.PCellDeclarationHelper):
    """l and w as the PDK takes them ('600.000u'); draws nothing for any
    other form, which is what a refusing PCell hands back. ``shift_um``
    draws 39/4 off the outline, ``draw`` False draws nothing at all."""

    def __init__(self, draw=True, shift_um=0.0):
        super().__init__()
        self._draw = draw
        self._shift = int(round(shift_um * 1000))
        self.param("l", self.TypeString, "l", default="1000.000u")
        self.param("w", self.TypeString, "w", default="1000.000u")

    def display_text_impl(self):
        return "sealring(%s, %s)" % (self.l, self.w)

    def produce_impl(self):
        size_x, size_y = _outline_nm(self.l), _outline_nm(self.w)
        if not self._draw or size_x is None or size_y is None:
            return
        ly, s = self.layout, self._shift
        self.cell.shapes(ly.layer(39, 4)).insert(
            db.Box(s, s, size_x + s, size_y + s))

        def ring(inset, width):
            outer = db.Region(db.Box(inset, inset, size_x - inset,
                                     size_y - inset))
            return outer - outer.sized(-width)

        for layer in RING_LAYERS:
            self.cell.shapes(ly.layer(*layer)).insert(
                ring(EDGESEAL_INSET_NM, RING_WIDTH_NM))
        self.cell.shapes(ly.layer(9, 0)).insert(
            ring(EDGESEAL_INSET_NM - 1000 - 4200, 4200))


class _FakeLibrary(db.Library):
    def __init__(self):
        self.description = "seal ring test library"
        self.layout().register_pcell("sealring", _FakeSealRing())
        self.layout().register_pcell("sealring_empty",
                                     _FakeSealRing(draw=False))
        self.layout().register_pcell("sealring_shifted",
                                     _FakeSealRing(shift_um=1.0))
        self.register(FAKE_LIBRARY)


if FAKE_LIBRARY not in db.Library.library_names():
    _FAKE_LIBRARY = _FakeLibrary()


@pytest.fixture
def fake_ring(monkeypatch):
    """The generator draws its ring from the fake library, with no PDK."""
    monkeypatch.setattr(h.GDSGenerator, "SEAL_RING_LIBRARY", FAKE_LIBRARY)
    monkeypatch.setattr(h.GDSGenerator, "_bootstrap_intm4tm2_pcells",
                        lambda self: True)
    monkeypatch.setattr(h.GDSGenerator, "_seal_ring_minimum_um",
                        lambda self: FAKE_MINIMUM_UM)


# --------------------------------------------------------------------------
# boards
# --------------------------------------------------------------------------

RECT = ((0.0, 0.0), (2000.0, 0.0), (2000.0, 1000.0), (0.0, 1000.0))
TRACE = ("TopMetal2", 100.0, 100.0, 1900.0, 100.0, 4.0)


def _m(um):
    return "%.9f" % (um * 1e-6)


def _hyp(tmp_path, loops=(RECT,), segments=(TRACE,), devices=()):
    """A .hyp in micrometres, in the GDS frame (the worker keeps y as is):
    ``loops`` of outline points, ``segments`` (layer, x1, y1, x2, y2, width)
    and ``devices`` (ref, gds, x, y)."""
    lines = ["{VERSION=2.14}", "{UNITS=METRIC LENGTH}", "",
             '{BOARD "synthetic"']
    for loop in loops:
        for (x1, y1), (x2, y2) in zip(loop, loop[1:] + loop[:1]):
            lines.append("  (PERIMETER_SEGMENT X1=%s Y1=%s X2=%s Y2=%s)"
                         % (_m(x1), _m(y1), _m(x2), _m(y2)))
    lines += ["}", "", "{STACKUP"]
    for layer in ("Metal4", "TopMetal2"):
        lines.append('  (SIGNAL T=3.5e-05 P=0 C=1.724e-08 L="%s" M=COPPER)'
                     % layer)
    lines += ["}", ""]
    if devices:
        lines.append("{DEVICES")
        for ref, gds, x, y in devices:
            lines.append('  (? REF="%s" L="TopMetal2" X=%s Y=%s R=0.00 '
                         'GDS_FILE="%s")' % (ref, _m(x), _m(y), gds))
        lines += ["}", ""]
    lines.append('{NET="n1"')
    for layer, x1, y1, x2, y2, width in segments:
        lines.append('  (SEG X1=%s Y1=%s X2=%s Y2=%s W=%s L="%s")'
                     % (_m(x1), _m(y1), _m(x2), _m(y2), _m(width), layer))
    lines.append("}")
    path = tmp_path / "board.hyp"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _die(tmp_path):
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("DIE")
    top.shapes(layout.layer(1235, 0)).insert(db.DBox(0, 0, 300, 200))
    path = tmp_path / "die.gds"
    layout.write(str(path))
    return str(path)


def _convert(tmp_path, hyp, **kwargs):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    ok = h.convert_hyp_to_gds(hyp_path=hyp,
                              output_path=str(out / "demo_interposer.gds"),
                              lyp_path=LYP, **kwargs)
    return ok, out


def _region(gds, layer):
    """(shapes on ``layer`` in the top cell, flattened, dbu) of a GDS. The
    shapes are copied out: a Region over the iterator of a layout that goes
    out of scope reads back empty."""
    layout = db.Layout()
    layout.read(str(gds))
    region = db.Region()
    index = layout.find_layer(*layer)
    if index is not None:
        region.insert(layout.top_cell().begin_shapes_rec(index))
    return region, layout.dbu


def _generator(outline_um=(0.0, 0.0, 600.0, 500.0)):
    """A generator with a board outline and nothing else on it."""
    g = h.GDSGenerator(h.LayerMap(LYP), "TOP", "METRIC", [], None)
    g.top_cell.shapes(g.layout.layer(*g.PRBOUNDARY_LAYER)).insert(
        db.DBox(*outline_um))
    return g


def _draw(g, layer, box_um):
    g.top_cell.shapes(g.layout.layer(*layer)).insert(db.DBox(*box_um))


# --------------------------------------------------------------------------
# The ring on the outline
# --------------------------------------------------------------------------

def test_the_ring_is_drawn_on_the_outline_and_carried_into_the_complete_gds(
        tmp_path, fake_ring, capsys):
    hyp = _hyp(tmp_path, devices=[("U1", _die(tmp_path), 1000.0, 500.0)])
    complete = tmp_path / "out" / "demo_complete.gds"
    ok, out = _convert(tmp_path, hyp, seal_ring=True, with_chiplets=True,
                       complete_output_path=str(complete))
    assert ok is True

    lines = capsys.readouterr().out.splitlines()
    drawn = ("Seal ring: drawn on the board outline (0.000, 0.000)-"
             "(2000.000, 1000.000) um, 2000.000 x 1000.000 um")
    carried = "Seal ring: carried into the complete GDS"
    assert lines.count(drawn) == 1
    assert lines.count(carried) == 1
    assert lines.index(drawn) < lines.index(carried)

    for gds in (out / "demo_interposer.gds", complete):
        boundary, dbu = _region(gds, (39, 4))
        prboundary, _dbu = _region(gds, h.GDSGenerator.PRBOUNDARY_LAYER)
        assert boundary.bbox() == prboundary.bbox()
        assert boundary.bbox() == db.Box(0, 0, 2000000, 1000000)
        edge_seal, _dbu = _region(gds, (39, 0))
        assert edge_seal.merged().holes().bbox() == db.Box(
            33500, 33500, 1966500, 966500)


def test_with_the_flag_off_no_ring_is_drawn(tmp_path, fake_ring, capsys):
    ok, out = _convert(tmp_path, _hyp(tmp_path))
    assert ok is True
    assert "Seal ring" not in capsys.readouterr().out
    for layer in ((39, 4), (39, 0)):
        assert _region(out / "demo_interposer.gds", layer)[0].is_empty()


L_SHAPE = ((0.0, 0.0), (2000.0, 0.0), (2000.0, 500.0), (1000.0, 500.0),
           (1000.0, 1000.0), (0.0, 1000.0))
SMALL = ((0.0, 0.0), (114.245, 0.0), (114.245, 300.0), (0.0, 300.0))
SECOND = tuple((x + 3000.0, y) for x, y in RECT)


@pytest.mark.parametrize("loops,segments,reason", [
    ((), (TRACE,),
     "the board has no outline on prBoundary 235/0 (Edge.Cuts did not give "
     "one closed loop)"),
    ((RECT, SECOND), (TRACE,),
     "the board outline is 2 loops on prBoundary 235/0; the ring needs "
     "exactly one"),
    ((L_SHAPE,), (TRACE,),
     "the board outline is not an axis-aligned rectangle (6 vertices); the "
     "sealring PCell draws a rectangular ring only"),
    ((SMALL,), (("TopMetal2", 50.0, 150.0, 60.0, 150.0, 4.0),),
     "the board outline 114.245 x 300.000 um is below the sealring minimum "
     "of 114.250 um per side"),
], ids=["no-outline", "two-loops", "not-a-rectangle", "under-the-minimum"])
def test_an_outline_the_ring_cannot_take_is_refused(
        tmp_path, fake_ring, capsys, loops, segments, reason):
    ok, out = _convert(tmp_path, _hyp(tmp_path, loops, segments),
                       seal_ring=True)
    assert ok is False
    err = capsys.readouterr().err
    assert "ERROR: seal ring refused: " + reason in err.splitlines()
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("patch,reason", [
    ({"_bootstrap_intm4tm2_pcells": lambda self: False},
     "the SealRingTestLib PCell library could not be loaded (set "
     "INTERPOSER_PDK_ROOT to an interposer PDK checkout)"),
    ({"SEAL_RING_LIBRARY": "NoSuchSealRingLib"},
     "the NoSuchSealRingLib library has no sealring PCell (use an interposer "
     "PDK checkout that ships it)"),
    ({"SEAL_RING_PCELL": "no_such_pcell"},
     "the SealRingTestLib library has no no_such_pcell PCell (use an "
     "interposer PDK checkout that ships it)"),
    ({"SEAL_RING_PCELL": "sealring_empty"},
     "the sealring_empty PCell returned an empty cell for l=2000.000u "
     "w=1000.000u"),
    ({"SEAL_RING_PCELL": "sealring_shifted"},
     "the sealring_shifted PCell drew EdgeSeal.boundary 39/4 at "
     "(1000,1000;2001000,1001000), not on the board outline "
     "(0,0;2000000,1000000)"),
], ids=["no-library", "unknown-library", "no-pcell", "empty-cell",
        "boundary-off-the-outline"])
def test_a_ring_the_pdk_cannot_give_is_refused(
        tmp_path, fake_ring, monkeypatch, capsys, patch, reason):
    for name, value in patch.items():
        monkeypatch.setattr(h.GDSGenerator, name, value)
    ok, out = _convert(tmp_path, _hyp(tmp_path), seal_ring=True)
    assert ok is False
    assert ("ERROR: seal ring refused: " + reason
            in capsys.readouterr().err.splitlines())
    assert list(out.iterdir()) == []


def test_a_refused_pcell_leaves_no_cell_behind(fake_ring, monkeypatch):
    monkeypatch.setattr(h.GDSGenerator, "SEAL_RING_PCELL", "sealring_empty")
    g = _generator()
    before = [cell.name for cell in g.layout.each_cell()]
    with pytest.raises(h.SealRingRefused):
        g.add_seal_ring()
    assert [cell.name for cell in g.layout.each_cell()] == before
    assert g.has_seal_ring() is False


def test_the_worker_cli_exits_1_on_a_refusal(tmp_path, fake_ring,
                                             monkeypatch, capsys):
    hyp = _hyp(tmp_path, loops=(L_SHAPE,))
    out = tmp_path / "cli.gds"
    monkeypatch.setattr(sys, "argv", ["hyp_to_gds.py", hyp, "-o", str(out),
                                      "--lyp", LYP, "--seal-ring"])
    assert h._main_reporting_refusals() == 1
    assert "ERROR: seal ring refused: " in capsys.readouterr().err
    assert not out.exists()


# --------------------------------------------------------------------------
# Keep-outs cut back to 39/4
# --------------------------------------------------------------------------

def test_keep_outs_are_clipped_to_the_ring_boundary(tmp_path, fake_ring,
                                                     capsys):
    nofill = tmp_path / "nofill.json"
    nofill.write_text(json.dumps({"regions": [
        # across the left board edge
        {"role": "global",
         "polygon_um": [[-50, 400], [80, 400], [80, 600], [-50, 600]]},
        # inside, untouched
        {"role": "M4",
         "polygon_um": [[500, 500], [600, 500], [600, 600], [500, 600]]},
        # wholly outside the board
        {"role": "TM2",
         "polygon_um": [[2100, 0], [2200, 0], [2200, 100], [2100, 100]]},
    ]}))
    ok, out = _convert(tmp_path, _hyp(tmp_path), seal_ring=True,
                       nofill_regions_json=str(nofill))
    assert ok is True
    assert ("Seal ring: clipped 2 no-fill keep-out shape(s) to "
            "EdgeSeal.boundary 39/4" in capsys.readouterr().out.splitlines())

    gds = out / "demo_interposer.gds"
    assert _region(gds, (160, 0))[0].bbox() == db.Box(0, 400000,
                                                      80000, 600000)
    assert _region(gds, (50, 23))[0].bbox() == db.Box(500000, 500000,
                                                      600000, 600000)
    assert _region(gds, (134, 23))[0].is_empty()


def test_keep_outs_are_left_alone_with_the_flag_off(tmp_path, capsys):
    nofill = tmp_path / "nofill.json"
    nofill.write_text(json.dumps({"regions": [
        {"role": "global",
         "polygon_um": [[-50, 400], [80, 400], [80, 600], [-50, 600]]}]}))
    ok, out = _convert(tmp_path, _hyp(tmp_path),
                       nofill_regions_json=str(nofill))
    assert ok is True
    assert "clipped" not in capsys.readouterr().out
    assert (_region(out / "demo_interposer.gds", (160, 0))[0].bbox()
            == db.Box(-50000, 400000, 80000, 600000))


# --------------------------------------------------------------------------
# The stop for geometry crowding the ring
# --------------------------------------------------------------------------

@pytest.mark.parametrize("layer,space_um", [
    ((50, 0), 0.21),     # Mn_b
    ((67, 0), 0.21),     # Mn_b
    ((126, 0), 1.64),    # TM1_b
    ((134, 0), 2.0),     # TM2_b
    ((66, 0), 0.005),    # one grid step
    ((9, 0), 0.005),
])
def test_the_space_to_the_ring_is_held_to_the_grid_step(
        tmp_path, fake_ring, layer, space_um):
    """At the rule, the write goes through; one grid step closer, it stops
    before a byte is written."""
    edge = INTERIOR_INSET_UM + space_um

    g = _generator()
    g.add_seal_ring()
    _draw(g, layer, (edge, 200.0, 60.0, 220.0))
    g.write(str(tmp_path / "at_rule.gds"))

    g = _generator()
    g.add_seal_ring()
    _draw(g, layer, (edge - 0.005, 200.0, 60.0, 220.0))
    with pytest.raises(h.SealRingRefused):
        g.write(str(tmp_path / "closer.gds"))
    assert not (tmp_path / "closer.gds").exists()
    assert not (tmp_path / "closer.boundaries.json").exists()


def test_a_trace_in_the_ring_band_stops_the_export(tmp_path, fake_ring,
                                                   capsys):
    band = ("TopMetal2", 100.0, 31.0, 1900.0, 31.0, 4.0)
    hyp = _hyp(tmp_path, segments=(TRACE, band))
    ok, out = _convert(tmp_path, hyp, seal_ring=True)
    assert ok is False
    err = capsys.readouterr().err.splitlines()
    head = err.index("ERROR: seal ring refused: fabricated geometry crowds "
                     "the seal ring")
    # The 4 um trace ends square, 2 um past each endpoint.
    assert err[head + 1:head + 3] == [
        "  134/0: 1 shape(s) in the ring band or closer than TM2_b = 2 um to "
        "the ring, first at (98.000, 29.000)-(1902.000, 33.000) um",
        "  only the basic space is checked here; the wide-metal space "
        "(TM2_bR) is left to the level-2 deck",
    ]
    assert list(out.iterdir()) == []


def test_every_crowding_layer_is_named(tmp_path, fake_ring):
    g = _generator()
    g.add_seal_ring()
    _draw(g, (50, 0), (10.0, 200.0, 40.0, 220.0))
    _draw(g, (50, 0), (560.0, 200.0, 590.0, 220.0))
    _draw(g, (146, 0), (100.0, 480.0, 120.0, 490.0))
    with pytest.raises(h.SealRingRefused) as refused:
        g.write(str(tmp_path / "x.gds"))
    lines = str(refused.value).splitlines()
    assert lines[1] == ("50/0: 2 shape(s) in the ring band or closer than "
                        "Mn_b = 0.21 um to the ring, first at (10.000, "
                        "200.000)-(33.710, 220.000) um")
    assert lines[2].startswith("146/0: 1 shape(s) in the ring band or closer "
                               "than one grid step (0.005 um) to the ring")
    assert "TM2_bR" in lines[-1]


def test_imported_dies_are_not_held_to_the_ring(tmp_path, fake_ring):
    g = _generator()
    g.add_seal_ring()
    die = g.layout.create_cell("DIE")
    die.shapes(g.layout.layer(134, 0)).insert(db.DBox(0, 0, 50, 50))
    g.top_cell.insert(db.DCellInstArray(die, db.DTrans(db.DVector(10, 10))))
    g._foreign_cells.add(die.cell_index())
    g.write(str(tmp_path / "x.gds"))


# --------------------------------------------------------------------------
# The sidecar digest is taken from the final bytes
# --------------------------------------------------------------------------

def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _engine_module(path, fill_stack):
    mod = types.ModuleType("fill_closure")
    mod.__file__ = str(path)
    mod.fill_stack = fill_stack
    return mod


def _stamp_filler(design, output, topcell, **_kw):
    """Changes the bytes the way the real engine does: in place, a filler."""
    layout = db.Layout()
    layout.read(design)
    layout.cell(topcell).shapes(layout.layer(50, 22)).insert(
        db.DBox(500, 500, 505, 505))
    opts = db.SaveLayoutOptions()
    opts.write_context_info = False
    layout.write(output, opts)
    return {"converged": True}


@pytest.mark.parametrize("seal_ring", [False, True], ids=["ring-off",
                                                          "ring-on"])
def test_with_the_fill_on_the_sidecar_binds_the_filled_bytes(
        tmp_path, fake_ring, monkeypatch, capsys, seal_ring):
    monkeypatch.setattr(h.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(h, "_import_fill_closure", lambda: _engine_module(
        tmp_path / "fill_closure.py", _stamp_filler))
    ok, out = _convert(tmp_path, _hyp(tmp_path), cell_name="INTERPOSER",
                       insert_metal_fill=True, seal_ring=seal_ring)
    assert ok is True

    gds = out / "demo_interposer.gds"
    assert not _region(gds, (50, 22))[0].is_empty()
    manifest = json.loads((out / "demo_interposer.boundaries.json")
                          .read_text())
    assert manifest["assembly_gds_sha256"] == _sha(gds)

    lines = capsys.readouterr().out.splitlines()
    written = [i for i, line in enumerate(lines)
               if line.startswith("  Boundary manifest written to: ")]
    assert len(written) == 1
    assert written[0] > lines.index(
        "Fill engine: %s" % (tmp_path / "fill_closure.py").resolve())
    assert written[0] > next(i for i, line in enumerate(lines)
                             if line.startswith("Fill density report: "))


def test_with_the_fill_off_the_sidecar_is_written_with_the_gds(tmp_path,
                                                               capsys):
    ok, out = _convert(tmp_path, _hyp(tmp_path))
    assert ok is True
    gds = out / "demo_interposer.gds"
    manifest = json.loads((out / "demo_interposer.boundaries.json")
                          .read_text())
    assert manifest["assembly_gds_sha256"] == _sha(gds)
    lines = capsys.readouterr().out.splitlines()
    written = next(i for i, line in enumerate(lines)
                   if line.startswith("  Boundary manifest written to: "))
    assert written < lines.index("Interposer GDS file written to: %s" % gds)


def test_a_skipped_fill_still_writes_the_sidecar(tmp_path, monkeypatch,
                                                 capsys):
    monkeypatch.setattr(h.shutil, "which", lambda name: None)
    ok, out = _convert(tmp_path, _hyp(tmp_path), insert_metal_fill=True)
    assert ok is True
    gds = out / "demo_interposer.gds"
    manifest = json.loads((out / "demo_interposer.boundaries.json")
                          .read_text())
    assert manifest["assembly_gds_sha256"] == _sha(gds)
    assert "Fill engine: " not in capsys.readouterr().out


def test_a_fill_that_stops_writes_no_sidecar(tmp_path, monkeypatch):
    def stop(*_a, **_kw):
        raise RuntimeError("fill_metal_run.drc: EdgeSeal ring has no "
                           "interior")

    monkeypatch.setattr(h.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(h, "_import_fill_closure", lambda: _engine_module(
        tmp_path / "fill_closure.py", stop))
    ok, out = _convert(tmp_path, _hyp(tmp_path), insert_metal_fill=True)
    assert ok is False
    assert sorted(p.name for p in out.iterdir()) == ["demo_interposer.gds"]


# --------------------------------------------------------------------------
# The complete GDS shares the filled interposer's frame
# --------------------------------------------------------------------------

def _top_shapes(gds, layer):
    """(top cell name, dbu, the top cell's own shapes on ``layer``)."""
    layout = db.Layout()
    layout.read(str(gds))
    top = layout.top_cell()
    index = layout.find_layer(*layer)
    region = db.Region()
    if index is not None:
        region.insert(top.shapes(index))
    return top.name, layout.dbu, region


def _layers(gds):
    layout = db.Layout()
    layout.read(str(gds))
    return {(info.layer, info.datatype) for info in
            (layout.get_info(i) for i in layout.layer_indexes())}


def test_the_complete_gds_carries_the_filled_interposer_at_identity(
        tmp_path, fake_ring, monkeypatch):
    # A viewer overlays the interposer's fill on the complete GDS. It may
    # take the frames to agree on the top cell's name, the dbu and its 235/0;
    # every carrier shape is in the complete GDS where it is in the
    # interposer. The bbox is not part of it: U2 hangs past the outline.
    monkeypatch.setattr(h.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(h, "_import_fill_closure", lambda: _engine_module(
        tmp_path / "fill_closure.py", _stamp_filler))
    die = _die(tmp_path)
    hyp = _hyp(tmp_path, devices=[("U1", die, 1000.0, 500.0),
                                  ("U2", die, 1900.0, 300.0)])
    complete = tmp_path / "out" / "demo_complete.gds"
    ok, out = _convert(tmp_path, hyp, cell_name="INTERPOSER",
                       insert_metal_fill=True, seal_ring=True,
                       with_chiplets=True, complete_output_path=str(complete))
    assert ok is True
    interposer = out / "demo_interposer.gds"

    name, dbu, prboundary = _top_shapes(interposer,
                                        h.GDSGenerator.PRBOUNDARY_LAYER)
    c_name, c_dbu, c_prboundary = _top_shapes(complete,
                                              h.GDSGenerator.PRBOUNDARY_LAYER)
    assert (c_name, c_dbu) == (name, dbu) == ("INTERPOSER", 0.001)
    assert prboundary.count() == c_prboundary.count() == 1
    assert (prboundary ^ c_prboundary).is_empty()

    fill_layers = set(h._filler_layers(h.LayerMap(LYP)))
    assert not _region(interposer, (50, 22))[0].is_empty()
    assert _region(complete, (50, 22))[0].is_empty()
    for layer in sorted(_layers(interposer) - fill_layers):
        carrier = _region(interposer, layer)[0]
        assert (carrier - _region(complete, layer)[0]).is_empty(), layer

    assert _region(complete, (1235, 0))[0].bbox().right > prboundary.bbox().right


# --------------------------------------------------------------------------
# The fill engine comes from the resolved PDK root, and says so
# --------------------------------------------------------------------------

ENGINE_SOURCE = (
    "def fill_stack(design, output, **_kw):\n"
    "    return {'converged': True}\n")


def _pdk_root(tmp_path, name="pdk", files=("fill_closure.py",)):
    root = tmp_path / name
    python_dir = root / "libs.tech" / "klayout" / "python"
    python_dir.mkdir(parents=True)
    for file_name in files:
        (python_dir / file_name).write_text(ENGINE_SOURCE)
    return root


@pytest.fixture
def clean_import(monkeypatch):
    """No fill_closure imported yet, and sys.path restored afterwards."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "fill_closure", raising=False)


def _no_walk(name):
    raise AssertionError("walked to a sibling checkout for %s" % name)


@pytest.mark.parametrize("files", [(), ("fill_stack.py",)],
                         ids=["no-engine", "fill-stack-only"])
def test_a_set_root_without_the_engine_is_an_error_with_no_walk(
        tmp_path, monkeypatch, clean_import, capsys, files):
    root = _pdk_root(tmp_path, files=files)
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(root))
    monkeypatch.setattr(h, "_discover_path_var", _no_walk)
    assert h._import_fill_closure() is None
    err = capsys.readouterr().err
    assert "INTERPOSER_PDK_ROOT=%s has no fill engine" % root in err
    assert "fill_closure" not in sys.modules


def test_the_engine_is_imported_from_the_set_root(tmp_path, monkeypatch,
                                                  clean_import):
    root = _pdk_root(tmp_path)
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(root))
    monkeypatch.setattr(h, "_discover_path_var", _no_walk)
    mod = h._import_fill_closure()
    assert mod is not None
    assert Path(mod.__file__).resolve() == (
        root / "libs.tech" / "klayout" / "python" / "fill_closure.py"
    ).resolve()


def test_an_unset_root_walks_to_the_sibling_checkout(tmp_path, monkeypatch,
                                                     clean_import):
    root = _pdk_root(tmp_path, name="interposer")
    monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
    monkeypatch.setattr(h, "_discover_path_var", lambda name: str(root))
    assert h._fill_engine_path() == (
        root / "libs.tech" / "klayout" / "python" / "fill_closure.py"
    ).resolve()


def test_an_engine_imported_from_elsewhere_is_refused(tmp_path, monkeypatch,
                                                      clean_import, capsys):
    root = _pdk_root(tmp_path)
    other = _pdk_root(tmp_path, name="other")
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(root))
    stale = types.ModuleType("fill_closure")
    stale.__file__ = str(other / "libs.tech" / "klayout" / "python"
                         / "fill_closure.py")
    monkeypatch.setitem(sys.modules, "fill_closure", stale)
    assert h._import_fill_closure() is None
    assert "refusing an engine from another checkout" in (
        capsys.readouterr().err)


def test_a_filling_run_names_the_engine_it_imported(tmp_path, monkeypatch,
                                                     clean_import, capsys):
    root = _pdk_root(tmp_path)
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(root))
    monkeypatch.setattr(h.shutil, "which", lambda name: "/usr/bin/" + name)
    gds = tmp_path / "interposer.gds"
    layout = db.Layout()
    layout.create_cell("INTERPOSER").shapes(layout.layer(235, 0)).insert(
        db.Box(0, 0, 1000, 1000))
    layout.write(str(gds))

    result = h._insert_metal_fill(str(gds), "INTERPOSER", "single-pass", [])
    assert result["status"] == "ok"
    engine = (root / "libs.tech" / "klayout" / "python"
              / "fill_closure.py").resolve()
    assert ("Fill engine: %s" % engine
            in capsys.readouterr().out.splitlines())


# --------------------------------------------------------------------------
# the real PCell, through the interposer PDK
# --------------------------------------------------------------------------

def _real_sealring_absent():
    root = h._discover_path_var("INTERPOSER_PDK_ROOT")
    if root is None:
        return ("needs the interposer PDK (INTERPOSER_PDK_ROOT or a sibling "
                "checkout) for the real sealring PCell")
    code = (Path(root) / "libs.tech" / "klayout" / "python"
            / "intm4tm2_pycell_lib" / "ihp" / "sealring_code.py")
    if not code.is_file():
        return "the interposer PDK at %s predates the sealring PCell" % root
    return ""


@pytest.mark.needs_sibling_sources
@pytest.mark.skipif(bool(_real_sealring_absent()),
                    reason=_real_sealring_absent() or "unused")
def test_the_real_pcell_draws_on_the_outline(tmp_path):
    g = _generator((100.0, 50.0, 700.0, 550.0))
    ring = g.add_seal_ring()
    assert g._seal_ring_minimum_um() == pytest.approx(114.25)
    assert (ring.left, ring.bottom, ring.right, ring.top) == (
        100.0, 50.0, 700.0, 550.0)
    gds = tmp_path / "real.gds"
    g.write(str(gds))

    layout = db.Layout()
    layout.read(str(gds))
    assert all(cell.name for cell in layout.each_cell())
    boundary, _dbu = _region(gds, (39, 4))
    assert boundary.bbox() == db.Box(100000, 50000, 700000, 550000)
    interior = _region(gds, (39, 0))[0].merged().holes()
    assert interior.count() == 1
    for layer in ((50, 0), (67, 0), (126, 0), (134, 0), (9, 0)):
        assert not _region(gds, layer)[0].is_empty()


@pytest.mark.needs_sibling_sources
@pytest.mark.skipif(bool(_real_sealring_absent()),
                    reason=_real_sealring_absent() or "unused")
def test_a_cmim_placed_before_the_ring_survives_it(tmp_path):
    """The ring binds the intm4tm2 technology a second time, after the CMIM
    PCells; their cells have to come through that with their geometry."""
    sidecar = tmp_path / "cmim_devices.json"
    sidecar.write_text(json.dumps({"version": 2, "cmim_devices": [
        {"ref": "C1", "x_um": 300.0, "y_um": 250.0, "w_um": 57.68,
         "l_um": 57.68, "m": 1}]}))
    g = _generator()
    assert g.add_cmim_devices(h.load_cmim_devices(str(sidecar))) == (1, [])
    g.add_seal_ring()
    gds = tmp_path / "cmim_ring.gds"
    g.write(str(gds))
    mim, _dbu = _region(gds, (36, 0))
    assert not mim.is_empty()
    assert mim.bbox().center() == db.Point(300000, 250000)


@pytest.mark.needs_sibling_sources
@pytest.mark.skipif(bool(_real_sealring_absent()),
                    reason=_real_sealring_absent() or "unused")
def test_the_real_minimum_is_the_pcells(tmp_path):
    g = _generator((0.0, 0.0, 114.25, 114.25))
    g.add_seal_ring()
    g = _generator((0.0, 0.0, 114.245, 200.0))
    with pytest.raises(h.SealRingRefused) as refused:
        g.add_seal_ring()
    assert "below the sealring minimum of 114.250 um" in str(refused.value)


# --------------------------------------------------------------------------
# the clearance layers are the deck's
# --------------------------------------------------------------------------

def _seal_deck_dir():
    root = h._discover_path_var("INTERPOSER_PDK_ROOT")
    if root is None:
        return None
    return Path(root) / "libs.tech" / "klayout" / "tech" / "drc" / "rule_decks"


def _seal_deck_absent():
    decks = _seal_deck_dir()
    if decks is None:
        return ("needs the interposer PDK (INTERPOSER_PDK_ROOT or a sibling "
                "checkout) for its sealring deck")
    for name in ("6_10_sealring.drc", "layers_def.drc"):
        if not (decks / name).is_file():
            return "the interposer PDK has no %s" % (decks / name)
    return ""


def _deck_layers(*decks):
    """{name: (layer, datatype)} for every ``name = polygons(l, d)`` in the
    decks, and every ``name = other`` alias of one, followed to the end."""
    polygons, aliases = {}, {}
    for deck in decks:
        for line in deck.read_text().splitlines():
            match = re.match(r"\s*(\w+)\s*=\s*polygons\(\s*(\d+)\s*,\s*(\d+)"
                             r"\s*\)\s*(#.*)?$", line)
            if match:
                polygons[match.group(1)] = (int(match.group(2)),
                                            int(match.group(3)))
                aliases.pop(match.group(1), None)
                continue
            match = re.match(r"\s*(\w+)\s*=\s*([a-z_]\w*)\s*(#.*)?$", line)
            if match:
                aliases[match.group(1)] = match.group(2)
                polygons.pop(match.group(1), None)
    resolved = dict(polygons)
    for name in aliases:
        seen, target = {name}, aliases[name]
        while target in aliases and target not in seen:
            seen.add(target)
            target = aliases[target]
        if target in polygons:
            resolved[name] = polygons[target]
    return resolved


@pytest.mark.needs_sibling_sources
@pytest.mark.skipif(bool(_seal_deck_absent()),
                    reason=_seal_deck_absent() or "unused")
def test_the_clearance_layers_are_the_decks_seal_req_4_layers():
    """SEAL_RING_CLEARANCE is a copy of what the sealring deck requires inside
    the ring; a layer the deck adds would otherwise leave the stop short."""
    decks = _seal_deck_dir()
    seal_deck = decks / "6_10_sealring.drc"
    arrays = re.findall(r"\[([^\[\]]*)\]\.each do \|seal_req_layer\|",
                        seal_deck.read_text())
    assert len(arrays) == 1, "SEAL.REQ.4 array not found in %s" % seal_deck
    names = [n.strip() for n in arrays[0].split(",") if n.strip()]
    assert names

    layers = _deck_layers(decks / "layers_def.drc", seal_deck)
    unresolved = [n for n in names if n not in layers]
    assert unresolved == [], "no polygons(l, d) for %s" % unresolved
    required = {layers[n] for n in names} | {(9, 0)}      # plus Passiv
    assert required == set(h.GDSGenerator.SEAL_RING_CLEARANCE)


# --------------------------------------------------------------------------
# flag off: the argv and the bytes are the ones from before the ring
# --------------------------------------------------------------------------

# build_cli_args for ExportOptions(output_dir="/out") on board "demo", as the
# export produced it before --seal-ring existed.
ARGV_BEFORE = [
    "hyp_to_gds.py", "in.hyp",
    "-o", "/out/layout/demo_interposer.gds",
    "-c", "INTERPOSER",
    "--update-chiplet-file", "/out/demo.chiplet",
]

# sha256 of the GDS the ablation board converts to, with the BGNLIB and
# BGNSTR timestamps zeroed, as the export produced it before the ring.
# Re-pinned for square track ends: against the flush-end GDS (25e22d3f)
# the merged copper only gains the six w/2 end caps of its three traces.
GDS_BEFORE = "650e062d7f125f354e031654f845ab68df5c21b570c4a1626c1903d1be3b993d"


def _timeless_sha(path):
    """sha256 of a GDS with its modification and access dates zeroed."""
    data = bytearray(Path(path).read_bytes())
    pos = 0
    while pos + 4 <= len(data):
        length, record = struct.unpack(">HB", data[pos:pos + 3])
        if length < 4:
            break
        if record in (0x01, 0x05):          # BGNLIB, BGNSTR
            data[pos + 4:pos + length] = bytes(length - 4)
        pos += length
    return hashlib.sha256(bytes(data)).hexdigest()


def test_the_flag_off_argv_is_the_argv_from_before():
    off = build_cli_args("hyp_to_gds.py", "in.hyp", "demo",
                         ExportOptions(output_dir="/out"))
    on = build_cli_args("hyp_to_gds.py", "in.hyp", "demo",
                        ExportOptions(output_dir="/out", seal_ring=True))
    assert off == ARGV_BEFORE
    assert on.count("--seal-ring") == 1
    assert [arg for arg in on if arg != "--seal-ring"] == off


def _ablation_board(tmp_path):
    nofill = tmp_path / "nofill.json"
    nofill.write_text(json.dumps({"regions": [
        {"role": "global",
         "polygon_um": [[-50, 400], [80, 400], [80, 600], [-50, 600]]}]}))
    hyp = _hyp(tmp_path, segments=(
        TRACE,
        ("TopMetal2", 100.0, 31.0, 1900.0, 31.0, 4.0),
        ("Metal4", 200.0, 300.0, 200.0, 900.0, 2.0),
        ("Metal4", 200.0, 900.0, 600.0, 500.0, 2.0),
    ))
    return hyp, str(nofill)


def _sg13g2_absent():
    root = h._discover_path_var("PDK_ROOT")
    return not root or not Path(root).joinpath(
        *h.GDSGenerator._SG13G2_KLAYOUT_SUBPATH).is_dir()


# GDS_BEFORE was written with the SG13G2 PDK present: its via PCells add
# layers to the layout before the export's own, which sets the order the
# writer emits them in. Without the PDK the geometry is the same and the
# bytes are not.
@pytest.mark.skipif(_sg13g2_absent(),
                    reason="needs the SG13G2 PDK (PDK_ROOT) the pinned bytes "
                           "were written with")
@pytest.mark.parametrize("flag", [{}, {"seal_ring": False}],
                         ids=["omitted", "false"])
def test_the_flag_off_bytes_are_the_bytes_from_before(tmp_path, flag):
    hyp, nofill = _ablation_board(tmp_path)
    ok, out = _convert(tmp_path, hyp, nofill_regions_json=nofill, **flag)
    assert ok is True
    gds = out / "demo_interposer.gds"
    assert _timeless_sha(gds) == GDS_BEFORE
    manifest = json.loads((out / "demo_interposer.boundaries.json")
                          .read_text())
    assert manifest["assembly_gds_sha256"] == _sha(gds)
