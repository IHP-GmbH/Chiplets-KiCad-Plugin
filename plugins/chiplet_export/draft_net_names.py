# SPDX-License-Identifier: GPL-3.0-or-later
"""The export report the draft's io_pad nets are named by.

A board interposer-pnr ``export --form`` drew against the designer's sheet
carries the sheet's net names; the draft ``.chiplet`` and the io_pads sidecar
name nets as the planner does. The report that export wrote beside the board
(``--export-report``) states the way back. Reading its ``round_trip``, matching
it to the board and naming each net are interposer-pnr's own rules, vendored in
``vendor/round_trip_names.py`` (gds_to_kicad vendors it too, at its own pin); the
writers apply them in ``chiplet_writer.carrier_identities``. HYP and the board
snapshot keep the board's names.
"""
import json

if __package__:
    from .vendor.round_trip_names import (
        BoardCarrier, CarrierIdError, RoundTripNamesError, planner_net,
        read_board_carriers as _read_board_carriers,
        read_export_report as _read_report,
    )
else:
    from vendor.round_trip_names import (
        BoardCarrier, CarrierIdError, RoundTripNamesError, planner_net,
        read_board_carriers as _read_board_carriers,
        read_export_report as _read_report,
    )


class BoardReportError(ValueError):
    """The export report is missing or does not describe this board's names."""


def read_board_carriers(carriers, report):
    """Each carrier's io_pad id and the names its nets go back to the
    planner's by (a ``BoardCarriers``); ``carriers`` are ``BoardCarrier``s.
    A refusal of the report, or of its absence, is a BoardReportError; one of
    a carrier's id stays a CarrierIdError, a fault of the board."""
    try:
        return _read_board_carriers(carriers, report)
    except CarrierIdError:
        raise
    except RoundTripNamesError as exc:
        raise BoardReportError(str(exc)) from None


def read_export_report(path):
    """The net names of the export report at ``path`` (an
    ``ExportReport``), None when no path is given. A file that cannot be read,
    or is not an export report the shared reader takes, raises ValueError
    naming ``path``."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as source:
            return _read_report(json.load(source), str(path))
    except (OSError, ValueError) as exc:
        raise ValueError("export report %s: %s" % (path, exc)) from None
