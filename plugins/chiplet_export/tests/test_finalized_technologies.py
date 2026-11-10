# SPDX-License-Identifier: GPL-3.0-or-later
"""Finalize technology metadata without changing identities or reader guards.

These tests call the real finalizer, guarded loader, and atomic writer. They
need the worker's Python dependencies but perform no GDS operations; the
mount plane comes from the fixture PDK root.
"""

import pytest
import yaml

import hyp_to_gds as h


@pytest.fixture(autouse=True)
def _mount_plane(attach_frame_pdk):
    """Each finalize here reads the plane from the fixture PDK root."""


_DOCUMENT = """\
format_version: "1.0"
_metadata:
  finalize_required: true
assembly:
  name: technology-metadata
components:
  - id: interposer
    type: interposer
    dimensions: {width: 1000.0, height: 1000.0, thickness: 100.0}
    position: {x: 0.0, y: 0.0, z: 0.0}
"""


@pytest.fixture
def finalize(tmp_path, monkeypatch):
    # Geometry is outside this metadata test. Supplying both bboxes avoids
    # reading a GDS; the remaining top-cell probe is the sole stub.
    monkeypatch.setattr(h, "_read_gds_top_cell", lambda _path: "INTERPOSER")
    bounds = (0.0, 0.0, 1000.0, 1000.0)

    def run(document):
        path = tmp_path / "assembly.chiplet"
        path.write_text(document, encoding="utf-8")
        before = path.read_bytes()
        success = h.update_chiplet_file(
            str(path), str(tmp_path / "interposer.gds"),
            bbox=bounds, outline_bbox=bounds,
        )
        return success, path, before

    return run


@pytest.mark.parametrize("block, expected", [
    ("technologies:\n", {}),
    ("technologies: null\n", {}),
    ("technologies: {}\n", {}),
    ("technologies:\n  custom_process:\n    description: Authored technology\n"
     "    dbu: 0.002\n    layer_properties: '${PDK_ROOT}/custom.lyp'\n",
     {"custom_process": {"description": "Authored technology", "dbu": 0.002,
                         "layer_properties": "${PDK_ROOT}/custom.lyp"}}),
])
def test_finalized_technology_map_preserves_declared_content(finalize, block, expected):
    success, path, _before = finalize(_DOCUMENT + block)
    assert success is True
    result = h._vendored_cfio().load(str(path))
    assert result["technologies"] == expected
    assert "_metadata" not in result
    assert "technology" not in result["components"][0]


def test_missing_technologies_remains_missing(finalize):
    success, path, _before = finalize(_DOCUMENT)
    assert success is True
    assert "technologies" not in yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("value", ["[]", "[sg13g2]", "sg13g2", "false", "7"])
def test_non_mapping_technologies_refuses_without_replacing_input(finalize, capsys, value):
    success, path, before = finalize(_DOCUMENT + f"technologies: {value}\n")
    assert success is False
    assert path.read_bytes() == before
    assert "'technologies' must be a mapping" in capsys.readouterr().err


@pytest.mark.parametrize("version", ["2.0", "invalid"])
def test_null_technologies_does_not_bypass_version_guard(finalize, capsys, version):
    document = _DOCUMENT.replace('format_version: "1.0"',
                                 f'format_version: "{version}"')
    success, path, before = finalize(document + "technologies: null\n")
    assert success is False
    assert path.read_bytes() == before
    assert "format_version" in capsys.readouterr().err
