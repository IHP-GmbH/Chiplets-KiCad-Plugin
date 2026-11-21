# SPDX-License-Identifier: GPL-3.0-or-later
"""The export from the routed documents, through the CLI, up to the worker.

The worker is replaced: these cases hold what the export hands it (the argv
and the staged intermediates) and every refusal the export makes before any
output exists. KiCad is not imported anywhere on this path: in process a
pcbnew import fails the case, and one case runs the whole CLI in a child
process that records any pcbnew import and any kicad-cli call.

The fixture is staged as a run (routed_t03_stage) so the layouts and pin
lists the routed geometry binds resolve.
"""

import importlib.abc
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2]
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))

from chiplet_export.routed import write_intermediates  # noqa: E402
from chiplet_export.routed.source import INPUTS_SCRIPT  # noqa: E402
from chiplet_export.writers.connection_stacks import (  # noqa: E402
    InterconnectManifestSnapshot, _manifest_reader,
)

from .routed_t03_stage import CHIPLET, DOCUMENT, IO_PADS  # noqa: E402
from .routed_t03_stage import stage as staged  # noqa: E402

PLUGIN_ROOT = PLUGINS / "chiplet_export"
CLI_SOURCE = PLUGIN_ROOT / "chiplet_export_cli.py"
NAME = "cf_typical_03_from_schematic"
WORKER = "/worker/hyp_to_gds.py"
ADAPTERS = ["--interposer-adapter", "intm4tm2", "--interconnect-adapter", "ihp_cupillar"]

try:
    _manifest_reader()
    _HAVE_MANIFEST = True
except ImportError:
    _HAVE_MANIFEST = False

needs_manifest = pytest.mark.skipif(
    not _HAVE_MANIFEST,
    reason="interconnect_pdk manifest not discoverable on this checkout")


class _NoKiCad(importlib.abc.MetaPathFinder):
    """Fails, and records, any import of pcbnew."""

    def __init__(self):
        self.attempts = []

    def find_spec(self, name, path=None, target=None):
        if name == "pcbnew" or name.startswith("pcbnew."):
            self.attempts.append(name)
            raise ImportError("the routed path imported %s" % name)
        return None


@pytest.fixture
def no_kicad(monkeypatch):
    finder = _NoKiCad()
    monkeypatch.delitem(sys.modules, "pcbnew", raising=False)
    for name in ("chiplet_export.writers.chiplet_writer",
                 "chiplet_export.writers.hyperlynx_writer"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [finder] + sys.meta_path)
    yield finder
    assert finder.attempts == []


@pytest.fixture
def worker(monkeypatch):
    """The worker's stand-in: records each argv and the bytes of every file
    it names, as they are when the worker would start."""
    from chiplet_export.pipeline import discovery, orchestrator, runner
    monkeypatch.setattr(discovery, "find_worker_python",
                        lambda *_a, **_kw: "/worker/python")
    monkeypatch.setattr(discovery, "find_hyp_to_gds", lambda *_a, **_kw: WORKER)
    monkeypatch.setattr(discovery, "find_adk_drc_runner",
                        lambda *_a, **_kw: "/adk/run_drc.py")
    # The worker's connection-method report names no method: all drawable.
    monkeypatch.setattr(orchestrator, "query_connection_availability",
                        lambda *_a, **_kw: ({}, "methods"))
    calls = []
    recorded = runner.run_async

    def run_async(command, **kwargs):
        if len(command) > 1 and command[1] == INPUTS_SCRIPT:
            # The routed inputs are read for real, by this interpreter.
            return recorded([sys.executable] + list(command[1:]), **kwargs)
        files = {}
        for arg in command:
            for part in arg.replace("=", ",").split(","):
                if part.startswith("/") and Path(part).is_file():
                    files[part] = Path(part).read_bytes()
        calls.append(types.SimpleNamespace(argv=list(command), files=files))
        return runner.RunResult(exit_code=1)

    monkeypatch.setattr(runner, "run_async", run_async)
    return calls


def _cli():
    spec = importlib.util.spec_from_file_location("_routed_cli_under_test", CLI_SOURCE)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def _board(tmp_path):
    """A board file that exists, and that no KiCad could open."""
    board = tmp_path / "unread.kicad_pcb"
    board.write_text("not a board\n")
    return board


def _routed_argv(tmp_path, *, document=None, chiplet=CHIPLET, io_pads=IO_PADS,
                 adapters=ADAPTERS, extra=()):
    if document is None:
        document = staged(tmp_path)
    return ["--board", str(_board(tmp_path)), "--output-dir", str(tmp_path / "out"),
            "--system-chiplet", str(chiplet), "--routed-geometry", str(document),
            "--name", NAME, "--io-pads", str(io_pads), *adapters, *extra]


