# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared YAML scalar escaping for the ``.chiplet`` writers.

KiCad-derived strings (footprint references, net names, field text, file
paths) are third-party data and can carry YAML metacharacters. They are
interpolated into the ``.chiplet`` output, so they must be escaped or the file
becomes malformed/injectable.

These helpers MIRROR ``escapeYamlDq()`` / ``yamlScalar()`` in the C++ exporter
(kicad/pcbnew/exporters/export_chiplet.cpp). The two producers must stay
byte-identical (tests/test_byte_exact_writers.py), so keep both ends in sync.
"""

import re

# A value safe to emit as a bare (unquoted) YAML scalar: a plain token whose
# first char is alnum/underscore, that is not a reserved word. The end anchor
# is \Z, not $: Python's $ also matches just before a trailing newline, which
# would let a value ending in '\n' slip through bare (a raw newline injected
# into the YAML) and diverge from the C++ isSafeBareScalar byte loop, which
# rejects the newline. \Z matches only the true end of string, keeping the two
# producers byte-identical.
_BARE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./+-]*\Z")
_RESERVED = {"true", "false", "yes", "no", "on", "off",
             "null", "none", "y", "n", "~"}


def escape_yaml_dq(value):
    """Escape a value for use INSIDE an already double-quoted YAML scalar.

    Backslash is escaped first so the escapes introduced afterwards are not
    doubled, matching the char-by-char C++ implementation.
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def yaml_scalar(value):
    """Render a value as a YAML scalar.

    Returns the value bare when it is an unambiguous plain token, otherwise
    double-quoted and escaped. Used for fields emitted without quotes in the
    template (ids, technology ids/keys, io_class).
    """
    s = str(value)
    if _BARE_RE.match(s) and s.lower() not in _RESERVED:
        return s
    return '"%s"' % escape_yaml_dq(s)
