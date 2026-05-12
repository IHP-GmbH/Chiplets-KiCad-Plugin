# SPDX-License-Identifier: GPL-2.0-or-later
"""
Locate the worker Python interpreter and the vendored hyp_to_gds.py.

The plugin runs inside KiCad's bundled Python, which does not have
klayout or PyYAML available. Discovery returns the path of an external
Python that does.

Resolution chain for the worker interpreter (first hit wins):

  1. Environment variable ``KICAD_CHIPLET_PYTHON``
  2. ``<plugin_dir>/.venv/bin/python3`` (recommended setup)
  3. KiCad project text variable ``KICAD_CHIPLET_PYTHON`` when a
     ``board`` is supplied (Board Setup > Text Variables)
  4. ``shutil.which('python3')`` if a subprocess probe can
     ``import klayout`` and ``import yaml``

Each step is independently testable; the test suite substitutes the
filesystem and subprocess calls with mocks.
"""

import os
import shutil
import subprocess
from pathlib import Path


WORKER_ENV_VAR = "KICAD_CHIPLET_PYTHON"
PROBE_TIMEOUT_SECONDS = 10


class DiscoveryError(Exception):
    """Base class for discovery failures."""


class WorkerPythonNotFoundError(DiscoveryError):
    """Raised when no usable worker Python can be located."""


class HypToGdsNotFoundError(DiscoveryError):
    """Raised when the vendored hyp_to_gds.py file is missing."""


def _is_executable(path):
    return path.is_file() and os.access(str(path), os.X_OK)


def _probe_imports(python_path):
    """Return True if `python_path` can import klayout and yaml."""
    try:
        result = subprocess.run(
            [str(python_path), "-c", "import klayout.db, yaml"],
            check=False,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _lookup_text_var(board, name):
    """Best-effort lookup of a KiCad project text variable.

    Mirrors the SWIG-map handling in writers/chiplet_writer.py so that
    a missing or differently-shaped binding never crashes the plugin.
    """
    if board is None:
        return None
    try:
        project = board.GetProject()
    except Exception:
        return None
    if project is None:
        return None
    try:
        text_vars = project.GetTextVars()
    except Exception:
        return None
    try:
        if name in text_vars:
            return str(text_vars[name])
    except (TypeError, KeyError):
        pass
    if hasattr(text_vars, "count") and hasattr(text_vars, "at"):
        try:
            if text_vars.count(name):
                return str(text_vars.at(name))
        except Exception:
            pass
    return None


def _venv_python(plugin_dir):
    candidate = Path(plugin_dir) / ".venv" / "bin" / "python3"
    return candidate if _is_executable(candidate) else None


def _venv_bootstrap_help(plugin_dir):
    return (
        "  cd {dir}\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/pip install -r requirements.txt"
    ).format(dir=plugin_dir)


def find_worker_python(plugin_dir, board=None):
    """Locate a Python interpreter that can run hyp_to_gds.py.

    Args:
        plugin_dir: Path of the installed plugin (used to resolve the
            recommended .venv).
        board: Optional ``pcbnew.BOARD`` for project text variable
            override.

    Returns:
        Absolute path (str) of the worker interpreter.

    Raises:
        WorkerPythonNotFoundError if no candidate succeeds. The error
        message describes the recommended venv bootstrap.
    """
    plugin_dir = str(Path(plugin_dir).resolve())
    tried = []

    env_value = os.environ.get(WORKER_ENV_VAR)
    if env_value:
        tried.append(("env %s" % WORKER_ENV_VAR, env_value))
        if _is_executable(Path(env_value)):
            return str(Path(env_value).resolve())

    venv = _venv_python(plugin_dir)
    if venv is not None:
        return str(venv.resolve())
    tried.append((
        "local .venv",
        str(Path(plugin_dir) / ".venv" / "bin" / "python3"),
    ))

    proj_value = _lookup_text_var(board, WORKER_ENV_VAR)
    if proj_value:
        tried.append(("project text var %s" % WORKER_ENV_VAR, proj_value))
        if _is_executable(Path(proj_value)):
            return str(Path(proj_value).resolve())

    which = shutil.which("python3")
    if which:
        tried.append(("PATH python3", which))
        if _probe_imports(which):
            return str(Path(which).resolve())

    raise WorkerPythonNotFoundError(
        "Could not locate a Python interpreter with klayout + PyYAML.\n"
        "Tried:\n  - "
        + "\n  - ".join("%s: %s" % c for c in tried)
        + "\n\nTo create the recommended worker venv:\n\n"
        + _venv_bootstrap_help(plugin_dir)
    )


def find_hyp_to_gds(plugin_dir):
    """Return absolute path to the vendored hyp_to_gds.py.

    Raises:
        HypToGdsNotFoundError if the script is missing from the plugin
        directory (the installation is broken).
    """
    candidate = (Path(plugin_dir) / "hyp_to_gds.py").resolve()
    if not candidate.is_file():
        raise HypToGdsNotFoundError(
            "hyp_to_gds.py not found next to plugin at %s. The plugin "
            "installation appears incomplete." % candidate
        )
    return str(candidate)
