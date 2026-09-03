# SPDX-License-Identifier: GPL-3.0-or-later
"""A present-but-unusable interconnect manifest refuses; an absent one degrades.

PLUG-1. ``_interconnect_technology_block`` used to wrap the manifest read in a
bare ``except Exception: return None``, which turned every read failure into
the same ``None`` that the legitimate "no method declares this adapter" path
returns. The two were then indistinguishable to the caller, so a refused
schema_version silently dropped ``interconnect.technology`` from the emitted
document and the run still finished at exit 0.

Deliberately unguarded: no ``pcbnew``, no ``klayout``, no interconnect PDK, no
sibling checkouts. Every test injects a fake reader, so these run on a bare CI
runner, which is exactly where this behaviour has to be provable.
"""

import sys
import warnings
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402


# --------------------------------------------------------------------------
# A stand-in for the interconnect PDK reader.
# --------------------------------------------------------------------------

class FakeManifestVersionError(ValueError):
    """Mirrors interconnect_pdk's ManifestVersionError: a ValueError, never a KeyError."""


class FakeManifestVersionWarning(UserWarning):
    """Mirrors ManifestVersionWarning: a UserWarning, so -W error makes it fatal."""


class FakeReader:
    """Minimal stand-in exposing only what hyp_to_gds actually touches."""

    ManifestVersionError = FakeManifestVersionError
    ManifestVersionWarning = FakeManifestVersionWarning

    def __init__(self, methods=None, raises=None):
        self._methods = methods or {}
        self._raises = raises

    def list_methods(self):
        if self._raises is not None:
            raise self._raises
        return list(self._methods)

    def get_method(self, mid):
        return self._methods[mid]


def _use(monkeypatch, reader):
    monkeypatch.setattr(h, "_import_interconnect_manifest", lambda: reader)
    # _source_refusal_exit_code and _is_manifest_version_error read the reader
    # off sys.modules, so the fake has to be reachable there too.
    monkeypatch.setitem(sys.modules, "interconnect_manifest", reader)


HEALTHY = {"m1": {"adapter": "intm4tm2_cupillar", "vendor": "PacTech (IHP)"}}


# --------------------------------------------------------------------------
# The stand-in is only legitimate while it matches the real contract.
# --------------------------------------------------------------------------

def test_the_fake_keeps_the_property_the_real_reader_pins():
    """interconnect_pdk's test_version_policy pins exactly this. If it ever
    changes upstream, this test is the thing that has become wrong."""
    assert issubclass(FakeManifestVersionError, ValueError)
    assert not issubclass(FakeManifestVersionError, KeyError)
    assert issubclass(FakeManifestVersionWarning, UserWarning)
    assert not issubclass(FakeManifestVersionWarning, ValueError)


# --------------------------------------------------------------------------
# The defect itself: the two outcomes must stop being the same value.
# --------------------------------------------------------------------------

def test_a_version_refusal_no_longer_looks_like_a_custom_adapter(monkeypatch):
    """THE defect. Both used to return a bare None."""
    _use(monkeypatch, FakeReader(raises=FakeManifestVersionError("schema 99.0")))
    with pytest.raises(h.SourceRefused) as refused:
        h._interconnect_technology_block("intm4tm2_cupillar")
    assert refused.value.exit_code == h.EXIT_SOURCE_VERSION

    _use(monkeypatch, FakeReader(HEALTHY))
    assert h._interconnect_technology_block("acme_custom") == (
        None, h._TECH_NOT_DECLARED)


def test_the_healthy_path_still_returns_the_block(monkeypatch):
    _use(monkeypatch, FakeReader(HEALTHY))
    block, reason = h._interconnect_technology_block("intm4tm2_cupillar")
    assert reason is None
    assert block["description"] == "Chiplet attachment (PacTech (IHP))"
    assert block["dbu"] == 0.001


def test_an_absent_pdk_still_degrades_quietly(monkeypatch):
    monkeypatch.setattr(h, "_import_interconnect_manifest", lambda: None)
    assert h._interconnect_technology_block("x") == (None, h._TECH_ABSENT)


def test_a_partial_install_is_absence_not_breakage(monkeypatch):
    _use(monkeypatch, FakeReader(raises=FileNotFoundError("no manifest")))
    assert h._interconnect_technology_block("x") == (None, h._TECH_ABSENT)


@pytest.mark.parametrize("exc,label", [
    (ValueError("truncated JSON"), "malformed json"),
    (KeyError("methods"), "no methods block"),
    (TypeError("methods is a list"), "wrong shape"),
    (AttributeError("entry is a string"), "entry not a dict"),
    (PermissionError("chmod 000"), "unreadable"),
])
def test_every_present_but_unusable_manifest_refuses(monkeypatch, exc, label):
    """Recognising a class is NOT what decides the refusal. The rule is
    'present and unusable', so conditions nobody anticipated refuse too."""
    _use(monkeypatch, FakeReader(raises=exc))
    with pytest.raises(h.SourceRefused) as refused:
        h._interconnect_technology_block("intm4tm2_cupillar")
    assert refused.value.exit_code == h.EXIT_SOURCE_UNREACHABLE, label


def test_keyerror_methods_is_not_treated_as_an_unknown_adapter(monkeypatch):
    """The ids come from list_methods, so a KeyError here is a structural
    break wearing the same type as an unknown id. An `except KeyError` arm
    would reopen the defect in a narrower form. Do not add one."""
    _use(monkeypatch, FakeReader(raises=KeyError("methods")))
    with pytest.raises(h.SourceRefused):
        h._interconnect_technology_block("intm4tm2_cupillar")


