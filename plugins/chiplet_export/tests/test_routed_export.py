# SPDX-License-Identifier: GPL-3.0-or-later
"""The export from the routed documents, through the CLI, up to the worker.

The worker is replaced: these cases hold what the export hands it (the argv
and the staged intermediates) and every refusal the export makes before any
output exists. KiCad is not imported anywhere on this path, so a pcbnew
import fails the case.
"""

import importlib.abc
import importlib.util
import json
import shutil
import sys
import types
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2]
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))

from chiplet_export.routed import write_intermediates  # noqa: E402
from chiplet_export.writers.connection_stacks import (  # noqa: E402
    InterconnectManifestSnapshot, _manifest_reader,
)

PLUGIN_ROOT = PLUGINS / "chiplet_export"
CLI_SOURCE = PLUGIN_ROOT / "chiplet_export_cli.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "routed_t03"
INPUTS = FIXTURE / "inputs"
DOCUMENT = INPUTS / "routed-geometry.json"
CHIPLET = INPUTS / "design.chiplet"
IO_PADS = INPUTS / "routed-io-pads.json"
NAME = "cf_typical_03_from_schematic"
WORKER = "/worker/hyp_to_gds.py"

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
def worker(monkeypatch, tmp_path):
    """The worker's stand-in: records each argv and the bytes of every file
    it names, as they are when the worker would start."""
    from chiplet_export.pipeline import discovery, runner
    monkeypatch.setattr(discovery, "find_worker_python",
                        lambda *_a, **_kw: "/worker/python")
    monkeypatch.setattr(discovery, "find_hyp_to_gds", lambda *_a, **_kw: WORKER)
    monkeypatch.setattr(discovery, "find_adk_drc_runner",
                        lambda *_a, **_kw: "/adk/run_drc.py")
    calls = []

    def run_async(command, **_kwargs):
        files = {}
        for arg in command:
            for part in arg.replace("=", ",").split(","):
                if part.startswith("/") and Path(part).is_file():
                    files[part] = Path(part).read_bytes()
        calls.append(types.SimpleNamespace(argv=list(command), files=files))
        return runner.RunResult(exit_code=1, stdout="", stderr="", cancelled=False, timed_out=False)

    monkeypatch.setattr(runner, "run_async", run_async)
    # The worker's connection-method report: every method drawable.
    from chiplet_export.pipeline import orchestrator
    monkeypatch.setattr(
        orchestrator, "query_connection_availability",
        lambda *_a, **_kw: (types.SimpleNamespace(get=lambda *_k: None), "methods"))
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


def _routed_argv(tmp_path, *, document=DOCUMENT, chiplet=CHIPLET,
                 io_pads=IO_PADS, extra=()):
    return ["--board", str(_board(tmp_path)), "--output-dir", str(tmp_path / "out"),
            "--system-chiplet", str(chiplet), "--routed-geometry", str(document),
            "--name", NAME, "--io-pads", str(io_pads), *extra]


def _worker_calls(calls):
    return [c for c in calls if len(c.argv) > 1 and c.argv[1] == WORKER]


@needs_manifest
def test_the_routed_cli_hands_the_worker_the_document_and_no_hyp(
        tmp_path, no_kicad, worker, capsys):
    _cli().main(_routed_argv(tmp_path, extra=[
        "--interposer-adapter", "intm4tm2", "--interconnect-adapter", "ihp_cupillar"]))
    out = capsys.readouterr().out
    (call,) = _worker_calls(worker)
    argv = call.argv
    assert argv[1:4] == [WORKER, "--routed-geometry", str(DOCUMENT)]
    assert not any(arg.endswith(".hyp") for arg in argv)
    assert "--die-connections" in argv
    assert argv[argv.index("--die-connections") + 1] == "U1=cupillar_opt1,U2=cupillar_opt1"
    assert "--die-outline-exempt=U1,U2" in argv
    assert argv[argv.index("--system-chiplet") + 1] == str(CHIPLET)
    assert "hyp_path:            \n" in out
    assert "the board is not opened" in out
    assert not (tmp_path / "out" / ("%s.hyp" % NAME)).exists()


@needs_manifest
def test_the_staged_intermediates_are_the_routed_ones(tmp_path, no_kicad, worker):
    """What the worker would read is what write_intermediates writes."""
    _cli().main(_routed_argv(tmp_path, extra=[
        "--interposer-adapter", "intm4tm2", "--interconnect-adapter", "ihp_cupillar"]))
    (call,) = _worker_calls(worker)
    ref = tmp_path / "ref"
    (ref / "pins").mkdir(parents=True)
    expected = write_intermediates(
        str(DOCUMENT), str(CHIPLET), str(IO_PADS),
        chiplet_output=str(ref / "x.chiplet"), pins_dir=str(ref / "pins"),
        board_name=NAME,
        interconnect_manifest=InterconnectManifestSnapshot(_manifest_reader().load_manifest()),
        interposer_adapter="intm4tm2", interconnect_adapter="ihp_cupillar")
    staged = {Path(p).name: data for p, data in call.files.items()}
    for ref_name, path in expected.pin_lists.items():
        assert staged["%s_pins.json" % ref_name] == Path(path).read_bytes()
    io_pads = call.argv[call.argv.index("--io-pads") + 1]
    assert call.files[io_pads] == IO_PADS.read_bytes()
    chiplet = Path(expected.chiplet).read_bytes()
    assert chiplet in call.files.values(), "the staged .chiplet is not the routed one"
    assert b'adapter: "ihp_cupillar"' in chiplet


def _refused(tmp_path, capsys, argv, expected_code=1):
    code = _cli().main(argv)
    out = capsys.readouterr()
    assert code == expected_code, out
    assert not (tmp_path / "out").exists()
    return out.out + out.err


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


@needs_manifest
def test_a_carrier_off_its_stack_layer_is_refused(tmp_path, no_kicad, worker, capsys):
    """The archive puts the carrier on F.Cu = TopMetal2; a document whose top
    metal is another layer fails the carrier layer check."""
    doc = json.loads(DOCUMENT.read_text())
    doc["stack"][-1]["metal"] = "Metal5"
    document = tmp_path / "routed-geometry.json"
    document.write_text(json.dumps(doc))
    text = _refused(tmp_path, capsys, _routed_argv(tmp_path, document=document))
    assert "Carrier pad domains refused" in text and "carrier layer" in text
    assert _worker_calls(worker) == []


@needs_manifest
def test_the_routed_run_hands_the_worker_no_nofill_and_no_cmim_sidecar(
        tmp_path, no_kicad, worker):
    _cli().main(_routed_argv(tmp_path))
    (call,) = _worker_calls(worker)
    flags = [arg.split("=")[0] for arg in call.argv if arg.startswith("--")]
    assert "--nofill-regions" not in flags
    assert not any(flag.startswith("--cmim") for flag in flags)


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


@pytest.mark.parametrize("flag", ["--name", "--io-pads"])
def test_a_routed_flag_without_the_document_is_refused(tmp_path, no_kicad, flag):
    argv = ["--board", str(_board(tmp_path)), "--output-dir", str(tmp_path / "out"),
            flag, "x"]
    with pytest.raises(SystemExit) as exc:
        _cli().main(argv)
    assert exc.value.code == 2
