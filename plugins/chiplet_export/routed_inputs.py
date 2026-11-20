# SPDX-License-Identifier: GPL-3.0-or-later
"""The routed path's three inputs, read by the vendored reader: worker tier.

The routed-geometry document, the elaborated .chiplet and the routed io_pads
manifest are read through the vendored ``chiplet_format_io`` (the version
policy, and the .chiplet's YAML). That package imports PyYAML, which the
CLI's interpreter does not have: the CLI runs under the system Python KiCad
installs pcbnew into. So the CLI never imports these readers. It runs this
file under the worker interpreter, as it runs connection_methods.py, and
takes the three inputs back as JSON (RoutedSource.preflight); the export's
in-process callers that run in the worker tier call ``read_inputs``.

Run as a script::

    python routed_inputs.py DOCUMENT CHIPLET IO_PADS IO_PADS_FRAME OUT

writes OUT, a JSON object: ``inputs`` (the document, the .chiplet and the io
pads, as read) or ``refused`` (why not), and ``warnings`` (the version
warnings the reads gave, in order). Exit 0 whenever OUT was written.
"""

import json
import sys

try:
    from . import io_pads_manifest, routed_document, vendored
except ImportError:
    import io_pads_manifest
    import routed_document
    import vendored


class InputsRefused(Exception):
    """The routed inputs cannot be read for the export; nothing is written."""


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


def _handed_over(inputs, chiplet_path):
    """``inputs`` as JSON text, refused unless JSON gives back the same data
    (a YAML date, a non-text key or a NaN in the .chiplet would not)."""
    try:
        text = json.dumps(list(inputs), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InputsRefused("%s holds a value the export cannot carry: %s"
                            % (chiplet_path, exc)) from exc
    if json.loads(text) != list(inputs):
        raise InputsRefused("%s holds a value the export cannot carry "
                            "unchanged (a key that is not text)" % chiplet_path)
    return text


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 5:
        sys.stderr.write("usage: routed_inputs.py DOCUMENT CHIPLET IO_PADS "
                         "IO_PADS_FRAME OUT\n")
        return 2
    document_path, chiplet_path, io_pads_path, frame, out = args
    warnings = []
    try:
        inputs = read_inputs(document_path, chiplet_path, io_pads_path,
                             io_pads_frame=frame, on_warn=warnings.append)
        body = '{"inputs": %s' % _handed_over(inputs, chiplet_path)
    except InputsRefused as exc:
        body = '{"refused": %s' % json.dumps(str(exc))
    with open(out, "w", encoding="utf-8") as f:
        f.write('%s, "warnings": %s}\n' % (body, json.dumps(warnings)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
