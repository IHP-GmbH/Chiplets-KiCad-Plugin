# SPDX-License-Identifier: GPL-3.0-or-later
"""The DIE_OUTLINE_LAYER footprint field and the export preflight on it.

Contract doubles, not native pcbnew. The pcbnew module, board and footprint
doubles raise on any name or method they do not declare, so the preflight
cannot lean on an API the doubles made up, and the footprint's graphics,
courtyard and bounding box raise when consulted: the declaration is a field,
never geometry. Each footprint records the fields looked up on it, which is how
"never read" is checked. What real KiCad loads from a board file needs a
native run (tests/test_chiplet_writer.py).
"""

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from .test_carrier_layer_mapping import methods_for, write_lyp  # noqa: E402
from .test_orchestrator import (  # noqa: E402
    _STAGED_CHIPLET, _install_run_export_fakes, _install_staged_finalize_fakes,
)
from .test_pad_domains import (  # noqa: E402
    PCBNEW as PAD_DOMAINS_PCBNEW, PROPERTY, document, footprint,
)
from chiplet_export import die_outline  # noqa: E402
from chiplet_export.die_outline import (  # noqa: E402
    ARCHIVED_DIE, CMIM, FIELD, IO_PAD, MAX_GDS_NUMBER, NOT_EMITTED,
    OUTLINE_DIE, DieOutlineLayerRefused, die_role, die_roles, format_layer,
    parse_layer, preflight_die_outline_layers, read_die_outline_layers,
)
from chiplet_export.pad_domains import PadDomainError  # noqa: E402
from chiplet_export.pipeline import orchestrator  # noqa: E402
from chiplet_export.pipeline.orchestrator import (  # noqa: E402
    ExportOptions, build_cli_args, describe_die_outline_layers,
    worker_reads_die_outlines,
)
from chiplet_export.writers.connection_stacks import (  # noqa: E402
    InterconnectManifestSnapshot,
)

CLI_SOURCE = Path(die_outline.__file__).parent / "chiplet_export_cli.py"
REGEN_SOURCE = Path(__file__).with_name("regenerate_wirebond_demo.py")
SET_FIELD = ("Set DIE_OUTLINE_LAYER to the LAYER/DATATYPE its GDS draws the "
             "die outline on (39/4 for SG13G2 dies)")


class _StrictModule(SimpleNamespace):
    """The pcbnew double: an undeclared name is a bug, not a default."""

    def __getattr__(self, name):
        raise AttributeError("module 'pcbnew' has no attribute %r" % name)


class _Strict:
    """Base for object doubles that raise on any undeclared method."""

    _what = "double"

    def __getattr__(self, name):
        raise AttributeError("%s has no attribute %r; declare it deliberately"
                             % (self._what, name))


PCBNEW = _StrictModule(FP_EXCLUDE_FROM_BOM=8, F_Cu=0, B_Cu=2, F_Fab=35)


class _Field(_Strict):
    _what = "PCB_FIELD double"

    def __init__(self, name, text):
        self._name, self._text = name, text

    def GetName(self):
        return self._name

    def GetText(self):
        return self._text


class _Footprint(_Strict):
    """A footprint whose fields are all it can show the preflight."""

    _what = "FOOTPRINT double"

    def __init__(self, ref="U1", fields=None, attributes=0, gds=True,
                 duplicate=None):
        self._ref = ref
        self._fields = {"GDS_FILE": "%s.gds" % ref} if gds else {}
        self._fields.update(fields or {})
        self._attributes = attributes
        self._duplicate = duplicate
        self.reads = []

    def GetReference(self):
        return self._ref

    def GetAttributes(self):
        return self._attributes

    def HasField(self, name):
        self.reads.append(name)
        return name in self._fields

    def GetFieldText(self, name):
        self.reads.append(name)
        return self._fields[name]

    def GetFields(self):
        fields = [_Field(name, text) for name, text in self._fields.items()]
        if self._duplicate is not None:
            fields.append(_Field(self._duplicate, "again"))
        return fields

    def GraphicalItems(self):
        raise AssertionError("%s: board graphics were read" % self._ref)

    def GetCourtyard(self, layer):
        raise AssertionError("%s: the courtyard was read" % self._ref)

    def BuildCourtyardCaches(self):
        raise AssertionError("%s: the courtyard cache was built" % self._ref)

    def GetBoundingBox(self):
        raise AssertionError("%s: the footprint bounding box was read"
                             % self._ref)


class _Settings(_Strict):
    _what = "BOARD_DESIGN_SETTINGS double"

    def GetBoardThickness(self):
        return 400_000


class _Board(_Strict):
    _what = "BOARD double"

    def __init__(self, footprints):
        self._footprints = list(footprints)

    def Footprints(self):
        return list(self._footprints)

    def GetFileName(self):
        return "/project/design.kicad_pcb"

    def GetDesignSettings(self):
        return _Settings()


def _die(ref="U1", layer="39/4", **kwargs):
    fields = dict(kwargs.pop("fields", {}))
    if layer is not None:
        fields[FIELD] = layer
    return _Footprint(ref, fields=fields, **kwargs)


def _marked(ref="U2", layer="garbage /// 7", **kwargs):
    """A CHIPLET_PAD_DOMAINS die. die_role only asks whether the marker is
    there; the carrier preflight is what reads it."""
    fields = {PROPERTY: "{not read here", "CONNECTION": "attachment"}
    fields.update(kwargs.pop("fields", {}))
    return _die(ref, layer, fields=fields, **kwargs)


def _out_of_scope():
    """One footprint per role the writer does not export as a die, each with
    a field that would refuse if it were read."""
    return [
        _Footprint("P1", fields={"IO_CLASS": "wire_bond", FIELD: "junk"}),
        _Footprint("C1", fields={"Model": "cap_cmim", FIELD: "junk"}),
        _Footprint("C2", fields={"Sim.Name": "cap_cmim"}, gds=False),
        _Footprint("X1", fields={FIELD: "junk"}, gds=False,
                   attributes=PCBNEW.FP_EXCLUDE_FROM_BOM),
    ]


