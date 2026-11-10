# SPDX-License-Identifier: GPL-3.0-or-later
"""Carrier pads read back by io_pad id, and their nets by the export report.

interposer-pnr ``export`` draws each carrier from ``chiplet_carrier`` with its
io_pad id in a hidden IO_PAD_ID property; ``export --form`` also names it by
the sheet's ``J<n>`` and links it to the sheet symbol. Both carrier readers
must emit the io_pad id on a board export drew, refuse a carrier that lost it,
ask for the report when such a carrier is linked, and leave a board drawn by
hand as it was. The rules themselves are interposer-pnr's
``read_board_carriers``; these tests drive them through real boards and both
writers.
"""

import json
import re
import sys
from pathlib import Path

import pytest

pcbnew = pytest.importorskip(
    "pcbnew", reason="needs pcbnew (KiCad Python)")
yaml = pytest.importorskip("yaml")

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_export.writers.chiplet_writer import (  # noqa: E402
    board_carrier, carrier_identities, io_pad_carriers, preflight_carriers, write_chiplet,
    write_io_pads_json)
from chiplet_export.draft_net_names import BoardReportError, read_export_report  # noqa: E402
from chiplet_export.vendor.round_trip_names import (  # noqa: E402
    BoardCarrier, CarrierIdError, ExportReport, RoundTripNames, RoundTripNamesError)

DRAFT = PLUGIN_ROOT / "tests" / "fixtures" / "flow_draft_board" / "cf_padring_die_cmos5l.draft.kicad_pcb"
CARRIER = re.compile(r'\(property "Reference" "(J_[^"]+)"')
IO_PAD_ID_LINE = re.compile(r'\n *\(property "IO_PAD_ID" "[^"]*"[^\n]*')


def carriers(text):
    return CARRIER.findall(text)


def carrier_block(text, ref):
    """The span of the footprint block whose Reference is ``ref``."""
    start = text.rindex("\n  (footprint ", 0, text.index(f'(property "Reference" "{ref}"')) + 1
    return start, text.index("\n  )\n", start) + 5


def sheeted(text, drop=(), ids=None, token="chiplet_carrier:EXT_PAD_80um", linked=True):
    """The draft as a sheet-drawn export writes it: carrier ``i`` becomes
    ``J<i+1>`` on ``token``, linked to a sheet symbol unless ``linked`` is
    false, with IO_PAD_ID its io_pad id (or ``ids``'), except the references
    in ``drop``, which keep no IO_PAD_ID."""
    ids = ids or {}
    for n, ref in enumerate(carriers(text), start=1):
        start, end = carrier_block(text, ref)
        block = IO_PAD_ID_LINE.sub("", text[start:end])
        header, rest = block.split("\n", 1)
        header = re.sub(r'^  \(footprint "[^"]*"', f'  (footprint "{token}"', header)
        if linked:
            header = header.replace('(layer "F.Cu")', '(layer "F.Cu") (path "/%08d-1111-4111-8111-111111111111")' % n, 1)
        prop = f'(property "Reference" "{ref}"'
        line = next(l for l in rest.splitlines() if l.lstrip().startswith(prop))
        new = line.replace(prop, f'(property "Reference" "J{n}"')
        if ref not in drop:
            new += "\n" + line.replace(prop, f'(property "IO_PAD_ID" "{ids.get(ref, ref)}"').replace(
                line[line.index("(uuid "):line.index("(uuid ") + 44], f'(uuid "{n:08d}-0000-4000-8000-0000000001aa"')
        text = text[:start] + header + "\n" + rest.replace(line, new, 1) + text[end:]
    return text


def load(tmp_path, text):
    path = tmp_path / "board.kicad_pcb"
    path.write_text(text)
    return pcbnew.LoadBoard(str(path))


def board_io_pads(board):
    """The carrier pads a report of ``board`` records: each Reference and its IO_PAD_ID."""
    carriers = map(board_carrier, io_pad_carriers(board.Footprints()))
    return {c.reference: c.io_pad_id for c in carriers}


def same_names(board):
    """A report of a sheet board whose map leaves every board net as it is."""
    nets = {pad.GetNetname() for fp in board.Footprints() for pad in fp.Pads()} - {""}
    return ExportReport(RoundTripNames({net: net for net in nets}, frozenset()),
                        board_io_pads(board))


def no_sheet(board):
    """A report of a board that follows no sheet, written for ``board``."""
    return ExportReport(None, board_io_pads(board))


