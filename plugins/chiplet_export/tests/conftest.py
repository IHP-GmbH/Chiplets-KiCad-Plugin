# SPDX-License-Identifier: GPL-3.0-or-later
"""Test bootstrap: make ``chiplet_export.*`` and top-level ``hyp_to_gds``
imports resolve no matter what the checkout directory is called or how
pytest is invoked.

The canonical package dirname is ``chiplet_export`` (it lives at
``plugins/chiplet_export`` in this repo and is symlinked under that name into
KiCad's plugins folder). A raw clone of the upstream repository lands in
``Chiplets-KiCad-Plugin`` and CI checkouts use the repository name, neither of
which is the package name. Two shims make the absolute imports resolve:

1. Put the package root on ``sys.path`` so the top-level ``import hyp_to_gds``
   used by several tests resolves, and so ``chiplet_export`` is importable when
   the directory really is named ``chiplet_export``.
2. If the directory is named something else, alias the package root as
   ``chiplet_export`` in ``sys.modules`` before any test module imports.
"""

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if (_ROOT.name != "chiplet_export"
        and "chiplet_export" not in sys.modules):
    _pkg = types.ModuleType("chiplet_export")
    _pkg.__path__ = [str(_ROOT)]
    sys.modules["chiplet_export"] = _pkg
