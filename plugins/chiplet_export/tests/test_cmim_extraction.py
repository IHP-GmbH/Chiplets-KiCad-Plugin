# SPDX-License-Identifier: GPL-3.0-or-later
"""Board-side cap_cmim extraction (needs pcbnew).

The worker-side half of this feature -- sidecar parsing and IntM4TM2 PCell
placement -- lives in test_cmim_devices.py and runs without pcbnew. Split so a
host without the KiCad bindings still exercises the unit-parsing contract that
the two halves share.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

pcbnew = pytest.importorskip("pcbnew")

from hyp_to_gds import _cmim_length_um, load_cmim_devices  # noqa: E402

from writers.chiplet_writer import (  # noqa: E402
    CMIM_SIDECAR_VERSION,
    is_cmim,
    parse_length_um,
    write_cmim_devices_json,
)


def _board_with_cmim(fields, x_mm=1.0, y_mm=2.0, ref="C1"):
    board = pcbnew.BOARD()
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm)))
    for name, value in fields.items():
        fp.SetField(name, value)
    board.Add(fp)
    return board


def test_board_parse_length_um_matches_the_worker_side():
    for text in ("57.68um", "57.68u", "5.768e-5"):
        assert parse_length_um(text) == pytest.approx(57.68)
    assert parse_length_um("nope") is None
    assert parse_length_um("") is None


def test_is_cmim_accepts_both_the_footprint_and_symbol_spelling():
    assert is_cmim(_board_with_cmim({"Model": "cap_cmim"}).Footprints()[0])
    assert is_cmim(_board_with_cmim({"Sim.Name": "cap_cmim"}).Footprints()[0])
    assert not is_cmim(_board_with_cmim({"Model": "cap_rfcmim"})
                       .Footprints()[0])
    assert not is_cmim(_board_with_cmim({}).Footprints()[0])


@pytest.mark.parametrize("w_text", ["57.68um", "57.68u", "5.768e-5"])
def test_extraction_normalises_every_convention_to_micrometres(tmp_path,
                                                               w_text):
    board = _board_with_cmim({"Model": "cap_cmim", "w": w_text, "l": w_text})
    out = tmp_path / "cmim.json"

    assert write_cmim_devices_json(board, str(out)) == 1

    data = json.loads(out.read_text())
    assert data["version"] == CMIM_SIDECAR_VERSION
    device = data["cmim_devices"][0]
    assert device["ref"] == "C1"
    assert device["w_um"] == pytest.approx(57.68)
    assert device["l_um"] == pytest.approx(57.68)
    assert device["m"] == 1
    # Same frame as write_io_pads_json: micrometers, Y negated.
    assert device["x_um"] == pytest.approx(1000.0)
    assert device["y_um"] == pytest.approx(-2000.0)


@pytest.mark.parametrize("fields", [
    {"Model": "cap_cmim", "l": "57.68um"},                    # missing w
    {"Model": "cap_cmim", "w": "wide", "l": "57.68um"},       # unparseable
    {"Model": "cap_cmim", "w": "-1um", "l": "57.68um"},       # non-positive
    {"Model": "cap_cmim", "w": "57.68um", "l": "57.68um", "m": "0"},
], ids=["missing", "unparseable", "negative", "zero_multiplier"])
def test_unusable_footprints_are_skipped_and_write_no_file(tmp_path, fields):
    out = tmp_path / "cmim.json"

    assert write_cmim_devices_json(_board_with_cmim(fields), str(out)) == 0
    assert not out.exists()


def test_a_footprint_that_cannot_be_described_is_reported_to_the_caller(
        tmp_path):
    """The worker's "requested but not placed" guard cannot see this one.

    A device dropped during extraction never reaches the sidecar, so unless
    the refs come back here the export would exit 0 with the capacitor
    missing from every artifact.
    """
    # The micro sign is the realistic way to get here: it looks right in the
    # field editor and is not the "um"/"u" the two libraries actually write.
    board = _board_with_cmim({"Model": "cap_cmim", "w": "8.11 µm",
                              "l": "8.11um"})
    skipped = []

    assert write_cmim_devices_json(board, str(tmp_path / "cmim.json"),
                                   skipped=skipped) == 0
    assert skipped == ["C1"]


def test_orientation_is_recorded_for_the_gds_frame(tmp_path):
    board = _board_with_cmim({"Model": "cap_cmim", "w": "20um", "l": "5um"})
    board.Footprints()[0].SetOrientationDegrees(90.0)
    out = tmp_path / "cmim.json"

    assert write_cmim_devices_json(board, str(out)) == 1

    # Y is negated for the GDS frame, so the rotation sense flips with it: a
    # rectangular part placed unrotated would be drawn 90 degrees off.
    device = json.loads(out.read_text())["cmim_devices"][0]
    assert device["rotation_deg"] == pytest.approx(270.0)


def test_non_cmim_footprints_write_no_sidecar(tmp_path):
    out = tmp_path / "cmim.json"
    board = _board_with_cmim({"Model": "cap_rfcmim", "w": "1um", "l": "1um"})

    assert write_cmim_devices_json(board, str(out)) == 0
    assert not out.exists()


def test_extraction_round_trips_into_the_placement_frame(tmp_path):
    """The exporter's own output must be readable by the worker unchanged."""
    board = _board_with_cmim(
        {"Model": "cap_cmim", "w": "57.68um", "l": "57.68um"})
    out = tmp_path / "cmim.json"
    write_cmim_devices_json(board, str(out))

    devices = load_cmim_devices(str(out))

    assert len(devices) == 1
    assert _cmim_length_um(devices[0], "w") == pytest.approx(57.68)
    assert _cmim_length_um(devices[0], "l") == pytest.approx(57.68)
