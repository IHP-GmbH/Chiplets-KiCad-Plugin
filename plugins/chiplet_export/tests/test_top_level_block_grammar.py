# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the chiplet-spec TOP-LEVEL BLOCK GRAMMAR oracle against the splitter.

The fixture is CONSUMED, never transcribed. Every case here is one row of
``conformance/fixtures/top_level_blocks_cases.json``, read at collection time,
so the oracle GATES this grammar instead of this file REMEMBERING it: a row
added upstream becomes a test here with no edit, and a row whose expectation
changes upstream changes the verdict here on the next re-vendor. Hand-written
cases would have frozen the 2026-09-03 semantics into a second, drifting copy,
which is exactly the failure the fixture exists to prevent.

Three verdicts, and they are independent (the fixture's own words):

* ``splits``          -- the split is the exact expected slice per key.
* ``refuse``          -- the SPLITTER refuses. A document here may still LOAD
                         (``loads``); refusal is about splitting, not reading.
* ``not_delimitable`` -- the flow node has no source slice: these documents are
                         valid and LOAD, the grammar just never captured that
                         node's bytes. Whether the splitter also refuses them is
                         a SEPARATE verdict carried per row in ``refused_by``,
                         and since version 5 both rows say it does. The group
                         name is not the verdict; the field is.

The pipeline's own, stricter verdict (``split_for_rewrite``, which also refuses
the not-delimitable documents because THIS caller is about to overwrite the
file) is tested separately at the bottom, and is deliberately not conflated
with the grammar's verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from chiplet_export.pipeline import chiplet_merge  # noqa: E402

from .conftest import chiplet_spec_pin, spec_blob_at_pin, spec_root  # noqa: E402

#: Vendored byte-for-byte from chiplet-spec's
#: ``conformance/fixtures/top_level_blocks_cases.json`` at ``ORACLE_COMMIT``.
#: Re-vendor with a plain copy; never edit it here (add a case upstream, in the
#: fixture). Provenance is also declared in ``VENDORED.md``.
ORACLE_UPSTREAM = "conformance/fixtures/top_level_blocks_cases.json"
ORACLE_PATH = os.path.join(HERE, "fixtures", "top_level_blocks_cases.json")
ORACLE_SHA256 = "45c37f47f142d88c63dfd3056a3dceba07afe6f2035d25243b7f87f2c37bccd0"
#: The chiplet-spec commit this suite is pinned to: the reader's pin, which
#: ``vendor/chiplet_format_io.SOURCE`` states. A tiered runner checks out that
#: one commit, so the oracle can follow no other.
ORACLE_COMMIT = chiplet_spec_pin()[0]

with open(ORACLE_PATH, "r", encoding="utf-8") as _fh:
    ORACLE = json.load(_fh)

#: The oracle's own version. Version 1 is any copy with no ``version`` key,
#: where the whole ``refuse`` group meant "a splitter refuses"; version 2 added
#: the per-case ``refused_by`` list and the U+000D rows; version 3 turned
#: ``loadable`` into an obligation in both directions; version 4 restates
#: the repeated-key justification and changed no verdict, so a consumer
#: pinned at 3 reads the same rows; version 5 generalises the quoted-key
#: refusal into ``unattributable_line_at_column_zero`` and DOES move verdicts:
#: the two ``not_delimitable`` documents are refused by a splitter now, so a
#: consumer pinned at 4 that splits them disagrees with the fixture. That is
#: why this constant is asserted and not tolerated -- the inversion is silent
#: otherwise, and it was: the harness scored those two rows 2/2 against version
#: 5 by agreeing with the rule version 4 had. A consumer that reads
#: ``refused_by`` off a version 1 copy gets ``None`` for every row and silently
#: tests nothing, which is the same shape one field along. Failing on the
#: version number is what makes a stale re-vendor loud instead of green.
ORACLE_VERSION = 5
assert ORACLE.get("version") == ORACLE_VERSION, (
    "the vendored oracle is version %r, this consumer reads version %d. A copy "
    "without 'refused_by' would make every refusal test below vacuous."
    % (ORACLE.get("version"), ORACLE_VERSION))


def _ids(rows, key="name"):
    return [row[key] for row in rows]


