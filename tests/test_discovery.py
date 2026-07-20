# SPDX-License-Identifier: GPL-3.0-or-later
"""
Unit tests for pipeline/discovery.py.

Stdlib + pytest only. No pcbnew dependency: the project text var
lookup is exercised via a lightweight fake BOARD/PROJECT pair.
"""

import os
import stat
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_kicad_plugin.pipeline import discovery  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class _FakeProject:
    def __init__(self, text_vars):
        self._vars = text_vars

    def GetTextVars(self):
        return self._vars


class _FakeBoard:
    def __init__(self, text_vars):
        self._project = _FakeProject(text_vars)

    def GetProject(self):
        return self._project


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(discovery.WORKER_ENV_VAR, raising=False)
    monkeypatch.delenv(discovery.ADK_ROOT_ENV_VAR, raising=False)


# ---------------------------------------------------------------------------
# find_worker_python
# ---------------------------------------------------------------------------

def test_env_var_override_wins(tmp_path, monkeypatch):
    exe = tmp_path / "python_env"
    _make_exe(exe)
    monkeypatch.setenv(discovery.WORKER_ENV_VAR, str(exe))
    # The env var takes precedence, so the venv path must not be probed.
    monkeypatch.setattr(
        discovery, "_venv_python",
        lambda d: pytest.fail("venv probed despite env override"),
    )
    assert discovery.find_worker_python(tmp_path) == str(exe.resolve())


def test_env_var_ignored_when_not_executable(tmp_path, monkeypatch):
    bogus = tmp_path / "not_executable"
    bogus.write_text("not exe")
    monkeypatch.setenv(discovery.WORKER_ENV_VAR, str(bogus))
    monkeypatch.setattr(discovery, "_venv_python", lambda d: None)
    monkeypatch.setattr(discovery.shutil, "which", lambda name: None)
    with pytest.raises(discovery.WorkerPythonNotFoundError):
        discovery.find_worker_python(tmp_path)


def test_falls_back_to_venv(tmp_path):
    venv_py = tmp_path / ".venv" / "bin" / "python3"
    _make_exe(venv_py)
    assert discovery.find_worker_python(tmp_path) == str(venv_py.resolve())


def test_project_text_var(tmp_path, monkeypatch):
    exe = tmp_path / "py_from_proj"
    _make_exe(exe)
    monkeypatch.setattr(discovery, "_venv_python", lambda d: None)
    board = _FakeBoard({discovery.WORKER_ENV_VAR: str(exe)})
    assert discovery.find_worker_python(tmp_path, board=board) == str(exe.resolve())


def test_project_text_var_swig_map_style(tmp_path, monkeypatch):
    """Validate the count()/at() fallback for std::map-style bindings."""
    exe = tmp_path / "py_from_proj_swig"
    _make_exe(exe)
    monkeypatch.setattr(discovery, "_venv_python", lambda d: None)

    class _SwigMap:
        def __init__(self, data):
            self._d = data

        def __contains__(self, key):
            raise TypeError("std::map operator[] not bound")

        def count(self, key):
            return 1 if key in self._d else 0

        def at(self, key):
            return self._d[key]

    board = _FakeBoard(_SwigMap({discovery.WORKER_ENV_VAR: str(exe)}))
    assert discovery.find_worker_python(tmp_path, board=board) == str(exe.resolve())


def test_path_probe_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "_venv_python", lambda d: None)
    monkeypatch.setattr(discovery.shutil, "which", lambda name: "/usr/bin/python3")
    monkeypatch.setattr(discovery, "_probe_imports", lambda py: True)
    # find_worker_python returns the path as given (absolute, but not
    # symlink-resolved). Resolving would short-circuit venvs found via
    # PATH.
    assert discovery.find_worker_python(tmp_path) == "/usr/bin/python3"


def test_path_probe_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "_venv_python", lambda d: None)
    monkeypatch.setattr(discovery.shutil, "which", lambda name: "/usr/bin/python3")
    monkeypatch.setattr(discovery, "_probe_imports", lambda py: False)
    with pytest.raises(discovery.WorkerPythonNotFoundError):
        discovery.find_worker_python(tmp_path)


def test_preview_reports_the_venv_and_its_source(tmp_path):
    venv_py = tmp_path / ".venv" / "bin" / "python3"
    _make_exe(venv_py)
    path, source = discovery.preview_worker_python(tmp_path)
    assert path == str(venv_py.resolve())
    assert source == "plugin .venv"


def test_preview_prefers_the_env_var(tmp_path, monkeypatch):
    exe = tmp_path / "py_from_env"
    _make_exe(exe)
    _make_exe(tmp_path / ".venv" / "bin" / "python3")
    monkeypatch.setenv(discovery.WORKER_ENV_VAR, str(exe))
    assert discovery.preview_worker_python(tmp_path) == (
        str(exe.resolve()), "$" + discovery.WORKER_ENV_VAR)


def test_preview_reads_the_project_text_var(tmp_path, monkeypatch):
    exe = tmp_path / "py_from_proj"
    _make_exe(exe)
    monkeypatch.setattr(discovery, "_venv_python", lambda d: None)
    board = _FakeBoard({discovery.WORKER_ENV_VAR: str(exe)})
    assert discovery.preview_worker_python(tmp_path, board=board) == (
        str(exe.resolve()), "project text variable")


def test_preview_never_probes_path(tmp_path, monkeypatch):
    """The PATH leg costs a subprocess with a multi-second timeout, and the
    dialog calls this while building widgets on the UI thread. Preview must
    report nothing rather than pay it."""
    monkeypatch.setattr(discovery, "_venv_python", lambda d: None)
    monkeypatch.setattr(discovery.shutil, "which", lambda name: "/usr/bin/python3")

    def _boom(py):
        raise AssertionError("preview must not probe imports")

    monkeypatch.setattr(discovery, "_probe_imports", _boom)
    assert discovery.preview_worker_python(tmp_path) == ("", "")


