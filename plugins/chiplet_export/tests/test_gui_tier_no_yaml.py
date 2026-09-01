# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI-tier constraint net: the export guard path must run with NO PyYAML.

``orchestrator.run_export`` -- and the ``.chiplet`` clobber guard it calls --
runs in KiCad's bundled Python, which by contract has NO PyYAML and NO klayout
(discovery.py:4-7). A separate worker Python (with yaml + klayout) runs
``hyp_to_gds.py`` as a subprocess. A guard-path module that imports ``yaml`` or
the vendored ``chiplet_format_io`` (which imports ``yaml`` unconditionally)
therefore crashes the very first export in the real GUI process at import time --
a production-breaking regression CI never caught, because CI runs run_export only
under a yaml-equipped Python (CLI/regen paths), never the GUI tier.

Two layers, both pcbnew-free and yaml-optional so they run in the normal pytest
job:

* A DYNAMIC test spawns a fresh interpreter that installs an import hook raising
  ModuleNotFoundError for ``yaml``/``chiplet_format_io`` (PER-PROCESS only, so a
  spawned worker still gets yaml -- exactly like KiCad spawning the worker),
  imports the pipeline + writers modules (skipping the KiCad-bound pcbnew/wx),
  and then EXECUTES the real guard path. This runs the exact code the KiCad
  thread runs under the exact import constraint, so a reintroduced ``import
  yaml`` anywhere on the guard path fails CI even though CI has yaml.

* A STATIC AST lint forbids any ``yaml`` / ``klayout`` / ``chiplet_format_io``
  import node in the GUI-tier source (pipeline/, writers/, the package
  ``__init__``, the action, the dialog, and the pcbnew-bound ``fill_readback``
  helper), so the regression is caught by grep-fast static analysis too, before
  any code runs.
"""

import ast
import subprocess
import sys
from pathlib import Path

CHIPLET_EXPORT_ROOT = Path(__file__).resolve().parents[1]   # plugins/chiplet_export
PLUGINS_DIR = Path(__file__).resolve().parents[2]           # plugins


# --------------------------------------------------------------------------
# Layer 1: dynamic no-yaml import + exercise, in a spawned interpreter.
# --------------------------------------------------------------------------

_CHILD_SCRIPT = r'''
import sys, os, types, tempfile, importlib

BLOCKED_TOP = {"yaml", "chiplet_format_io", "klayout"}


class _GuiTierBlocker:
    """Raise ModuleNotFoundError for yaml / chiplet_format_io, this process only.

    Mirrors KiCad's bundled Python: no PyYAML, no vendored cfio (which imports
    yaml). Children spawned from here start with a clean meta_path and still see
    yaml, exactly like KiCad spawning the worker interpreter.
    """
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED_TOP:
            raise ModuleNotFoundError(
                "GUI-tier simulation: %r is unavailable" % name, name=name)
        return None


# Defensive: a fresh -c interpreter should not have these yet, but drop any so
# the hook actually fires on the next import.
for _m in list(sys.modules):
    if _m.split(".")[0] in BLOCKED_TOP:
        del sys.modules[_m]
sys.meta_path.insert(0, _GuiTierBlocker())

plugins_dir, chiplet_export_dir = sys.argv[1], sys.argv[2]
for _p in (plugins_dir, chiplet_export_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# Mirror conftest's alias shim (a no-op when the dir is named chiplet_export).
if (os.path.basename(chiplet_export_dir) != "chiplet_export"
        and "chiplet_export" not in sys.modules):
    _pkg = types.ModuleType("chiplet_export")
    _pkg.__path__ = [chiplet_export_dir]
    sys.modules["chiplet_export"] = _pkg

TARGETS = [
    "chiplet_export.pipeline.orchestrator",
    "chiplet_export.pipeline.chiplet_merge",
    "chiplet_export.pipeline.discovery",
    "chiplet_export.pipeline.runner",
    "chiplet_export.writers.chiplet_writer",
    "chiplet_export.writers.connection_stacks",
    "chiplet_export.writers.hyperlynx_writer",
    "chiplet_export.writers._yaml",
]
for name in TARGETS:
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name in ("pcbnew", "wx"):
            print("SKIP-IMPORT %s (needs %s)" % (name, exc.name))
            continue
        print("IMPORTFAIL %s -> ModuleNotFoundError: %s" % (name, exc.name))
        sys.exit(2)

# Execute the REAL guard path under the no-yaml constraint.
cm = importlib.import_module("chiplet_export.pipeline.chiplet_merge")

OWNED = (
    'format_version: "1.0"\n\nassembly:\n  name: demo\n  units: um\n\n'
    'components:\n  - id: die_a\n    type: die\n    position:\n'
    '      x: %.1f\n      y: 50.0\n      z: 0.0\n'
)
FLOW = "flow:\n  engine: interposer-pnr\n  steps:\n    - route\n"


def _w(p, t):
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(t)


def _r(p):
    with open(p, "r", encoding="utf-8") as fh:
        return fh.read()


d = tempfile.mkdtemp()
final = os.path.join(d, "demo.chiplet")
inter = os.path.join(d, "demo.intermediate.chiplet")

try:
    # baseline: record then detect -> not tripped
    _w(final, OWNED % 100.0)
    cm.record_exporter_content_digest(final)
    assert cm.foreign_hand_edit_detected(final) is False, "baseline tripped"

    # owned hand-edit -> tripped
    _w(final, OWNED % 123.0)
    assert cm.foreign_hand_edit_detected(final) is True, "owned edit did not trip"

    # foreign-only edit -> not tripped
    cm.record_exporter_content_digest(final)
    _w(final, (OWNED % 123.0) + "\n" + FLOW)
    assert cm.foreign_hand_edit_detected(final) is False, "foreign edit tripped"

    # carry_over appends the foreign block into the staged intermediate
    _w(final, (OWNED % 100.0) + "\n" + FLOW)
    _w(inter, OWNED % 100.0)
    carried = cm.carry_over_foreign_blocks(final, inter)
    assert carried == ["flow"], "carried=%r" % (carried,)
    assert "engine: interposer-pnr" in _r(inter), "flow not carried"
except AssertionError as exc:
    print("GUARDFAIL %s" % exc)
    sys.exit(3)

print("GUARD-OK")
sys.exit(0)
'''


def test_guard_path_imports_and_runs_without_yaml():
    """The real guard path imports and executes with yaml/cfio unavailable.

    Fails (naming the module) if any guard-path module pulls in yaml or
    chiplet_format_io -- the exact regression that crashed the first GUI export.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT,
         str(PLUGINS_DIR), str(CHIPLET_EXPORT_ROOT)],
        capture_output=True, text=True, timeout=120,
    )
    detail = "\n--- stdout ---\n%s\n--- stderr ---\n%s" % (proc.stdout, proc.stderr)
    assert proc.returncode == 0, (
        "GUI-tier (no-yaml) guard-path run failed (rc=%d).%s"
        % (proc.returncode, detail))
    assert "GUARD-OK" in proc.stdout, "guard path did not complete." + detail


