# SPDX-License-Identifier: GPL-3.0-or-later
"""Which trace ends get the w/2 square end (free_ends).

An end that another trace of its net runs through, at least as wide, is
drawn flush: that trace already carries the board's round cap there. Every
other end is free. Plain geometry on board coordinates, no PDK needed.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hyp_to_gds import ChainedTrace, free_ends  # noqa: E402

DBU = 0.001


def _ends(*traces):
    return free_ends([ChainedTrace("TopMetal1", "N", w, list(pts))
                      for pts, w in traces], DBU)


def test_a_stub_teed_into_a_run_is_flush():
    run, stub = ([(0.0, 0.0), (40.0, 0.0)], 4.0), ([(20.0, 0.0), (20.0, 30.0)], 4.0)
    assert _ends(run, stub) == [(True, True), (False, True)]


def test_a_branch_from_a_corner_is_flush():
    corner = ([(0.0, 0.0), (20.0, 0.0), (40.0, 20.0)], 4.0)
    branch = ([(20.0, 0.0), (20.0, 30.0)], 4.0)
    assert _ends(corner, branch) == [(True, True), (False, True)]


def test_at_a_width_change_only_the_narrow_end_is_flush():
    # The wide end's square covers the step; the narrow end lands in it.
    wide, narrow = ([(0.0, 0.0), (0.0, 20.0)], 6.0), ([(0.0, 20.0), (0.0, 40.0)], 2.0)
    assert _ends(wide, narrow) == [(True, True), (False, True)]


def test_two_ends_as_wide_meeting_stay_free():
    # A nanometre apart, so not chained into one path: nothing runs through
    # the point, and flush ends there would leave the corner (or a slot) open.
    left = ([(0.0, 0.0), (20.0, 0.0)], 4.0)
    up = ([(20.001, 0.0), (20.001, 20.0)], 4.0)
    assert _ends(left, up) == [(True, True), (True, True)]


def test_a_closed_loop_keeps_both_ends():
    loop = ([(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 0.0)], 4.0)
    assert _ends(loop) == [(True, True)]


def test_a_repeated_end_point_stays_free():
    assert _ends(([(0.0, 0.0), (0.0, 0.0), (20.0, 0.0)], 4.0)) == [(True, True)]


def test_an_end_beside_a_trace_is_free():
    # One nanometre off the run is still on it (the board's own rounding),
    # a grid step off is a separate end.
    run = ([(0.0, 0.0), (40.0, 0.0)], 4.0)
    assert _ends(run, ([(20.0, 0.001), (20.0, 30.0)], 4.0))[1] == (False, True)
    assert _ends(run, ([(20.0, 0.005), (20.0, 30.0)], 4.0))[1] == (True, True)
