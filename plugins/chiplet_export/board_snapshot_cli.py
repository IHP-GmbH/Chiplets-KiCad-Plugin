# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent, read-only PCB observation CLI. Requires pcbnew, never a GUI."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

if __package__:
    from .board_snapshot import SnapshotError, observe_board
    from .output_files import create_output_temp
else:
    from board_snapshot import SnapshotError, observe_board
    from output_files import create_output_temp


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_file(board_path, output_path):
    """Load one PCB and exclusively publish its snapshot after a digest check."""
    board_path = Path(board_path).resolve(strict=True)
    output_path = Path(output_path).absolute()
    if (output_path == board_path or
            (output_path.exists() and output_path.samefile(board_path))):
        raise SnapshotError("Snapshot output must not overwrite the source PCB")
    if os.path.lexists(output_path):
        raise FileExistsError("Snapshot output already exists: %s" % output_path)

    before = _sha256(board_path)
    import pcbnew

    try:
        producer_version = pcbnew.GetBuildVersion()
    except Exception as exc:
        raise SnapshotError("pcbnew producer version unavailable: "
                            "GetBuildVersion() failed (%s: %s)" %
                            (type(exc).__name__, exc)) from exc
    if not isinstance(producer_version, str) or not producer_version.strip():
        raise SnapshotError("pcbnew producer version unavailable: "
                            "GetBuildVersion() did not return a nonempty string")

    board = pcbnew.LoadBoard(str(board_path))
    if board is None:
        raise SnapshotError("pcbnew could not load %s" % board_path)
    snapshot = observe_board(board)
    snapshot["source"].update(path=str(board_path), sha256=before)
    snapshot["producer"] = {"tool": "pcbnew", "version": producer_version}
    text = json.dumps(snapshot, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if _sha256(board_path) != before:
        raise SnapshotError("Source PCB changed while it was being observed")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = create_output_temp(prefix=".board-snapshot-", suffix=".json",
                                       directory=output_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        # Publish a complete file atomically, refusing even a destination
        # created between the existence check and publication. replace() would
        # silently overwrite earlier evidence. Both paths share a filesystem.
        os.link(temporary, output_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Observe PCB placement, raw fields, pads and net membership "
                    "in absolute KiCad millimetres (Y down).")
    parser.add_argument("--board", required=True, help="Source .kicad_pcb (read-only).")
    parser.add_argument("--output", required=True, help="Destination snapshot JSON.")
    args = parser.parse_args(argv)
    try:
        snapshot_file(args.board, args.output)
    except Exception as exc:
        print("board-snapshot: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