def _refused_by(who, rows=None):
    """The refuse rows THIS kind of implementation must reject.

    Read the field, never the group name. The group is "documents SOME
    implementation refuses"; it was "documents a splitter refuses" only while
    every row in it happened to be one. When reader-only rows arrived (a
    forbidden line break, which a splitter has a perfectly good answer for),
    every consumer that had parametrized "the splitter must raise" over the
    whole group went red on a verdict that had been inverted under it.
    """
    return [row for row in (ORACLE["refuse"] if rows is None else rows)
            if who in row["refused_by"]]


#: Every row that names the splitter, in WHATEVER group it lives. Version 5
#: added two rows to this set without moving them between groups: a flow-style
#: document and ``flow :`` stayed in ``not_delimitable`` and grew
#: ``refused_by: ["splitter"]``, because both are lines at column zero that no
#: top-level key owns. Built from the group list rather than from ``refuse``
#: alone for exactly the reason ``_refused_by`` exists: the group a row sits in
#: is not its verdict.
_GROUPS_CARRYING_A_SPLITTER_VERDICT = ("refuse", "not_delimitable")
SPLITTER_REFUSES = [row
                    for group in _GROUPS_CARRYING_A_SPLITTER_VERDICT
                    for row in _refused_by("splitter", ORACLE[group])]
#: Only the refuse group: every ``not_delimitable`` document LOADS by
#: construction, which is what makes it undelimitable rather than ill-formed.
READER_REFUSES = _refused_by("reader")


# --------------------------------------------------------------------------
# Provenance: the copy must stay a copy.
# --------------------------------------------------------------------------

def test_oracle_copy_is_unmodified():
    """The vendored fixture is the pinned upstream bytes, not a local edit."""
    with open(ORACLE_PATH, "rb") as fh:
        got = hashlib.sha256(fh.read()).hexdigest()
    assert got == ORACLE_SHA256, (
        "tests/fixtures/top_level_blocks_cases.json was edited in place. It is "
        "a copy of chiplet-spec %s; change the fixture upstream and re-vendor, "
        "updating ORACLE_SHA256 and VENDORED.md in the same commit." % ORACLE_COMMIT)


def test_vendored_md_states_the_oracle_pin():
    """VENDORED.md's oracle entry names the pin and these bytes."""
    from .test_vendored_copies import vendored_md_entry
    commit, sha256 = vendored_md_entry("tests/fixtures/top_level_blocks_cases.json",
                                       ORACLE_UPSTREAM)
    assert ORACLE_COMMIT.startswith(commit) and len(commit) >= 7, (commit, ORACLE_COMMIT)
    assert sha256 == ORACLE_SHA256


@pytest.mark.needs_sibling_sources
def test_oracle_copy_matches_the_spec_checkout_when_present():
    """When chiplet-spec is checked out next door, the copy must equal it.

    Skipped where the sibling repo is absent (CI for this repo alone), so the
    suite still runs standalone; where it IS present, a fixture that moved
    upstream fails HERE rather than silently keeping the old semantics.
    """
    root = spec_root()
    if root is None:
        pytest.skip("chiplet-spec checkout not found (set CHIPLET_SPEC_ROOT)")
    upstream = os.path.join(root, *ORACLE_UPSTREAM.split("/"))
    if not os.path.exists(upstream):
        pytest.skip("chiplet-spec checkout has no top_level_blocks_cases.json")
    with open(upstream, "rb") as fh:
        upstream_sha = hashlib.sha256(fh.read()).hexdigest()
    if upstream_sha == ORACLE_SHA256:
        return
    head = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    pytest.fail(
        "the oracle moved upstream (chiplet-spec %s) and this repo still "
        "vendors %s. Re-vendor the fixture and re-run; if a verdict changed, "
        "the grammar changes with it.\n  vendored: %s\n  upstream: %s"
        % (head or "?", ORACLE_COMMIT, ORACLE_SHA256, upstream_sha))


@pytest.mark.needs_sibling_sources
def test_the_oracle_pin_carries_those_bytes():
    """ORACLE_COMMIT names a commit whose fixture IS this copy.

    The check above follows the checkout's working tree, so the oracle moving
    upstream is loud. This one binds the constant: under a tiered runner it
    must be the pinned chiplet-spec, and on a host its blob is read from
    history (``spec_blob_at_pin``).
    """
    root = spec_root()
    if root is None:
        pytest.skip("chiplet-spec checkout not found (set CHIPLET_SPEC_ROOT)")
    blob = spec_blob_at_pin(root, ORACLE_COMMIT, ORACLE_UPSTREAM)
    assert hashlib.sha256(blob).hexdigest() == ORACLE_SHA256, (
        "chiplet-spec %s:%s does not hash to the pinned sha256. The pin names "
        "the wrong commit, or the copy came from somewhere else."
        % (ORACLE_COMMIT, ORACLE_UPSTREAM))


