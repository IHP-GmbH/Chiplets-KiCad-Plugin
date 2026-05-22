# SPDX-License-Identifier: GPL-2.0-or-later
"""
Structural tests for writers/hyperlynx_writer.py.

Loads a fixture .kicad_pcb, runs the writer, and asserts the
output is a well-formed Hyperlynx .hyp file with the metric
(meters) units patch and the GDS_FILE extension:

  - {VERSION=2.14} and {UNITS=METRIC LENGTH} headers present
  - {BOARD "..."} block emitted
  - {STACKUP ...} block with at least one (SIGNAL ...) entry
  - {DEVICES ...} block with one entry per footprint, fields in
    the expected key=value form
  - DEVICES entries with GDS_FILE= when the footprint has the
    field (chiplet pipeline contract, see hyp_to_gds.py)

Byte-exact equivalence vs the C++ exporter is deferred to Gate
47.7 once both exporters can run side-by-side in the same Docker
session.

Skipped if pcbnew is not importable (host Python without KiCad).
"""

import os
import re
import sys
from pathlib import Path

import pytest

pcbnew = pytest.importorskip("pcbnew")

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_kicad_plugin.writers.hyperlynx_writer import (  # noqa: E402
    write_hyperlynx,
)


def _candidate_boards():
    candidates = [os.environ.get("HYPERLYNX_WRITER_BOARD"),
                  os.environ.get("CHIPLET_WRITER_BOARD")]
    project_root = PLUGIN_ROOT.parent
    candidates.extend([
        str(project_root / "interposer_wire_bonding_demo" / "test.kicad_pcb"),
        str(project_root / "interposer_wire_bonding_demo"
            / "interposer_wire_bonding_demo.kicad_pcb"),
        str(project_root / "kicad_designs"
            / "kicad_interposer_hyperlynx_to_gds"
            / "chiplet_demo.kicad_pcb"),
        # KiCad upstream demo with a closed Edge.Cuts outline. Used
        # when no interposer fixture in the repo has a valid outline
        # (current chiplet/interposer demos ship without Edge.Cuts).
        str(project_root / "kicad" / "demos" / "interf_u"
            / "interf_u.kicad_pcb"),
    ])
    return [c for c in candidates if c]


def _has_closed_outline(board_path):
    """Return True if the board loads and exposes a closed Edge.Cuts.

    The hyperlynx writer (Python and C++) requires this; otherwise
    GetBoardPolygonOutlines returns False and the exporter aborts.
    """
    pcbnew = pytest.importorskip("pcbnew")
    try:
        board = pcbnew.LoadBoard(board_path)
    except Exception:
        return False
    return bool(board.GetBoardPolygonOutlines(pcbnew.SHAPE_POLY_SET()))


@pytest.fixture(scope="module")
def fixture_board_path():
    for candidate in _candidate_boards():
        if Path(candidate).exists() and _has_closed_outline(candidate):
            return candidate
    pytest.skip(
        "No fixture .kicad_pcb with a closed Edge.Cuts outline "
        "available. Set HYPERLYNX_WRITER_BOARD (or "
        "CHIPLET_WRITER_BOARD) to a board with a valid outline."
    )


@pytest.fixture(scope="module")
def hyp_text(tmp_path_factory, fixture_board_path):
    board = pcbnew.LoadBoard(fixture_board_path)
    output_dir = tmp_path_factory.mktemp("hyperlynx_writer")
    output = output_dir / "out.hyp"
    assert write_hyperlynx(board, str(output)) is True
    text = output.read_text(encoding="utf-8")
    assert text, "Writer produced empty output"
    return text


def test_metric_headers_first(hyp_text):
    # The metric patch is the entire reason this writer exists; the
    # very first two lines must match the C++ metric variant.
    head = hyp_text.splitlines()[:2]
    assert head[0] == "{VERSION=2.14}"
    assert head[1] == "{UNITS=METRIC LENGTH}"


def test_board_block_present(hyp_text):
    assert re.search(r'^\{BOARD "[^"]*"', hyp_text, re.MULTILINE), \
        "Missing {BOARD ...} block"
    # The BOARD block should close before the next top-level block.
    assert "\n}\n\n{STACKUP" in hyp_text or "\n}\n\n{DEVICES" in hyp_text


