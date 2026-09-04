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

What this grammar can and cannot know, since both mechanisms rest on it:

The splitter decides block identity with one predicate on one line -- is this
column-0 line a bare ``key:``. That predicate is INCOMPLETE (YAML has top-level
keys it does not match: quoted, ``flow :``, explicit, flow style) and it is not
SOUND either (a line at column 0 can sit inside a multi-line flow scalar opened
earlier, where YAML sees no key at all). Each gap is a real defect: the first
attaches a foreign-looking block to the owned block above it, which is how an
owned key rides into a document unseen by the ownership filter AND by the
digest, since it travels inside foreign bytes; the second invents a block that
is not in the file, hands its bytes -- carved out of somebody's assembly name --
to a foreign host, and drops the real block behind it.

So the boundary is made explicit instead of implicit, and it is split across the
two tiers by what each one can actually prove:

* Here, on text alone: ``unmodelled_top_level_line`` names the five column-0
  shapes this grammar models, default-deny. When it passes, every top-level key
  a YAML reader can see is a key line here -- the incompleteness gap is closed.
  When it does not, ``split_for_rewrite`` REFUSES; the caller aborts the export
  and says which line, because silently carrying nothing is the data loss this
  module exists to prevent.
* One tier down, where PyYAML exists (``hyp_to_gds``): the key list this module
  produced is compared against the parser's. That closes the unsoundness gap,
  which text alone cannot, and it discriminates the exact proposition in the one
  place both parsers are available.

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
#:
#: "Owned" does not mean one writer produces the whole block. ``interconnect:``
#: has two, with different reach: the writer emits only ``adapter``, from the
#: board text variable, while the ``technology`` subblock is derived from the
#: interconnect PDK manifest and added later by the finalizer. A block can
#: therefore be legitimately half-written at the moment this guard runs and
#: still be fully owned. Judge ownership by what the pipeline REGENERATES over
#: a whole run, not by what any single writer emits in one step.
EXPORTER_OWNED_TOP_LEVEL_KEYS = frozenset({
    "format_version",
    "_metadata",
    "assembly",
    "interposer",
    "technologies",
    "components",
    "stackup",
    # Added late, and the comment above had already predicted the defect: the
    # exporter grew this key (writer emits interconnect.adapter, finalizer
    # regenerates the whole block including the derived technology subblock)
    # and nobody added it here. So it counted as foreign and was carried over
    # verbatim from the existing document, with two consequences.
    #
    # Clearing the INTERCONNECT_ADAPTER text variable could never remove the
    # block, because a carried key is one the staged run legitimately omits.
    #
    # And the adapter is not inert data: it reaches run_drc as
    # --interconnect-adapter, which resolves it to a .drc that the assembly
    # deck reads into the source it evaluates. So a .chiplet arriving with a
    # third-party project chose code that ran. `interposer` was never
    # reachable that way for exactly one reason: it was already in this set.
    "interconnect",
})

#: Sidecar suffix for the exporter-content digest tripwire.
DIGEST_SIDECAR_SUFFIX = ".exportcontent.sha256"

#: Bucket key for lines that precede the first top-level key (a leading comment
#: or blank line). Neither owned nor foreign; never carried, never hashed.
_PREAMBLE_KEY = ""

#: A top-level key line, matched against LINE CONTENT (the text up to the next
#: LF, with one optional trailing CR already removed) -- never against the raw
#: slice. ``\Z``, not ``$``: ``$`` also matches just before a trailing newline,
#: so with ``$`` a two-line string passes a predicate that claims to be about
#: one line. ``[^\n]`` rather than ``.`` for the same reason, and because it is
#: what the C++ reference has to write.
_KEY_LINE_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*):(?:\s[^\n]*)?\Z")

#: A key line spelled with whitespace before the colon: a key to YAML, nothing
#: at all to this grammar. Used only to give the refusal a specific reason.
_SPACED_KEY_LINE_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s+:(?:\s[^\n]*)?\Z")


class TopLevelGrammarRefusal(ValueError):
    """This grammar cannot say which top-level key owns some line of the text.

    Raised instead of returning a split the caller would then act on. Carries
    the 1-based ``lineno``, the offending ``line`` content and a ``kind``, so a
    caller can name the line to the user instead of saying "malformed file".
    """

    def __init__(self, reason, lineno, line, kind):
        self.reason = reason
        self.lineno = lineno
        self.line = line
        self.kind = kind
        ValueError.__init__(
            self, "line %d: %s\n    %s" % (lineno, reason, line))


# ---------------------------------------------------------------------------
# The line grammar. One definition, shared by every reader of the text.
# ---------------------------------------------------------------------------

