# SPDX-License-Identifier: GPL-3.0-or-later
"""The board-path oracle of the routed_t03 fixture.

Runs the export's board-path functions, unmodified, on the draft board in
inputs/, with real pcbnew. Their outputs are what the export computes from a
loaded board before it starts the worker; the routed path must reproduce
them without a board.

    python make_oracle.py compute [--out DIR] [--interconnect-root ROOT]
        Write the oracle files into DIR (default: oracle/ next to this file).
    python make_oracle.py check [--interconnect-root ROOT]
        Recompute in memory and compare with the committed oracle/; exit 1
        on the first difference.
    python make_oracle.py mask-report SOURCE DEST
        Write the export report with its run paths replaced (no pcbnew).

compute() is importable, so a test can rerun it and hold the committed bytes
to the board path as it is now.

Normalising: the oracle files may carry exactly one volatile string, the
absolute folder of inputs/ at run time, which becomes RUN_TOKEN. After that no
output may name an absolute host or container path (VOLATILE_ROOTS).
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
INPUTS = HERE / "inputs"
ORACLE = HERE / "oracle"
PLUGINS = HERE.parents[3]

BOARD_NAME = "cf_typical_03_from_schematic.draft"
BOARD = INPUTS / (BOARD_NAME + ".kicad_pcb")
EXPORT_REPORT = INPUTS / "export.json"

RUN_TOKEN = "<RUN>"
VOLATILE_ROOTS = ("/home/", "/tmp/", "/work/", "/opt/")

#: The export report names the run's files by absolute path. The export reads
#: none of those fields (it takes the round_trip block), so the committed copy
#: replaces them, longest prefix first. compute --report-pair proves that the
#: masked copy gives the same oracle bytes as the original.
REPORT_MASKS = (
    ("/work/typical_03_from_schematic/runs/2026-11-19_20-49-29Z/run", "<RUN>"),
    ("/work/typical_03_from_schematic", "<WORK>"),
    ("/opt/adk-tools", "<TOOLS>"),
)

#: The oracle files, in the order the export computes them.
CHIPLET = BOARD_NAME + ".chiplet"
PIN_LISTS = ("U1_pins.json", "U2_pins.json")
CONNECTIONS = "die_connections.json"
THICKNESSES = "die_thicknesses.json"
IO_PADS = "io_pads.json"
COUNTS = "counts.json"


def _json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def normalise(data, run_root):
    """Replace ``run_root`` with RUN_TOKEN; refuse any other absolute path."""
    text = data.decode("utf-8").replace(str(run_root), RUN_TOKEN)
    for root in VOLATILE_ROOTS:
        if root in text:
            raise ValueError("oracle output still names %s after normalising" % root)
    return text.encode("utf-8")


def compute(board_path=BOARD, export_report=EXPORT_REPORT, interconnect_root=""):
    """{file name: normalised bytes} of the board path on ``board_path``."""
    if str(PLUGINS) not in sys.path:
        sys.path.insert(0, str(PLUGINS))
    import pcbnew
    from chiplet_export.draft_net_names import read_export_report
    from chiplet_export.pipeline.orchestrator import _preflight_interconnect_manifest
    from chiplet_export.writers.chiplet_writer import (
        read_die_connections, read_die_thicknesses, write_chiplet,
        write_cmim_devices_json, write_die_pin_lists, write_io_pads_json,
        write_nofill_regions_json,
    )

    board_path = Path(board_path).resolve()
    run_root = board_path.parent
    board = pcbnew.LoadBoard(str(board_path))
    manifest = _preflight_interconnect_manifest(interconnect_root, board=board)
    report = read_export_report(str(export_report))

    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        chiplet = tmp / CHIPLET
        if not write_chiplet(board, str(chiplet), interconnect_manifest=manifest,
                             export_report=report):
            raise RuntimeError("write_chiplet returned False")
        out[CHIPLET] = chiplet.read_bytes()
        pins_dir = tmp / "pins"
        pins_dir.mkdir()
        pins = write_die_pin_lists(board, str(pins_dir))
        for ref, path in sorted(pins.items()):
            out["%s_pins.json" % ref] = Path(path).read_bytes()
        out[CONNECTIONS] = _json_bytes(read_die_connections(board))
        out[THICKNESSES] = _json_bytes(read_die_thicknesses(board))
        io_pads = tmp / IO_PADS
        carriers = write_io_pads_json(board, str(io_pads), export_report=report)
        if carriers:
            out[IO_PADS] = io_pads.read_bytes()
        skipped = []
        cmim = write_cmim_devices_json(board, str(tmp / "cmim.json"), skipped=skipped)
        nofill = write_nofill_regions_json(board, str(tmp / "nofill.json"))
        out[COUNTS] = _json_bytes({
            "cmim_devices": cmim, "cmim_skipped": skipped,
            "nofill_regions": nofill, "io_pads": carriers,
            "pin_lists": sorted(pins),
        })
    return {name: normalise(data, run_root) for name, data in out.items()}


def mask_report(text):
    for prefix, token in REPORT_MASKS:
        text = text.replace(prefix, token)
    for root in VOLATILE_ROOTS:
        if root in text:
            raise ValueError("masked report still names %s" % root)
    return text


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _cmd_compute(args):
    files = compute(interconnect_root=args.interconnect_root)
    if args.report_pair:
        original = compute(export_report=args.report_pair,
                           interconnect_root=args.interconnect_root)
        if original != files:
            differ = sorted(n for n in set(files) | set(original)
                            if files.get(n) != original.get(n))
            raise SystemExit("the masked report changes the oracle: %s" % ", ".join(differ))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_bytes(data)
    import pcbnew
    record = {
        "pcbnew": str(pcbnew.GetBuildVersion()),
        "python": sys.version.split()[0],
        "inputs": {p.relative_to(HERE).as_posix(): _sha256(p)
                   for p in sorted(INPUTS.rglob("*")) if p.is_file()},
        "outputs": {name: hashlib.sha256(data).hexdigest()
                    for name, data in sorted(files.items())},
    }
    if args.report_pair:
        record["unmasked_report_sha256"] = _sha256(args.report_pair)
    (out / "run.json").write_bytes(_json_bytes(record))
    print("wrote %d oracle file(s) to %s" % (len(files), out))
    return 0


def _cmd_check(args):
    files = compute(interconnect_root=args.interconnect_root)
    committed = {p.name: p.read_bytes() for p in ORACLE.iterdir()
                 if p.is_file() and p.name != "run.json"}
    for name in sorted(set(files) | set(committed)):
        if files.get(name) != committed.get(name):
            print("oracle differs: %s" % name, file=sys.stderr)
            return 1
    print("oracle holds: %d file(s)" % len(files))
    return 0


def _cmd_mask_report(args):
    text = Path(args.source).read_text(encoding="utf-8")
    Path(args.dest).write_text(mask_report(text), encoding="utf-8")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("compute")
    p.add_argument("--out", default=str(ORACLE))
    p.add_argument("--interconnect-root", default=os.environ.get("INTERCONNECT_PDK_ROOT", ""))
    p.add_argument("--report-pair", default="",
                   help="the unmasked export report; the oracle must not change with it")
    p.set_defaults(func=_cmd_compute)
    p = sub.add_parser("check")
    p.add_argument("--interconnect-root", default=os.environ.get("INTERCONNECT_PDK_ROOT", ""))
    p.set_defaults(func=_cmd_check)
    p = sub.add_parser("mask-report")
    p.add_argument("source")
    p.add_argument("dest")
    p.set_defaults(func=_cmd_mask_report)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
