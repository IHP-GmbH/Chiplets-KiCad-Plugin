# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless chiplet export (first-class CLI over the plugin pipeline).

Exposes ``pipeline.orchestrator.run_export`` as a stable command-line tool so
the flow can be driven without pcbnew's GUI: a KiCad board goes in, the full
output tree (canonical ``.chiplet`` + driving ``.hyp`` + interposer/complete
GDS) plus the ADK assembly DRC come out. The Chiplets Project Manager's export
stage drives it, and it is the intended entry point for an adk-tools
``chiplet-export`` wrapper.

Sequence mirrors the dialog's Run button (write Hyperlynx + intermediate
.chiplet, run hyp_to_gds.py, run the assembly DRC over the complete GDS). The
DRC report lands at ``<output-dir>/reports/<board>_assembly_drc.lyrdb``; the
complete GDS at ``<output-dir>/layout/<board>_complete.gds``.

``--require-drc`` exits nonzero unless the assembly DRC ran and PASSED
(``ExportResult.assembly_drc_exit_code == 0``). It is off by default so an
environment without the klayout CLI can still regenerate the GDS artifacts.

Headless: must run under a Python that can ``import pcbnew`` (the KiCad fork's
interpreter); the worker python for hyp_to_gds.py is resolved separately via the
pipeline discovery chain.
"""

import argparse
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

import pcbnew  # noqa: E402

from chiplet_kicad_plugin.pipeline.orchestrator import (  # noqa: E402
    ExportOptions, run_export, describe_assembly_drc,
)


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="chiplet-export",
        description="Export a chiplet assembly headless (board -> .chiplet + "
                    ".hyp + GDS + assembly DRC).",
    )
    parser.add_argument(
        "--board", required=True,
        help="Path to the assembly .kicad_pcb to export.",
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Directory for the export outputs (created if absent). GDS goes "
             "under layout/, the DRC report under reports/.",
    )
    parser.add_argument(
        "--require-drc", action="store_true",
        help="Exit nonzero unless the ADK assembly DRC ran and PASSED. Off by "
             "default so environments without the klayout CLI can still export.",
    )
    parser.add_argument(
        "--interposer-adapter", default="",
        help="Interposer adapter override. Empty = read interposer.adapter from "
             "the .chiplet (final fallback intm4tm2).",
    )
    parser.add_argument(
        "--interconnect-adapter", default="",
        help="Interconnect adapter override. Empty = read interconnect.adapter "
             "from the .chiplet (absent there = no interconnect axis).",
    )
    parser.add_argument(
        "--connection", default="",
        help="Connection stack (e.g. cupillar_opt1/2/3, sbump_sac305). A "
             "cupillar stack auto-generates pillars from the die pads.",
    )
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)

    output_dir = str(Path(args.output_dir).absolute())
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    print("chiplet-export: %s -> %s" % (args.board, output_dir))
    print("  plugin_dir:  %s" % PLUGIN_ROOT)

    board = pcbnew.LoadBoard(args.board)
    options = ExportOptions(
        output_dir=output_dir,
        emit_chiplet=True,
        # The interposer GDS is always produced (hyp_to_gds writes it
        # unconditionally and the .chiplet's layout: references it); it is no
        # longer an ExportOptions toggle. result.interposer_gds_path below
        # still carries its path.
        emit_complete_gds=True,
        top_cell="INTERPOSER",
        emit_assembly_drc=True,
        connection_type=args.connection,
        interposer_adapter=args.interposer_adapter,
        interconnect_adapter=args.interconnect_adapter,
    )

    def on_log(line):
        line = line.rstrip("\n")
        if line:
            print("  | %s" % line)

    result = run_export(board, options, str(PLUGIN_ROOT), on_log=on_log)

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

    ok = result.exit_code == 0 and not result.error
    if args.require_drc and result.assembly_drc_exit_code != 0:
        print("FAIL: --require-drc set and the assembly DRC did not pass")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
