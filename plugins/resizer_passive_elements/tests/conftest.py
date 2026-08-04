# SPDX-License-Identifier: GPL-3.0-or-later
"""Test bootstrap.

The plugin modules use relative imports (``from . import paths``), so the
suite has to import them as the ``resizer_passive_elements`` package: put the
``plugins/`` directory on ``sys.path`` and let the package name come from the
directory, exactly as pcbnew loads it through the KiCad symlink.
"""
import sys
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parents[2]

if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))