def chiplet_ids(tmp_path, board, report=None):
    out = tmp_path / "out.chiplet"
    write_chiplet(board, str(out), export_report=report)
    data = yaml.safe_load(out.read_text())
    interposer = next(c for c in data["components"] if c["id"] == "interposer")
    return [p["id"] for p in interposer["io_pads"]]


def json_refs(tmp_path, board, report=None):
    out = tmp_path / "io_pads.json"
    write_io_pads_json(board, str(out), export_report=report)
    return [p["ref"] for p in json.loads(out.read_text())["io_pads"]]


def test_an_unsheeted_board_keeps_its_references(tmp_path):
    text = DRAFT.read_text()
    names = carriers(text)
    assert sorted(chiplet_ids(tmp_path, load(tmp_path, text))) == sorted(names)
    assert sorted(json_refs(tmp_path, load(tmp_path, text))) == sorted(names)


def test_a_sheeted_board_reads_ids_not_designators(tmp_path):
    text = DRAFT.read_text()
    names = carriers(text)
    board_text = sheeted(text)
    assert not carriers(board_text)
    board = load(tmp_path, board_text)
    assert sorted(chiplet_ids(tmp_path, board, same_names(board))) == sorted(names)
    assert sorted(json_refs(tmp_path, board, same_names(board))) == sorted(names)


@pytest.mark.parametrize("writer", [write_chiplet, write_io_pads_json])
def test_a_sheeted_carrier_without_its_id_is_refused(tmp_path, writer):
    text = DRAFT.read_text()
    second = carriers(text)[1]
    board = load(tmp_path, sheeted(text, drop={second}))
    out = tmp_path / "out"
    with pytest.raises(CarrierIdError, match="carrier pad J2: no IO_PAD_ID"):
        writer(board, str(out), export_report=same_names(board))
    assert not out.exists()


@pytest.mark.parametrize("writer", [write_chiplet, write_io_pads_json])
def test_every_id_lost_never_falls_back_to_the_designators(tmp_path, writer):
    # From chiplet_carrier and linked to a sheet, the board needs its report;
    # given it, or unlinked, a carrier without IO_PAD_ID is refused.
    text = DRAFT.read_text()
    out = tmp_path / "out"
    board = load(tmp_path, sheeted(text, drop=set(carriers(text))))
    with pytest.raises(BoardReportError, match=r"carrier pad J\d+ is linked to a sheet symbol"):
        writer(board, str(out))
    with pytest.raises(CarrierIdError, match=r"carrier pad J\d+: no IO_PAD_ID"):
        writer(board, str(out), export_report=same_names(board))
    # Moved to another library as well, nothing on the board shows export drew
    # it, so its sheet report is the one refused.
    board = load(tmp_path, sheeted(text, drop=set(carriers(text)), token="project:bondpad"))
    with pytest.raises(BoardReportError, match=r"but no carrier of this board was drawn by export"):
        writer(board, str(out), export_report=same_names(board))
    board = load(tmp_path, sheeted(text, drop=set(carriers(text)), linked=False))
    with pytest.raises(CarrierIdError, match=r"carrier pad J\d+: no IO_PAD_ID"):
        writer(board, str(out))
    assert not out.exists()


def test_a_carrier_reads_off_the_board_as_its_four_fields(tmp_path):
    # What the shared reader is given, read off real boards: an export-drawn
    # sheet carrier and a draft carrier of the reference flow run, which export
    # drew with IO_PAD_ID its own Reference and linked to no symbol.
    def read(board):
        return {c.reference: c for c in map(board_carrier, io_pad_carriers(board.Footprints()))}
    assert read(sheet_board(tmp_path))["J1"] == BoardCarrier("J1", "J_EXT", "chiplet_carrier", True)
    assert read(load(tmp_path, DRAFT.read_text()))["J_IOVDD"] == BoardCarrier(
        "J_IOVDD", "J_IOVDD", "chiplet_carrier", False)


def test_a_board_drawn_by_hand_from_its_own_sheet_needs_no_report(tmp_path):
    # Its footprints link its own symbols and come from its project library;
    # its nets are its own.
    text = DRAFT.read_text()
    board = load(tmp_path, sheeted(text, drop=set(carriers(text)), token="project:bondpad"))
    count = len(carriers(text))
    assert sorted(chiplet_ids(tmp_path, board)) == sorted(f"J{n}" for n in range(1, count + 1))


