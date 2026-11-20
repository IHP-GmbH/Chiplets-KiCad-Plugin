# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading the routed-geometry document, for the fields this package uses.

The checks are routed_document's, which the worker runs too, so a document
the worker would refuse is refused here, before any output; this module turns
its refusals into RoutedRefused. The .chiplet binding is checked where the
.chiplet is read (routed_inputs.py).
"""

from .. import routed_document as _copper
from ..pipeline.export_source import SourceRefused
from ..routed_document import (  # noqa: F401  (the names this package used)
    ARTIFACT, FRAME, SCHEMA, SUPPORTED_VERSION, sha256_file,
)


class RoutedRefused(SourceRefused):
    """The routed inputs cannot give the export's intermediates; nothing is
    written.

    Not a ``ValueError``: the decoders below raise those for their own
    causes, and a caller must not mistake one for the other.
    """

    def lines(self):
        return ["Routed intermediates refused: %s" % self]


def check_document(document, *, on_warn=None):
    """Refuse ``document`` unless it holds what the intermediates and the
    worker read (routed_document.check_document, which the worker runs
    too); RoutedRefused otherwise."""
    try:
        return _copper.check_document(document, on_warn=on_warn)
    except _copper.DocumentRefused as exc:
        raise RoutedRefused(str(exc)) from exc


def read_document(path, *, on_warn=None):
    """The routed-geometry document at ``path``, checked; RoutedRefused
    otherwise."""
    try:
        return _copper.read_document(path, on_warn=on_warn)
    except _copper.DocumentRefused as exc:
        raise RoutedRefused(str(exc)) from exc


def bound_files(document, document_path):
    """{ref: {"layout": path, "pins": path}}, checked against the sha256 the
    document binds (routed_document.bound_files, which the worker reads
    too); RoutedRefused otherwise."""
    try:
        return _copper.bound_files(document, document_path)
    except _copper.DocumentRefused as exc:
        raise RoutedRefused(str(exc)) from exc

