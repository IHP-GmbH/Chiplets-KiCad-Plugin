# SPDX-License-Identifier: GPL-2.0-or-later
"""
Locate the Python interpreter and hyp_to_gds.py path used to run the
GDS pipeline.

Search strategy (first hit wins):
  1. Env var override
  2. Local .venv next to the plugin directory
  3. Project text variable override (when invoked from pcbnew)
  4. PATH probe with `klayout` + `yaml` import verification

Implementation lands in Gate 47.5.
"""


def discover_worker_python(plugin_dir):
    """Return the absolute path of the worker Python interpreter.

    Raises:
        FileNotFoundError if no candidate satisfies the import probe.
    """
    raise NotImplementedError("Gate 47.5 placeholder")


def discover_hyp_to_gds(plugin_dir):
    """Return the absolute path of hyp_to_gds.py.

    Raises:
        FileNotFoundError if the script cannot be located.
    """
    raise NotImplementedError("Gate 47.5 placeholder")
