# SPDX-License-Identifier: GPL-3.0-or-later
"""The board path holds a version 2 carrier to the top metal.

A version 2 pad-domain record names no metal of its own, so on the board its
carrier.layer is compared, through the selected LYP, with the top metal of
the carrier technology package's stack: the last of beol.metals in the file
at stack.path. Version 1 keeps its TopMetal2 check in decode_domains.
"""

import json
import sys
from unittest.mock import Mock

import pytest

from chiplet_export.layer_map import CarrierPackageRefused, LayerMap, read_carrier_package
from chiplet_export.pad_domains import PadDomainError, validate_board_pad_domains

from .conftest import FIXTURE_PDK_ROOT, write_carrier_package
from .test_carrier_layer_mapping import board_for, methods_for, write_lyp
from .test_pad_domains import PCBNEW, document, footprint
from .test_pad_domains_v2 import version_2

ALIASED = [("TopMetal2.drawing", "134/0"), ("AlTop.drawing", "134/0"),
           ("TopMetal1.drawing", "126/0"), ("Metal5.drawing", "67/0")]


def top_metal_text(layer, top):
    return ("U1 carrier layer: carrier.layer is '%s', but the top metal of the board is "
            "'%s'. Carrier pads land on the top metal only." % (layer, top))


def validate(tmp_path, monkeypatch, doc, board_name, *, load_top_metal, entries=ALIASED):
    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    fp, _ = footprint(doc)
    layer_map = LayerMap(write_lyp(tmp_path / "selected.lyp", entries), strict=True)
    return validate_board_pad_domains(
        board_for(fp, board_name), methods_for(document()),
        load_layer_map=lambda: layer_map, load_top_metal=load_top_metal)


def test_the_fixture_packages_top_metal_is_the_last_metal_of_its_stack():
    assert read_carrier_package(str(FIXTURE_PDK_ROOT)).top_metal() == "TopMetal2"


def test_the_top_metal_is_read_from_the_stack_not_named_in_the_code(tmp_path):
    write_carrier_package(tmp_path, stack="libs.tech/s.json")
    (tmp_path / "libs.tech" / "s.json").write_text(json.dumps(
        {"beol": {"metals": [{"name": "Metal1"}, {"name": "AlTop"}]}}), encoding="utf-8")
    assert read_carrier_package(str(tmp_path)).top_metal() == "AlTop"


@pytest.mark.parametrize("stack", [
    {}, {"beol": None}, {"beol": {"metals": []}}, {"beol": {"metals": [{}]}},
    {"beol": {"metals": [{"name": "Metal1"}, {"name": ""}]}}, [], "beol",
])
def test_a_stack_without_a_top_metal_name_is_refused(tmp_path, stack):
    write_carrier_package(tmp_path, stack="libs.tech/s.json")
    path = tmp_path / "libs.tech" / "s.json"
    path.write_text(json.dumps(stack), encoding="utf-8")
    with pytest.raises(CarrierPackageRefused) as caught:
        read_carrier_package(str(tmp_path)).top_metal()
    assert str(caught.value) == "PACKAGE_STACK_KEY: %s has no metal name at beol.metals" % path


def test_a_stack_that_is_not_json_is_refused(tmp_path):
    write_carrier_package(tmp_path, stack="libs.tech/s.json")
    path = tmp_path / "libs.tech" / "s.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(CarrierPackageRefused) as caught:
        read_carrier_package(str(tmp_path)).top_metal()
    assert caught.value.message_id == "PACKAGE_STACK_KEY"
    assert str(caught.value).startswith("PACKAGE_STACK_KEY: %s: " % path)


def test_a_version_2_carrier_on_the_top_metal_passes(tmp_path, monkeypatch):
    load = Mock(return_value="TopMetal2")
    assert validate(tmp_path, monkeypatch, version_2(), "TopMetal2",
                    load_top_metal=load) == {"U1": "attachment"}
    load.assert_called_once_with()


def test_a_version_2_carrier_on_an_alias_of_the_top_metal_passes(tmp_path, monkeypatch):
    """The comparison is by layer, through the LYP, as the board-name check."""
    assert validate(tmp_path, monkeypatch, version_2(layer="AlTop"), "AlTop",
                    load_top_metal=lambda: "TopMetal2") == {"U1": "attachment"}


