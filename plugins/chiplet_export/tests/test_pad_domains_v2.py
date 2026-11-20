# SPDX-License-Identifier: GPL-3.0-or-later
"""Pad-domain archive version 2, beside version 1.

Version 2 (interposer-pnr's writer from interconnect methods 1.2 on) names
the two pad parameters by their neutral names, passivation_opening_um and
pad_metal_enclosure_um, and makes carrier.kicad_layer optional: a technology
without a KiCad view has none. It names no metal the reader must expect; the
carrier layer is held by the physical check instead (the board's LYP on the
board path, the routed stack's top metal on the routed path). Version 1, as
kept boards and signed records hold it, reads as before.
"""

import copy
import json
from types import SimpleNamespace

import pytest

from chiplet_export.pad_domains import (
    PadDomainError, archive_pin_list, decode_domains, footprint_pin_list)
from chiplet_export.routed.source import RoutedSource

from .test_pad_domains import PCBNEW, document, footprint

NO_KICAD_VIEW = ("U1: this technology package has no KiCad view (carrier.kicad_layer "
                 "is not set), so the KiCad board cannot be written. Add kicad_template "
                 "and a kicad_copper_layer for every routing layer.")


def version_2(doc=None, *, layer="TopMetal2", kicad_layer="F.Cu"):
    """``doc`` (a version 1 archive) as interposer-pnr writes it at version 2."""
    doc = copy.deepcopy(document() if doc is None else doc)
    doc["version"] = 2
    interconnect = doc["interconnect"]
    interconnect["passivation_opening_um"] = interconnect.pop("passiv_opening_um")
    interconnect["pad_metal_enclosure_um"] = interconnect.pop("tm2_enclosure_um")
    doc["carrier"]["layer"] = layer
    if kicad_layer is None:
        del doc["carrier"]["kicad_layer"]
    else:
        doc["carrier"]["kicad_layer"] = kicad_layer
    return doc


def decode(doc):
    return decode_domains(json.dumps(doc), "U1", "attachment")


@pytest.mark.parametrize("kicad_layer", ["F.Cu", None], ids=["kicad view", "no kicad view"])
def test_a_version_2_archive_decodes(kicad_layer):
    doc = version_2(kicad_layer=kicad_layer)
    assert decode(doc) == doc


def test_version_2_names_no_metal_of_its_own():
    """The TopMetal2 literal holds version 1 only: a second carrier's top metal
    has its own name."""
    assert decode(version_2(layer="Metal7", kicad_layer=None))["carrier"]["layer"] == "Metal7"


def test_the_landing_diameter_is_read_by_the_versions_names():
    doc = version_2(document(opening=45, enclosure=10))
    assert doc["carrier"]["diameter_um"] == 65
    decode(doc)
    doc["carrier"]["diameter_um"] = 50
    with pytest.raises(PadDomainError, match="diameter differs"):
        decode(doc)


@pytest.mark.parametrize("edit", [
    lambda d: d["carrier"].update(layer=""),
    lambda d: d["carrier"].pop("layer"),
    lambda d: d["carrier"].update(kicad_layer="TopMetal2"),
    lambda d: d["carrier"].update(kicad_layer=None),
    lambda d: d["carrier"].update(shape="rect"),
    lambda d: d["interconnect"].update(pad_metal_enclosure_um=float("nan")),
    lambda d: d["interconnect"].pop("passivation_opening_um"),
    lambda d: d.update(version=3),
], ids=["empty layer", "no layer", "kicad layer not canonical", "kicad layer null",
        "shape", "enclosure nan", "no opening", "version 3"])
def test_a_tampered_version_2_archive_refuses(edit):
    doc = version_2()
    edit(doc)
    with pytest.raises(PadDomainError):
        decode(doc)


