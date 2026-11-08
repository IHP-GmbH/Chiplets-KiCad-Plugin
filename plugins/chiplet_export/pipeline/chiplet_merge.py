# SPDX-License-Identifier: GPL-3.0-or-later
"""Clobber guard for the ``.chiplet`` waist -- pure stdlib, GUI-tier safe.

Background:
``write_chiplet`` regenerates the ``.chiplet`` from board state only. It never
emits the human/Studio-authored top-level blocks (``flow:``, ``netlist:``), and
it cannot recover a hand-edited position. The orchestrator stages that freshly
regenerated file next to the canonical ``.chiplet`` and, once the worker has
finalized it, replaces the canonical file with it wholesale (orchestrator.py,
the ``emit_chiplet`` branch). So a pasted ``flow:`` block or a hand-edited
position would be destroyed on every KiCad re-export, silently. The finalizer
(``hyp_to_gds --update-chiplet-file``) is load-modify-dump and round-trips
foreign top-level keys, so it is NOT the destroying door; the replace is.

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

This module guards that door with two mechanisms, both operating on raw
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

def _is_blank(content):
    """Blank content is SPACE and TAB only, and nothing else.

    The format's own definition, not Python's: ``str.isspace`` would also call
    NBSP, the Unicode separators and the C0 controls blank, and those are
    exactly the characters the line-break and column-zero rules are about. A
    line of spaces and tabs owns nothing and needs to own nothing.
    """
    return content.strip(" \t") == ""


#: The five shapes a column-0 line may have without being a top-level key line
#: and still be attributable: a blank line, a ``#`` comment, a ``%`` directive,
#: a document marker (``---`` or ``...``) and a block sequence entry (``-``
#: followed by a space, a tab, or end of content).
def _document_marker(content):
    """True for ``---`` and ``...``, alone or with content after a space."""
    for marker in ("---", "..."):
        if content == marker or content.startswith(marker + " "):
            return True
    return False


def _unattributable_reason(content):
    """The GRAMMAR's own rule, and only it: can this line be attributed?

    Separate from ``_unmodelled_reason`` below, which is the REWRITE policy and
    is stricter on purpose. Collapsing the two was a real defect: the splitter
    began refusing three documents the reference splitter accepts (a TAB blank
    inside a quoted scalar, a ``%YAML`` directive before ``---``, a terminal
    ``...``), because it had started applying restrictions that belong to the
    caller that is about to overwrite the file, not to the grammar.

    Nothing here asks whether the document LOADS, and that is not an oversight.
    Measured against the vendored reference on this corpus: it attributes a
    marker or a directive in three documents PyYAML refuses outright
    (``---`` after a key, ``...`` before one, ``%TAG`` after one). Splitting is
    not reading. A document that no reader accepts still has an answer to
    "which top-level key owns this line", and refusing to give it here would
    make the splitter disagree with the reference over bytes neither of them
    is being asked to read.
    """
    if _is_blank(content):
        return None
    head = content[0]
    if head == " ":
        return None                                    # indented block content
    if head == "#":
        return None                                    # comment at column 0
    if head == "%":
        return None                                    # directive
    if _document_marker(content):
        return None
    if head == "-" and (len(content) == 1 or content[1] in " \t"):
        return None                                    # top-level sequence item
    if head == "\t":
        return ("a TAB-led CONTENT line is at column 0, not indented: no YAML "
                "parser reads a tab as block indentation, so this grammar "
                "cannot say which top-level key owns it")
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


def _unmodelled_reason(content, seen_key):
    """None if this column-0 non-key line is modelled; else why it is not.

    The REWRITE policy. Stricter than ``_unattributable_reason`` above, and it
    stays stricter: this is what a caller about to overwrite the file consults,
    so a second YAML document, a directive or a terminal marker is refused here
    even though the grammar can attribute all three. Splitting them is right;
    rewriting a file around them is not, because what comes back is not what a
    reader of the whole stream saw.
    """
    if _is_blank(content):
        return None                                    # blank line
    head = content[0]
    if head == " ":
        return None                                    # indented block content
    if head == "\t":
        # A TAB is not indentation. Column zero means "does not start with a
        # space", and YAML has no tab indentation in block context at all:
        # PyYAML 6.0.3 and yaml-cpp 0.8.0 both refuse a tab-indented mapping
        # outright. So the only documents where a tab-led line arises are
        # quoted scalars continued onto one, which both parsers DO read, and
        # this grammar cannot tell that continuation from a line owning
        # nothing. Treating it as indentation was reading the character as a
        # human would rather than as either parser does.
        return ("a TAB-led line is at column 0, not indented: no YAML parser "
                "reads a tab as block indentation, so this grammar cannot say "
                "which top-level key owns it")
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

    Documents this used to answer, wrongly, are refused with
    ``TopLevelGrammarRefusal``. The first kind is not a list of spellings but
    one property, ``unattributable_line_at_column_zero``: a line at column 0
    that is not a key line and not one of the five attributable non-key shapes
    (blank, ``#`` comment, ``%`` directive, ``---``/``...`` marker, ``- ``
    sequence entry). ``_unattributable_reason`` decides, and NOT the rewrite
    policy in ``_unmodelled_reason``: the two answer different questions and
    delegating to the stricter one made this refuse three documents the
    reference splitter accepts.

    * a QUOTED key at column 0. It is a key to YAML and not to this grammar, so
      the split attached its block to the preceding key, whose owner
      regenerates it away -- which is also how an exporter-owned key rides into
      a document inside a foreign block, unseen by the ownership filter and by
      the digest, because it travels in foreign content. There is no right
      answer to give here, so none is given.
    * a REPEATED top-level key. Concatenating the two runs decides who owns the
      text but not which value wins, and no reading is conforming: PyYAML takes
      the last value and yaml-cpp the first. Measured against the yaml-cpp the
      ecosystem actually links, 0.8.0, and the version is named because a
      reading taken off a vendored 0.6.3 copy has already been wrong once about
      a different question (0.8.0 breaks a line on a lone CR; 0.6.3 does not).
      That build also reports ``size() == 3`` for the two-key map above, so the
      duplicate is not merged away but carried, which is a second reason not to
      hand these bytes on.

    Undelimitable is a SEPARATE verdict from unsplittable, and the two used to
    be stated here as if the first implied the absence of the second. They are
    independent: a flow-style document and ``flow :`` yield no slice for the
    flow node AND are unsplittable, because each is a line at column 0 that no
    top-level key owns, so both are refused here. A document can perfectly well
    be undelimitable and still split; that is the caller's verdict to act on,
    in ``split_for_rewrite``, and it is not this function's business.
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
        why = _unattributable_reason(content)
        if why is not None:
            raise TopLevelGrammarRefusal(
                why, lineno, content, "unattributable_line_at_column_zero")
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

    Answers TWO questions, and used to answer only the second.

    This delegated readability to the digest tripwire, on the reasoning that a
    file we cannot read is "somebody else's verdict ... which fails closed and
    which force deliberately bypasses". That delegation was void exactly when it
    mattered. The tripwire returns early whenever the baseline is missing, empty
    or unreadable, so on those paths it never reaches the decode and never fails
    closed; nothing else asked, and the orchestrator went on to copy2 over an
    undecodable .chiplet with force NOT set. The bytes were the user's only copy
    and no other component held them.

    So readability is answered here, before any copy. Note this is not a new
    principle: the refusal below already sits OUTSIDE the force arm on the same
    reasoning, that force overrides the hand-edit tripwire, which is a judgement
    about content this exporter CAN read, and not "this exporter cannot read
    your file". A document that will not decode belongs on that same side.
    Neither a missing baseline nor force is permission to discard bytes nobody
    could read.
    """
    import os
    if not path or not os.path.exists(path):
        return None                                    # first export
    try:
        text = read_document(path)
    except (OSError, UnicodeDecodeError) as exc:
        return ("The existing .chiplet cannot be read, so re-exporting would "
                "overwrite content this exporter never saw. Refusing; the file "
                "on disk is unchanged.\n"
                "  File: %s\n"
                "  Why: %s\n"
                "  Fix: move the file aside to keep its bytes, then re-export "
                "to regenerate it from the board. If it should have been "
                "readable, restore it from backup or version control and "
                "re-export. A .chiplet is UTF-8 text; a decode error usually "
                "means a different encoding or a truncated/binary file.\n"
                "  force=True does not bypass this: force overrides the "
                "hand-edit tripwire, which judges content this exporter can "
                "read. It is not permission to discard a document nobody "
                "could read."
                % (path, exc))
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


