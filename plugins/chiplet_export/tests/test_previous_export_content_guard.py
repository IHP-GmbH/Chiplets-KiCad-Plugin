# SPDX-License-Identifier: GPL-3.0-or-later
"""The previous-export content guard: four named states, and what gets said.

The guard answered with a bare bool, and False meant two unrelated things:
"compared against the recorded baseline and it matches" and "there was no
baseline to compare against". A caller reading False could not tell a verified
file from an unverified one, so the reassuring case and the blind case produced
the same output, which was nothing. The four states name them apart.

What this suite is NOT about: changing what the export does. The bool is a
wrapper over the same evaluation and every state maps to the same decision it
mapped to before, unreadable sidecar included. The proposition under test is
that the guard now SAYS which case it is, and that it cannot go quiet without a
test failing. A guard that stopped reporting would look exactly like a guard
that found nothing, which is the failure this file exists to make impossible.

Also asserted here, because it is the one thing that could silently rot: the
assessment is taken ONCE. Every consumer reads that one object. After the
export rewrites the file, "did this change since the last export" has a
different answer, and reporting that answer as this one describes a state that
no longer exists.
"""

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_export.pipeline import chiplet_merge as cm  # noqa: E402
from chiplet_export.pipeline import orchestrator  # noqa: E402


CANONICAL = (
    'format_version: "1.0"\n'
    'assembly:\n'
    '  name: demo\n'
    'components:\n'
    '  - id: interposer\n'
    '    position:\n'
    '      x: 100.0\n'
    '      y: 50.0\n'
    'flow:\n'
    '  steps: []\n'
)

#: The same document with an OWNED field changed (components.position.x).
OWNED_EDIT = CANONICAL.replace("x: 100.0", "x: 999.0")

#: The same document with only the FOREIGN block changed. flow: is not in
#: EXPORTER_OWNED_TOP_LEVEL_KEYS, so the digest must not see this at all.
FOREIGN_EDIT = CANONICAL.replace("  steps: []", "  steps: [place, route]")

INTERCONNECT = 'interconnect:\n  adapter: "ihp_cupillar"\n'

# Frozen output of exporter_content_digest at e4c3447aa97bec8cf14b094462806cc9473f2f49
# (051dfd8's parent), for BOTH CANONICAL and CANONICAL + INTERCONNECT. The old
# exporter omitted interconnect from the hash. Do not regenerate this baseline
# with today's exporter or install an old hashing algorithm in production.
PRE_051DFD8_DIGEST = (
    "5f04df816e88691f08a0b2b0b5c047fa947cce938f2f2e5550ae65edd4bcf838"
)


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


@pytest.fixture
def baselined(tmp_path):
    """A canonical file with a recorded digest: the guard has something to check."""
    final = tmp_path / "demo.chiplet"
    _write(final, CANONICAL)
    cm.record_exporter_content_digest(str(final))
    return final


# --- the four states ------------------------------------------------------

def test_unchanged_when_the_file_matches_its_recorded_digest(baselined):
    a = cm.assess_previous_export_content(str(baselined))
    assert a.state == cm.GUARD_UNCHANGED
    assert a.verified is True, "a compared file is the only kind that is verified"
    assert a.tripped is False


def test_changed_when_owned_content_was_edited(baselined):
    _write(baselined, OWNED_EDIT)
    a = cm.assess_previous_export_content(str(baselined))
    assert a.state == cm.GUARD_CHANGED
    assert a.tripped is True
    assert a.recorded and a.current and a.recorded != a.current, (
        "the changed state has to carry both digests, or the next reader "
        "cannot tell a real divergence from a bookkeeping slip")


def test_no_baseline_for_a_first_export(tmp_path):
    a = cm.assess_previous_export_content(str(tmp_path / "absent.chiplet"))
    assert a.state == cm.GUARD_NO_BASELINE
    assert a.verified is False
    assert "first export" in a.reason


def test_no_baseline_for_an_existing_file_without_a_digest(tmp_path):
    """The state the old bool hid: the file is REAL and nothing protects it.

    Distinct from a first export in what it means for the user (owned edits to
    this document will be regenerated away without a word) and identical to it
    in what the bool says. Only the reason separates them.
    """
    final = tmp_path / "demo.chiplet"
    _write(final, CANONICAL)
    a = cm.assess_previous_export_content(str(final))
    assert a.state == cm.GUARD_NO_BASELINE
    assert "not protected" in a.reason, (
        "this reason is the whole point of the state: %r" % a.reason)
    assert "first export" not in a.reason, (
        "an existing file is not a first export, and saying so would tell the "
        "user their document is new")


