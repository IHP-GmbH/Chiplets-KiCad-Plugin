# SPDX-License-Identifier: GPL-3.0-or-later
"""Test bootstrap: make ``chiplet_kicad_plugin.*`` imports resolve no matter
what the checkout directory is called.

The canonical ecosystem dirname is ``chiplet_kicad_plugin``, but a default
clone of the upstream repository lands in ``Chiplets-KiCad-Plugin`` (and CI
checkouts use the repository name, which is not even a valid Python
identifier). The absolute imports in the tests resolve only for the
canonical dirname; otherwise alias the repo root as the package here, before
any test module imports.
"""

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

if (_ROOT.name != "chiplet_kicad_plugin"
        and "chiplet_kicad_plugin" not in sys.modules):
    _pkg = types.ModuleType("chiplet_kicad_plugin")
    _pkg.__path__ = [str(_ROOT)]
    sys.modules["chiplet_kicad_plugin"] = _pkg
