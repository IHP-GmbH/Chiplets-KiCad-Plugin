# SPDX-License-Identifier: GPL-3.0-or-later
"""Both sources run through one export body and print the same lines.

The routed source and a board-shaped twin of it (the same intermediates, but a
.hyp written from the sealed typical_03 board run and handed to the worker)
each go through the CLI's export with the worker replaced by one that replays
the golden board run's worker output. Every stdout line must be the same on
both, once the workspace folder is masked, except the lines that say where the
inputs came from and how the worker was given its geometry; those differ by a
named list, and nothing else may. The routed path's ``hyp_path:`` line is
empty. The files staged for the worker are the same bytes, the .hyp aside.

No pcbnew is needed: the twin is the board source's shape (``writes_hyp``,
the board's origin labels), not a KiCad board.
"""

import difflib
import re
import shutil
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2]
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))

from chiplet_export.routed.source import RoutedSource  # noqa: E402

from .routed_t03_stage import BOARD_HYP, CHIPLET, FIXTURE, IO_PADS  # noqa: E402
from .routed_t03_stage import stage  # noqa: E402
from .test_routed_export import (  # noqa: E402
    NAME, WORKER, _cli, needs_manifest,
    no_kicad, worker,  # noqa: F401  (fixtures, used by name)
)

#: The golden board run's worker output, as its run log recorded it.
WORKER_LOG = FIXTURE / "inputs" / "board-path.worker.log"

class BoardShapedTwin(RoutedSource):
    """The routed source's intermediates in the board source's shape: the
    worker gets a .hyp, and the run log names the board as the origin."""

    writes_hyp = True
    routed_geometry = ""
    origin = "board"
    field_origin = "board fields"

    def describe(self):
        return None

    def write_hyperlynx(self, path):
        shutil.copyfile(BOARD_HYP, path)
        return True


def _replay(monkeypatch):
    """The worker stand-in prints the golden worker's lines and succeeds."""
    from chiplet_export.pipeline import runner
    recorded = runner.run_async
    lines = WORKER_LOG.read_text().splitlines()

    def run_async(command, on_stdout=None, **kwargs):
        result = recorded(command, on_stdout=on_stdout, **kwargs)
        if len(command) > 1 and command[1] == WORKER:
            for line in lines:
                if on_stdout:
                    on_stdout(line)
            return runner.RunResult(exit_code=0, stdout="\n".join(lines))
        return result

    monkeypatch.setattr(runner, "run_async", run_async)


def _args(document):
    """The routed run's flags, parsed by the CLI's own parser."""
    return _cli()._build_parser().parse_args([
        "--board", "unread.kicad_pcb", "--output-dir", "out",
        "--system-chiplet", str(CHIPLET), "--routed-geometry", str(document),
        "--name", NAME, "--io-pads", str(IO_PADS),
        "--interposer-adapter", "intm4tm2", "--interconnect-adapter", "ihp_cupillar",
        "--seal-ring"])


def _run(tmp_path, source, capsys):
    out_dir = tmp_path / "out"
    code = _cli()._export(source, _args(source.document_path), str(out_dir))
    text = capsys.readouterr().out
    return code, _masked(text, tmp_path)


def _masked(text, tmp_path):
    text = text.replace(str(tmp_path), "<TMP>")
    text = re.sub(r"/[^\s]*chiplet_export_[a-z0-9_]+", "<WORKSPACE>", text)
    text = re.sub(r"\d{8}_\d{6}_", "<STAMP>_", text)
    return text.splitlines()


