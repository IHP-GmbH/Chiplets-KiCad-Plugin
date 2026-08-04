# SPDX-License-Identifier: GPL-3.0-or-later
"""Footprint generation: labels, dimension bounds, field ownership.

The bound tests drive the real OpenIntM4TM2 generator (loaded by path, never
vendored) and skip when the interposer PDK checkout is not resolvable.
"""
import pytest

from resizer_passive_elements import apply_resize, paths

TECH_JSON = paths.discover_tech_json_path()
GEN_SCRIPT = paths.discover_footprint_gen_path()

needs_pdk = pytest.mark.skipif(
    not (TECH_JSON and GEN_SCRIPT),
    reason="interposer PDK checkout not resolvable; cannot load the generator",
)


@pytest.fixture(scope="module")
def tech():
    return apply_resize.load_tech(TECH_JSON, GEN_SCRIPT)


@pytest.fixture(scope="module")
def gen():
    return apply_resize._load_generator_module(GEN_SCRIPT)


@pytest.mark.parametrize("cap_fF,label", [
    (100, "100fF"),
    (10, "10fF"),
    (1500, "1p5pF"),
    (5000, "5pF"),
    (99.95575, "99p956fF"),
])
def test_cap_label_matches_the_committed_footprint_family(cap_fF, label):
    assert apply_resize._format_cap_label(cap_fF) == label


def test_meters_formatting_is_what_the_symbol_stores():
    # The symbol library stores w/l in metres as a bare number, and that is
    # the form the sibling chiplet_export exporter reads back.
    assert apply_resize._format_meters(8.11) == "8.11e-06"
    assert apply_resize._format_meters(57.68) == "5.768e-05"


def test_capacitance_formatting_keeps_the_unit():
    assert apply_resize._format_capacitance_fF(151.6) == "151.6fF"


def test_managed_fields_cover_everything_the_plugin_writes():
    # Any field the plugin writes itself must be excluded from the
    # carry-over of the replaced instance's fields, or the stale value wins.
    for name in ("Model", "Sim.Name", "w", "l", "m", "Capacitance",
                 "CMIM_GENERATED_FILE", "Reference", "Value"):
        assert name in apply_resize._MANAGED_FIELDS


@needs_pdk
def test_max_side_comes_from_the_pdk_not_a_literal(gen, tech):
    max_um = apply_resize._max_side_um(gen, tech)

    assert max_um is not None
    _cmin, cmax = gen.cap_bounds_fF(tech)
    assert max_um == pytest.approx(gen.cap_to_width(cmax, tech))
    assert tech["minLW_um"] < max_um < 1000.0


def test_max_side_degrades_to_none_on_a_broken_generator():
    class Broken:
        def cap_bounds_fF(self, _tech):
            raise RuntimeError("no bounds")

    assert apply_resize._max_side_um(Broken(), {}) is None


@needs_pdk
def test_an_oversized_w_is_rejected_instead_of_hanging(tmp_path, tech):
    # A bare "8.11" in the w field reads as metres (board_reader._parse_um),
    # i.e. 8.11e6 um. Without the upper bound the generator walks the Vmim via
    # array over that whole area, roughly 1e13 iterations on the wx thread,
    # and pcbnew never comes back. The guard must reject it outright.
    logged = []
    params = {"reference": "C1", "model": "cap_cmim",
              "w_um": 8.11e6, "l_um": 8.11e6}

    out = apply_resize.generate_footprint_file(
        params, tech, str(tmp_path), on_log=logged.append,
        gen_script_path=GEN_SCRIPT)

    assert out is None
    assert any("above the device maximum" in line for line in logged), logged
    assert not list(tmp_path.iterdir()), "nothing should have been written"


@needs_pdk
def test_an_undersized_w_is_still_rejected(tmp_path, tech):
    logged = []
    params = {"reference": "C1", "model": "cap_cmim",
              "w_um": 0.5, "l_um": 0.5}

    out = apply_resize.generate_footprint_file(
        params, tech, str(tmp_path), on_log=logged.append,
        gen_script_path=GEN_SCRIPT)

    assert out is None
    assert any("below the device minimum" in line for line in logged), logged


@needs_pdk
def test_a_legitimate_size_still_generates(tmp_path, tech):
    logged = []
    params = {"reference": "C1", "model": "cap_cmim",
              "w_um": 8.11, "l_um": 8.11}

    out = apply_resize.generate_footprint_file(
        params, tech, str(tmp_path), on_log=logged.append,
        gen_script_path=GEN_SCRIPT)

    assert out is not None
    assert out.endswith(".kicad_mod")
    assert params["capacitance_fF"] == pytest.approx(99.95575, rel=1e-4)


@needs_pdk
def test_an_out_of_range_capacitance_is_rejected(tmp_path, tech):
    logged = []
    params = {"reference": "C1", "model": "cap_cmim",
              "capacitance_fF": 1e9}

    out = apply_resize.generate_footprint_file(
        params, tech, str(tmp_path), on_log=logged.append,
        gen_script_path=GEN_SCRIPT)

    assert out is None
    assert any("out of range" in line for line in logged), logged


def test_an_unregistered_model_is_reported(tmp_path):
    logged = []

    out = apply_resize.generate_footprint_file(
        {"reference": "R1", "model": "res_rsil"}, {}, str(tmp_path),
        on_log=logged.append)

    assert out is None
    assert logged
