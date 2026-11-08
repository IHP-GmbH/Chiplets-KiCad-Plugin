# SPDX-License-Identifier: GPL-3.0-or-later
"""Publication permissions and bytes, without changing the GUI's umask."""

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import board_snapshot_cli as snapshot
import hyp_to_gds as worker
import output_files


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX umask/mode contract")


@contextmanager
def fixed_umask(value):
    # Tests run serially. Production must never change this process-wide value;
    # using the saved function lets cleanup restore it even if a check fails.
    set_mask = os.umask
    old = set_mask(value)
    try:
        with patch.object(os, "umask", side_effect=AssertionError("writer changed umask")):
            yield
    finally:
        set_mask(old)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("mask,expected", [(0o022, 0o644), (0o077, 0o600)])
def test_new_chiplet_honors_umask_without_changing_bytes(tmp_path, mask, expected):
    target = tmp_path / "new.chiplet"
    payload = 'format_version: "1.0"\nassembly:\n  name: example\n'
    with fixed_umask(mask):
        worker._atomic_write_text(target, payload)
    assert mode(target) == expected
    assert target.read_bytes() == payload.encode()
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("mask", [0o022, 0o077])
@pytest.mark.parametrize("existing_mode", [0o600, 0o640, 0o644])
def test_replacement_preserves_existing_permissions(tmp_path, mask, existing_mode):
    target = tmp_path / "existing.chiplet"
    target.write_bytes(b"old content")
    target.chmod(existing_mode)
    with fixed_umask(mask):
        worker._atomic_write_text(target, "new content\n")
    assert mode(target) == existing_mode
    assert target.read_bytes() == b"new content\n"


@pytest.mark.parametrize("failing_operation", ["fsync", "fchmod"])
def test_failed_replacement_preserves_mode_and_bytes(tmp_path, monkeypatch, failing_operation):
    target = tmp_path / "existing.chiplet"
    target.write_bytes(b"accepted content")
    target.chmod(0o600)
    def fail(*_args):
        raise OSError("staging failed")
    monkeypatch.setattr(worker.os, failing_operation, fail)
    with fixed_umask(0o022), pytest.raises(OSError, match="staging failed"):
        worker._atomic_write_text(target, "replacement")
    assert mode(target) == 0o600
    assert target.read_bytes() == b"accepted content"
    assert list(tmp_path.iterdir()) == [target]


@pytest.fixture
def observed_board(tmp_path, monkeypatch):
    board = tmp_path / "source.kicad_pcb"
    board.write_bytes(b"source board\n")
    monkeypatch.setitem(sys.modules, "pcbnew", SimpleNamespace(
        LoadBoard=lambda _path: object(), GetBuildVersion=lambda: "test-build"))
    monkeypatch.setattr(snapshot, "observe_board", lambda _board: {"source": {}})
    return board


@pytest.mark.parametrize("mask,expected", [(0o022, 0o644), (0o077, 0o600)])
def test_new_snapshot_honors_umask_and_preserves_serialization(observed_board, mask, expected):
    output = observed_board.parent / "snapshot.json"
    with fixed_umask(mask):
        result = snapshot.snapshot_file(observed_board, output)
    assert mode(output) == expected
    assert output.read_bytes() == (json.dumps(result, indent=2, sort_keys=True,
                                            allow_nan=False) + "\n").encode()
    assert observed_board.read_bytes() == b"source board\n"
    assert not list(output.parent.glob(".board-snapshot-*"))


def test_existing_snapshot_permissions_are_untouched(observed_board):
    output = observed_board.parent / "snapshot.json"
    output.write_bytes(b"accepted snapshot")
    output.chmod(0o600)
    with fixed_umask(0o022), pytest.raises(FileExistsError):
        snapshot.snapshot_file(observed_board, output)
    assert mode(output) == 0o600
    assert output.read_bytes() == b"accepted snapshot"


@pytest.fixture
def composer():
    path = Path(__file__).parents[1] / "compose_existing_carrier.py"
    spec = importlib.util.spec_from_file_location("mode_test_composer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def composition_sources(tmp_path):
    # A tiny native hierarchy exercises the actual KLayout serializer's mode
    # behavior. This does not establish DRC/LVS or fabrication validity.
    import klayout.db as db
    layout = db.Layout()
    layout.dbu = .001
    cell = layout.create_cell("SOURCE")
    cell.shapes(layout.layer(134, 0)).insert(db.Box(0, 0, 100, 100))
    path = tmp_path / "source.gds"
    layout.write(str(path))
    source = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "top_cell": "SOURCE", "dbu_um": .001}
    die = dict(source, id="U1", transform={"rotation_degrees": 0, "mirror_x": False,
                                        "translation_dbu": [1000, 0]})
    return source, [die]


@pytest.mark.parametrize("mask,expected", [(0o022, 0o644), (0o077, 0o600)])
def test_new_composed_gds_honors_umask(composer, composition_sources, tmp_path, mask, expected):
    carrier, dies = composition_sources
    target = tmp_path / "complete.gds"
    original = Path(carrier["path"]).read_bytes()
    with fixed_umask(mask):
        result = composer.compose_existing_carrier(carrier, dies, target)
    assert mode(target) == expected
    assert hashlib.sha256(target.read_bytes()).hexdigest() == result["output_sha256"]
    assert Path(carrier["path"]).read_bytes() == original
    assert not list(tmp_path.glob(".compose-*"))


def test_existing_gds_permissions_are_untouched(composer, composition_sources, tmp_path):
    carrier, dies = composition_sources
    target = tmp_path / "complete.gds"
    target.write_bytes(b"accepted gds")
    target.chmod(0o600)
    with fixed_umask(0o022), pytest.raises(composer.CompositionRefused):
        composer.compose_existing_carrier(carrier, dies, target)
    assert mode(target) == 0o600
    assert target.read_bytes() == b"accepted gds"


def test_composer_temp_collision_preserves_existing_file(
        composer, composition_sources, tmp_path, monkeypatch):
    carrier, dies = composition_sources
    occupied = tmp_path / ".compose-occupied.gds"
    occupied.write_bytes(b"existing staging file")
    occupied.chmod(0o600)
    names = iter(["occupied", "fresh"])
    monkeypatch.setattr(composer.secrets, "token_hex", lambda _size: next(names))
    target = tmp_path / "complete.gds"
    with fixed_umask(0o022):
        composer.compose_existing_carrier(carrier, dies, target)
    assert mode(target) == 0o644
    assert mode(occupied) == 0o600
    assert occupied.read_bytes() == b"existing staging file"
    assert not (tmp_path / ".compose-fresh.gds").exists()


def test_temporary_name_collision_cannot_modify_existing_file(tmp_path, monkeypatch):
    existing = tmp_path / ".output-occupied.tmp"
    existing.write_bytes(b"existing bytes")
    existing.chmod(0o600)
    names = iter(["occupied", "fresh"])
    monkeypatch.setattr(output_files.secrets, "token_hex", lambda _size: next(names))
    fd, created = output_files.create_output_temp(directory=tmp_path, prefix=".output-", suffix=".tmp")
    os.close(fd)
    assert created != str(existing)
    assert existing.read_bytes() == b"existing bytes"
    assert mode(existing) == 0o600


def test_composer_still_loads_by_path_without_plugin_on_sys_path(tmp_path):
    script = Path(__file__).parents[1] / "compose_existing_carrier.py"
    code = ("import importlib.util,sys; "
            "s=importlib.util.spec_from_file_location('standalone_composer',sys.argv[1]); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)")
    result = subprocess.run([sys.executable, "-B", "-I", "-c", code, str(script)],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
