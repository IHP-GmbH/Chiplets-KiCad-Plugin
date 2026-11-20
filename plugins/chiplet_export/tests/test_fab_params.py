# SPDX-License-Identifier: GPL-3.0-or-later
"""A method's pad parameters under interconnect methods 1.2's names.

From registry 1.2 on a method may state each pad parameter under two names,
passivation_opening_um beside passiv_opening_um and pad_metal_enclosure_um
beside tm2_enclosure_um, and the 1.x name stays required. Every plugin read
goes through method_fab_params. With interconnect_pdk's reader, which must
read interconnect methods 1.2 or later, the reader owns the rule and its
texts; without one (the board path) the plugin applies chiplet-spec 1.25.0's
rule (technology_package.method_fab_params) with the same two texts.
"""

import copy
import json

import pytest

from chiplet_export.carrier_profile import FabParamsRefused, check_reader, method_fab_params
from chiplet_export.pad_domains import PadDomainError, footprint_pin_list
from chiplet_export.pipeline import orchestrator

from .test_orchestrator import (  # noqa: F401 (stub_reader is a fixture)
    _RunExportBoard, _drc_options, _landing_fakes, stub_reader)
from .test_pad_domains import PCBNEW, footprint

RENAMED = [("passivation_opening_um", "passiv_opening_um"),
           ("pad_metal_enclosure_um", "tm2_enclosure_um")]
WORDS = {"passivation_opening_um": "passivation opening",
         "pad_metal_enclosure_um": "pad metal enclosure"}


def specs(**fab_params):
    return {"cupillar_opt2": {"fab_params": dict(fab_params)}}


OLD = {"passiv_opening_um": 40.0, "tm2_enclosure_um": 7.5}
BOTH = dict(OLD, passivation_opening_um=40.0, pad_metal_enclosure_um=7.5)


class OldReader:
    """interconnect_pdk's reader before 1.2: the entry as written."""

    SUPPORTED_SCHEMA_VERSION = "1.1"
    __file__ = "/pdk/libs.tech/klayout/python/interconnect_manifest.py"

    def fab_params(self, method_id, manifest, *, where=None):
        raise AssertionError("a reader below 1.2 was asked")


class Reader12:
    """A reader from 1.2 on, as its contract states: the whole dict with both
    names present and equal, or its own FabParamsRefused."""

    SUPPORTED_SCHEMA_VERSION = "1.2"

    class FabParamsRefused(Exception):
        pass

    def fab_params(self, method_id, manifest, *, where=None):
        fab = dict(manifest["methods"][method_id]["fab_params"])
        for new, old in RENAMED:
            if old not in fab:
                raise self.FabParamsRefused("PACKAGE_METHODS_FAB_PARAMS_MISSING: %s" % where)
            if fab.setdefault(new, fab[old]) != fab[old]:
                raise self.FabParamsRefused("PACKAGE_METHODS_FAB_PARAMS_DISAGREE: %s" % where)
        return fab


READERS = pytest.mark.parametrize("reader", [None, Reader12()],
                                  ids=["no reader", "reader 1.2"])


READER_PATH = "/pdk/libs.tech/klayout/python/interconnect_manifest.py"
NEEDS_1_2 = (" This plugin needs version 1.2 or later, because 1.2 names the pad "
             "parameters passivation_opening_um and pad_metal_enclosure_um. Update "
             "interconnect_pdk.")


@pytest.mark.parametrize("version", ["1.1", "1.0", "1.x"])
def test_a_reader_below_1_2_is_refused_before_it_is_asked(version):
    reader = OldReader()
    reader.SUPPORTED_SCHEMA_VERSION = version
    with pytest.raises(FabParamsRefused) as caught:
        method_fab_params("cupillar_opt2", specs(**BOTH), where="r.json", reader=reader)
    assert str(caught.value) == (
        "interconnect_pdk at %s reads interconnect methods up to version %s." % (
            READER_PATH, version) + NEEDS_1_2)


