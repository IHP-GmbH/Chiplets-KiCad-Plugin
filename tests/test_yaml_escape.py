# SPDX-License-Identifier: GPL-2.0-or-later
"""Unit tests for the shared YAML scalar escaper (writers/_yaml.py).

These pin the bare-vs-quoted classifier and the escape set at the boundary,
where the C++/Python byte-locked pair is most likely to diverge. They are pure
Python, so they run on any host without pcbnew. A value ending in a newline
previously slipped through bare on the Python side (the `$` regex anchor)
while the C++ byte loop quoted it; this is the regression guard for that.
"""
import importlib.util
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_SPEC = importlib.util.spec_from_file_location(
    "_yaml_under_test",
    Path(__file__).resolve().parents[1] / "writers" / "_yaml.py",
)
_M = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_M)
yaml_scalar = _M.yaml_scalar
escape_yaml_dq = _M.escape_yaml_dq


def test_clean_tokens_stay_bare():
    # No-op on clean input: clean tokens must be emitted bare and unchanged.
    for s in ["U1", "sg13g2", "wire_bond", "0x10", "x.y", "x/y", "intm4tm2_U1"]:
        assert yaml_scalar(s) == s


@pytest.mark.parametrize("s", ["-x", ".x", "+x", "on", "ON", "null", "~", "no"])
def test_boundary_values_are_quoted(s):
    # Leading non-bare chars and reserved words must be quoted, and round-trip
    # back as the literal string rather than the wrong scalar type.
    out = yaml_scalar(s)
    assert out.startswith('"') and out.endswith('"')
    assert yaml.safe_load(out) == s


def test_trailing_newline_is_quoted_not_bare():
    # Regression: Python `$` matched before a trailing newline, emitting the
    # value bare with a raw newline; it must now quote+escape like the C++ side.
    assert yaml_scalar("U1\n") == '"U1\\n"'
    assert yaml.safe_load(yaml_scalar("U1\n")) == "U1\n"


@pytest.mark.parametrize("s", ['q"x', "a\\b", "x: y", "x\ny", "#c", "* a", "a\tb"])
def test_quoted_values_round_trip_and_have_no_raw_control(s):
    out = yaml_scalar(s)
    assert "\n" not in out.strip("\n") or out.startswith('"')
    assert "\t" not in out  # tab is escaped, never raw
    assert yaml.safe_load(out) == s


def test_escape_yaml_dq_exact():
    assert escape_yaml_dq('a"b\\c\nd\re\tf') == 'a\\"b\\\\c\\nd\\re\\tf'