# --------------------------------------------------------------------------
# key_lines: the one-line predicate.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("row", ORACLE["key_lines"]["accept"],
                         ids=_ids(ORACLE["key_lines"]["accept"], "line"))
def test_key_line_accepted(row):
    assert chiplet_merge.top_level_key_line(row["line"]) == row["key"]


@pytest.mark.parametrize("line", ORACLE["key_lines"]["reject"],
                         ids=[repr(x) for x in ORACLE["key_lines"]["reject"]])
def test_key_line_rejected(line):
    assert chiplet_merge.top_level_key_line(line) is None


@pytest.mark.parametrize("row", ORACLE["key_lines"]["accept"],
                         ids=_ids(ORACLE["key_lines"]["accept"], "line"))
@pytest.mark.parametrize("terminator", ["", "\n", "\r\n"])
def test_key_line_predicate_is_anchored_at_the_end(row, terminator):
    """``\\Z`` and not ``$``: the predicate must see exactly one line.

    The fixture warns about this directly. With ``$`` the expression also
    matches before a trailing newline, so a two-line string would pass a
    one-line predicate; here the terminator is stripped by ``iter_lines``
    first, and this pins that the two agree.
    """
    lines = list(chiplet_merge.iter_lines(row["line"] + terminator))
    assert len(lines) == 1
    assert chiplet_merge.top_level_key_line(lines[0][1]) == row["key"]


# --------------------------------------------------------------------------
# splits: the exact slices.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case", ORACLE["splits"], ids=_ids(ORACLE["splits"]))
def test_split_matches_the_oracle(case):
    got = list(chiplet_merge.split_top_level_blocks(case["doc"]).items())
    want = [(block["key"], block["text"]) for block in case["blocks"]]
    assert got == want


@pytest.mark.parametrize("case", ORACLE["splits"], ids=_ids(ORACLE["splits"]))
def test_split_is_lossless(case):
    """Joining the slices in order reproduces the document byte for byte."""
    got = chiplet_merge.split_top_level_blocks(case["doc"])
    assert "".join(got.values()) == case["doc"]


# --------------------------------------------------------------------------
# refuse / not_delimitable: the two verdicts people keep conflating.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case", SPLITTER_REFUSES, ids=_ids(SPLITTER_REFUSES))
def test_splitter_refuses(case):
    with pytest.raises(chiplet_merge.TopLevelGrammarRefusal) as excinfo:
        chiplet_merge.split_top_level_blocks(case["doc"])
    # The message must name the line, since that is what the user has to fix.
    assert excinfo.value.lineno >= 1
    assert excinfo.value.line in case["doc"]


@pytest.mark.parametrize("case", READER_REFUSES, ids=_ids(READER_REFUSES))
def test_a_reader_only_refusal_is_not_the_splitters_job(case):
    """The grammar has an answer for these bytes, and giving it is correct.

    A forbidden line break is a character a YAML parser breaks a line on and
    this grammar does not. That is a READER's refusal: the splitter sees no key
    line there, so it attaches nothing and loses nothing. Asserting the split
    still succeeds is what stops a future "just refuse everything in the refuse
    group" from being mistaken for conformance.
    """
    if "splitter" in case["refused_by"]:
        pytest.skip("also a splitter refusal; covered above")
    blocks = chiplet_merge.split_top_level_blocks(case["doc"])
    assert "".join(blocks.values()) == case["doc"]


@pytest.mark.parametrize("case", ORACLE["not_delimitable"],
                         ids=_ids(ORACLE["not_delimitable"]))
