# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The planner names of a board's nets, from interposer-pnr's export report.

interposer-pnr ``export --form`` writes a board that follows the designer's
sheet: its pads and copper carry the sheet's net names, not the planner's. The
report it writes beside the board holds a ``round_trip`` object saying how to
go back. From ``round_trip`` version 3:

- ``net_names`` maps each routed planner net to the sheet net the board names
  it (planner to sheet, one to one);
- ``sheet_nets`` maps each sheet net on a pad no routed net binds to that pad,
  ``[component, pad]``; such a net has no planner name.

Versions 1 and 2 describe a board that follows no sheet: its net names are the
planner's already, and their ``net_names`` is not read here.

Versions 4 and 5 are versions 2 and 3 for a board that carries reserved copper,
drawn by another tool and never edited at the gate: their ``reserved`` object
states it, per board net, as :func:`read_round_trip_reserved` reads it. A
reader of names only reads version 5 as version 3 and 4 as 2.

Version 6 names what it states instead of numbering it: its ``features`` list
holds ``pad_domains`` and ``widths`` always, and ``sheet`` and ``reserved`` when
the board follows a sheet or carries reserved copper. A block is present
exactly when its feature is listed. ``widths`` states the width each net with
a requirement was routed to, as :func:`read_round_trip_widths` reads it.

The rule that keeps version 6 open: a feature may only add a block. It never
changes how existing names are read and never changes an existing block,
``footprints`` included. A feature that renames nets, or changes what a
carrier or footprint binding means, is version 7, never a feature.

So this file holds the whole version table, not only the names, and every
reader asks :func:`round_trip_states` what a record states. That function
returns the features it does not know in ``unknown`` and never refuses them;
refusing is the caller's call. interposer-pnr's gate refuses them. The name
readers (:func:`read_export_report`, :func:`read_board_carriers`, :func:`planner_net`)
never do, since by the rule above no feature changes what they read.

A reader of a board net goes back to the planner's name in three steps:
:func:`read_export_report` on the parsed report (when one is given),
:func:`read_board_carriers` to match it to the board's carriers, :func:`planner_net`
per net. The caller reads each carrier off its board as a :class:`BoardCarrier`.