def _digest_blocks(blocks) -> str:
    """Hash already parsed blocks so the assessment need not reread the file."""
    canonical = _owned_canonical(blocks)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def exporter_content_digest(path: str) -> str:
    """SHA-256 of the file's exporter-owned content (see ``_owned_canonical``).

    Stable across identical on-disk bytes and independent of trailing whitespace
    only. It changes when exporter-owned content changes and does NOT change when
    a foreign block such as ``flow:`` is added or edited.
    """
    return _digest_blocks(split_for_rewrite(read_document(path)))


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


#: The four states of one previous-export content guard evaluation.
#:
#: The guard used to answer with a bare bool, and False carried two unrelated
#: meanings: "compared against the recorded baseline and it matches" and "there
#: was no baseline to compare against". A caller reading False could not tell a
#: verified file from an unverified one, so the reassuring case and the blind
#: case printed the same nothing. These name them apart. The bool survives as a
#: wrapper over the same evaluation, so what changes is what can be SAID, not
#: what the export does.
GUARD_UNCHANGED = "unchanged"
GUARD_CHANGED = "changed"
GUARD_NO_BASELINE = "no_baseline"
GUARD_BASELINE_UNREADABLE = "baseline_unreadable"


class ContentGuardAssessment(object):
    """The result of ONE evaluation of the previous-export content guard.

    One evaluation, one object: every consumer (the refusal, the log line, the
    MANIFEST entry, the compatibility bool) reads THIS, and none of them reruns
    the comparison. Two evaluations of a file that a running export is about to
    rewrite are two different questions, and answering the second one while
    reporting on the first is how a guard comes to describe a state that no
    longer exists.

    ``state`` is one of the four ``GUARD_*`` constants. ``reason`` is prose for
    the states that have something to explain: which half of "no baseline" this
    is, why the baseline could not be read, or a possible cause of a mismatch.
    It is never the state itself.
    """

    __slots__ = ("state", "path", "reason", "recorded", "current")

    def __init__(self, state, path, reason=None, recorded=None, current=None):
        self.state = state
        self.path = path
        self.reason = reason
        self.recorded = recorded
        self.current = current

    @property
    def tripped(self) -> bool:
        """The compatibility bool: True only for a recorded digest mismatch.

        Deliberately False for BOTH no-baseline states. An unreadable sidecar is
        our own artifact failing, not the user's document changing, and refusing
        the export would charge the user for our bookkeeping with a fix they
        cannot find (delete a file they do not know exists).

        What that costs, stated plainly because an earlier version of this
        docstring did not: when there is no usable baseline the export CONTINUES
        WITHOUT CONTENT-GUARD PROTECTION for this file. Hand edits to
        exporter-owned content are regenerated away exactly as they would have
        been before this guard existed. ``describe`` reporting the state does
        not change that; a warning is observability, not protection, and it
        neither guards owned edits nor establishes that the content is
        unchanged. What the report buys is that the unprotected case is now
        distinguishable from the verified one, which is the whole of the claim.

        Whether an unreadable baseline should instead refuse, or require force,
        is a real question and a separate decision; it is not settled here.

        A corrupt CANONICAL file is a different failure and still raises out of
        the assessment, so the caller still fails closed on it.
        """
        return self.state == GUARD_CHANGED

    @property
    def verified(self) -> bool:
        """True only when a baseline existed and was actually compared."""
        return self.state in (GUARD_UNCHANGED, GUARD_CHANGED)

    def describe(self) -> str:
        """One line for the log and the MANIFEST, naming what was checked.

        "previous-export content guard", never "verification" unqualified: this
        compares the file against the digest of OUR last export of it and
        nothing else. It does not validate the document, check it against the
        board, or say the content is correct.

        The two unbaselined states say what the export then does, and they have
        to: "nothing was checked" describes the guard, and a reader can take it
        as a note about our bookkeeping rather than about their file. The export
        proceeds and regenerates exporter-owned content either way, so the line
        says so.
        """
        if self.state == GUARD_UNCHANGED:
            return ("previous-export content guard: exporter-owned content "
                    "matches the last export of this file")
        if self.state == GUARD_CHANGED:
            note = ("previous-export content guard: exporter-owned content "
                    "DIGEST MISMATCH against the recorded baseline; this alone "
                    "does not prove hand edits")
            return "%s; %s" % (note, self.reason) if self.reason else note
        if self.state == GUARD_NO_BASELINE:
            return ("previous-export content guard: no baseline, nothing was "
                    "checked (%s); this export continues WITHOUT content-guard "
                    "protection for this file" % self.reason)
        return ("previous-export content guard: baseline unreadable, nothing "
                "was checked (%s); this export continues WITHOUT content-guard "
                "protection for this file" % self.reason)