#: The lines the sources print differently, by design, and nothing else.
SOURCE_LINES = {
    "board": [
        "  | Die outline layers (board fields): exempt, archived PinList size: U1, U2",
        "  | Writing Hyperlynx ...",
        "  | Auto-extracted 6 io_pad(s) from board",
        "  | Per-die connections from board fields: U1=cupillar_opt1, U2=cupillar_opt1",
        "  | Per-die thickness (um) from board fields: U1=750.0, U2=750.0",
        "  | Wrote Hyperlynx: <TMP>/out/%s.hyp" % NAME,
        "hyp_path:            <TMP>/out/%s.hyp" % NAME,
    ],
    "routed": [
        "  | Die outline layers (routed geometry): exempt, archived PinList size: U1, U2",
        "  | Routed geometry: <DOC> (the board is not opened)",
        "  | Auto-extracted 6 io_pad(s) from routed geometry",
        "  | Per-die connections from routed geometry: U1=cupillar_opt1, U2=cupillar_opt1",
        "  | Per-die thickness (um) from routed geometry: U1=750.0, U2=750.0",
        "hyp_path:            ",
    ],
}


def _worker_line(lines):
    return [line for line in lines if line.startswith("  | $ ") and WORKER in line]


@needs_manifest
def test_both_sources_print_the_same_lines(tmp_path, no_kicad, worker,
                                           monkeypatch, capsys):
    _replay(monkeypatch)
    runs = {}
    for kind, cls in (("board", BoardShapedTwin), ("routed", RoutedSource)):
        root = tmp_path / kind
        document = stage(root)
        source = cls(str(document), str(CHIPLET), str(IO_PADS), name=NAME,
                     interposer_adapter="intm4tm2", interconnect_adapter="ihp_cupillar")
        code, lines = _run(root, source, capsys)
        lines = [line.replace(str(document).replace(str(root), "<TMP>"), "<DOC>")
                 for line in lines]
        runs[kind] = (code, lines)
    assert runs["board"][0] == runs["routed"][0]

    board, routed = runs["board"][1], runs["routed"][1]
    # The worker's argv differs in its geometry input; compared below.
    board_cmd, routed_cmd = _worker_line(board), _worker_line(routed)
    assert len(board_cmd) == len(routed_cmd) == 1
    board_rest = [line for line in board if line not in board_cmd]
    routed_rest = [line for line in routed if line not in routed_cmd]

    removed, added = [], []
    for op in difflib.ndiff(board_rest, routed_rest):
        if op.startswith("- "):
            removed.append(op[2:])
        elif op.startswith("+ "):
            added.append(op[2:])
    assert sorted(removed) == sorted(SOURCE_LINES["board"])
    assert sorted(added) == sorted(SOURCE_LINES["routed"])

    # The worker's lines are relayed whole and in order (blank ones dropped).
    replayed = [line for line in _masked(WORKER_LOG.read_text(), tmp_path)
                if line.strip()]
    for lines in (board_rest, routed_rest):
        relayed = [line[4:] for line in lines if line.startswith("  | ")]
        start = relayed.index(replayed[0])
        assert relayed[start:start + len(replayed)] == replayed

    hyp = "<WORKSPACE>/%s.hyp" % NAME
    assert board_cmd[0].replace(" " + hyp + " ", " --routed-geometry <DOC> ", 1) \
        == routed_cmd[0].replace(" " + hyp + " ", " ", 1)


@needs_manifest
def test_both_sources_stage_the_same_files(tmp_path, no_kicad, worker, capsys):
    staged = {}
    for kind, cls in (("board", BoardShapedTwin), ("routed", RoutedSource)):
        root = tmp_path / kind
        source = cls(str(stage(root)), str(CHIPLET), str(IO_PADS), name=NAME,
                     interposer_adapter="intm4tm2", interconnect_adapter="ihp_cupillar")
        del worker[:]
        _run(root, source, capsys)
        (call,) = [c for c in worker if c.argv[1] == WORKER]
        staged[kind] = {Path(p).name: data for p, data in call.files.items()}
    board, routed = staged["board"], staged["routed"]
    assert board.pop("%s.hyp" % NAME) == BOARD_HYP.read_bytes()
    routed.pop("routed-geometry.json")
    assert board == routed and routed
