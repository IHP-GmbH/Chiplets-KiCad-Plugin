#!/usr/bin/env python3
"""
check_complete_gds_alignment.py - KLayout-independent geometric check
for the cu-pillar / die alignment in *_complete.gds files.

Implements section 7.3 of chiplet-studio/docs/coord_frame_contract.md.
The script reads the complete GDS produced by hyp_to_gds.py end-to-end
and asserts the die's flipped-instance bbox overlaps the cu-pillar
group bbox with a tight tolerance on the X centroid. The Y centroid
delta is reported as diagnostic (the pad cluster in a real die is not
guaranteed to be centered on the die bbox; the canonical alignment
invariant is containment + X-centroid).

This script is intentionally independent of chiplet-studio so a
chiplet-studio bug cannot mask a real GDS misalignment.

Usage:
    python3 check_complete_gds_alignment.py <complete.gds>
                                            [--die U1]
                                            [--centroid-tolerance-um 1.0]

Exit codes:
    0 - alignment OK
    1 - alignment failed (clear diagnostic on stderr)
    2 - file or expected cell not found
"""

import argparse
import re
import sys
from typing import Optional, Tuple

import klayout.db as db


def find_die_flipped_cell(layout: db.Layout, die_ref: str) -> Optional[db.Cell]:
    """Locate the flipped die cell for the given device reference.

    hyp_to_gds.py::_place_die_flipped names the wrapper
    f"{device.ref}_{template_cell_name}_flipped". We scan layout cells
    for the prefix+suffix pattern so the template cell name does not
    have to be hard-coded.
    """
    pattern = re.compile(rf"^{re.escape(die_ref)}_.*_flipped$")
    candidates = [c for c in layout.each_cell() if pattern.match(c.name)]
    if len(candidates) == 0:
        return None
    if len(candidates) > 1:
        names = [c.name for c in candidates]
        print(f"WARNING: multiple flipped cells match {die_ref!r}: {names}; "
              f"using {candidates[0].name}", file=sys.stderr)
    return candidates[0]


def find_cupillars_cell(layout: db.Layout, die_ref: str) -> Optional[db.Cell]:
    """Locate the cu-pillar group cell. hyp_to_gds.py::add_cupillar_pads
    names it f"CUPILLARS_{device_ref}".
    """
    name = f"CUPILLARS_{die_ref}"
    if layout.has_cell(name):
        return layout.cell(name)
    return None


def find_top_instance_bbox_um(top: db.Cell, child: db.Cell,
                               dbu: float) -> Optional[db.DBox]:
    """Return the bbox of `child` in TOP coordinates (in um), looking
    one level deep for a wrapper cell.

    hyp_to_gds.py finalization sometimes wraps the cu-pillar group in
    an intermediate cell (e.g. `TOP$1`) when KLayout resolves a name
    collision during merge. Looking only at TOP's direct children
    would miss it, so we also walk one level deeper. Two levels is
    sufficient for the current pipeline.
    """
    target_index = child.cell_index()
    # Direct child of TOP
    for inst in top.each_inst():
        if inst.cell.cell_index() == target_index:
            return inst.bbox().to_dtype(dbu)
    # One level deeper via a wrapper cell
    for wrapper_inst in top.each_inst():
        wrapper = wrapper_inst.cell
        for inst in wrapper.each_inst():
            if inst.cell.cell_index() == target_index:
                bbox_in_wrapper = inst.bbox()
                bbox_in_top = wrapper_inst.trans * bbox_in_wrapper
                return bbox_in_top.to_dtype(dbu)
    return None


def centroid_um(box: db.DBox) -> Tuple[float, float]:
    return (0.5 * (box.left + box.right), 0.5 * (box.bottom + box.top))