def test_two_carriers_with_one_id_are_refused(tmp_path):
    text = DRAFT.read_text()
    first, second = carriers(text)[:2]
    board = load(tmp_path, sheeted(text, ids={second: first}))
    with pytest.raises(CarrierIdError, match=f"carrier pads J[12] and J[12] both carry IO_PAD_ID {first}"):
        write_chiplet(board, str(tmp_path / "out.chiplet"), export_report=same_names(board))


def test_two_carriers_sharing_a_reference_keep_their_own_ids(tmp_path):
    # Ids follow the footprints: a duplicated J1 must not hand both pads one id.
    # Unlinked, so no report: one keyed by reference cannot hold two J1s, and
    # one written before the edit names a J2 the board no longer has.
    text = DRAFT.read_text()
    names = carriers(text)
    board_text = sheeted(text, linked=False).replace(
        '(property "Reference" "J2"', '(property "Reference" "J1"', 1)
    board = load(tmp_path, board_text)
    assert sorted(chiplet_ids(tmp_path, board)) == sorted(names)
    assert sorted(json_refs(tmp_path, board)) == sorted(names)


def test_a_board_drawn_from_another_library_keeps_its_references(tmp_path):
    # A hand-drawn board takes its carriers from a project library, links no
    # sheet symbol and names them by io_pad id.
    text = DRAFT.read_text()
    board = load(tmp_path, sheeted(text, drop=set(carriers(text)), token="project:bondpad",
                                   linked=False))
    count = len(carriers(text))
    assert sorted(chiplet_ids(tmp_path, board)) == sorted(f"J{n}" for n in range(1, count + 1))


def updated_draft(text):
    """The draft after KiCad's Update PCB from its draft sheet: each carrier,
    drawn from chiplet_carrier with IO_PAD_ID its Reference, now links its
    EXT_PAD symbol."""
    for n, ref in enumerate(carriers(text), start=1):
        start, end = carrier_block(text, ref)
        block = IO_PAD_ID_LINE.sub("", text[start:end])
        header, rest = block.split("\n", 1)
        header = re.sub(r'^  \(footprint "[^"]*"', '  (footprint "chiplet_carrier:EXT_PAD_80um"', header)
        header = header.replace('(layer "F.Cu")', '(layer "F.Cu") (path "/%08d-2222-4222-8222-222222222222")' % n, 1)
        line = next(l for l in rest.splitlines() if l.lstrip().startswith(f'(property "Reference" "{ref}"'))
        rest = rest.replace(line, line + "\n" + line.replace('"Reference"', '"IO_PAD_ID"').replace(
            line[line.index("(uuid "):line.index("(uuid ") + 44], f'(uuid "{n:08d}-0000-4000-8000-0000000002bb"'), 1)
        text = text[:start] + header + "\n" + rest + text[end:]
    return text


def test_a_draft_updated_from_its_sheet_reads_by_its_own_report(tmp_path):
    text = DRAFT.read_text()
    board = load(tmp_path, updated_draft(text))
    assert all(fp.GetPath().AsString() for fp in board.Footprints() if fp.HasField("IO_CLASS"))
    assert sorted(chiplet_ids(tmp_path, board, no_sheet(board))) == sorted(carriers(text))
    with pytest.raises(BoardReportError, match="is linked to a sheet symbol"):
        preflight_carriers(board, None)


@pytest.mark.parametrize("writer", [write_chiplet, write_io_pads_json])
@pytest.mark.parametrize("fault", ["repeated", "missing"])
def test_a_carrier_left_out_of_the_bom_is_refused_by_both_writers(tmp_path, writer, fault):
    # The sidecar keeps BOM-excluded carriers, so the .chiplet must refuse the
    # same board: one refusing alone was only a warning, and no pads shipped.
    text = DRAFT.read_text()
    first, second = carriers(text)[:2]
    board_text = (sheeted(text, ids={second: first}) if fault == "repeated"
                  else sheeted(text, drop={second}))
    board = load(tmp_path, board_text)
    pad = next(fp for fp in board.Footprints()
               if fp.GetReference() == "J2")
    pad.SetAttributes(pad.GetAttributes() | pcbnew.FP_EXCLUDE_FROM_BOM)
    out = tmp_path / "out"
    match = (f"both carry IO_PAD_ID {first}" if fault == "repeated"
             else "carrier pad J2: no IO_PAD_ID")
    with pytest.raises(CarrierIdError, match=match):
        writer(board, str(out), export_report=same_names(board))
    assert not out.exists()