def test_baseline_unreadable_for_a_corrupt_sidecar(baselined):
    with open(cm.digest_sidecar_path(str(baselined)), "wb") as fh:
        fh.write(b"\xff\xfe not a valid utf-8 digest \x83\x28")
    a = cm.assess_previous_export_content(str(baselined))
    assert a.state == cm.GUARD_BASELINE_UNREADABLE
    assert a.verified is False
    assert a.reason and "could not be read" in a.reason


def test_baseline_unreadable_for_an_empty_sidecar(baselined):
    """Empty and corrupt are the same state with different reasons.

    They were one silent False before, and the reason is what the lead's
    four-state shape buys over a three-state one: the state says the baseline
    is unusable, the reason says which way.
    """
    with open(cm.digest_sidecar_path(str(baselined)), "w",
              encoding="utf-8") as fh:
        fh.write("   \n")
    a = cm.assess_previous_export_content(str(baselined))
    assert a.state == cm.GUARD_BASELINE_UNREADABLE
    assert "empty" in a.reason


@pytest.mark.parametrize("state", [cm.GUARD_NO_BASELINE,
                                   cm.GUARD_BASELINE_UNREADABLE])
def test_an_unbaselined_state_says_the_export_is_unprotected(state):
    """The report must not let a warning read as protection.

    An earlier version of this change argued that proceeding without a baseline
    was made safe by reporting it. That is false and the wording carried the
    error: observability does not guard an owned edit and does not establish
    that the content is unchanged. With no usable baseline the export continues
    and regenerates owned content exactly as it would have before the guard
    existed, and the line the user sees has to say that, not merely that the
    guard checked nothing.
    """
    line = cm.ContentGuardAssessment(state, "/x", reason="r").describe()
    assert "WITHOUT content-guard protection" in line, (
        "%s reports the guard's state but not the consequence for the file: %r"
        % (state, line))


def test_every_state_describes_itself_as_the_previous_export_guard():
    """Wording, asserted because it is a claim about scope, not decoration.

    "verification" unqualified would say the exporter checked the document, and
    it did not: it compared the file against the digest of its own last export
    of that file. A user who reads the stronger claim stops looking.
    """
    for state in (cm.GUARD_UNCHANGED, cm.GUARD_CHANGED,
                  cm.GUARD_NO_BASELINE, cm.GUARD_BASELINE_UNREADABLE):
        line = cm.ContentGuardAssessment(state, "/x", reason="r").describe()
        assert "previous-export content guard" in line
        assert "global verification" not in line
        assert "verified" not in line.lower(), (
            "%s claims more than a digest comparison: %r" % (state, line))


# --- the bool stays a wrapper over the SAME evaluation ---------------------

@pytest.mark.parametrize("state,expected", [
    (cm.GUARD_UNCHANGED, False),
    (cm.GUARD_CHANGED, True),
    (cm.GUARD_NO_BASELINE, False),
    (cm.GUARD_BASELINE_UNREADABLE, False),
])
def test_the_compatibility_bool_maps_exactly_one_state_to_true(state, expected):
    """Only `changed` refuses. Pinned so a later state cannot quietly join it.

    baseline_unreadable is False on purpose. The sidecar is the exporter's own
    artifact; refusing an export because OUR bookkeeping broke charges the user
    for it, with a fix they cannot find.

    That is a cost, not a fix. With no usable baseline the export continues
    without content-guard protection and regenerates owned edits exactly as it
    would have before the guard existed. Reporting the state does not protect
    anything; it only makes the unprotected case distinguishable from the
    verified one. Whether it should refuse instead is a separate decision and is
    not settled by this test.
    """
    assert cm.ContentGuardAssessment(state, "/x").tripped is expected


def test_the_bool_is_the_assessment_and_not_a_second_comparison(baselined,
                                                                monkeypatch):
    """One evaluation. The wrapper must not re-read the file to answer.

    A wrapper that recomputed would be a second question asked at a different
    moment, and during an export the file between the two is not the same file.
    """
    calls = []
    real = cm.assess_previous_export_content

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(cm, "assess_previous_export_content", counting)
    assert cm.foreign_hand_edit_detected(str(baselined)) is False
    assert len(calls) == 1, (
        "the bool made %d assessments; it is a view of one, not its own"
        % len(calls))


