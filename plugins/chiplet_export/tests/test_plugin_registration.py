# SPDX-License-Identifier: GPL-3.0-or-later
"""Both plugin packages register their action only under pcbnew's loader.

Registering a pcbnew.ActionPlugin outside a running pcbnew has no KiCad
program behind it: the first registration trips an assert and the second
aborts the process, so one pytest over both suites died at collection. Each
package's __init__ now registers only while pcbnew.LoadPlugins imports it.

The first two cases run each import in a child with a stand-in pcbnew that
records registrations. The third imports both packages beside KiCad's own
pcbnew: this interpreter when it imports pcbnew (the KiCad image), else the
AppDir named by KICAD_APPDIR, run with KICAD_APPDIR_PYTHON (default python3).
"""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

PLUGINS = Path(__file__).resolve().parents[2]
PACKAGES = ("chiplet_export", "resizer_passive_elements")

STAND_IN = '''
import importlib, sys, types
pcbnew = types.ModuleType("pcbnew")
registered = []
class ActionPlugin:
    def register(self):
        registered.append(type(self).__name__)
pcbnew.ActionPlugin = ActionPlugin
exec("def LoadPluginModule(name):\\n    return importlib.import_module(name)",
     pcbnew.__dict__)
pcbnew.importlib = importlib
sys.modules["pcbnew"] = pcbnew
'''


def _child(python, body, extra_env=None):
    """Run ``body`` under ``python`` with the plugins directory importable."""
    env = dict(os.environ, **(extra_env or {}))
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(PLUGINS), env.get("PYTHONPATH"))))
    return subprocess.run([python, "-B", "-c", body], cwd=str(PLUGINS), env=env,
                          capture_output=True, text=True, timeout=120)


def test_a_plain_import_registers_nothing():
    proc = _child(sys.executable, STAND_IN + '''
import chiplet_export, resizer_passive_elements
assert chiplet_export.ChipletExportPlugin is not None
assert resizer_passive_elements.ResizerPassiveElementsPlugin is not None
print(registered)
''')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


def test_pcbnews_loader_registers_each_action():
    proc = _child(sys.executable, STAND_IN + '''
for name in %r:
    pcbnew.LoadPluginModule(name)
print(registered)
''' % (PACKAGES,))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "['ChipletExportPlugin', 'ResizerPassiveElementsPlugin']"


def _kicad_python():
    """(python, its extra env) that imports KiCad's pcbnew, or (None, None)."""
    if importlib.util.find_spec("pcbnew") is not None:
        return sys.executable, {}
    appdir = os.environ.get("KICAD_APPDIR")
    if appdir:
        usr = Path(appdir) / "usr"
        dist = usr / "lib" / "python3" / "dist-packages"
        if (dist / "pcbnew.py").is_file():
            return (os.environ.get("KICAD_APPDIR_PYTHON", "python3"),
                    {"PYTHONPATH": str(dist), "LD_LIBRARY_PATH": str(usr / "lib")})
    return None, None


def test_both_packages_import_beside_kicads_pcbnew(tmp_path):
    python, env = _kicad_python()
    if python is None:
        pytest.skip("needs pcbnew (KiCad Python); on a host, KICAD_APPDIR "
                    "names a KiCad AppDir")
    home = tmp_path / "home"
    home.mkdir()
    env = dict(env, HOME=str(home), KICAD_CONFIG_HOME=str(home / "kicad"))
    proc = _child(python, '''
import pcbnew, chiplet_export, resizer_passive_elements
assert chiplet_export.ChipletExportPlugin is not None
assert resizer_passive_elements.ResizerPassiveElementsPlugin is not None
''', env)
    assert proc.returncode == 0, (proc.returncode, proc.stderr[-2000:])
