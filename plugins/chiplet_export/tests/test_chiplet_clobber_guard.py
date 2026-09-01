# SPDX-License-Identifier: GPL-3.0-or-later
"""H-A clobber guard: prove-it-can-fail tests for pipeline/chiplet_merge.py.

The defect (reproducible today): the orchestrator stages the freshly regenerated
intermediate .chiplet over the canonical file with a bare ``shutil.copy2``. The
intermediate is board-derived and carries no ``flow:``/``netlist:`` block, so a
pasted ``flow:`` block (required, because Chiplet Studio's FlowEngine reads it
only from the EMBEDDED block) is silently destroyed on every KiCad re-export.

The guard is PURE STDLIB (no PyYAML, no chiplet_format_io): it runs in KiCad's
bundled Python, which has neither (discovery.py:4-7). It works on raw top-level
block TEXT, so these tests build fixtures as plain ``.chiplet`` text and never
import ``yaml`` -- except the single end-to-end test that deliberately drives a
finalizer round-trip through ``yaml`` (guarded by a local importorskip so it
skips, not fails, on a runner without PyYAML).

Coverage:
* the bare-copy2 defect (answer key) and the guard that fixes it;
* carry-over: no-foreign no-op, first-export no-existing, owned-content left to
  the board, and VERBATIM comment-preserving carry;
* the digest tripwire: silent first export, a hand-edited owned field trips, a
  foreign-only edit does not, a formatting-only OWNED edit trips (accepted
  delta), trailing-whitespace churn does not;
* the secondary fix: a corrupt/undecodable canonical makes the detector RAISE
  (so the orchestrator can fail closed), and force bypasses it.
"""

import shutil
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_export.pipeline import chiplet_merge as cm  # noqa: E402


# --- text fixtures (no yaml) ----------------------------------------------

_OWNED_HEAD = (
    'format_version: "1.0"\n'
    '\n'
    'assembly:\n'
    '  name: demo\n'
    '  units: um\n'
    '\n'
    'technologies:\n'
    '  intm4tm2:\n'
    '    layer_properties: intm4tm2.lyp\n'
)


def _components_block(die_pos=(100.0, 50.0, 0.0)):
    x, y, z = die_pos
    return (
        'components:\n'
        '  - id: interposer\n'
        '    type: interposer\n'
        '    anchor: bbox_center\n'
        '    position:\n'
        '      x: 0.0\n'
        '      y: 0.0\n'
        '      z: 0.0\n'
        '  - id: die_a\n'
        '    type: die\n'
        '    anchor: gds_origin\n'
        '    position:\n'
        '      x: %.6f\n'
        '      y: %.6f\n'
        '      z: %.6f\n'
    ) % (x, y, z)


FLOW_BLOCK = (
    'flow:\n'
    '  engine: interposer-pnr\n'
    '  steps:\n'
    '    - route\n'
    '    - drc\n'
)

FLOW_BLOCK_WITH_COMMENT = (
    'flow:\n'
    '  # hand-authored: keep my ordering, do not touch\n'
    '  engine: interposer-pnr\n'
    '  steps:\n'
    '    - route   # critical net first\n'
    '    - drc\n'
)

NETLIST_BLOCK = (
    'netlist:\n'
    '  nets:\n'
    '    - name: vdd\n'
    '      pins:\n'
    '        - die_a.p1\n'
)


def _canonical_final(*, flow=True, netlist=True, die_pos=(100.0, 50.0, 0.0),
                     flow_block=FLOW_BLOCK):
    """A finalized (canonical) .chiplet as text: owned blocks + optional foreign."""
    text = _OWNED_HEAD + '\n' + _components_block(die_pos)
    if flow:
        text += '\n' + flow_block
    if netlist:
        text += '\n' + NETLIST_BLOCK
    return text


def _fresh_intermediate(die_pos=(100.0, 50.0, 0.0)):
    """What write_chiplet produces: board-derived, finalize_required, no flow:."""
    return (
        'format_version: "1.0"\n'
        '\n'
        '_metadata:\n'
        '  frame: pcb-bbox-corner\n'
        '  finalize_required: true\n'
        '\n'
        'assembly:\n'
        '  name: demo\n'
        '  units: um\n'
        '\n'
        'technologies:\n'
        '  intm4tm2:\n'
        '    layer_properties: intm4tm2.lyp\n'
        '\n'
        + _components_block(die_pos)
    )


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# --- the defect (answer key): bare copy2 clobbers -------------------------

