# SPDX-License-Identifier: GPL-3.0-or-later
"""The export from the routed documents, through the CLI, up to the worker.

The worker is replaced: these cases hold what the export hands it (the argv
and the staged intermediates) and every refusal the export makes before any
output exists. KiCad is not imported anywhere on this path: in process a
pcbnew import fails the case, and one case runs the whole CLI in a child
process that records any pcbnew import and any kicad-cli call.

The routed geometry names its die layouts by the board's GDS_FILE text,
four folders up, and its pin lists under ``<RUN>``; ``staged`` lays the
fixture out that way in a temporary folder.
"""

import importlib.abc
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2]
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))

from chiplet_export import connection_methods  # noqa: E402
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
DIES = {"U1": "PAD_FRAME_L", "U2": "PAD_FRAME_R"}
ADAPTERS = ["--interposer-adapter", "intm4tm2", "--interconnect-adapter", "ihp_cupillar"]

try:
    _manifest_reader()
    _HAVE_MANIFEST = True
except ImportError:
    _HAVE_MANIFEST = False

needs_manifest = pytest.mark.skipif(
    not _HAVE_MANIFEST,
    reason="interconnect_pdk manifest not discoverable on this checkout")


def staged(tmp_path, edit=None):
    """The fixture laid out as a run: dies/ at the root, the document four
    folders below it, ``<RUN>`` resolved. ``edit(document)`` may change the
    document first. Returns the document's path."""
    root = tmp_path / "run"
    for die in DIES.values():
        (root / "dies" / die).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURE / "dies" / die / "layout.gds",
                        root / "dies" / die / "layout.gds")
        shutil.copyfile(INPUTS / "pins" / ("%s.pins.json" % die),
                        root / "dies" / die / "pins.json")
    document = json.loads(DOCUMENT.read_text().replace("<RUN>", str(root)))
    if edit is not None:
        edit(document)
    path = root / "a" / "b" / "c" / "pnr" / "routed-geometry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n")
    return path


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

    def run_async(command, **_kwargs):
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
        interposer_adapter="intm4tm2", interconnect_adapter="ihp_cupillar")
    staged_files = {Path(p).name: data for p, data in call.files.items()}
    for ref_name, path in expected.pin_lists.items():
        assert staged_files["%s_pins.json" % ref_name] == Path(path).read_bytes()
    io_pads = call.argv[call.argv.index("--io-pads") + 1]
    assert call.files[io_pads] == IO_PADS.read_bytes()
    chiplet = Path(expected.chiplet).read_bytes()
    assert chiplet in call.files.values(), "the staged .chiplet is not the routed one"
    assert b'adapter: "ihp_cupillar"' in chiplet


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
    metal has another name (the copper renamed with it) fails the carrier
    layer check."""
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


@pytest.mark.parametrize("flag", ["--name", "--io-pads"])
def test_a_routed_flag_without_the_document_is_refused(tmp_path, no_kicad, flag):
    argv = ["--board", str(_board(tmp_path)), "--output-dir", str(tmp_path / "out"),
            flag, "x"]
    with pytest.raises(SystemExit) as exc:
        _cli().main(argv)
    assert exc.value.code == 2


# The whole CLI in a child process. A finder placed before anything is
# imported records every pcbnew import (and lets it fail), and a kicad-cli on
# PATH records every call; the worker python records its argv and reports
# every connection method drawable.
_CHILD = r"""
import importlib.abc, runpy, sys
record = sys.argv[1]
class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "pcbnew" or name.startswith("pcbnew."):
            with open(record, "a") as f:
                f.write(name + "\n")
            raise ImportError("recorded")
        return None
sys.meta_path.insert(0, Finder())
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _executable(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return path


@needs_manifest
def test_the_routed_cli_process_never_imports_pcbnew_or_runs_kicad_cli(tmp_path):
    records = tmp_path / "records"
    records.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "kicad-cli",
                '#!/bin/sh\necho "$@" >> "%s"\nexit 1\n' % (records / "kicad-cli"))
    report = json.dumps({"format": connection_methods.FORMAT,
                         "version": connection_methods.VERSION, "methods": []})
    worker_python = _executable(
        tmp_path / "worker-python",
        '#!/bin/sh\necho "$@" >> "%s"\n'
        'case "$1" in *connection_methods.py) printf %%s \'%s\'; exit 0;; esac\n'
        'exit 1\n' % (records / "worker", report))
    env = dict(os.environ, KICAD_CHIPLET_PYTHON=str(worker_python),
               PATH="%s%s%s" % (bin_dir, os.pathsep, os.environ.get("PATH", "")))
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(records / "pcbnew"), str(CLI_SOURCE),
         *_routed_argv(tmp_path)],
        env=env, capture_output=True, text=True, timeout=120, check=False)
    calls = ((records / "worker").read_text().splitlines()
             if (records / "worker").exists() else [])
    assert any("--routed-geometry" in line for line in calls), (
        result.stdout + result.stderr)
    assert not (records / "pcbnew").exists(), (records / "pcbnew").read_text()
    assert not (records / "kicad-cli").exists()
