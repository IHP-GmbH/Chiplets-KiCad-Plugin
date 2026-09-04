# SPDX-License-Identifier: GPL-3.0-or-later
"""The .chiplet write is all-or-nothing.

PLUG-8: the finalizer used a plain open(path, 'w'), which truncates the
existing document before a byte of the new one is written. Any failure in
yaml.dump left a truncated .chiplet where a valid one used to be, on the file
the clobber guard exists to protect.

Unguarded on purpose: no pcbnew, no klayout, no interconnect PDK.
"""

import os
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402


ORIGINAL = "format_version: \"1.0\"\nassembly:\n  name: keep-me\n"


def _fail_the_write(monkeypatch):
    """Fail partway through writing, the way a full disk does."""
    def boom(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(h.os, "fsync", boom)


def test_a_failed_write_leaves_the_original_document_intact(tmp_path, monkeypatch):
    target = tmp_path / "a.chiplet"
    target.write_text(ORIGINAL)
    _fail_the_write(monkeypatch)

    with pytest.raises(OSError):
        h._atomic_write_text(str(target), "new content")

    assert target.read_text() == ORIGINAL, "the original was destroyed"


def test_a_failed_write_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    target = tmp_path / "a.chiplet"
    target.write_text(ORIGINAL)
    _fail_the_write(monkeypatch)

    with pytest.raises(OSError):
        h._atomic_write_text(str(target), "x")

    assert [p.name for p in tmp_path.iterdir()] == ["a.chiplet"]


def test_a_successful_write_replaces_the_whole_document(tmp_path):
    target = tmp_path / "a.chiplet"
    target.write_text(ORIGINAL)
    h._atomic_write_text(str(target), "brand new\n")
    assert target.read_text() == "brand new\n"


def test_the_temp_file_is_a_sibling_so_replace_stays_atomic(tmp_path, monkeypatch):
    """os.replace is atomic only within one filesystem. A temp in /tmp would
    silently give back the guarantee this function exists to provide."""
    target = tmp_path / "sub" / "a.chiplet"
    target.parent.mkdir()
    target.write_text(ORIGINAL)

    seen = {}
    real_mkstemp = h.tempfile.mkstemp

    def spy(*args, **kwargs):
        seen["dir"] = kwargs.get("dir")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(h.tempfile, "mkstemp", spy)
    h._atomic_write_text(str(target), "x\n")
    assert Path(seen["dir"]).resolve() == target.parent.resolve()


def test_writing_creates_the_file_when_there_was_none(tmp_path):
    target = tmp_path / "new.chiplet"
    h._atomic_write_text(str(target), "fresh\n")
    assert target.read_text() == "fresh\n"