def _worker_calls(calls):
    return [c for c in calls if len(c.argv) > 1 and c.argv[1] == WORKER]


@needs_manifest
def test_the_routed_cli_hands_the_worker_the_document_and_no_hyp(
        tmp_path, no_kicad, worker, capsys):
    document = staged(tmp_path)
    _cli().main(_routed_argv(tmp_path, document=document))
    out = capsys.readouterr().out
    (call,) = _worker_calls(worker)
    argv = call.argv
    assert argv[1:4] == [WORKER, "--routed-geometry", str(document)]
    assert not any(arg.endswith(".hyp") for arg in argv)
    assert argv[argv.index("--die-connections") + 1] == "U1=cupillar_opt1,U2=cupillar_opt1"
    assert "--die-outline-exempt=U1,U2" in argv
    assert argv[argv.index("--system-chiplet") + 1] == str(CHIPLET)
    assert "hyp_path:            \n" in out
    assert "the board is not opened" in out
    assert not (tmp_path / "out" / ("%s.hyp" % NAME)).exists()


@needs_manifest
def test_the_staged_intermediates_are_the_routed_ones(tmp_path, no_kicad, worker):
    """What the worker would read is what write_intermediates writes."""
    _cli().main(_routed_argv(tmp_path))
    (call,) = _worker_calls(worker)
    ref = tmp_path / "ref"
    (ref / "pins").mkdir(parents=True)
    expected = write_intermediates(
        str(DOCUMENT), str(CHIPLET), str(IO_PADS),
        chiplet_output=str(ref / "x.chiplet"), pins_dir=str(ref / "pins"),
        board_name=NAME,
        interconnect_manifest=InterconnectManifestSnapshot(_manifest_reader().load_manifest()),
        interposer_adapter="intm4tm2", interconnect_adapter="")
    staged_files = {Path(p).name: data for p, data in call.files.items()}
    for ref_name, path in expected.pin_lists.items():
        assert staged_files["%s_pins.json" % ref_name] == Path(path).read_bytes()
    io_pads = call.argv[call.argv.index("--io-pads") + 1]
    assert call.files[io_pads] == IO_PADS.read_bytes()
    chiplet = Path(expected.chiplet).read_bytes()
    assert chiplet in call.files.values(), "the staged .chiplet is not the routed one"
    # --interconnect-adapter steers the DRC; as on the board path, the
    # intermediate carries no interconnect block (the worker adds it).
    assert b"\ninterconnect:" not in chiplet and b'adapter: "intm4tm2"' in chiplet


@needs_manifest
def test_the_routed_run_hands_the_worker_no_nofill_and_no_cmim_sidecar(
        tmp_path, no_kicad, worker):
    _cli().main(_routed_argv(tmp_path))
    (call,) = _worker_calls(worker)
    flags = [arg.split("=")[0] for arg in call.argv if arg.startswith("--")]
    assert "--nofill-regions" not in flags
    assert not any(flag.startswith("--cmim") for flag in flags)


def _refused(tmp_path, capsys, argv, expected_code=1):
    code = _cli().main(argv)
    out = capsys.readouterr()
    assert code == expected_code, out
    assert not (tmp_path / "out").exists()
    return out.out + out.err


def _corrupt(path):
    path.write_bytes(path.read_bytes() + b"\0")


def _drop_board_name(document):
    del document["nets"][0]["board_name"]


def _empty_devices(document):
    document["devices"] = []


def _other_top_metal(document):
    """The archive puts the carrier on F.Cu = TopMetal2; a stack whose top
    metal has another name (the KiCad copper layer renamed with it) fails
    the carrier layer check."""
    renamed = json.loads(json.dumps({"stack": document["stack"],
                                     "nets": document["nets"]})
                         .replace('"TopMetal2"', '"TopMetal9"'))
    document.update(renamed)


