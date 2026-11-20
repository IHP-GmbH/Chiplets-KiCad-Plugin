# SPDX-License-Identifier: GPL-3.0-or-later
"""The routed_t03 fixture laid out as a run, for the tests that read the
files the routed geometry binds.

The document names its die layouts by the board's GDS_FILE text, four folders
up, and its pin lists under ``<RUN>``; ``stage`` copies ``dies/`` and the pin
lists to a temporary root and writes the document four folders below it, with
``<RUN>`` resolved.
"""

import json
import shutil
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "routed_t03"
INPUTS = FIXTURE / "inputs"
DOCUMENT = INPUTS / "routed-geometry.json"
CHIPLET = INPUTS / "design.chiplet"
IO_PADS = INPUTS / "routed-io-pads.json"
BOARD_HYP = INPUTS / "board-path.hyp"
DIES = {"U1": "PAD_FRAME_L", "U2": "PAD_FRAME_R"}


def stage(tmp_path, edit=None):
    """The document's path in a staged run under ``tmp_path``.
    ``edit(document)`` may change the document before it is written."""
    root = Path(tmp_path) / "run"
    for die in DIES.values():
        (root / "dies" / die).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURE / "dies" / die / "layout.gds",
                        root / "dies" / die / "layout.gds")
        shutil.copyfile(INPUTS / "pins" / ("%s.pins.json" % die),
                        root / "dies" / die / "pins.json")
    document = json.loads(DOCUMENT.read_text().replace("<RUN>", str(root)))
    if edit is not None:
        edit(document)
    path = root / "a" / "b" / "c" / "pnr" / "routed-geometry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n")
    return path
