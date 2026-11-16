# SPDX-License-Identifier: GPL-3.0-or-later
"""The io_pads.json the export writes is chiplet-spec's io_pad manifest.

write_io_pads_json against footprint and board doubles, not a claim of native
pcbnew execution (test_io_pad_ids.py drives real boards): the manifest is
checked against the schema at the reader's pin, its numbers and layers are
checked by value, and a carrier the schema calls an emitter error is refused
before any output, by preflight_carriers and by both writers.
"""

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
import warnings

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from .conftest import (  # noqa: E402
    chiplet_spec_pin, spec_blob_at_pin, spec_root, write_io_pads,
)
from chiplet_export import io_pads_manifest  # noqa: E402
from chiplet_export.io_pads_manifest import IoPadsRefused  # noqa: E402

VENDOR = Path(__file__).resolve().parents[1] / "vendor"
ENVELOPE = {"schema": io_pads_manifest.SCHEMA,
            "version": io_pads_manifest.VERSION,
            "frame": io_pads_manifest.FRAME}

PCBNEW = SimpleNamespace(PCB_IU_PER_MM=1_000_000)
F_CU, B_CU = 0, 31
chiplet_writer = None


@pytest.fixture(autouse=True)
def _pcbnew(monkeypatch):
    """The writer imports pcbnew at module level: a host has none."""
    global chiplet_writer
    if "pcbnew" not in sys.modules:
        monkeypatch.setitem(sys.modules, "pcbnew", PCBNEW)
    from chiplet_export.writers import chiplet_writer as module
    monkeypatch.setattr(module, "pcbnew", PCBNEW)
    chiplet_writer = module


def _pad(sx_um, sy_um, net="VDD"):
    return SimpleNamespace(GetSizeX=lambda: int(sx_um * 1000),
                           GetSizeY=lambda: int(sy_um * 1000),
                           GetNetname=lambda: net)


def _footprint(ref, x_nm, y_nm, fields, pads=(), layer=F_CU):
    return SimpleNamespace(
        GetReference=lambda: ref,
        HasField=lambda name: name in fields,
        GetFieldText=lambda name: fields[name],
        Pads=lambda: list(pads),
        GetPosition=lambda: SimpleNamespace(x=x_nm, y=y_nm),
        GetLayer=lambda: layer,
        GetFPID=lambda: SimpleNamespace(GetLibNickname=lambda: "hand_drawn"),
        GetPath=lambda: SimpleNamespace(AsString=lambda: ""))


def _board(footprints, filename="/work/demo.kicad_pcb", layer_names=None):
    names = layer_names if layer_names is not None else {F_CU: "F.Cu", B_CU: "B.Cu"}
    return SimpleNamespace(Footprints=lambda: list(footprints),
                           GetLayerName=lambda layer: names[layer],
                           GetFileName=lambda: filename)


def _carriers():
    """A sized wire-bond pad off the grid, a bump sized by its pad, and a
    bump on the back."""
    return [
        _footprint("J1", 1_234_567, 2_345_678,
                   {"IO_CLASS": "wire_bond", "IO_PAD_SIZE_UM": "80x60"},
                   [_pad(10, 10)]),
        _footprint("J2", -500_000, 0, {"IO_CLASS": "flipped_bump"},
                   [_pad(25.5, 25.5, net="GND")]),
        _footprint("J3", 0, -7_000_001,
                   {"IO_CLASS": "tsv_bump", "IO_PAD_SIZE_UM": "0x0"},
                   [_pad(30, 40, net="")], layer=B_CU),
    ]


def _written(tmp_path, board):
    out = tmp_path / "io_pads.json"
    assert chiplet_writer.write_io_pads_json(board, str(out)) == len(list(board.Footprints()))
    return json.loads(out.read_text())


# --- against the schema ------------------------------------------------------

_ANNOTATIONS = {"$schema", "$id", "title", "description"}