@pytest.mark.parametrize("layer", ["TopMetal1", "Metal5"])
def test_a_version_2_carrier_below_the_top_metal_is_refused(tmp_path, monkeypatch, layer):
    """The board names the layer the record names, so only the top-metal
    rule refuses it."""
    with pytest.raises(PadDomainError) as caught:
        validate(tmp_path, monkeypatch, version_2(layer=layer), layer,
                 load_top_metal=lambda: "TopMetal2")
    assert str(caught.value) == top_metal_text(layer, "TopMetal2")


def test_the_top_metal_is_the_packages_not_topmetal2(tmp_path, monkeypatch):
    with pytest.raises(PadDomainError) as caught:
        validate(tmp_path, monkeypatch, version_2(), "TopMetal2",
                 load_top_metal=lambda: "TopMetal1")
    assert str(caught.value) == top_metal_text("TopMetal2", "TopMetal1")


def test_version_1_does_not_read_the_package(tmp_path, monkeypatch):
    def forbid():
        raise AssertionError("version 1 read the carrier package")

    assert validate(tmp_path, monkeypatch, document(), "TopMetal2",
                    load_top_metal=forbid) == {"U1": "attachment"}


def test_version_2_without_the_package_is_refused(tmp_path, monkeypatch):
    with pytest.raises(PadDomainError) as caught:
        validate(tmp_path, monkeypatch, version_2(), "TopMetal2", load_top_metal=None)
    assert str(caught.value) == (
        "U1 carrier layer: Carrier validation requires the carrier technology package")


def test_a_package_refusal_is_a_pad_domain_refusal(tmp_path, monkeypatch):
    def refused():
        raise CarrierPackageRefused("PACKAGE_STACK_KEY", "s.json has no metal name at beol.metals")

    with pytest.raises(PadDomainError) as caught:
        validate(tmp_path, monkeypatch, version_2(), "TopMetal2", load_top_metal=refused)
    assert str(caught.value) == (
        "U1 carrier layer: PACKAGE_STACK_KEY: s.json has no metal name at beol.metals")


# The export: the top metal is read from the package the worker is given.

@pytest.fixture
def export_v2(tmp_path, monkeypatch):
    from chiplet_export.pipeline import orchestrator
    from chiplet_export.writers.connection_stacks import InterconnectManifestSnapshot

    from .test_orchestrator import _install_run_export_fakes

    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
    fp, _ = footprint(version_2())
    calls, _ = _install_run_export_fakes(monkeypatch, die_connections={"U1": "attachment"})
    snapshot = InterconnectManifestSnapshot({"methods": methods_for(document())})
    monkeypatch.setattr(orchestrator, "_preflight_interconnect_manifest",
                        lambda *a, **kw: snapshot)
    options = orchestrator.ExportOptions(
        output_dir=str(tmp_path / "out"), emit_chiplet=False, emit_assembly_drc=False,
        lyp_override=str(write_lyp(tmp_path / "selected.lyp", ALIASED)))

    def run(plugin_dir):
        return orchestrator.run_export(board_for(fp), options, str(plugin_dir))

    return options, calls, run


def test_the_export_reads_the_top_metal_of_the_selected_package(tmp_path, export_v2):
    options, calls, run = export_v2
    root = tmp_path / "pdk"
    write_carrier_package(root, stack="libs.tech/s.json")
    (root / "libs.tech" / "s.json").write_text(json.dumps(
        {"beol": {"metals": [{"name": "TopMetal2"}, {"name": "TopMetal1"}]}}), encoding="utf-8")
    options.interposer_pdk_root = str(root)
    result = run(tmp_path / "plugin")
    assert top_metal_text("TopMetal2", "TopMetal1") in result.error
    assert calls == [] and not (tmp_path / "out").exists()


def test_the_export_without_a_carrier_package_is_refused(tmp_path, export_v2):
    options, calls, run = export_v2
    result = run(tmp_path / "plugin")
    assert ("U1 carrier layer: PACKAGE_MANIFEST_MISSING: no carrier technology package was "
            "found; set INTERPOSER_PDK_ROOT to its root") in result.error
    assert calls == [] and not (tmp_path / "out").exists()
