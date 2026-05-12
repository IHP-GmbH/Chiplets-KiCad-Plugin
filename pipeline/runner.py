# SPDX-License-Identifier: GPL-2.0-or-later
"""
Async subprocess runner for hyp_to_gds.py.

Streams stdout/stderr to a callback so the dialog can display live
progress, propagates the exit code, and supports cancel via wxProcess
kill.

Implementation lands in Gate 47.5.
"""


def run_hyp_to_gds(python_executable, hyp_to_gds_path, args, on_output, on_done):
    """Spawn hyp_to_gds.py as a subprocess and stream output.

    Args:
        python_executable: Absolute path to the worker Python.
        hyp_to_gds_path: Absolute path to hyp_to_gds.py.
        args: List of CLI arguments to pass after the script path.
        on_output: Callable invoked once per stdout/stderr line.
        on_done: Callable invoked with the exit code on termination.

    Returns:
        An opaque handle for cancel/kill operations.
    """
    raise NotImplementedError("Gate 47.5 placeholder")
