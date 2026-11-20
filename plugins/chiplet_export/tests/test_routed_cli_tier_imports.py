# SPDX-License-Identifier: GPL-3.0-or-later
"""The routed CLI process imports no PyYAML and no third-party module.

``chiplet-export`` runs the CLI under the system Python the KiCad fork
installs pcbnew into. That interpreter has none of the worker's packages:
not PyYAML, so not the vendored ``chiplet_format_io`` either, and not
KLayout. On the routed path the CLI reads its three inputs by running
routed_inputs.py under the worker interpreter (RoutedSource.preflight), and
computes the rest itself.

The CLI runs here in a child process whose ``import yaml`` fails, as it does
under that interpreter, with a stand-in worker interpreter that reads the
inputs with this Python and stops at the drawing. The CLI must get to the
drawing, must never try PyYAML, and may import no third-party module beyond
CLI_TIER_THIRD_PARTY: a module this test does not know of would fail in the
release image the same way PyYAML did.
"""

import json
import os
import subprocess
import sys

from chiplet_export import connection_methods

from .test_routed_export import CLI_SOURCE, _executable, _routed_argv, needs_manifest

#: The third-party modules the routed CLI process may import. The CLI tier is
#: the standard library, the plugin's own modules and this one: the
#: interconnect PDK's manifest reader, loaded by path from the PDK
#: (writers/connection_stacks.py), not from the interpreter, and itself
#: standard library only.
CLI_TIER_THIRD_PARTY = frozenset({"interconnect_manifest"})

#: The CLI, run with ``import yaml`` failing, and its imports written out at
#: exit: the third-party top-level modules (neither standard library nor the
#: plugin's) and whether the vendored reader was loaded.
_CHILD = r"""
import atexit, importlib.abc, json, os, runpy, sys
record, plugins = sys.argv[1], os.path.realpath(sys.argv[2])
class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "yaml" or name.startswith("yaml."):
            with open(record + ".yaml", "a") as f:
                f.write(name + "\n")
            raise ImportError("no PyYAML in the CLI's interpreter")
        return None
sys.meta_path.insert(0, Finder())
before = set(sys.modules)
def census():
    third_party = set()
    for name in set(sys.modules) - before:
        top = name.partition(".")[0]
        if top in sys.stdlib_module_names or top.startswith("_routed_cli"):
            continue
        origin = getattr(sys.modules.get(top), "__file__", None) or ""
        if origin and os.path.realpath(origin).startswith(plugins + os.sep):
            continue
        third_party.add(top)
    with open(record, "w") as f:
        json.dump({"third_party": sorted(third_party),
                   "vendored_reader": "chiplet_format_io" in sys.modules}, f)
atexit.register(census)
sys.argv = sys.argv[3:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


@needs_manifest
def test_the_routed_cli_imports_no_yaml_and_no_third_party_module(tmp_path):
    records = tmp_path / "records"
    records.mkdir()
    report = json.dumps({"format": connection_methods.FORMAT,
                         "version": connection_methods.VERSION, "methods": []})
    worker_python = _executable(
        tmp_path / "worker-python",
        '#!/bin/sh\necho "$@" >> "%s"\n'
        'case "$1" in *connection_methods.py) printf %%s \'%s\'; exit 0;; '
        '*routed_inputs.py) exec "%s" "$@";; esac\n'
        'exit 1\n' % (records / "worker", report, sys.executable))
    env = dict(os.environ, KICAD_CHIPLET_PYTHON=str(worker_python))
    plugins = CLI_SOURCE.resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(records / "cli"), str(plugins),
         str(CLI_SOURCE), *_routed_argv(tmp_path)],
        env=env, capture_output=True, text=True, timeout=120, check=False)
    output = result.stdout + result.stderr

    assert not (records / "cli.yaml").exists(), (
        "the routed CLI process imports PyYAML, which its interpreter does "
        "not have:\n" + output)
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