SHEET_FIXTURE = PLUGIN_ROOT / "tests" / "fixtures" / "sheet_net_names"
LEGACY = ExportReport(None)


def sheet_board(tmp_path):
    return load(tmp_path, (SHEET_FIXTURE / "board.kicad_pcb").read_text())


def written_nets(tmp_path, board, report):
    sidecar, draft = tmp_path / "pads.json", tmp_path / "draft.chiplet"
    write_io_pads_json(board, str(sidecar), export_report=report)
    write_chiplet(board, str(draft), export_report=report)
    pads = json.loads(sidecar.read_text())["io_pads"]
    interposer = next(c for c in yaml.safe_load(draft.read_text())["components"]
                      if c["id"] == "interposer")
    return ({p["ref"]: p["net"] for p in pads},
            {p["id"]: p["net"] for p in interposer["io_pads"]})


def test_real_form_export_round_trips_carrier_nets_into_the_plan(tmp_path):
    report = read_export_report(SHEET_FIXTURE / "export.json")
    assert written_nets(tmp_path, sheet_board(tmp_path), report) == (
        {"J_EXT": "D_UP_A0", "J_VSS": "vss", "J_VSS2": "vss"},
        {"J_EXT": "D_UP_A0", "J_VSS": "vss", "J_VSS2": "vss"})


def as_version_6(tmp_path, features, drop=(), **blocks):
    """The sheet fixture's report restated as version 6 with ``features``."""
    report = json.loads((SHEET_FIXTURE / "export.json").read_text())
    report["round_trip"].update(version=6, features=features, widths={}, **blocks)
    for key in drop:
        del report["round_trip"][key]
    path = tmp_path / "v6.export.json"
    path.write_text(json.dumps(report))
    return read_export_report(path)


def test_a_version_6_report_names_carrier_nets_as_its_version_3_twin(tmp_path):
    """A feature this copy does not read (zz) changes no name: interposer-pnr's
    rule is that a feature only adds a block. The gate refuses it, not this."""
    v3 = read_export_report(SHEET_FIXTURE / "export.json")
    expected = written_nets(tmp_path, sheet_board(tmp_path), v3)
    for i, (features, blocks) in enumerate(((["pad_domains", "sheet", "widths"], {}),
                                            (["pad_domains", "sheet", "widths", "zz"],
                                             {"zz": [1]}))):
        at = tmp_path / f"v6_{i}"
        at.mkdir()
        report = as_version_6(at, features, **blocks)
        assert report._replace(path="") == v3._replace(path="")
        assert written_nets(at, sheet_board(at), report) == expected


def test_a_version_6_report_without_the_sheet_feature_follows_no_sheet(tmp_path):
    report = as_version_6(tmp_path, ["pad_domains", "widths"], drop=("sheet_nets",))
    assert report.names is None


def test_a_sheet_only_carrier_net_becomes_unbound_in_both_outputs(tmp_path):
    board = sheet_board(tmp_path)
    report = read_export_report(SHEET_FIXTURE / "export.json")
    lone = next(iter(report.names.sheet_only))
    carrier = next(fp for fp in board.Footprints() if fp.HasField("IO_PAD_ID"))
    next(iter(carrier.Pads())).SetNet(board.FindNet(lone))
    for result in written_nets(tmp_path, board, report):
        assert result[carrier.GetFieldText("IO_PAD_ID")] == ""


def refused(writer, board, out, report, match):
    with pytest.raises(ValueError, match=match):
        writer(board, str(out), export_report=report)
    assert not out.exists()


@pytest.mark.parametrize("writer", [write_chiplet, write_io_pads_json])
def test_a_sheet_board_without_a_report_that_follows_a_sheet_is_refused(tmp_path, writer):
    board = sheet_board(tmp_path)
    out = tmp_path / "refused"
    refused(writer, board, out, None,
            r"carrier pad J\d+ is linked to a sheet symbol.*give the export report")
    # A draft's report names other carriers: the sheet's J<n> are not its io_pad ids.
    draft = {ident: ident for ident in ("J_EXT", "J_VSS", "J_VSS2")}
    refused(writer, board, out, LEGACY._replace(io_pads=draft),
            r"was written for another board: its carrier pads differ \(only on the board: "
            r"J1, J10, J2; only in the report: J_EXT, J_VSS, J_VSS2\)$")


