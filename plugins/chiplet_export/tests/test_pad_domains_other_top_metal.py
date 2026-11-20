# SPDX-License-Identifier: GPL-3.0-or-later
"""A version 2 record of a technology whose top metal is not TopMetal2.

The fixture is one device's CHIPLET_PAD_DOMAINS record as interposer-pnr's
routed-geometry writer writes it for its frozen typical_03 routed fixture with
the top metal renamed AlTop and no KiCad view (its tests/test_routed_geometry.py,
without_kicad_view_and_top_metal_named), so a version 2 record with no
carrier.kicad_layer. Its sha256 below holds the bytes. The plugin decodes it; its board
and layout remain IntM4TM2 only (README, carrier electrical pads).
"""

import hashlib
import json
from pathlib import Path

import pytest

from chiplet_export.pad_domains import PadDomainError, decode_domains

from .test_pad_domains import document
from .test_pad_domains_v2 import STACK, _LayerMap, _routed

FIXTURE = Path(__file__).parent / "fixtures" / "pad_domains_v2_other_top_metal.json"
FIXTURE_SHA256 = "6c36f2376b7fe2d9f3c06b6f5ab7fabd548bc0c427e2f6806a75c4e3f64e549f"


def test_the_fixture_is_the_record_interposer_pnr_wrote():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
    record = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert (record["version"], record["carrier"]["layer"]) == (2, "AlTop")
    assert "kicad_layer" not in record["carrier"]


def test_a_record_whose_top_metal_is_not_topmetal2_decodes():
    record = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert decode_domains(FIXTURE.read_text(encoding="utf-8"), "U1",
                          record["interconnect"]["method"]) == record


def test_the_routed_source_holds_an_intm4tm2_carrier_on_the_top_metal():
    assert _routed(STACK, document()["carrier"]).validate_pad_domains(
        {}, load_layer_map=_LayerMap) == {"U1": "attachment"}


def test_a_carrier_below_the_top_metal_is_refused_though_its_layer_exists():
    """TopMetal1 is a metal of the IntM4TM2 stack, so the layer map holds it;
    only the top-metal rule refuses it."""
    carrier = dict(document()["carrier"], layer="TopMetal1")
    with pytest.raises(PadDomainError) as caught:
        _routed(STACK, carrier).validate_pad_domains({}, load_layer_map=_LayerMap)
    assert str(caught.value) == (
        "U1 carrier layer: carrier.layer is 'TopMetal1', but the top metal of the "
        "routed stack is 'TopMetal2'. The routed path lands carrier pads on the top "
        "metal only.")