def _check(value, schema, at="$"):
    """``value`` against the keywords io_pads.schema.json uses; any other
    keyword fails the case, so a schema that grows one cannot pass unchecked."""
    unknown = set(schema) - _ANNOTATIONS - {
        "type", "required", "additionalProperties", "properties", "items",
        "const", "enum", "pattern", "minLength", "exclusiveMinimum"}
    assert not unknown, "%s: keywords this check does not read: %s" % (at, sorted(unknown))
    kind = schema.get("type")
    if kind == "object":
        assert isinstance(value, dict), at
        for key in schema.get("required", ()):
            assert key in value, "%s: %s missing" % (at, key)
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            assert set(value) <= set(props), "%s: %s" % (at, sorted(set(value) - set(props)))
        for key, sub in props.items():
            if key in value:
                _check(value[key], sub, "%s.%s" % (at, key))
    elif kind == "array":
        assert isinstance(value, list), at
        for n, item in enumerate(value):
            _check(item, schema["items"], "%s[%d]" % (at, n))
    elif kind == "string":
        assert isinstance(value, str), at
    elif kind == "number":
        assert isinstance(value, (int, float)) and not isinstance(value, bool), at
    else:
        assert kind is None, "%s: type %s" % (at, kind)
    if "const" in schema:
        assert value == schema["const"], at
    if "enum" in schema:
        assert value in schema["enum"], "%s: %r" % (at, value)
    if "pattern" in schema:
        assert re.match(schema["pattern"], value), "%s: %r" % (at, value)
    if "minLength" in schema:
        assert len(value) >= schema["minLength"], at
    if "exclusiveMinimum" in schema:
        assert value > schema["exclusiveMinimum"], "%s: %r" % (at, value)


def _pinned_schema():
    root = spec_root()
    if root is None:
        pytest.skip("chiplet-spec checkout not found (set CHIPLET_SPEC_ROOT)")
    commit = chiplet_spec_pin()[0]
    return json.loads(spec_blob_at_pin(root, commit, "schemas/io_pads.schema.json"))


def test_the_manifest_is_valid_at_the_pinned_schema(tmp_path):
    schema = _pinned_schema()
    _check(_written(tmp_path, _board(_carriers())), schema)


@pytest.mark.parametrize("field, value, fault", [
    ("size_x_um", 0.0, "size_x_um"), ("io_class", "wirebond", "io_class"),
    ("layer", "", "layer")])
def test_the_schema_check_reaches_each_pad(tmp_path, field, value, fault):
    schema = _pinned_schema()
    data = _written(tmp_path, _board(_carriers()))
    data["io_pads"][1][field] = value
    with pytest.raises(AssertionError, match=r"io_pads\[1\]\.%s" % fault):
        _check(data, schema)


def test_the_classes_are_the_pinned_schema_vocabulary():
    pads = _pinned_schema()["properties"]["io_pads"]["items"]["properties"]
    assert list(chiplet_writer.IO_CLASSES) == pads["io_class"]["enum"]


def test_the_schema_check_refuses_the_bare_list(tmp_path):
    """The check is not vacuous: the file this writer used to write fails it."""
    schema = _pinned_schema()
    bare = {"io_pads": _written(tmp_path, _board(_carriers()))["io_pads"]}
    with pytest.raises(AssertionError, match="schema missing"):
        _check(bare, schema)


# --- by value ----------------------------------------------------------------

def test_the_envelope_names_the_board_frame(tmp_path):
    data = _written(tmp_path, _board(_carriers()))
    assert list(data) == ["schema", "version", "frame", "generator", "board", "io_pads"]
    assert data["schema"] == "chiplet-io-pad-manifest"
    assert data["version"] == "1.0.0"
    assert data["frame"] == "board_absolute_ynegated_um"
    assert data["generator"] == "chiplet_writer.write_io_pads_json"
    assert data["board"] == "demo.kicad_pcb"


