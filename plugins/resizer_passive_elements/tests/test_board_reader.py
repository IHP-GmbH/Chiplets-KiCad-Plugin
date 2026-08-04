# SPDX-License-Identifier: GPL-3.0-or-later
"""Board reading: field parsing and device dispatch.

No pcbnew: the stand-ins below are the ones ARCHITECTURE.md section 8
documents, and they are enough because board_reader.py never imports pcbnew.

The unit handling is the part worth pinning. Two conventions for `w`/`l`
genuinely occur on a board and are only told apart by the suffix, so reading
one as the other is a silent factor-of-1e6 error in both directions:
`8.11um` from the PDK's footprint generator, `8.11e-6` (metres) from the
symbol library. The sibling chiplet_export plugin reads the same fields and
must agree; see its writers/chiplet_writer.parse_length_um.
"""
import pytest

from resizer_passive_elements import board_reader


class FakeFootprint:
    def __init__(self, fields, ref="C1"):
        self._fields = dict(fields)
        self._ref = ref

    def HasField(self, name):
        return name in self._fields

    def GetFieldText(self, name):
        return self._fields[name]

    def GetReference(self):
        return self._ref


class FakeBoard:
    def __init__(self, footprints):
        self._footprints = list(footprints)

    def Footprints(self):
        return self._footprints


class LegacyFakeBoard(FakeBoard):
    """A pcbnew build exposing GetFootprints() instead of Footprints()."""

    def Footprints(self):
        raise AttributeError("Footprints")

    def GetFootprints(self):
        return self._footprints


@pytest.mark.parametrize("text,expected", [
    ("8.11um", 8.11),          # cmim_footprint_gen.py's footprint property
    ("8.11u", 8.11),           # short suffix
    (" 57.68UM ", 57.68),      # case and padding
    ("8.11e-6", 8.11),         # symbol library: metres
    ("5.768e-5", 57.68),
    ("0", 0.0),
])
def test_parse_um_accepts_both_board_conventions(text, expected):
    assert board_reader._parse_um(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", [None, "", "   ", "wide", "8,11um"])
def test_parse_um_rejects_junk(text):
    assert board_reader._parse_um(text) is None


@pytest.mark.parametrize("text,expected", [
    ("100.00fF", 100.0),
    ("250f", 250.0),
    ("1pf", 1000.0),
    ("1.5p", 1500.0),
    ("250", 250.0),
])
def test_parse_capacitance_reads_every_suffix(text, expected):
    assert board_reader._parse_capacitance_fF(text) == pytest.approx(expected)


def test_parse_capacitance_tries_long_suffixes_first():
    # "1pf" must not be read as "1p" followed by a stray "f".
    assert board_reader._parse_capacitance_fF("1pf") == 1000.0
    assert board_reader._parse_capacitance_fF("nope") is None
    assert board_reader._parse_capacitance_fF(None) is None


def test_reads_a_well_formed_cap_cmim():
    fp = FakeFootprint({"Model": "cap_cmim", "w": "8.11e-6", "l": "8.11e-6",
                        "Capacitance": "100 fF"})
    logged = []

    params = board_reader._read_cap_cmim_fields(fp, logged.append)

    assert params["reference"] == "C1"
    assert params["model"] == "cap_cmim"
    assert params["w_um"] == pytest.approx(8.11)
    assert params["l_um"] == pytest.approx(8.11)
    assert params["footprint_obj"] is fp
    assert logged == []


@pytest.mark.parametrize("fields,expect_none", [
    ({"Model": "cap_cmim", "l": "8.11um"}, "w_um"),
    ({"Model": "cap_cmim", "w": "wide", "l": "8.11um"}, "w_um"),
    ({"Model": "cap_cmim", "w": "8.11um"}, "l_um"),
])
def test_bad_fields_are_reported_never_dropped_silently(fields, expect_none):
    logged = []

    params = board_reader._read_cap_cmim_fields(FakeFootprint(fields),
                                                logged.append)

    assert params[expect_none] is None
    assert logged, "a registered device with a bad field must warn"


def test_find_supported_footprints_dispatches_on_model():
    known = FakeFootprint({"Model": "cap_cmim", "w": "8.11um", "l": "8.11um"},
                          ref="C1")
    unknown = FakeFootprint({"Model": "res_rsil", "w": "1um"}, ref="R1")
    plain = FakeFootprint({}, ref="U1")

    found = board_reader.find_supported_footprints(
        FakeBoard([known, unknown, plain]))

    # An unregistered Model is not this plugin's business and is skipped
    # without noise; only registered devices come back.
    assert [p["reference"] for p in found] == ["C1"]


def test_find_supported_footprints_falls_back_to_getfootprints():
    fp = FakeFootprint({"Model": "cap_cmim", "w": "8.11um", "l": "8.11um"})

    found = board_reader.find_supported_footprints(LegacyFakeBoard([fp]))

    assert [p["reference"] for p in found] == ["C1"]


def test_cap_cmim_is_the_registered_device():
    assert "cap_cmim" in board_reader.DEVICE_READERS
