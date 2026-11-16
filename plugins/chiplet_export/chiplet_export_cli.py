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

Exit codes (``pipeline/export_exit.py``, this tool's own table): 0 when the
files were written and the assembly DRC PASSED; 1 when nothing trustworthy was
written; 2 when the board is missing or unreadable, or on a usage error; 3 when
the files were written but the assembly DRC did not pass (FAILED, REFUSED or
NOT RUN). ``--allow-drc-fail`` turns 3 into 0 and says so on the last line;
``--require-drc`` is accepted and changes nothing, the DRC is required by
default. The two flags are mutually exclusive.

Headless: an export must run under a Python that can ``import pcbnew`` (the
KiCad fork's interpreter); the worker python for hyp_to_gds.py is resolved
separately via the pipeline discovery chain. ``--help`` and usage errors need
no KiCad: pcbnew is imported once the arguments are parsed.

The full reference (flags, inputs, environment, outputs, exit codes,
examples) is CLI.md next to this file.
"""

import argparse
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
EXPORT_REPORT_FLAG = "--export-report"
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_export.pipeline.orchestrator import (  # noqa: E402
    ExportOptions, run_export, describe_assembly_drc,
)
from chiplet_export.pipeline.export_exit import (  # noqa: E402
    EXIT_BOARD, EXIT_TABLE_HELP, export_exit,
)


#: The full CLI reference; the epilog names it.
REFERENCE = PLUGIN_ROOT / "CLI.md"


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="chiplet-export",
        description="Export a chiplet assembly headless (board -> .chiplet + "
                    ".hyp + GDS + assembly DRC).",
        epilog="%s\n\nreference: %s" % (EXIT_TABLE_HELP, REFERENCE),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    drc = parser.add_mutually_exclusive_group()
    drc.add_argument(
        "--require-drc", action="store_true",
        help="Accepted for older callers; changes nothing. The assembly DRC "
             "is required by default: exit 3 unless it PASSED.",
    )
    drc.add_argument(
        "--allow-drc-fail", action="store_true",
        help="Exit 0 when the files were written even if the assembly DRC "
             "FAILED, was REFUSED or did NOT RUN; the last line says which.",
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
        help="Connection stack (e.g. cupillar_opt1/2/3). A cupillar stack "
             "or a solder bump auto-generates its landings from the die pads. "
             "A method whose "
             "landing the worker cannot draw (connection_methods.py) is "
             "refused before the export stages anything.",
    )
    parser.add_argument(
        "--insert-metal-fill", action="store_true",
        help="Stamp metal density fill on the interposer GDS via the "
             "interposer PDK engine. Needs the PDK fill work and the klayout "
             "binary; off by default.",
    )
    parser.add_argument(
        "--fill-mode", choices=("single-pass", "closure"), default="single-pass",
        help="single-pass (fast, all four metals) or closure (M4/M5 "
             "density-feedback, deck-verified). Ignored without "
             "--insert-metal-fill.",
    )
    parser.add_argument(
        "--seal-ring", action="store_true",
        help="Draw the interposer PDK seal ring on the board outline (one "
             "axis-aligned rectangle). Refused, with nothing written, when the "
             "outline cannot take it or drawn geometry crowds it; off by "
             "default.",
    )
    parser.add_argument(
        "--system-chiplet", default="",
        help="The System .chiplet the assembly was built from. Each "
             "component's technology, and the technologies block, are taken "
             "from it by component id; the export is refused, with nothing "
             "written, when one has no match there. Empty = as the board "
             "gives them.",
    )
    parser.add_argument(
        "--nofill-regions", default="",
        help="Sidecar JSON of no-fill keep-outs. Empty = auto-extract from the "
             "board's NoMetFiller / <metal>.nofill layers.",
    )
    parser.add_argument(
        EXPORT_REPORT_FLAG, default="",
        help="The report interposer-pnr export wrote with the board. "
             "Required when a carrier pad export drew (from chiplet_carrier, "
             "or carrying IO_PAD_ID) links a sheet symbol, as on a board "
             "drawn against a sheet or a draft updated from its draft sheet: "
             "the io_pad nets take the planner's names from it. Never "
             "guessed.",
    )
    parser.add_argument(
        "--die-layouts", default="",
        help="Sidecar JSON naming, per die, its full layout and the "
             "contract's top_cell and bbox_um. The .chiplet then names that "
             "die's full layout; the complete GDS keeps the abstract its "
             "GDS_FILE gives. Refused, with nothing written, when either "
             "file does not match the contract. Empty = each die's layout is "
             "its GDS_FILE.",
    )
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    # Here, not at the top: --help and a usage error need no KiCad.
    import pcbnew

    # Read the board before creating anything. A board that cannot be read used
    # to leave an output tree and a log directory behind, which reads like a run
    # that started.
    board_path = Path(args.board)
    if not board_path.is_file():
        sys.stderr.write(
            "chiplet-export: board not found: %s\n"
            "  resolved from the current directory %s\n"
            "  Pass an absolute path, or run from the directory the relative "
            "one is written against.\n" % (args.board, Path.cwd()))
        return EXIT_BOARD
    if args.system_chiplet and not Path(args.system_chiplet).is_file():
        sys.stderr.write(
            "chiplet-export: System .chiplet not found: %s\n"
            "  resolved from the current directory %s\n"
            % (args.system_chiplet, Path.cwd()))
        return EXIT_BOARD
    if args.die_layouts and not Path(args.die_layouts).is_file():
        sys.stderr.write(
            "chiplet-export: die layouts sidecar not found: %s\n"
            "  resolved from the current directory %s\n"
            % (args.die_layouts, Path.cwd()))
        return EXIT_BOARD
    if args.export_report and not Path(args.export_report).is_file():
        sys.stderr.write("chiplet-export: export report not found: %s\n" % args.export_report)
        return EXIT_BOARD

    # Not created here: run_export creates it once its preflights pass, so a
    # refused board (carrier pad domains, die outline layers) leaves nothing
    # behind.
    output_dir = str(Path(args.output_dir).absolute())

    print("chiplet-export: %s -> %s" % (args.board, output_dir))
    print("  plugin_dir:  %s" % PLUGIN_ROOT)

    board = pcbnew.LoadBoard(args.board)
    # LoadBoard returns None rather than raising. Unchecked, that None travelled
    # into the Hyperlynx writer and surfaced twelve frames later as
    # "'NoneType' object has no attribute 'GetFileName'", which names neither
    # the board nor the reason.
    if board is None:
        sys.stderr.write(
            "chiplet-export: KiCad could not read the board: %s\n"
            "  The file exists but pcbnew returned no board. It may be from an "
            "incompatible KiCad version, or corrupt.\n" % board_path)
        return EXIT_BOARD
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
        insert_metal_fill=args.insert_metal_fill,
        fill_mode=args.fill_mode,
        seal_ring=args.seal_ring,
        nofill_regions_json=(str(Path(args.nofill_regions).absolute())
                             if args.nofill_regions else ""),
        system_chiplet=(str(Path(args.system_chiplet).absolute())
                        if args.system_chiplet else ""),
        export_report=(str(Path(args.export_report).absolute())
                       if args.export_report else ""),
        die_layouts_json=(str(Path(args.die_layouts).absolute())
                          if args.die_layouts else ""),
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
        error = result.error
        if result.error_input == "export_report":
            error += "; the export report is the file given with " + EXPORT_REPORT_FLAG
        print("error:               %s" % error)
    print("hyp_path:            %s" % result.hyp_path)
    print("chiplet_path:        %s" % result.chiplet_path)
    print("interposer_gds_path: %s" % result.interposer_gds_path)
    print("complete_gds_path:   %s" % result.complete_gds_path)
    if result.fill_density_report_path:
        print("fill_density_report: %s" % result.fill_density_report_path)
    if result.fill_coverage_path:
        print("fill_coverage_path:  %s" % result.fill_coverage_path)
    print("%s" % describe_assembly_drc(result))
    if result.assembly_drc_report_path:
        print("assembly_drc_report: %s" % result.assembly_drc_report_path)

    if result.assembly_drc_error:
        sys.stderr.write(result.assembly_drc_error)
    code, last_line = export_exit(result, args.allow_drc_fail)
    if last_line:
        print(last_line)
    return code


if __name__ == "__main__":
    sys.exit(main())
