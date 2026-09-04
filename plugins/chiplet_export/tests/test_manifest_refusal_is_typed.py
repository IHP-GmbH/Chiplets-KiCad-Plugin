# SPDX-License-Identifier: GPL-3.0-or-later
"""A manifest refusal from the by-id helpers is typed, not a raw traceback.

PLUG-7. ``_connection_to_body_diameter`` and ``_connection_to_adapter`` caught
only ``(KeyError, FileNotFoundError)``, so the reader's version refusal escaped
raw. Escaping was better than swallowing, but it surfaced as a traceback out of
main with exit 1, which means bad caller input, and in ``convert_hyp_to_gds``
it arrived AFTER the interposer GDS was already on disk. A refusal that lands
after the artifact exists is a report, not a refusal.

The ``KeyError`` arm stays, and the tests below pin that it stays: unlike the
technology-block site of PLUG-1, these two look a method up BY ID, so a
KeyError genuinely means "that id is not in the manifest".

Unguarded on purpose: no pcbnew, no klayout, no interconnect PDK.
"""

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402


class FakeVersionError(ValueError):
    """Mirrors interconnect_pdk's ManifestVersionError: a ValueError."""


class RaisingReader:
    ManifestVersionError = FakeVersionError

    def __init__(self, exc):
        self._exc = exc

    def get_method(self, mid):
        raise self._exc


def _use(monkeypatch, reader):
    monkeypatch.setattr(h, "_import_interconnect_manifest", lambda: reader)
    monkeypatch.setitem(sys.modules, "interconnect_manifest", reader)


BY_ID_HELPERS = [h._connection_to_body_diameter, h._connection_to_adapter]


@pytest.mark.parametrize("helper", BY_ID_HELPERS)
def test_a_version_refusal_from_a_by_id_helper_is_typed(monkeypatch, helper):
    _use(monkeypatch, RaisingReader(FakeVersionError("schema 99.0")))
    with pytest.raises(h.SourceRefused) as refused:
        helper("cupillar_opt2")
    assert refused.value.exit_code == h.EXIT_SOURCE_VERSION


@pytest.mark.parametrize("helper", BY_ID_HELPERS)
def test_a_corrupt_manifest_from_a_by_id_helper_is_typed(monkeypatch, helper):
    """Not only the version class. The rule is present-and-unusable."""
    _use(monkeypatch, RaisingReader(ValueError("truncated JSON")))
    with pytest.raises(h.SourceRefused) as refused:
        helper("cupillar_opt2")
    assert refused.value.exit_code == h.EXIT_SOURCE_UNREACHABLE


@pytest.mark.parametrize("helper", BY_ID_HELPERS)
def test_an_unknown_method_id_stays_a_quiet_none(monkeypatch, helper):
    """KeyError IS the 'unknown method' signal in these two, because they look
    a method up BY ID. That arm must survive the PLUG-7 change."""
    _use(monkeypatch, RaisingReader(KeyError("no such method")))
    assert helper("nope") is None


@pytest.mark.parametrize("helper", BY_ID_HELPERS)
def test_a_partial_install_stays_a_quiet_none(monkeypatch, helper):
    _use(monkeypatch, RaisingReader(FileNotFoundError("no manifest")))
    assert helper("cupillar_opt2") is None


@pytest.mark.parametrize("helper", BY_ID_HELPERS)
def test_an_absent_pdk_stays_a_quiet_none(monkeypatch, helper):
    monkeypatch.setattr(h, "_import_interconnect_manifest", lambda: None)
    assert helper("cupillar_opt2") is None


def test_the_boundary_reports_a_refusal_instead_of_a_traceback(monkeypatch):
    """Without this the refusal leaves as exit 1, which means bad caller
    input, and sends the user looking for an argument to change."""
    def boom():
        raise h.SourceRefused("unusable", exit_code=h.EXIT_SOURCE_VERSION)

    monkeypatch.setattr(h, "main", boom)
    assert h._main_reporting_refusals() == h.EXIT_SOURCE_VERSION