def test_splitter_does_not_refuse_a_merely_undelimitable_document(case):
    """Undelimitable is not the same verdict as unsplittable; read the row.

    A document whose flow node has no source slice still LOADS, and refusing it
    in the grammar for that reason alone would break flow rule 1. But "has no
    slice" and "can be split" are separate questions, and version 5 separated
    them on these two rows: a flow-style document and ``flow :`` are lines at
    column zero that no top-level key owns, so they are unsplittable too, and
    both rows now say ``refused_by: ["splitter"]``.

    This test asserted the SUCCESS half over the whole group, which was the
    group's verdict when it was written and is no longer every row's. The check
    that matters is the same one everywhere else in this file: read the field.
    The refusing rows are covered by ``test_splitter_refuses``, which also
    checks the message names the offending line.

    Not covered by this test today, by construction: with version 5 every row
    in the group names the splitter, so every row skips and the success half
    below runs against nothing. It is kept rather than deleted because it is
    the group's defining property and the next row added may well carry it.
    """
    if "splitter" in case["refused_by"]:
        pytest.skip("also a splitter refusal; covered by test_splitter_refuses")
    blocks = chiplet_merge.split_top_level_blocks(case["doc"])
    assert "flow" not in blocks
    assert "".join(blocks.values()) == case["doc"]


# --------------------------------------------------------------------------
# Agreement with the reference splitter, on documents the oracle has no row
# for. The oracle is the contract; this is the part of the contract nobody
# wrote a row for yet, and it is where the last regression actually lived.
# --------------------------------------------------------------------------

#: Documents where the two splitters must agree, each named by the property it
#: is a witness for. The first three were found by probing the reference, not
#: by reading the fixture, and all three were REFUSED here while the reference
#: attributed them: the splitter had begun consulting the rewrite policy, which
#: is stricter on purpose, so restrictions belonging to a caller about to
#: overwrite a file leaked into the grammar.
#:
#: The fourth is the negative control, and it has to be a TAB-led CONTENT line.
#: A standalone TAB blank OUTSIDE a scalar is not the witness: PyYAML does not
#: read that document at all, so it cannot separate "the grammar refuses" from
#: "nothing reads this anyway".
#: The first four texts are the review's own probe cases, kept byte for byte so
#: this test and the recorded evidence are about the same documents. That probe
#: PRINTED the difference and exited 0, which is an observation and not a gate;
#: turning each row into an assertion is the whole point of this test existing.
_BASE = 'format_version: "1.0"\nassembly:\n  name: demo\n'
REFERENCE_AGREEMENT = [
    ("tab_only_blank_line", _BASE + "\t\n"),
    ("tab_and_space_blank_inside_a_quoted_scalar",
     _BASE + 'flow:\n  description: "first\n\t \n    last"\n'),
    ("yaml_directive_before_the_document_marker", "%YAML 1.1\n---\n" + _BASE),
    ("terminal_document_end_marker", _BASE + "...\n"),
    ("tab_led_content_line_is_still_refused",
     'format_version: "1.0"\nassembly:\n  name: "demo\n\tcontinued"\n'),
]


def _reference_splits(reader, doc):
    """(accepted, blocks-or-None) from the vendored reference splitter.

    ``ChipletFormatError`` and nothing wider. This caught bare ``Exception``,
    which is the same defect this suite's harness had: any crash on the way to
    an answer scored as the refusal the row expected, so a reference that fell
    over read as a reference that disagreed. ``top_level_blocks`` documents
    ``ChipletFormatError`` as its refusal, and a TypeError out of the vendored
    copy is news, not a verdict -- it must reach the failure report as itself.
    """
    try:
        return True, reader.top_level_blocks(doc)
    except reader.ChipletFormatError:
        return False, None


@pytest.mark.parametrize("name,doc", REFERENCE_AGREEMENT,
                         ids=[row[0] for row in REFERENCE_AGREEMENT])
def test_the_splitter_agrees_with_the_reference(name, doc):
    """Same verdict as the vendored reference, and the same slices when it splits.

    Not "our splitter accepts these": that would freeze today's answer and go
    stale the moment the reference moves. The proposition is AGREEMENT, and the
    reference is a pinned, gated copy, so when it changes its mind this fails
    and somebody decides, which is the whole reason the copy is vendored.

    Note what is deliberately NOT asserted: that PyYAML loads any of them. Two
    of the four are documents PyYAML refuses outright, and the reference
    attributes them anyway. Splitting is not reading; a document nobody will
    load still has an answer to "which top-level key owns this line", and the
    grammar owes that answer.
    """
    reader = _vendored_reader()
    accepted, reference_blocks = _reference_splits(reader, doc)
    try:
        ours = chiplet_merge.split_top_level_blocks(doc)
    except chiplet_merge.TopLevelGrammarRefusal as exc:
        assert not accepted, (
            "the reference splitter attributes every line of %s and this one "
            "refuses it: %s. A refusal the reference does not share is this "
            "grammar applying a restriction that belongs somewhere else."
            % (name, exc))
        return
    assert accepted, (
        "this splitter attributed %s and the reference refuses it" % name)
    assert list(ours.items()) == list(reference_blocks.items()), (
        "both accept %s and they cut it differently" % name)
    assert "".join(ours.values()) == doc, "the split of %s is lossy" % name


