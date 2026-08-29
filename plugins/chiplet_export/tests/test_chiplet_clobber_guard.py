# SPDX-License-Identifier: GPL-3.0-or-later
"""H-A clobber guard: prove-it-can-fail tests for pipeline/chiplet_merge.py.

The defect (reproducible today): the orchestrator stages the freshly regenerated
intermediate .chiplet over the canonical file with a bare ``shutil.copy2``. The
intermediate is board-derived and carries no ``flow:``/``netlist:`` block, so a
pasted ``flow:`` block (required, because Chiplet Studio's FlowEngine reads it
only from the EMBEDDED block) is silently destroyed on every KiCad re-export.

These tests exercise the exact copy2 door in isolation (stdlib + PyYAML, no
pcbnew), so they run on any host Python:

* ``test_defect_reproduced_*`` is the answer key: the bare copy2, WITHOUT the
  guard, loses the flow: block. It documents that the scenario genuinely fails.
* ``test_guard_preserves_*`` is the fix: the guard carries the foreign blocks
  over before the copy, and they survive the finalizer's load-modify-dump round
  trip, still EMBEDDED.

Plus the digest tripwire: a hand-edited position is detected, a foreign-only
edit (pasting flow:) is not, and a first export is silent.
"""

import shutil
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_export.pipeline import chiplet_merge as cm  # noqa: E402


# --- fixtures -------------------------------------------------------------

FLOW_BLOCK = {"steps": ["route", "drc"], "engine": "interposer-pnr"}
NETLIST_BLOCK = {"nets": [{"name": "vdd", "pins": ["die_a.p1"]}]}


def _base_doc(interposer_pos, die_pos):
    """A minimal, canonical (finalized) .chiplet as a dict."""
    return {
        "format_version": "1.0",
        "assembly": {"name": "demo", "units": "um"},
        "technologies": {"intm4tm2": {"lyp": "intm4tm2.lyp"}},
        "components": [
            {"id": "interposer", "type": "interposer",
             "anchor": "bbox_center", "position": list(interposer_pos)},
            {"id": "die_a", "type": "die",
             "anchor": "gds_origin", "position": list(die_pos)},
        ],
    }


def _write(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _write_canonical_final(path, *, flow=True, netlist=True,
                           die_pos=(100.0, 50.0, 0.0)):
    """The canonical .chiplet a user has been hosting a flow: block in."""
    doc = _base_doc((0.0, 0.0, 0.0), die_pos)
    if flow:
        doc["flow"] = dict(FLOW_BLOCK)
    if netlist:
        doc["netlist"] = dict(NETLIST_BLOCK)
    _write(path, doc)


def _write_fresh_intermediate(path, *, die_pos=(100.0, 50.0, 0.0)):
    """What write_chiplet produces: board-derived, finalize_required, no flow:."""
    doc = _base_doc((0.0, 0.0, 0.0), die_pos)
    # exporter emits keys in a fixed order; put _metadata first as the writer does
    doc = {"format_version": doc["format_version"],
           "_metadata": {"finalize_required": True},
           **{k: v for k, v in doc.items() if k != "format_version"}}
    _write(path, doc)


def _fake_finalize(path):
    """Mimic hyp_to_gds update_chiplet_file: load, pop _metadata, dump whole."""
    data = _read(path)
    data.pop("_metadata", None)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)


# --- the defect (answer key): bare copy2 clobbers ------------------------

def test_defect_reproduced_bare_copy2_loses_flow_block(tmp_path):
    final = tmp_path / "demo.chiplet"
    inter = tmp_path / "demo.intermediate.chiplet"
    _write_canonical_final(final)
    _write_fresh_intermediate(inter)
    assert "flow" in _read(final)

    # The unguarded door, verbatim (orchestrator.py emit_chiplet branch).
    shutil.copy2(inter, final)
    _fake_finalize(final)

    got = _read(final)
    assert "flow" not in got, "expected the bare copy2 to destroy flow: (defect)"
    assert "netlist" not in got


