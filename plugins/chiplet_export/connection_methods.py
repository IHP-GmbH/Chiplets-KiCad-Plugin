# SPDX-License-Identifier: GPL-3.0-or-later
"""Report which interconnect methods this worker can draw a landing for.

Runs, for every method of the interconnect PDK manifest, the check that
hyp_to_gds.py refuses an export on, with the same PDK resolution. The export
dialog and chiplet-export run this command before they offer or use a method.
Prints one JSON document on stdout.
"""

import argparse
import contextlib
import json
import sys

FORMAT = "chiplet_export.connection_methods"
VERSION = 1


def report():
    """The availability document: one entry per method, in manifest order."""
    # Imported here, not at module level: hyp_to_gds exits without klayout,
    # and --help must work without it.
    if __package__:
        from . import hyp_to_gds
    else:
        import hyp_to_gds
    availability = hyp_to_gds.connection_method_availability()
    return {
        "format": FORMAT,
        "version": VERSION,
        "methods": [{"id": method, "available": reason is None,
                     "reason": reason}
                    for method, reason in availability.items()],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="INTERCONNECT_PDK_ROOT and INTERPOSER_PDK_ROOT select the PDKs, "
               "as for hyp_to_gds.py. Exit 0 with the report on stdout; 5 or 6 "
               "when the interconnect manifest is present but unusable.")
    parser.parse_args(argv)
    if __package__:
        from .hyp_to_gds import SourceRefused
    else:
        from hyp_to_gds import SourceRefused
    try:
        # The PDK modules may print while they load; stdout carries only JSON.
        with contextlib.redirect_stdout(sys.stderr):
            document = report()
    except SourceRefused as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return exc.exit_code
    json.dump(document, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
