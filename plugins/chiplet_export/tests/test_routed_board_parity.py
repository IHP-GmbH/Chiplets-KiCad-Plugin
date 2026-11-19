# SPDX-License-Identifier: GPL-3.0-or-later
"""The routed facts against the board's, with real pcbnew (the KiCad image).

read_board_facts(board) and facts_from_documents(documents) must give the same
ChipletFacts for the fixture's draft board, positions within 1 nm. A variant
board turns U2 to 270 degrees and moves it by 0.3335 um, so the rotation
KiCad normalises and the nanometer rounding are both exercised. The committed
oracle is recomputed from the board path as it is now, so a board-path change
cannot leave it stale.
"""

import copy
import dataclasses
import importlib.util
import sys
from pathlib import Path

import pytest

pcbnew = pytest.importorskip("pcbnew", reason="needs pcbnew (KiCad Python)")

PLUGINS = Path(__file__).resolve().parents[2]
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))

from chiplet_export.draft_net_names import read_export_report  # noqa: E402
from chiplet_export.routed import facts_from_documents  # noqa: E402
from chiplet_export.writers.chiplet_writer import read_board_facts  # noqa: E402

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "routed_t03_host_tests", HERE / "test_routed_intermediates.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)

oracle = host.oracle
INPUTS = host.INPUTS

#: Positions may differ by this many internal units (1 nm).
POSITION_TOL_IU = 1


def board_facts(board_path):
    board = pcbnew.LoadBoard(str(board_path))
    report = read_export_report(str(INPUTS / "export.json"))
    return read_board_facts(board, host.manifest_snapshot(), report)


def routed_facts(document=None):
    doc, chiplet, io_pads = host.documents()
    return facts_from_documents(document or doc, chiplet, io_pads,
                                board_name=oracle.BOARD_NAME,
                                interconnect_manifest=host.manifest_snapshot())


def assert_same_facts(board, routed):
    """Every field exact, except x_iu and y_iu within POSITION_TOL_IU."""
    def split(record):
        fields = dataclasses.asdict(record)
        return ({k: v for k, v in fields.items() if k not in ("x_iu", "y_iu")},
                (fields["x_iu"], fields["y_iu"]))

    top = lambda f: {k: v for k, v in dataclasses.asdict(f).items()  # noqa: E731
                     if k not in ("carriers", "dies")}
    assert top(board) == top(routed)
    for kind in ("dies", "carriers"):
        b_list, r_list = getattr(board, kind), getattr(routed, kind)
        assert len(b_list) == len(r_list), kind
        for b, r in zip(b_list, r_list):
            (b_rest, b_xy), (r_rest, r_xy) = split(b), split(r)
            assert b_rest == r_rest, kind
            assert all(abs(p - q) <= POSITION_TOL_IU for p, q in zip(b_xy, r_xy)), (
                kind, b_rest.get("ref") or b_rest.get("ident"), b_xy, r_xy)


def test_the_committed_oracle_is_the_board_path_now():
    host.assert_count_floor()
    files = oracle.compute(interconnect_root="")
    committed = {name: host.oracle_bytes(name) for name in files}
    for name, data in files.items():
        host.assert_same_bytes(name, data, committed[name])


def test_board_and_routed_facts_agree():
    """J2 on the golden draft board."""
    host.assert_count_floor()
    assert_same_facts(board_facts(oracle.BOARD), routed_facts())


def test_a_turned_and_shifted_die_agrees(tmp_path):
    """J2 on a variant: U2 at 270 degrees, moved +0.3335 um in x."""
    shift_um = 0.3335
    board = pcbnew.LoadBoard(str(oracle.BOARD))
    u2 = board.FindFootprintByReference("U2")
    pos = u2.GetPosition()
    u2.SetPosition(pcbnew.VECTOR2I(pos.x + pcbnew.FromMM(shift_um / 1000.0), pos.y))
    u2.SetOrientationDegrees(270.0)
    variant = tmp_path / (oracle.BOARD_NAME + ".kicad_pcb")
    assert pcbnew.SaveBoard(str(variant), board)
    # The variant must actually differ where it claims to.
    reloaded = board_facts(variant)
    die = {d.ref: d for d in reloaded.dies}["U2"]
    assert die.rotation_deg == -90.0

    doc, _chiplet, _io_pads = host.documents()
    doc = copy.deepcopy(doc)
    device = {d["ref"]: d for d in doc["devices"]}["U2"]
    device["origin"][0] += shift_um
    device["rotation_deg"] = 270.0
    # The variant board keeps the golden board's name, so both name it alike.
    assert_same_facts(reloaded, routed_facts(doc))