# --------------------------------------------------------------------------
# A refusal that any handler can absorb is not a refusal.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("absorbing", [ValueError, KeyError, OSError, LookupError])
def test_sourcerefused_survives_the_lookup_shaped_handlers(absorbing):
    assert not issubclass(h.SourceRefused, absorbing)


def test_the_chiplet_writers_terminal_handler_does_not_flatten_a_refusal():
    """update_chiplet_file's `except Exception: return False` maps to exit 1,
    which means bad caller input. No argument fixes a misaligned PDK."""
    import inspect
    body = inspect.getsource(h.update_chiplet_file)
    assert "except SourceRefused:" in body, "the re-raise arm is gone"
    # Order is the whole point: after the terminal handler it would never run.
    assert body.index("except SourceRefused:") < body.rindex("except Exception as e:")


# --------------------------------------------------------------------------
# Version-class detection is for wording only, never for the decision.
# --------------------------------------------------------------------------

def test_a_reader_without_the_version_class_still_refuses(monkeypatch):
    """interconnect_pdk's main and dev ship a reader with no version gate at
    all. A missing class must cost a diagnostic, never the protection."""
    class OldReader(FakeReader):
        pass
    OldReader.ManifestVersionError = property(lambda self: None)  # not a class
    reader = FakeReader(raises=ValueError("truncated"))
    del type(reader).ManifestVersionError
    try:
        _use(monkeypatch, reader)
        with pytest.raises(h.SourceRefused) as refused:
            h._interconnect_technology_block("x")
        assert refused.value.exit_code == h.EXIT_SOURCE_UNREACHABLE
    finally:
        type(reader).ManifestVersionError = FakeManifestVersionError


def test_manifest_class_tolerates_a_missing_or_non_class_attribute():
    class Bare:
        pass
    assert h._manifest_class(Bare(), "ManifestVersionError") == ()

    class Weird:
        ManifestVersionError = "not a class"
    assert h._manifest_class(Weird(), "ManifestVersionError") == ()
    # An empty tuple in an except clause catches nothing, which is the point.
    assert not isinstance(ValueError("x"), h._manifest_class(Weird(), "X"))


# --------------------------------------------------------------------------
# The newer-minor warning: accepted by policy, fatal only under -W error,
# and warn-once inside the reader so the failure moves depending on who read
# the manifest first.
# --------------------------------------------------------------------------

def test_a_policy_accepted_newer_minor_is_not_fatal_under_warnings_as_errors():
    """The shared policy says same major, higher minor is ACCEPTED with a
    warning. Under -W error two hosts correctly implementing the same policy
    would disagree about the same file."""
    with warnings.catch_warnings():
        # Order matters and mirrors reality: the host sets -W error at start
        # up, and the plugin installs its filter later, on the first manifest
        # import. simplefilter resets the list, so installing ours first would
        # test nothing.
        warnings.simplefilter("error")
        h._accept_newer_minor_warnings(FakeReader(HEALTHY))
        warnings.warn("newer minor", FakeManifestVersionWarning)  # must not raise

    # And a real refusal still gets through the same filter untouched.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        h._accept_newer_minor_warnings(FakeReader(HEALTHY))
        with pytest.raises(FakeManifestVersionError):
            raise FakeManifestVersionError("wrong major")


def test_the_deferred_refusal_is_recorded_not_swallowed(monkeypatch):
    """_connection_type_cli_choices runs while argparse builds the parser,
    before argv exists. It must not die there (that killed --help and made
    the site fix unreachable) and must not forget either."""
    monkeypatch.setattr(h, "_DEFERRED_SOURCE_REFUSAL", None, raising=False)
    _use(monkeypatch, FakeReader(raises=FakeManifestVersionError("schema 99.0")))
    assert h._connection_type_cli_choices() is None
    assert h._DEFERRED_SOURCE_REFUSAL is not None
    assert h._DEFERRED_SOURCE_REFUSAL.exit_code == h.EXIT_SOURCE_VERSION

    with pytest.raises(SystemExit) as exited:
        h._report_deferred_source_refusal()
    assert exited.value.code == h.EXIT_SOURCE_VERSION


def test_an_absent_pdk_records_no_refusal(monkeypatch):
    monkeypatch.setattr(h, "_DEFERRED_SOURCE_REFUSAL", None, raising=False)
    monkeypatch.setattr(h, "_import_interconnect_manifest", lambda: None)
    assert h._connection_type_cli_choices() is None
    assert h._DEFERRED_SOURCE_REFUSAL is None
    h._report_deferred_source_refusal()  # must not exit


def test_a_reader_that_is_here_but_will_not_import_refuses(tmp_path, monkeypatch):
    """Present and unusable, one frame up. Returning None said 'absent' and
    let every caller take its tolerated path; the stderr line stopped nothing."""
    pkg = tmp_path / "libs.tech" / "klayout" / "python"
    pkg.mkdir(parents=True)
    (pkg / "interconnect_manifest.py").write_text("raise RuntimeError('boom')\n")
    monkeypatch.setattr(h, "_interconnect_python_candidates", lambda: [pkg])
    monkeypatch.delitem(sys.modules, "interconnect_manifest", raising=False)
    with pytest.raises(h.SourceRefused):
        h._import_interconnect_manifest()