This file is the source of copies vendored byte-identical by readers outside
interposer-pnr (each beside a ``round_trip_names.SOURCE`` naming the commit). It
imports only the standard library, so a copy runs anywhere Python does.
"""

import math
import re
from types import MappingProxyType
from typing import Dict, FrozenSet, Mapping, NamedTuple, Optional, Sequence, Tuple

#: The ``format`` of the report ``export`` writes, at version 1.
EXPORT_REPORT_FORMAT = "interposer_pnr.export"

#: What each numbered ``round_trip`` version states: (pad domains, a sheet
#: the board follows, reserved copper). Version 1, the first producer's,
#: states none. None states widths.
_STATES = {1: (False, False, False),
           2: (True, False, False),
           3: (True, True, False),
           4: (True, False, True),
           5: (True, True, True)}

#: The version that names its features (``features``) instead.
FEATURES_VERSION = 6

#: Every ``round_trip`` version this file reads.
ROUND_TRIP_VERSIONS = tuple(_STATES) + (FEATURES_VERSION,)

#: The features this file reads at version 6, and those every version 6
#: record lists (export writes both on every board).
FEATURES = ("pad_domains", "sheet", "reserved", "widths")
REQUIRED_FEATURES = ("pad_domains", "widths")

_SHA256 = re.compile(r"[0-9a-f]{64}")
_NET_KEYS = frozenset({"owner", "copper_sha256", "arrays"})
_ARRAY_KEYS = frozenset({"cuts", "tiles"})
_TILE_KEYS = frozenset({"at_mm", "size_mm", "drill_mm", "layers"})
#: How a ``widths`` entry is judged: each segment against its layer's width,
#: or (reserved copper) the summed width across a cut, as route measured it.
RULE_PER_SEGMENT = "per_segment"
RULE_CROSS_SECTION = "cross_section"
_WIDTH_KEYS = {RULE_PER_SEGMENT: frozenset({"rule", "requirement", "required_um", "pin_layer",
                                            "taper_boxes_um"}),
               RULE_CROSS_SECTION: frozenset({"rule", "requirement", "directions"})}
_REQUIREMENT_KEYS = frozenset({"width_um", "stated_um"})
_AMPS_KEYS = frozenset({"amps", "current_density_ma_per_um"})
_DIRECTIONS = ("horizontal", "vertical")
_DIRECTION_KEYS = frozenset({"required_um", "measured_um", "at_um"})


class RoundTripNamesError(ValueError):
    """A ``round_trip`` object that cannot name a board net unambiguously."""


class CarrierIdError(RoundTripNamesError):
    """A board carrier whose io_pad id cannot be read: a fault of the board,
    never of the report given with it."""


class RoundTripNames(NamedTuple):
    """A sheet board's net names, read back to the planner's."""
    #: The inverse of ``net_names``: each routed net's board name to its
    #: planner net.
    sheet_to_planner: Dict[str, str]
    #: The board names of nets on pads no routed net binds (``sheet_nets``).
    sheet_only: FrozenSet[str]


class RoundTripStates(NamedTuple):
    """What a ``round_trip`` record states, from :func:`round_trip_states`."""
    version: int
    pad_domains: bool
    sheet: bool
    reserved: bool
    widths: bool
    #: Version 6 features this file does not read, for the caller to judge.
    unknown: FrozenSet[str] = frozenset()


def round_trip_version(record) -> int:
    """The version of ``round_trip`` object ``record``, one of
    :data:`ROUND_TRIP_VERSIONS`.

    A missing version is 1, the version the first producer wrote without
    stating it. A ``record`` that is not an object (JSON null included) is
    refused: a caller must not read a null ``round_trip`` as version 1, since
    it says nothing about which names the board carries.
    """
    if not isinstance(record, dict):
        raise RoundTripNamesError("round_trip must be an object")
    version = record.get("version", 1)
    if type(version) is not int or version not in ROUND_TRIP_VERSIONS:
        newer = type(version) is int and version > max(ROUND_TRIP_VERSIONS)
        raise RoundTripNamesError(
            f"unsupported round_trip version {version!r}; "
            + ("this reader predates it, update the reader" if newer
               else "re-export with a supported producer"))
    return version


def round_trip_states(record) -> RoundTripStates:
    """What round_trip ``record`` states: from the version table for versions
    1 to 5, from its ``features`` list at version 6.

    A version 6 ``features`` must be a list of distinct nonempty strings that
    holds every one of :data:`REQUIRED_FEATURES`; a ``features`` key at any
    other version is refused. Features this file does not read come back in
    ``unknown``, never refused here (see the module docstring).
    """
    version = round_trip_version(record)
    if version != FEATURES_VERSION:
        if "features" in record:
            raise RoundTripNamesError(
                f"round_trip version {version} states no features; "
                f"only version {FEATURES_VERSION} names them")
        return RoundTripStates(version, *_STATES[version], widths=False)
    features = record.get("features")
    if not isinstance(features, list):
        raise RoundTripNamesError(
            f"round_trip version {FEATURES_VERSION} must list its features")
    for feature in features:
        _text(feature, "round_trip.features entry")
    if len(set(features)) != len(features):
        raise RoundTripNamesError("round_trip.features names a feature twice")
    missing = [f for f in REQUIRED_FEATURES if f not in features]
    if missing:
        raise RoundTripNamesError(
            f"round_trip version {FEATURES_VERSION} must list {', '.join(missing)}; "
            "export states it on every board")
    return RoundTripStates(version, *(f in features for f in FEATURES),
                           unknown=frozenset(features) - set(FEATURES))


def read_sheet_nets(record, states: RoundTripStates) -> Dict[str, list]:
    """``record``'s ``sheet_nets``, checked: required when the board follows a
    sheet (``states.sheet``), refused otherwise, when the result is empty."""
    if not states.sheet:
        if "sheet_nets" in record:
            raise RoundTripNamesError(
                f"round_trip version {states.version} states no sheet_nets; "
                "only a board that follows a sheet has them")
        return {}
    sheet_nets = _object(record.get("sheet_nets"), "round_trip.sheet_nets")
    for net, pad in sheet_nets.items():
        _text(net, "round_trip.sheet_nets net")
        if not isinstance(pad, list) or len(pad) != 2:
            raise RoundTripNamesError(f"round_trip.sheet_nets.{net} must be [component, pad]")
        for value in pad:
            _text(value, f"round_trip.sheet_nets.{net}")
    return sheet_nets


def read_round_trip_names(record) -> Optional[RoundTripNames]:
    """The planner names of the nets of the board ``record`` describes.

    None for a board that follows no sheet: the board's names are the
    planner's, pass them through. For a board that follows one both maps are
    required (either may be empty), a sheet net named by two planner nets is
    refused, and so is a sheet net both routed and in ``sheet_nets``. A board
    net in neither map has no planner name; what to do with it is the caller's
    call. ``reserved`` and ``widths`` are read too, though they name nothing
    here, so every reader refuses the same records; a feature this file does
    not read is not. ``record`` is not changed.
    """
    states = round_trip_states(record)
    sheet_nets = read_sheet_nets(record, states)
    read_round_trip_reserved(record, states)
    read_round_trip_widths(record, states)
    if not states.sheet:
        return None
    sheet_to_planner = {}
    for planner, sheet in _object(record.get("net_names"), "round_trip.net_names").items():
        _text(planner, "round_trip.net_names planner net")
        _text(sheet, f"round_trip.net_names.{planner}")
        if sheet in sheet_to_planner:
            raise RoundTripNamesError(
                f"round_trip.net_names maps both {sheet_to_planner[sheet]!r} and {planner!r} "
                f"to sheet net {sheet!r}")
        sheet_to_planner[sheet] = planner
    for net in sheet_nets:
        if net in sheet_to_planner:
            raise RoundTripNamesError(
                f"sheet net {net!r} is both a routed net's board name and a sheet-only net")
    return RoundTripNames(sheet_to_planner, frozenset(sheet_nets))


def read_round_trip_reserved(record, states: RoundTripStates) -> Dict[str, dict]:
    """``record``'s ``reserved``, checked: required, and not empty, when the
    board carries reserved copper (``states.reserved``), refused otherwise,
    when the result is empty.

    Each board net of reserved copper maps to its ``owner`` (the tool that
    drew it), ``copper_sha256`` (the digest of all of the net's copper on the
    board) and ``arrays``: each cut array id to its ``cuts`` and the ``tiles``
    the board draws it as, each a via ``at_mm`` with its ``size_mm``,
    ``drill_mm`` and copper ``layers`` (top, bottom). The block is closed: a
    key it does not name, at any of the three levels, is refused. ``record``
    is not changed.
    """
    if not states.reserved:
        if "reserved" in record:
            raise RoundTripNamesError(
                f"round_trip version {states.version} states no reserved copper; "
                "only a board that carries it has it")
        return {}
    reserved = _object(record.get("reserved"), "round_trip.reserved")
    if not reserved:
        raise RoundTripNamesError(f"round_trip version {states.version} states no reserved net")
    for net, entry in reserved.items():
        where = f"round_trip.reserved.{_text(net, 'round_trip.reserved net')}"
        _keys(_object(entry, where), _NET_KEYS, where)
        _text(entry.get("owner"), f"{where}.owner")
        digest = entry.get("copper_sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise RoundTripNamesError(f"{where}.copper_sha256 must be a sha256 hex digest")
        for array, cuts in _object(entry.get("arrays"), f"{where}.arrays").items():
            at = f"{where}.arrays.{_text(array, f'{where}.arrays id')}"
            _keys(_object(cuts, at), _ARRAY_KEYS, at)
            if type(cuts.get("cuts")) is not int or cuts["cuts"] < 1:
                raise RoundTripNamesError(f"{at}.cuts must be a positive integer")
            tiles = cuts.get("tiles")
            if not isinstance(tiles, list) or not tiles:
                raise RoundTripNamesError(f"{at}.tiles must be a nonempty list")
            for tile in tiles:
                _keys(_object(tile, f"{at}.tiles"), _TILE_KEYS, f"{at}.tiles")
                point = tile.get("at_mm")
                if not isinstance(point, list) or len(point) != 2:
                    raise RoundTripNamesError(f"{at}.tiles at_mm must be [x, y]")
                for value in point:
                    _number(value, f"{at}.tiles at_mm")
                for key in ("size_mm", "drill_mm"):
                    if _number(tile.get(key), f"{at}.tiles {key}") <= 0:
                        raise RoundTripNamesError(f"{at}.tiles {key} must be positive")
                layers = tile.get("layers")
                if not isinstance(layers, list) or len(layers) != 2:
                    raise RoundTripNamesError(f"{at}.tiles layers must be [top, bottom]")
                for layer in layers:
                    _text(layer, f"{at}.tiles layers")
    return reserved


def read_round_trip_widths(record, states: RoundTripStates) -> Dict[str, dict]:
    """``record``'s ``widths``, checked: required when the record states widths
    (``states.widths``; it may be empty, for a route with no requirement),
    refused otherwise, when the result is empty.

    Each board net with a requirement maps to an entry whose ``rule`` says how
    it is judged. Both carry the ``requirement``: the routed ``width_um`` and
    the ``stated_um`` it was rounded up from, plus ``amps`` and
    ``current_density_ma_per_um`` together when the width is sized from
    current.

    - ``per_segment`` (routed copper): ``required_um``, the width each layer
      was routed to; ``pin_layer``; and ``taper_boxes_um``, the boxes
      ``[x0, y0, x1, y1]`` in which copper on the pin layer may be narrower.
    - ``cross_section`` (reserved copper): ``directions``, per stripe
      direction, the ``required_um``, the ``measured_um`` route found and the
      cut ``at_um`` it found it at.

    The block is closed: a key it does not name, at any level, is refused.
    ``record`` is not changed.
    """
    if not states.widths:
        if "widths" in record:
            raise RoundTripNamesError(
                f"round_trip version {states.version} states no widths; "
                f"only version {FEATURES_VERSION} with the widths feature has them")
        return {}
    widths = _object(record.get("widths"), "round_trip.widths")
    for net, entry in widths.items():
        where = f"round_trip.widths.{_text(net, 'round_trip.widths net')}"
        rule = _object(entry, where).get("rule")
        if rule not in _WIDTH_KEYS:
            raise RoundTripNamesError(
                f"{where}.rule must be {RULE_PER_SEGMENT} or {RULE_CROSS_SECTION}")
        _keys(entry, _WIDTH_KEYS[rule], where)
        requirement = _object(entry["requirement"], f"{where}.requirement")
        if set(requirement) not in (_REQUIREMENT_KEYS, _REQUIREMENT_KEYS | _AMPS_KEYS):
            raise RoundTripNamesError(
                f"{where}.requirement must state width_um and stated_um, and amps "
                "with current_density_ma_per_um only together")
        for key, value in requirement.items():
            _positive(value, f"{where}.requirement.{key}")
        if rule == RULE_PER_SEGMENT:
            required = _object(entry["required_um"], f"{where}.required_um")
            if not required:
                raise RoundTripNamesError(f"{where}.required_um states no layer")
            for layer, value in required.items():
                _text(layer, f"{where}.required_um layer")
                _positive(value, f"{where}.required_um.{layer}")
            _text(entry["pin_layer"], f"{where}.pin_layer")
            boxes = entry["taper_boxes_um"]
            if not isinstance(boxes, list):
                raise RoundTripNamesError(f"{where}.taper_boxes_um must be a list")
            for box in boxes:
                if not isinstance(box, list) or len(box) != 4:
                    raise RoundTripNamesError(
                        f"{where}.taper_boxes_um entries must be [x0, y0, x1, y1]")
                x0, y0, x1, y1 = (_number(v, f"{where}.taper_boxes_um") for v in box)
                if x0 > x1 or y0 > y1:
                    raise RoundTripNamesError(
                        f"{where}.taper_boxes_um entries must be [x0, y0, x1, y1] with "
                        "x0 <= x1 and y0 <= y1")
        else:
            directions = _object(entry["directions"], f"{where}.directions")
            if not directions or not set(directions) <= set(_DIRECTIONS):
                raise RoundTripNamesError(
                    f"{where}.directions must state horizontal, vertical or both")
            for direction, cut in directions.items():
                at = f"{where}.directions.{direction}"
                _keys(_object(cut, at), _DIRECTION_KEYS, at)
                _positive(cut["required_um"], f"{at}.required_um")
                _positive(cut["measured_um"], f"{at}.measured_um")
                _number(cut["at_um"], f"{at}.at_um")
    return widths


class ExportReport(NamedTuple):
    """An export report, as far as net names go."""
    #: Its round_trip's names; None for a board that follows no sheet.
    names: Optional[RoundTripNames]
    #: The carrier pads its board was exported with: each Reference and the
    #: io_pad id its record states, ``""`` for none.
    io_pads: Mapping[str, str] = MappingProxyType({})
    #: Where it was read from, for a refusal to name; ``""`` when unknown.
    path: str = ""


def read_export_report(report, path: str = "") -> ExportReport:
    """The net names of ``report``, a parsed export report read from ``path``,
    and the carrier pads its board had. Anything but an
    ``interposer_pnr.export`` report, version 1, is refused, and so is a
    round_trip :func:`read_round_trip_names` refuses."""
    if (not isinstance(report, dict) or report.get("format") != EXPORT_REPORT_FORMAT
            or type(report.get("version")) is not int or report["version"] != 1):
        raise RoundTripNamesError(f"not an {EXPORT_REPORT_FORMAT} report, version 1")
    record = report.get("round_trip")
    names = read_round_trip_names(record)
    # The record keys each carrier pad by its board Reference, and holds the
    # block only when the board had one.
    io_pads = {ref: _io_pad_id(pad, f"round_trip.io_pads.{ref}") for ref, pad in
               _object(record.get("io_pads", {}), "round_trip.io_pads").items()}
    return ExportReport(names, MappingProxyType(io_pads), path)


def _io_pad_id(pad, where: str) -> str:
    """The io_pad id a carrier pad record states (its IO_PAD_ID), ``""`` for none."""
    ident = _object(_object(pad, where).get("properties", {}), f"{where}.properties").get(
        "io_pad_id", "")
    if not isinstance(ident, str):
        raise RoundTripNamesError(f"{where}.properties.io_pad_id must be a string")
    return ident


#: The footprint library a carrier pad is drawn from on both export paths
#: (chiplet-spec ``chiplet_format_io.footprint_ids.CARRIER_LIBRARY``).
CARRIER_LIBRARY = "chiplet_carrier"


class BoardCarrier(NamedTuple):
    """A carrier pad footprint as a board shows it, read by the caller."""
    reference: str
    #: Its ``IO_PAD_ID`` property, ``""`` when it carries none.
    io_pad_id: str
    #: Its footprint's library nickname.
    library: str
    #: Whether the footprint links a symbol on a sheet (a non-empty ``path``).
    linked: bool


class BoardCarriers(NamedTuple):
    """What :func:`read_board_carriers` reads off a board's carriers."""
    #: The io_pad id of each carrier, in order.
    ids: Tuple[str, ...]
    #: The names its carrier nets go back to the planner's by; None when they
    #: are the planner's already.
    names: Optional[RoundTripNames]


