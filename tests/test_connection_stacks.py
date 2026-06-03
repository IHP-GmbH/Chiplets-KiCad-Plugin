# SPDX-License-Identifier: GPL-2.0-or-later
"""
Host-side byte-exact test for writers/connection_stacks.py.

The manifest-driven connection_stacks block must reproduce, byte for byte, the
literal the writer used to hardcode (and that export_chiplet.cpp still emits).
This guards the byte-exact writer-parity gate (47.7b) without needing pcbnew.
"""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_kicad_plugin.writers.connection_stacks import (  # noqa: E402
    emit_connection_stacks_block,
    emit_interconnect_block,
)


# Transcribed verbatim from the pre-split literal (chiplet_writer.py:274-293
# / export_chiplet.cpp). The f.write("\n") separator the caller adds afterwards
# is NOT part of this block.
EXPECTED_BLOCK = (
    "connection_stacks:\n"
    "  cupillar_opt1:\n"
    '    description: "PacTech Cu Pillar, Table 6.1 Option 1 (35um opening)"\n'
    "    layers:\n"
    "      - {name: CuPillar, material: Cu, height: 28.0, diameter: 44.0}\n"
    "      - {name: SnAgCap, material: SnAg, height: 16.0, diameter: 44.0}\n"
    "  cupillar_opt2:\n"
    '    description: "PacTech Cu Pillar, Table 6.1 Option 2 (40um opening)"\n'
    "    layers:\n"
    "      - {name: CuPillar, material: Cu, height: 32.0, diameter: 49.0}\n"
    "      - {name: SnAgCap, material: SnAg, height: 16.0, diameter: 49.0}\n"
    "  cupillar_opt3:\n"
    '    description: "PacTech Cu Pillar, Table 6.1 Option 3 (45um opening)"\n'
    "    layers:\n"
    "      - {name: CuPillar, material: Cu, height: 42.0, diameter: 54.0}\n"
    "      - {name: SnAgCap, material: SnAg, height: 19.0, diameter: 54.0}\n"
    "  sbump_sac305:\n"
    '    description: "PacTech SAC305 solder bump (80um ball)"\n'
    "    layers:\n"
    "      - {name: SolderBall, material: SAC305, height: 80.0, diameter: 80.0}\n"
)


def test_connection_stacks_block_byte_exact():
    assert emit_connection_stacks_block() == EXPECTED_BLOCK


def test_vendorx_excluded_from_default_library():
    """The non-IHP demo method must not leak into the default emitted block."""
    block = emit_connection_stacks_block()
    assert "vendorx" not in block.lower()
    assert "VendorXBump" not in block


def test_interconnect_block_empty_when_no_adapter():
    assert emit_interconnect_block("") == ""
    assert emit_interconnect_block(None) == ""


def test_interconnect_block_mirrors_interposer():
    assert emit_interconnect_block("ihp_cupillar") == (
        'interconnect:\n  adapter: "ihp_cupillar"\n\n'
    )