@pytest.mark.parametrize("writer", [write_chiplet, write_io_pads_json])
def test_a_report_pairing_a_carrier_with_another_io_pad_is_refused(tmp_path, writer):
    # The board's references, but J1 recorded as another io_pad: a stale report.
    board = sheet_board(tmp_path)
    report = same_names(board)
    report = report._replace(io_pads=dict(report.io_pads, J1="J_VSS"))
    out = tmp_path / "refused"
    with pytest.raises(BoardReportError, match=r"was written for another board: carrier pad J1 "
                                               r"carries IO_PAD_ID J_EXT, the report's J_VSS$"):
        writer(board, str(out), export_report=report)
    assert not out.exists()


@pytest.mark.parametrize("writer", [write_chiplet, write_io_pads_json])
def test_a_sheet_report_on_a_board_drawn_against_no_sheet_is_refused(tmp_path, writer):
    report = read_export_report(SHEET_FIXTURE / "export.json")
    refused(writer, load(tmp_path, DRAFT.read_text()), tmp_path / "refused", report,
            "export report .*export.json was written for another board")
    # Written for these carriers, a sheet's report still needs them linked.
    board = load(tmp_path, sheeted(DRAFT.read_text(), linked=False))
    refused(writer, board, tmp_path / "refused", same_names(board),
            r"follows a sheet, but no carrier of this board is linked to a sheet symbol")


@pytest.mark.parametrize("writer", [write_chiplet, write_io_pads_json])
def test_a_carrier_net_in_neither_map_is_refused_by_name(tmp_path, writer):
    board = sheet_board(tmp_path)
    added = pcbnew.NETINFO_ITEM(board, "ADDED")
    board.Add(added)
    carrier = next(fp for fp in board.Footprints() if fp.HasField("IO_PAD_ID"))
    next(iter(carrier.Pads())).SetNet(added)
    refused(writer, board, tmp_path / "refused",
            read_export_report(SHEET_FIXTURE / "export.json"),
            r"carrier %s \(io_pad %s\): net ADDED is neither"
            % (carrier.GetReference(), carrier.GetFieldText("IO_PAD_ID")))


def test_a_die_sheet_path_alone_does_not_make_a_sheet_board(tmp_path):
    # Carriers alone tell a sheet board apart: a die placed from a sheet,
    # with carriers linked to no symbol, keeps the board's names and needs no
    # report.
    text = DRAFT.read_text()
    first, _ = carrier_block(text, "U1")
    text = text[:first] + text[first:].replace(
        '(layer "F.Cu")', '(layer "F.Cu") (path "/11111111-1111-4111-8111-111111111111")', 1)
    board = load(tmp_path, text)
    assert any(fp.GetPath().AsString() for fp in board.Footprints())
    assert sorted(json_refs(tmp_path, board)) == sorted(carriers(DRAFT.read_text()))


def test_a_board_drawn_against_no_sheet_keeps_its_own_net_names(tmp_path):
    # No report and a report that follows no sheet write the same bytes, and every
    # io_pad net is the carrier's own first pad's.
    board = load(tmp_path, DRAFT.read_text())
    for name in ("write_chiplet", "write_io_pads_json"):
        none, legacy = tmp_path / (name + ".none"), tmp_path / (name + ".legacy")
        globals()[name](board, str(none))
        globals()[name](board, str(legacy), export_report=no_sheet(board))
        assert none.read_bytes() == legacy.read_bytes()
    own = {fp.GetReference(): next(iter(fp.Pads())).GetNetname()
           for fp in board.Footprints() if fp.HasField("IO_CLASS")}
    assert set(own.values()) - {""}
    sidecar, draft = written_nets(tmp_path, board, None)
    assert sidecar == own and draft == own


@pytest.mark.parametrize("sheet", [False, True], ids=["hand-drawn", "sheet-drawn"])
@pytest.mark.parametrize("first_net, second_net", [("IOVSS", "IOVSS"), ("", "")])
def test_carrier_pads_on_one_net_are_accepted(tmp_path, sheet, first_net, second_net):
    board, fp = two_pad_carrier(tmp_path, sheet, first_net, second_net)
    report = same_names(board) if sheet else None
    preflight_carriers(board, report)
    sidecar, draft = written_nets(tmp_path, board, report)
    ident = fp.GetFieldText("IO_PAD_ID") if sheet else fp.GetReference()
    assert sidecar[ident] == draft[ident] == first_net


