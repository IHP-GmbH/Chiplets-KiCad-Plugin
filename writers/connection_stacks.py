# SPDX-License-Identifier: GPL-2.0-or-later
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
import sys
from pathlib import Path


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
    return 'interconnect:\n  adapter: "%s"\n\n' % adapter


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
        methods = list(im.list_methods())
        adapters = sorted({im.adapter_for(m) for m in methods} - {None, ""})
    except Exception as exc:
        sys.stderr.write(
            "Warning: interconnect manifest not discoverable (%s); skipping "
            "adapter/method validation -- studio and the assembly DRC "
            "validate downstream.\n" % exc
        )
        return

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