def test_an_unsaved_board_names_no_file(tmp_path):
    assert "board" not in _written(tmp_path, _board(_carriers(), filename=""))


def test_pads_are_board_absolute_y_negated(tmp_path):
    pads = _written(tmp_path, _board(_carriers()))["io_pads"]
    assert [(p["x_um"], p["y_um"]) for p in pads] == [
        (1234.567, -2345.678), (-500.0, 0.0), (0.0, 7000.001)]


def test_a_size_comes_from_the_field_else_the_first_pad(tmp_path):
    pads = _written(tmp_path, _board(_carriers()))["io_pads"]
    assert [(p["size_x_um"], p["size_y_um"]) for p in pads] == [
        (80.0, 60.0), (25.5, 25.5), (30.0, 40.0)]


def test_layer_is_the_pdk_layer_and_the_board_layer_is_provenance(tmp_path):
    board = _board(_carriers(), layer_names={F_CU: "TopMetal2", B_CU: "B.Cu"})
    pads = _written(tmp_path, board)["io_pads"]
    assert [p["layer"] for p in pads] == ["TopMetal2"] * 3
    assert [p["source_layer"] for p in pads] == ["TopMetal2", "TopMetal2", "B.Cu"]


def test_classes_and_nets_go_through(tmp_path):
    pads = _written(tmp_path, _board(_carriers()))["io_pads"]
    assert [(p["ref"], p["io_class"], p["net"]) for p in pads] == [
        ("J1", "wire_bond", "VDD"), ("J2", "flipped_bump", "GND"), ("J3", "tsv_bump", "")]


# --- refusals ----------------------------------------------------------------

SIZELESS = {
    "no field, no pad": ({"IO_CLASS": "wire_bond"}, ()),
    "unparsable field, no pad": ({"IO_CLASS": "wire_bond", "IO_PAD_SIZE_UM": "80"}, ()),
    "zero field, zero pad": ({"IO_CLASS": "wire_bond", "IO_PAD_SIZE_UM": "0x0"}, [_pad(0, 0)]),
    "a flat pad": ({"IO_CLASS": "wire_bond"}, [_pad(50, 0)]),
    "below a nanometer": ({"IO_CLASS": "wire_bond", "IO_PAD_SIZE_UM": "0.0004x50"}, ()),
    "an infinite field": ({"IO_CLASS": "wire_bond", "IO_PAD_SIZE_UM": "infx50"}, ()),
    "a nan field": ({"IO_CLASS": "wire_bond", "IO_PAD_SIZE_UM": "nanx50"}, ()),
}


@pytest.mark.parametrize("case", sorted(SIZELESS))
def test_a_pad_of_no_size_is_refused_before_the_file(tmp_path, case):
    fields, pads = SIZELESS[case]
    board = _board(_carriers() + [_footprint("J9", 0, 0, fields, pads)])
    out = tmp_path / "io_pads.json"
    with pytest.raises(ValueError, match="J9 has no pad size"):
        chiplet_writer.write_io_pads_json(board, str(out))
    assert not out.exists()
    with pytest.raises(ValueError, match="J9 has no pad size"):
        chiplet_writer.preflight_carriers(board, None)


def test_a_field_size_wins_unless_it_rounds_to_zero():
    fp = _footprint("J1", 0, 0, {"IO_CLASS": "wire_bond", "IO_PAD_SIZE_UM": "0.0004x50"},
                    [_pad(10, 20)])
    assert chiplet_writer.io_pad_size_um(fp) == (10.0, 20.0)
    fp = _footprint("J1", 0, 0, {"IO_CLASS": "wire_bond", "IO_PAD_SIZE_UM": "70x50"},
                    [_pad(10, 20)])
    assert chiplet_writer.io_pad_size_um(fp) == (70.0, 50.0)


