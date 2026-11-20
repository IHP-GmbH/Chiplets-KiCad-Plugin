# SPDX-License-Identifier: GPL-3.0-or-later
"""The routed intermediates against the board path's oracle, on host Python.

fixtures/routed_t03/oracle/ is what the board path computed from the draft
board with real pcbnew (make_oracle.py). Here the routed path computes the
same from the documents alone, and every byte must match. The board-side
comparison of the facts themselves needs pcbnew: test_routed_board_parity.py.
"""

import copy
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2]
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))

from chiplet_export.routed import (  # noqa: E402
    RoutedRefused, die_connections, die_pin_lists, die_thicknesses,
    facts_from_documents, read_document, write_intermediates,
)
from chiplet_export.writers.connection_stacks import (  # noqa: E402
    InterconnectManifestSnapshot, _manifest_reader, emit_connection_stacks_block,
)
from chiplet_export import io_pads_manifest, vendored  # noqa: E402
from chiplet_export.routed.source import RoutedSource  # noqa: E402

from .routed_t03_stage import stage  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "routed_t03"
INPUTS = FIXTURE / "inputs"
ORACLE = FIXTURE / "oracle"
DOCUMENT = INPUTS / "routed-geometry.json"
CHIPLET = INPUTS / "design.chiplet"
IO_PADS = INPUTS / "routed-io-pads.json"


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oracle = _load_module("routed_t03_make_oracle", FIXTURE / "make_oracle.py")

# The routed path is held against the interconnect manifest, found as the
# export finds it (env var or sibling checkout). On a lone checkout, as on a
# bare CI runner, the cases that read it skip; the rest still run.
try:
    _manifest_reader()
    _HAVE_MANIFEST = True
except ImportError:
    _HAVE_MANIFEST = False

needs_manifest = pytest.mark.skipif(
    not _HAVE_MANIFEST,
    reason="interconnect_pdk manifest not discoverable on this checkout")


def manifest_snapshot():
    """The interconnect manifest, found as the export finds it."""
    return InterconnectManifestSnapshot(_manifest_reader().load_manifest())


def oracle_bytes(name):
    return (ORACLE / name).read_bytes()


def assert_same_bytes(name, actual, expected):
    """Equal, or fail on the first differing line, record order included."""
    if actual == expected:
        return
    a_lines = actual.decode("utf-8").splitlines()
    e_lines = expected.decode("utf-8").splitlines()
    for number, (a, e) in enumerate(zip(a_lines, e_lines), 1):
        if a != e:
            pytest.fail("%s differs at line %d:\n  routed: %r\n  oracle: %r"
                        % (name, number, a, e))
    pytest.fail("%s differs in length: routed %d lines, oracle %d"
                % (name, len(a_lines), len(e_lines)))


def assert_count_floor():
    """An emptied or truncated oracle fails here, before any comparison."""
    pins = [p for p in oracle.PIN_LISTS if (ORACLE / p).is_file()]
    assert pins == ["U1_pins.json", "U2_pins.json"]
    for name in pins:
        assert json.loads(oracle_bytes(name))["pins"], name
    text = oracle_bytes(oracle.CHIPLET).decode("utf-8")
    assert text.count("        io_class: ") == 6
    assert text.count("    type: die\n") == 2
    assert len(json.loads(oracle_bytes(oracle.CONNECTIONS))) == 2
    assert "      thickness: 260.810000\n" in text
    assert json.loads(oracle_bytes(oracle.COUNTS))["io_pads"] == 6


def documents():
    document = read_document(str(DOCUMENT))
    chiplet = vendored.chiplet_format_io().load(str(CHIPLET))
    io_pads = io_pads_manifest.read(str(IO_PADS), frame="gds_bbox_corner_um")
    return document, chiplet, io_pads


def run_routed(tmp_path, **overrides):
    out = tmp_path / "out"
    (out / "pins").mkdir(parents=True)
    kwargs = dict(chiplet_output=str(out / oracle.CHIPLET), pins_dir=str(out / "pins"),
                  board_name=oracle.BOARD_NAME,
                  interconnect_manifest=manifest_snapshot())
    kwargs.update(overrides)
    document = kwargs.pop("document", str(DOCUMENT))
    chiplet = kwargs.pop("chiplet", str(CHIPLET))
    io_pads = kwargs.pop("io_pads", str(IO_PADS))
    return out, write_intermediates(document, chiplet, io_pads, **kwargs)


def test_count_floor():
    assert_count_floor()


@needs_manifest
def test_the_oracle_and_this_host_read_one_connection_library():
    """The connection_stacks block is the shared emitter's, from whatever
    manifest the host has. If this fails, the .chiplet comparison is about
    the manifest, not about the routed path."""
    block = emit_connection_stacks_block(manifest_snapshot())
    assert block and block in oracle_bytes(oracle.CHIPLET).decode("utf-8"), (
        "the interconnect manifest found here writes a different "
        "connection_stacks block than the oracle's")


