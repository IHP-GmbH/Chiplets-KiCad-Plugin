# SPDX-License-Identifier: GPL-2.0-or-later
"""
Pipeline orchestrator for the chiplet export plugin.

Wires up writers + discovery + runner into a single end-to-end export
call that the dialog drives. Keeping the orchestration in a dedicated
module (no wx, no pcbnew at module load) makes the CLI-args helper
unit-testable on host Python.

Public surface:
  - ExportOptions:  user-visible toggles collected by the dialog.
  - ExportResult:   outcome with exit code, cancelled flag, output paths.
  - build_cli_args: pure function, stdlib-only, returns the argv passed
                    to hyp_to_gds.py. The whole test_orchestrator.py
                    suite covers this function.
  - run_export:     end-to-end orchestrator. Imports pcbnew / writers
                    lazily so the test suite never reaches them.
"""

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass
class ExportOptions:
    """User-visible options collected by the dialog."""

    output_dir: str = ""
    emit_chiplet: bool = True
    emit_interposer_gds: bool = True
    emit_complete_gds: bool = False
    keep_intermediate_hyp: bool = False
    top_cell: str = "TOP"
    connection_type: str = ""          # empty = no --connection-type
    lyp_override: str = ""             # empty = hyp_to_gds default
    io_pads_json: str = ""             # empty = no --io-pads
    worker_python_override: str = ""   # empty = use discovery chain


@dataclass
class ExportResult:
    """Outcome of a single ``run_export`` call."""

    exit_code: int = -1
    cancelled: bool = False
    error: str = ""
    hyp_path: str = ""
    chiplet_path: str = ""
    interposer_gds_path: str = ""
    complete_gds_path: str = ""


def build_cli_args(hyp_to_gds_path: str,
                   hyp_path: str,
                   board_name: str,
                   options: ExportOptions) -> List[str]:
    """Construct argv for the hyp_to_gds.py subprocess.

    The list starts with the script path and the positional hyp input,
    then appends flags driven by `options`. Output paths are absolute
    (rooted at ``options.output_dir``).
    """
    out_dir = options.output_dir
    args: List[str] = [hyp_to_gds_path, hyp_path]

    if options.emit_interposer_gds:
        args += ["-o", os.path.join(out_dir, "%s_interposer.gds" % board_name)]

    if options.top_cell and options.top_cell != "TOP":
        args += ["-c", options.top_cell]

    if options.lyp_override:
        args += ["-l", options.lyp_override]

    if options.emit_complete_gds:
        args += [
            "--with-chiplets",
            "--complete-output",
            os.path.join(out_dir, "%s_complete.gds" % board_name),
        ]

    if options.emit_chiplet:
        args += [
            "--update-chiplet-file",
            os.path.join(out_dir, "%s.chiplet" % board_name),
        ]

    if options.connection_type:
        args += ["--connection-type", options.connection_type]

    if options.io_pads_json:
        args += ["--io-pads", options.io_pads_json]

    return args


def run_export(board, options, plugin_dir,
               on_log=None, cancel_event=None) -> ExportResult:
    """End-to-end export driven by the dialog's Run button.

    Sequence:
      1. Resolve worker python and hyp_to_gds.py (early failure).
      2. Create a workspace tmpdir.
      3. Write Hyperlynx and intermediate .chiplet via the Python ports.
      4. Stage the intermediate .chiplet into ``options.output_dir``
         so ``--update-chiplet-file`` can rewrite it in place.
      5. Invoke hyp_to_gds.py via the async runner; stream log lines
         to ``on_log``.
      6. Optionally copy the intermediate .hyp to the output directory.
      7. Always remove the tmpdir.

    Returns:
        ExportResult.  If ``error`` is non-empty the call failed before
        any subprocess ran and ``exit_code`` is -1.
    """
    from .discovery import (
        find_worker_python, find_hyp_to_gds,
        WorkerPythonNotFoundError, HypToGdsNotFoundError,
    )
    from .runner import run_async
    from ..writers.chiplet_writer import write_chiplet
    from ..writers.hyperlynx_writer import write_hyperlynx

    def _log(line):
        if on_log is not None:
            try:
                on_log(line)
            except Exception:
                pass

    if not options.output_dir:
        return ExportResult(error="Output directory is empty.")
    try:
        Path(options.output_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return ExportResult(error="Cannot create output directory: %s" % exc)

    board_file = ""
    try:
        board_file = board.GetFileName() or ""
    except Exception:
        pass
    board_name = Path(board_file).stem or "board"

    try:
        worker_py = (options.worker_python_override
                     or find_worker_python(plugin_dir, board=board))
    except WorkerPythonNotFoundError as exc:
        return ExportResult(error=str(exc))

    try:
        hyp_to_gds = find_hyp_to_gds(plugin_dir)
    except HypToGdsNotFoundError as exc:
        return ExportResult(error=str(exc))

    tmpdir = tempfile.mkdtemp(prefix="chiplet_export_")
    _log("Workspace: %s" % tmpdir)
    try:
        hyp_path = os.path.join(tmpdir, "%s.hyp" % board_name)
        chiplet_intermediate = os.path.join(tmpdir, "%s.chiplet" % board_name)

        _log("Writing Hyperlynx ...")
        try:
            hyp_ok = write_hyperlynx(board, hyp_path)
        except Exception as exc:
            import traceback
            return ExportResult(
                error="Hyperlynx writer crashed: %s\n%s"
                      % (exc, traceback.format_exc()),
            )
        if not hyp_ok:
            return ExportResult(
                error=(
                    "Hyperlynx writer aborted (most commonly: the "
                    "board has no closed Edge.Cuts outline). Add a "
                    "board outline, then retry."
                ),
            )
        _log("Writing intermediate .chiplet ...")
        try:
            chiplet_ok = write_chiplet(board, chiplet_intermediate)
        except Exception as exc:
            import traceback
            return ExportResult(
                error="Chiplet writer crashed: %s\n%s"
                      % (exc, traceback.format_exc()),
            )
        if not chiplet_ok:
            return ExportResult(
                error="Chiplet writer aborted (could not open the "
                      "intermediate .chiplet for writing).",
            )

        chiplet_final = os.path.join(options.output_dir,
                                     "%s.chiplet" % board_name)
        if options.emit_chiplet:
            shutil.copy2(chiplet_intermediate, chiplet_final)

        cli = build_cli_args(hyp_to_gds, hyp_path, board_name, options)
        command = [worker_py] + cli
        _log("$ " + " ".join(command))

        run = run_async(
            command,
            on_stdout=_log,
            on_stderr=lambda s: _log("[stderr] " + s),
            cancel_event=cancel_event,
        )

        hyp_kept = ""
        if options.keep_intermediate_hyp:
            hyp_kept = os.path.join(options.output_dir, "%s.hyp" % board_name)
            try:
                shutil.copy2(hyp_path, hyp_kept)
                _log("Kept intermediate .hyp at %s" % hyp_kept)
            except OSError as exc:
                _log("Warning: could not copy intermediate .hyp: %s" % exc)
                hyp_kept = ""

        return ExportResult(
            exit_code=run.exit_code,
            cancelled=run.cancelled,
            hyp_path=hyp_kept,
            chiplet_path=(chiplet_final if options.emit_chiplet else ""),
            interposer_gds_path=(
                os.path.join(options.output_dir,
                             "%s_interposer.gds" % board_name)
                if options.emit_interposer_gds else ""
            ),
            complete_gds_path=(
                os.path.join(options.output_dir,
                             "%s_complete.gds" % board_name)
                if options.emit_complete_gds else ""
            ),
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
