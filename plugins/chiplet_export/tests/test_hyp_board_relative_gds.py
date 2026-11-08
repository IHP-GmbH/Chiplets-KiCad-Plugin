# SPDX-License-Identifier: GPL-3.0-or-later
"""Board-relative die GDS_FILE resolution in the HYP parser.

A die footprint may carry a board-relative GDS_FILE (e.g. ../chiplets/die.gds)
so the example ships its own die layout next to the board. The .hyp records the
source board via the {BOARD "..."} header; the parser resolves a relative die
path against that board's directory instead of the process CWD (the .hyp itself
lives in a temp dir, so a bare relative path would otherwise resolve against an
arbitrary CWD). Absolute and ${VAR} paths are left untouched.

Pure-parser tests: no pcbnew, no GDS is read.
"""
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402


_PERIMETER = (
    "  (PERIMETER_SEGMENT X1=0.000000 Y1=0.000000 X2=0.002000 Y2=0.000000)\n"
    "  (PERIMETER_SEGMENT X1=0.002000 Y1=0.000000 X2=0.002000 Y2=-0.001000)\n"
    "  (PERIMETER_SEGMENT X1=0.002000 Y1=-0.001000 X2=0.000000 Y2=-0.001000)\n"
    "  (PERIMETER_SEGMENT X1=0.000000 Y1=-0.001000 X2=0.000000 Y2=0.000000)\n"
)


def _hyp(board_path, gds_file):
    return (
        "{VERSION=2.14}\n{UNITS=METRIC LENGTH}\n\n"
        '{BOARD "%s"\n%s}\n\n'
        "{DEVICES\n"
        '  (? REF="U1" L="TopMetal2" X=0.000200 Y=-0.000500 R=0.00 '
        'GDS_FILE="%s")\n'
        "}\n" % (board_path, _PERIMETER, gds_file)
    )


def _parse_gds_file(tmp_path, board_path, gds_file):
    hyp = tmp_path / "work" / "demo.hyp"
    hyp.parent.mkdir(parents=True, exist_ok=True)
    hyp.write_text(_hyp(board_path, gds_file))
    p = h.HYPParser(str(hyp))
    p.parse()
    assert len(p.devices) == 1
    return p.devices[0].gds_file


def test_board_relative_resolves_against_board_dir(tmp_path):
    board = tmp_path / "kicad" / "demo.kicad_pcb"
    board.parent.mkdir(parents=True)
    got = _parse_gds_file(tmp_path, str(board), "../chiplets/die.gds")
    assert got == os.path.normpath(str(tmp_path / "chiplets" / "die.gds"))


def test_absolute_gds_file_left_untouched(tmp_path):
    board = tmp_path / "kicad" / "demo.kicad_pcb"
    board.parent.mkdir(parents=True)
    abs_gds = str(tmp_path / "elsewhere" / "die.gds")
    assert _parse_gds_file(tmp_path, str(board), abs_gds) == abs_gds


def test_var_gds_file_left_untouched(tmp_path):
    board = tmp_path / "kicad" / "demo.kicad_pcb"
    board.parent.mkdir(parents=True)
    val = "${GDS_TO_KICAD_ROOT}/gds_files/x/die.gds"
    assert _parse_gds_file(tmp_path, str(board), val) == val


def test_no_board_dir_preserves_cwd_relative(tmp_path):
    # A synthetic {BOARD "name"} with no directory component must not rebase:
    # the path stays relative and resolves against the CWD, as before.
    assert _parse_gds_file(tmp_path, "synthetic", "u1.gds") == "u1.gds"
