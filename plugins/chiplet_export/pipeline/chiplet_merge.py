# SPDX-License-Identifier: GPL-3.0-or-later
"""H-A clobber guard for the ``.chiplet`` waist -- pure stdlib, GUI-tier safe.

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

Why this module is stdlib-only (no PyYAML, no ``chiplet_format_io``):
``orchestrator.run_export`` -- and therefore this guard -- runs in KiCad's
bundled Python, which by contract has NO PyYAML and NO klayout (discovery.py
lines 4-7). The guard only needs top-level *block identity* -- which key owns
which run of lines -- to carry a foreign block over verbatim and to hash the
exporter-owned content. That is a pure-text operation, so it must not import
``yaml`` or the vendored ``chiplet_format_io`` (which imports ``yaml``
unconditionally); doing so crashes the very first export in the real GUI process
at import time. ``chiplet_format_io`` remains the reference parser only where
parsing actually happens -- the worker interpreter and ``hyp_to_gds.py`` -- both
of which have PyYAML. The GUI tier parses no YAML at all.

This module guards the ``copy2`` door with two mechanisms, both operating on raw
top-level block TEXT:

1. ``carry_over_foreign_blocks`` -- a shallow top-level merge that copies the
   exporter-*unowned* top-level blocks (``flow:``, ``netlist:``, any future
   block this exporter does not own) VERBATIM from the existing canonical file
   into the freshly staged intermediate BEFORE the copy. Because the finalizer
   round-trips them, ``flow:`` stays EMBEDDED, which is exactly what Chiplet
   Studio's FlowEngine requires (it reads ``flow:`` only from the embedded
   block).

   Note on provenance, since it is easy to read this the wrong way: these
   blocks are whatever the existing file contained, not necessarily anything
   the current user wrote. A ``.chiplet`` that arrived with a downloaded
   project brings its author's blocks, and this merge re-emits them into a
   document the user's own tool just produced. That is the intended contract,
   preserving unowned content is the whole point, but it means a freshly
   generated file is NOT evidence that its ``flow:`` block was locally
   authored. Chiplet Studio executes ``flow:``, and its execution policy is
   what has to close that; do not "fix" it here by dropping blocks.

2. An exporter-content digest tripwire. After a successful export the digest of
   the finalized file's exporter-*owned* content is recorded in a sidecar. On the
   next export, if that content no longer matches (a human hand-edited an
   exporter-owned field, e.g. a position, outside KiCad), the caller aborts the
   re-export unless forced. Editing/adding a foreign block does NOT change this
   digest, so the common hosting workflow never trips it. The digest sidecar has
   no external consumer -- ``record`` and ``check`` only need to agree with each
   other -- so a raw-text hash is sufficient and the finalizer's own round-trip
   does not false-trip it (record and check run on the same on-disk bytes).

Two accepted semantic deltas versus the previous YAML-parsing guard:
  * A *formatting-only* edit inside an exporter-OWNED block now trips the wire.
    Acceptable: it is an edit made to the canonical file outside KiCad, and
    ``force=True`` bypasses the wire.
  * A carried foreign block keeps the human's comments and formatting verbatim
    until the finalizer's ``yaml`` round-trip normalizes it (the finalizer runs
    in the worker tier, which has PyYAML).

Stdlib only; it does not import ``pcbnew``, so it is unit-testable on a host
Python without KiCad and it runs unchanged in the GUI tier without PyYAML.
"""

from __future__ import annotations

import collections
import hashlib
import re
from typing import List


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

#: Bucket key for lines that precede the first top-level key (a leading comment
#: or blank line). Neither owned nor foreign; never carried, never hashed.
_PREAMBLE_KEY = ""

#: A top-level key line: an unindented ``key:`` at column 0, optionally followed
#: by whitespace and a value. This is exactly the block-style layout the
#: finalizer (yaml.dump default) and writers/chiplet_writer emit -- top-level
#: keys at column 0, nested content indented. A ``#`` comment or an indented
#: line is not a key line and stays inside the current block.
_KEY_LINE_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*):(?:\s.*)?$")


def split_top_level_blocks(text: str) -> "collections.OrderedDict[str, str]":
    """Split a ``.chiplet`` document into top-level blocks, insertion-ordered.

    A line is a top-level key line iff it matches ``_KEY_LINE_RE`` at column 0
    (no leading whitespace); ``group(1)`` is the key. That key line PLUS every
    following line until the next column-0 key line forms the block, kept
    VERBATIM (the key line, indented content, comments, blank lines, trailing
    newlines all included). Lines before the first key go into the preamble
    bucket under key ``""`` (neither owned nor foreign). A duplicate top-level
    key concatenates its blocks. No YAML is parsed.
    """
    blocks: "collections.OrderedDict[str, str]" = collections.OrderedDict()
    current = _PREAMBLE_KEY
    blocks[_PREAMBLE_KEY] = ""
    for line in text.splitlines(keepends=True):
        match = _KEY_LINE_RE.match(line)
        if match:
            current = match.group(1)
            # A duplicate top-level key concatenates onto the first occurrence
            # (which keeps its original insertion position).
            blocks[current] = blocks.get(current, "") + line
        else:
            blocks[current] = blocks.get(current, "") + line
    if not blocks[_PREAMBLE_KEY]:
        del blocks[_PREAMBLE_KEY]
    return blocks