def iter_lines(text):
    """Yield ``(raw, content)`` per line, cutting on LF and on nothing else.

    ``raw`` is the source slice including its terminator, so joining every
    ``raw`` reproduces ``text`` byte for byte. ``content`` is ``raw`` without
    the terminating LF and without one optional CR immediately before it; the
    key-line predicate is matched against ``content``.

    Why not ``str.splitlines()``: it also breaks on CR alone, VT, FF, FS, GS,
    RS, U+0085 NEL, U+2028 LINE SEPARATOR and U+2029 PARAGRAPH SEPARATOR. A YAML
    line ends at LF, with one optional preceding CR, and nowhere else, so every
    one of those characters is ordinary scalar content. Breaking on one invents
    a line, and therefore a top-level key, that no reader of the document sees:
    the block that key "owns" is cut out of somebody's ``assembly.name`` and
    handed to a foreign host, and the real block behind it is lost.

    This only holds if the caller read the file WITHOUT newline translation
    (``newline=""``). Python's text mode turns a lone CR into LF before this
    function ever sees it, which puts the CR half of the defect back.
    """
    start = 0
    end = len(text)
    while start < end:
        nl = text.find("\n", start)
        if nl < 0:
            raw = text[start:]
            yield raw, raw
            return
        raw = text[start:nl + 1]
        content = raw[:-1]
        if content.endswith("\r"):
            content = content[:-1]
        yield raw, content
        start = nl + 1


