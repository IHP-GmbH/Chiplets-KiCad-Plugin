"""Tests for the opt-in, viewer-only boundary annotation layer in hyp_to_gds.

The annotation layer restores eyeball inspection of chiplet boundaries (lost
when the boundary moved off the 190/0 fab layer into the manifest). It must be
strictly opt-in and read by no DRC rule -- these tests pin that contract:
  * off by default (no synthetic geometry in the production GDS),
  * when on, one polygon + one instance label per boundary on the chosen layer,
  * never the legacy 190/0 fab layer,
  * the boundary manifest is unaffected (annotation is not the contract).
"""
import json
import os
import sys
from pathlib import Path

import klayout.db as db
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hyp_to_gds import GDSGenerator, LayerMap  # noqa: E402

# The plugin bundles the LYP it uses by default; prefer it so the test is
# self-contained (no dependency on the sibling interposer subproject).
LYP_PATH = Path(os.environ.get("INTERPOSER_LYP", REPO / "intm4tm2.lyp"))
EXCHANGE0 = (190, 0)
DEFAULT_VIZ = (1000, 0)


@pytest.fixture(scope="session", autouse=True)
def _require_lyp():
    if not LYP_PATH.exists():
        pytest.skip(f"interposer LYP not found: {LYP_PATH}")


def _make_generator(**kwargs) -> GDSGenerator:
    return GDSGenerator(LayerMap(str(LYP_PATH)), "TOP", "METRIC", [], None, **kwargs)


def _fake_records():
    # Two chiplets, axis-aligned rectangles, in DBU (1 DBU = 1 nm).
    return [
        {"instance": "U1", "source_die": "ACME_PHY", "class": "chiplet",
         "polygon_dbu": [[0, 0], [200_000, 0], [200_000, 150_000], [0, 150_000]]},
        {"instance": "U2", "source_die": "ACME_MEM", "class": "chiplet",
         "polygon_dbu": [[300_000, 0], [500_000, 0], [500_000, 150_000], [300_000, 150_000]]},
    ]


def _write_with_records(tmp_path, name, **gen_kwargs):
    gen = _make_generator(**gen_kwargs)
    gen._boundary_records = _fake_records()
    out = tmp_path / name
    gen.write(str(out))
    layout = db.Layout()
    layout.read(str(out))
    return layout, layout.top_cell(), out


def test_annotation_off_by_default(tmp_path):
    layout, _, _ = _write_with_records(tmp_path, "no_annot.gds")
    assert layout.find_layer(*DEFAULT_VIZ) is None, \
        "annotation layer must be absent unless --annotate-boundaries is set"


def test_annotation_paints_polygon_and_label_per_boundary(tmp_path):
    layout, top, _ = _write_with_records(tmp_path, "annot.gds", annotate_boundaries=True)
    idx = layout.find_layer(*DEFAULT_VIZ)
    assert idx is not None, "annotation layer 1000/0 missing"

    polys = [s for s in top.shapes(idx).each() if s.is_polygon() or s.is_box()]
    texts = [s for s in top.shapes(idx).each() if s.is_text()]
    assert len(polys) == 2, "one boundary polygon per chiplet expected"
    assert {t.text.string for t in texts} == {"U1", "U2"}, \
        "each boundary must carry its instance label"


def test_annotation_never_uses_exchange0(tmp_path):
    layout, _, _ = _write_with_records(tmp_path, "annot.gds", annotate_boundaries=True)
    assert layout.find_layer(*EXCHANGE0) is None, \
        "annotation must not resurrect the 190/0 fab layer"


def test_custom_viz_layer_is_honored(tmp_path):
    layout, _, _ = _write_with_records(
        tmp_path, "annot.gds", annotate_boundaries=True, boundary_viz_layer=(1234, 5))
    assert layout.find_layer(1234, 5) is not None
    assert layout.find_layer(*DEFAULT_VIZ) is None


def test_dual_write_interposer_empty_then_complete(tmp_path):
    # Mirrors the real flow: one generator writes the interposer GDS (no
    # chiplets yet -> empty records) and later the complete GDS (records full).
    # The interposer must carry no annotation; the complete must not duplicate.
    gen = _make_generator(annotate_boundaries=True)

    interposer = tmp_path / "interposer.gds"
    gen.write(str(interposer))  # records still empty
    lay_i = db.Layout()
    lay_i.read(str(interposer))
    assert lay_i.find_layer(*DEFAULT_VIZ) is None, \
        "interposer GDS (no chiplets) must carry no annotation"

    gen._boundary_records = _fake_records()
    complete = tmp_path / "complete.gds"
    gen.write(str(complete))
    lay_c = db.Layout()
    lay_c.read(str(complete))
    idx = lay_c.find_layer(*DEFAULT_VIZ)
    assert idx is not None
    polys = [s for s in lay_c.top_cell().shapes(idx).each()
             if s.is_polygon() or s.is_box()]
    assert len(polys) == 2, "complete GDS must hold exactly one rect per chiplet"


def test_annotation_does_not_alter_manifest(tmp_path):
    _, _, out = _write_with_records(tmp_path, "annot.gds", annotate_boundaries=True)
    manifest = json.loads(out.with_name("annot.boundaries.json").read_text())
    assert len(manifest["boundaries"]) == 2
    assert manifest["schema"] == "adk-boundary-manifest"
    # Version pin: the adk readers validate this exact string (see
    # adk/docs/boundary_manifest.md); bump both sides together.
    assert manifest["version"] == "1.0.0"
    # The annotation layer must not leak into the contract metadata.
    assert "1000/0" not in json.dumps(manifest)