def read_board_carriers(carriers: Sequence[BoardCarrier],
                        report: Optional[ExportReport]) -> BoardCarriers:
    """The io_pad id of each of a board's carriers and the names its nets go
    back to the planner's by; ``report`` is None when none was given.

    Two questions, answered apart. Where a carrier's id comes from: a board
    with a carrier from :data:`CARRIER_LIBRARY` or carrying ``IO_PAD_ID`` was
    drawn by ``export``, on either path, and every carrier on it must carry a
    distinct ``IO_PAD_ID``; any other board keeps the reference as the id.

    Whether the board follows a sheet: the report says, when given, once its
    board is shown to have had exactly these carrier references, each with
    the io_pad id it carries, if it carries one. Without one,
    a board drawn by export with a carrier linked to a symbol may name the
    sheet's nets, so it is refused naming the missing report; KiCad links a
    draft board too once it is updated from its draft sheet, which the
    draft's own report then reads. A board drawn by hand from its own sheet
    links its footprints as well, but names its nets itself. A report of a
    board that follows a sheet is refused on a board no carrier of which is
    linked, or none of which was drawn by export.

    An id refusal is a :class:`CarrierIdError`, a fault of the board; any
    other refusal is about the report, or its absence.
    """
    carriers = tuple(carriers)
    drawn = next((c for c in carriers if _drawn_by_export(c)), None)
    names = _report_names(carriers, drawn, report)
    if drawn is None:
        return BoardCarriers(tuple(c.reference for c in carriers), names)
    return BoardCarriers(_io_pad_ids(carriers, drawn), names)