# --- decisions and reporting, through the REAL orchestrator branch ---------

def _decide(final, force=False):
    """Drive orchestrator.evaluate_content_guard, capturing what it said."""
    lines = []
    refusal, note = orchestrator.evaluate_content_guard(
        str(final), force, lines.append)
    return refusal, note, lines


def test_an_owned_edit_is_refused_and_the_bytes_are_left_alone(baselined):
    """The refusal must not be a write. The user's document is the evidence."""
    _write(baselined, OWNED_EDIT)
    before = baselined.read_bytes()
    refusal, _note, _lines = _decide(baselined)
    assert refusal and "hand edits" in refusal
    assert str(baselined) in refusal
    assert baselined.read_bytes() == before, (
        "the guard rewrote the file it was refusing to overwrite")


def test_a_foreign_only_edit_is_not_refused(baselined):
    """flow: is not owned, so editing it is not a divergence and never was."""
    _write(baselined, FOREIGN_EDIT)
    refusal, note, _lines = _decide(baselined)
    assert refusal is None
    assert "matches the last export" in note


def test_the_guard_reports_on_every_state(baselined, tmp_path):
    """The silence mutant. Delete a notice in evaluate_content_guard and this
    fails, for that state, by name.

    It is worth being explicit about why this is a test and not a review note.
    A guard that stops reporting produces exactly the output of a guard that
    found nothing to report: no line. Nothing else in the suite distinguishes
    them, because every other assertion here is about the decision, and the
    decision is identical in the two cases.
    """
    cases = {
        "unchanged": baselined,
        "no_baseline": tmp_path / "absent.chiplet",
    }
    for label, path in cases.items():
        _refusal, note, lines = _decide(path)
        assert note, "%s produced no manifest note" % label
        assert lines, "%s logged nothing" % label
        assert any("previous-export content guard" in line for line in lines), (
            "%s logged something that does not name the guard: %r"
            % (label, lines))


def test_a_forced_run_over_a_changed_file_says_override_not_unchanged(baselined):
    """The case the force path used to swallow whole.

    Force skips the refusal, which is what it is for. It used to skip the
    evaluation too, so the one run that regenerated a hand edit away reported
    the same nothing as a run over an untouched file. The report must name the
    override, and must never read as if the content matched.
    """
    _write(baselined, OWNED_EDIT)
    refusal, note, lines = _decide(baselined, force=True)
    assert refusal is None, "force must still not refuse"
    assert "OVERRIDE" in note
    assert "DIGEST MISMATCH" in note
    assert "matches the last export" not in note, (
        "a forced run over a changed file reported the unchanged wording: %r"
        % note)
    assert any("OVERRIDE" in line for line in lines)


