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
    DocumentRefused, check_nets, stack_metals,
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


CASES = {
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
