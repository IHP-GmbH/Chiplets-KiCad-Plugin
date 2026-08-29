# SPDX-License-Identifier: GPL-3.0-or-later
"""H-A clobber guard for the ``.chiplet`` waist.

Background (the real mechanism, see CHIPLET_FLOW_ROADMAP.md "Interop hardening"):
``write_chiplet`` regenerates the ``.chiplet`` from board state only. It never
emits the human/Studio-authored top-level blocks (``flow:``, ``netlist:``), and
it cannot recover a hand-edited position. The orchestrator then stages that
freshly regenerated file over the canonical ``.chiplet`` with a wholesale
``shutil.copy2`` (orchestrator.py, the ``emit_chiplet`` branch). So a pasted
``flow:`` block or a hand-edited position is destroyed on every KiCad re-export,
silently. The finalizer (``hyp_to_gds --update-chiplet-file``) is load-modify-
dump and round-trips foreign top-level keys, so it is NOT the destroying door;
the ``copy2`` is.

This module guards that door with two mechanisms, both routed through the shared
reference loader ``chiplet_format_io`` (the confirmed single guarded read/write
path), never through a new raw ``yaml.safe_load`` (which the Lane 2 raw-load lint
forbids):

1. ``carry_over_foreign_blocks`` -- shallow top-level merge that copies the
   exporter-*unowned* keys (``flow:``, ``netlist:``, any future hand-authored
   block) from the existing canonical file into the freshly staged intermediate
   BEFORE the copy. Because the finalizer round-trips them, ``flow:`` stays
   EMBEDDED, which is exactly what Chiplet Studio's FlowEngine requires (it reads
   ``flow:`` only from the embedded block).

2. An exporter-content digest tripwire. After a successful export the digest of
   the finalized file's exporter-*owned* content is recorded in a sidecar. On the
   next export, if that content no longer matches (a human hand-edited an
   exporter-owned field, e.g. a position, outside KiCad), the caller aborts the
   re-export unless forced. Foreign-only edits (pasting ``flow:``) do NOT change
   this digest, so the common hosting workflow never trips it.

Pure format logic: stdlib + the vendored ``chiplet_format_io`` (PyYAML only). It
does not import ``pcbnew``, so it is unit-testable on a host Python without KiCad.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List

try:  # normal package import (pytest / KiCad both reach it this way)
    from ..vendor import chiplet_format_io as cfio
except ImportError:  # pragma: no cover - fallback for odd load contexts
    import os
    import sys
    sys.path.insert(
        0, os.path.join(os.path.dirname(__file__), os.pardir, "vendor"))
    import chiplet_format_io as cfio  # type: ignore


#: Top-level keys the exporter pipeline owns and regenerates on every run
#: (writer + finalizer). Everything else at the top level is human/Studio
#: authored and must be preserved across a re-export. Additive maintenance
#: point: when the exporter grows a new owned top-level key, add it here, or the
#: guard would treat a run that legitimately omits it as a foreign block and
#: carry a stale copy over.
EXPORTER_OWNED_TOP_LEVEL_KEYS = frozenset({
    "format_version",
    "_metadata",
    "assembly",
    "interposer",
    "technologies",
    "components",
    "stackup",
})

#: Sidecar suffix for the exporter-content digest tripwire.
DIGEST_SIDECAR_SUFFIX = ".exportcontent.sha256"


def _load_permissive(path: str) -> Dict[str, Any]:
    """Parse a ``.chiplet`` through the shared loader, permissively.

    Validation is off and intermediates are allowed: the guard only needs the
    top-level key set, and it must never abort an export just because the
    on-disk file is an intermediate or fails a strict check. Routing through
    ``cfio`` (not a bare ``yaml.safe_load``) keeps this a delegating consumer.
    """
    data = cfio.load(path, allow_intermediate=True, validate=False)
    if not isinstance(data, dict):
        raise cfio.ChipletFormatError(
            "top-level .chiplet document must be a mapping")
    return data


def foreign_top_level_keys(data: Dict[str, Any]) -> List[str]:
    """Top-level keys of ``data`` the exporter does not own (insertion order)."""
    return [k for k in data if k not in EXPORTER_OWNED_TOP_LEVEL_KEYS]


def carry_over_foreign_blocks(existing_final: str, staged_intermediate: str) -> List[str]:
    """Merge exporter-unowned top-level blocks into the staged intermediate.

    Reads the existing canonical file (if any) and the freshly staged
    intermediate, copies every exporter-unowned top-level key that the
    intermediate does not already carry from the former into the latter, and
    rewrites the intermediate in place through the shared writer. Returns the
    list of keys carried over (empty when there is nothing to preserve, e.g. a
    first export or a board with no ``flow:``/``netlist:``).

    Idempotent and non-destructive: exporter-owned content in the intermediate
    is left untouched, so board state stays the source of truth for it.
    """
    import os
    if not existing_final or not os.path.exists(existing_final):
        return []

    existing = _load_permissive(existing_final)
    foreign = foreign_top_level_keys(existing)
    if not foreign:
        return []

    staged = _load_permissive(staged_intermediate)
    carried: List[str] = []
    for key in foreign:
        if key in staged:
            # The exporter already produced this key this run: its output wins.
            continue
        staged[key] = existing[key]
        carried.append(key)

    if carried:
        # validate=False: the intermediate carries _metadata.finalize_required
        # and may predate the tolerant reader; the guard must not gate on it.
        cfio.dump(staged, staged_intermediate, validate=False)
    return carried


def _exporter_owned_subset(data: Dict[str, Any]) -> Dict[str, Any]:
    """Exporter-owned top-level keys of ``data``, sorted for a stable digest."""
    return {k: data[k] for k in sorted(data) if k in EXPORTER_OWNED_TOP_LEVEL_KEYS}


def exporter_content_digest(path: str) -> str:
    """SHA-256 of the file's exporter-owned content, canonicalized.

    Independent of on-disk formatting and key order (the subset is re-serialized
    with sorted keys), so it changes only when the *exporter-owned* content
    changes, never when a foreign block such as ``flow:`` is added or edited.
    """
    data = _load_permissive(path)
    canonical = cfio.dumps(_exporter_owned_subset(data), validate=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def digest_sidecar_path(final_path: str) -> str:
    """Path of the exporter-content digest sidecar for a canonical file."""
    return final_path + DIGEST_SIDECAR_SUFFIX


def record_exporter_content_digest(final_path: str) -> str:
    """Record the finalized file's exporter-content digest; return it.

    Called only after a successful finalize. Best-effort semantics belong to the
    caller: a sidecar-write failure must never fail an otherwise good export.
    """
    digest = exporter_content_digest(final_path)
    with open(digest_sidecar_path(final_path), "w", encoding="utf-8") as fh:
        fh.write(digest + "\n")
    return digest


def foreign_hand_edit_detected(final_path: str) -> bool:
    """True when the canonical file's exporter-owned content diverged.

    Compares the current exporter-content digest against the sidecar recorded
    after the last successful export. False (no tripwire) when either the file or
    the sidecar is absent (a first export, or a checkout predating the guard):
    the guard adds friction only when there is a recorded baseline to violate.
    """
    import os
    sidecar = digest_sidecar_path(final_path)
    if not os.path.exists(final_path) or not os.path.exists(sidecar):
        return False
    try:
        with open(sidecar, "r", encoding="utf-8") as fh:
            recorded = fh.read().strip()
    except OSError:
        return False
    if not recorded:
        return False
    return exporter_content_digest(final_path) != recorded
