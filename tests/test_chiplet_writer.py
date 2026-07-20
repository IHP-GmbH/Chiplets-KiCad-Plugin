# SPDX-License-Identifier: GPL-3.0-or-later
"""
Structural tests for writers/chiplet_writer.py.

Loads a fixture .kicad_pcb, runs the writer, and asserts invariants
on the YAML output:

  - _metadata.finalize_required is True (intermediate frame marker)
  - assembly section present with units=um
  - default connection_stacks emitted (cupillar_opt1/2/3, sbump_sac305)
  - interposer component with anchor=bbox_center, position (0,0,0)
  - die components carry anchor=gds_origin
  - flip-chip dies carry connection=cupillar_opt2

Byte-exact equivalence vs the C++ exporter is deferred to Gate 47.7
once both exporters can run side-by-side in the same Docker session.

Skipped if pcbnew is not importable (host Python without KiCad).
"""

import os
import sys
from pathlib import Path

import pytest

pcbnew = pytest.importorskip("pcbnew")
yaml = pytest.importorskip("yaml")

# Allow the plugin package to be imported regardless of CWD when the
# test runs from inside the KiCad bundled Python.
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_kicad_plugin.writers.chiplet_writer import write_chiplet  # noqa: E402


def _candidate_boards():
    candidates = [os.environ.get("CHIPLET_WRITER_BOARD")]
    project_root = PLUGIN_ROOT.parent
    candidates.extend([
        str(project_root / "interposer_wire_bonding_demo" / "test.kicad_pcb"),
        str(project_root / "interposer_wire_bonding_demo"
            / "interposer_wire_bonding_demo.kicad_pcb"),
        # adk-tools image / meta-repo layout: the demo ships under
        # examples/ split into a kicad/ source dir and an outputs/ dir.
        str(project_root / "examples"
            / "interposer_wire_bonding_demo"
            / "kicad"
            / "interposer_wire_bonding_demo.kicad_pcb"),
    ])
    return [c for c in candidates if c]


@pytest.fixture(scope="module")
def fixture_board_path():
    for candidate in _candidate_boards():
        if Path(candidate).exists():
            return candidate
    pytest.skip(
        "No fixture .kicad_pcb available. Set CHIPLET_WRITER_BOARD "
        "or place the wire-bond demo in the expected location."
    )


@pytest.fixture(scope="module")
def chiplet_data(tmp_path_factory, fixture_board_path):
    """Generate the .chiplet once per session and parse it."""
    board = pcbnew.LoadBoard(fixture_board_path)
    output_dir = tmp_path_factory.mktemp("chiplet_writer")
    output = output_dir / "out.chiplet"
    assert write_chiplet(board, str(output)) is True
    return yaml.safe_load(output.read_text())


def test_format_version(chiplet_data):
    assert chiplet_data["format_version"] == "1.0"


def test_intermediate_metadata_block(chiplet_data):
    md = chiplet_data["_metadata"]
    assert md["frame"] == "pcb-bbox-corner"
    assert md["finalize_required"] is True
    assert "hyp_to_gds.py" in md["finalizer"]


def test_assembly_section(chiplet_data):
    assembly = chiplet_data["assembly"]
    assert assembly["units"] == "um"
    assert assembly["name"]


def test_default_connection_stacks(chiplet_data):
    stacks = chiplet_data["connection_stacks"]
    for required in ("cupillar_opt1", "cupillar_opt2", "cupillar_opt3", "sbump_sac305"):
        assert required in stacks, "Missing default stack: %s" % required
    opt2 = stacks["cupillar_opt2"]["layers"]
    assert opt2[0]["material"] == "Cu"
    assert opt2[0]["height"] == 32.0
    assert opt2[1]["material"] == "SnAg"
    assert opt2[1]["height"] == 16.0


def test_interposer_component(chiplet_data):
    interposer = next(c for c in chiplet_data["components"] if c["id"] == "interposer")
    assert interposer["type"] == "interposer"
    assert interposer["anchor"] == "bbox_center"
    assert interposer["top_cell"] == "INTERPOSER"
    assert interposer["position"] == {"x": 0.0, "y": 0.0, "z": 0.0}
    assert "dimensions" in interposer
    assert interposer["dimensions"]["width"] > 0
    assert interposer["dimensions"]["height"] > 0


def test_die_components_anchor(chiplet_data):
    dies = [c for c in chiplet_data["components"] if c.get("type") == "die"]
    assert dies, "Fixture has no die components; cannot validate anchor invariant."
    for die in dies:
        assert die["anchor"] == "gds_origin", (
            "Die %s missing gds_origin anchor" % die["id"])


def test_flip_chip_die_has_connection(chiplet_data):
    flip_chips = [
        c for c in chiplet_data["components"]
        if c.get("type") == "die" and c.get("orientation") == "flip_chip"
    ]
    for fp in flip_chips:
        assert fp.get("connection") == "cupillar_opt2", (
            "Flip-chip die %s missing connection field" % fp["id"])


def test_io_pads_nested_under_interposer(chiplet_data):
    interposer = next(c for c in chiplet_data["components"] if c["id"] == "interposer")
    if "io_pads" in interposer:
        for pad in interposer["io_pads"]:
            assert "id" in pad
            assert "io_class" in pad
            assert "position" in pad
            assert "size" in pad
            assert pad.get("layer") == "TopMetal2"