def test_defect_reproduced_bare_copy2_loses_flow_block(tmp_path):
    final = tmp_path / "demo.chiplet"
    inter = tmp_path / "demo.intermediate.chiplet"
    _write(final, _canonical_final())
    _write(inter, _fresh_intermediate())
    assert "flow" in cm.split_top_level_blocks(_read(final))

    # The unguarded door, verbatim (orchestrator.py emit_chiplet branch).
    shutil.copy2(inter, final)

    blocks = cm.split_top_level_blocks(_read(final))
    assert "flow" not in blocks, "expected the bare copy2 to destroy flow: (defect)"
    assert "netlist" not in blocks


# --- the fix: guard preserves foreign blocks ------------------------------

def test_guard_carries_flow_and_netlist_before_copy2(tmp_path):
    final = tmp_path / "demo.chiplet"
    inter = tmp_path / "demo.intermediate.chiplet"
    _write(final, _canonical_final())
    _write(inter, _fresh_intermediate())

    carried = cm.carry_over_foreign_blocks(str(final), str(inter))
    assert set(carried) == {"flow", "netlist"}

    shutil.copy2(inter, final)
    blocks = cm.split_top_level_blocks(_read(final))
    # flow: survives AND stays a top-level EMBEDDED block.
    assert "flow" in blocks
    assert "netlist" in blocks
    assert "engine: interposer-pnr" in blocks["flow"]


def test_guard_preserves_flow_block_through_finalize_roundtrip(tmp_path):
    # The ONE test that exercises the real finalizer (yaml load-modify-dump).
    yaml = pytest.importorskip("yaml")
    final = tmp_path / "demo.chiplet"
    inter = tmp_path / "demo.intermediate.chiplet"
    _write(final, _canonical_final())
    _write(inter, _fresh_intermediate())

    cm.carry_over_foreign_blocks(str(final), str(inter))
    shutil.copy2(inter, final)

    # hyp_to_gds --update-chiplet-file: load, drop _metadata, dump the whole doc.
    data = yaml.safe_load(_read(final))
    data.pop("_metadata", None)
    with open(final, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)

    got = yaml.safe_load(_read(final))
    assert got.get("flow") == {"engine": "interposer-pnr",
                               "steps": ["route", "drc"]}
    assert got.get("netlist") == {"nets": [{"name": "vdd",
                                            "pins": ["die_a.p1"]}]}
    assert "_metadata" not in got  # finalize stripped it, file is canonical


def test_carry_over_no_foreign_blocks_is_noop(tmp_path):
    final = tmp_path / "demo.chiplet"
    inter = tmp_path / "demo.intermediate.chiplet"
    _write(final, _canonical_final(flow=False, netlist=False))
    before = _read(inter) if inter.exists() else None
    _write(inter, _fresh_intermediate())
    before = _read(inter)
    assert cm.carry_over_foreign_blocks(str(final), str(inter)) == []
    assert _read(inter) == before  # staged file untouched


def test_carry_over_first_export_no_existing_final(tmp_path):
    inter = tmp_path / "demo.intermediate.chiplet"
    _write(inter, _fresh_intermediate())
    assert cm.carry_over_foreign_blocks(str(tmp_path / "absent.chiplet"),
                                        str(inter)) == []


def test_carry_over_leaves_exporter_owned_to_the_board(tmp_path):
    # The board (intermediate) is the source of truth for positions: an old
    # position in the canonical file must NOT override the fresh one.
    final = tmp_path / "demo.chiplet"
    inter = tmp_path / "demo.intermediate.chiplet"
    _write(final, _canonical_final(die_pos=(999.0, 999.0, 0.0)))
    _write(inter, _fresh_intermediate(die_pos=(100.0, 50.0, 0.0)))

    cm.carry_over_foreign_blocks(str(final), str(inter))
    staged = cm.split_top_level_blocks(_read(inter))
    assert "100.000000" in staged["components"]
    assert "999.000000" not in staged["components"]


