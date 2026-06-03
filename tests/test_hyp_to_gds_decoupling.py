# SPDX-License-Identifier: GPL-2.0-or-later
"""
Host-side tests for hyp_to_gds connection-stack decoupling from the interconnect
PDK manifest. hyp_to_gds has no pcbnew/wx dependency, so these run on host.

Guards 0-regression: the manifest-sourced tables must reproduce the prior IHP
literals exactly, while the vendor demo method becomes selectable.
"""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402


def test_default_connection_stacks_byte_equal_to_literal():
    stacks = h.get_default_connection_stacks()
    assert list(stacks.keys()) == [
        "cupillar_opt1", "cupillar_opt2", "cupillar_opt3", "sbump_sac305"]
    assert stacks["cupillar_opt1"] == {
        "description": "PacTech Cu Pillar, Table 6.1 Option 1 (35um opening)",
        "layers": [
            {"name": "CuPillar", "material": "Cu", "height": 28.0, "diameter": 44.0},
            {"name": "SnAgCap", "material": "SnAg", "height": 16.0, "diameter": 44.0},
        ],
    }
    assert stacks["cupillar_opt2"]["layers"][0]["diameter"] == 49.0
    assert stacks["cupillar_opt3"]["layers"][1]["height"] == 19.0
    assert stacks["sbump_sac305"]["layers"] == [
        {"name": "SolderBall", "material": "SAC305", "height": 80.0, "diameter": 80.0}]
    # The non-IHP demo method must not leak into the default library.
    assert "vendorx" not in str(stacks).lower()


def test_body_diameter_ihp_identical():
    assert h._connection_to_body_diameter("cupillar_opt1") == 44
    assert h._connection_to_body_diameter("cupillar_opt2") == 49
    assert h._connection_to_body_diameter("cupillar_opt3") == 54
    # Solder bump -> no single pillar body -> None (skip pillar gen).
    assert h._connection_to_body_diameter("sbump_sac305") is None
    assert h._connection_to_body_diameter("") is None
    assert h._connection_to_body_diameter("bogus") is None


def test_body_diameter_vendor_enabled():
    """The vendor microbump is pillar-style, so it gets a body diameter."""
    assert h._connection_to_body_diameter("vendorx_microbump") == 40


def test_cli_choices_include_all_methods():
    choices = h._connection_type_cli_choices()
    for method in ("cupillar_opt1", "cupillar_opt2", "cupillar_opt3",
                   "sbump_sac305", "vendorx_microbump"):
        assert method in choices
