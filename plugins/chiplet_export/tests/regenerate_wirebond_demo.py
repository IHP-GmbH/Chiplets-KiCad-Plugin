# SPDX-License-Identifier: GPL-3.0-or-later
"""
Regenerate the wire-bond demo via the Python plugin pipeline.

Feeds the chiplet-studio CoordFrameContract* gtests with output
produced by the plugin's writers + hyp_to_gds.py worker (instead of
the legacy C++ menu actions).

Two modes:

* default: full end-to-end via run_export (write_hyperlynx +
  write_chiplet + hyp_to_gds.py). Requires the board to have a
  closed Edge.Cuts outline.

* --use-existing-hyp PATH: skip the Hyperlynx writer step and run
  the rest of the pipeline against the supplied .hyp. Needed while
  the wire-bond demo .kicad_pcb still ships without Edge.Cuts (the
  legacy .hyp on disk was captured when the board had an outline).
  The die outline flags come from the board the way run_export
  derives them (die_outline.preflight_die_outline_layers), and a
  board that preflight refuses stops the run before anything is
  written. This mode runs no assembly DRC: a worker that exits 0
  exits 0 here, and the last line says no DRC ran.

The default mode exits with chiplet-export's table
(pipeline/export_exit.py): 0 when the files were written and the
assembly DRC PASSED, 1 when nothing trustworthy was written, 3 when
the files were written but the DRC did not pass. --allow-drc-fail
turns that 3 into 0 and says so; --require-drc changes nothing.

Headless: must run inside kicad-builder with pcbnew on PYTHONPATH and
the worker venv discoverable.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

import pcbnew  # noqa: E402

from chiplet_export.die_outline import (  # noqa: E402
    DieOutlineLayerRefused, format_layer, preflight_die_outline_layers,
)
from chiplet_export.pad_domains import PadDomainError  # noqa: E402
from chiplet_export.pipeline.discovery import (  # noqa: E402
    find_worker_python, find_hyp_to_gds,
)
from chiplet_export.pipeline.export_exit import export_exit  # noqa: E402
from chiplet_export.pipeline.orchestrator import (  # noqa: E402
    ExportOptions, run_export, describe_assembly_drc,
    describe_die_outline_layers, layout_dir, layout_path,
)
from chiplet_export.writers.chiplet_writer import (  # noqa: E402
    write_chiplet,
)


def _run_with_existing_hyp(board_path, hyp_path, lyp_path,
                            io_pads_path, cupillar_gds_path, output_dir):
    """Drive the pipeline without invoking the Hyperlynx writer.

    Path used when the source .kicad_pcb lacks a closed outline.
    Re-anchors the intermediate .chiplet using hyp_to_gds.py against
    the supplied (pre-existing) .hyp.
    """
    board = pcbnew.LoadBoard(board_path)
    board_name = Path(board_path).stem

    # The worker sizes every die from the outline layer the board declares
    # for it, and leaves the marked dies at their archived size. Derived and
    # checked here the way run_export does it, before anything is written.
    print("Stage 0: die outline layers (board fields)")
    try:
        outline_layers, outline_exempt = preflight_die_outline_layers(board)
    except DieOutlineLayerRefused as exc:
        print("  ERROR: Die outline layer refused:")
        for ref, cause in exc.offenders:
            print("    %s: %s" % (ref, cause))
        return 1
    except PadDomainError as exc:
        print("  ERROR: Carrier pad domains refused: %s" % exc)
        return 1
    outline_line = describe_die_outline_layers(outline_layers, outline_exempt)
    if outline_line:
        print("  %s" % outline_line)

    output_dir = str(Path(output_dir).absolute())
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    # GDS layouts go under layout/; the .chiplet stays at the output-dir root.
    Path(layout_dir(output_dir)).mkdir(parents=True, exist_ok=True)

    chiplet_path = os.path.join(output_dir, "%s.chiplet" % board_name)
    interposer_gds = layout_path(output_dir, "%s_interposer.gds" % board_name)
    complete_gds = layout_path(output_dir, "%s_complete.gds" % board_name)
    work_hyp = os.path.join(output_dir, "%s.hyp" % board_name)

    print("Stage 1: write intermediate .chiplet (Python writer)")
    if not write_chiplet(board, chiplet_path):
        print("  ERROR: write_chiplet returned False")
        return 1

    print("Stage 2: copy committed .hyp into workspace")
    shutil.copy2(hyp_path, work_hyp)

    print("Stage 3: run hyp_to_gds.py (interposer GDS + complete GDS + "
          "re-anchor chiplet)")
    worker_py = find_worker_python(str(PLUGIN_ROOT))
    hyp_to_gds = find_hyp_to_gds(str(PLUGIN_ROOT))
    cmd = [
        worker_py, hyp_to_gds, work_hyp,
        "-o", interposer_gds,
        "-c", "INTERPOSER",
        "--with-chiplets",
        "--complete-output", complete_gds,
        "--update-chiplet-file", chiplet_path,
    ]
    if lyp_path:
        cmd += ["--lyp", lyp_path]
    if io_pads_path:
        cmd += ["--io-pads", io_pads_path]
    if cupillar_gds_path:
        cmd += ["--cupillar-gds", cupillar_gds_path]
    # The --flag=VALUE form, as build_cli_args writes it: a reference may
    # start with '-', which argparse would take for an option.
    if outline_layers:
        cmd.append("--die-outline-layers=" + ",".join(
            "%s=%s" % (ref, format_layer(layer))
            for ref, layer in sorted(outline_layers.items())))
    if outline_exempt:
        cmd.append("--die-outline-exempt=" + ",".join(outline_exempt))
    print("  $ %s" % " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.splitlines():
            print("  | %s" % line)
    if result.stderr:
        for line in result.stderr.splitlines():
            print("  | [stderr] %s" % line)
    print()
    print("chiplet_path:        %s" % chiplet_path)
    print("interposer_gds_path: %s" % interposer_gds)
    print("complete_gds_path:   %s" % complete_gds)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--board",
        default=str(PLUGIN_ROOT.parent / "examples"
                    / "interposer_wire_bonding_demo"
                    / "kicad"
                    / "interposer_wire_bonding_demo.kicad_pcb"),
        help="Path to the wire-bond demo .kicad_pcb",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Output directory (defaults to a tmpdir)",
    )
    parser.add_argument(
        "--use-existing-hyp",
        default="",
        help="Skip Hyperlynx writer; use this .hyp path instead.",
    )
    parser.add_argument(
        "--lyp",
        default="",
        help="LYP layer-properties file path (forwarded to hyp_to_gds.py).",
    )
    parser.add_argument(
        "--io-pads",
        default="",
        help="Sidecar io_pads.json (forwarded to hyp_to_gds.py).",
    )
    parser.add_argument(
        "--cupillar-gds",
        default="",
        help="Pre-generated cu-pillar GDS to merge into the interposer "
             "(forwarded to hyp_to_gds.py).",
    )
    drc = parser.add_mutually_exclusive_group()
    drc.add_argument(
        "--require-drc", action="store_true",
        help="Accepted for older callers; changes nothing. The assembly DRC "
             "is required by default: exit 3 unless it PASSED.",
    )
    drc.add_argument(
        "--allow-drc-fail", action="store_true",
        help="Exit 0 when the files were written even if the assembly DRC "
             "FAILED, was REFUSED or did NOT RUN; the last line says which. "
             "--use-existing-hyp runs no DRC and ignores both flags.",
    )
    parser.add_argument(
        "--connection",
        default="",
        help="Connection stack (cupillar_opt1/2/3, sbump_sac305). When a "
             "cupillar stack is set, pillars are auto-generated from the die "
             "footprint pads with DRC validation.",
    )
    args = parser.parse_args()

    # An --output-dir is not created here: both paths create it once the
    # board passes their preflights, so a refused board leaves nothing
    # behind. The default is a fresh, empty tmpdir.
    output_dir = args.output_dir or tempfile.mkdtemp(prefix="wirebond_regen_")

    print("Regenerating wire-bond demo via plugin pipeline ...")
    print("  board:       %s" % args.board)
    print("  output_dir:  %s" % output_dir)
    print("  plugin_dir:  %s" % PLUGIN_ROOT)
    if args.use_existing_hyp:
        print("  hyp source:  %s (skipping Hyperlynx writer)"
              % args.use_existing_hyp)

    if args.use_existing_hyp:
        rc = _run_with_existing_hyp(
            args.board, args.use_existing_hyp, args.lyp,
            args.io_pads, args.cupillar_gds, output_dir,
        )
        if rc == 0:
            print("No assembly DRC ran (--use-existing-hyp runs none); exit 0")
        sys.exit(rc)

    board = pcbnew.LoadBoard(args.board)
    options = ExportOptions(
        output_dir=output_dir,
        emit_chiplet=True,
        emit_complete_gds=True,
        top_cell="INTERPOSER",
        connection_type=args.connection,
        lyp_override=args.lyp,
        io_pads_json=args.io_pads,
        cupillar_gds=args.cupillar_gds,
    )

    def on_log(line):
        line = line.rstrip("\n")
        if line:
            print("  | %s" % line)

    result = run_export(
        board, options, str(PLUGIN_ROOT), on_log=on_log,
    )

    print()
    print("exit_code:           %d" % result.exit_code)
    print("cancelled:           %s" % result.cancelled)
    if result.error:
        print("error:               %s" % result.error)
    print("hyp_path:            %s" % result.hyp_path)
    print("chiplet_path:        %s" % result.chiplet_path)
    print("interposer_gds_path: %s" % result.interposer_gds_path)
    print("complete_gds_path:   %s" % result.complete_gds_path)
    print("%s" % describe_assembly_drc(result))
    if result.assembly_drc_report_path:
        print("assembly_drc_report: %s" % result.assembly_drc_report_path)

    if result.assembly_drc_error:
        sys.stderr.write(result.assembly_drc_error)
    code, last_line = export_exit(result, args.allow_drc_fail)
    if last_line:
        print(last_line)
    sys.exit(code)


if __name__ == "__main__":
    main()
