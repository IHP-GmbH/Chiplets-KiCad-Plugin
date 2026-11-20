# SPDX-License-Identifier: GPL-3.0-or-later
"""The routed path's three inputs, read by the vendored reader: worker tier.

The routed-geometry document, the elaborated .chiplet and the routed io_pads
manifest are read through the vendored ``chiplet_format_io`` (the version
policy, and the .chiplet's YAML). That package imports PyYAML, which the
CLI's interpreter does not have: the CLI runs under the system Python KiCad
installs pcbnew into. So the CLI runs this file under the worker interpreter,
as it runs connection_methods.py, and reads the report it prints
(RoutedSource.preflight). ``read_inputs`` reads them in the calling process,
for a caller that has PyYAML (routed.write_intermediates).

Run as a script::

    python routed_inputs.py DOCUMENT CHIPLET IO_PADS IO_PADS_FRAME

prints one JSON report on stdout: ``inputs`` (the document, the .chiplet and
the io pads, as read) or ``refused`` (why not), and ``warnings`` (the
version warnings the reads gave, in order). Exit 0 whenever the report was
printed.

The module also holds the report's reader, so the writer and the reader share
one copy. It imports only the standard library and stdlib-only siblings at
module level; the vendored reader is loaded only when the inputs are read.
"""

import contextlib
import json
import sys
import warnings

try:
    from . import io_pads_manifest, routed_document, vendored
except ImportError:
    import io_pads_manifest
    import routed_document
    import vendored

FORMAT = "chiplet_export.routed_inputs"
VERSION = 1


class InputsRefused(Exception):
    """The routed inputs cannot be read for the export; nothing is written."""


class ReportError(ValueError):
    """A routed-inputs report that cannot be trusted."""


def read_inputs(document_path, chiplet_path, io_pads_path, *, io_pads_frame,
                on_warn=None):
    """(document, chiplet, io_pads): the routed-geometry document, checked;
    the .chiplet it binds, loaded; the io pads of the manifest in
    ``io_pads_frame``. InputsRefused otherwise, in that order."""
    try:
        document = routed_document.read_document(document_path, on_warn=on_warn)
        routed_document.check_chiplet_binding(document, chiplet_path)
    except routed_document.DocumentRefused as exc:
        raise InputsRefused(str(exc)) from exc
    cfio = vendored.chiplet_format_io()
    try:
        chiplet = cfio.load(chiplet_path)
    except (OSError, cfio.ChipletFormatError) as exc:
        raise InputsRefused("cannot read %s: %s" % (chiplet_path, exc)) from exc
    try:
        io_pads = io_pads_manifest.read(io_pads_path, on_warn=on_warn,
                                        frame=io_pads_frame)
    except io_pads_manifest.IoPadsRefused as exc:
        raise InputsRefused(str(exc)) from exc
    return document, chiplet, io_pads


def _carried(value, path):
    """``value`` unchanged through JSON, or InputsRefused naming ``path``:
    a YAML date, a key that is not text or a NaN would not survive."""
    try:
        text = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InputsRefused("%s holds a value the export cannot carry: %s"
                            % (path, exc)) from exc
    if json.loads(text) != value:
        raise InputsRefused("%s holds a value the export cannot carry "
                            "unchanged (a key that is not text)" % path)
    return value


def report(document_path, chiplet_path, io_pads_path, io_pads_frame):
    """The report document. Every version warning the reads give is in it,
    in order, the vendored reader's own (a newer .chiplet format minor)
    included; any other warning goes to stderr, as it would unrecorded."""
    # Every read gives its version warnings to the warnings channel when it
    # is given no on_warn; the filters stay the interpreter's, so a run that
    # turns warnings into errors refuses here as it would in process.
    version_warning = vendored.chiplet_format_io().ContractVersionWarning
    with warnings.catch_warnings(record=True) as caught:
        try:
            inputs = read_inputs(document_path, chiplet_path, io_pads_path,
                                 io_pads_frame=io_pads_frame)
            body = {"inputs": [_carried(value, path) for value, path in zip(
                inputs, (document_path, chiplet_path, io_pads_path))]}
        except InputsRefused as exc:
            body = {"refused": str(exc)}
    messages = []
    for warning in caught:
        if issubclass(warning.category, version_warning):
            messages.append(str(warning.message))
        else:
            warnings.showwarning(warning.message, warning.category,
                                 warning.filename, warning.lineno)
    return dict(format=FORMAT, version=VERSION, warnings=messages, **body)


def parse_report(text):
    """``(inputs, warnings, refused)`` from a report: ``inputs`` a tuple
    (document, chiplet, io_pads) and ``refused`` None, or ``inputs`` None and
    ``refused`` why. ReportError when the text is no report of this version."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ReportError("no JSON report (%s)" % exc) from exc
    if not isinstance(document, dict):
        document = {}
    version = document.get("version")
    messages = document.get("warnings")
    if (document.get("format") != FORMAT or type(version) is not int
            or version != VERSION or not isinstance(messages, list)
            or not all(isinstance(m, str) for m in messages)):
        raise ReportError("an unrecognized report")
    keys = set(document) - {"format", "version", "warnings"}
    if keys == {"refused"} and isinstance(document["refused"], str):
        return None, messages, document["refused"]
    inputs = document.get("inputs")
    if (keys != {"inputs"} or not isinstance(inputs, list) or len(inputs) != 3
            or not isinstance(inputs[0], dict) or not isinstance(inputs[1], dict)
            or not isinstance(inputs[2], list)):
        raise ReportError("a malformed report")
    return tuple(inputs), messages, None


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4:
        sys.stderr.write("usage: routed_inputs.py DOCUMENT CHIPLET IO_PADS "
                         "IO_PADS_FRAME\n")
        return 2
    # A module the reads load may print; stdout carries only the report.
    with contextlib.redirect_stdout(sys.stderr):
        document = report(*args)
    json.dump(document, sys.stdout, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
