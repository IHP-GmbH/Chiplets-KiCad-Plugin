# SPDX-License-Identifier: GPL-3.0-or-later
"""The export report read from a file; no KiCad needed.

The rules the report is read and matched to a board by are interposer-pnr's
and tested there; here, that the plugin reads a real report with them and
names the file in every refusal.
"""
import json
import re
from pathlib import Path

import pytest

from chiplet_export.draft_net_names import read_export_report

REAL_REPORT = Path(__file__).resolve().parent / "fixtures" / "sheet_net_names" / "export.json"


def write(tmp_path, report):
    path = tmp_path / "export.json"
    path.write_text(json.dumps(report))
    return path


def test_a_real_export_report_maps_sheet_names_back_to_the_plan():
    names = read_export_report(REAL_REPORT).names
    receipt = json.loads(REAL_REPORT.read_text())["round_trip"]
    assert names.sheet_to_planner == {sheet: planner
                                      for planner, sheet in receipt["net_names"].items()}
    assert names.sheet_only == frozenset(receipt["sheet_nets"])


def test_no_path_is_no_report_and_a_legacy_report_has_no_names(tmp_path):
    assert read_export_report("") is None
    assert read_export_report(None) is None
    report = {"format": "interposer_pnr.export", "version": 1, "round_trip": {"version": 2}}
    assert read_export_report(write(tmp_path, report)).names is None


RESERVED = {"vdd": {"owner": "pdn", "copper_sha256": "0" * 64, "arrays": {"a0": {
    "cuts": 1, "tiles": [{"at_mm": [1.0, -1.0], "size_mm": 0.02, "drill_mm": 0.01,
                          "layers": ["F.Cu", "In1.Cu"]}]}}}}


def with_round_trip(**changes):
    report = json.loads(REAL_REPORT.read_text())
    report["round_trip"].update(changes)
    return report


def test_a_version_5_report_names_as_version_3_and_a_version_4_as_none(tmp_path):
    # 5 and 4 are 3 and 2 with reserved copper, which names no net.
    names = read_export_report(REAL_REPORT).names
    assert read_export_report(write(tmp_path, with_round_trip(
        version=5, reserved=RESERVED))).names == names
    report = {"format": "interposer_pnr.export", "version": 1,
              "round_trip": {"version": 4, "reserved": RESERVED}}
    assert read_export_report(write(tmp_path, report)).names is None


@pytest.mark.parametrize("changes, match", [
    ({"version": 5}, "round_trip.reserved must be an object"),
    ({"reserved": RESERVED}, "version 3 states no reserved copper"),
    ({"version": 7}, "unsupported round_trip version 7; this reader predates it"),
    ({"version": 6}, "round_trip version 6 must list its features"),
    ({"format": "interposer_pnr.export", "version": 1, "round_trip": {"version": 4}},
     "round_trip.reserved must be an object"),
])
def test_a_report_the_vendored_reader_refuses_is_refused(tmp_path, changes, match):
    path = write(tmp_path, changes if "round_trip" in changes else with_round_trip(**changes))
    with pytest.raises(ValueError, match="export report %s: .*%s" % (re.escape(str(path)), match)):
        read_export_report(path)


@pytest.mark.parametrize("text, match", [
    (None, "No such file"),
    ("not json", "Expecting value"),
    ("[]", "not an interposer_pnr.export report, version 1"),
    (json.dumps({"format": "interposer_pnr.export", "version": 1, "round_trip": None}),
     "round_trip must be an object"),
])
def test_every_refusal_names_the_file(tmp_path, text, match):
    path = tmp_path / "export.json"
    if text is not None:
        path.write_text(text)
    with pytest.raises(ValueError, match="export report %s: .*%s" % (re.escape(str(path)), match)):
        read_export_report(path)