def _drawn_by_export(carrier: BoardCarrier) -> bool:
    return bool(carrier.io_pad_id) or carrier.library == CARRIER_LIBRARY


def _report_names(carriers: Tuple[BoardCarrier, ...], drawn: Optional[BoardCarrier],
                  report: Optional[ExportReport]) -> Optional[RoundTripNames]:
    """The names ``report`` reads the board's nets back by, refusing it, or
    its absence, where it cannot be this board's."""
    if report is None:
        linked = [c.reference for c in carriers if c.linked and _drawn_by_export(c)]
        if linked:
            raise RoundTripNamesError(
                f"carrier pad {linked[0]} is linked to a sheet symbol, so this board's "
                "nets may be the sheet's; give the export report interposer-pnr export "
                "--report wrote for it")
        return None
    _same_carriers(carriers, report)
    if report.names is not None and carriers:
        if not any(c.linked for c in carriers):
            raise RoundTripNamesError(
                "the export report describes a board that follows a sheet, but no "
                "carrier of this board is linked to a sheet symbol "
                f"({carriers[0].reference} is not)")
        if drawn is None:
            raise RoundTripNamesError(
                "the export report describes a board export drew against a sheet, but no "
                f"carrier of this board was drawn by export ({carriers[0].reference} carries "
                f"no IO_PAD_ID and is not from {CARRIER_LIBRARY})")
    return report.names