def assess_previous_export_content(final_path: str) -> ContentGuardAssessment:
    """Evaluate the previous-export content guard for ``final_path``, once.

    Compares the file's current exporter-content digest against the sidecar
    recorded after the last successful export. Returns one of the four states;
    never raises for a missing, empty or corrupt SIDECAR, which is the
    exporter's own artifact. It DOES propagate the exception from a corrupt or
    undecodable CANONICAL file, so the caller fails closed on a document it
    cannot read (orchestrator.py). Those are two different failures and the
    guard keeps them apart.
    """
    import os
    sidecar = digest_sidecar_path(final_path)
    if not os.path.exists(final_path):
        return ContentGuardAssessment(
            GUARD_NO_BASELINE, final_path,
            reason="first export: no canonical file on disk yet")
    if not os.path.exists(sidecar):
        return ContentGuardAssessment(
            GUARD_NO_BASELINE, final_path,
            reason="the file exists but has no recorded digest, so edits to "
                   "exporter-owned content are not protected until this export "
                   "records one")
    try:
        with open(sidecar, "r", encoding="utf-8") as fh:
            recorded = fh.read().strip()
    except (OSError, UnicodeDecodeError) as exc:
        return ContentGuardAssessment(
            GUARD_BASELINE_UNREADABLE, final_path,
            reason="the recorded digest could not be read: %s" % exc)
    if not recorded:
        return ContentGuardAssessment(
            GUARD_BASELINE_UNREADABLE, final_path,
            reason="the recorded digest is empty")
    blocks = split_for_rewrite(read_document(final_path))
    current = _digest_blocks(blocks)
    reason = None
    if current != recorded and "interconnect" in blocks:
        # Sidecars contain no exporter version. Presence of this block makes
        # the historical ownership change a possible cause, not a diagnosis.
        reason = ("exports made before 051dfd8 did not hash interconnect:, so "
                  "an older baseline can mismatch even if this file was not "
                  "edited")
    return ContentGuardAssessment(
        GUARD_CHANGED if current != recorded else GUARD_UNCHANGED,
        final_path, reason=reason, recorded=recorded, current=current)


def foreign_hand_edit_detected(final_path: str) -> bool:
    """True when the canonical file's exporter-content digest differs.

    Compatibility wrapper over ``assess_previous_export_content``: the SAME
    evaluation, read through ``tripped``. It is not a second comparison, and it
    must not become one. Behaviour is unchanged from before the four states
    existed, including False for a missing, empty or corrupt sidecar; what the
    states added is the ability to SAY which False this is.
    """
    return assess_previous_export_content(final_path).tripped