def contains_with_slack(outer: db.DBox, inner: db.DBox,
                         slack_um: float) -> bool:
    """Return True iff `inner` is contained within `outer` allowing for
    a `slack_um` margin (the cu-pillar SnAg cap can extend slightly
    past the TopMetal2 pad)."""
    return (inner.left   >= outer.left   - slack_um and
            inner.right  <= outer.right  + slack_um and
            inner.bottom >= outer.bottom - slack_um and
            inner.top    <= outer.top    + slack_um)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Geometric alignment check for *_complete.gds.")
    parser.add_argument("gds_path", help="Path to *_complete.gds file.")
    parser.add_argument("--die", default="U1",
                        help="Die reference (default: U1).")
    parser.add_argument("--centroid-tolerance-um", type=float, default=1.0,
                        help="Tolerance for X centroid alignment "
                             "(default: 1.0 um per contract).")
    parser.add_argument("--containment-slack-um", type=float, default=30.0,
                        help="Allow cu-pillar bbox to extend past die bbox by "
                             "this slack (default: 30 um, accommodates SnAg "
                             "cap overhang).")
    args = parser.parse_args()

    layout = db.Layout()
    try:
        layout.read(args.gds_path)
    except Exception as exc:
        print(f"ERROR: failed to read GDS: {exc}", file=sys.stderr)
        return 2

    print(f"Layout dbu = {layout.dbu} um/dbu")

    if not layout.has_cell("INTERPOSER"):
        print(f"ERROR: INTERPOSER cell not found in {args.gds_path}",
              file=sys.stderr)
        return 2
    top = layout.cell("INTERPOSER")

    flipped = find_die_flipped_cell(layout, args.die)
    if flipped is None:
        print(f"ERROR: no flipped cell matching {args.die}_*_flipped in "
              f"{args.gds_path}", file=sys.stderr)
        return 2

    cupillars = find_cupillars_cell(layout, args.die)
    if cupillars is None:
        print(f"ERROR: CUPILLARS_{args.die} cell not found in "
              f"{args.gds_path}", file=sys.stderr)
        return 2

    die_box = find_top_instance_bbox_um(top, flipped, layout.dbu)
    if die_box is None:
        print(f"ERROR: {flipped.name} is not instanced directly under TOP",
              file=sys.stderr)
        return 2

    cupillar_box = find_top_instance_bbox_um(top, cupillars, layout.dbu)
    if cupillar_box is None:
        print(f"ERROR: {cupillars.name} is not instanced directly under TOP",
              file=sys.stderr)
        return 2

    die_cx, die_cy = centroid_um(die_box)
    cu_cx, cu_cy   = centroid_um(cupillar_box)
    dx = cu_cx - die_cx
    dy = cu_cy - die_cy

    print(f"\nDie cell:       {flipped.name}")
    print(f"  bbox (um):    ({die_box.left:.3f}, {die_box.bottom:.3f}) .. "
          f"({die_box.right:.3f}, {die_box.top:.3f})")
    print(f"  size (um):    {die_box.width():.3f} x {die_box.height():.3f}")
    print(f"  centroid:     ({die_cx:.3f}, {die_cy:.3f})")

    print(f"\nCu-pillar cell: {cupillars.name}")
    print(f"  bbox (um):    ({cupillar_box.left:.3f}, {cupillar_box.bottom:.3f}) .. "
          f"({cupillar_box.right:.3f}, {cupillar_box.top:.3f})")
    print(f"  size (um):    {cupillar_box.width():.3f} x {cupillar_box.height():.3f}")
    print(f"  centroid:     ({cu_cx:.3f}, {cu_cy:.3f})")

    print(f"\nCentroid delta (cupillars - die):")
    print(f"  dx = {dx:+.3f} um")
    print(f"  dy = {dy:+.3f} um  [diagnostic only, depends on die pad layout]")

    failures = []

    if not cupillar_box.overlaps(die_box):
        failures.append(
            f"Cu-pillar bbox does not overlap die bbox at all. The die has "
            f"been placed in a completely wrong location relative to its "
            f"cu-pillars. Check hyp_to_gds.py update_chiplet_file and "
            f"_place_die_flipped.")

    if not contains_with_slack(die_box, cupillar_box, args.containment_slack_um):
        failures.append(
            f"Cu-pillar bbox is not contained within die bbox (slack "
            f"{args.containment_slack_um:.1f} um). Cu-pillars are placed "
            f"outside the die footprint — the die is mis-sized or the "
            f"cu-pillar locations are wrong.")

    if abs(dx) > args.centroid_tolerance_um:
        failures.append(
            f"X centroid mismatch: |dx| = {abs(dx):.3f} um > "
            f"tolerance {args.centroid_tolerance_um:.3f} um. Probable cause: "
            f"systematic X offset between die placement and cu-pillar "
            f"placement (check kicad_pcb_to_iopads.py center_x_dbu output "
            f"or add_cupillar_pads coordinate math).")

    if failures:
        print(f"\nFAIL: {len(failures)} alignment check(s) failed:",
              file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"\nOK: alignment checks passed.")
    print(f"  - Cu-pillars overlap die bbox.")
    print(f"  - Cu-pillars contained within die bbox "
          f"(slack {args.containment_slack_um:.1f} um).")
    print(f"  - X centroid match within {args.centroid_tolerance_um:.3f} um "
          f"(actual: {abs(dx):.3f} um).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
