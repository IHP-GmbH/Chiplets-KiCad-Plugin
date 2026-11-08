# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure profile checks; these do not claim to execute the PDK PCell."""

import pytest

from carrier_profile import resolve_carrier_profile


TABLE = {44.0: {"passiv_opening": 35.0}}


@pytest.mark.parametrize("body,opening,enclosure", [
    (44, 35, 7.5), (80, 60, 10), (44, 35, 9),
])
def test_explicit_manifest_geometry_wins(body, opening, enclosure):
    assert resolve_carrier_profile("attachment", {
        "passiv_opening_um": opening, "tm2_enclosure_um": enclosure,
    }, body, TABLE) == (opening, enclosure)


@pytest.mark.parametrize("key", ["passiv_opening_um", "tm2_enclosure_um"])
@pytest.mark.parametrize("value", [None, True, "7.5", 0, -1, float("nan"),
                                  float("inf"), 10 ** 1000])
def test_absent_or_invalid_fabrication_values_refuse(key, value):
    fab = {"passiv_opening_um": 35, "tm2_enclosure_um": 7.5}
    fab[key] = value
    with pytest.raises(ValueError, match=key):
        resolve_carrier_profile("attachment", fab, 44, TABLE)


def test_known_body_with_different_opening_refuses_pdk_override():
    with pytest.raises(ValueError, match="cannot honor"):
        resolve_carrier_profile("attachment", {
            "passiv_opening_um": 36, "tm2_enclosure_um": 7.5,
        }, 44, TABLE)


def test_missing_fab_mapping_does_not_fall_back_to_table():
    with pytest.raises(ValueError, match="fab_params"):
        resolve_carrier_profile("attachment", None, 44, TABLE)


def test_overflowing_landing_diameter_refuses():
    with pytest.raises(ValueError, match="landing diameter"):
        resolve_carrier_profile("attachment", {
            "passiv_opening_um": 1e308, "tm2_enclosure_um": 1e308,
        }, 80, TABLE)
