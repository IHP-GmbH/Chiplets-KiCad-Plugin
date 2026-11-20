# SPDX-License-Identifier: GPL-3.0-or-later
"""The stand-in routed-geometry document of the routed_t03 fixture.

interposer-pnr does not write the routed-geometry document yet. Until it does,
this script builds one from what the same run recorded about the board it drew:
the export report's round_trip block (each die's position, rotation,
orientation, layout and pad-domain archive), the board's outline and its
thickness. It writes only the fields the routed intermediates read, and says
in "producer" that it is a stand-in. Once the writer exists its output replaces
this file and the tests run again on it.

    python make_standin.py [--check]

No pcbnew; standard library only. --check rebuilds in memory and exits 1 if
the committed inputs/routed-geometry.json differs.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INPUTS = HERE / "inputs"
BOARD = INPUTS / "cf_typical_03_from_schematic.draft.kicad_pcb"
EXPORT_REPORT = INPUTS / "export.json"
CHIPLET = INPUTS / "design.chiplet"
PINS = INPUTS / "pins"
OUT = INPUTS / "routed-geometry.json"


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _um(mm):
    """Millimeters to micrometers, to the nanometer a board stores."""
    return round(mm * 1_000_000) / 1000.0


def _board_thickness_um(text):
    found = re.search(r"\(general\s*\(thickness ([0-9.]+)\)", text)
    if found is None:
        raise SystemExit("the board states no (general (thickness))")
    return _um(float(found.group(1)))


def build():
    report = json.loads(EXPORT_REPORT.read_text(encoding="utf-8"))
    round_trip = report["round_trip"]
    x0, y0, x1, y1 = round_trip["outline_mm"]
    # KiCad Y grows down; the document's frame is Y up from the lower-left.
    left, right = _um(min(x0, x1)), _um(max(x0, x1))
    bottom, top = _um(-max(y0, y1)), _um(-min(y0, y1))
    pins_by_sha = {_sha256(p): p for p in sorted(PINS.iterdir()) if p.is_file()}

    devices, pins = [], {}
    for ref, record in round_trip["footprints"].items():
        x_mm, y_mm = record["at_mm"]
        archive = record["properties"]["pad_domains"]
        devices.append({
            "ref": ref,
            "layout": {"path": record["gds_file"], "sha256": record["gds_sha256"]},
            "origin": [_um(x_mm), _um(-y_mm)],
            "rotation_deg": record["rotation_deg"],
            "mirrored": record["properties"]["orientation"] == "flip_chip",
            "pad_domains": archive,
        })
        source = pins_by_sha[archive["die"]["pins_sha256"]]
        pins[ref] = {"path": source.relative_to(INPUTS).as_posix(),
                     "sha256": _sha256(source)}

    document = {
        "schema": "routed-geometry", "version": "1.0",
        "frame": "interposer_local_y_up_um",
        "outline": [[left, bottom], [right, bottom], [right, top], [left, top]],
        "interposer": {"body_thickness_um":
                       _board_thickness_um(BOARD.read_text(encoding="utf-8"))},
        "devices": devices,
        "inputs": {
            "chiplet": {"path": CHIPLET.name, "sha256": _sha256(CHIPLET)},
            "pins": pins,
        },
        "producer": {"tool": "make_standin.py", "version": "stand-in",
                     "from": {"export_report": _sha256(EXPORT_REPORT),
                              "board": _sha256(BOARD)}},
    }
    return (json.dumps(document, indent=2) + "\n").encode("utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    data = build()
    if args.check:
        if OUT.read_bytes() != data:
            print("%s differs from a rebuild" % OUT.name, file=sys.stderr)
            return 1
        print("%s holds" % OUT.name)
        return 0
    OUT.write_bytes(data)
    print("wrote %s" % OUT.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