@needs_manifest
def test_the_routed_path_writes_the_board_paths_bytes(tmp_path):
    """The .chiplet, the pin lists, the connections and the thicknesses."""
    assert_count_floor()
    out, result = run_routed(tmp_path)
    normalise = lambda data: oracle.normalise(data, INPUTS)  # noqa: E731

    assert_same_bytes(oracle.CHIPLET,
                      normalise((out / oracle.CHIPLET).read_bytes()),
                      oracle_bytes(oracle.CHIPLET))
    assert sorted(result.pin_lists) == ["U1", "U2"]
    for ref, path in result.pin_lists.items():
        name = "%s_pins.json" % ref
        assert_same_bytes(name, normalise(Path(path).read_bytes()), oracle_bytes(name))
    assert_same_bytes(oracle.CONNECTIONS, oracle._json_bytes(result.die_connections),
                      oracle_bytes(oracle.CONNECTIONS))
    assert_same_bytes(oracle.THICKNESSES, oracle._json_bytes(result.die_thicknesses),
                      oracle_bytes(oracle.THICKNESSES))
    counts = json.loads(oracle_bytes(oracle.COUNTS))
    assert (result.io_pads, result.cmim_devices) == (counts["io_pads"],
                                                     counts["cmim_devices"])
    assert counts["nofill_regions"] == 0


@needs_manifest
def test_the_routed_source_writes_the_board_paths_chiplet_under_the_flows_flags(tmp_path):
    """The flow gives both paths --interconnect-adapter. On the board path the
    flag steers the assembly DRC and the intermediate .chiplet gains no
    interconnect block, since the draft project sets no INTERCONNECT_ADAPTER;
    the routed source, given the same flags, writes the same bytes."""
    assert_count_floor()
    source = RoutedSource(str(stage(tmp_path / "staged")), str(CHIPLET), str(IO_PADS),
                          name=oracle.BOARD_NAME, interposer_adapter="intm4tm2",
                          interconnect_adapter="ihp_cupillar")
    source.preflight(manifest_snapshot())
    path = tmp_path / oracle.CHIPLET
    assert source.write_chiplet(str(path), interconnect_manifest=manifest_snapshot(),
                                export_report=None)
    assert_same_bytes(oracle.CHIPLET, oracle.normalise(path.read_bytes(), INPUTS),
                      oracle_bytes(oracle.CHIPLET))


@needs_manifest
def test_the_pure_functions_return_what_write_intermediates_wrote(tmp_path):
    document, chiplet, _io_pads = documents()
    _out, result = run_routed(tmp_path)
    assert die_connections(document, chiplet) == result.die_connections
    assert die_thicknesses(document, chiplet) == result.die_thicknesses
    for ref, sidecar in die_pin_lists(document, chiplet).items():
        assert json.loads(Path(result.pin_lists[ref]).read_text()) == sidecar


def test_the_routed_io_pads_are_the_board_paths(tmp_path):
    """The projector's manifest is the board sidecar, entry for entry."""
    routed = io_pads_manifest.read(str(IO_PADS), frame="gds_bbox_corner_um")
    board = json.loads(oracle_bytes(oracle.IO_PADS))["io_pads"]
    assert len(routed) == len(board) == 6
    for r, b in zip(routed, board):
        for key in ("ref", "io_class", "net", "layer", "size_x_um", "size_y_um"):
            assert r[key] == b[key], (r["ref"], key)
        for key in ("x_um", "y_um"):
            assert abs(r[key] - b[key]) <= 0.001, (r["ref"], key)


def test_the_document_is_the_writers_and_binds_these_inputs():
    """The routed geometry is interposer-pnr's, written from this run: it
    binds this .chiplet and these pin lists, and names each layout by the
    board's GDS_FILE text. No checkout path is left in it."""
    text = DOCUMENT.read_text()
    assert "/home/" not in text and "/tmp/" not in text
    document = json.loads(text)
    assert document["producer"]["tool"] == "interposer-pnr"
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    assert document["inputs"]["chiplet"]["sha256"] == digest(CHIPLET)
    pins = {"U1": "PAD_FRAME_L", "U2": "PAD_FRAME_R"}
    board = (INPUTS / "cf_typical_03_from_schematic.draft.kicad_pcb").read_text()
    for device in document["devices"]:
        die = pins[device["ref"]]
        assert (document["inputs"]["pins"][device["ref"]]["sha256"]
                == digest(INPUTS / "pins" / ("%s.pins.json" % die)))
        assert '(property "GDS_FILE" "%s"' % device["layout"]["path"] in board


