# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI provenance and exclusive publication checks; no KiCad installation."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import board_snapshot_cli as cli
from board_snapshot import SnapshotError


class SnapshotCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.board = self.root / "source.kicad_pcb"
        self.board.write_bytes(b"source board bytes\n")
        self.output = self.root / "observation.json"
        self.load = Mock(return_value=object())
        self.version = Mock(return_value="9.99.0-test-build")
        self.bindings = patch.dict(sys.modules, {"pcbnew": SimpleNamespace(
            LoadBoard=self.load, GetBuildVersion=self.version)})
        self.bindings.start()
        self.addCleanup(self.bindings.stop)
        self.observe = patch.object(cli, "observe_board", return_value={"source": {}})
        self.observer = self.observe.start()
        self.addCleanup(self.observe.stop)

    def test_success_records_saved_path_digest_and_leaves_board_unchanged(self):
        original = self.board.read_bytes()
        result = cli.snapshot_file(self.board, self.output)
        self.assertEqual(result["source"], {
            "path": str(self.board.resolve()),
            "sha256": hashlib.sha256(original).hexdigest(),
        })
        self.assertEqual(json.loads(self.output.read_text()), result)
        self.assertEqual(result["producer"], {"tool": "pcbnew", "version": "9.99.0-test-build"})
        self.assertEqual(self.board.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".board-snapshot-*")), [])

    def test_output_cannot_alias_source_by_path_symlink_or_hardlink(self):
        symlink, hardlink = self.root / "alias", self.root / "hardlink"
        symlink.symlink_to(self.board)
        os.link(self.board, hardlink)
        for target in [self.board, symlink, hardlink]:
            # The name, not the Path: under xdist a subTest parameter goes
            # through execnet, which cannot serialize a Path.
            with self.subTest(target=target.name), self.assertRaises(SnapshotError):
                cli.snapshot_file(self.board, target)
        self.load.assert_not_called()
        self.assertEqual(self.board.read_bytes(), b"source board bytes\n")

    def test_existing_evidence_is_never_overwritten(self):
        self.output.write_text("prior evidence")
        with self.assertRaises(FileExistsError):
            cli.snapshot_file(self.board, self.output)
        self.assertEqual(self.output.read_text(), "prior evidence")
        self.load.assert_not_called()

    def test_dangling_output_symlink_is_refused(self):
        self.output.symlink_to(self.root / "missing")
        with self.assertRaises(FileExistsError):
            cli.snapshot_file(self.board, self.output)
        self.assertFalse((self.root / "missing").exists())

    def test_source_mutation_during_observation_refuses_without_output(self):
        def change_board(_board):
            self.board.write_bytes(b"changed by another process")
            return {"source": {}}
        self.observer.side_effect = change_board
        with self.assertRaisesRegex(SnapshotError, "changed while"):
            cli.snapshot_file(self.board, self.output)
        self.assertFalse(self.output.exists())

    def test_observation_failure_never_leaves_partial_output(self):
        self.observer.side_effect = SnapshotError("Unsupported Edge.Cuts shape")
        self.assertEqual(cli.main(["--board", str(self.board), "--output", str(self.output)]), 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".board-snapshot-*")), [])

    def test_concurrent_output_creation_is_not_clobbered(self):
        link = os.link
        def competing_writer(source, target):
            Path(target).write_text("concurrent evidence")
            link(source, target)
        with patch.object(cli.os, "link", side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                cli.snapshot_file(self.board, self.output)
        self.assertEqual(self.output.read_text(), "concurrent evidence")
        self.assertEqual(list(self.root.glob(".board-snapshot-*")), [])

    def test_failed_publication_cleans_temporary_file(self):
        with patch.object(cli.os, "link", side_effect=OSError("publication failed")):
            with self.assertRaisesRegex(OSError, "publication failed"):
                cli.snapshot_file(self.board, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".board-snapshot-*")), [])

    def test_nonfinite_snapshot_and_failed_board_load_refuse(self):
        self.observer.return_value = {"source": {}, "bad": float("nan")}
        with self.assertRaises(ValueError):
            cli.snapshot_file(self.board, self.output)
        self.load.return_value = None
        with self.assertRaisesRegex(SnapshotError, "could not load"):
            cli.snapshot_file(self.board, self.output)
        self.assertFalse(self.output.exists())

    def test_help_needs_neither_pcbnew_nor_wx(self):
        script = str(Path(cli.__file__).resolve())
        code = ("import runpy,sys; sys.modules['pcbnew']=None; sys.modules['wx']=None; "
                "sys.path.insert(0,sys.argv[1]); sys.argv=[sys.argv[2],'--help']; "
                "runpy.run_path(sys.argv[0],run_name='__main__')")
        result = subprocess.run([sys.executable, "-c", code,
                                 str(Path(script).parent), script],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--board", result.stdout)

    def test_unavailable_producer_version_refuses_without_output(self):
        del sys.modules["pcbnew"].GetBuildVersion
        with self.assertRaisesRegex(SnapshotError, "producer version unavailable.*GetBuildVersion"):
            cli.snapshot_file(self.board, self.output)
        sys.modules["pcbnew"].GetBuildVersion = self.version
        self.version.side_effect = RuntimeError("native version failure")
        with self.assertRaisesRegex(SnapshotError, "producer version unavailable.*native version failure"):
            cli.snapshot_file(self.board, self.output)
        self.load.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_invalid_producer_version_is_not_fabricated(self):
        for value in [None, "", "  ", 99]:
            self.version.return_value = value
            with self.subTest(value=value), self.assertRaisesRegex(
                    SnapshotError, "producer version unavailable.*nonempty string"):
                cli.snapshot_file(self.board, self.output)
        self.load.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
