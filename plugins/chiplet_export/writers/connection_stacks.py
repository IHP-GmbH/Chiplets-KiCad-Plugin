# SPDX-License-Identifier: GPL-3.0-or-later
"""
Connection-stacks and interconnect-block emission for the .chiplet writer.

Pure (no pcbnew) so it is unit-testable on host Python. Reads the interconnect
PDK manifest (the single source of truth) instead of hardcoding the bump table,
and formats the YAML byte-identically to the pre-split literal that
export_chiplet.cpp still emits -- so the byte-exact writer-parity gate stays
green.

The manifest reader (interconnect_pdk/libs.tech/klayout/python/
interconnect_manifest.py) is located via $INTERCONNECT_PDK_ROOT or a
sibling-repo search, then imported.
"""

import os
import re
import sys
from pathlib import Path

from ._yaml import escape_yaml_dq


def _manifest_reader():
    """Import and return the interconnect_pdk manifest reader module.

    Resolution: $INTERCONNECT_PDK_ROOT/libs.tech/klayout/python, then a
    sibling-repo walk up from this file (locates
    <project>/interconnect_pdk/libs.tech/klayout/python). Raises ImportError
    with actionable text if the interconnect PDK is not installed.
    """
    if "interconnect_manifest" in sys.modules:
        return sys.modules["interconnect_manifest"]

    candidates = []
    py_subdir = ("libs.tech", "klayout", "python")
    env = os.environ.get("INTERCONNECT_PDK_ROOT")
    if env:
        candidates.append(Path(env).joinpath(*py_subdir))
    here = Path(__file__).resolve()
    for base in here.parents:
        # Canonical directory name first, then the GitHub repo name a
        # default clone produces (same alias order as hyp_to_gds).
        for dirname in ("interconnect_pdk", "IHP-Interconnect-IntM4TM2"):
            candidates.append((base / dirname).joinpath(*py_subdir))

    for cand in candidates:
        if (cand / "interconnect_manifest.py").is_file():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            import interconnect_manifest  # noqa: E402
            return interconnect_manifest

    raise ImportError(
        "interconnect_pdk reader not found. Set INTERCONNECT_PDK_ROOT to the "
        "interconnect_pdk repo root, or install it as a sibling of the plugin."
    )


def _fmt(value):
    """Format a dimension like the pre-split literal: 28.0, 44.0, 80.0."""
    return "%s" % float(value)


def emit_connection_stacks_block(manifest=None):
    """Return the ``connection_stacks:`` YAML block from the manifest.

    Byte-identical to the literal block the writer used to hardcode (and that
    export_chiplet.cpp still emits): the default bump library in manifest order,
    each method's description and layer list. Ends with a single newline; the
    caller adds the blank-line separator, mirroring the old code.
    """
    im = _manifest_reader()
    library = im.get_connection_library(manifest)
    lines = ["connection_stacks:"]
    for method_id, stack in library.items():
        lines.append("  %s:" % method_id)
        lines.append('    description: "%s"' % stack["description"])
        lines.append("    layers:")
        for layer in stack["layers"]:
            lines.append(
                "      - {name: %s, material: %s, height: %s, diameter: %s}"
                % (layer["name"], layer["material"],
                   _fmt(layer["height"]), _fmt(layer["diameter"]))
            )
    return "\n".join(lines) + "\n"


def emit_interconnect_block(adapter):
    """Return the ``interconnect:`` block declaring the adapter, or "" if empty.

    Mirrors the ``interposer:`` block. Emitted only when an adapter is set, so a
    design without an interconnect method produces no block (and no IXN checks).
    """
    if not adapter:
        return ""
    return 'interconnect:\n  adapter: "%s"\n\n' % escape_yaml_dq(adapter)