def _preflight(footprints, explicit=None):
    return preflight_die_outline_layers(_Board(footprints), explicit, PCBNEW)


def _refused(footprints, explicit=None):
    with pytest.raises(DieOutlineLayerRefused) as refused:
        _preflight(footprints, explicit)
    return refused.value


# --- 1. the value grammar ----------------------------------------------------

@pytest.mark.parametrize("text,layer", [
    ("39/4", (39, 4)), ("189/4", (189, 4)), (" 39/4 ", (39, 4)),
    ("206/0", (206, 0)), ("0/0", (0, 0)), ("65535/65535", (65535, 65535)),
])
def test_the_grammar_accepts_layer_slash_datatype(text, layer):
    assert parse_layer(text.strip()) == layer
    assert format_layer(layer) == text.strip()
    assert _preflight([_die(layer=text)]) == ({"U1": layer}, [])


GRAMMAR_REFUSALS = ["", "39", "39/", "/4", "39/4/1", "-1/4", "39.0/4",
                    "39 / 4", "a/b", "65536/0", "0/65536", "39,4", "39=4",
                    "39/4\n0", "+39/4", "３９/4"]


@pytest.mark.parametrize("text", GRAMMAR_REFUSALS)
def test_the_grammar_refuses_anything_else(text):
    with pytest.raises(ValueError):
        parse_layer(text.strip())


@pytest.mark.parametrize("text", [t for t in GRAMMAR_REFUSALS if t.strip()])
def test_a_malformed_field_refuses_quoting_the_raw_text(text):
    refused = _refused([_die(layer=text)])
    assert refused.offenders == [("U1", "%s is %r, which is not "
                                  "LAYER/DATATYPE, two integers from 0 to %d. "
                                  "%s" % (FIELD, text, MAX_GDS_NUMBER,
                                          SET_FIELD))]


def test_the_worker_parses_with_the_same_grammar():
    """One grammar for field, dialog and worker: die_outline_gds takes the
    very objects, so the flag and the field cannot drift apart."""
    pytest.importorskip("klayout.db")
    # Both ways the worker module is loaded: as the script's sibling, and
    # inside the package.
    import die_outline_gds as gds
    from chiplet_export import die_outline_gds as packaged
    script_grammar = sys.modules["die_outline"]
    for name in ("parse_layer", "format_layer", "_LAYER_TOKEN",
                 "MAX_GDS_NUMBER"):
        assert getattr(packaged, name) is getattr(die_outline, name), name
        assert getattr(gds, name) is getattr(script_grammar, name), name
    assert gds.parse_outline_layers("U1= 39/4 ,U2=206/0") == {
        "U1": (39, 4), "U2": (206, 0)}
    for bad in ("39 / 4", "-1/4", "65536/0"):
        with pytest.raises(ValueError):
            gds.parse_outline_layers("U1=%s" % bad)


# --- 2. the preflight: what is read and what is not --------------------------

def test_a_board_with_fields_passes_with_its_map_and_exempt_list():
    footprints = ([_die("U3", "206/0"), _marked("U2"), _die("U1", " 39/4 ")]
                  + _out_of_scope())
    layers, exempt = _preflight(footprints)
    assert layers == {"U3": (206, 0), "U1": (39, 4)}
    assert list(layers) == ["U3", "U1"]  # board order; argv sorts it
    assert exempt == ["U2"]


def test_a_marked_dies_garbage_field_is_never_read():
    marked = _marked("U2", layer="-1/  and more garbage")
    no_field = _marked("U4", layer=None)
    assert _preflight([marked, no_field]) == ({}, ["U2", "U4"])
    for fp in (marked, no_field):
        assert FIELD not in fp.reads and "GDS_FILE" not in fp.reads, fp.reads


def test_io_cmim_and_bom_excluded_footprints_are_never_read():
    footprints = _out_of_scope()
    roles = {fp.GetReference(): die_role(fp, PCBNEW) for fp in footprints}
    assert roles == {"P1": IO_PAD, "C1": CMIM, "C2": CMIM, "X1": NOT_EMITTED}
    for fp in footprints:
        fp.reads.clear()
    assert _preflight(footprints) == ({}, [])
    for fp in footprints:
        assert FIELD not in fp.reads and "GDS_FILE" not in fp.reads, (
            fp.GetReference(), fp.reads)


def test_roles_follow_the_writers_order():
    """BOM exclusion first, then IO_CLASS, then cap_cmim, then the marker."""
    assert die_role(_marked(attributes=PCBNEW.FP_EXCLUDE_FROM_BOM),
                    PCBNEW) == NOT_EMITTED
    io = _marked(fields={"IO_CLASS": "wire_bond"})
    assert die_role(io, PCBNEW) == IO_PAD
    both = _Footprint("C1", fields={"Model": "cap_cmim", PROPERTY: "{}"})
    assert die_role(both, PCBNEW) == CMIM
    assert die_role(_marked(), PCBNEW) == ARCHIVED_DIE
    assert die_role(_die(), PCBNEW) == OUTLINE_DIE
    assert die_role(_Footprint("J1", gds=False), PCBNEW) == OUTLINE_DIE


def test_the_reader_returns_raw_text_for_every_die_sized_from_its_gds():
    footprints = ([_die("U1", " 39/4 "), _die("U3", "39 / 4"),
                   _die("U5", None), _marked("U2")] + _out_of_scope())
    assert read_die_outline_layers(_Board(footprints), PCBNEW) == {
        "U1": " 39/4 ", "U3": "39 / 4", "U5": ""}
    assert FIELD not in footprints[3].reads


