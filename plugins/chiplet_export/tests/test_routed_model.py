# SPDX-License-Identifier: GPL-3.0-or-later
"""The worker's model of the routed geometry is the board path's.

The oracle is the board path's own .hyp from the sealed typical_03 golden run
(fixtures/routed_t03/inputs/board-path.hyp), read by the worker's HYPParser.
RoutedGeometryParser reads the routed geometry of the same design. Every field
the composer reads must agree, record by record and in order: positions within
1 nm and equal once snapped to the layout's 1 nm grid, everything else exactly.
A failure names the first record that differs.

Where the two inputs cannot agree by construction, the comparison says how it
normalises, and nothing else is normalised:

- perimeter: KiCad writes the outline's edges in its own order and direction,
  the document gives the outline's points. Compared as the set of undirected
  edges.
- padstack index: the .hyp numbers footprint pad stacks first (0, 1) and the
  via stack after them (2); the routed model has no footprint pad stacks. Each
  via is compared by the pad stack it resolves to, not by its index.
- pins: the .hyp names the wire-bond carriers by the board's references (J1,
  ...), the routed model by the io pad manifest's (J_U1_ext[0], ...). Die pins
  are compared by reference; carrier pins by count. Pins draw nothing.
- gds_file: the .hyp's paths are the container's; each is resolved against
  the staged document's folder and compared by sha256.
"""

import hashlib
import math
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("klayout.db")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import hyp_to_gds as _h  # noqa: E402

from .routed_t03_stage import BOARD_HYP, DIES, IO_PADS, stage  # noqa: E402

M_TO_NM = 1e9


def _nm(value):
    """A coordinate in metres as the layout stores it: whole nanometres,
    rounded half up as KLayout rounds."""
    return math.floor(value * M_TO_NM + 0.5)


def _close(a, b):
    return abs(a - b) * M_TO_NM <= 1.0 and _nm(a) == _nm(b)


def board_model():
    model = _h.HYPParser(str(BOARD_HYP))
    model.parse()
    return model


def routed_model(tmp_path, edit=None):
    model = _h.RoutedGeometryParser(str(stage(tmp_path, edit)), str(IO_PADS))
    model.parse()
    return model


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _edges(model):
    return sorted(tuple(sorted(((_nm(s.x1), _nm(s.y1)), (_nm(s.x2), _nm(s.y2)))))
                  for s in model.perimeter_segments)


def _stack(model, via):
    ps = model.padstacks[via.padstack_index]
    return (_nm(ps.drill), tuple(ps.layers), _nm(ps.pad_width), _nm(ps.pad_height))


def first_difference(board, routed, document_dir):
    """None, or a line naming the first record where the models differ."""
    if board.units != routed.units:
        return "units: %s against %s" % (board.units, routed.units)
    if board.stackup_layers != routed.stackup_layers:
        return "stackup: %s against %s" % (board.stackup_layers, routed.stackup_layers)
    if _edges(board) != _edges(routed):
        return "perimeter: %s against %s" % (_edges(board), _edges(routed))
    if board.arcs or routed.arcs:
        return "arcs: %d against %d" % (len(board.arcs), len(routed.arcs))
    if len(board.devices) != len(routed.devices):
        return "devices: %d against %d" % (len(board.devices), len(routed.devices))
    board_dir = os.path.dirname(board.board_path)
    for i, (b, r) in enumerate(zip(board.devices, routed.devices)):
        text = os.path.relpath(b.gds_file, board_dir)
        layout = os.path.join(document_dir, text)
        if (b.ref, b.layer, b.rotation) != (r.ref, r.layer, r.rotation) \
                or not (_close(b.x, r.x) and _close(b.y, r.y)) \
                or _sha256(layout) != _sha256(r.gds_file):
            return "device %d: %s against %s" % (i, b, r)
    for name in ("segments", "vias"):
        bs, rs = getattr(board, name), getattr(routed, name)
        if len(bs) != len(rs):
            return "%s: %d against %d" % (name, len(bs), len(rs))
    for i, (b, r) in enumerate(zip(board.segments, routed.segments)):
        if (b.net_name, b.layer) != (r.net_name, r.layer) or not all(
                _close(getattr(b, k), getattr(r, k))
                for k in ("x1", "y1", "x2", "y2", "width")):
            return "segment %d: %s against %s" % (i, b, r)
    for i, (b, r) in enumerate(zip(board.vias, routed.vias)):
        if b.net_name != r.net_name or not (_close(b.x, r.x) and _close(b.y, r.y)) \
                or _stack(board, b) != _stack(routed, r):
            return "via %d: %s %s against %s %s" % (
                i, b, _stack(board, b), r, _stack(routed, r))
    dies = set(DIES)
    die_pins = [sorted(p.ref for p in m.pins if p.ref.split(".", 1)[0] in dies)
                for m in (board, routed)]
    if die_pins[0] != die_pins[1]:
        return "die pins: %s against %s" % tuple(die_pins)
    carriers = [sum(1 for p in m.pins if p.ref.split(".", 1)[0] not in dies)
                for m in (board, routed)]
    if carriers[0] != carriers[1]:
        return "carrier pins: %d against %d" % tuple(carriers)
    return None


def test_the_oracle_has_the_golden_counts():
    board = board_model()
    assert len({s.net_name for s in board.segments}) == 10
    assert len(board.segments) == 54
    assert len(board.vias) == 22
    assert len(board.pins) == 22
    assert len(board.devices) == 2
    assert board.perimeter_segments


def _names_sort_against_board_names(document):
    """The net whose board name sorts last gets a name that sorts first, so
    the two orders differ: a model ordered by name no longer matches."""
    last = max(document["nets"], key=lambda net: net["board_name"])
    last["name"] = "!" + last["name"]


@pytest.mark.parametrize("variant", [None, _names_sort_against_board_names],
                         ids=["as-written", "names-sort-otherwise"])
def test_the_routed_model_is_the_board_paths(tmp_path, variant):
    routed = routed_model(tmp_path, variant)
    document_dir = os.path.dirname(routed.hyp_path)
    assert first_difference(board_model(), routed, document_dir) is None


def test_the_comparison_sees_a_one_nanometre_move(tmp_path):
    """The equality above is not vacuous: one segment end moved by 1 nm on
    the document side is a difference, and it is named."""
    def move(document):
        document["nets"][0]["segments"][0]["x1"] += 0.001
    routed = routed_model(tmp_path, move)
    diff = first_difference(board_model(), routed, os.path.dirname(routed.hyp_path))
    assert diff is not None and diff.startswith("segment ")