def two_pad_carrier(tmp_path, sheet, first_net, second_net):
    text = DRAFT.read_text()
    board = load(tmp_path, sheeted(text) if sheet else text)
    fp = next(fp for fp in board.Footprints() if fp.HasField("IO_CLASS"))
    second = pcbnew.PAD(fp)
    second.SetNumber("2")
    fp.Add(second)
    # KiCad inserts the new pad at the front; assign after reading its actual
    # iteration order so the regression really starts with an unnetted pad.
    first, second = list(fp.Pads())
    for pad, net in ((first, first_net), (second, second_net)):
        pad.SetNetCode(board.FindNet(net).GetNetCode() if net else 0)
    assert [pad.GetNetname() for pad in fp.Pads()] == [first_net, second_net]
    return board, fp


@pytest.mark.parametrize("sheet", [False, True], ids=["hand-drawn", "sheet-drawn"])
@pytest.mark.parametrize("first_net, second_net", [("", "IOVSS"), ("IOVSS", "IOVDD")])
def test_carrier_pads_on_different_nets_are_refused(tmp_path, sheet, first_net, second_net):
    board, fp = two_pad_carrier(tmp_path, sheet, first_net, second_net)
    report = same_names(board) if sheet else None
    label = fp.GetReference()
    if sheet:
        label += " (io_pad %s)" % fp.GetFieldText("IO_PAD_ID")
    found = ", ".join(map(repr, sorted({first_net, second_net})))
    expected = ("carrier %s: its pads carry different nets (%s); a carrier's pads carry one net"
                % (label, found))
    with pytest.raises(ValueError) as refused:
        preflight_carriers(board, report)
    assert str(refused.value) == expected
    assert not isinstance(refused.value, BoardReportError)
    for write in (write_chiplet, write_io_pads_json):
        out = tmp_path / write.__name__
        with pytest.raises(ValueError) as refused:
            write(board, str(out), export_report=report)
        assert str(refused.value) == expected
        assert not isinstance(refused.value, BoardReportError)
        assert not out.exists()


@pytest.mark.parametrize("kind", ["missing", "legacy", "unsheeted"])
def test_only_board_report_refusals_have_the_report_error_type(tmp_path, kind):
    text = DRAFT.read_text()
    board = load(tmp_path, text if kind == "unsheeted" else sheeted(text))
    report = {"missing": None, "legacy": LEGACY, "unsheeted": same_names(board)}[kind]
    with pytest.raises(BoardReportError):
        carrier_identities([fp for fp in board.Footprints() if fp.HasField("IO_CLASS")], report)


def test_an_unknown_sheet_net_is_not_a_report_input_refusal(tmp_path, monkeypatch):
    from chiplet_export.pipeline import orchestrator
    from chiplet_export.writers.connection_stacks import InterconnectManifestSnapshot

    board = sheet_board(tmp_path)
    # Keep the sheet's actual carriers; the die attachment-domain contract is
    # unrelated and would require an interconnect manifest before this check.
    for fp in list(board.Footprints()):
        if not fp.HasField("IO_CLASS"):
            board.Remove(fp)
    added = pcbnew.NETINFO_ITEM(board, "ADDED")
    board.Add(added)
    carriers = [fp for fp in board.Footprints() if fp.HasField("IO_CLASS")]
    carrier = carriers[0]
    next(iter(carrier.Pads())).SetNet(added)
    report_path = SHEET_FIXTURE / "export.json"
    with pytest.raises(RoundTripNamesError) as refused:
        carrier_identities(carriers, read_export_report(report_path))
    assert type(refused.value) is RoundTripNamesError
    assert not isinstance(refused.value, BoardReportError)
    expected = ("carrier %s (io_pad %s): net ADDED is neither a routed net nor a "
                "sheet net of the export report"
                % (carrier.GetReference(), carrier.GetFieldText("IO_PAD_ID")))
    assert str(refused.value) == expected
    # The manifest source is unrelated to this refusal. Run the real carrier
    # preflight and report reader; stop before discovery or output creation.
    monkeypatch.setattr(orchestrator, "_preflight_interconnect_manifest",
                        lambda *_a, **_kw: InterconnectManifestSnapshot(None))
    out = tmp_path / "out"
    options = orchestrator.ExportOptions(output_dir=str(out), emit_chiplet=False,
                                          export_report=str(report_path))
    result = orchestrator.run_export(board, options, str(PLUGIN_ROOT))
    assert result.error == "IO pad carriers refused: " + expected
    assert result.error_input == ""
    assert not out.exists()