@pytest.mark.parametrize("version", [None, "", 12], ids=["none", "empty", "not a string"])
def test_a_reader_that_reports_no_version_is_refused_saying_so(version):
    reader = OldReader()
    reader.SUPPORTED_SCHEMA_VERSION = version
    with pytest.raises(FabParamsRefused) as caught:
        method_fab_params("cupillar_opt2", specs(**BOTH), where="r.json", reader=reader)
    assert str(caught.value) == (
        "interconnect_pdk at %s does not report which version of interconnect methods "
        "it reads." % READER_PATH + NEEDS_1_2)


@pytest.mark.parametrize("version", ["1.2", "1.10", "2.0"])
def test_a_reader_from_1_2_on_is_taken(version):
    reader = Reader12()
    reader.SUPPORTED_SCHEMA_VERSION = version
    check_reader(reader)
    assert method_fab_params("cupillar_opt2", specs(**OLD), reader=reader) == BOTH


@READERS
@pytest.mark.parametrize("fab", [OLD, BOTH], ids=["1.x names", "both names"])
def test_the_new_names_carry_the_old_names_value(reader, fab):
    params = method_fab_params("cupillar_opt2", specs(**fab), where="r.json", reader=reader)
    assert params == BOTH


def test_on_a_1_2_readers_output_it_changes_nothing():
    method_specs = specs(**OLD, vendor_key="kept")
    given = Reader12().fab_params("cupillar_opt2", {"methods": method_specs})
    assert method_fab_params("cupillar_opt2", method_specs, where="r.json",
                             reader=Reader12()) == given


def test_a_1_2_readers_refusal_is_the_plugins_with_its_text():
    with pytest.raises(FabParamsRefused, match="^PACKAGE_METHODS_FAB_PARAMS_DISAGREE: r.json$"):
        method_fab_params("cupillar_opt2", specs(**dict(OLD, passiv_opening_um=45.0),
                                                passivation_opening_um=40.0),
                          where="r.json", reader=Reader12())


# The two texts, word for word as chiplet-spec 1.25.0's conformance tests pin
# them, on the path without a reader (the board's).

@pytest.mark.parametrize("new,old", RENAMED)
def test_two_spellings_with_two_values_are_refused(new, old):
    reader = None
    fab = dict(OLD)
    fab[new] = fab[old] + 5.0
    with pytest.raises(FabParamsRefused) as caught:
        method_fab_params("cupillar_opt2", specs(**fab), where="r.json", reader=reader)
    assert str(caught.value) == (
        "PACKAGE_METHODS_FAB_PARAMS_DISAGREE: r.json: method cupillar_opt2 gives "
        "%s %s and %s %s. Since interconnect methods 1.2 these are two names for one "
        "value, so the file has no single reading. Make them equal."
        % (old, fab[old], new, fab[new]))


@pytest.mark.parametrize("new,old", RENAMED)
def test_a_method_without_the_old_name_is_refused(new, old):
    reader = None
    fab = dict(BOTH)
    del fab[old]
    with pytest.raises(FabParamsRefused) as caught:
        method_fab_params("cupillar_opt2", specs(**fab), where="r.json", reader=reader)
    assert str(caught.value) == (
        "PACKAGE_METHODS_FAB_PARAMS_MISSING: r.json: method cupillar_opt2 has no "
        "fab_params.%s. Interconnect methods 1.x keeps the old name beside %s, so "
        "that readers older than 1.2 still find it. Add it with the same value."
        % (old, new))


@pytest.mark.parametrize("new,old", RENAMED)
def test_a_method_with_neither_name_is_refused(new, old):
    reader = None
    fab = dict(OLD)
    del fab[old]
    with pytest.raises(FabParamsRefused) as caught:
        method_fab_params("cupillar_opt2", specs(**fab), where="r.json", reader=reader)
    assert str(caught.value) == (
        "PACKAGE_METHODS_FAB_PARAMS_MISSING: r.json: method cupillar_opt2 has no "
        "%s: fab_params.%s and %s are both missing. Add both, with the same value."
        % (WORDS[new], old, new))


@pytest.mark.parametrize("spec", [None, "fab", {"fab_params": "x"}, {"fab_params": None}])
def test_what_is_not_a_method_reads_as_one_without_pad_parameters(spec):
    with pytest.raises(FabParamsRefused, match="^PACKAGE_METHODS_FAB_PARAMS_MISSING"):
        method_fab_params("m", {"m": spec}, where="r.json")