def test_a_forced_run_over_an_unchanged_file_does_not_claim_an_override(
        baselined):
    """The positive control for the test above.

    Without it, "always print OVERRIDE under force" passes that test and tells
    the user their untouched file was overridden.
    """
    _refusal, note, _lines = _decide(baselined, force=True)
    assert "OVERRIDE" not in note
    assert "matches the last export" in note
    assert "force is set" in note


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("with_interconnect", [False, True])
@pytest.mark.parametrize("foreign_edit", [False, True])
def test_old_export_baseline_explains_only_the_applicable_mismatch(
        tmp_path, monkeypatch, force, with_interconnect, foreign_edit):
    final = tmp_path / "old.chiplet"
    document = FOREIGN_EDIT if foreign_edit else CANONICAL
    _write(final, document + (INTERCONNECT if with_interconnect else ""))
    sidecar = Path(cm.digest_sidecar_path(str(final)))
    sidecar.write_text(PRE_051DFD8_DIGEST + "\n", encoding="utf-8")
    before = final.read_bytes(), sidecar.read_bytes()

    reads = []
    real_read = cm.read_document

    def read_once(path):
        reads.append(path)
        assert len(reads) == 1, "the legacy hint must use the already read file"
        return real_read(path)

    monkeypatch.setattr(cm, "read_document", read_once)
    refusal, note, lines = _decide(final, force=force)
    assert len(reads) == 1
    assert bool(refusal) == (with_interconnect and not force)
    assert ("051dfd8" in note) == with_interconnect
    assert ("DIGEST MISMATCH" in note) == with_interconnect
    assert ("OVERRIDE" in note) == (with_interconnect and force)
    assert note in "\n".join(lines)
    if with_interconnect:
        assert "does not prove hand edits" in note
        assert "did not hash interconnect:" in note
        assert "even if this file was not edited" in note
        if force:
            assert "WILL BE regenerated" in note
            orchestrator._write_outputs_manifest(str(tmp_path), "old", note)
            assert note in (tmp_path / "MANIFEST.md").read_text(encoding="utf-8")
        else:
            assert note in refusal
            assert "Back up and review" in refusal
            assert "force=True only if" in refusal
    else:
        assert "matches the last export" in note
    assert (final.read_bytes(), sidecar.read_bytes()) == before


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("original,edited,legacy_hint", [
    (CANONICAL, OWNED_EDIT, False),
    (CANONICAL + INTERCONNECT, OWNED_EDIT + INTERCONNECT, True),
    (CANONICAL + INTERCONNECT,
     CANONICAL + INTERCONNECT.replace("ihp_cupillar", "vendorx_microbump"), True),
    # A textual mention in a foreign block is not a top-level interconnect key.
    (CANONICAL + '  note: "interconnect: description"\n',
     OWNED_EDIT + '  note: "interconnect: description"\n', False),
])
def test_real_edits_keep_the_same_decision_without_claiming_a_cause(
        tmp_path, force, original, edited, legacy_hint):
    final = tmp_path / "edited.chiplet"
    _write(final, original)
    cm.record_exporter_content_digest(str(final))
    _write(final, edited)
    refusal, note, _lines = _decide(final, force=force)
    assert bool(refusal) == (not force)
    assert "DIGEST MISMATCH" in note
    assert "does not prove hand edits" in note
    assert ("051dfd8" in note) == legacy_hint
    assert ("OVERRIDE" in note) == force


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("baseline", ["current", "missing", "empty", "unreadable"])
def test_interconnect_without_a_mismatch_does_not_get_the_legacy_hint(
        tmp_path, force, baseline):
    final = tmp_path / "current.chiplet"
    _write(final, CANONICAL + INTERCONNECT)
    cm.record_exporter_content_digest(str(final))
    sidecar = Path(cm.digest_sidecar_path(str(final)))
    if baseline == "missing":
        sidecar.unlink()
    elif baseline == "empty":
        sidecar.write_bytes(b"\n")
    elif baseline == "unreadable":
        sidecar.write_bytes(b"\xff\xfe")
    refusal, note, lines = _decide(final, force=force)
    assert refusal is None
    assert "051dfd8" not in note
    assert "DIGEST MISMATCH" not in note
    assert "OVERRIDE" not in note
    if baseline == "current":
        assert "matches the last export" in note
    else:
        assert "WITHOUT content-guard protection" in note
    assert note in "\n".join(lines)


def test_a_corrupt_canonical_still_fails_closed_and_is_not_called_no_baseline(
        baselined):
    """Corrupt DOCUMENT and unreadable SIDECAR are different failures.

    The document is the user's and an unreadable one must abort; the sidecar is
    ours and an unreadable one must not. Collapsing them either way is a defect:
    one direction blocks exports on our own bookkeeping, the other overwrites a
    document nobody could read.
    """
    with open(baselined, "wb") as fh:
        fh.write(b"\xff\xfe\x00 not valid utf-8 \x83\x28")
    refusal, note, _lines = _decide(baselined)
    assert refusal and "force=True" in refusal
    assert "no baseline" not in refusal.lower(), (
        "an unreadable document was reported as a missing baseline: %r"
        % refusal)
    assert note == "", "a refused export must not leave a reassuring note"


def test_the_manifest_carries_the_note_and_omits_the_section_without_one(
        tmp_path):
    """No note, no section. An empty note means no .chiplet was emitted, and a
    manifest that printed a guard line anyway would describe a check that never
    ran."""
    out = tmp_path / "out"
    out.mkdir()
    orchestrator._write_outputs_manifest(
        str(out), "demo", "previous-export content guard: something happened")
    text = (out / "MANIFEST.md").read_text(encoding="utf-8")
    assert "Previous-export content guard" in text
    assert "something happened" in text

    orchestrator._write_outputs_manifest(str(out), "demo", "")
    text = (out / "MANIFEST.md").read_text(encoding="utf-8")
    assert "Previous-export content guard" not in text
