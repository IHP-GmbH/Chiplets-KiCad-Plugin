# SPDX-License-Identifier: GPL-3.0-or-later
"""Report which interconnect methods this worker can draw a landing for.

Runs, for every method of the interconnect PDK manifest, the check that
hyp_to_gds.py refuses an export on, with the same PDK resolution. The export
dialog and chiplet-export run this command before they offer or use a method.
Prints one JSON document on stdout: each method's id and the reason it is
refused, null when it can be drawn.

The module also holds the report's reader and the rule that picks each die's
method, so the writer, the readers and the worker share one copy of each. It
imports only the standard library at module level.
"""

import argparse
import contextlib
import json
import sys

FORMAT = "chiplet_export.connection_methods"
VERSION = 1


class ReportError(ValueError):
    """A connection-methods report that cannot be trusted."""


def resolve_die_methods(die_refs, die_connections, default_method):
    """``{ref: method}`` for the dies that use a method.

    A die's own entry wins, else the assembly-wide default; a die whose entry
    or default is empty uses none and is left out. The worker's pad loop and
    every check made before it resolve dies with this.
    """
    resolved = {}
    for ref in die_refs:
        method = (die_connections or {}).get(ref, default_method)
        if method:
            resolved[ref] = method
    return resolved


def refused_landings(availability, die_methods):
    """``(ref, method, reason)``, sorted by ref, for each die whose method the
    report refuses. Ids the report does not name pass."""
    return [(ref, method, availability[method])
            for ref, method in sorted(die_methods.items())
            if availability.get(method)]


def report(hyp_to_gds):
    """The availability document: one entry per method, in manifest order."""
    availability = hyp_to_gds.connection_method_availability()
    return {
        "format": FORMAT,
        "version": VERSION,
        "methods": [{"id": method, "reason": reason}
                    for method, reason in availability.items()],
    }


def parse_report(text):
    """``{method: None or reason}`` from a report, or ReportError."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ReportError("no JSON report (%s)" % exc) from exc
    if not isinstance(document, dict):
        document = {}
    entries = document.get("methods")
    version = document.get("version")
    if (document.get("format") != FORMAT or type(version) is not int
            or version != VERSION or not isinstance(entries, list)):
        raise ReportError("an unrecognized report")
    availability = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"id", "reason"}:
            raise ReportError("a malformed method entry: %r" % (entry,))
        method, reason = entry["id"], entry["reason"]
        if (not isinstance(method, str) or not method or method in availability
                or not (reason is None or (isinstance(reason, str) and reason))):
            raise ReportError("a malformed method entry: %r" % (entry,))
        availability[method] = reason
    return availability


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="INTERCONNECT_PDK_ROOT and INTERPOSER_PDK_ROOT select the PDKs, "
               "as for hyp_to_gds.py. Exit 0 with the report on stdout; 5 or 6 "
               "when the interconnect manifest is present but unusable.")
    parser.parse_args(argv)
    # Imported here, not at module level: hyp_to_gds exits without klayout,
    # and --help must work without it.
    if __package__:
        from . import hyp_to_gds
    else:
        import hyp_to_gds
    try:
        # The PDK modules may print while they load; stdout carries only JSON.
        with contextlib.redirect_stdout(sys.stderr):
            document = report(hyp_to_gds)
    except hyp_to_gds.SourceRefused as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return exc.exit_code
    json.dump(document, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