def test_writer_survives_board_without_text_vars(tmp_path):
    """Regression: write_chiplet must not crash on a board whose PROJECT
    SWIG wrapper lacks GetTextVars. Repro board is the KiCad demo
    interf_u, which loads as a raw SwigPyObject project.
    """
    interfu = (PLUGIN_ROOT.parent / "kicad" / "demos"
               / "interf_u" / "interf_u.kicad_pcb")
    if not interfu.exists():
        pytest.skip("interf_u demo not available")
    board = pcbnew.LoadBoard(str(interfu))
    out = tmp_path / "interf_u.chiplet"
    assert write_chiplet(board, str(out)) is True
    assert out.exists() and out.stat().st_size > 0


def test_die_connection_field_roundtrip(fixture_board_path):
    """list/read/write_die_connections operate on the die footprints'
    CONNECTION fields (in-memory board; nothing is saved here)."""
    from chiplet_kicad_plugin.writers.chiplet_writer import (
        list_die_refs, read_die_connections, write_die_connections)

    board = pcbnew.LoadBoard(fixture_board_path)
    refs = list_die_refs(board)
    assert refs, "fixture board has no die footprints (GDS_FILE field)"

    target = refs[0]
    initial = read_die_connections(board)
    new_value = ("cupillar_opt2" if initial.get(target) != "cupillar_opt2"
                 else "cupillar_opt3")

    changed = write_die_connections(board, {target: new_value})
    assert changed == [target]
    assert read_die_connections(board)[target] == new_value

    # Same value again -> no-op.
    assert write_die_connections(board, {target: new_value}) == []

    # Clearing the override removes the die from the map.
    assert write_die_connections(board, {target: ""}) == [target]
    assert target not in read_die_connections(board)


def test_invalid_orientation_field_is_a_hard_error(fixture_board_path, tmp_path):
    """A die footprint carrying a non-canonical ORIENTATION (a typo, or the
    non-canonical 'face_down') must fail loudly at export, not be silently
    emitted un-mirrored and without its connection stack."""
    from chiplet_kicad_plugin.writers.chiplet_writer import _field_text

    board = pcbnew.LoadBoard(fixture_board_path)
    die = next((fp for fp in board.Footprints()
                if _field_text(fp, "GDS_FILE")), None)
    assert die is not None, "fixture board has no die footprint"
    for bad in ("face_down", "flip-chip"):
        die.SetField("ORIENTATION", bad)
        out = tmp_path / ("bad_%s.chiplet" % bad.replace("-", "_"))
        with pytest.raises(ValueError, match="ORIENTATION"):
            write_chiplet(board, str(out))
    # A canonical value still exports.
    die.SetField("ORIENTATION", "flip_chip")
    write_chiplet(board, str(tmp_path / "ok.chiplet"))


def test_write_die_connections_ignores_non_die_footprints(fixture_board_path):
    """Refs without a GDS_FILE field are never touched."""
    from chiplet_kicad_plugin.writers.chiplet_writer import (
        list_die_refs, write_die_connections)

    board = pcbnew.LoadBoard(fixture_board_path)
    die_refs = set(list_die_refs(board))
    other = [fp.GetReference() for fp in board.Footprints()
             if fp.GetReference() not in die_refs]
    if not other:
        pytest.skip("fixture board has only die footprints")
    changed = write_die_connections(board, {other[0]: "cupillar_opt1"})
    assert changed == []


def test_parse_thickness_um():
    """Positive finite numbers pass; empty/garbage/non-positive are None."""
    from chiplet_kicad_plugin.writers.chiplet_writer import parse_thickness_um

    assert parse_thickness_um("750") == 750.0
    assert parse_thickness_um(" 253.5 ") == 253.5
    for bad in ("", "  ", None, "abc", "0", "-1", "nan", "inf", "1,5"):
        assert parse_thickness_um(bad) is None, bad


def test_die_thickness_field_roundtrip(fixture_board_path):
    """read/write_die_thicknesses operate on the die footprints'
    DIE_THICKNESS_UM fields (in-memory board; nothing is saved here)."""
    from chiplet_kicad_plugin.writers.chiplet_writer import (
        list_die_refs, read_die_thicknesses, write_die_thicknesses)

    board = pcbnew.LoadBoard(fixture_board_path)
    refs = list_die_refs(board)
    assert refs, "fixture board has no die footprints (GDS_FILE field)"

    target = refs[0]
    initial = read_die_thicknesses(board)
    new_value = "750" if initial.get(target) != 750.0 else "250"

    changed = write_die_thicknesses(board, {target: new_value})
    assert changed == [target]
    assert read_die_thicknesses(board)[target] == float(new_value)

    # Same value again -> no-op.
    assert write_die_thicknesses(board, {target: new_value}) == []

    # Clearing removes the die from the map (format default 0.0 applies
    # downstream).
    assert write_die_thicknesses(board, {target: ""}) == [target]
    assert target not in read_die_thicknesses(board)


def test_read_die_thicknesses_skips_malformed(fixture_board_path, capsys):
    """A non-numeric field value warns and is skipped, never raises."""
    from chiplet_kicad_plugin.writers.chiplet_writer import (
        list_die_refs, read_die_thicknesses, write_die_thicknesses)

    board = pcbnew.LoadBoard(fixture_board_path)
    target = list_die_refs(board)[0]
    write_die_thicknesses(board, {target: "not-a-number"})
    result = read_die_thicknesses(board)
    err = capsys.readouterr().err
    assert target not in result
    assert "not-a-number" in err
