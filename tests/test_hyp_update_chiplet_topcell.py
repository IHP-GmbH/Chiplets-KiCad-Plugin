# SPDX-License-Identifier: GPL-3.0-or-later
"""update_chiplet_file reads a die's top_cell from the device GDS path.

The die top_cell is read from the die GDS. A board-relative die layout (e.g.
../chiplets/die.gds) is stored in the .chiplet relative to the .chiplet's own
directory, so at export time -- when the .chiplet may sit in a throwaway output
dir far from the die GDS -- update_chiplet_file must read top_cell from the
device's already board-resolved GDS path, not the .chiplet-relative layout
(which would resolve against the wrong directory and drop top_cell).
"""
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402


_CHIPLET = """\
format_version: "1.0"
name: t
components:
  - id: interposer
    type: interposer
    dimensions: {width: 1000.0, height: 1000.0, thickness: 100.0}
    position: {x: 0.0, y: 0.0, z: 0.0}
  - id: U1
    type: die
    orientation: flip_chip
    layout: ../chiplets/die.gds
    dimensions: {width: 100.0, height: 100.0, thickness: 0.0}
    position: {x: 10.0, y: 10.0, z: 0.0}
"""


def _write_die_gds(db, path, top_name):
    layout = db.Layout()
    top = layout.create_cell(top_name)
    top.shapes(layout.layer(134, 0)).insert(db.Box(0, 0, 1000, 1000))
    layout.write(str(path))


def test_top_cell_read_from_device_gds_path(tmp_path):
    db = pytest.importorskip("klayout.db")
    yaml = pytest.importorskip("yaml")

    # Die GDS lives under chiplets/; the .chiplet sits in a throwaway output
    # dir, so its ../chiplets/die.gds does NOT resolve from there. The device's
    # gds_file (board-resolved absolute) is what makes top_cell resolvable.
    die_gds = tmp_path / "chiplets" / "die.gds"
    die_gds.parent.mkdir(parents=True)
    _write_die_gds(db, die_gds, "MyDie")

    out = tmp_path / "throwaway_out"
    out.mkdir()
    chiplet = out / "t.chiplet"
    chiplet.write_text(_CHIPLET)

    dev = h.Device(ref="U1", layer="TopMetal2", x=0.0, y=0.0, rotation=0.0,
                   gds_file=str(die_gds))

    ok = h.update_chiplet_file(
        str(chiplet), "/nonexistent_interposer.gds",
        bbox=(0.0, 0.0, 1000.0, 1000.0), devices=[dev])
    assert ok is True

    data = yaml.safe_load(chiplet.read_text())
    die = next(c for c in data["components"] if c.get("id") == "U1")
    assert die.get("top_cell") == "MyDie"