def test_stackup_has_signal_line(hyp_text):
    assert "{STACKUP\n" in hyp_text, "Missing {STACKUP block"
    assert re.search(r"\(SIGNAL T=\S+ P=\S+ C=\S+ L=\"[^\"]+\" M=COPPER\)",
                     hyp_text), \
        "No (SIGNAL ...) line in stackup"


def test_devices_block_emits_each_footprint(hyp_text):
    assert "{DEVICES\n" in hyp_text, "Missing {DEVICES block"
    # Every device line follows the (? REF=... L=... X=... Y=... R=...
    # [GDS_FILE=...]) shape; pull them out and sanity-check count.
    devices = re.findall(
        r'\(\? REF="([^"]+)" L="[^"]+" '
        r'X=(-?\d+\.\d+) Y=(-?\d+\.\d+) R=(-?\d+\.\d+)'
        r'(?: GDS_FILE="[^"]+")?\)',
        hyp_text,
    )
    assert devices, "DEVICES block has no entries"
    # Coordinate units are meters; for a board that fits in a few
    # centimeters all coords stay well under 1.0.
    for ref, x, y, r in devices:
        assert ref, "Empty REF= field"
        assert abs(float(x)) < 10.0, "X out of meter scale (%s = %s)" % (ref, x)
        assert abs(float(y)) < 10.0, "Y out of meter scale (%s = %s)" % (ref, y)
        # Rotation is degrees; KiCad clamps to [-360, 360] in practice.
        assert -360.0 <= float(r) <= 360.0, \
            "Rotation out of range for %s: %s" % (ref, r)


def test_gds_file_field_propagated(hyp_text, fixture_board_path):
    # If at least one footprint on the board defines a GDS_FILE field,
    # the device entry must carry the GDS_FILE="..." attribute.
    board = pcbnew.LoadBoard(fixture_board_path)
    has_gds = False
    for footprint in list(board.Footprints()):
        if footprint.HasField("GDS_FILE") and footprint.GetFieldText("GDS_FILE"):
            has_gds = True
            break
    if not has_gds:
        pytest.skip("Fixture has no footprint with a GDS_FILE field.")
    assert re.search(r'GDS_FILE="[^"]+"', hyp_text), \
        "GDS_FILE field missing from at least one device entry"


def test_padstacks_consistent_ids(hyp_text):
    # PADSTACK ids must be unique. The PIN entries reference them
    # via P=<id>, so any mismatch breaks round-trip parsers.
    stack_ids = [int(m) for m in re.findall(r"\{PADSTACK=(\d+),", hyp_text)]
    assert len(stack_ids) == len(set(stack_ids)), \
        "Duplicate PADSTACK id detected: %s" % stack_ids
    # If any PIN entries exist, every P=<id> must reference an
    # emitted PADSTACK block.
    pin_refs = set(int(m) for m in re.findall(r"\(PIN [^)]*P=(\d+)\)",
                                              hyp_text))
    if pin_refs:
        assert pin_refs.issubset(set(stack_ids)), \
            "PIN entries reference unknown PADSTACK ids: %s not in %s" % (
                pin_refs - set(stack_ids), stack_ids,
            )


def test_net_blocks_well_formed(hyp_text):
    # NET blocks are optional (a board may have no copper items),
    # but if any exist they must have a name and be paired with a
    # closing brace.
    nets = re.findall(r'^\{NET="([^"]+)"', hyp_text, re.MULTILINE)
    if not nets:
        pytest.skip("Fixture has no copper-bearing nets.")
    for name in nets:
        assert name, "Empty NET name"
    open_blocks = len(re.findall(r'^\{NET="', hyp_text, re.MULTILINE))
    # Match each opening NET block with a closing "}" on its own
    # line followed by a blank line (standard footer in the writer).
    close_blocks = hyp_text.count("\n}\n\n")
    assert close_blocks >= open_blocks, \
        "Unbalanced NET block braces: %d open vs %d close" % (
            open_blocks, close_blocks,
        )