#: Every adapter id must match this. The registry contract fixes one regex for
#: all three id namespaces; it forbids "/", "~", a leading "." or "-", ".." and
#: "${...}" by construction.
#:
#: What it does NOT do, despite how it is easy to read: it does not separate an
#: id from a path. "intm4tm2.drc" matches, and so does any single-segment
#: relative filename, which is a real path. What actually forbids paths is
#: fail-closed lookup in a vetted set. Treat this as a shape check that rejects
#: the obvious, never as the authority that decides what a value may name.
#:
#: The contract writes the anchor as "$", but in Python "$" also matches just
#: BEFORE a trailing newline, so "intm4tm2\n" would pass and go straight into
#: the emitted double-quoted scalar. "\Z" is the absolute end of the string and
#: is the only anchor that holds here. Keeping it in the constant, rather than
#: relying on callers to use fullmatch, means the value is safe no matter how
#: it is applied.
#:
#: An anchor is only safe within one dialect: "\Z" does not exist in ECMA-262,
#: so this pattern would mean something different to a JS or JSON-schema
#: validator than it does here, both looking correct against the same contract
#: text. That is worse than being wrong in one direction. This constant is read
#: only by Python today; if it is ever mirrored into a schema, express it in the
#: portable form (chiplet-spec uses "(?![\s\S])") rather than porting "\Z".
ADAPTER_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")


def validate_adapter_id(value, source):
    """Fail closed on an adapter id that is not a well-formed id.

    Validating here, at emit, is NOT a security boundary and must not be read
    as one: the producer of a hostile document is the attacker, so a document
    that never passed through this writer never met this check. What it buys
    is a good error naming the text variable the user has to fix, and it keeps
    this writer from originating a malformed id itself.

    The authoritative gate is at LOAD. Every consumer that reads an id out of
    a document validates it there and resolves fail-closed, and that includes
    this plugin reading back a ``.chiplet`` it did not write. An earlier
    version of this docstring said the opposite, that the gate was at the
    producer and consumers should resolve rather than re-validate; that rule
    is superseded, and it is worth knowing it was superseded because two
    careful implementers in two repos followed it into the same open read leg.

    An empty value means "unset" and is left to the caller's own default
    handling.

    This is a shape check only, and deliberately independent of
    :func:`validate_interconnect_ids`, which checks membership against the
    interconnect PDK manifest and degrades to a warning when that manifest is
    not discoverable. Shape must never degrade: on a host without the PDK the
    membership check is skipped, and the regex is then the only thing standing
    between a path-shaped text variable and the emitted document.

    Call this during data gathering, never mid-write: the emit sites run
    inside the open output file, so failing there would leave a partial
    ``.chiplet`` on disk.

    Raises:
        ValueError naming the offending value and where it came from.
    """
    if not value:
        return
    if not ADAPTER_ID_RE.match(value):
        raise ValueError(
            "invalid adapter id %r (%s); an adapter is an id, never a path, "
            "and must match %s" % (value, source, ADAPTER_ID_RE.pattern)
        )


def validate_interconnect_ids(adapter=None, die_methods=None):
    """Fail loudly on interconnect ids the manifest does not know.

    Called at export time so a typo in the INTERCONNECT_ADAPTER text variable
    or in a per-die CONNECTION field surfaces in the export run instead of
    later in studio or the assembly DRC. When the interconnect PDK is not
    discoverable the check degrades to a single stderr warning: the .chiplet
    is machine-local and both downstream consumers re-validate loudly.

    Raises:
        ValueError naming the offending id(s) and the valid set.
    """
    wanted_adapter = adapter or None
    wanted_methods = sorted({m for m in (die_methods or []) if m})
    if not wanted_adapter and not wanted_methods:
        return

    try:
        im = _manifest_reader()
    except ImportError as exc:
        # The interconnect PDK is genuinely absent: degrade to a warning (the
        # .chiplet is machine-local and both downstream consumers re-validate).
        sys.stderr.write(
            "Warning: interconnect manifest not discoverable (%s); skipping "
            "adapter/method validation -- studio and the assembly DRC "
            "validate downstream.\n" % exc
        )
        return
    # A present-but-malformed manifest must surface a real error here rather
    # than be swallowed into a skipped validation, so build the lists outside
    # the catch above.
    methods = list(im.list_methods())
    adapters = sorted({im.adapter_for(m) for m in methods} - {None, ""})

    if wanted_adapter and wanted_adapter not in adapters:
        raise ValueError(
            "unknown interconnect adapter '%s' (INTERCONNECT_ADAPTER text "
            "variable); the interconnect PDK manifest knows: %s"
            % (wanted_adapter, ", ".join(adapters))
        )
    unknown = [m for m in wanted_methods if m not in methods]
    if unknown:
        raise ValueError(
            "unknown interconnect method(s) %s (per-die CONNECTION fields); "
            "the interconnect PDK manifest knows: %s"
            % (", ".join("'%s'" % m for m in unknown), ", ".join(methods))
        )