@pytest.mark.parametrize("io_class", ["wirebond", "solder_bump", " wire_bond"])
def test_a_class_outside_the_vocabulary_is_refused_before_the_file(tmp_path, io_class):
    """Off the bill of materials too: write_chiplet never sees such a carrier,
    the manifest does."""
    board = _board(_carriers() + [_footprint("J9", 0, 0, {"IO_CLASS": io_class}, [_pad(5, 5)])])
    out = tmp_path / "io_pads.json"
    with pytest.raises(ValueError, match="J9 has an unrecognized IO_CLASS"):
        chiplet_writer.write_io_pads_json(board, str(out))
    assert not out.exists()
    with pytest.raises(ValueError, match="J9 has an unrecognized IO_CLASS"):
        chiplet_writer.preflight_carriers(board, None)


def test_a_class_is_refused_before_a_net(tmp_path):
    """read_io_pads reads classes and sizes before ids and nets, so the
    preflight and the writer name the same first fault."""
    split = _footprint("J8", 0, 0, {"IO_CLASS": "wire_bond"},
                       [_pad(5, 5, net="A"), _pad(5, 5, net="B")])
    board = _board([split, _footprint("J9", 0, 0, {"IO_CLASS": "wirebond"}, [_pad(5, 5)])])
    with pytest.raises(ValueError, match="different nets"):
        chiplet_writer.preflight_carriers(_board([split]), None)
    with pytest.raises(ValueError, match="J9 has an unrecognized IO_CLASS"):
        chiplet_writer.preflight_carriers(board, None)
    with pytest.raises(ValueError, match="J9 has an unrecognized IO_CLASS"):
        chiplet_writer.write_io_pads_json(board, str(tmp_path / "io_pads.json"))


def test_a_board_without_carriers_writes_nothing(tmp_path):
    out = tmp_path / "io_pads.json"
    assert chiplet_writer.write_io_pads_json(_board([]), str(out)) == 0
    assert not out.exists()


# --- the reader takes it -----------------------------------------------------

@pytest.mark.needs_sibling_sources
def test_hyp_to_gds_places_the_manifest_pads(tmp_path):
    """The worker reads the envelope as it read the bare list: the pads it
    supports (wire bonds), at the manifest's coordinates on its grid and the
    manifest's size."""
    pytest.importorskip("klayout.db")
    pytest.importorskip("yaml")
    import hyp_to_gds as h
    lyp = Path(h._find_default_lyp())
    if not lyp.exists():
        pytest.skip("interposer LYP not found: %s" % lyp)
    path = tmp_path / "io_pads.json"
    assert chiplet_writer.write_io_pads_json(_board(_carriers()), str(path)) == 3
    gen = h.GDSGenerator(h.LayerMap(str(lyp)), "TOP", "METRIC", [], None)
    placed = gen.add_io_pads(h._io_pads_manifest.read(str(path)))
    assert [(p["ref"], p["x_um"], p["y_um"], p["size_x_um"], p["size_y_um"])
            for p in placed] == [("J1", gen._snap_um(1234.567), gen._snap_um(-2345.678),
                                  80.0, 60.0)]


@pytest.fixture
def cfio():
    """The vendored chiplet_format_io the reader checks versions with, its
    warn-once memory cleared. It imports PyYAML; io_pads_manifest does not
    until it reads."""
    pytest.importorskip("yaml")
    from chiplet_export import vendored
    module = vendored.chiplet_format_io()
    assert Path(module.__file__).is_relative_to(VENDOR), module.__file__
    module._reset_version_warnings()
    return module


def test_the_envelope_the_writer_stamps_is_the_one_the_reader_takes(
        tmp_path, cfio):
    path = tmp_path / "io_pads.json"
    assert chiplet_writer.write_io_pads_json(_board(_carriers()), str(path)) == 3
    written = json.loads(path.read_text(encoding="utf-8"))["io_pads"]
    assert io_pads_manifest.read(str(path)) == written