def test_carry_over_preserves_comments_verbatim(tmp_path):
    # A foreign block's comments/formatting survive byte-for-byte through the
    # carry (until the finalizer normalizes them -- accepted delta).
    final = tmp_path / "demo.chiplet"
    inter = tmp_path / "demo.intermediate.chiplet"
    _write(final, _canonical_final(flow=True, netlist=False,
                                   flow_block=FLOW_BLOCK_WITH_COMMENT))
    _write(inter, _fresh_intermediate())

    carried = cm.carry_over_foreign_blocks(str(final), str(inter))
    assert carried == ["flow"]

    staged_text = _read(inter)
    # the whole foreign block, comments included, appears verbatim
    assert FLOW_BLOCK_WITH_COMMENT.rstrip("\n") in staged_text
    assert "# hand-authored: keep my ordering, do not touch" in staged_text
    assert "- route   # critical net first" in staged_text


# --- the digest tripwire --------------------------------------------------

def test_tripwire_silent_on_first_export(tmp_path):
    final = tmp_path / "demo.chiplet"
    _write(final, _canonical_final())
    # no sidecar yet
    assert cm.foreign_hand_edit_detected(str(final)) is False


def test_tripwire_detects_handedited_position(tmp_path):
    final = tmp_path / "demo.chiplet"
    _write(final, _canonical_final())
    cm.record_exporter_content_digest(str(final))

    # user hand-edits a die position in the .chiplet outside KiCad
    _write(final, _canonical_final(die_pos=(123.0, 456.0, 0.0)))
    assert cm.foreign_hand_edit_detected(str(final)) is True


def test_tripwire_ignores_foreign_only_edit(tmp_path):
    final = tmp_path / "demo.chiplet"
    _write(final, _canonical_final(flow=False, netlist=False))
    cm.record_exporter_content_digest(str(final))

    # user pastes a flow: block (the supported hosting workflow) -- no owned
    # change: the tripwire must stay silent so carry-over is not blocked.
    _write(final, _canonical_final(flow=True, netlist=False))
    assert cm.foreign_hand_edit_detected(str(final)) is False


def test_tripwire_ignores_adding_and_editing_foreign_block(tmp_path):
    final = tmp_path / "demo.chiplet"
    _write(final, _canonical_final(flow=True, netlist=False))
    cm.record_exporter_content_digest(str(final))

    # edit the existing foreign flow block (reorder / add a comment): owned
    # content is unchanged, so no trip.
    _write(final, _canonical_final(flow=True, netlist=False,
                                   flow_block=FLOW_BLOCK_WITH_COMMENT))
    assert cm.foreign_hand_edit_detected(str(final)) is False


def test_tripwire_ignores_foreign_block_with_leading_comment(tmp_path):
    # A column-0 ``#`` comment pasted directly above a foreign block attaches to
    # the last owned block textually (it is not a key line). The owned digest
    # must ignore such trailing comment lines, so the supported hosting workflow
    # (paste flow:, optionally with a lead-in comment) never trips the wire.
    final = tmp_path / "demo.chiplet"
    _write(final, _canonical_final(flow=False, netlist=False))
    cm.record_exporter_content_digest(str(final))

    pasted = (_canonical_final(flow=False, netlist=False)
              + "\n# pinned by hand: do not reorder\n"
              + FLOW_BLOCK)
    _write(final, pasted)
    assert cm.foreign_hand_edit_detected(str(final)) is False


def test_tripwire_trips_on_formatting_only_owned_edit(tmp_path):
    # Accepted delta: a formatting-only edit inside an OWNED block trips the wire.
    final = tmp_path / "demo.chiplet"
    _write(final, _canonical_final())
    cm.record_exporter_content_digest(str(final))

    # reflow the OWNED assembly block to inline flow style (same meaning, new text)
    reflowed = _read(final).replace(
        'assembly:\n  name: demo\n  units: um\n',
        'assembly: {name: demo, units: um}\n')
    assert reflowed != _read(final)
    _write(final, reflowed)
    assert cm.foreign_hand_edit_detected(str(final)) is True


def test_digest_stable_across_trailing_whitespace(tmp_path):
    final = tmp_path / "demo.chiplet"
    _write(final, _canonical_final())
    d1 = cm.exporter_content_digest(str(final))

    # add trailing spaces on owned lines and extra blank lines at EOF: pure
    # end-of-line whitespace churn must NOT change the digest.
    churned = "\n".join(ln + "   " for ln in _read(final).splitlines()) + "\n\n\n"
    _write(final, churned)
    d2 = cm.exporter_content_digest(str(final))
    assert d1 == d2


