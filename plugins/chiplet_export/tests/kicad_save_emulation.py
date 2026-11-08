# SPDX-License-Identifier: GPL-3.0-or-later
"""Save a board the way pcbnew's Ctrl+S does, headless, and report both sides.

usage: kicad_save_emulation.py BOARD REPORT_JSON

Runs under a Python that imports pcbnew, in a process of its own: a save
writes KiCad settings under HOME, so the caller gives it an empty one.

PCB_EDIT_FRAME::SavePcbFile (pcbnew/files.cpp) writes the project first,
creating the .kicad_pro when the board has none. Because the project now
exists, it calls BOARD::SynchronizeProperties(), which replaces the board-root
properties with the project's text variables. Only then is the board written.
pcbnew.SaveBoard alone skips the middle step, so it cannot show what a GUI
save keeps. The three calls below are that sequence.

BOARD is saved in place, as Ctrl+S does, then loaded again. The report holds
the root properties and the board observation (board_snapshot.observe_board)
before and after, and the text variables of the project the save left.
"""

import json
from pathlib import Path
import sys

import pcbnew

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from board_snapshot import observe_board  # noqa: E402


def _side(board):
    return {"root_properties": {str(k): str(v)
                                for k, v in dict(board.GetProperties()).items()},
            "observation": observe_board(board)}


def main(board_path, report_path):
    board_path = str(Path(board_path).resolve())
    board = pcbnew.LoadBoard(board_path)
    before = _side(board)
    pcbnew.SaveBoard(board_path, board)
    board.SynchronizeProperties()
    pcbnew.SaveBoard(board_path, board, True)
    after = _side(pcbnew.LoadBoard(board_path))
    project = Path(board_path).with_suffix(".kicad_pro")
    text_vars = (json.loads(project.read_text(encoding="utf-8")).get("text_variables", {})
                 if project.is_file() else None)
    report = {"pcbnew": str(pcbnew.GetBuildVersion()), "before": before,
              "after": after, "project_text_variables": text_vars}
    Path(report_path).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n",
                                 encoding="utf-8")


if __name__ == "__main__":
    main(*sys.argv[1:])