def test_die_roles_never_hide_a_die_behind_a_shared_reference():
    """One role per reference, for the dialog's per-die rows: a die sized from
    its GDS wins over a marked die, and either over a non-die, in any board
    order. The fields themselves are never read."""
    footprints = ([
        _Footprint("U1", fields={"IO_CLASS": "wire_bond"}), _die("U1"),
        _die("U4"), _Footprint("U4", fields={"Model": "cap_cmim"}),
        _marked("U2"), _Footprint("U2", fields={"Model": "cap_cmim"}),
        _marked("U3"), _die("U3"),
    ] + _out_of_scope())
    assert die_roles(_Board(footprints), PCBNEW) == {
        "U1": OUTLINE_DIE, "U4": OUTLINE_DIE, "U2": ARCHIVED_DIE,
        "U3": OUTLINE_DIE, "P1": IO_PAD, "C1": CMIM, "C2": CMIM,
        "X1": NOT_EMITTED}
    for fp in footprints:
        assert FIELD not in fp.reads, (fp.GetReference(), fp.reads)


def test_an_empty_board_never_imports_pcbnew(monkeypatch):
    monkeypatch.setitem(sys.modules, "pcbnew", None)
    assert preflight_die_outline_layers(_Board([])) == ({}, [])
    assert read_die_outline_layers(_Board([])) == {}


def test_pcbnew_is_imported_lazily_when_the_board_has_footprints(monkeypatch):
    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    assert preflight_die_outline_layers(_Board([_die()])) == (
        {"U1": (39, 4)}, [])


def test_a_duplicated_property_propagates_as_pad_domain_error():
    die = _die(duplicate=FIELD)
    with pytest.raises(PadDomainError, match="Duplicate footprint property"):
        _preflight([die])


# --- 3. the preflight: refusals ----------------------------------------------

def test_a_die_without_the_field_refuses_by_name():
    refused = _refused([_die("U7", None)])
    assert refused.offenders == [
        ("U7", "no %s field. %s" % (FIELD, SET_FIELD))]
    assert str(refused) == ("Die outline layer refused: U7: U7: no "
                            "DIE_OUTLINE_LAYER field. " + SET_FIELD)


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_an_empty_field_refuses_by_name(value):
    refused = _refused([_die("U7", value)])
    assert refused.offenders == [
        ("U7", "%s is empty. %s" % (FIELD, SET_FIELD))]


def test_every_offender_is_named_at_once_in_board_order():
    refused = _refused([_die("U1", None), _die("U2"), _die("U3", "39 / 4"),
                        _die("J5", gds=False), _marked("U4"),
                        _die("U6", "")])
    assert [ref for ref, _cause in refused.offenders] == [
        "U1", "U3", "J5", "U6"]
    message = str(refused)
    assert message.startswith(
        "Die outline layer refused: U1, U3, J5, U6: U1: ")
    # The dialog's status line keeps 80 characters; the refs are all in them.
    assert "U1, U3, J5, U6" in message[:80]
    assert "U2" not in message and "U4" not in message


def test_one_cause_is_stated_once_with_every_reference_it_applies_to():
    """A board full of library parts refuses every one for the same reason.
    The refusal states that reason once, with all of them, so neither the
    log line nor the dialog's message box grows a sentence per part."""
    parts = ["J%d" % n for n in range(1, 41)]
    refused = _refused([_die("U1", None)]
                       + [_Footprint(ref, gds=False) for ref in parts]
                       + [_die("U2", "a/b")])
    assert len(refused.offenders) == 42
    causes = dict(refused.offenders)
    assert refused.by_cause() == [(["U1"], causes["U1"]),
                                  (parts, causes["J1"]),
                                  (["U2"], causes["U2"])]
    assert str(refused) == "Die outline layer refused: %s: %s" % (
        ", ".join(["U1"] + parts + ["U2"]),
        "; ".join(["U1: " + causes["U1"],
                   ", ".join(parts) + ": " + causes["J1"],
                   "U2: " + causes["U2"]]))
    assert str(refused).count("no GDS_FILE field") == 1


def test_a_die_without_gds_file_refuses_with_the_bom_hint():
    part = _die("J5", gds=False)
    refused = _refused([part])
    # On the symbol first: Update PCB from Schematic copies the attribute
    # from it and would undo a footprint-only change.
    assert refused.offenders == [("J5", (
        "no GDS_FILE field, so there is no GDS to read its die outline from. "
        "If it is not a die, set 'Exclude from bill of materials' on its "
        "symbol in the schematic (on the footprint, for a part that is only "
        "on the board) and the .chiplet export stops treating it as one. If "
        "it is, set GDS_FILE and DIE_OUTLINE_LAYER"))]

    # A blank GDS_FILE is no GDS_FILE.
    blank = _die("J6", fields={"GDS_FILE": "  "}, gds=False)
    assert _refused([blank]).offenders[0][1].startswith("no GDS_FILE field")

    # The advice works: excluded from the BOM, the part is out of scope.
    part._attributes = PCBNEW.FP_EXCLUDE_FROM_BOM
    assert die_role(part, PCBNEW) == NOT_EMITTED
    assert _preflight([part]) == ({}, [])


@pytest.mark.parametrize("second", ["outline", "marked"])
def test_a_reference_two_dies_share_refuses_once(second):
    other = _die("U1", "206/0") if second == "outline" else _marked("U1")
    refused = _refused([_die("U1"), _die("U2"), other])
    assert refused.offenders == [("U1", (
        "2 footprints the .chiplet exports as dies carry this reference, and "
        "a per-die outline layer cannot tell them apart. Give each die its "
        "own reference"))]


SHARED_WITH_THE_WORKER = (
    "an IO pad (IO_CLASS) or a part with a GDS_FILE carries this reference "
    "too, and the worker, which finds both by reference, cannot tell it from "
    "the die. Give each die its own reference")

# Footprints that are not dies but that the worker finds by reference: an
# IO_CLASS footprint is an --io-pads entry (write_io_pads_json takes every
# one, excluded from the bill of materials or not), and any footprint with a
# GDS_FILE is a .hyp device.
WORKER_KNOWS_BY_REFERENCE = {
    "IO pad": dict(fields={"IO_CLASS": "wire_bond"}, gds=False),
    "IO pad with GDS_FILE": dict(fields={"IO_CLASS": "wire_bond"}),
    "BOM-excluded IO pad": dict(fields={"IO_CLASS": "wire_bond"}, gds=False,
                                attributes=PCBNEW.FP_EXCLUDE_FROM_BOM),
    "BOM-excluded part with GDS_FILE": dict(
        attributes=PCBNEW.FP_EXCLUDE_FROM_BOM),
    "cap_cmim with GDS_FILE": dict(fields={"Model": "cap_cmim"}),
}