def test_the_rewrite_policy_stays_stricter_than_the_grammar():
    """The separation is the point, so it is asserted rather than assumed.

    Two of the witnesses above are documents the GRAMMAR must attribute and the
    REWRITE policy must still refuse: rewriting a file around a second document
    or a directive hands back something no reader of the whole stream saw. If a
    later edit collapses the two functions again the suite has to notice, and
    the only way it notices is by checking that they still disagree.
    """
    # By NAME and not by index. Selecting these two positionally broke the
    # moment a case was inserted above them, and it broke silently in the sense
    # that matters: the test still ran, against two documents nobody chose.
    witnesses = dict(REFERENCE_AGREEMENT)
    for name in ("yaml_directive_before_the_document_marker",
                 "terminal_document_end_marker"):
        doc = witnesses[name]
        chiplet_merge.split_top_level_blocks(doc)          # grammar: fine
        assert chiplet_merge.unmodelled_top_level_line(doc) is not None, (
            "the rewrite policy stopped refusing a document it used to refuse; "
            "the grammar was meant to get more permissive, not this")

def _vendored_reader():
    """The vendored reference reader, worker tier only (it needs PyYAML)."""
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "vendor"))
    import chiplet_format_io  # noqa: E402
    return chiplet_format_io


def _assert_refused_for_the_right_reason(cfio, row):
    """Refused, AND the refusal is about the thing the row is about.

    ``pytest.raises(Exception)`` is not enough here and the first version of
    this test made exactly that mistake. Every one of these documents is a
    fragment with no ``format_version``, so the reader refuses all of them with
    ``ChipletFormatError`` whether or not it knows anything about line breaks;
    the check passed against a reader that had never heard of them. So the
    proposition is the MESSAGE: it has to name the code point and the line, and
    the oracle carries both, which is what makes it discriminating.
    """
    with pytest.raises(cfio.ChipletFormatError) as excinfo:
        cfio.loads(row["doc"])
    message = str(excinfo.value)
    if row.get("code_point"):
        assert row["code_point"] in message, (
            "refused, but not for the line break: %r" % message)
        assert "line %d" % row["line"] in message, (
            "refusal does not name line %d: %r" % (row["line"], message))
    return message


@pytest.mark.parametrize("case", READER_REFUSES, ids=_ids(READER_REFUSES))
def test_the_vendored_reader_refuses_what_a_reader_must(case):
    """The copy in ``vendor/`` has to satisfy the oracle it is shipped beside.

    Worth its own test because the copy is the thing that goes stale. The reader
    vendored before this one was ten commits behind and predated the CR row of
    the line-break set, so it LOADED a document the oracle says every reader
    refuses, and nothing here said so: the fixture was gated, the reader was
    not. Pinning the reader against the same fixture closes that gap, and the
    next stale re-vendor fails on the row it actually gets wrong.

    Measured against that older copy, this test fails on nine of nine rows, and
    four of them fail with ``unsupported format_version '9.0'``: the reader had
    read the SMUGGLED ``format_version`` past the line break, so the attack the
    row describes is visible succeeding in the error message.
    """
    _assert_refused_for_the_right_reason(_vendored_reader(), case)


def test_the_vendored_reader_refuses_the_unloadable_split_cases():
    """``loadable: false`` is an obligation in version 3, in both directions.

    It used to mean "no reader is REQUIRED to load this", a permission, and one
    row carried a false claim under it while every test stayed green. So the
    rows that carry it are executed, not read.
    """
    cfio = _vendored_reader()
    unloadable = [row for group in ("splits", "not_delimitable")
                  for row in ORACLE[group] if row.get("loadable") is False]
    assert unloadable, "no loadable:false row left; this test has lost its subject"
    for row in unloadable:
        _assert_refused_for_the_right_reason(cfio, row)


