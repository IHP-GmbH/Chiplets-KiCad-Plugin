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
        # adk-tools image / meta-repo layout (examples/ ships the demo)
        str(project_root / "examples"
            / "interposer_wire_bonding_demo"
            / "interposer_wire_bonding_demo.kicad_pcb"),
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


# ---------------------------------------------------------------------------
# Latent-divergence guards. The shipping single-PDK / 2-layer fixtures cannot
# exercise the technologies-block ordering or the inner-copper PADSTACK
# ordering, so each test synthesizes the triggering geometry on top of a real
# fixture board. Both diverged before the parity fix (sorted std::map / .Seq).
# ---------------------------------------------------------------------------

def _add_die_footprint(board, ref, lyp, gds, x_mm, y_mm):
    """Add a bare die footprint carrying LYP_FILE + GDS_FILE fields."""
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetField("LYP_FILE", lyp)
    fp.SetField("GDS_FILE", gds)
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm)))
    board.Add(fp)
    return fp


def test_chiplet_byte_exact_multi_tech(tmp_path, chiplet_board_path):
    """Multi-PDK technologies block stays byte-identical to the C++ exporter.

    The C++ techMap is a std::map (sorted iteration); a Python dict emits in
    insertion order. Two extra dies are added with LYP ids whose insertion
    order ('zzz' then 'aaa') is NOT sorted, so an insertion-order regression
    diverges here even though the single-tech demo cannot trigger it.
    """
    cpp_path = tmp_path / "cpp_multi.chiplet"
    py_path = tmp_path / "py_multi.chiplet"

    def prep():
        board = pcbnew.LoadBoard(chiplet_board_path)
        _add_die_footprint(board, "ZZZ1", "zzz_tech.lyp", "ZZZ1.gds", 1.0, 1.0)
        _add_die_footprint(board, "AAA1", "aaa_tech.lyp", "AAA1.gds", 2.0, 2.0)
        return board

    assert pcbnew.ExportBoardToChipletFile(prep(), str(cpp_path)) is True
    assert write_chiplet(prep(), str(py_path)) is True
    _assert_byte_exact("chiplet multi-tech", cpp_path, py_path)


def test_hyperlynx_byte_exact_inner_copper(tmp_path, hyperlynx_board_path):
    """Inner-copper PADSTACK ordering stays byte-identical to the C++ exporter.

    A through via on a 4-layer board makes the C++ WritePadStack emit its
    copper layers in raw-bit (.Seq) order -- top, bottom, In1, In2 -- where a
    .CuStack()-ordered Python writer would interleave the inner layers. The
    2-layer interf_u fixture cannot trigger it.
    """
    cpp_path = tmp_path / "cpp_inner.hyp"
    py_path = tmp_path / "py_inner.hyp"

    def prep():
        board = pcbnew.LoadBoard(hyperlynx_board_path)
        board.SetCopperLayerCount(4)
        via = pcbnew.PCB_VIA(board)
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetPosition(board.GetBoardEdgesBoundingBox().GetCenter())
        via.SetWidth(pcbnew.FromMM(0.6))
        via.SetDrill(pcbnew.FromMM(0.3))
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        board.Add(via)
        return board

    assert pcbnew.ExportBoardToHyperlynxFile(prep(), str(cpp_path)) is True
    assert write_hyperlynx(prep(), str(py_path)) is True
    _assert_byte_exact("hyperlynx inner-copper", cpp_path, py_path)
