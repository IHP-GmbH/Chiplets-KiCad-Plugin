# SPDX-License-Identifier: GPL-2.0-or-later
"""
Byte-exact regression: Python writers vs C++ exporters.

Both writers must produce files that are byte-identical to the C++
reference exporters (export_chiplet.cpp / export_hyperlynx.cpp), so
the Python port can drop into the same downstream pipelines (chiplet-
studio, hyp_to_gds.py) without introducing silent format drift.

The C++ reference is reached via the headless helpers added in the
kicad fork:

  pcbnew.ExportBoardToChipletFile(board, path)
  pcbnew.ExportBoardToHyperlynxFile(board, path)

Both writers in this module require ``pcbnew`` and run only inside
the kicad-builder Docker image. ``pytest.importorskip("pcbnew")``
keeps host-side runs clean.
"""

import difflib
import os
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

pcbnew = pytest.importorskip("pcbnew")

if not hasattr(pcbnew, "ExportBoardToChipletFile") or \
        not hasattr(pcbnew, "ExportBoardToHyperlynxFile"):
    pytest.skip(
        "C++ headless exporter helpers not present in this pcbnew "
        "build. Rebuild the kicad fork with the export_chiplet.h / "
        "export_hyperlynx.h SWIG patch.",
        allow_module_level=True,
    )

from chiplet_kicad_plugin.writers.chiplet_writer import (  # noqa: E402
    write_chiplet,
)
from chiplet_kicad_plugin.writers.hyperlynx_writer import (  # noqa: E402
    write_hyperlynx,
)


# ---------------------------------------------------------------------------
# Fixture discovery (mirrors the structural-invariant tests)
# ---------------------------------------------------------------------------

def _candidate_chiplet_boards():
    project_root = PLUGIN_ROOT.parent
    candidates = [
        os.environ.get("CHIPLET_WRITER_BOARD"),
        str(project_root / "kicad_designs"
            / "interposer_wire_bonding_demo"
            / "interposer_wire_bonding_demo.kicad_pcb"),
        str(project_root / "kicad_designs"
            / "kicad_interposer_hyperlynx_to_gds"
            / "chiplet_demo.kicad_pcb"),
    ]
    return [c for c in candidates if c]


def _candidate_hyperlynx_boards():
    project_root = PLUGIN_ROOT.parent
    candidates = [
        os.environ.get("HYPERLYNX_WRITER_BOARD"),
        # KiCad upstream demo: closed Edge.Cuts and rich stackup so
        # both the dielectric and copper branches of the writer are
        # exercised end-to-end.
        str(project_root / "kicad" / "demos" / "interf_u"
            / "interf_u.kicad_pcb"),
    ]
    return [c for c in candidates if c]


def _has_closed_outline(board_path):
    try:
        board = pcbnew.LoadBoard(board_path)
    except Exception:
        return False
    return bool(board.GetBoardPolygonOutlines(pcbnew.SHAPE_POLY_SET()))


@pytest.fixture(scope="module")
def chiplet_board_path():
    for candidate in _candidate_chiplet_boards():
        if candidate and Path(candidate).exists():
            return candidate
    pytest.skip("No chiplet fixture .kicad_pcb available.")


@pytest.fixture(scope="module")
def hyperlynx_board_path():
    for candidate in _candidate_hyperlynx_boards():
        if candidate and Path(candidate).exists() \
                and _has_closed_outline(candidate):
            return candidate
    pytest.skip(
        "No hyperlynx fixture .kicad_pcb with a closed Edge.Cuts "
        "outline available."
    )


# ---------------------------------------------------------------------------
# Diff helper
# ---------------------------------------------------------------------------

def _assert_byte_exact(label, baseline_path, candidate_path):
    """Assert two files are byte-identical; on failure print a context diff."""
    with open(baseline_path, "rb") as f:
        baseline_bytes = f.read()
    with open(candidate_path, "rb") as f:
        candidate_bytes = f.read()
    if baseline_bytes == candidate_bytes:
        return
    # Decode best-effort for a readable diff. Replace undecodable bytes
    # rather than aborting so even binary differences produce output.
    baseline_text = baseline_bytes.decode("utf-8", errors="replace")
    candidate_text = candidate_bytes.decode("utf-8", errors="replace")
    diff = "\n".join(difflib.unified_diff(
        baseline_text.splitlines(),
        candidate_text.splitlines(),
        fromfile="cpp_baseline (%d bytes)" % len(baseline_bytes),
        tofile="python_port (%d bytes)" % len(candidate_bytes),
        n=2,
        lineterm="",
    ))
    # Truncate the diff so a wholesale mismatch does not flood the
    # test output; show the first ~80 differing lines.
    diff_lines = diff.splitlines()
    if len(diff_lines) > 200:
        diff = "\n".join(diff_lines[:200]) + "\n... [truncated]"
    pytest.fail(
        "%s: Python writer output is not byte-identical to the C++ "
        "exporter baseline.\n\n%s" % (label, diff)
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_chiplet_byte_exact(tmp_path, chiplet_board_path):
    """write_chiplet must byte-match ExportBoardToChipletFile."""
    cpp_path = tmp_path / "cpp_baseline.chiplet"
    py_path = tmp_path / "python_port.chiplet"

    board = pcbnew.LoadBoard(chiplet_board_path)
    assert pcbnew.ExportBoardToChipletFile(board, str(cpp_path)) is True, \
        "C++ chiplet exporter returned False"

    board_py = pcbnew.LoadBoard(chiplet_board_path)
    assert write_chiplet(board_py, str(py_path)) is True, \
        "Python chiplet writer returned False"

    _assert_byte_exact("chiplet", cpp_path, py_path)


def test_hyperlynx_byte_exact(tmp_path, hyperlynx_board_path):
    """write_hyperlynx must byte-match ExportBoardToHyperlynxFile."""
    cpp_path = tmp_path / "cpp_baseline.hyp"
    py_path = tmp_path / "python_port.hyp"

    board = pcbnew.LoadBoard(hyperlynx_board_path)
    assert pcbnew.ExportBoardToHyperlynxFile(board, str(cpp_path)) is True, \
        "C++ hyperlynx exporter returned False"

    board_py = pcbnew.LoadBoard(hyperlynx_board_path)
    assert write_hyperlynx(board_py, str(py_path)) is True, \
        "Python hyperlynx writer returned False"

    _assert_byte_exact("hyperlynx", cpp_path, py_path)