def test_a_loadable_row_really_does_load():
    """The other half, or the tests above only prove the reader says no a lot.

    A reader that refused every document would satisfy every assertion in this
    section. The oracle's rule is that a case WITHOUT the flag is one every
    reader loads, so at least the complete documents in it have to come back.
    """
    cfio = _vendored_reader()
    loaded = 0
    for group in ("splits", "not_delimitable"):
        for row in ORACLE[group]:
            if row.get("loadable") is False:
                continue
            try:
                cfio.loads(row["doc"])
            except cfio.ChipletFormatError as exc:
                # Most rows are fragments written to exercise the grammar, not
                # whole documents, so a missing required key is expected. A
                # line-break complaint is NOT, and that is the discrimination.
                assert "line breaks are LF and CRLF" not in str(exc), row["name"]
                continue
            loaded += 1
    assert loaded, "no oracle row loads at all; the reader is refusing everything"


# --------------------------------------------------------------------------
# The pipeline's own verdict: stricter, and stricter on purpose.
# --------------------------------------------------------------------------

#: The one oracle-splittable document this pipeline still refuses to REWRITE,
#: and the reason is recorded here rather than in a skip so it stays visible.
#:
#: After a PLAIN scalar the character starts a new line for PyYAML and not for
#: this grammar, which is how an owned ``components:`` rides into a carried
#: foreign block and wins by last-wins. Telling that apart from the quoted case
#: requires a YAML parser, and the GUI tier has none.
#:
#: An earlier version of this comment said U+2028 inside a QUOTED scalar is
#: folded by PyYAML so both readers agree and rewriting it would be safe. That
#: is wrong in both halves, and it is worth keeping the correction because the
#: mistake was generalising from key-set agreement to agreement. Measured:
#: inside a quoted scalar CR and NEL ARE folded, to a single space, so the raw
#: byte does not survive and a YAML 1.2 reader that keeps it returns a
#: DIFFERENT string, silently; U+2028 and U+2029 are not folded at all. So the
#: quoted case is not the safe one, it is the one where two readers disagree
#: about a value while agreeing about the keys. The refusal is about the RAW
#: BYTES; escaped forms stay legal and are the way to write these characters.
#:
#: So the write verdict is blunt on purpose. Measured cost: 0 refusals across
#: 187 real .chiplet documents in the ecosystem; the only files it stops are
#: chiplet-spec's own negative fixtures. A blunt refusal that costs nothing
#: measurable beats a sharp one that needs a parser this tier cannot have.
_REWRITE_REFUSES_ANYWAY = {"unicode_line_separator_inside_a_scalar_is_not_a_line_break"}


@pytest.mark.parametrize("case", ORACLE["splits"], ids=_ids(ORACLE["splits"]))
def test_rewrite_accepts_every_splittable_document(case):
    """The extra strictness must not touch a document the oracle splits.

    One documented exception, above. The oracle constrains a SPLITTER, and
    ``split_top_level_blocks`` does accept that row (the parity harness scores
    50/50); what this pipeline declines is writing it back.
    """
    if case["name"] in _REWRITE_REFUSES_ANYWAY:
        with pytest.raises(chiplet_merge.TopLevelGrammarRefusal) as refused:
            chiplet_merge.split_for_rewrite(case["doc"])
        assert refused.value.kind == "break_character"
        # The split itself, which is what the oracle actually pins, still works.
        assert (list(chiplet_merge.split_top_level_blocks(case["doc"]).items())
                == [(b["key"], b["text"]) for b in case["blocks"]])
        return
    assert (list(chiplet_merge.split_for_rewrite(case["doc"]).items())
            == [(b["key"], b["text"]) for b in case["blocks"]])


@pytest.mark.parametrize(
    "case", ORACLE["refuse"] + ORACLE["not_delimitable"],
    ids=_ids(ORACLE["refuse"]) + _ids(ORACLE["not_delimitable"]))
def test_rewrite_refuses_everything_it_cannot_attribute(case):
    """A rewriter refuses the undelimitable documents too, and says why.

    Not a disagreement with the oracle: the oracle constrains a SPLITTER, and
    ``split_for_rewrite`` is a caller that is about to destroy the existing
    bytes. A block whose bytes the grammar never captured is content that
    disappears, so this caller has to refuse where a reader would not.
    """
    with pytest.raises(chiplet_merge.TopLevelGrammarRefusal):
        chiplet_merge.split_for_rewrite(case["doc"])
