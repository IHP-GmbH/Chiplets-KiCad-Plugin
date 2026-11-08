# SPDX-License-Identifier: GPL-3.0-or-later
"""The harness scores the splitter; this scores the harness.

``tests/oracle_harness_top_level_blocks.py`` sits at the repository root and is
not collected by either plugin suite, so until now nothing checked the thing
that reports conformance. That is a bad place for an unchecked rule: the
harness is what prints "agree 59, differ 0", and a scorer that is wrong in the
permissive direction produces exactly that line while measuring nothing.

The rule under test: a refusal is ``TopLevelGrammarRefusal``, and any other
exception is a CRASH, which fails. Scoring ``except Exception`` as a refusal
made a TypeError indistinguishable from a verdict, so a splitter that fell over
on its way to an answer scored the same as one that gave the right answer.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
HARNESS_PATH = os.path.join(REPO_ROOT, "tests", "oracle_harness_top_level_blocks.py")


@pytest.fixture(scope="module")
def harness():
    if not os.path.exists(HARNESS_PATH):
        pytest.skip("harness not found at %s" % HARNESS_PATH)
    spec = importlib.util.spec_from_file_location("oracle_harness", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["oracle_harness"] = module
    spec.loader.exec_module(module)
    return module


def _one_row_oracle(refused_by):
    """A minimal oracle carrying a single row, so the scorer is what is tested."""
    return {
        "refuse": [{"name": "row", "doc": "a:\n", "kind": "k",
                    "refused_by": refused_by}],
        "not_delimitable": [{"name": "row", "doc": "a:\n", "kind": "k",
                             "refused_by": refused_by}],
    }


class _Boom(object):
    """Stands in for a splitter that crashes rather than deciding."""

    def __init__(self, exc):
        self.exc = exc

    def __call__(self, _doc):
        raise self.exc


@pytest.mark.parametrize("scorer,group", [
    ("score_refuse", "refuse.splitter"),
    ("score_not_delimitable", "not_delimitable"),
])
def test_a_crash_is_not_scored_as_a_refusal(harness, monkeypatch, scorer, group):
    """The mutant: the splitter raises TypeError where a refusal was expected.

    Both scorers used to catch ``Exception`` and treat it as the refusal the row
    asked for, so this row scored OK. The row expects a verdict, and a stack
    unwinding past the scorer is not one.
    """
    monkeypatch.setattr(harness.chiplet_merge, "split_top_level_blocks",
                        _Boom(TypeError("exploded")))
    rep = harness.Report()
    getattr(harness, scorer)(_one_row_oracle(["splitter"]), rep)
    assert rep.failures(), (
        "%s scored a TypeError as a refusal; a crash and a verdict are not the "
        "same answer" % scorer)
    detail = rep.failures()[0][3]
    assert "TypeError" in detail and "not a refusal" in detail, (
        "the failure has to name what actually happened, or the next person "
        "reads it as the splitter disagreeing with the oracle: %r" % detail)


@pytest.mark.parametrize("scorer,group", [
    ("score_refuse", "refuse.splitter"),
    ("score_not_delimitable", "not_delimitable"),
])
def test_a_real_refusal_still_scores_as_one(harness, monkeypatch, scorer, group):
    """The positive control, without which the test above passes by rejecting
    everything. A scorer that failed on ALL exceptions would satisfy the mutant
    check and measure nothing."""
    refusal = harness.chiplet_merge.TopLevelGrammarRefusal(
        "no", 1, "a:", "unattributable_line_at_column_zero")
    monkeypatch.setattr(harness.chiplet_merge, "split_top_level_blocks",
                        _Boom(refusal))
    rep = harness.Report()
    getattr(harness, scorer)(_one_row_oracle(["splitter"]), rep)
    assert not rep.failures(), (
        "%s stopped accepting a genuine TopLevelGrammarRefusal: %r"
        % (scorer, rep.failures()))