def foreign_top_level_keys(blocks: "collections.OrderedDict[str, str]") -> List[str]:
    """Top-level keys the exporter does not own, in insertion order.

    Operates on the split-block dict. Excludes the exporter-owned keys and the
    ``""`` preamble bucket.
    """
    return [k for k in blocks
            if k != _PREAMBLE_KEY and k not in EXPORTER_OWNED_TOP_LEVEL_KEYS]


def carry_over_foreign_blocks(existing_final: str, staged_intermediate: str) -> List[str]:
    """Merge exporter-unowned top-level blocks into the staged intermediate.

    Reads the existing canonical file (if any) and the freshly staged
    intermediate as UTF-8 text, splits both, and for every exporter-unowned
    top-level key present in the existing file but absent from the staged file
    APPENDS its block VERBATIM (existing-file order) to the staged text. Blocks
    are separated by exactly one blank line, and the staged text is normalized to
    a single trailing newline. Returns the list of keys carried over (empty when
    there is nothing to preserve, e.g. a first export or a board with no
    ``flow:``/``netlist:``).

    Non-destructive: exporter-owned content in the staged file is never touched,
    so board state stays the source of truth for it. A missing/empty existing
    file returns ``[]``. The orchestrator wraps this call and degrades to the
    pre-guard behaviour on any exception, so a malformed file never crashes an
    export.
    """
    import os
    if not existing_final or not os.path.exists(existing_final):
        return []

    with open(existing_final, "r", encoding="utf-8") as fh:
        existing_text = fh.read()
    existing_blocks = split_top_level_blocks(existing_text)
    foreign = foreign_top_level_keys(existing_blocks)
    if not foreign:
        return []

    with open(staged_intermediate, "r", encoding="utf-8") as fh:
        staged_text = fh.read()
    staged_blocks = split_top_level_blocks(staged_text)

    carried = [key for key in foreign if key not in staged_blocks]
    if not carried:
        return []

    # Each block is appended verbatim; trailing newlines are trimmed only so the
    # blocks join with exactly one blank line between them.
    pieces = [existing_blocks[key].rstrip("\n") for key in carried]
    staged_text = staged_text.rstrip("\n") + "\n"      # single trailing newline
    staged_text += "\n" + "\n\n".join(pieces) + "\n"   # blank line before/between
    with open(staged_intermediate, "w", encoding="utf-8") as fh:
        fh.write(staged_text)
    return carried


def _owned_canonical(blocks: "collections.OrderedDict[str, str]") -> str:
    """Deterministic canonical text of the exporter-owned blocks.

    Owned blocks sorted by key; within each block every line is right-stripped
    and trailing blank lines are dropped, so pure end-of-line/EOF whitespace
    churn never changes the result. Formatting *inside* an owned block is
    otherwise preserved, so a formatting-only owned edit does change it (an
    accepted delta -- it is an out-of-KiCad edit and ``force`` bypasses).
    """
    parts = []
    for key in sorted(k for k in blocks if k in EXPORTER_OWNED_TOP_LEVEL_KEYS):
        lines = [ln.rstrip() for ln in blocks[key].splitlines()]
        # Drop trailing blank AND full-line comment lines. A column-0 ``#``
        # comment pasted just above a foreign block (e.g. above ``flow:``)
        # attaches to the preceding owned block as its trailing line(s); dropping
        # it keeps the promise that adding a foreign block never trips the wire.
        # Comments carry no exporter-owned semantics (the finalizer drops them),
        # and neither producer emits a literal block scalar where a trailing
        # ``#`` line would be significant.
        while lines and (lines[-1] == "" or lines[-1].lstrip().startswith("#")):
            lines.pop()
        parts.append("\n".join(lines))
    return "\n".join(parts)


def exporter_content_digest(path: str) -> str:
    """SHA-256 of the file's exporter-owned content (see ``_owned_canonical``).

    Stable across identical on-disk bytes and independent of trailing whitespace
    only. It changes when exporter-owned content changes and does NOT change when
    a foreign block such as ``flow:`` is added or edited.
    """
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    canonical = _owned_canonical(split_top_level_blocks(text))
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
    the guard adds friction only when there is a recorded baseline to violate. A
    corrupt/undecodable canonical file makes ``exporter_content_digest`` raise;
    that propagates so the caller can fail closed (orchestrator.py).
    """
    import os
    sidecar = digest_sidecar_path(final_path)
    if not os.path.exists(final_path) or not os.path.exists(sidecar):
        return False
    try:
        with open(sidecar, "r", encoding="utf-8") as fh:
            recorded = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        # The sidecar is our own best-effort artifact; an unreadable or corrupt
        # one means we lost the baseline, so behave like "no baseline" (no trip)
        # rather than fail closed on a file the user did not author.
        return False
    if not recorded:
        return False
    return exporter_content_digest(final_path) != recorded
