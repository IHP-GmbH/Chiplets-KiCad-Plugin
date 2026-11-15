# SPDX-License-Identifier: GPL-3.0-or-later
"""The carrier technology package reader: the member path and version rules
it shares with chiplet-spec's validator, its refusals, and the fixture."""

import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys

import pytest

from .conftest import (
    FIXTURE_PDK_ROOT, PACKAGE_MANIFEST, STACK_DESCRIPTOR, spec_root,
    write_carrier_package,
)
from chiplet_export import layer_map
from chiplet_export.layer_map import CarrierPackageRefused, read_carrier_package

MEMBER_PATHS = [
    "libs.tech/x.json", "./libs.tech/x.json", "libs.tech//x.json", "x",
    "a\0b", "libs.tech\\x.json", "${ROOT}/x", "%ROOT%/x", "~/x", "~x",
    "C:x", "c:/x", "/abs/x", "../x", "a/../b", "a/..", "a/.../b", "..x",
]
VERSIONS = [
    "1.0", "1.1", "1.10", "1.0.0", "1.0.7", "2.0", "0.9", "1", "1.0.0.0",
    " 1.0", "1.0 ", "+1.0", "1.-0", "1._0", "1.\u0661", "", "1..0", 1.0, None,
]


@pytest.fixture
def spec_modules(monkeypatch):
    """chiplet-spec's ``chiplet_format_io`` and its technology_package, not
    the copy the plugin vendors under the same name; sys.modules is as it
    was after the case."""
    root = spec_root()
    if root is None:
        pytest.skip("chiplet-spec checkout not found (set CHIPLET_SPEC_ROOT)")
    for name in list(sys.modules):
        if name.split(".")[0] == "chiplet_format_io":
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(os.path.join(root, "reference", "python"))
    cfio = importlib.import_module("chiplet_format_io")
    package = importlib.import_module("chiplet_format_io.technology_package")
    assert Path(cfio.__file__).is_relative_to(Path(root)), cfio.__file__
    yield cfio, package
    for name in list(sys.modules):
        if name.split(".")[0] == "chiplet_format_io":
            del sys.modules[name]


def _refusal(exc_info):
    return str(exc_info.value).split(":", 1)[0]


# ---------------------------------------------------------------------------
# The rules chiplet-spec's validator applies, held equal to it
# ---------------------------------------------------------------------------

def test_member_paths_are_refused_as_the_package_validator_refuses_them(
        spec_modules):
    _cfio, package = spec_modules
    assert ({raw: layer_map._unportable(raw) for raw in MEMBER_PATHS}
            == {raw: package._unportable(raw) for raw in MEMBER_PATHS})


def test_versions_are_read_as_the_package_validator_reads_them(spec_modules):
    cfio, _package = spec_modules
    assert ({repr(v): layer_map._contract_version(v) for v in VERSIONS}
            == {repr(v): cfio._parse_contract_version(v) for v in VERSIONS})


@pytest.mark.parametrize("fixture, refusal", [
    ("intm4tm2", None),
    ("synthetic_carrier", "CARRIER_UNSUPPORTED_TECHNOLOGY"),
])
def test_the_conformance_packages_are_read_or_refused_by_carrier_only(
        fixture, refusal):
    """The validator accepts both; the export draws only IntM4TM2."""
    root = spec_root()
    if root is None:
        pytest.skip("chiplet-spec checkout not found (set CHIPLET_SPEC_ROOT)")
    package_root = os.path.join(root, "conformance", "fixtures",
                                "technology_package", fixture)
    if refusal is None:
        assert read_carrier_package(package_root).id == fixture
        return
    with pytest.raises(CarrierPackageRefused) as exc_info:
        read_carrier_package(package_root)
    assert _refusal(exc_info) == refusal


def test_the_manifests_the_tests_write_hold_every_required_member(tmp_path):
    """No case passes on a manifest the package validator would refuse for
    a missing member: every member the schema requires, at the top and
    inside each member the manifest has, is there."""
    root = spec_root()
    if root is None:
        pytest.skip("chiplet-spec checkout not found (set CHIPLET_SPEC_ROOT)")
    schema = json.loads(Path(root, "schemas", "technology_package.schema.json")
                        .read_text(encoding="utf-8"))
    for manifest in (PACKAGE_MANIFEST, write_carrier_package(tmp_path)):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        missing = [key for key in schema["required"] if key not in data]
        for key, value in data.items():
            node = schema["properties"].get(key, {})
            if isinstance(value, dict):
                missing += ["%s.%s" % (key, sub)
                            for sub in node.get("required", ())
                            if sub not in value]
        assert missing == [], manifest


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def _package(tmp_path, **fields):
    root = tmp_path / "pdk"
    manifest = write_carrier_package(root)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    for key, value in fields.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return root


@pytest.mark.parametrize("raw", [
    "a\0b", "libs.tech\\x.json", "${ROOT}/x", "~/x", "C:x", "/abs/x", "../x",
])
def test_a_member_path_that_would_not_travel_is_refused(tmp_path, raw):
    root = _package(tmp_path, stack={"path": raw})
    with pytest.raises(CarrierPackageRefused,
                       match=r"^PACKAGE_MEMBER_PATH: stack\.path in "):
        read_carrier_package(root).member("stack", "path")