def test_a_thickness_goes_through_the_boards_text():
    """(d) parses ``"%g" % t``, the text the board carries, not t itself."""
    document, chiplet, _io_pads = documents()
    chiplet = copy.deepcopy(chiplet)
    for component in chiplet["components"]:
        if component["id"] == "U1":
            component["dimensions"]["thickness"] = 123.4567891
        if component["id"] == "U2":
            component["dimensions"]["thickness"] = 0.0
    assert die_thicknesses(document, chiplet) == {"U1": 123.457}


def test_rotation_is_stored_as_kicad_stores_it():
    document, chiplet, io_pads = documents()
    document = copy.deepcopy(document)
    document["devices"][1]["rotation_deg"] = 270.0
    document["devices"][0]["rotation_deg"] = -180.0
    facts = facts_from_documents(document, chiplet, io_pads,
                                 board_name=oracle.BOARD_NAME)
    assert [d.rotation_deg for d in facts.dies] == [180.0, -90.0]


def test_a_position_rounds_to_the_boards_nanometer():
    document, chiplet, io_pads = documents()
    document = copy.deepcopy(document)
    document["devices"][1]["origin"][0] += 0.3335
    facts = facts_from_documents(document, chiplet, io_pads,
                                 board_name=oracle.BOARD_NAME)
    # (2182 + 50) um and 333.5 nm, rounded to the nanometer the board holds.
    assert facts.dies[1].x_iu in (2232333, 2232334)


# --- refusals, each before any file is written -----------------------------

def _variant(tmp_path, name, edit):
    data = json.loads(DOCUMENT.read_text())
    edit(data)
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


def _assert_refused_unwritten(tmp_path, match, **overrides):
    with pytest.raises(RoutedRefused, match=match):
        run_routed(tmp_path, **overrides)
    out = tmp_path / "out"
    assert not (out / oracle.CHIPLET).exists()
    assert not any((out / "pins").iterdir())


@needs_manifest
def test_refuses_a_document_of_another_major(tmp_path):
    doc = _variant(tmp_path, "v2.json", lambda d: d.update(version="2.0"))
    _assert_refused_unwritten(tmp_path, "unsupported version '2.0'", document=doc)


@needs_manifest
def test_refuses_a_device_without_its_archive(tmp_path):
    doc = _variant(tmp_path, "nopd.json", lambda d: d["devices"][1].pop("pad_domains"))
    _assert_refused_unwritten(tmp_path, "device U2 has no pad_domains", document=doc)


@needs_manifest
def test_refuses_a_chiplet_the_document_does_not_bind(tmp_path):
    other = tmp_path / "design.chiplet"
    shutil.copy(CHIPLET, other)
    with open(other, "a") as f:
        f.write("# edited\n")
    _assert_refused_unwritten(tmp_path, "is not the .chiplet the routed geometry "
                              "was written from", chiplet=str(other))


@needs_manifest
def test_refuses_a_device_that_is_no_component(tmp_path):
    doc = _variant(tmp_path, "ref.json", lambda d: d["devices"][0].update(ref="U9"))
    _assert_refused_unwritten(tmp_path, "device U9 is no component of the .chiplet",
                              document=doc)


@needs_manifest
def test_refuses_an_explicit_cmim_sidecar(tmp_path):
    _assert_refused_unwritten(tmp_path, "CMIM sidecar",
                              cmim_devices_json=str(tmp_path / "cmim.json"))


@needs_manifest
def test_refuses_an_io_pads_manifest_in_the_board_frame(tmp_path):
    _assert_refused_unwritten(tmp_path, "board_absolute_ynegated_um",
                              io_pads=str(ORACLE / oracle.IO_PADS))


@needs_manifest
def test_refuses_an_archive_that_names_another_die(tmp_path):
    def swap(d):
        d["devices"][0]["pad_domains"], d["devices"][1]["pad_domains"] = (
            d["devices"][1]["pad_domains"], d["devices"][0]["pad_domains"])
    doc = _variant(tmp_path, "swap.json", swap)
    _assert_refused_unwritten(tmp_path, "device U1: .*component differs", document=doc)


@needs_manifest
def test_refuses_a_die_of_the_chiplet_left_unplaced(tmp_path):
    doc = _variant(tmp_path, "drop.json", lambda d: d["devices"].pop(1))
    _assert_refused_unwritten(tmp_path, "die U2 of the .chiplet has no device",
                              document=doc)


@needs_manifest
def test_refuses_an_outline_off_the_frames_origin(tmp_path):
    def shift(d):
        d["outline"] = [[x + 1000.0, y] for x, y in d["outline"]]
    doc = _variant(tmp_path, "shift.json", shift)
    _assert_refused_unwritten(tmp_path, r"lower-left is \(1000, 0\)", document=doc)


