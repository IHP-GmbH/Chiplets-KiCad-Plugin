# SPDX-License-Identifier: GPL-3.0-or-later
"""What the routed CLI process may load: no KiCad, no PyYAML, no third-party
module beyond a stated list.

``chiplet-export`` runs the CLI under the system Python the KiCad fork
installs pcbnew into. On the routed path the board is never opened, so the
CLI must load neither pcbnew nor kicad-cli. And that interpreter has none of
the worker's packages: not PyYAML, so not the vendored ``chiplet_format_io``
either, and not KLayout. The CLI reads its three inputs by running
routed_inputs.py under the worker interpreter (RoutedSource.preflight), and
computes the rest itself.

The CLI runs here in a child process whose ``import pcbnew`` and ``import
yaml`` fail and are recorded, with a kicad-cli on PATH that records its
calls, and a stand-in worker interpreter that reads the inputs with this
Python and stops at the drawing. The CLI must get to the drawing having
tried neither, and may import no third-party module beyond
CLI_TIER_THIRD_PARTY: a module this test does not know of would fail in the
release image the same way PyYAML did.

It needs the interconnect PDK's manifest, so it skips on a checkout without
the PDK; the tiered runs have it.
"""

import json
import os
import subprocess
import sys

from chiplet_export import connection_methods

from .test_routed_export import CLI_SOURCE, _routed_argv, needs_manifest

#: The third-party modules the routed CLI process may import. The CLI tier is
#: the standard library, the plugin's own modules and this one: the
#: interconnect PDK's manifest reader, loaded by path from the PDK
#: (writers/connection_stacks.py), not from the interpreter, and itself
#: standard library only.
CLI_TIER_THIRD_PARTY = frozenset({"interconnect_manifest"})

#: The modules the child refuses, as the CLI's interpreter would or must.
BLOCKED = ("pcbnew", "yaml")

#: The CLI, with every BLOCKED import failing and recorded, and its imports
#: written out at exit: the third-party top-level modules and whether the
#: vendored reader was loaded. A module is the plugin's own when its top-level
#: name is one of the plugin's (its packages and script modules) and it was
#: loaded from the plugin; a virtualenv under the plugin is not the plugin.
_CHILD = r"""
import atexit, importlib.abc, json, os, runpy, sys
record, plugins, blocked = sys.argv[1], os.path.realpath(sys.argv[2]), sys.argv[3].split(",")
own = set()
for folder in (plugins, os.path.join(plugins, "chiplet_export")):
    for entry in os.listdir(folder):
        path = os.path.join(folder, entry)
        if entry.endswith(".py"):
            own.add(entry[:-3])
        elif os.path.isfile(os.path.join(path, "__init__.py")):
            own.add(entry)
class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.partition(".")[0] in blocked:
            with open(record + ".blocked", "a") as f:
                f.write(name + "\n")
            raise ImportError("%s is not in the CLI's interpreter" % name)
        return None
sys.meta_path.insert(0, Finder())
before = set(sys.modules)
def census():
    third_party = set()
    for name in set(sys.modules) - before:
        top = name.partition(".")[0]
        if top in sys.stdlib_module_names:
            continue
        origin = os.path.realpath(getattr(sys.modules.get(top), "__file__", None) or "/")
        if top in own and origin.startswith(plugins + os.sep):
            continue
        third_party.add(top)
    with open(record, "w") as f:
        json.dump({"third_party": sorted(third_party),
                   "vendored_reader": "chiplet_format_io" in sys.modules}, f)
atexit.register(census)
sys.argv = sys.argv[4:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _executable(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return path


@needs_manifest
def test_the_routed_cli_loads_no_kicad_no_yaml_and_no_third_party_module(tmp_path):
    records = tmp_path / "records"
    records.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "kicad-cli",
                '#!/bin/sh\necho "$@" >> "%s"\nexit 1\n' % (records / "kicad-cli"))
    report = json.dumps({"format": connection_methods.FORMAT,
                         "version": connection_methods.VERSION, "methods": []})
    worker_python = _executable(
        tmp_path / "worker-python",
        '#!/bin/sh\necho "$@" >> "%s"\n'
        'case "$1" in *connection_methods.py) printf %%s \'%s\'; exit 0;; '
        '*routed_inputs.py) exec "%s" "$@";; esac\n'
        'exit 1\n' % (records / "worker", report, sys.executable))
    env = dict(os.environ, KICAD_CHIPLET_PYTHON=str(worker_python),
               PATH="%s%s%s" % (bin_dir, os.pathsep, os.environ.get("PATH", "")))
    plugins = CLI_SOURCE.resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(records / "cli"), str(plugins),
         ",".join(BLOCKED), str(CLI_SOURCE), *_routed_argv(tmp_path)],
        env=env, capture_output=True, text=True, timeout=120, check=False)
    output = result.stdout + result.stderr

    blocked = records / "cli.blocked"
    assert not blocked.exists(), (
        "the routed CLI process imports %s, which its interpreter does not "
        "have or must not load:\n%s" % (blocked.read_text().split(), output))
    assert not (records / "kicad-cli").exists(), "the routed CLI ran kicad-cli"
    calls = ((records / "worker").read_text().splitlines()
             if (records / "worker").exists() else [])
    assert any("--routed-geometry" in line for line in calls), (
        "the routed CLI did not get to the drawing:\n" + output)
    census = json.loads((records / "cli").read_text())
    assert not census["vendored_reader"], "the CLI process loaded the vendored reader"
    extra = sorted(set(census["third_party"]) - CLI_TIER_THIRD_PARTY)
    assert not extra, (
        "the routed CLI process imports third-party module(s) %s; its "
        "interpreter has none beyond %s" % (", ".join(extra),
                                             sorted(CLI_TIER_THIRD_PARTY)))
