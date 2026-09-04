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
* ``not_delimitable`` -- the splitter SUCCEEDS and simply produces no ``flow``
                         key. These documents are valid and loadable; the
                         grammar just never captured that node's bytes.

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

#: Vendored byte-for-byte from chiplet-spec 8a2e6be
#: ``conformance/fixtures/top_level_blocks_cases.json``. Re-vendor with a plain
#: copy; never edit it here (add a case upstream, in the fixture).
ORACLE_PATH = os.path.join(HERE, "fixtures", "top_level_blocks_cases.json")
ORACLE_SHA256 = "20a80fc0623346643479cebeab697cc3883060ffe2e9a305fa9e44add64e0de3"
ORACLE_COMMIT = "8a2e6be"

with open(ORACLE_PATH, "r", encoding="utf-8") as _fh:
    ORACLE = json.load(_fh)


def _ids(rows, key="name"):
    return [row[key] for row in rows]


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
        "updating ORACLE_SHA256/ORACLE_COMMIT in the same commit." % ORACLE_COMMIT)


def _spec_root():
    env = os.environ.get("CHIPLET_SPEC_ROOT")
    if env and os.path.isdir(env):
        return env
    sibling = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", "chiplet-spec"))
    return sibling if os.path.isdir(os.path.join(sibling, ".git")) else None


def test_oracle_copy_matches_the_spec_checkout_when_present():
    """When chiplet-spec is checked out next door, the copy must equal it.

    Skipped where the sibling repo is absent (CI for this repo alone), so the
    suite still runs standalone; where it IS present, a fixture that moved
    upstream fails HERE rather than silently keeping the old semantics.
    """
    root = _spec_root()
    if root is None:
        pytest.skip("chiplet-spec checkout not found (set CHIPLET_SPEC_ROOT)")
    upstream = os.path.join(root, "conformance", "fixtures",
                            "top_level_blocks_cases.json")
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

@pytest.mark.parametrize("case", ORACLE["refuse"], ids=_ids(ORACLE["refuse"]))
def test_splitter_refuses(case):
    with pytest.raises(chiplet_merge.TopLevelGrammarRefusal) as excinfo:
        chiplet_merge.split_top_level_blocks(case["doc"])
    # The message must name the line, since that is what the user has to fix.
    assert excinfo.value.lineno >= 1
    assert excinfo.value.line in case["doc"]


@pytest.mark.parametrize("case", ORACLE["not_delimitable"],
                         ids=_ids(ORACLE["not_delimitable"]))
def test_splitter_does_not_refuse_a_merely_undelimitable_document(case):
    """These LOAD and SPLIT; they just yield no slice for the flow node.

    Refusing them in the grammar would break flow rule 1. Refusing them in the
    EXPORTER is a different decision, made one layer up; see below.
    """
    blocks = chiplet_merge.split_top_level_blocks(case["doc"])
    assert "flow" not in blocks
    assert "".join(blocks.values()) == case["doc"]


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
    40/40); what this pipeline declines is writing it back.
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
