# SPDX-License-Identifier: GPL-3.0-or-later
"""Run native dialog smoke outside KiCad without registering an action plugin.

Usage from the repository root:
    xvfb-run -a python3 scripts/run_dialog_catalogue_wx.py -vv -s

Real wx and pcbnew are required. Only package-import registration is suppressed;
the tests retain real GUI/data objects and their ordinary shutdown lifecycle.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import patch


def main(argv=None):
    repo_root = Path(__file__).resolve().parents[1]
    plugin_root = repo_root / "plugins" / "chiplet_export"
    sys.path.insert(0, str(plugin_root.parent))

    import pcbnew

    # Pytest resolves the package containing tests/conftest.py before executing
    # the native test module. Its __init__ registers an action with KiCad's host
    # registry, which does not exist in this standalone process. Import it here
    # first, with that one operation disabled, and immediately restore register
    # (including inherited-method ownership) before pytest or any tests run.
    with patch.object(pcbnew.ActionPlugin, "register", autospec=True):
        package = importlib.import_module("chiplet_export")
    expected = (plugin_root / "__init__.py").resolve()
    actual = Path(package.__file__).resolve()
    if actual != expected:
        raise RuntimeError(
            "Native smoke imported %s; expected this checkout at %s"
            % (actual, expected))

    import pytest

    arguments = list(sys.argv[1:] if argv is None else argv)
    return pytest.main([
        "-c", str(repo_root / "pyproject.toml"),
        str(plugin_root / "tests" / "test_dialog_catalogue_wx.py"),
        *arguments,
    ])


if __name__ == "__main__":
    # Preserve pytest's result and normal interpreter/native-library shutdown.
    raise SystemExit(main())
