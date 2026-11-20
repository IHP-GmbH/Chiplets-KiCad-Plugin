# SPDX-License-Identifier: GPL-3.0-or-later
"""routed_inputs.py, the worker-tier read of the routed path's three inputs,
and the CLI tier's call of it (routed.source.read_in_worker).

The report must carry every input unchanged and every version warning the
reads give, the vendored reader's own included, and no other; a report the CLI cannot trust, and a
script that fails, is refused, never read as inputs.
"""

import hashlib
import json
import sys

import pytest

from chiplet_export import routed_inputs
from chiplet_export.routed.document import RoutedRefused
from chiplet_export.routed.intermediates import read_inputs
from chiplet_export.routed.source import read_in_worker

from .routed_t03_stage import CHIPLET, IO_PADS, stage


def _run(document, chiplet=CHIPLET, *, python=sys.executable):
    messages = []
    inputs = read_in_worker(python, str(document), str(chiplet), str(IO_PADS),
                            on_warn=messages.append)
    return inputs, messages


def test_the_worker_read_gives_what_the_in_process_read_gives(tmp_path):
    document = stage(tmp_path)
    inputs, messages = _run(document)
    assert inputs == read_inputs(str(document), str(CHIPLET), str(IO_PADS))
    assert messages == []


def _newer_chiplet(tmp_path):
    """The fixture .chiplet at a newer format minor, and a document bound to it."""
    chiplet = tmp_path / "newer.chiplet"
    text = CHIPLET.read_text()
    assert text.startswith("format_version: '1.0'\n")
    chiplet.write_text(text.replace("format_version: '1.0'", "format_version: '1.9'", 1))
    digest = hashlib.sha256(chiplet.read_bytes()).hexdigest()

    def bind(document):
        document["inputs"]["chiplet"]["sha256"] = digest
    return chiplet, stage(tmp_path, bind)


def test_the_chiplets_format_warning_reaches_the_cli(tmp_path):
    """Red: the vendored reader warns about a newer .chiplet minor on the
    worker's stderr only, and the CLI never sees it."""
    chiplet, document = _newer_chiplet(tmp_path)
    _inputs, messages = _run(document, chiplet)
    assert len(messages) == 1 and "'1.9' is newer" in messages[0], messages


@pytest.mark.parametrize("script, expected", [
    ("import sys; sys.exit(3)", "exited 3"),
    ("pass", "printed no JSON report"),
    ("print('{}')", "printed an unrecognized report"),
    ("import json; print(json.dumps({'format': %r, 'version': %d, 'warnings': [],"
     " 'inputs': [{}, {}]}))" % (routed_inputs.FORMAT, routed_inputs.VERSION),
     "printed a malformed report"),
], ids=["exit", "silent", "foreign", "short"])
def test_a_worker_that_does_not_report_is_refused(tmp_path, script, expected):
    python = tmp_path / "python"
    python.write_text('#!/bin/sh\nexec "%s" -c "%s"\n'
                      % (sys.executable, script.replace('"', '\\"')))
    python.chmod(0o755)
    with pytest.raises(RoutedRefused, match=expected):
        _run(stage(tmp_path), python=str(python))


def test_a_missing_worker_is_refused(tmp_path):
    with pytest.raises(RoutedRefused, match="cannot read the routed inputs"):
        _run(stage(tmp_path), python=str(tmp_path / "absent"))


def test_a_refusal_comes_back_as_one(tmp_path):
    document = stage(tmp_path, lambda d: d.update(version="2.0"))
    with pytest.raises(RoutedRefused, match="2.0"):
        _run(document)


@pytest.mark.parametrize("value, why", [
    ({"x": float("nan")}, "cannot carry"),
    ({1: "x"}, "a key that is not text"),
])
def test_a_value_json_cannot_carry_names_its_file(value, why):
    with pytest.raises(routed_inputs.InputsRefused) as exc:
        routed_inputs._carried(value, "named.json")
    assert "named.json" in str(exc.value) and why in str(exc.value)


def test_the_report_is_versioned():
    assert routed_inputs.parse_report(json.dumps({
        "format": routed_inputs.FORMAT, "version": routed_inputs.VERSION,
        "warnings": ["w"], "refused": "why"})) == (None, ["w"], "why")
    with pytest.raises(routed_inputs.ReportError):
        routed_inputs.parse_report(json.dumps({
            "format": routed_inputs.FORMAT, "version": routed_inputs.VERSION + 1,
            "warnings": [], "refused": "why"}))


def test_a_warning_that_is_not_a_version_warning_stays_out_of_the_report(
        tmp_path, monkeypatch):
    """The worker's interpreter may warn about anything (a deprecation in a
    package the reader loads); only the readers' version warnings are the
    export's."""
    import warnings
    real = routed_inputs.read_inputs

    def noisy(*args, **kwargs):
        warnings.warn("unrelated", RuntimeWarning)
        return real(*args, **kwargs)
    monkeypatch.setattr(routed_inputs, "read_inputs", noisy)
    # In this process the reader warns once per version; start afresh.
    routed_inputs.vendored.chiplet_format_io()._reset_version_warnings()
    chiplet, document = _newer_chiplet(tmp_path)
    with pytest.warns(RuntimeWarning, match="unrelated"):
        report = routed_inputs.report(str(document), str(chiplet), str(IO_PADS),
                                      "gds_bbox_corner_um")
    assert len(report["warnings"]) == 1 and "'1.9' is newer" in report["warnings"][0]
