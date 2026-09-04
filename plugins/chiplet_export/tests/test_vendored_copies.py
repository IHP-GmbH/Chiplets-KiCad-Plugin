# SPDX-License-Identifier: GPL-3.0-or-later
"""Gate the vendored reader copy against the commit ``VENDORED.md`` declares.

A provenance file nobody enforces is a claim, not a property: it stays true only
until the first person edits the copy in place, and it is precisely then that it
stops being read. So the declaration and the check live next to each other, and
the check is what fails.

Two questions, deliberately separate:

* the local copy is the pinned bytes (always runs, no sibling repo needed);
* the pin names a commit whose blob IS those bytes (runs where chiplet-spec is
  checked out, and reads the COMMIT, not the working tree).

The second one reads ``git show <commit>:<path>`` rather than the checkout's
current file on purpose. A sibling checkout parked on a feature branch is not
evidence that this copy drifted, and a gate that goes red for that reason trains
people to ignore it. Drift that matters is a decision to re-vendor, which is made
by looking at upstream, not by a red here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)

#: Vendored byte-for-byte from chiplet-spec; see ``VENDORED.md``, which carries
#: the same three facts and must be updated in the same commit as the copy.
READER_PATH = os.path.join(PLUGIN_ROOT, "vendor", "chiplet_format_io", "__init__.py")
READER_UPSTREAM = "reference/python/chiplet_format_io/__init__.py"
#: The same file addressed inside THIS repo, for git history queries.
READER_UPSTREAM_LOCAL = "plugins/chiplet_export/vendor/chiplet_format_io/__init__.py"
ORACLE_PATH = os.path.join(HERE, "fixtures", "top_level_blocks_cases.json")
READER_COMMIT = "cdfa737"
READER_SHA256 = "5c1d3ebe71c2926bc1c1505e2b42493c98a8fff1ef0b503beed9a32ded624cf2"


def _sha256(blob):
    return hashlib.sha256(blob).hexdigest()


def test_the_reader_copy_is_unmodified():
    """The vendored reader is the pinned bytes, not a local edit."""
    with open(READER_PATH, "rb") as fh:
        got = _sha256(fh.read())
    assert got == READER_SHA256, (
        "vendor/chiplet_format_io/__init__.py was edited in place. It is a copy "
        "of chiplet-spec %s; change it upstream and re-vendor, updating "
        "READER_SHA256/READER_COMMIT and VENDORED.md in the same commit."
        % READER_COMMIT)


def _spec_root():
    env = os.environ.get("CHIPLET_SPEC_ROOT")
    if env and os.path.isdir(env):
        return env
    sibling = os.path.abspath(os.path.join(PLUGIN_ROOT, "..", "..", "..", "chiplet-spec"))
    return sibling if os.path.isdir(os.path.join(sibling, ".git")) else None


def test_the_pinned_commit_really_carries_those_bytes():
    """The pin names a real commit, and that commit's blob is what we vendored.

    Without this the sha256 above only says the copy did not change since
    somebody typed it, which is true of a copy of anything.
    """
    root = _spec_root()
    if root is None:
        pytest.skip("chiplet-spec checkout not found (set CHIPLET_SPEC_ROOT)")
    proc = subprocess.run(
        ["git", "-C", root, "show", "%s:%s" % (READER_COMMIT, READER_UPSTREAM)],
        capture_output=True)
    if proc.returncode != 0:
        pytest.skip("chiplet-spec checkout does not have %s (shallow or stale)"
                    % READER_COMMIT)
    assert _sha256(proc.stdout) == READER_SHA256, (
        "chiplet-spec %s:%s does not hash to the pinned sha256. The pin names "
        "the wrong commit, or the copy came from somewhere else."
        % (READER_COMMIT, READER_UPSTREAM))


def test_vendored_md_declares_the_same_pin():
    """The prose and the gate cannot disagree about what was vendored."""
    with open(os.path.join(PLUGIN_ROOT, "VENDORED.md"), "r", encoding="utf-8") as fh:
        text = fh.read()
    for fact in (READER_UPSTREAM, READER_COMMIT, READER_SHA256):
        assert fact in text, (
            "VENDORED.md does not mention %r. Update it in the same commit as "
            "the re-vendor; a provenance file that lags the pin is worse than "
            "none, because it is believed." % fact)


_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)


def _declared_version(blob):
    match = _VERSION_RE.search(blob.decode("utf-8", "replace"))
    return match.group(1) if match else None


def _previous_and_current_reader():
    """(previous committed bytes that DIFFER, bytes on disk). Skips if neither.

    The most recent COMMITTED state that differs from what is on disk. Walking
    for the first difference rather than taking the previous commit is not
    tidiness: on an uncommitted re-vendor that commit still holds the OLD bytes,
    so a fixed slot compares the new file against the wrong side and passes for
    the wrong reason. It did, here, before this loop existed.
    """
    proc = subprocess.run(
        ["git", "-C", PLUGIN_ROOT, "log", "--format=%H", "--", READER_PATH],
        capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("not a git checkout")
    with open(READER_PATH, "rb") as fh:
        current = fh.read()
    for commit in proc.stdout.split():
        blob = subprocess.run(
            ["git", "-C", PLUGIN_ROOT, "show",
             "%s:%s" % (commit, READER_UPSTREAM_LOCAL)],
            capture_output=True).stdout
        if blob and blob != current:
            return blob, current
    pytest.skip("the vendored reader has no earlier, different version")


def test_a_re_vendor_that_changed_the_bytes_also_moved_the_version():
    """The number has to move when the bytes move, or it stops meaning anything.

    Everything else in this file, and every version check upstream, asks whether
    the sites that DECLARE a version agree with each other. None of them asks
    whether the version MOVED when the bytes did. Those are different questions,
    and the second one is the one a consumer needs: agreement between copies of
    a value says nothing about whether the value is right.

    It bites for real. Upstream shipped two different readers both declaring
    1.2.0, and the ecosystem's own registry calls same-version-different-bytes
    DRIFTED precisely because that is what an undeclared in-place edit looks
    like. So two honest copies, taken from two commits, accuse each other.

    Implemented against THIS repo's history rather than upstream's, because that
    is what a consumer can see: compare the vendored file with the previous
    commit that CHANGED it. Bytes always differ between those two by
    construction, so the check is never vacuous.

    What a green here does NOT cover: whether the bump was the right SIZE
    (patch where a minor was due), and anything about the fixture, which
    declares an oracle version instead and is pinned separately.
    """
    previous, current = _previous_and_current_reader()
    was, now = _declared_version(previous), _declared_version(current)
    assert was is not None and now is not None, (
        "the vendored reader stopped declaring __version__, so nothing "
        "downstream can tell two copies of it apart")
    assert was != now, (
        "the vendored reader changed bytes while still declaring %s. Re-vendor "
        "from an upstream release that moved the number, or the copy is "
        "indistinguishable from a hand edit to every gate that reads it." % now)


def _minor(version):
    parts = (version or "").split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


def _load_reader(blob, name, tmp_path):
    path = tmp_path / (name + ".py")
    path.write_bytes(blob)
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _refusal_frontier(reader, documents):
    """Which documents this reader says no to. The observable it can't fake."""
    frontier = set()
    for name, doc in documents:
        try:
            reader.loads(doc)
        except Exception:                                 # noqa: BLE001
            frontier.add(name)
    return frontier