@pytest.mark.parametrize("shape", sorted(WORKER_KNOWS_BY_REFERENCE))
@pytest.mark.parametrize("marked", [False, True])
def test_a_die_sharing_a_reference_the_worker_also_finds_refuses(shape,
                                                                  marked):
    """The worker would refuse the die for the shared reference once output
    exists: two .hyp devices, or an --io-pads entry, carry it."""
    die = _marked("U1") if marked else _die("U1")
    other = _Footprint("U1", **WORKER_KNOWS_BY_REFERENCE[shape])
    for board in ([die, other], [other, die]):
        refused = _refused(board + [_die("U2")])
        assert refused.offenders == [("U1", SHARED_WITH_THE_WORKER)]


def test_a_reference_the_hyp_gives_another_footprint_counts_as_shared():
    """The .hyp names a footprint without a reference EMPTY and rewrites a
    double quote to a single one, so those devices carry the die's REF."""
    for other in (_Footprint("", attributes=PCBNEW.FP_EXCLUDE_FROM_BOM),
                  _Footprint('U"1', attributes=PCBNEW.FP_EXCLUDE_FROM_BOM)):
        ref = "EMPTY" if not other.GetReference() else "U'1"
        refused = _refused([_die(ref), other])
        assert refused.offenders == [(ref, SHARED_WITH_THE_WORKER)]


def test_a_die_sharing_a_reference_with_a_part_the_worker_never_sees_passes():
    """A non-die without a GDS_FILE and without IO_CLASS is neither a .hyp
    device nor an --io-pads entry, so the worker never meets its reference."""
    others = [_Footprint("U1", fields={"Model": "cap_cmim"}, gds=False),
              _Footprint("U1", gds=False,
                         attributes=PCBNEW.FP_EXCLUDE_FROM_BOM)]
    assert _preflight([_die("U1")] + others) == ({"U1": (39, 4)}, [])
    for other in others:
        assert FIELD not in other.reads, other.reads


@pytest.mark.parametrize("shape", ["IO pad", "BOM-excluded part with GDS_FILE"])
def test_run_export_refuses_a_shared_reference_before_any_output(
        export, monkeypatch, shape):
    options, calls, seams = export
    mkdtemp = Mock(side_effect=AssertionError("preflight created a workspace"))
    monkeypatch.setattr(orchestrator.tempfile, "mkdtemp", mkdtemp)
    board = _Board([_die("U1"),
                    _Footprint("U1", **WORKER_KNOWS_BY_REFERENCE[shape])])
    result = orchestrator.run_export(board, options, "/unused/plugin")
    assert result.error == ("Die outline layer refused: U1: U1: "
                            + SHARED_WITH_THE_WORKER)
    assert not Path(options.output_dir).exists()
    assert calls == [] and seams["writer_manifests"] == []
    mkdtemp.assert_not_called()


HYP_REWRITES = "contains a double quote, a tab or a line break"
YAML_READS_OTHERWISE = "YAML takes it for a number or a date"


@pytest.mark.parametrize("ref,cause", [
    ("", "empty reference"),
    ("U1,U2", "contains ',' or '='"),
    ("U=1", "contains ',' or '='"),
    (" U1", "starts or ends with whitespace"),
    ("U1\t", "starts or ends with whitespace"),
    # The .hyp device would carry another REF than the flags name.
    ('U"1', HYP_REWRITES), ("U\t1", HYP_REWRITES), ("U\n1", HYP_REWRITES),
    ("U\r1", HYP_REWRITES),
    # The .chiplet id, written bare, would read back as another string.
    ("007", YAML_READS_OTHERWISE), ("010", YAML_READS_OTHERWISE),
    ("1_0", YAML_READS_OTHERWISE), ("0x1F", YAML_READS_OTHERWISE),
    ("0b11", YAML_READS_OTHERWISE), ("1.50", YAML_READS_OTHERWISE),
    ("1.", YAML_READS_OTHERWISE), ("1.0e+3", YAML_READS_OTHERWISE),
    ("2001-02-30", YAML_READS_OTHERWISE), ("0x_", YAML_READS_OTHERWISE),
])
@pytest.mark.parametrize("marked", [False, True])
def test_a_reference_the_flags_cannot_carry_refuses(ref, cause, marked):
    die = _marked(ref) if marked else _die(ref)
    refused = _refused([die])
    assert [r for r, _c in refused.offenders] == [ref]
    assert cause in refused.offenders[0][1]
    # Quoted in the message, so an empty or comma-bearing ref stays legible.
    assert str(refused).startswith("Die outline layer refused: %r: " % ref)


@pytest.mark.parametrize("ref", ["U1", "U'1", "1", "10", "0", "1.5", "0.25",
                                 "2001-02-03", "0b", "0x", "08", "1e5",
                                 "U 1", "yes", "null", "U1.5"])
def test_a_reference_that_reads_back_as_itself_passes(ref):
    """Quoted by the writer (U 1, yes, null), a string to YAML (0b, 08, 1e5)
    or a number or date whose text is the reference (1, 1.5, 2001-02-03)."""
    assert _preflight([_die(ref)]) == ({ref: (39, 4)}, [])


def test_the_yaml_read_back_is_what_pyyaml_reads():
    """The preflight's YAML rule against the writer's own id and the reader
    the worker loads the .chiplet with, over every short token of the
    characters that matter."""
    yaml = pytest.importorskip("yaml")
    import itertools
    from chiplet_export.writers._yaml import yaml_scalar
    alphabet = "019_.-xbe+"
    tokens = ["".join(chars) for size in range(1, 5)
              for chars in itertools.product(alphabet, repeat=size)]
    tokens += ["2001-02-03", "2001-02-30", "0000-01-01", "2001-1-1",
               "1999-12-31", "0x1F", "0b102", "0o17", "1.0E+3", "1.5e3"]
    for ref in tokens:
        # What the .chiplet writer puts after "id: ", quoted or bare.
        try:
            same = str(yaml.safe_load(
                "id: %s\n" % yaml_scalar(ref))["id"]) == ref
        except (yaml.YAMLError, ValueError):
            same = False
        assert (die_outline._yaml_read_back(ref) == ref) == same, ref