def read_document(path):
    """Read a ``.chiplet`` as text with NO newline translation.

    ``open(path, "r")`` is universal-newlines: it rewrites CRLF and lone CR to
    LF. Two consequences this module cannot live with. A lone CR inside a scalar
    would become a real line break and grow a phantom top-level key (the CR half
    of the line-grammar defect, reintroduced by the reader). And a CRLF document
    would be split into slices that no longer match the file, so
    ``carry_over_foreign_blocks`` would silently rewrite a foreign block's line
    endings while promising to copy it verbatim.
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_document(path, text):
    """Write a ``.chiplet`` with NO newline translation (see ``read_document``).

    Without ``newline=""`` Python turns every LF into ``os.linesep`` on write,
    so on Windows -- a first-class KiCad platform -- the guard would convert the
    whole document to CRLF as a side effect of preserving a ``flow:`` block.
    """
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def top_level_key_line(content):
    """The top-level key this LINE CONTENT opens, or None."""
    match = _KEY_LINE_RE.match(content)
    return match.group(1) if match else None


def top_level_key_lines(text):
    """Every top-level key in document order, REPEATS INCLUDED.

    The list the worker tier compares against PyYAML's keys. A list and not a
    set because ``split_top_level_blocks`` cannot express a repeated key, so a
    set comparison would agree with PyYAML on the one document no two readers
    agree on. It shares ``iter_lines`` and ``top_level_key_line`` with the
    splitter deliberately: the proposition under test is about the key list the
    ownership filter actually used, and a second implementation that happens to
    agree with PyYAML says nothing about the one that made the decision.
    """
    keys = []
    for _raw, content in iter_lines(text):
        key = top_level_key_line(content)
        if key is not None:
            keys.append(key)
    return keys


# ---------------------------------------------------------------------------
# The competence boundary, stated positively and tested as a whole.
# ---------------------------------------------------------------------------

def _unmodelled_reason(content, seen_key):
    """None if this column-0 non-key line is modelled; else why it is not."""
    if content == "":
        return None                                    # blank line
    head = content[0]
    if head in " \t":
        return None                                    # indented block content
    if head == "#":
        return None                                    # comment at column 0
    if content == "---" or content.startswith("--- "):
        if not seen_key:
            return None                                # leading marker: preamble
        return ("a document marker here starts a SECOND YAML document, whose "
                "top-level keys this grammar cannot see at all")
    if head == "-" and (len(content) == 1 or content[1] in " \t"):
        return None                                    # top-level sequence item
    if head in "\"'":
        return ("a quoted key at column 0 is a key to YAML but not to this "
                "grammar, so its block would be attributed to the block above "
                "it, whose owner regenerates it away on the next export")
    if head in "{[":
        return ("flow style at column 0: YAML reads a mapping here and this "
                "grammar reads one line that owns nothing, so the block's "
                "bytes are never captured and the re-export drops them")
    if _SPACED_KEY_LINE_RE.match(content):
        return ("YAML reads a key here but this grammar does not, because the "
                "colon does not follow the key directly, so the block's bytes "
                "are never captured and the re-export drops them")
    return ("this line is at column 0 but is not a top-level key line, so the "
            "grammar cannot tell which top-level key owns it")


def unmodelled_top_level_line(text):
    """First column-0 line the grammar does not model: ``(lineno, line, why)``.

    None when every line is either a top-level key line at column 0 or one of
    the five modelled non-key shapes: blank, indented, ``#`` comment at column
    0, ``- `` sequence item at column 0 (``yaml.dump`` does not indent a
    sequence under its key, so these are ordinary), and a leading ``---``
    before the first key.

    That list is this module's whole competence claim and it is default-deny: a
    column-0 shape not on it is refused, never assumed harmless. What the claim
    buys is one-directional, and worth stating exactly. When this returns None,
    every top-level key a YAML reader can see IS a key line here. The converse
    -- that every key line here is a key to the reader -- is NOT claimed, and
    cannot be established from text alone: a column-0 line can sit inside a
    multi-line flow scalar or flow collection opened on an earlier line, and
    deciding that needs a parser. That remaining direction is closed one tier
    down, by ``top_level_key_lines`` against PyYAML.
    """
    seen_key = False
    lineno = 0
    for _raw, content in iter_lines(text):
        lineno += 1
        if top_level_key_line(content) is not None:
            seen_key = True
            continue
        why = _unmodelled_reason(content, seen_key)
        if why is not None:
            return lineno, content, why
    return None


def split_top_level_blocks(text):
    """Split a ``.chiplet`` into top-level blocks, insertion-ordered, or REFUSE.

    A line is a top-level key line iff its CONTENT matches ``_KEY_LINE_RE`` at
    column 0; ``group(1)`` is the key. That line plus every following line up to
    the next key line is the block, kept VERBATIM (key line, indented content,
    comments, blank lines, line endings). Lines before the first key line are
    the preamble, keyed ``""``. Joining the slices in document order reproduces
    the input byte for byte. No YAML is parsed.

    Two documents this used to answer, wrongly, are now refused with
    ``TopLevelGrammarRefusal``:

    * a QUOTED key at column 0. It is a key to YAML and not to this grammar, so
      the split attached its block to the preceding key, whose owner
      regenerates it away -- which is also how an exporter-owned key rides into
      a document inside a foreign block, unseen by the ownership filter and by
      the digest, because it travels in foreign content. There is no right
      answer to give here, so none is given.
    * a REPEATED top-level key. Concatenating the two runs decides who owns the
      text but not which value wins, and no reading is conforming: PyYAML takes
      the last value and yaml-cpp the first.

    It does NOT refuse a document it merely cannot DELIMIT (a flow-style
    document, ``flow :``). Those split correctly; they just yield no slice for
    the node. Whether that is safe depends on what the caller does next, so it
    is the caller's verdict and not the grammar's: ``split_for_rewrite``.
    """
    blocks = collections.OrderedDict()
    current = _PREAMBLE_KEY
    blocks[_PREAMBLE_KEY] = ""
    opened_at = {}
    lineno = 0
    for raw, content in iter_lines(text):
        lineno += 1
        key = top_level_key_line(content)
        if key is not None:
            if key in opened_at:
                raise TopLevelGrammarRefusal(
                    "the top-level key %r is named twice (already opened at "
                    "line %d). PyYAML resolves a repeated key to the LAST "
                    "value and yaml-cpp to the FIRST, so two conforming "
                    "readers build different documents from these bytes"
                    % (key, opened_at[key]),
                    lineno, content, "repeated_top_level_key")
            opened_at[key] = lineno
            current = key
            blocks[current] = raw
            continue
        if content[:1] in ("\"", "'"):
            raise TopLevelGrammarRefusal(
                _unmodelled_reason(content, True), lineno, content,
                "quoted_key_at_column_zero")
        blocks[current] = blocks.get(current, "") + raw
    if not blocks[_PREAMBLE_KEY]:
        del blocks[_PREAMBLE_KEY]
    return blocks


#: Characters PyYAML treats as a LINE BREAK but this LF-only grammar treats as
#: ordinary content: a lone CR (a CR that is not part of CRLF), NEL, and the
#: Unicode line and paragraph separators.
#:
#: This is the residual PLUG-9 left behind, and it inverts the original defect
#: rather than repeating it. Cutting only on LF is right for the grammar, and
#: the oracle requires the SPLITTER to succeed on a U+2028 document. But it
#: means PyYAML can see a line break where the grammar sees none, so a second
#: ``components:`` written after a plain scalar, separated by one of these,
#: lands inside a carried foreign block and wins by last-wins in the consumer.
#: Measured: all four smuggled an owned block past both the layout refusal and
#: the worker key-set check.
#:
#: It belongs to the WRITE verdict only, never to ``split_top_level_blocks``:
#: reading such a document is fine and the oracle pins that the split succeeds.
#: What is refused is writing it back, because the grammar cannot agree with
#: the consumer about where its lines are.
_BREAK_CHAR_RE = re.compile("\r(?!\n)|[\x85  ]")


def break_character_line(text):
    """``(lineno, content, why)`` for the first line-break disagreement, else None.

    Line numbers count LF-terminated lines, so they match what the rest of the
    verdict reports and what a user's editor shows for the same file.
    """
    # Searched over the WHOLE text, never per line. Splitting first would eat
    # the LF that the ``\r(?!\n)`` lookahead needs, so every CRLF document
    # would report a lone carriage return and be refused. That is not a
    # hypothetical: it is the first thing this function got wrong.
    match = _BREAK_CHAR_RE.search(text)
    if match is None:
        return None
    found = match.group(0)
    lineno = text.count("\n", 0, match.start()) + 1
    line = text.split("\n")[lineno - 1]
    names = {"\r": "a carriage return not followed by a newline",
             "\x85": "a NEL (U+0085)",
             "\u2028": "a Unicode line separator (U+2028)",
             "\u2029": "a Unicode paragraph separator (U+2029)"}
    shown = found.encode("unicode_escape").decode("ascii")
    return (lineno,
            line.replace(found, "<%s>" % shown),
            "the line contains %s, which YAML reads as a line break and this "
            "grammar reads as text, so the two disagree about where this "
            "document's lines are" % names[found])


def split_for_rewrite(text):
    """``split_top_level_blocks`` for a caller about to REWRITE the document.

    The same split over a narrower domain. It refuses the whole unmodelled-shape
    set, not only the two shapes the grammar itself cannot answer, because this
    caller is about to destroy the existing bytes: a block whose delimitation
    the grammar got wrong, or whose bytes it never captured, is content that
    disappears with no trace and no message. A reader may go on reading such a
    document -- flow rule 1 says it must -- but a rewriter may not write it back.
    """
    bad = break_character_line(text)
    if bad is not None:
        lineno, content, why = bad
        raise TopLevelGrammarRefusal(why, lineno, content, "break_character")
    bad = unmodelled_top_level_line(text)
    if bad is not None:
        lineno, content, why = bad
        raise TopLevelGrammarRefusal(why, lineno, content, "unmodelled_line")
    return split_top_level_blocks(text)


def unwritable_reason(path):
    """User-facing reason this ``.chiplet`` cannot be safely re-exported, or None.

    The whole GUI-tier verdict in one call, so the orchestrator branch stays
    three lines and the message is testable without KiCad. None means: every
    byte of the document is attributable to a top-level key this grammar
    models, so regenerating the owned blocks and carrying the foreign ones over
    cannot lose content the guard could not see.

    Answers the LAYOUT question only. A file that cannot be read or decoded is
    somebody else's verdict (the digest tripwire, which fails closed and which
    ``force`` deliberately bypasses), and answering it here would silently take
    that override away.
    """
    import os
    if not path or not os.path.exists(path):
        return None                                    # first export
    try:
        text = read_document(path)
    except (OSError, UnicodeDecodeError):
        return None                                    # not this check's question
    try:
        split_for_rewrite(text)
    except TopLevelGrammarRefusal as exc:
        return ("The existing .chiplet has a top-level line this exporter "
                "cannot attribute to a block, so re-exporting it could destroy "
                "content the guard cannot see. Refusing; the file on disk is "
                "unchanged.\n"
                "  File: %s\n"
                "  Line %d: %s\n"
                "  Why: %s\n"
                "  Fix: write every top-level key at column 0 as an unquoted "
                "`key:`, once each, then re-export. Or delete the file to "
                "regenerate it from the board, losing whatever it holds.\n"
                "  force=True does not bypass this: it overrides the hand-edit "
                "tripwire, not a document this exporter cannot read."
                % (path, exc.lineno, exc.line, exc.reason))
    return None


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

    existing_text = read_document(existing_final)
    existing_blocks = split_for_rewrite(existing_text)
    foreign = foreign_top_level_keys(existing_blocks)
    if not foreign:
        return []

    staged_text = read_document(staged_intermediate)
    staged_blocks = split_for_rewrite(staged_text)

    carried = [key for key in foreign if key not in staged_blocks]
    if not carried:
        return []

    # Each block is appended verbatim; trailing newlines are trimmed only so the
    # blocks join with exactly one blank line between them.
    pieces = [existing_blocks[key].rstrip("\n") for key in carried]
    staged_text = staged_text.rstrip("\n") + "\n"      # single trailing newline
    staged_text += "\n" + "\n\n".join(pieces) + "\n"   # blank line before/between
    write_document(staged_intermediate, staged_text)
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
        lines = [content.rstrip()
                 for _raw, content in iter_lines(blocks[key])]
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
    canonical = _owned_canonical(split_for_rewrite(read_document(path)))
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