# --------------------------------------------------------------------------
# Layer 2: static AST lint of the GUI-tier source.
# --------------------------------------------------------------------------

def _gui_tier_sources():
    """Every GUI-tier .py: pipeline/ + writers/ + the top-level GUI modules.

    The named files are the modules KiCad loads into its own process: the plugin
    package ``__init__``, the action, the dialog, and the fill read-back helper
    the dialog imports. ``fill_readback.py`` is pcbnew-bound so the dynamic layer
    cannot reach it; the static lint covers it (and the others) instead.

    Excludes vendor/ (the reference parser legitimately imports yaml),
    hyp_to_gds.py (runs in the worker tier), and tests/.
    """
    files = []
    for sub in ("pipeline", "writers"):
        files.extend(sorted((CHIPLET_EXPORT_ROOT / sub).rglob("*.py")))
    for name in ("__init__.py", "dialog_chiplet_export.py",
                 "chiplet_export_action.py", "fill_readback.py"):
        p = CHIPLET_EXPORT_ROOT / name
        if p.exists():
            files.append(p)
    return files


#: Top-level modules the GUI tier must never import (discovery.py:4-7 bans all
#: three from KiCad's bundled Python: yaml, the vendored cfio which imports yaml,
#: and klayout).
_FORBIDDEN_TOP = ("yaml", "klayout")


def _forbidden_imports(tree):
    """yaml / klayout / chiplet_format_io import nodes anywhere in `tree`.

    Walks the whole tree, so function-body imports are caught too. Note:
    discovery.py carries the substring ``import klayout.db, yaml`` only inside a
    subprocess command STRING, which is an ast.Constant, not an import node, so
    it is invisible here and needs no exemption. The writers'
    ``from ._yaml import ...`` is the local escaping helper (module ``_yaml``,
    not ``yaml``) and is likewise not matched.
    """
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] in _FORBIDDEN_TOP or "chiplet_format_io" in parts:
                    hits.append((node.lineno, "import %s" % alias.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            modparts = mod.split(".") if mod else []
            if (modparts and modparts[0] in _FORBIDDEN_TOP) \
                    or "chiplet_format_io" in modparts:
                hits.append((node.lineno, "from %s import ..." % mod))
            for alias in node.names:
                if alias.name == "chiplet_format_io":
                    hits.append((node.lineno,
                                 "from %s import %s" % (mod, alias.name)))
    return hits


def test_no_yaml_or_cfio_imports_in_gui_tier_source():
    """No GUI-tier module may import yaml or the vendored chiplet_format_io."""
    sources = _gui_tier_sources()
    assert sources, "no GUI-tier sources found (path wiring changed?)"

    violations = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, what in _forbidden_imports(tree):
            violations.append("%s:%d  %s" % (path.name, lineno, what))

    assert not violations, (
        "GUI-tier source imports yaml/chiplet_format_io (crashes the first "
        "KiCad export, which has neither):\n  " + "\n  ".join(violations))
