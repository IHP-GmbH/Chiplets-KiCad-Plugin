# SPDX-License-Identifier: GPL-3.0-or-later
"""
Host-side tests for writers/_technology.py (no pcbnew).

The technology id a die's LYP_FILE gives: the file stem for a sheet named
after its technology, the holding folder for a project's per-technology
``layers.lyp``.
"""

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_export.writers._technology import (  # noqa: E402
    PROJECT_SHEET,
    technology_id,
)


def test_project_sheet_name():
    assert PROJECT_SHEET == "layers.lyp"


@pytest.mark.parametrize("lyp, expected", [
    ("${GDS_TO_KICAD_ROOT}/pdks/sg13g2.lyp", "sg13g2"),
    ("/opt/pdk/libs.tech/klayout/tech/intm4tm2.lyp", "intm4tm2"),
    ("zzz_tech.lyp", "zzz_tech"),
    ("aaa_tech.lyp", "aaa_tech"),
    ("my tech.v2.lyp", "my_tech_v2"),
])
def test_a_named_sheet_keeps_its_stem(lyp, expected):
    """Ids that exist today do not move."""
    assert technology_id(lyp) == expected


@pytest.mark.parametrize("lyp, expected", [
    ("${CHIPLET_FLOW_TECH}/sg13g2/layers.lyp", "sg13g2"),
    ("/work/p/inputs/tech/gf180mcu/layers.lyp", "gf180mcu"),
    ("inputs/tech/sg13g2/layers.lyp", "sg13g2"),
    ("sg13g2/layers.lyp", "sg13g2"),
    ("${CHIPLET_FLOW_TECH}/my tech.v2/layers.lyp", "my_tech_v2"),
])
def test_a_project_sheet_takes_its_folder(lyp, expected):
    assert technology_id(lyp) == expected


def test_two_project_sheets_are_two_technologies():
    a = technology_id("${CHIPLET_FLOW_TECH}/tech_a/layers.lyp")
    b = technology_id("${CHIPLET_FLOW_TECH}/tech_b/layers.lyp")
    assert (a, b) == ("tech_a", "tech_b")


@pytest.mark.parametrize("lyp", [
    "layers.lyp",
    "./layers.lyp",
    "../layers.lyp",
    "${CHIPLET_FLOW_TECH}/layers.lyp",
])
def test_a_project_sheet_without_a_technology_folder_keeps_its_stem(lyp):
    assert technology_id(lyp) == "layers"


@pytest.mark.parametrize("lyp, expected", [
    ("/x/foo/bar.lyp", "bar"),
    ("/x/sg13g2/Layers.lyp", "Layers"),
    ("/x/sg13g2/layers.lyp.bak", "layers_lyp"),
    ("/x/sg13g2/layers.xml", "layers"),
])
def test_only_the_project_sheet_name_reads_the_folder(lyp, expected):
    assert technology_id(lyp) == expected