REFUSALS = {
    # case: (document edit, staged file to corrupt, argv change, message)
    "net without board_name": (_drop_board_name, None, None, "board_name"),
    "empty devices": (_empty_devices, None, None, "devices"),
    "carrier off its stack layer": (_other_top_metal, None, None, "carrier layer"),
    "layout digest": (None, "dies/PAD_FRAME_L/layout.gds", None, "layout of U1"),
    "pin list digest": (None, "dies/PAD_FRAME_R/pins.json", None, "pin list of U2"),
    "no interposer adapter": (None, None, {"adapters": ADAPTERS[2:]}, "both adapters"),
    "no interconnect adapter": (None, None, {"adapters": ADAPTERS[:2]}, "both adapters"),
    "empty interposer adapter": (
        None, None, {"adapters": ["--interposer-adapter", ""] + ADAPTERS[2:]},
        "interposer adapter is empty"),
    "malformed interconnect adapter": (
        None, None, {"adapters": ADAPTERS[:2] + ["--interconnect-adapter", "a/b"]},
        "adapter"),
    "unknown interconnect adapter": (
        None, None, {"adapters": ADAPTERS[:2] + ["--interconnect-adapter", "acme_custom"]},
        "unknown interconnect adapter 'acme_custom'"),
}


@needs_manifest
@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_routed_refusal_comes_before_any_output(
        tmp_path, no_kicad, worker, capsys, case):
    edit, corrupt, change, message = REFUSALS[case]
    document = staged(tmp_path, edit)
    if corrupt:
        _corrupt(tmp_path / "run" / corrupt)
    text = _refused(tmp_path, capsys,
                    _routed_argv(tmp_path, document=document, **(change or {})))
    assert "error:" in text and message in text, text
    assert _worker_calls(worker) == []


@needs_manifest
def test_a_chiplet_the_document_does_not_bind_is_refused_before_any_output(
        tmp_path, no_kicad, worker, capsys):
    other = tmp_path / "design.chiplet"
    other.write_bytes(CHIPLET.read_bytes() + b"# edited\n")
    text = _refused(tmp_path, capsys, _routed_argv(tmp_path, chiplet=other))
    assert "Routed geometry refused" in text and "binds" in text
    assert _worker_calls(worker) == []


@needs_manifest
def test_an_io_pads_manifest_in_another_frame_is_refused(
        tmp_path, no_kicad, worker, capsys):
    doc = json.loads(IO_PADS.read_text())
    doc["frame"] = "board_absolute_ynegated_um"
    io_pads = tmp_path / "io.json"
    io_pads.write_text(json.dumps(doc))
    text = _refused(tmp_path, capsys, _routed_argv(tmp_path, io_pads=io_pads))
    assert "Routed geometry refused" in text
    assert _worker_calls(worker) == []


@pytest.mark.parametrize("missing", ["board", "document", "io_pads", "chiplet"])
def test_a_missing_routed_input_exits_2_before_any_output(
        tmp_path, no_kicad, worker, capsys, missing):
    argv = _routed_argv(tmp_path)
    flag = {"board": "--board", "document": "--routed-geometry",
            "io_pads": "--io-pads", "chiplet": "--system-chiplet"}[missing]
    argv[argv.index(flag) + 1] = str(tmp_path / "absent")
    _refused(tmp_path, capsys, argv, expected_code=2)
    assert worker == []


@pytest.mark.parametrize("drop", ["--name", "--io-pads", "--system-chiplet"])
def test_the_routed_path_needs_its_three_flags(tmp_path, no_kicad, drop):
    argv = _routed_argv(tmp_path)
    i = argv.index(drop)
    del argv[i:i + 2]
    with pytest.raises(SystemExit) as exc:
        _cli().main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("name", ["../x", "a/b", "..", "a\\b"])
def test_the_output_name_is_a_stem_not_a_path(tmp_path, no_kicad, worker, capsys, name):
    argv = _routed_argv(tmp_path)
    argv[argv.index("--name") + 1] = name
    with pytest.raises(SystemExit) as exc:
        _cli().main(argv)
    assert exc.value.code == 2
    assert "must be a file stem, not a path" in capsys.readouterr().err
    assert worker == [] and not (tmp_path / "out").exists()


def test_the_source_refuses_a_path_for_a_name():
    from chiplet_export.routed.document import RoutedRefused
    from chiplet_export.routed.source import RoutedSource
    with pytest.raises(RoutedRefused, match="file stem, not a path"):
        RoutedSource("d.json", "c.chiplet", "p.json", name="../x",
                     interposer_adapter="intm4tm2", interconnect_adapter="")


@pytest.mark.parametrize("flag", ["--name", "--io-pads"])
def test_a_routed_flag_without_the_document_is_refused(tmp_path, no_kicad, flag):
    argv = ["--board", str(_board(tmp_path)), "--output-dir", str(tmp_path / "out"),
            flag, "x"]
    with pytest.raises(SystemExit) as exc:
        _cli().main(argv)
    assert exc.value.code == 2
