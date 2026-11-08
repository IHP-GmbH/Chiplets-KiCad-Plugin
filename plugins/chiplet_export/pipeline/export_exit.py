# SPDX-License-Identifier: GPL-3.0-or-later
"""
Exit codes of the headless export (``chiplet-export``).

One table for ``chiplet_export_cli.py`` and ``tests/regenerate_wirebond_demo.py``:
both hand their ``ExportResult`` to :func:`export_exit` and exit with the code it
returns. Exit 0 means what the run reports held: the files were written and the
assembly DRC passed. A DRC that did not pass is exit 3 unless the caller said it
does not require the DRC (``--allow-drc-fail``), and then the last line says so.

This table is chiplet-export's own. Other tools in the ecosystem keep theirs.

Pure stdlib; reads the result by attribute, so it imports nothing from the
pipeline and runs under stock pytest.
"""

from typing import Tuple


EXIT_OK = 0
EXIT_NOT_WRITTEN = 1
EXIT_BOARD = 2
EXIT_DRC_NOT_PASSED = 3

EXIT_TABLE_HELP = """\
exit codes:
  0  the files were written and the assembly DRC PASSED
     (or did not pass, under --allow-drc-fail)
  1  nothing trustworthy was written: a refused board, a failed worker,
     a .chiplet that could not be replaced, or a crash before the DRC
  2  the board is missing or KiCad cannot read it; also a usage error
  3  the files were written, the assembly DRC did not pass: FAILED,
     REFUSED or NOT RUN (a DRC stage that raised included)"""


def files_written(result) -> bool:
    """True when the export produced its files: the worker exited 0 and
    nothing after it (the .chiplet replace) failed."""
    return result.exit_code == 0 and not result.error


def drc_outcome(result) -> str:
    """How the assembly DRC ended, in the words the last line uses.

    run_drc's own exit 1 carries violations, refusals and crashes alike, so a
    nonzero run_drc exit is "failed or refused". A refusal the export made
    itself (an adapter that is not an id) is REFUSED.
    """
    code = result.assembly_drc_exit_code
    if code == 0 and not result.assembly_drc_error:
        return "PASSED"
    if result.assembly_drc_refused:
        return "REFUSED"
    if result.assembly_drc_error:
        return "NOT RUN, the DRC stage raised (traceback on stderr)"
    if code == -1:
        return "NOT RUN"
    return "failed or refused (run_drc exit %d)" % code


def export_exit(result, allow_drc_fail: bool = False) -> Tuple[int, str]:
    """The exit code for ``result`` and the last line to print, or "".

    No line is added when nothing was written (exit 1, the summary already
    says why) or when the DRC passed. The line never carries the token
    ``assembly DRC:``, which belongs to the verdict line alone.
    """
    if not files_written(result):
        return EXIT_NOT_WRITTEN, ""
    outcome = drc_outcome(result)
    if outcome == "PASSED":
        return EXIT_OK, ""
    if allow_drc_fail:
        return EXIT_OK, (
            "DRC not required (--allow-drc-fail): files written, assembly "
            "DRC not passed (%s); exit 0" % outcome)
    return EXIT_DRC_NOT_PASSED, (
        "FAIL: files written, assembly DRC not passed (%s); exit 3" % outcome)