def _io_pad_ids(carriers: Tuple[BoardCarrier, ...], drawn: BoardCarrier) -> Tuple[str, ...]:
    """Each carrier's ``IO_PAD_ID`` on a board export drew (``drawn`` shows
    it), refusing a carrier without one and two with the same."""
    seen: Dict[str, str] = {}
    for c in carriers:
        if not c.io_pad_id:
            raise CarrierIdError(
                f"carrier pad {c.reference}: no IO_PAD_ID; this board was drawn by export "
                f"({drawn.reference}), so a carrier's reference is not its io_pad id")
        if c.io_pad_id in seen:
            raise CarrierIdError(
                f"carrier pads {seen[c.io_pad_id]} and {c.reference} both carry IO_PAD_ID "
                f"{c.io_pad_id}")
        seen[c.io_pad_id] = c.reference
    return tuple(c.io_pad_id for c in carriers)


def _same_carriers(carriers: Sequence[BoardCarrier], report: ExportReport) -> None:
    """Refuses ``report`` unless the board it was exported with had exactly
    these carrier references, and the io_pad id of each carrier that carries
    one: a report of another board names other nets, and a draft's report
    given for a sheet board, or the reverse, is one."""
    where = f"the export report {report.path}" if report.path else "the export report"
    board, written = {c.reference for c in carriers}, set(report.io_pads)
    if board != written:
        differences = [f"{label} {', '.join(sorted(refs))}" for label, refs in (
            ("only on the board:", board - written),
            ("only in the report:", written - board)) if refs]
        raise RoundTripNamesError(
            f"{where} was written for another board: its carrier pads differ "
            f"({'; '.join(differences)})")
    for c in carriers:
        if c.io_pad_id and c.io_pad_id != report.io_pads[c.reference]:
            raise RoundTripNamesError(
                f"{where} was written for another board: carrier pad {c.reference} carries "
                f"IO_PAD_ID {c.io_pad_id}, the report's "
                f"{report.io_pads[c.reference] or 'none'}")


def planner_net(carrier: str, net: str, names: RoundTripNames) -> str:
    """The planner's name of ``carrier``'s board ``net``: the routed net it
    maps, ``""`` for no net or a sheet-only net. Any other net is refused."""
    if not net or net in names.sheet_only:
        return ""
    if net in names.sheet_to_planner:
        return names.sheet_to_planner[net]
    raise RoundTripNamesError(f"carrier {carrier}: net {net} is neither a routed net nor a "
                              "sheet net of the export report")


def _object(value, where):
    if not isinstance(value, dict):
        raise RoundTripNamesError(f"{where} must be an object")
    return value


def _keys(value, keys, where):
    if set(value) != keys:
        raise RoundTripNamesError(f"{where} must state exactly {', '.join(sorted(keys))}")


def _number(value, where):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise RoundTripNamesError(f"{where} must be a finite number")
    return value


def _positive(value, where):
    if _number(value, where) <= 0:
        raise RoundTripNamesError(f"{where} must be positive")
    return value


def _text(value, where):
    if not isinstance(value, str) or not value:
        raise RoundTripNamesError(f"{where} must be a nonempty string")
    return value