@pytest.mark.parametrize("version, key", [
    (2, "passiv_opening_um"), (2, "tm2_enclosure_um"),
    (1, "passivation_opening_um"), (1, "pad_metal_enclosure_um"),
])
def test_an_archive_never_mixes_the_two_spellings(version, key):
    """As interposer-pnr's validator: a key of the other version is refused, so
    no reader picks one of two values for one parameter."""
    doc = version_2() if version == 2 else document()
    doc["interconnect"][key] = 1.0
    with pytest.raises(PadDomainError) as caught:
        decode(doc)
    assert str(caught.value) == (
        "U1: interconnect.%s is not a key of pad domain version %d" % (key, version))


@pytest.mark.parametrize("edit", [
    lambda d: d["carrier"].update(layer="Metal7"),
    lambda d: d["carrier"].pop("kicad_layer"),
], ids=["another metal", "no kicad layer"])
def test_version_1_reads_as_before(edit):
    doc = document()
    edit(doc)
    with pytest.raises(PadDomainError, match="Unsupported carrier shape/layer profile"):
        decode(doc)


def test_the_routed_path_reads_a_version_2_archive():
    sidecar = archive_pin_list(version_2(kicad_layer=None), "U1", "attachment", "flip_chip")
    assert sidecar == archive_pin_list(document(), "U1", "attachment", "flip_chip")


def test_the_board_path_refuses_an_archive_without_a_kicad_view():
    fp, _ = footprint(version_2(kicad_layer=None))
    with pytest.raises(PadDomainError) as exc:
        footprint_pin_list(fp, PCBNEW)
    assert str(exc.value) == NO_KICAD_VIEW


def test_the_board_path_reads_a_version_2_archive_with_a_kicad_view():
    fp, _ = footprint(version_2())
    assert footprint_pin_list(fp, PCBNEW) == footprint_pin_list(footprint()[0], PCBNEW)


# The routed source: the archive's carrier layer is the routed stack's top
# metal, then the worker LYP resolves it.

class _LayerMap:
    """Every name is a layer of its own, as the strict LYP names them."""

    def get_layer(self, name):
        return ("layer", name)


def _routed(stack, *carriers):
    source = RoutedSource.__new__(RoutedSource)
    source._prepared = SimpleNamespace(
        document={"stack": stack, "devices": [
            {"ref": "U%d" % i, "pad_domains": {"carrier": carrier}}
            for i, carrier in enumerate(carriers, 1)]},
        die_connections={"U%d" % i: "attachment" for i in range(1, len(carriers) + 1)})
    return source


STACK = [{"metal": "Metal1", "cut_above": "Via1"},
         {"metal": "TopMetal1", "cut_above": "TopVia2"},
         {"metal": "TopMetal2", "cut_above": None}]


@pytest.mark.parametrize("carrier", [
    version_2(kicad_layer=None)["carrier"], version_2()["carrier"], document()["carrier"],
], ids=["v2 no kicad view", "v2 F.Cu", "v1"])
def test_the_routed_source_holds_the_carrier_to_the_top_metal(carrier):
    source = _routed(STACK, carrier)
    assert source.validate_pad_domains({}, load_layer_map=_LayerMap) == {"U1": "attachment"}


def test_a_carrier_off_the_top_metal_is_refused_naming_both():
    stack = STACK[:-1] + [{"metal": "TopMetal2", "cut_above": "TopVia3"},
                          {"metal": "TopMetal3", "cut_above": None}]
    with pytest.raises(PadDomainError) as exc:
        _routed(stack, document()["carrier"]).validate_pad_domains(
            {}, load_layer_map=_LayerMap)
    assert str(exc.value) == (
        "U1 carrier layer: carrier.layer is 'TopMetal2', but the top metal of the "
        "routed stack is 'TopMetal3'. The routed path lands carrier pads on the top "
        "metal only.")


def test_a_layer_map_refusal_is_a_pad_domain_refusal():
    from chiplet_export.layer_map import LayerMapError

    def refused():
        raise LayerMapError("Cannot read LYP 'x.lyp': gone")

    with pytest.raises(PadDomainError, match=r"^U1 carrier layer: Cannot read LYP"):
        _routed(STACK, document()["carrier"]).validate_pad_domains({}, load_layer_map=refused)
