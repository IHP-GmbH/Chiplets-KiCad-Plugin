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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


# Default ADK interposer adapter used when neither the dialog nor the
# .chiplet file declare one. Matches the only adapter shipped today
# (adk/pdk_adapters/interposer/ihp_sg13g2_interposer.drc).
DEFAULT_INTERPOSER_ADAPTER = "ihp_sg13g2_interposer"


@dataclass
class ExportOptions:
    """User-visible options collected by the dialog."""

    output_dir: str = ""
    emit_chiplet: bool = True
    emit_interposer_gds: bool = True
    emit_complete_gds: bool = False
    keep_intermediate_hyp: bool = False
    top_cell: str = "INTERPOSER"
    connection_type: str = ""          # empty = no --connection-type
    lyp_override: str = ""             # empty = hyp_to_gds default (built-in IHP)
    io_pads_json: str = ""             # empty = auto-extract from board
    cupillar_gds: str = ""             # non-empty = pre-generated GDS override
    worker_python_override: str = ""   # empty = use discovery chain
    # Assembly DRC against the ADK deck. Runs after hyp_to_gds when a
    # complete.gds was emitted; can be disabled when the user only wants
    # the GDS output.
    emit_assembly_drc: bool = True
    # Explicit adapter override. Empty = read from .chiplet file's
    # `interposer.adapter` field, with DEFAULT_INTERPOSER_ADAPTER as the
    # final fallback.
    interposer_adapter: str = ""
    # {ref: pin_list_json} auto-extracted die bumps; drives Cu-pillar
    # generation when connection_type names a cupillar stack.
    pad_locations: Dict[str, str] = field(default_factory=dict)


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
    cupillar_drc_path: str = ""
    # ADK assembly DRC outcome. ``exit_code`` of -1 means the deck did
    # not run (disabled, no complete.gds, or runner not found).
    assembly_drc_exit_code: int = -1
    assembly_drc_report_path: str = ""