def test_digest_stable_across_identical_bytes(tmp_path):
    a = tmp_path / "a.chiplet"
    b = tmp_path / "b.chiplet"
    _write(a, _canonical_final())
    _write(b, _canonical_final())
    assert cm.exporter_content_digest(str(a)) == cm.exporter_content_digest(str(b))


# --- the secondary fix: fail closed on a corrupt canonical ----------------

def test_detector_raises_on_undecodable_canonical(tmp_path):
    # With a recorded baseline, an undecodable canonical makes the detector
    # RAISE (not return), so the orchestrator can fail closed instead of
    # crashing the export thread.
    final = tmp_path / "demo.chiplet"
    _write(final, _canonical_final())
    cm.record_exporter_content_digest(str(final))

    with open(final, "wb") as fh:
        fh.write(b"\xff\xfe\x00 not valid utf-8 \x83\x28")

    with pytest.raises(Exception):
        cm.foreign_hand_edit_detected(str(final))


def test_corrupt_sidecar_does_not_trip(tmp_path):
    # The sidecar is our own best-effort artifact. A corrupt (non-utf-8) one
    # means the baseline is lost, so the detector behaves like "no baseline"
    # (returns False) rather than raising and failing an export closed on a file
    # the user never authored.
    final = tmp_path / "demo.chiplet"
    _write(final, _canonical_final())
    with open(cm.digest_sidecar_path(str(final)), "wb") as fh:
        fh.write(b"\xff\xfe not a valid utf-8 digest \x83\x28")
    assert cm.foreign_hand_edit_detected(str(final)) is False


# --- the secondary fix: orchestrator decision branch ----------------------
#
# run_export needs a board (pcbnew), so its emit_chiplet guard cannot be reached
# on host Python. These tests exercise the REAL tripwire + REAL ExportResult
# through a faithful mirror of orchestrator.py's branch (kept in lockstep with
# the `if options.emit_chiplet:` block). They prove force bypasses BOTH a tripped
# wire AND a corrupt canonical, and that a corrupt canonical fails CLOSED.

def _emit_chiplet_guard_decision(chiplet_final, force):
    """Mirror of orchestrator.run_export's pre-copy guard (emit_chiplet branch).

    Returns an ExportResult on abort, or None to proceed. Uses the real
    chiplet_merge detector and the real ExportResult so a drift in either shows
    up here.
    """
    from chiplet_export.pipeline.orchestrator import ExportResult
    if not force:
        try:
            tripped = cm.foreign_hand_edit_detected(chiplet_final)
        except Exception as exc:
            return ExportResult(error=(
                "Could not verify the canonical .chiplet against the last "
                "export (%s); refusing to overwrite it. Fix or remove the "
                "file, or re-run with force=True.\n  File: %s"
                % (exc, chiplet_final)))
        if tripped:
            return ExportResult(error=(
                "The canonical .chiplet has hand edits to exporter-owned "
                "content ...\n  File: %s" % chiplet_final))
    return None


def _corrupt_canonical_with_baseline(tmp_path):
    final = tmp_path / "demo.chiplet"
    _write(final, _canonical_final())
    cm.record_exporter_content_digest(str(final))
    with open(final, "wb") as fh:
        fh.write(b"\xff\xfe\x00 not valid utf-8 \x83\x28")
    return final


def test_orchestrator_branch_fails_closed_on_corrupt_canonical(tmp_path):
    final = _corrupt_canonical_with_baseline(tmp_path)
    result = _emit_chiplet_guard_decision(str(final), force=False)
    assert result is not None
    assert result.error
    assert "force=True" in result.error
    assert str(final) in result.error


def test_orchestrator_branch_force_bypasses_corrupt_canonical(tmp_path):
    final = _corrupt_canonical_with_baseline(tmp_path)
    # force must never call the detector, so the corrupt file cannot abort.
    assert _emit_chiplet_guard_decision(str(final), force=True) is None


def test_orchestrator_branch_force_bypasses_tripped_wire(tmp_path):
    final = tmp_path / "demo.chiplet"
    _write(final, _canonical_final())
    cm.record_exporter_content_digest(str(final))
    _write(final, _canonical_final(die_pos=(1.0, 2.0, 3.0)))  # owned hand-edit
    assert cm.foreign_hand_edit_detected(str(final)) is True   # would trip
    assert _emit_chiplet_guard_decision(str(final), force=True) is None
