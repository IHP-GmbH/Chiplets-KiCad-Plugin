# SPDX-License-Identifier: GPL-3.0-or-later
"""routed_document: the stack and nets of the routed geometry, nothing ignored.

Each case changes one thing in the routed_t03 document and expects a refusal
naming it; the document as written must pass.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2]
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))

from chiplet_export.routed_document import (  # noqa: E402
    DocumentRefused, bound_files, check_nets, stack_metals,
)

DOCUMENT = (Path(__file__).resolve().parent / "fixtures" / "routed_t03" / "inputs"
            / "routed-geometry.json")


@pytest.fixture(scope="module")
def document():
    return json.loads(DOCUMENT.read_text())


def test_the_written_document_passes(document):
    nets = check_nets(document)
    assert len(nets) == 10
    assert sum(len(net["segments"]) for net in nets) == 54
    assert sum(len(net["drawn"]) for net in nets) == 22
    assert stack_metals(document) == ["Metal4", "Metal5", "TopMetal1", "TopMetal2"]


def _net(document, name="VDD"):
    return next(net for net in document["nets"] if net["name"] == name)


def _arc_list(d):
    _net(d)["arcs"] = []


def _segment_key(d):
    _net(d)["segments"][0]["xc"] = 0.0


def _segment_missing(d):
    del _net(d)["segments"][0]["width"]


def _zero_width(d):
    _net(d)["segments"][0]["width"] = 0


def _unknown_layer(d):
    _net(d)["segments"][0]["layer"] = "Metal3"


def _empty_board_name(d):
    _net(d)["board_name"] = ""


def _drawn_moved(d):
    net = next(net for net in d["nets"] if net["drawn"])
    net["drawn"][0]["x"] += 1.0


def _drawn_missing(d):
    net = next(net for net in d["nets"] if net["drawn"])
    net["drawn"].pop()


def _drawn_key(d):
    net = next(net for net in d["nets"] if net["drawn"])
    net["drawn"][0]["shape"] = "round"


def _label_not_a_name(d):
    _net(d)["net_class"] = 3


def _duplicate_net(d):
    d["nets"].append(copy.deepcopy(d["nets"][0]))


def _cut_on_top(d):
    d["stack"][-1]["cut_above"] = "TopVia3"


def _stack_key(d):
    d["stack"][0]["thickness"] = 0.5


def _drawn(d):
    return next(net for net in d["nets"] if net["drawn"])["drawn"][0]


def _pad_single(d):
    _drawn(d)["pad"] = [1.9]


def _pad_zero(d):
    _drawn(d)["pad"] = [1.9, 0]


def _cut_zero(d):
    _drawn(d)["cut"] = 0.0


def _enclosure_negative(d):
    _drawn(d)["enclosure"] = -0.1


def _k_zero(d):
    _drawn(d)["k"] = 0


def _k_true(d):
    _drawn(d)["k"] = True


def _k_without_pitch(d):
    _drawn(d)["k"] = 2


def _pitch_zero(d):
    _drawn(d)["pitch"] = 0.0


def _size_negative(d):
    net = next(net for net in d["nets"] if net["vias"])
    net["vias"][0]["size"] = -1.0


def _via_skips_a_metal(d):
    net = next(net for net in d["nets"] if net["vias"])
    for record in (net["vias"][0], net["drawn"][0]):
        record["bottom_layer"] = "Metal5"


CASES = {
    "a pad that is not a pair": (_pad_single, "pad"),
    "a zero pad": (_pad_zero, "pad[1]"),
    "a zero cut": (_cut_zero, ".cut"),
    "a negative enclosure": (_enclosure_negative, "enclosure"),
    "no cut": (_k_zero, ".k"),
    "a boolean count": (_k_true, ".k"),
    "cuts without a pitch": (_k_without_pitch, "no pitch"),
    "a zero pitch": (_pitch_zero, "pitch"),
    "a negative via size": (_size_negative, "size"),
    "a via across two cuts": (_via_skips_a_metal, "not adjacent"),
    "an arc list": (_arc_list, "'arcs'"),
    "a segment key": (_segment_key, "'xc'"),
    "a missing segment key": (_segment_missing, "'width'"),
    "a zero width": (_zero_width, "width"),
    "a layer the stack does not name": (_unknown_layer, "Metal3"),
    "an empty board name": (_empty_board_name, "board_name"),
    "a drawn via off its placement": (_drawn_moved, "placed x"),
    "a via drawn short": (_drawn_missing, "places"),
    "a drawn via key": (_drawn_key, "'shape'"),
    "a label that is not a name": (_label_not_a_name, "net_class"),
    "a net twice": (_duplicate_net, "twice"),
    "a cut above the top metal": (_cut_on_top, "cut_above"),
    "a stack key": (_stack_key, "'thickness'"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_changed_document_is_refused(document, case):
    edit, message = CASES[case]
    changed = copy.deepcopy(document)
    edit(changed)
    with pytest.raises(DocumentRefused) as exc:
        check_nets(changed)
    assert message in str(exc.value)


def test_a_device_without_a_bound_pin_list_is_refused(document):
    """Before any file is read: the pin list of the first device is unbound."""
    changed = copy.deepcopy(document)
    ref = changed["devices"][0]["ref"]
    del changed["inputs"]["pins"][ref]
    with pytest.raises(DocumentRefused) as exc:
        bound_files(changed, str(DOCUMENT))
    assert "binds no pin list for %s" % ref in str(exc.value)


def test_a_device_without_a_layout_digest_is_refused(document):
    changed = copy.deepcopy(document)
    del changed["devices"][0]["layout"]["sha256"]
    with pytest.raises(DocumentRefused) as exc:
        bound_files(changed, str(DOCUMENT))
    assert "no layout path and sha256" in str(exc.value)