def test_the_refusal_is_not_value_shaped():
    for absorbing in (ValueError, KeyError, OSError, LookupError):
        assert not issubclass(DieOutlineLayerRefused, absorbing)


# --- 4. the explicit map -----------------------------------------------------

def test_an_explicit_map_replaces_the_board_fields_wholesale():
    u1, u2 = _die("U1", "39/4"), _die("U2", "board garbage")
    layers, exempt = _preflight([u1, u2, _marked("U3")],
                                {"U1": "206/0", "U2": (189, 4)})
    assert layers == {"U1": (206, 0), "U2": (189, 4)}
    assert exempt == ["U3"]
    assert FIELD not in u1.reads and FIELD not in u2.reads


def test_a_partial_explicit_map_refuses_the_die_it_leaves_out():
    refused = _refused([_die("U1"), _die("U2")], {"U1": "39/4"})
    assert refused.offenders == [("U2", (
        "the export dialog gives it no outline layer. " + SET_FIELD))]


@pytest.mark.parametrize("value", ["39 / 4", (39.0, 4), (True, 4), (-1, 4),
                                   (65536, 0), 39])
def test_an_explicit_value_goes_through_the_same_grammar(value):
    refused = _refused([_die("U1")], {"U1": value})
    [(ref, cause)] = refused.offenders
    assert ref == "U1"
    assert cause.startswith("the export dialog gives it the outline layer ")
    assert cause.endswith(SET_FIELD)


def test_an_explicit_entry_for_anything_but_a_die_refuses():
    footprints = [_die("U1")] + _out_of_scope() + [_marked("U2")]
    refused = _refused(footprints, {"U1": "39/4", "P1": "39/4", "X1": "39/4",
                                    "U2": "39/4", "U9": "39/4"})
    causes = dict(refused.offenders)
    assert [ref for ref, _c in refused.offenders] == ["P1", "X1", "U2", "U9"]
    assert "it is an IO pad" in causes["P1"]
    assert "excluded from the bill of materials" in causes["X1"]
    assert "marked with CHIPLET_PAD_DOMAINS" in causes["U2"]
    assert "no footprint on the board carries this reference" in causes["U9"]


# --- 5. the log line and the gate --------------------------------------------

def test_the_log_line_is_sorted_and_names_its_source():
    layers = {"U3": (206, 0), "U1": (39, 4)}
    assert describe_die_outline_layers(layers, ["U4", "U2"]) == (
        "Die outline layers (board fields): U1=39/4, U3=206/0; exempt, "
        "archived PinList size: U2, U4")
    assert describe_die_outline_layers(layers, [], from_dialog=True) == (
        "Die outline layers (export dialog): U1=39/4, U3=206/0")
    assert describe_die_outline_layers({}, ["U2"]) == (
        "Die outline layers (board fields): exempt, archived PinList size: U2")
    assert describe_die_outline_layers({}, []) == ""


@pytest.mark.parametrize("emit_chiplet", [False, True])
@pytest.mark.parametrize("emit_complete_gds", [False, True])
def test_the_gate_is_exactly_the_argv_that_resolves_outlines(
        tmp_path, emit_chiplet, emit_complete_gds):
    options = ExportOptions(output_dir=str(tmp_path),
                            emit_chiplet=emit_chiplet,
                            emit_complete_gds=emit_complete_gds)
    argv = build_cli_args("hyp_to_gds.py", "in.hyp", "demo", options)
    assert worker_reads_die_outlines(options) == (
        "--update-chiplet-file" in argv or "--with-chiplets" in argv)


# --- 6. run_export -----------------------------------------------------------

def _export_harness(tmp_path, monkeypatch, **fakes):
    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    calls, seams = _install_run_export_fakes(monkeypatch, **fakes)
    snapshot = InterconnectManifestSnapshot({"methods": {}})
    monkeypatch.setattr(orchestrator, "_preflight_interconnect_manifest",
                        lambda *a, **kw: snapshot)
    # --with-chiplets without --update-chiplet-file: the lighter of the two
    # modes that read outlines, with no .chiplet guard to stage around.
    options = ExportOptions(
        output_dir=str(tmp_path / "out"), emit_chiplet=False,
        emit_complete_gds=True, emit_assembly_drc=False)
    return options, calls, seams


@pytest.fixture
def export(tmp_path, monkeypatch):
    return _export_harness(tmp_path, monkeypatch)


def _flag(argv, name):
    found = [a.split("=", 1)[1] for a in argv if a.startswith(name + "=")]
    return found[0] if found else None


@pytest.mark.parametrize("existing", [False, True])
def test_preflight_refuses_before_any_output_or_writer(
        export, monkeypatch, existing):
    options, calls, seams = export
    output = Path(options.output_dir)
    if existing:
        output.mkdir()
        for filename in ("design.hyp", "design.chiplet", "accepted.gds"):
            (output / filename).write_bytes(b"accepted evidence")
    before = {p.name: p.read_bytes() for p in output.glob("*")}
    mkdtemp = Mock(side_effect=AssertionError("preflight created a workspace"))
    monkeypatch.setattr(orchestrator.tempfile, "mkdtemp", mkdtemp)
    result = orchestrator.run_export(_Board([_die("U7", None)]), options,
                                     "/unused/plugin")
    assert result.error.startswith(
        "Die outline layer refused: U7: U7: no DIE_OUTLINE_LAYER field"
    ), result.error
    assert "crashed" not in result.error
    assert {p.name: p.read_bytes() for p in output.glob("*")} == before
    assert output.exists() == existing
    assert calls == [] and seams["writer_manifests"] == []
    mkdtemp.assert_not_called()