def test_the_reader_names_the_pinned_schema_and_its_board_frame():
    schema = _pinned_schema()["properties"]
    assert io_pads_manifest.SCHEMA == schema["schema"]["const"]
    assert io_pads_manifest.FRAME in schema["frame"]["enum"]
    assert re.match(schema["version"]["pattern"], io_pads_manifest.VERSION)


@pytest.mark.parametrize("version", ["1.0", "1.0.0", "1.0.7"])
def test_a_major_1_manifest_is_read(tmp_path, cfio, version):
    notes = []
    path = write_io_pads(tmp_path / "io_pads.json", [{"ref": "J1"}],
                         version=version)
    assert io_pads_manifest.read(str(path), on_warn=notes.append) == [
        {"ref": "J1"}]
    assert notes == []


def test_a_newer_minor_is_read_with_a_note(tmp_path, cfio):
    notes = []
    path = write_io_pads(tmp_path / "io_pads.json", [], version="1.3")
    with pytest.warns(cfio.ContractVersionWarning):
        assert io_pads_manifest.read(str(path),
                                     on_warn=notes.append) == []
    assert len(notes) == 1 and "newer than the supported '1.0'" in notes[0]


def test_a_newer_minor_is_read_under_warnings_as_errors(tmp_path, cfio):
    """The policy accepts it; -W error turns the reader's stdlib warning
    into an exception, which must not refuse or crash the run."""
    notes = []
    path = write_io_pads(tmp_path / "io_pads.json", [], version="1.3")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert io_pads_manifest.read(str(path),
                                     on_warn=notes.append) == []
    assert len(notes) == 1


@pytest.mark.parametrize("envelope, cause", [
    ({"schema": None, "version": None, "frame": None},
     r" has no schema, version or frame; io_pads\.schema\.json requires"),
    ({"schema": None}, r" has schema None, not 'chiplet-io-pad-manifest'$"),
    ({"schema": "chiplet-io-pads"}, r" has schema 'chiplet-io-pads', not "),
    ({"version": None}, r": io_pads\.json: missing required key: version$"),
    ({"version": "2.0"}, r": io_pads\.json: unsupported version '2\.0'"),
    ({"version": "1"}, r": io_pads\.json: malformed version '1'"),
    ({"version": 1.0}, r": io_pads\.json: malformed version 1\.0"),
    ({"frame": None}, r" is in frame None; hyp_to_gds reads only "),
    ({"frame": "gds_bbox_corner_um"},
     r" is in frame 'gds_bbox_corner_um'; hyp_to_gds reads only "
     r"'board_absolute_ynegated_um' and never converts$"),
], ids=["bare", "no-schema", "other-schema", "no-version", "major-2",
        "no-minor", "number", "no-frame", "canonical-frame"])
def test_an_envelope_the_worker_does_not_read_is_refused(tmp_path, cfio,
                                                         envelope, cause):
    path = write_io_pads(tmp_path / "io_pads.json", [{"ref": "J1"}],
                         **envelope)
    with pytest.raises(IoPadsRefused,
                       match="^" + re.escape(str(path)) + cause):
        io_pads_manifest.read(str(path))


@pytest.mark.parametrize("text, cause", [
    (None, r"^cannot read the --io-pads manifest .*: \[Errno 2\]"),
    ("{", r"^cannot read the --io-pads manifest "),
    ("[]", r" is not a JSON object$"),
    (json.dumps(ENVELOPE), r" has no io_pads list$"),
    (json.dumps(dict(ENVELOPE, io_pads={})), r" has no io_pads list$"),
], ids=["missing", "not-json", "a-list", "no-pads", "pads-not-a-list"])
def test_a_file_that_is_no_manifest_is_refused(tmp_path, cfio, text, cause):
    path = tmp_path / "io_pads.json"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    with pytest.raises(IoPadsRefused, match=cause):
        io_pads_manifest.read(str(path))