@pytest.mark.parametrize("raw", ["./libs.tech/s.json", "libs.tech//s.json"])
def test_a_member_path_the_validator_takes_is_read(tmp_path, raw):
    root = _package(tmp_path, stack={"path": raw})
    (root / "libs.tech" / "s.json").write_text("{}", encoding="utf-8")
    assert read_carrier_package(root).member("stack", "path") == str(
        root / "libs.tech" / "s.json")


def test_a_member_that_leaves_through_a_link_is_refused(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    root = _package(tmp_path, stack={"path": "libs.tech/s.json"})
    (root / "libs.tech" / "s.json").symlink_to(outside)
    with pytest.raises(CarrierPackageRefused,
                       match=r"^PACKAGE_MEMBER_PATH: .* through a link$"):
        read_carrier_package(root).member("stack", "path")


def test_a_manifest_that_leaves_through_a_link_is_refused(tmp_path):
    real = _package(tmp_path / "real")
    root = tmp_path / "pdk"
    (root / "libs.tech" / "adk").mkdir(parents=True)
    (root / "libs.tech" / "adk" / "technology.json").symlink_to(
        real / "libs.tech" / "adk" / "technology.json")
    with pytest.raises(CarrierPackageRefused,
                       match=r"^PACKAGE_MANIFEST_MISSING: .* through a link$"):
        read_carrier_package(root)


MANIFEST_CASES = [
    ({"schema_version": "1.0.0"}, None),
    ({"schema_version": "1.3"}, None),
    ({"schema_version": "2.0"}, "PACKAGE_SCHEMA_VERSION"),
    ({"schema_version": "1"}, "PACKAGE_SCHEMA_VERSION"),
    ({"schema_version": 1.0}, "PACKAGE_SCHEMA_VERSION"),
    ({"schema": "technology"}, "PACKAGE_SCHEMA_INVALID"),
    ({"roles": ["die"]}, "PACKAGE_ROLE_NOT_MODELLED"),
    ({"roles": ["carrier", "die"]}, "PACKAGE_ROLE_NOT_MODELLED"),
    ({"roles": None}, "PACKAGE_SCHEMA_INVALID"),
    ({"roles": []}, "PACKAGE_SCHEMA_INVALID"),
    ({"id": None}, "PACKAGE_SCHEMA_INVALID"),
    ({"id": "interposer_x"}, "CARRIER_UNSUPPORTED_TECHNOLOGY"),
]
MANIFEST_IDS = ["patch", "minor", "major", "no-minor", "number", "schema",
                "die-role", "die-role-too", "no-roles", "empty-roles", "no-id",
                "another-carrier"]


@pytest.mark.parametrize("fields, refusal", MANIFEST_CASES, ids=MANIFEST_IDS)
def test_the_manifest_is_read_or_refused(tmp_path, fields, refusal):
    root = _package(tmp_path, **fields)
    if refusal is None:
        assert read_carrier_package(root).id == "intm4tm2"
        return
    with pytest.raises(CarrierPackageRefused) as exc_info:
        read_carrier_package(root)
    assert _refusal(exc_info) == refusal


@pytest.mark.parametrize("fields, refusal", MANIFEST_CASES, ids=MANIFEST_IDS)
def test_the_package_validator_refuses_each_manifest_with_the_same_id(
        tmp_path, spec_modules, fields, refusal):
    """The same table through chiplet-spec's load_manifest, which takes any
    carrier: only CARRIER_UNSUPPORTED_TECHNOLOGY is the export's own."""
    pytest.importorskip("jsonschema")
    _cfio, package = spec_modules
    root = _package(tmp_path, **fields)
    expected = None if refusal == "CARRIER_UNSUPPORTED_TECHNOLOGY" else refusal
    try:
        package.load_manifest(root, on_warn=lambda text: None)
        got = None
    except package.TechnologyPackageError as exc:
        got = exc.message_id
    assert got == expected


# ---------------------------------------------------------------------------
# The fixture root
# ---------------------------------------------------------------------------

def test_the_fixture_files_are_the_ones_its_readme_names():
    readme = (FIXTURE_PDK_ROOT / "README.md").read_text(encoding="utf-8")
    for path in (PACKAGE_MANIFEST, STACK_DESCRIPTOR):
        rel = path.relative_to(FIXTURE_PDK_ROOT).as_posix()
        named = re.search(r"^- `%s`.*?sha256\s+`([0-9a-f]{64})`" % re.escape(rel),
                          readme, re.S | re.M)
        assert named, rel
        assert hashlib.sha256(path.read_bytes()).hexdigest() == named.group(1)


def test_a_member_on_another_drive_is_refused_as_outside(tmp_path,
                                                          monkeypatch):
    """commonpath raises ValueError for paths on two Windows drives; that is
    a member outside the package, not a crash past the refusal handlers."""
    root = _package(tmp_path, stack={"path": "libs.tech/s.json"})
    (root / "libs.tech" / "s.json").write_text("{}", encoding="utf-8")
    package = read_carrier_package(root)

    def two_drives(paths):
        raise ValueError("Paths don't have the same drive")
    monkeypatch.setattr(layer_map.os.path, "commonpath", two_drives)
    with pytest.raises(CarrierPackageRefused,
                       match=r"^PACKAGE_MEMBER_PATH: .* through a link$"):
        package.member("stack", "path")