def test_preflight_is_what_stops_the_run(export, monkeypatch):
    """The mutant for the preflight: without it the same board reaches the
    (doubled) writer and creates the output directory."""
    options, calls, seams = export
    monkeypatch.setattr(die_outline, "preflight_die_outline_layers",
                        lambda *a, **kw: ({}, []))
    result = orchestrator.run_export(_Board([_die("U7", None)]), options,
                                     "/unused/plugin")
    assert "Die outline layer refused" not in result.error
    assert Path(options.output_dir).exists()
    assert len(seams["writer_manifests"]) == 1


def test_preflight_passes_a_board_with_fields(export):
    options, calls, seams = export
    logs = []
    board = _Board([_die("U3", "206/0"), _die("U1")] + _out_of_scope())
    result = orchestrator.run_export(board, options, "/unused/plugin",
                                     on_log=logs.append)
    assert result.error == ""
    assert len(seams["writer_manifests"]) == 1
    assert _flag(calls[0], "--die-outline-layers") == "U1=39/4,U3=206/0"
    assert _flag(calls[0], "--die-outline-exempt") is None
    assert [line for line in logs if line.startswith("Die outline")] == [
        "Die outline layers (board fields): U1=39/4, U3=206/0"]


def test_preflight_reads_every_die_not_only_the_first(export, monkeypatch):
    """A good die first, then two that refuse. A preflight that stopped early
    would let the run create the output tree and the workspace."""
    options, calls, seams = export
    mkdtemp = Mock(side_effect=AssertionError("preflight created a workspace"))
    monkeypatch.setattr(orchestrator.tempfile, "mkdtemp", mkdtemp)
    board = _Board([_die("U1"), _die("U2", None), _die("U3", "39/4/0")])
    result = orchestrator.run_export(board, options, "/unused/plugin")
    assert result.error.startswith("Die outline layer refused: U2, U3: "), (
        result.error)
    assert not Path(options.output_dir).exists()
    assert calls == [] and seams["writer_manifests"] == []
    mkdtemp.assert_not_called()


def test_the_explicit_map_reaches_the_worker_and_the_log(export):
    options, calls, seams = export
    options.die_outline_layers = {"U1": "206/0"}
    logs = []
    result = orchestrator.run_export(_Board([_die("U1", "not read")]),
                                     options, "/unused/plugin",
                                     on_log=logs.append)
    assert result.error == ""
    assert _flag(calls[0], "--die-outline-layers") == "U1=206/0"
    assert "Die outline layers (export dialog): U1=206/0" in logs


def test_the_argv_carries_the_preflights_map_and_exempt_list(
        tmp_path, monkeypatch):
    """A marked die that passes the carrier preflight, beside an unmarked die
    with its field: one is exempt, the other declared. A caller's own exempt
    list is overwritten, since the exemption is derived from the markers."""
    monkeypatch.setitem(sys.modules, "pcbnew", PAD_DOMAINS_PCBNEW)
    marked, _props = footprint()
    calls, seams = _install_run_export_fakes(
        monkeypatch, die_connections={"U1": "attachment"})
    snapshot = InterconnectManifestSnapshot(
        {"methods": methods_for(document())})
    monkeypatch.setattr(orchestrator, "_preflight_interconnect_manifest",
                        lambda *a, **kw: snapshot)
    board = SimpleNamespace(
        Footprints=lambda: [marked, _die("U2", "206/0")],
        GetLayerName=Mock(return_value="TopMetal2"),
        GetFileName=lambda: "/project/design.kicad_pcb")
    options = ExportOptions(
        output_dir=str(tmp_path / "out"), emit_chiplet=False,
        emit_complete_gds=True, emit_assembly_drc=False,
        lyp_override=str(write_lyp(tmp_path / "selected.lyp")),
        die_outline_exempt=["U9"])
    logs = []
    result = orchestrator.run_export(board, options, "/unused/plugin",
                                     on_log=logs.append)
    assert result.error == ""
    assert _flag(calls[0], "--die-outline-layers") == "U2=206/0"
    assert _flag(calls[0], "--die-outline-exempt") == "U1"
    assert options.die_outline_exempt == ["U9"]  # the caller's copy is not
    assert ("Die outline layers (board fields): U2=206/0; exempt, archived "
            "PinList size: U1") in logs