# --- the fix: guard preserves foreign blocks, still embedded -------------

def test_guard_preserves_flow_block_through_copy2_and_finalize(tmp_path):
    final = tmp_path / "demo.chiplet"
    inter = tmp_path / "demo.intermediate.chiplet"
    _write_canonical_final(final)
    _write_fresh_intermediate(inter)

    carried = cm.carry_over_foreign_blocks(str(final), str(inter))
    assert set(carried) == {"flow", "netlist"}

    shutil.copy2(inter, final)
    _fake_finalize(final)

    got = _read(final)
    # flow: survives AND stays a top-level EMBEDDED block (FlowEngine reads it
    # only from the embedded block), byte-for-value identical to the paste.
    assert got.get("flow") == FLOW_BLOCK
    assert got.get("netlist") == NETLIST_BLOCK
    assert "_metadata" not in got  # finalize stripped it, file is canonical


def test_carry_over_no_foreign_blocks_is_noop(tmp_path):
    final = tmp_path / "demo.chiplet"
    inter = tmp_path / "demo.intermediate.chiplet"
    _write_canonical_final(final, flow=False, netlist=False)
    _write_fresh_intermediate(inter)
    assert cm.carry_over_foreign_blocks(str(final), str(inter)) == []


def test_carry_over_first_export_no_existing_final(tmp_path):
    inter = tmp_path / "demo.intermediate.chiplet"
    _write_fresh_intermediate(inter)
    assert cm.carry_over_foreign_blocks(str(tmp_path / "absent.chiplet"),
                                        str(inter)) == []


def test_carry_over_leaves_exporter_owned_to_the_board(tmp_path):
    # The board (intermediate) is the source of truth for positions: an old
    # position in the canonical file must NOT override the fresh one.
    final = tmp_path / "demo.chiplet"
    inter = tmp_path / "demo.intermediate.chiplet"
    _write_canonical_final(final, die_pos=(999.0, 999.0, 0.0))
    _write_fresh_intermediate(inter, die_pos=(100.0, 50.0, 0.0))

    cm.carry_over_foreign_blocks(str(final), str(inter))
    staged = _read(inter)
    die = next(c for c in staged["components"] if c["id"] == "die_a")
    assert die["position"] == [100.0, 50.0, 0.0]


# --- the digest tripwire --------------------------------------------------

def test_tripwire_silent_on_first_export(tmp_path):
    final = tmp_path / "demo.chiplet"
    _write_canonical_final(final)
    # no sidecar yet
    assert cm.foreign_hand_edit_detected(str(final)) is False


def test_tripwire_detects_handedited_position(tmp_path):
    final = tmp_path / "demo.chiplet"
    _write_canonical_final(final)
    cm.record_exporter_content_digest(str(final))

    # user hand-edits a position in the .chiplet outside KiCad
    doc = _read(final)
    doc["components"][1]["position"] = [123.0, 456.0, 0.0]
    _write(final, doc)

    assert cm.foreign_hand_edit_detected(str(final)) is True


def test_tripwire_ignores_foreign_only_edit(tmp_path):
    final = tmp_path / "demo.chiplet"
    _write_canonical_final(final, flow=False, netlist=False)
    cm.record_exporter_content_digest(str(final))

    # user pastes a flow: block (the supported hosting workflow) -- no position
    # change: the tripwire must stay silent so the carry-over path is not blocked.
    doc = _read(final)
    doc["flow"] = dict(FLOW_BLOCK)
    _write(final, doc)

    assert cm.foreign_hand_edit_detected(str(final)) is False


def test_digest_stable_across_reformatting(tmp_path):
    final = tmp_path / "demo.chiplet"
    _write_canonical_final(final)
    d1 = cm.exporter_content_digest(str(final))

    # reserialize with different key order / flow style: same exporter content
    doc = _read(final)
    reordered = {k: doc[k] for k in reversed(list(doc))}
    _write(final, reordered)
    d2 = cm.exporter_content_digest(str(final))
    assert d1 == d2