def _io_pads_variant(tmp_path, edit):
    data = json.loads(IO_PADS.read_text())
    edit(data["io_pads"])
    path = tmp_path / "io_pads.json"
    path.write_text(json.dumps(data))
    return str(path)


@needs_manifest
@pytest.mark.parametrize("edit, match", [
    (lambda pads: pads[0].update(io_class="bogus"), "io_class 'bogus'"),
    (lambda pads: pads[0].update(layer="Metal1"), "is on 'Metal1'"),
    (lambda pads: pads[1].update(ref=pads[0]["ref"]), "is listed twice"),
    (lambda pads: pads[0].pop("layer"), r"io_pads\[0\] has no layer"),
], ids=["io-class", "layer", "duplicate-ref", "no-layer"])
def test_refuses_a_carrier_the_board_path_refuses(tmp_path, edit, match):
    _assert_refused_unwritten(tmp_path, match, io_pads=_io_pads_variant(tmp_path, edit))


def _method_specs(**change):
    """The manifest's methods, as the board preflight passes them, with
    ``change`` applied to the method the fixture's archives name."""
    methods = copy.deepcopy(manifest_snapshot().methods)
    method = documents()[0]["devices"][0]["pad_domains"]["interconnect"]["method"]
    methods[method].update(change)
    return methods


@needs_manifest
def test_an_archive_is_held_against_the_manifest_method(tmp_path):
    out, _result = run_routed(tmp_path, method_specs=_method_specs())
    assert (out / oracle.CHIPLET).is_file()
    _assert_refused_unwritten(tmp_path / "b", "body_diameter_um differs from selected",
                              method_specs=_method_specs(body_diameter_um=45.0))


@needs_manifest
def test_refuses_a_missing_pin_list_folder(tmp_path):
    with pytest.raises(RoutedRefused, match="pin list folder"):
        run_routed(tmp_path, pins_dir=str(tmp_path / "absent"))
    assert not (tmp_path / "out" / oracle.CHIPLET).exists()


def test_a_negative_zero_rotation_is_written_as_zero():
    document, chiplet, io_pads = documents()
    document = copy.deepcopy(document)
    document["devices"][0]["rotation_deg"] = -0.0
    facts = facts_from_documents(document, chiplet, io_pads,
                                 board_name=oracle.BOARD_NAME)
    assert str(facts.dies[0].rotation_deg) == "0.0"


# --- no pcbnew, no kicad-cli ------------------------------------------------

NO_BOARD_CHILD = r'''
import json, os, sys
seen = []

class Recorder:
    """Records every attempt to import pcbnew, whether or not it exists."""
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("pcbnew", "_pcbnew"):
            seen.append(name)
        return None

sys.meta_path.insert(0, Recorder())
from chiplet_export.routed import write_intermediates
from chiplet_export.writers.connection_stacks import (
    InterconnectManifestSnapshot, _manifest_reader)
out = sys.argv[1]
os.makedirs(os.path.join(out, "pins"))
result = write_intermediates(
    sys.argv[2], sys.argv[3], sys.argv[4],
    chiplet_output=os.path.join(out, "x.chiplet"), pins_dir=os.path.join(out, "pins"),
    board_name="t", interconnect_manifest=InterconnectManifestSnapshot(
        _manifest_reader().load_manifest()))
print(json.dumps({"pcbnew": seen, "loaded": "pcbnew" in sys.modules,
                  "pin_lists": sorted(result.pin_lists)}))
'''


@needs_manifest
def test_the_routed_path_imports_no_pcbnew_and_runs_no_kicad_cli(tmp_path):
    """The recorder sees an import attempt even where pcbnew is not
    installed, so this holds on a plain host and in the KiCad image alike."""
    import os
    import subprocess
    shim = tmp_path / "bin"
    shim.mkdir()
    calls = tmp_path / "kicad-cli.calls"
    (shim / "kicad-cli").write_text("#!/bin/sh\necho \"$@\" >> %s\nexit 1\n" % calls)
    (shim / "kicad-cli").chmod(0o755)
    env = dict(os.environ, PATH=os.pathsep.join((str(shim), os.environ.get("PATH", ""))))
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(PLUGINS), env.get("PYTHONPATH"))))
    proc = subprocess.run(
        [sys.executable, "-B", "-c", NO_BOARD_CHILD, str(tmp_path / "out"),
         str(DOCUMENT), str(CHIPLET), str(IO_PADS)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    record = json.loads(proc.stdout.strip().splitlines()[-1])
    assert record == {"pcbnew": [], "loaded": False, "pin_lists": ["U1", "U2"]}
    assert not calls.exists()