def test_the_chiplet_mode_gets_the_same_flags(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    calls, seams = _install_staged_finalize_fakes(
        monkeypatch, chiplet_body=_STAGED_CHIPLET)
    options = ExportOptions(output_dir=str(tmp_path / "out"),
                            emit_chiplet=True, emit_complete_gds=False)
    result = orchestrator.run_export(_Board([_die("U1")]), options,
                                     "/unused/plugin")
    assert result.error == ""
    assert "--update-chiplet-file" in calls[0]
    assert _flag(calls[0], "--die-outline-layers") == "U1=39/4"


def test_no_preflight_when_the_worker_reads_no_outlines(export, monkeypatch):
    """Neither --update-chiplet-file nor --with-chiplets: the worker reads no
    outline, so an unset field does not refuse, and nothing a caller put in
    the two options reaches the argv."""
    options, calls, seams = export
    options.emit_complete_gds = False
    options.die_outline_layers = {"U1": "39/4"}
    options.die_outline_exempt = ["U2"]

    def forbidden(*_a, **_kw):
        raise AssertionError("the outline preflight ran for a run whose "
                             "worker reads no outlines")

    monkeypatch.setattr(die_outline, "preflight_die_outline_layers", forbidden)
    logs = []
    result = orchestrator.run_export(_Board([_die("U7", None)]), options,
                                     "/unused/plugin", on_log=logs.append)
    assert result.error == ""
    assert not [a for a in calls[0] if a.startswith("--die-outline-")]
    assert not [line for line in logs if line.startswith("Die outline")]


def test_the_cli_creates_nothing_before_a_refusal(export, tmp_path,
                                                   monkeypatch):
    """chiplet_export_cli used to create the output directory before
    run_export, so a refusal through the CLI left one behind. The real
    run_export and its preflight run here; only pcbnew and the writers are
    doubles."""
    board_file = tmp_path / "design.kicad_pcb"
    board_file.write_text("(kicad_pcb)\n", encoding="utf-8")
    board = _Board([_die("U7", None)])
    monkeypatch.setitem(sys.modules, "pcbnew", _StrictModule(
        **vars(PCBNEW), LoadBoard=lambda path: board))
    mkdtemp = Mock(side_effect=AssertionError("preflight created a workspace"))
    monkeypatch.setattr(orchestrator.tempfile, "mkdtemp", mkdtemp)
    # Loaded under a private name, so the double pcbnew it imports never
    # lands in sys.modules as the real CLI module.
    spec = importlib.util.spec_from_file_location("_cli_under_test",
                                                  CLI_SOURCE)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    output = tmp_path / "cli_out"
    assert cli.main(["--board", str(board_file),
                     "--output-dir", str(output)]) == 1
    assert not output.exists()
    mkdtemp.assert_not_called()


# --- 7. write_die_outline_layers, against doubles ----------------------------

class _ManagedField(_Field):
    """A field with the presentation _style_managed_field adjusts."""

    _what = "managed PCB_FIELD double"

    def __init__(self, name, text):
        super().__init__(name, text)
        self.layer, self.visible = PCBNEW.F_Cu, True
        self.size, self.thickness = (1_270_000, 1_270_000), 150_000

    def GetLayer(self):
        return self.layer

    def SetLayer(self, layer):
        self.layer = layer

    def IsVisible(self):
        return self.visible

    def SetVisible(self, visible):
        self.visible = visible

    def GetTextSize(self):
        return SimpleNamespace(x=self.size[0], y=self.size[1])

    def SetTextSize(self, size):
        self.size = (size.x, size.y)

    def GetTextThickness(self):
        return self.thickness

    def SetTextThickness(self, thickness):
        self.thickness = thickness


class _WritableFootprint(_Footprint):
    _what = "writable FOOTPRINT double"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._objects = {name: _ManagedField(name, text)
                         for name, text in self._fields.items()}
        self.writes = []

    def GetFields(self):
        return list(self._objects.values())

    def SetField(self, name, text):
        self.writes.append((name, text))
        self._fields[name] = text
        self._objects.setdefault(name, _ManagedField(name, text))._text = text


@pytest.fixture
def writer(monkeypatch):
    double = _StrictModule(
        **vars(PCBNEW), FromMM=lambda mm: int(round(mm * 1_000_000)),
        VECTOR2I=lambda x, y: SimpleNamespace(x=x, y=y))
    monkeypatch.setitem(sys.modules, "pcbnew", double)
    from chiplet_export.writers import chiplet_writer
    monkeypatch.setattr(chiplet_writer, "pcbnew", double)
    return chiplet_writer


def test_the_writer_writes_only_dies_sized_from_their_gds(writer):
    u1 = _WritableFootprint("U1")
    u3 = _WritableFootprint("U3", fields={FIELD: "189/4"})
    marked = _WritableFootprint("U2", fields={PROPERTY: "{}"})
    io = _WritableFootprint("P1", fields={"IO_CLASS": "wire_bond"})
    excluded = _WritableFootprint("X1", attributes=PCBNEW.FP_EXCLUDE_FROM_BOM)
    board = _Board([u1, u3, marked, io, excluded])
    changed = writer.write_die_outline_layers(board, {
        "U1": " 39/4 ", "U3": "", "U2": "39/4", "P1": "39/4", "X1": "39/4"})
    assert changed == ["U1"]
    assert u1.writes == [(FIELD, "39/4")]
    assert u1.GetFieldText(FIELD) == "39/4"
    field = u1._objects[FIELD]
    assert (field.layer, field.visible) == (PCBNEW.F_Fab, False)
    # Never cleared: an empty text leaves the field as it was.
    assert u3.writes == [] and u3.GetFieldText(FIELD) == "189/4"
    # A marked die never gets the field; non-dies are left alone.
    for fp in (marked, io, excluded):
        assert fp.writes == [] and FIELD not in fp._fields, fp.GetReference()
    # The same text again is a no-op.
    assert writer.write_die_outline_layers(board, {"U1": "39/4"}) == []
    assert read_die_outline_layers(board, writer.pcbnew)["U1"] == "39/4"


def _writable_part(ref, fields=None, attributes=0, gds=True):
    """A footprint write_chiplet can emit on the host: fields, placement and
    a bounding box, with an empty courtyard."""
    props = {"GDS_FILE": "%s.gds" % ref} if gds else {}
    props.update(fields or {})
    return Mock(
        GetReference=Mock(return_value=ref),
        # Drawn by hand: a project library, linked to no sheet symbol.
        GetFPID=Mock(return_value=SimpleNamespace(GetLibNickname=lambda: "project")),
        GetPath=Mock(return_value=SimpleNamespace(AsString=lambda: "")),
        GetAttributes=Mock(return_value=attributes),
        HasField=Mock(side_effect=lambda key: key in props),
        GetFieldText=Mock(side_effect=lambda key: props[key]),
        GetFields=Mock(side_effect=lambda: [
            SimpleNamespace(GetName=lambda k=k: k, GetText=lambda v=v: v)
            for k, v in props.items()]),
        GetPosition=Mock(return_value=SimpleNamespace(x=1_000_000,
                                                      y=2_000_000)),
        GetLayer=Mock(return_value=PAD_DOMAINS_PCBNEW.F_Cu),
        GetBoundingBox=Mock(return_value=Mock(
            GetWidth=Mock(return_value=500_000),
            GetHeight=Mock(return_value=400_000))),
        GetCourtyard=Mock(return_value=Mock(IsEmpty=Mock(return_value=True))),
        GetOrientation=Mock(return_value=SimpleNamespace(
            AsDegrees=lambda: 0.0)),
        Pads=Mock(return_value=[]),
    )


def test_the_writer_exports_exactly_the_dies_the_preflight_names(
        tmp_path, monkeypatch):
    """Every die component write_chiplet emits is in exactly one of the
    preflight's two lists, and nothing else is: one footprint per role on
    one board, through the real writer loop."""
    monkeypatch.setitem(sys.modules, "pcbnew", PAD_DOMAINS_PCBNEW)
    from chiplet_export.writers import chiplet_writer
    monkeypatch.setattr(chiplet_writer, "pcbnew", PAD_DOMAINS_PCBNEW)
    monkeypatch.setattr(chiplet_writer, "validate_interconnect_ids",
                        lambda **kw: None)
    monkeypatch.setattr(chiplet_writer, "emit_connection_stacks_block",
                        lambda *a: "connection_stacks: {}\n")
    marked, _props = footprint()  # U1, with a valid CHIPLET_PAD_DOMAINS
    marked.GetPosition.return_value = SimpleNamespace(x=0, y=0)
    marked.GetLayer.return_value = PAD_DOMAINS_PCBNEW.F_Cu
    excluded = PAD_DOMAINS_PCBNEW.FP_EXCLUDE_FROM_BOM
    footprints = [
        marked,
        _writable_part("U2", {FIELD: "39/4"}),
        _writable_part("U3", {FIELD: "206/0"}),
        _writable_part("P1", {"IO_CLASS": "wire_bond",
                              "IO_PAD_SIZE_UM": "60x60"}),
        _writable_part("C1", {"Model": "cap_cmim"}),
        _writable_part("X1", {FIELD: "39/4"}, attributes=excluded),
    ]
    board = Mock(
        Footprints=Mock(return_value=footprints),
        GetProperties=Mock(return_value={}),
        GetProject=Mock(return_value=None),
        GetFileName=Mock(return_value="design.kicad_pcb"),
        GetBoardEdgesBoundingBox=Mock(return_value=Mock(
            IsValid=Mock(return_value=True),
            GetWidth=Mock(return_value=5_000_000),
            GetHeight=Mock(return_value=5_000_000),
            GetX=Mock(return_value=0), GetBottom=Mock(return_value=5_000_000))),
        GetDesignSettings=Mock(return_value=SimpleNamespace(
            GetBoardThickness=lambda: 400_000)),
    )
    output = tmp_path / "design.chiplet"
    assert chiplet_writer.write_chiplet(board, output)
    text = output.read_text(encoding="utf-8")
    dies = re.findall(r"^  - id: (\S+)\n    type: die$", text, re.M)
    io_pads = re.findall(r"^      - id: (\S+)\n        io_class:", text, re.M)

    layers, exempt = preflight_die_outline_layers(board, None,
                                                  PAD_DOMAINS_PCBNEW)
    assert sorted(dies) == ["U1", "U2", "U3"]
    assert set(dies) == set(layers) | set(exempt)
    assert not set(layers) & set(exempt)
    assert exempt == ["U1"]
    assert io_pads == ["P1"]


# --- 8. the wire-bond regeneration driver ------------------------------------

def _regen_driver(monkeypatch, board):
    """tests/regenerate_wirebond_demo.py with pcbnew, the writer and the
    worker subprocess doubled. Loaded under a private name, like the CLI
    above, so none of its doubles lands in sys.modules for good."""
    runs, written = [], []
    monkeypatch.setitem(sys.modules, "pcbnew", _StrictModule(
        **vars(PCBNEW), LoadBoard=lambda path: board))
    writer = ModuleType("chiplet_export.writers.chiplet_writer")

    def write_chiplet(_board, path):
        written.append(path)
        Path(path).write_text("components: []\n", encoding="utf-8")
        return True

    writer.write_chiplet = write_chiplet
    monkeypatch.setitem(sys.modules, writer.__name__, writer)
    spec = importlib.util.spec_from_file_location("_regen_under_test",
                                                  REGEN_SOURCE)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    monkeypatch.setattr(driver, "find_worker_python",
                        lambda *a, **kw: "/worker/python")
    monkeypatch.setattr(driver, "find_hyp_to_gds",
                        lambda *a, **kw: "/plugin/hyp_to_gds.py")

    def run(cmd, **kwargs):
        runs.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(driver, "subprocess", SimpleNamespace(run=run))
    return driver, runs, written


def _regen_argv(tmp_path, output):
    hyp = tmp_path / "demo.hyp"
    hyp.write_text("{VERSION=2.14}\n", encoding="utf-8")
    return ["regenerate_wirebond_demo.py",
            "--board", str(tmp_path / "demo.kicad_pcb"),
            "--output-dir", str(output), "--use-existing-hyp", str(hyp)]


def test_the_regen_driver_passes_the_flags_the_board_declares(
        tmp_path, monkeypatch, capsys):
    """The --use-existing-hyp path builds its own worker argv, so it derives
    both flags itself, from the same preflight run_export uses."""
    board = _Board([_die("U3", "206/0"), _marked("U2"), _die("U1")]
                   + _out_of_scope())
    driver, runs, written = _regen_driver(monkeypatch, board)
    output = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", _regen_argv(tmp_path, output))
    with pytest.raises(SystemExit) as exited:
        driver.main()
    assert exited.value.code == 0
    assert _flag(runs[0], "--die-outline-layers") == "U1=39/4,U3=206/0"
    assert _flag(runs[0], "--die-outline-exempt") == "U2"
    assert written == [str(output / "demo.chiplet")]
    assert ("Die outline layers (board fields): U1=39/4, U3=206/0; exempt, "
            "archived PinList size: U2") in capsys.readouterr().out


def test_the_regen_driver_refuses_before_anything_is_written(
        tmp_path, monkeypatch, capsys):
    board = _Board([_die("U1"), _die("U2", None), _Footprint("J1", gds=False)])
    driver, runs, written = _regen_driver(monkeypatch, board)
    output = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", _regen_argv(tmp_path, output))
    with pytest.raises(SystemExit) as exited:
        driver.main()
    assert exited.value.code == 1
    assert not output.exists()
    assert written == [] and runs == []
    out = capsys.readouterr().out
    assert "ERROR: Die outline layer refused:" in out
    assert "    U2: no DIE_OUTLINE_LAYER field. " + SET_FIELD in out
    assert "    J1: no GDS_FILE field" in out
