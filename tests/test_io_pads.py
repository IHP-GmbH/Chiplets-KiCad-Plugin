"""Tests for the I/O pad support added to hyp_to_gds.GDSGenerator."""
import json
import sys
from pathlib import Path

import klayout.db as db
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hyp_to_gds import GDSGenerator, LayerMap, update_chiplet_file  # noqa: E402

# The interposer LYP lives in the interposer/ subproject. Allow override via env.
import os  # noqa: E402
LYP_PATH = Path(os.environ.get(
    "INTERPOSER_LYP",
    REPO.parents[1] / "interposer" / "interposer_klayout" / "tech" / "intm4tm2.lyp",
))
TM2_LAYER = (134, 0)


@pytest.fixture(scope="session", autouse=True)
def _require_lyp():
    if not LYP_PATH.exists():
        pytest.skip(f"interposer LYP not found: {LYP_PATH}")


def _make_generator() -> GDSGenerator:
    return GDSGenerator(LayerMap(str(LYP_PATH)), "TOP", "METRIC", [], None)


def _read_top_cell(gds_path: Path):
    layout = db.Layout()
    layout.read(str(gds_path))
    layout.flatten(layout.top_cell().cell_index(), -1, True)
    return layout, layout.top_cell()


def test_add_io_pads_creates_topmetal2_squares(tmp_path):
    gen = _make_generator()
    pads = {
        "io_pads": [
            {"ref": "J1", "io_class": "wire_bond",
             "x_um": 0.0, "y_um": 0.0,
             "size_x_um": 100.0, "size_y_um": 100.0, "net": "VDD_EXT"},
            {"ref": "J2", "io_class": "wire_bond",
             "x_um": 200.0, "y_um": 50.0,
             "size_x_um": 100.0, "size_y_um": 100.0, "net": "OUT0"},
        ]
    }
    json_path = tmp_path / "io_pads.json"
    json_path.write_text(json.dumps(pads))

    placed = gen.add_io_pads(str(json_path))
    assert len(placed) == 2

    out_gds = tmp_path / "out.gds"
    gen.write(str(out_gds))

    layout, top = _read_top_cell(out_gds)
    layer_idx = layout.find_layer(*TM2_LAYER)
    assert layer_idx is not None, "TopMetal2 (134/0) layer missing in output"

    boxes = [s.box for s in top.shapes(layer_idx).each() if s.is_box()]
    assert len(boxes) == 2

    # Coordinates are in DBU (1 DBU = 1 nm) -> 100 um = 100_000 nm
    centers = sorted(((b.left + b.right) // 2, (b.bottom + b.top) // 2)
                      for b in boxes)
    assert centers == [(0, 0), (200_000, 50_000)]

    # Each box must be 100 um x 100 um
    for b in boxes:
        assert (b.right - b.left) == 100_000
        assert (b.top - b.bottom) == 100_000


def test_io_class_dispatch_skips_reserved(tmp_path):
    gen = _make_generator()
    pads = {
        "io_pads": [
            {"ref": "J1", "io_class": "wire_bond", "x_um": 0.0, "y_um": 0.0,
             "size_x_um": 100.0, "size_y_um": 100.0, "net": "A"},
            {"ref": "J2", "io_class": "flipped_bump", "x_um": 100.0, "y_um": 0.0,
             "size_x_um": 80.0, "size_y_um": 80.0, "net": "B"},
            {"ref": "J3", "io_class": "tsv_bump", "x_um": -100.0, "y_um": 0.0,
             "size_x_um": 60.0, "size_y_um": 60.0, "net": "C"},
        ]
    }
    json_path = tmp_path / "io_pads.json"
    json_path.write_text(json.dumps(pads))

    placed = gen.add_io_pads(str(json_path))
    assert len(placed) == 1
    assert placed[0]["ref"] == "J1"


def test_add_io_pads_handles_missing_file(tmp_path):
    gen = _make_generator()
    placed = gen.add_io_pads(str(tmp_path / "does_not_exist.json"))
    assert placed == []


def test_add_io_pads_skips_invalid_size(tmp_path):
    gen = _make_generator()
    pads = {"io_pads": [
        {"ref": "J1", "io_class": "wire_bond",
         "x_um": 0.0, "y_um": 0.0,
         "size_x_um": 0.0, "size_y_um": 100.0, "net": "X"},
    ]}
    json_path = tmp_path / "io_pads.json"
    json_path.write_text(json.dumps(pads))
    placed = gen.add_io_pads(str(json_path))
    assert placed == []


def test_update_chiplet_file_injects_io_pads(tmp_path):
    chiplet_path = tmp_path / "demo.chiplet"
    chiplet_path.write_text(
        "format_version: '1.0'\n"
        "assembly:\n"
        "  name: demo\n"
        "  units: um\n"
        "components:\n"
        "- id: interposer\n"
        "  type: interposer\n"
        "  technology: intm4tm2\n"
        "  layout: ''\n"
        "  dimensions:\n"
        "    width: 1000.0\n"
        "    height: 1000.0\n"
        "    thickness: 13.83\n"
    )
    # Create a minimal interposer GDS so update_chiplet_file can compute bbox
    layout = db.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("TOP")
    layer_idx = layout.layer(*TM2_LAYER)
    cell.shapes(layer_idx).insert(db.DBox(-500.0, -500.0, 500.0, 500.0))
    interposer_gds = tmp_path / "interposer.gds"
    layout.write(str(interposer_gds))

    placed = [
        {"ref": "J1", "io_class": "wire_bond",
         "x_um": 100.0, "y_um": 200.0,
         "size_x_um": 100.0, "size_y_um": 100.0, "net": "VDD_EXT"},
        {"ref": "J2", "io_class": "wire_bond",
         "x_um": -100.0, "y_um": 200.0,
         "size_x_um": 150.0, "size_y_um": 150.0, "net": "OUT0"},
    ]
    ok = update_chiplet_file(str(chiplet_path), str(interposer_gds),
                              io_pads=placed)
    assert ok

    import yaml
    with chiplet_path.open() as f:
        data = yaml.safe_load(f)
    interposer = next(c for c in data["components"] if c["id"] == "interposer")
    assert "io_pads" in interposer
    pads = interposer["io_pads"]
    assert len(pads) == 2
    j1 = next(p for p in pads if p["id"] == "J1")
    assert j1["io_class"] == "wire_bond"
    assert j1["net"] == "VDD_EXT"
    assert j1["position"] == {"x": 100.0, "y": 200.0}
    assert j1["size"] == {"x": 100.0, "y": 100.0}
    assert j1["layer"] == "TopMetal2"