def test_error_message_describes_which_legs_are_probed(tmp_path, monkeypatch):
    """The message used to claim the .venv leg required a klayout+PyYAML
    import; it does not, and only the PATH candidate is probed."""
    monkeypatch.setattr(discovery, "_venv_python", lambda d: None)
    monkeypatch.setattr(discovery.shutil, "which", lambda name: None)
    with pytest.raises(discovery.WorkerPythonNotFoundError) as exc:
        discovery.find_worker_python(tmp_path)
    msg = str(exc.value)
    assert "only the PATH python3 candidate is probed" in msg


def test_all_fail_raises_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "_venv_python", lambda d: None)
    monkeypatch.setattr(discovery.shutil, "which", lambda name: None)
    monkeypatch.setattr(discovery, "_probe_imports", lambda py: False)
    with pytest.raises(discovery.WorkerPythonNotFoundError) as exc:
        discovery.find_worker_python(tmp_path)
    msg = str(exc.value)
    # The message must guide the user to the venv bootstrap commands.
    assert ".venv" in msg
    assert "pip install -r requirements.txt" in msg
    assert str(tmp_path.resolve()) in msg


def test_board_none_skips_text_var(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "_venv_python", lambda d: None)
    monkeypatch.setattr(discovery.shutil, "which", lambda name: None)
    # Without a board, the project lookup must not be reached.
    sentinel = {"called": False}

    def _spy(board, name):
        sentinel["called"] = bool(board)
        return None

    monkeypatch.setattr(discovery, "_lookup_text_var", _spy)
    with pytest.raises(discovery.WorkerPythonNotFoundError):
        discovery.find_worker_python(tmp_path, board=None)
    assert sentinel["called"] is False


# ---------------------------------------------------------------------------
# find_hyp_to_gds
# ---------------------------------------------------------------------------

def test_find_hyp_to_gds(tmp_path):
    target = tmp_path / "hyp_to_gds.py"
    target.write_text("# placeholder\n")
    assert discovery.find_hyp_to_gds(tmp_path) == str(target.resolve())


def test_find_hyp_to_gds_missing(tmp_path):
    with pytest.raises(discovery.HypToGdsNotFoundError):
        discovery.find_hyp_to_gds(tmp_path)


# ---------------------------------------------------------------------------
# find_adk_drc_runner
# ---------------------------------------------------------------------------

def _make_adk_tree(root):
    """Create a fake ``adk/klayout/drc/run_drc.py`` under ``root``."""
    runner = Path(root) / "klayout" / "drc" / "run_drc.py"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text("# fake ADK runner\n")
    return runner


def test_find_adk_runner_env_var_wins(tmp_path, monkeypatch):
    adk_root = tmp_path / "alt_adk"
    runner = _make_adk_tree(adk_root)
    monkeypatch.setenv(discovery.ADK_ROOT_ENV_VAR, str(adk_root))
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    assert discovery.find_adk_drc_runner(plugin_dir) == str(runner.absolute())


def test_find_adk_runner_sibling_fallback(tmp_path):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    runner = _make_adk_tree(tmp_path / "adk")
    assert discovery.find_adk_drc_runner(plugin_dir) == str(runner.absolute())


def test_find_adk_runner_project_text_var(tmp_path):
    adk_root = tmp_path / "proj_adk"
    runner = _make_adk_tree(adk_root)
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()  # sibling adk/ does NOT exist
    board = _FakeBoard({discovery.ADK_ROOT_ENV_VAR: str(adk_root)})
    assert discovery.find_adk_drc_runner(plugin_dir, board=board) \
        == str(runner.absolute())


def test_find_adk_runner_all_fail_raises_actionable(tmp_path):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    with pytest.raises(discovery.AdkRunnerNotFoundError) as exc:
        discovery.find_adk_drc_runner(plugin_dir)
    msg = str(exc.value)
    assert "ADK_ROOT" in msg
    assert "sibling of plugin_dir" in msg


def test_find_adk_runner_env_takes_precedence_over_sibling(tmp_path,
                                                             monkeypatch):
    # Both sibling and env var resolve; env var must win.
    sibling = _make_adk_tree(tmp_path / "adk")
    alt = _make_adk_tree(tmp_path / "alt_adk")
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    monkeypatch.setenv(discovery.ADK_ROOT_ENV_VAR, str(tmp_path / "alt_adk"))
    chosen = discovery.find_adk_drc_runner(plugin_dir)
    assert chosen == str(alt.absolute())
    assert chosen != str(sibling.absolute())


def test_find_adk_runner_dialog_override_beats_env(tmp_path, monkeypatch):
    # The dialog's explicit root outranks every other leg of the chain.
    env_adk = _make_adk_tree(tmp_path / "env_adk")
    chosen_adk = _make_adk_tree(tmp_path / "dialog_adk")
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    monkeypatch.setenv(discovery.ADK_ROOT_ENV_VAR, str(tmp_path / "env_adk"))
    chosen = discovery.find_adk_drc_runner(
        plugin_dir, root_override=str(tmp_path / "dialog_adk"))
    assert chosen == str(chosen_adk.absolute())
    assert chosen != str(env_adk.absolute())


def test_find_adk_runner_board_none_skips_text_var(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    sentinel = {"called": False}

    def _spy(board, name):
        sentinel["called"] = bool(board)
        return None

    monkeypatch.setattr(discovery, "_lookup_text_var", _spy)
    with pytest.raises(discovery.AdkRunnerNotFoundError):
        discovery.find_adk_drc_runner(plugin_dir, board=None)
    assert sentinel["called"] is False