def load_interposer_adapter(chiplet_path: str) -> str:
    """Return the interposer adapter declared in a ``.chiplet`` YAML file.

    Reads the top-level ``interposer.adapter`` field and returns its
    value. Falls back to :data:`DEFAULT_INTERPOSER_ADAPTER` when the file
    is missing, unreadable, or does not declare the field.

    A minimal hand-rolled parser is used so this helper is callable from
    KiCad's bundled Python (which lacks PyYAML). It accepts the canonical
    block shape emitted by the writer::

        interposer:
          adapter: "ihp_sg13g2_interposer"

    Quoted (single or double) and unquoted values are both accepted.
    Lines beginning with ``#`` and inline ``#`` comments are stripped.
    """
    try:
        with open(chiplet_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return DEFAULT_INTERPOSER_ADAPTER

    in_block = False
    for raw in lines:
        line = raw.rstrip("\n")
        if "#" in line:
            line = line[: line.index("#")].rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if indent == 0:
            in_block = (stripped == "interposer:")
            continue
        if not in_block:
            continue
        if stripped.startswith("adapter:"):
            value = stripped[len("adapter:"):].strip()
            if (len(value) >= 2
                    and value[0] in ("'", '"')
                    and value[-1] == value[0]):
                value = value[1:-1]
            return value or DEFAULT_INTERPOSER_ADAPTER
    return DEFAULT_INTERPOSER_ADAPTER


def build_adk_drc_argv(adk_runner_path: str,
                       gds_path: str,
                       interposer_adapter: str,
                       report_path: Optional[str] = None,
                       run_dir: Optional[str] = None,
                       topcell: Optional[str] = None,
                       threads: Optional[int] = None,
                       run_mode: Optional[str] = None) -> List[str]:
    """Construct argv for the ADK ``run_drc.py`` subprocess.

    The returned list begins with ``adk_runner_path`` and the required
    ``--path`` / ``--interposer-adapter`` flags; the remaining flags are
    appended only when the caller provides a value. Output paths are
    passed through unchanged (callers are expected to pre-resolve them).
    """
    args: List[str] = [
        adk_runner_path,
        "--path", gds_path,
        "--interposer-adapter", interposer_adapter,
    ]
    if report_path:
        args += ["--report", report_path]
    if run_dir:
        args += ["--run_dir", run_dir]
    if topcell:
        args += ["--topcell", topcell]
    if threads is not None:
        args += ["--threads", str(threads)]
    if run_mode:
        args += ["--run_mode", run_mode]
    return args


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

    if options.top_cell:
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

    if options.cupillar_gds:
        args += ["--cupillar-gds", options.cupillar_gds]

    if options.pad_locations:
        spec = ",".join("%s=%s" % (ref, p)
                        for ref, p in sorted(options.pad_locations.items()))
        args += ["--pad-locations", spec]

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
        find_worker_python, find_hyp_to_gds, find_adk_drc_runner,
        WorkerPythonNotFoundError, HypToGdsNotFoundError,
        AdkRunnerNotFoundError,
    )
    import dataclasses

    from .runner import run_async
    from ..writers.chiplet_writer import (
        write_chiplet, write_io_pads_json, write_die_pin_lists,
    )
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

        # Auto-extract io_pads from the board so hyp_to_gds renders the pad
        # geometry (and the interposer GDS bbox includes them). A non-empty
        # options.io_pads_json acts as an explicit override.
        effective_io_pads = options.io_pads_json
        if not effective_io_pads:
            io_pads_auto = os.path.join(tmpdir, "%s_io_pads.json" % board_name)
            try:
                n_io = write_io_pads_json(board, io_pads_auto)
            except Exception as exc:
                n_io = 0
                _log("Warning: io_pads auto-extraction failed: %s" % exc)
            if n_io:
                effective_io_pads = io_pads_auto
                _log("Auto-extracted %d io_pad(s) from board" % n_io)

        # Auto-extract die footprint pads so the Cu-pillar generator places
        # DRC-validated pillars under each flip-chip die (acts only when
        # connection_type names a cupillar stack). A user-supplied
        # cupillar_gds is a pre-generated override and disables auto-extract.
        effective_pad_locs = options.pad_locations
        if not effective_pad_locs and not options.cupillar_gds:
            try:
                effective_pad_locs = write_die_pin_lists(board, tmpdir)
            except Exception as exc:
                effective_pad_locs = {}
                _log("Warning: die pad extraction failed: %s" % exc)
            if effective_pad_locs:
                _log("Auto-extracted die bumps for cu-pillars: %s"
                     % ", ".join(sorted(effective_pad_locs)))

        effective_options = dataclasses.replace(
            options, io_pads_json=effective_io_pads,
            pad_locations=effective_pad_locs)
        cli = build_cli_args(hyp_to_gds, hyp_path, board_name, effective_options)
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

        # The worker writes <board>_cupillar_drc.json next to the interposer
        # GDS when a cupillar stack drives pillar generation. Surface it only
        # if it was actually produced this run.
        drc_report = os.path.join(options.output_dir,
                                  "%s_cupillar_drc.json" % board_name)
        if not os.path.exists(drc_report):
            drc_report = ""

        # ADK assembly DRC over the complete.gds (chiplets stamped on the
        # interposer). Runs only when there is a complete.gds to check
        # and the user did not opt out via emit_assembly_drc=False.
        assembly_drc_exit = -1
        assembly_drc_report = ""
        complete_gds_abs = os.path.join(
            options.output_dir, "%s_complete.gds" % board_name,
        )
        should_run_drc = (
            options.emit_complete_gds
            and options.emit_assembly_drc
            and run.exit_code == 0
            and not run.cancelled
            and os.path.exists(complete_gds_abs)
        )
        if should_run_drc:
            try:
                adk_runner = find_adk_drc_runner(plugin_dir, board=board)
            except AdkRunnerNotFoundError as exc:
                _log("Assembly DRC skipped: %s" % exc)
                adk_runner = ""
            if adk_runner:
                effective_adapter = (
                    options.interposer_adapter
                    or load_interposer_adapter(chiplet_final)
                )
                drc_run_dir = os.path.join(
                    options.output_dir, "assembly_drc",
                )
                assembly_drc_report_target = os.path.join(
                    options.output_dir,
                    "%s_assembly_drc.lyrdb" % board_name,
                )
                adk_cli = build_adk_drc_argv(
                    adk_runner,
                    gds_path=complete_gds_abs,
                    interposer_adapter=effective_adapter,
                    report_path=assembly_drc_report_target,
                    run_dir=drc_run_dir,
                    topcell=options.top_cell or None,
                )
                adk_command = [worker_py] + adk_cli
                _log("$ " + " ".join(adk_command))
                adk_run = run_async(
                    adk_command,
                    on_stdout=_log,
                    on_stderr=lambda s: _log("[stderr] " + s),
                    cancel_event=cancel_event,
                )
                assembly_drc_exit = adk_run.exit_code
                if os.path.exists(assembly_drc_report_target):
                    assembly_drc_report = assembly_drc_report_target

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
                complete_gds_abs if options.emit_complete_gds else ""
            ),
            cupillar_drc_path=drc_report,
            assembly_drc_exit_code=assembly_drc_exit,
            assembly_drc_report_path=assembly_drc_report,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
