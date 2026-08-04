# SPDX-License-Identifier: GPL-3.0-or-later
"""Footprint generation: labels, dimension bounds, field ownership.

The bound tests drive the real OpenIntM4TM2 generator (loaded by path, never
vendored) and skip when the interposer PDK checkout is not resolvable.

Naming follows the PDK's own two-property convention. `Nominal` is the round
value a part is named for, `Capacitance` is what the drawn plate actually
gives, and they differ by the placement grid: the exact width for 100 fF is
8.111807 um, the 5 nm grid forces 8.110, and the result is 99.95575 fF. Naming
from the recomputed value instead of the nominal is what renamed the stock
CMIM_100fF part to CMIM_99p956fF on its second pass through the plugin.
"""
import re

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
def test_the_bound_is_on_capacitance_not_on_the_side(gen, tech):
    over = apply_resize._over_max_capacitance

    # What runs away is the via array, whose cell count scales with w*l, and
    # what the PDK specifies is Cmax. A tall thin rectangle is in spec.
    assert over(gen, tech, 100.0, 50.0) is None      # 7512 fF
    assert over(gen, tech, 200.0, 20.0) is None      # 6018 fF
    assert over(gen, tech, 8.11, 8.11) is None
    # The square at Cmax is the boundary, but only for a square.
    _cmin, cmax = gen.cap_bounds_fF(tech)
    side = gen.cap_to_width(cmax, tech)
    assert over(gen, tech, side, side) is None
    assert over(gen, tech, side * 1.1, side * 1.1) is not None
    # The metres/micrometres mix-up, the case this exists for.
    cap_fF, cmax_fF = over(gen, tech, 8.11e6, 8.11e6)
    assert cap_fF > cmax_fF


def test_the_bound_degrades_to_none_on_a_broken_generator():
    class Broken:
        def cap_bounds_fF(self, _tech):
            raise RuntimeError("no bounds")

    assert apply_resize._over_max_capacitance(Broken(), {}, 1.0, 1.0) is None


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
def test_an_in_spec_rectangle_still_generates(tmp_path, tech):
    # 100 x 50 um is 7512 fF, inside Cmax: bounding by the square side at Cmax
    # (72.975 um) would reject it, which the pre-fix plugin did not.
    logged = []
    params = {"reference": "C1", "model": "cap_cmim",
              "w_um": 100.0, "l_um": 50.0}

    out = apply_resize.generate_footprint_file(
        params, tech, str(tmp_path), on_log=logged.append,
        gen_script_path=GEN_SCRIPT)

    assert out is not None, logged
    assert params["capacitance_fF"] == pytest.approx(7512.0, rel=1e-4)


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


@pytest.mark.parametrize("cap_fF,label", [
    (100, "100fF"),
    (5000, "5pF"),
    (1500, "1.5pF"),      # the property keeps the dot; only the NAME swaps it
])
def test_nominal_label_keeps_the_decimal_point(cap_fF, label):
    assert apply_resize._format_nominal_label(cap_fF) == label


@needs_pdk
def test_a_nominal_survives_only_while_it_describes_the_geometry(gen, tech):
    live = apply_resize._live_nominal_fF

    # 8.11 um square is the grid-snapped width for 100 fF: the label holds.
    assert live(gen, tech, 100.0, 8.11, 8.11) == 100.0
    # Resized: the part is no longer a 100 fF device, so the label must go.
    assert live(gen, tech, 100.0, 10.0, 10.0) is None
    # The nominal family is square only.
    assert live(gen, tech, 100.0, 8.11, 12.0) is None
    # Nothing to preserve.
    assert live(gen, tech, None, 8.11, 8.11) is None
    assert live(gen, tech, 0.0, 8.11, 8.11) is None


@needs_pdk
def test_a_stock_part_keeps_its_name_across_runs(tmp_path, tech):
    """The plugin rewrites Capacitance with the recomputed value; that value
    must not become the next run's name, or nothing is ever stable."""
    params = {"reference": "C1", "model": "cap_cmim", "w_um": 8.11,
              "l_um": 8.11, "capacitance_fF": 100.0, "nominal_fF": 100.0}
    names = []

    for _ in range(3):
        out = apply_resize.generate_footprint_file(
            params, tech, str(tmp_path), gen_script_path=GEN_SCRIPT)
        names.append(out)
        # feed back what _apply_cap_cmim_fields would stamp on the footprint
        params = dict(params, capacitance_fF=params["capacitance_fF"],
                      nominal_fF=params["nominal_fF"])

    assert all(n.endswith("CMIM_100fF.kicad_mod") for n in names), names


@needs_pdk
def test_a_resized_part_is_named_for_what_it_now_is(tmp_path, tech):
    # Stale Nominal left at 100fF while w/l describe a larger plate.
    params = {"reference": "C1", "model": "cap_cmim", "w_um": 10.0,
              "l_um": 10.0, "capacitance_fF": 100.0, "nominal_fF": 100.0}

    out = apply_resize.generate_footprint_file(
        params, tech, str(tmp_path), gen_script_path=GEN_SCRIPT)

    assert out.endswith("CMIM_151p6fF.kicad_mod"), out
    # The stale label is dropped rather than carried onto a different device.
    assert params["nominal_fF"] is None


@needs_pdk
def test_the_generated_part_matches_the_committed_one(tmp_path, tech):
    """Regenerating a family member must reproduce the PDK's own footprint."""
    committed = (paths.Path(paths.discover_tech_json_path()).parents[3]
                 / "kicad" / "footprints" / "intm4tm2.pretty"
                 / "CMIM_100fF.kicad_mod")
    if not committed.is_file():
        pytest.skip("committed CMIM_100fF.kicad_mod not present")

    out = apply_resize.generate_footprint_file(
        {"reference": "C1", "model": "cap_cmim", "w_um": 8.11, "l_um": 8.11,
         "capacitance_fF": 100.0, "nominal_fF": 100.0},
        tech, str(tmp_path), gen_script_path=GEN_SCRIPT)

    def properties(text):
        return dict(re.findall(r'\(property "([^"]+)" "([^"]*)"', text))

    generated = properties(paths.Path(out).read_text())
    reference = properties(committed.read_text())
    for key in ("Value", "Nominal", "Capacitance", "w", "l", "m"):
        assert generated[key] == reference[key], key


def test_an_unregistered_model_is_reported(tmp_path):
    logged = []

    out = apply_resize.generate_footprint_file(
        {"reference": "R1", "model": "res_rsil"}, {}, str(tmp_path),
        on_log=logged.append)

    assert out is None
    assert logged
