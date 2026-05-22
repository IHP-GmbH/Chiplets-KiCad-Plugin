# SPDX-License-Identifier: GPL-2.0-or-later
"""
Regenerate the wire-bond demo via the Python plugin pipeline.

Used by gate 47.7e to feed the chiplet-studio CoordFrameContract*
gtests with output produced by the plugin's writers + hyp_to_gds.py
worker (instead of the legacy C++ menu actions).

Two modes:

* default: full end-to-end via run_export (write_hyperlynx +
  write_chiplet + hyp_to_gds.py). Requires the board to have a
  closed Edge.Cuts outline.

* --use-existing-hyp PATH: skip the Hyperlynx writer step and run
  the rest of the pipeline against the supplied .hyp. Needed while
  the wire-bond demo .kicad_pcb still ships without Edge.Cuts (the
  legacy .hyp on disk was captured when the board had an outline).

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

from chiplet_kicad_plugin.pipeline.discovery import (  # noqa: E402
    find_worker_python, find_hyp_to_gds,
)
from chiplet_kicad_plugin.pipeline.orchestrator import (  # noqa: E402
    ExportOptions, run_export,
)
from chiplet_kicad_plugin.writers.chiplet_writer import (  # noqa: E402
    write_chiplet,
)


def _run_with_existing_hyp(board_path, hyp_path, lyp_path,
                            io_pads_path, output_dir):
    """Drive the pipeline without invoking the Hyperlynx writer.

    Path used when the source .kicad_pcb lacks a closed outline.
    Re-anchors the intermediate .chiplet using hyp_to_gds.py against
    the supplied (pre-existing) .hyp.
    """
    board = pcbnew.LoadBoard(board_path)
    board_name = Path(board_path).stem
    output_dir = str(Path(output_dir).absolute())
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    chiplet_path = os.path.join(output_dir, "%s.chiplet" % board_name)
    interposer_gds = os.path.join(
        output_dir, "%s_interposer.gds" % board_name,
    )
    complete_gds = os.path.join(output_dir, "%s_complete.gds" % board_name)
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
        "-c", "TOP",
        "--with-chiplets",
        "--complete-output", complete_gds,
        "--update-chiplet-file", chiplet_path,
    ]
    if lyp_path:
        cmd += ["--lyp", lyp_path]
    if io_pads_path:
        cmd += ["--io-pads", io_pads_path]
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
        default=str(PLUGIN_ROOT.parent / "kicad_designs"
                    / "interposer_wire_bonding_demo"
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
    args = parser.parse_args()

    output_dir = args.output_dir or tempfile.mkdtemp(prefix="wirebond_regen_")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

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
            args.io_pads, output_dir,
        )
        sys.exit(rc)

    board = pcbnew.LoadBoard(args.board)
    options = ExportOptions(
        output_dir=output_dir,
        emit_chiplet=True,
        emit_interposer_gds=True,
        emit_complete_gds=True,
        keep_intermediate_hyp=True,
        top_cell="TOP",
        lyp_override=args.lyp,
        io_pads_json=args.io_pads,
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

    sys.exit(0 if result.exit_code == 0 and not result.error else 1)


if __name__ == "__main__":
    main()