def test_an_unknown_method_stays_a_key_error():
    with pytest.raises(KeyError):
        method_fab_params("m", {}, where="r.json")


# The board path: a method with unequal spellings is refused, naming the
# method and both values.

def test_the_board_path_refuses_a_method_whose_spellings_disagree():
    fp, _ = footprint()
    source = {"fab_anchor": "ihp_cupillar_anchor", "body_diameter_um": 44,
              "fab_params": {"passiv_opening_um": 35, "tm2_enclosure_um": 7.5,
                             "passivation_opening_um": 36}}
    with pytest.raises(PadDomainError) as caught:
        footprint_pin_list(fp, PCBNEW, {"attachment": source})
    assert str(caught.value) == (
        "PACKAGE_METHODS_FAB_PARAMS_DISAGREE: interconnect_methods.json: method "
        "attachment gives passiv_opening_um 35 and passivation_opening_um 36. Since "
        "interconnect methods 1.2 these are two names for one value, so the file has "
        "no single reading. Make them equal.")
    source["fab_params"]["passivation_opening_um"] = 35
    assert footprint_pin_list(fp, PCBNEW, {"attachment": source})


# The export: refused before any output, not after the worker.

def test_the_export_refuses_a_named_method_before_any_output(tmp_path, monkeypatch):
    calls = _landing_fakes(monkeypatch)
    snapshot = orchestrator._load_interconnect_manifest()
    snapshot.manifest["methods"]["pillar"] = {
        "fab_params": {"passiv_opening_um": 35.0, "tm2_enclosure_um": 7.5,
                       "pad_metal_enclosure_um": 8.0}}

    def forbid(*_a, **_kw):
        raise AssertionError("the refusal reached the landing query")

    monkeypatch.setattr(orchestrator, "query_connection_availability", forbid)
    options = _drc_options(tmp_path)
    options.connection_type = "pillar"
    result = orchestrator.run_export(_RunExportBoard(), options, "/plugin")
    assert result.error == (
        "Interconnect PDK source refused: PACKAGE_METHODS_FAB_PARAMS_DISAGREE: "
        "interconnect_methods.json: method pillar gives tm2_enclosure_um 7.5 and "
        "pad_metal_enclosure_um 8.0. Since interconnect methods 1.2 these are two "
        "names for one value, so the file has no single reading. Make them equal.")
    assert calls == []
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_a_method_the_export_does_not_name_is_not_its_refusal(tmp_path, monkeypatch):
    _landing_fakes(monkeypatch)
    snapshot = orchestrator._load_interconnect_manifest()
    snapshot.manifest["methods"]["bump"] = {
        "fab_params": {"passiv_opening_um": 35.0, "pad_metal_enclosure_um": 8.0}}
    monkeypatch.setattr(orchestrator, "query_connection_availability",
                        lambda *_a, **_kw: ({"pillar": None}, "query"))
    options = _drc_options(tmp_path)
    options.connection_type = "pillar"
    result = orchestrator.run_export(_RunExportBoard(), options, "/plugin")
    assert "PACKAGE_METHODS_FAB_PARAMS" not in result.error


def test_the_drc_sidecar_reads_the_new_name(tmp_path, stub_reader):
    chiplet = tmp_path / "a.chiplet"
    chiplet.write_text("components:\n  - id: U1\n    connection: m\n", encoding="utf-8")
    methods = {"m": {"pitch_rules": {"IXN_spacing": 40.0, "IXN_pitch": 75.0},
                     "fab_params": {"passiv_opening_um": 35.0, "tm2_enclosure_um": 7.5}}}
    derived = orchestrator.derive_interconnect_methods(str(chiplet), methods_db=methods)
    assert derived == {"m": {"dies": ["U1"], "IXN_spacing": 40.0, "IXN_pitch": 75.0,
                             "IXN_pad_size": 35.0}}
    methods["m"]["fab_params"]["passivation_opening_um"] = 36.0
    with pytest.raises(FabParamsRefused):
        orchestrator.derive_interconnect_methods(str(chiplet), methods_db=methods)