def test_a_reader_whose_answers_changed_moved_more_than_the_patch(tmp_path):
    """A relaxed refusal is a MINOR, and nothing upstream can see it.

    The companion check upstream pins the digest of what the writer EMITS, so a
    patch that changes output fails. It cannot see a change with no byte
    consequence: a refusal quietly relaxed changes no emitted byte and moves no
    digest, and a patch bump hides it. That half is visible from here, because a
    consumer can run both readers and compare their answers.

    The observable is the refusal frontier over the oracle corpus: which of
    those documents the reader says no to. If the frontier moved, the reader
    answers a question differently than it did, which is a MINOR at least, and
    a patch-only bump is the defect.

    What this does NOT cover, and it is narrow: the corpus is the top-level
    block oracle, so the frontier it sees is the line-break and grammar one. A
    refusal relaxed somewhere else (a vocabulary rule, a version gate) is
    invisible here. That frontier is the one worth pinning in THIS repo anyway:
    it is what SPEC-36 was about, and the previously vendored reader loaded a
    smuggled key straight through it.
    """
    previous, current = _previous_and_current_reader()
    with open(ORACLE_PATH, "r", encoding="utf-8") as fh:
        oracle = json.load(fh)
    documents = [(row["name"], row["doc"])
                 for group in ("splits", "refuse", "not_delimitable")
                 for row in oracle[group]]
    assert documents, "empty corpus; this check would pass by having nothing to ask"

    before = _refusal_frontier(_load_reader(previous, "cfio_previous", tmp_path),
                               documents)
    after = _refusal_frontier(_load_reader(current, "cfio_current", tmp_path),
                              documents)
    complaint = frontier_verdict(before, after,
                                 _declared_version(previous),
                                 _declared_version(current))
    assert complaint is None, complaint


def frontier_verdict(before, after, was_version, now_version):
    """None when the pair is fine, else the complaint. Pure, so it is testable.

    Separated from the reading and the git walk because those two make the
    integration test above VACUOUS whenever the frontier did not move, which is
    most of the time and is the case today. A rule nothing exercises is a rule
    nobody has checked; the unit tests below exercise both ways of being wrong.
    """
    if before == after:
        return None
    was, now = _minor(was_version), _minor(now_version)
    if was is None or now is None:
        return "unreadable version on one side (%s -> %s)" % (was_version, now_version)
    if now > was:
        return None
    return ("the vendored reader answers differently and the version did not "
            "move beyond the patch (%s -> %s). Relaxed: %s. Tightened: %s. A "
            "changed answer is a minor at least; a patch says nothing moved."
            % (was_version, now_version,
               sorted(before - after) or "none", sorted(after - before) or "none"))


def test_the_frontier_rule_catches_both_ways_of_being_wrong():
    """The negative controls, because the check above can pass by having nothing
    to compare. Written after two tests in one day passed against unfixed code.
    """
    moved = ({"a"}, {"a", "b"})
    # Wrong way 1: a relaxed refusal shipped as a patch.
    assert frontier_verdict({"a", "b"}, {"a"}, "1.3.0", "1.3.1") is not None
    # Wrong way 2: answers changed, version stood still.
    assert frontier_verdict(*moved, "1.3.0", "1.3.0") is not None
    # And the two shapes that are fine.
    assert frontier_verdict(*moved, "1.3.0", "1.4.0") is None
    assert frontier_verdict({"a"}, {"a"}, "1.3.0", "1.3.1") is None
    # The complaint has to say WHICH document moved, or it cannot be acted on.
    assert "b" in frontier_verdict({"a", "b"}, {"a"}, "1.3.0", "1.3.1")
