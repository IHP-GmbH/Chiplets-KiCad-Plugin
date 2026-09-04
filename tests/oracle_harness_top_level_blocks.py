# SPDX-License-Identifier: GPL-3.0-or-later
"""Score ``chiplet_merge.split_top_level_blocks`` against the chiplet-spec oracle.

Read-only conformance harness for the TOP-LEVEL BLOCK GRAMMAR fixture
``conformance/fixtures/top_level_blocks_cases.json`` (chiplet-spec 6e640fb,
oracle version 3).
It never edits the plugin; it only reports which oracle rows the current
splitter agrees with.

Usage::

    python3 tests/oracle_harness_top_level_blocks.py [path/to/top_level_blocks_cases.json]

Exit code 0 when every row agrees, 1 otherwise. Groups scored:

* ``key_lines.accept``  -- ``_KEY_LINE_RE`` matches and ``group(1)`` is the key.
* ``key_lines.reject``  -- ``_KEY_LINE_RE`` must not match.
* ``splits``            -- the returned OrderedDict must equal the expected
                           ``blocks`` list EXACTLY: same keys, same order, same
                           text bytes. The lossless property (concatenation of
                           every slice reproduces ``doc``) is checked too.
* ``refuse.splitter``   -- rows whose ``refused_by`` names the splitter: it must
                           REFUSE, observed as raising; returning blocks is not
                           a refusal.
* ``refuse.reader_only``-- rows a READER refuses and the splitter must not. The
                           grammar has an answer for a forbidden line break (no
                           key line starts there), so refusing would be wrong;
                           scored as "split, and still lossless".
* ``not_delimitable``   -- the splitter must succeed and produce NO ``flow`` key
                           (the flow node exists to a YAML reader but the grammar
                           cannot delimit its bytes).
"""

from __future__ import annotations

import collections
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_ROOT = os.path.join(os.path.dirname(_HERE), "plugins")
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from chiplet_export.pipeline import chiplet_merge  # noqa: E402

#: The vendored copy, which is pinned and gated (see ``VENDORED.md``). It used
#: to default to a scratch copy under /tmp, which existed on exactly one machine
#: for exactly one session: the harness then reported a perfect score against a
#: file nobody else had, and against a version of the oracle that predated the
#: ``refused_by`` field. Default to the copy the suite also runs.
DEFAULT_ORACLE = os.path.join(
    os.path.dirname(_HERE), "plugins", "chiplet_export", "tests", "fixtures",
    "top_level_blocks_cases.json",
)

#: Oracle version this harness understands; see the note in
#: ``test_top_level_block_grammar.py``. Reading ``refused_by`` off an older copy
#: yields ``None`` for every row and scores a vacuous 100%.
ORACLE_VERSION = 3


def _q(text: str) -> str:
    """Readable, unambiguous rendering of a slice (escapes and all)."""
    return json.dumps(text, ensure_ascii=True)


class Report(object):
    def __init__(self):
        self.rows = []          # (group, name, ok, detail)

    def add(self, group, name, ok, detail=""):
        self.rows.append((group, name, bool(ok), detail))

    def group_counts(self):
        counts = collections.OrderedDict()
        for group, _name, ok, _detail in self.rows:
            passed, total = counts.get(group, (0, 0))
            counts[group] = (passed + (1 if ok else 0), total + 1)
        return counts

    def failures(self):
        return [r for r in self.rows if not r[2]]


def score_key_lines(oracle, rep):
    for row in oracle["key_lines"]["accept"]:
        line, want = row["line"], row["key"]
        m = chiplet_merge._KEY_LINE_RE.match(line)
        got = m.group(1) if m else None
        rep.add("key_lines.accept", _q(line), got == want,
                "want key %r, got %r" % (want, got))
    for line in oracle["key_lines"]["reject"]:
        m = chiplet_merge._KEY_LINE_RE.match(line)
        got = m.group(1) if m else None
        rep.add("key_lines.reject", _q(line), m is None,
                "expected no match, got key %r" % (got,))


def score_splits(oracle, rep):
    for case in oracle["splits"]:
        name, doc = case["name"], case["doc"]
        want = [(b["key"], b["text"]) for b in case["blocks"]]
        try:
            got = list(chiplet_merge.split_top_level_blocks(doc).items())
        except Exception as exc:                      # noqa: BLE001
            rep.add("splits", name, False, "raised %s: %s" % (type(exc).__name__, exc))
            continue
        ok = got == want
        detail = ""
        if not ok:
            detail = ("keys want %r got %r" % ([k for k, _ in want], [k for k, _ in got]))
            for i in range(max(len(want), len(got))):
                w = want[i] if i < len(want) else None
                g = got[i] if i < len(got) else None
                if w != g:
                    detail += ("\n      slot %d: want %s -> %s\n                got  %s -> %s"
                               % (i,
                                  _q(w[0]) if w else "<missing>",
                                  _q(w[1]) if w else "<missing>",
                                  _q(g[0]) if g else "<missing>",
                                  _q(g[1]) if g else "<missing>"))
        rep.add("splits", name, ok, detail)
        # Lossless property: concatenating the slices in order reproduces doc.
        joined = "".join(t for _k, t in got)
        rep.add("splits.lossless", name, joined == doc,
                "concat != doc\n      concat %s\n      doc    %s" % (_q(joined), _q(doc)))


