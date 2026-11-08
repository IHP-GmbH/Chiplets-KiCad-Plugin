# SPDX-License-Identifier: GPL-3.0-or-later
"""No-fill region extraction against the shape names pcbnew really exports.

Contract/API doubles, not a claim of native pcbnew execution.
"""

import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

WRITER = Path(__file__).resolve().parents[1] / "writers" / "chiplet_writer.py"

#: The SHAPE_T names the SWIG bindings define: one per SHAPE_T enumerator
#: (include/eda_shape.h), plus the SHAPE_T_RECT alias pcb_shape.i adds. There is
#: no SHAPE_T_POLYGON; a lookup of it raises AttributeError on a real board.
REAL_SHAPE_T = {
    "SHAPE_T_UNDEFINED", "SHAPE_T_SEGMENT", "SHAPE_T_RECTANGLE", "SHAPE_T_ARC",
    "SHAPE_T_CIRCLE", "SHAPE_T_POLY", "SHAPE_T_BEZIER", "SHAPE_T_RECT",
}


class _Strict(SimpleNamespace):
    """A pcbnew double that raises on any name it does not define."""

    def __getattr__(self, name):
        raise AttributeError("module 'pcbnew' has no attribute %r" % name)


class _Shape:
    pass


PCBNEW = _Strict(
    PCB_IU_PER_MM=1_000_000, PCB_SHAPE=_Shape,
    SHAPE_T_SEGMENT=0, SHAPE_T_RECTANGLE=1, SHAPE_T_RECT=1, SHAPE_T_ARC=2,
    SHAPE_T_CIRCLE=3, SHAPE_T_POLY=4, SHAPE_T_BEZIER=5,
)


def _pt(x_um, y_um):
    return SimpleNamespace(x=int(x_um * 1000), y=int(y_um * 1000))


def _polyset(points_um):
    outline = SimpleNamespace(
        PointCount=lambda: len(points_um),
        CPoint=lambda i: _pt(*points_um[i]))
    return SimpleNamespace(OutlineCount=lambda: 1, COutline=lambda i: outline)


def _shape(layer, kind, **kw):
    item = _Shape()
    item.GetLayer = lambda: layer
    item.GetShape = lambda: kind
    for name, value in kw.items():
        setattr(item, name, lambda value=value: value)
    return item


def test_writer_uses_only_shape_names_pcbnew_defines():
    used = set(re.findall(r"pcbnew\.(SHAPE_T_\w+)", WRITER.read_text()))
    assert used, "the no-fill reader no longer compares shapes; update this test"
    assert used <= REAL_SHAPE_T, sorted(used - REAL_SHAPE_T)


def test_rect_polygon_and_zone_on_nofill_layers_are_all_extracted(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    from chiplet_export.writers import chiplet_writer
    monkeypatch.setattr(chiplet_writer, "pcbnew", PCBNEW)

    layers = {1: "TM2.nofill", 2: "NoMetFiller", 3: "M4.nofill", 4: "F.Fab"}
    drawings = [
        _shape(1, PCBNEW.SHAPE_T_RECT, GetStart=_pt(0, 0), GetEnd=_pt(10, 20)),
        _shape(2, PCBNEW.SHAPE_T_POLY,
               GetPolyShape=_polyset([(0, 0), (5, 0), (5, 5)])),
        _shape(4, PCBNEW.SHAPE_T_RECT, GetStart=_pt(0, 0), GetEnd=_pt(1, 1)),
        _shape(1, PCBNEW.SHAPE_T_SEGMENT),
    ]
    zone = SimpleNamespace(
        GetLayer=lambda: 3, Outline=lambda: _polyset([(1, 1), (2, 1), (2, 2)]))
    board = SimpleNamespace(
        GetDrawings=lambda: drawings, Zones=lambda: [zone],
        GetLayerName=lambda layer: layers[layer])

    out = tmp_path / "nofill.json"
    assert chiplet_writer.write_nofill_regions_json(board, str(out)) == 3
    regions = json.loads(out.read_text())["regions"]
    assert [r["role"] for r in regions] == ["TM2", "global", "M4"]
    assert regions[0]["polygon_um"] == [[0, 0], [10, 0], [10, -20], [0, -20]]
    assert regions[1]["polygon_um"] == [[0, 0], [5, 0], [5, -5]]