def score_refuse(oracle, rep):
    """Score BOTH directions of the refuse group, split by ``refused_by``.

    The group is "documents SOME implementation refuses". Scoring the whole of
    it as "the splitter must raise" was correct only while every row happened to
    be a splitter row; the reader-only rows (a forbidden line break, which the
    grammar has a perfectly good answer for) invert the verdict underneath. A
    harness that keeps scoring them the old way reports a failure where the
    splitter is right, which is the same defect as reporting a pass where it is
    wrong, only in the direction people notice.
    """
    for case in oracle["refuse"]:
        name = case["name"]
        must_refuse = "splitter" in case["refused_by"]
        group = "refuse.splitter" if must_refuse else "refuse.reader_only"
        try:
            got = list(chiplet_merge.split_top_level_blocks(case["doc"]).items())
        except Exception as exc:                      # noqa: BLE001
            rep.add(group, name, must_refuse,
                    "refused with %s: %s" % (type(exc).__name__, exc)
                    if must_refuse else
                    "kind=%s: refused with %s, but this row is refused_by=%r. "
                    "The splitter loses nothing here: no key line starts at "
                    "that character, so the block is attributed correctly.\n"
                    "      %s: %s"
                    % (case.get("kind"), case["refused_by"],
                       type(exc).__name__, exc))
            continue
        if not must_refuse:
            # It must also still be LOSSLESS, or "did not refuse" is hiding a
            # silent drop rather than a correct attribution.
            joined = "".join(t for _k, t in got)
            rep.add(group, name, joined == case["doc"],
                    "did not refuse (correct) but concat != doc\n"
                    "      concat %s\n      doc    %s" % (_q(joined), _q(case["doc"])))
            continue
        detail = ("kind=%s: did NOT refuse; returned keys %r"
                  % (case.get("kind"), [k for k, _ in got]))
        for k, t in got:
            detail += "\n      %s -> %s" % (_q(k), _q(t))
        rep.add(group, name, False, detail)


def score_not_delimitable(oracle, rep):
    for case in oracle["not_delimitable"]:
        name = case["name"]
        try:
            got = chiplet_merge.split_top_level_blocks(case["doc"])
        except Exception as exc:                      # noqa: BLE001
            rep.add("not_delimitable", name, False,
                    "raised %s: %s" % (type(exc).__name__, exc))
            continue
        ok = "flow" not in got
        rep.add("not_delimitable", name, ok,
                "keys %r; 'flow' present=%s (expected absent)"
                % (list(got), "flow" in got))


def main(argv):
    path = argv[1] if len(argv) > 1 else DEFAULT_ORACLE
    with open(path, "r", encoding="utf-8") as fh:
        oracle = json.load(fh)

    got_version = oracle.get("version")
    if got_version != ORACLE_VERSION:
        print("oracle: %s" % path)
        print("REFUSING to score: oracle version %r, this harness reads %d.\n"
              "A copy without 'refused_by' scores every refuse row the old way "
              "and reports a number that means nothing." % (got_version, ORACLE_VERSION))
        return 1

    rep = Report()
    score_key_lines(oracle, rep)
    score_splits(oracle, rep)
    score_refuse(oracle, rep)
    score_not_delimitable(oracle, rep)

    print("oracle: %s" % path)
    print("target: %s" % chiplet_merge.__file__)
    print("")
    print("per-group score")
    scored_total = 0
    scored_pass = 0
    for group, (passed, total) in rep.group_counts().items():
        marker = "OK  " if passed == total else "FAIL"
        print("  %s %-22s %d/%d" % (marker, group, passed, total))
        if group != "splits.lossless":          # derived check, not an oracle row
            scored_total += total
            scored_pass += passed
    print("")
    print("oracle rows: %d, agree %d, differ %d"
          % (scored_total, scored_pass, scored_total - scored_pass))

    fails = rep.failures()
    if fails:
        print("")
        print("disagreeing rows")
        for group, name, _ok, detail in fails:
            print("  [%s] %s" % (group, name))
            if detail:
                print("      %s" % detail)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
