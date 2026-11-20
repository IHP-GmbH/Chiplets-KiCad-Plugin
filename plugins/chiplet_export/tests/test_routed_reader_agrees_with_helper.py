# SPDX-License-Identifier: GPL-3.0-or-later
"""The plugin's routed-geometry reader and chiplet-spec's agree.

chiplet-spec ships ``chiplet_format_io.routed_geometry.load_routed_geometry``,
which checks a document against the canonical schema with jsonschema. KiCad's
Python has no jsonschema, so the plugin reads the document with its own parser
(``routed_document.read_document``) and calls the shared version policy from
its vendored ``chiplet_format_io``. Two readers of one format drift unless
something holds them together; this is that something.

Both readers run over the same documents: the typical_03 fixture and the
schema's refusal cases applied to it. For every case they must give the same
verdict, and where both accept, the same data and one version warning each.
The plugin may be stricter than the schema (it refuses keys it does not
read); it may never accept what the helper refuses. A second test sweeps the
fixture mechanically (every key dropped, every value replaced by one of the
wrong type, an empty string, zero, a negative or a trailing newline) and holds
the plugin to refusing every variant the helper refuses.

The helper is loaded from chiplet-spec at the commit the vendored reader is
pinned to, so the version policy on both sides is the same bytes. Where
jsonschema or the checkout is missing the case skips; the plugin's tiered
runs have both. When KiCad's Python can import the helper, the plugin should
call it and this file goes.
"""

import copy
import hashlib
import importlib
import importlib.util
import json
import sys

import pytest

from .conftest import chiplet_spec_pin, spec_blob_at_pin, spec_root
from .routed_t03_stage import stage

from chiplet_export import routed_document

#: The helper's files, from the pinned chiplet-spec, as the package needs them
#: to import and to find its schema.
HELPER_MODULES = ("__init__.py", "routed_geometry.py", "die_contract.py",
                  "supply_domains.py")
SCHEMA_FILE = "schemas/routed_geometry.schema.json"
PACKAGE = "_pinned_chiplet_format_io"


@pytest.fixture(scope="module")
def helper(tmp_path_factory):
    """chiplet-spec's ``routed_geometry`` module at the vendored reader's pin."""
    pytest.importorskip("jsonschema")
    root = spec_root()
    if root is None:
        pytest.skip("chiplet-spec checkout not found (set CHIPLET_SPEC_ROOT)")
    commit, upstream, digests = chiplet_spec_pin()
    package = tmp_path_factory.mktemp("helper") / PACKAGE
    (package / "schemas").mkdir(parents=True)
    for name in HELPER_MODULES:
        (package / name).write_bytes(
            spec_blob_at_pin(root, commit, "%s/%s" % (upstream, name)))
    (package / SCHEMA_FILE).write_bytes(spec_blob_at_pin(root, commit, SCHEMA_FILE))
    # Same version policy on both sides: the helper's package is the vendored one.
    assert hashlib.sha256((package / "__init__.py").read_bytes()).hexdigest() \
        == digests["__init__.py"]

    spec = importlib.util.spec_from_file_location(
        PACKAGE, package / "__init__.py", submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    try:
        spec.loader.exec_module(module)
        yield importlib.import_module(PACKAGE + ".routed_geometry")
    finally:
        for name in [n for n in sys.modules if n == PACKAGE or n.startswith(PACKAGE + ".")]:
            del sys.modules[name]


def _first(document, key):
    return next(net for net in document["nets"] if net[key])[key][0]


def _set(key, value):
    def edit(document):
        document[key] = value
    return edit


def _drop_drawn(document):
    del document["nets"][0]["drawn"]


def _via_without_size(document):
    del _first(document, "vias")["size"]


def _one_cut_with_pitch(document):
    _first(document, "drawn").update(k=1, pitch=1.0)


def _digest_with_newline(document):
    document["devices"][0]["layout"]["sha256"] += "\n"


def _newer_minor_with_a_key(document):
    document.update(version="1.1", future=True)


#: (edit, both accept, what a red here means)
CASES = {
    "typical_03": (None, True,
                   "the readers disagree on the routed export's own document"),
    "newer minor": (_set("version", "1.1"), True,
                    "a compatible minor is read differently, or warned about differently"),
    "newer minor, added key": (_newer_minor_with_a_key, True,
                               "a compatible addition is read differently"),
    "other major": (_set("version", "2.0"), False,
                    "one reader accepts a major version the policy refuses"),
    "net without drawn": (_drop_drawn, False,
                          "one reader accepts a net without its drawn vias"),
    "digest with newline": (_digest_with_newline, False,
                            "one reader accepts a digest the schema's pattern refuses"),
    "wrong schema": (_set("schema", "routed-geometry-x"), False,
                     "one reader accepts another document type"),
    "no devices": (_set("devices", []), False,
                   "one reader accepts a document with no devices"),
    "via without size": (_via_without_size, False,
                         "one reader accepts a placed via with no size"),
    "one cut with a pitch": (_one_cut_with_pitch, False,
                             "one reader accepts a pitch on a single cut"),
    "board frame": (_set("frame", "board_absolute_ynegated_um"), False,
                    "one reader accepts the board's frame"),
}


def _read(read, refused, reset, path):
    """``(accepted, data, warnings)`` from one reader."""
    reset()
    events = []
    try:
        data = read(str(path), on_warn=events.append)
    except refused as exc:
        return False, str(exc), len(events)
    finally:
        reset()
    return True, data, len(events)


@pytest.mark.needs_sibling_sources
@pytest.mark.parametrize("case", list(CASES))
def test_the_two_readers_agree(case, helper, tmp_path):
    edit, accepted, red = CASES[case]
    path = stage(tmp_path, edit)
    cfio = sys.modules[PACKAGE]
    vendored = routed_document._chiplet_format_io()

    ours = _read(routed_document.read_document, routed_document.DocumentRefused,
                 vendored._reset_version_warnings, path)
    theirs = _read(helper.load_routed_geometry, cfio.ChipletFormatError,
                   cfio._reset_version_warnings, path)

    assert ours[0] == theirs[0] == accepted, (
        "%s: %s. plugin %s, helper %s"
        % (case, red, "accepts" if ours[0] else "refuses (%s)" % ours[1],
           "accepts" if theirs[0] else "refuses (%s)" % theirs[1]))
    if accepted:
        written = json.loads(path.read_text())
        assert ours[1] == theirs[1] == written, "%s: %s" % (case, red)
        minor = written["version"] != routed_document.SUPPORTED_VERSION
        assert ours[2] == theirs[2] == int(minor), (
            "%s: %s. plugin warned %d time(s), helper %d"
            % (case, red, ours[2], theirs[2]))


@pytest.mark.needs_sibling_sources
def test_the_two_readers_support_one_version(helper):
    """The plugin's supported version is the helper's, and the document type
    is the same name."""
    assert routed_document.SUPPORTED_VERSION == helper.SUPPORTED_VERSION
    assert routed_document.SCHEMA == helper.SCHEMA


def _nodes(node, path=()):
    """Every value under ``node`` with its path; of a list, the first and
    the last item."""
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _nodes(value, path + (key,))
    elif isinstance(node, list):
        for index in sorted({0, len(node) - 1}) if node else ():
            yield from _nodes(node[index], path + (index,))


def _wrong(value):
    """Values the schema refuses where ``value`` stands, or may: the other
    types, and the edges of its own."""
    if isinstance(value, bool):
        return ["x", 1]
    if isinstance(value, (int, float)):
        return ["x", 0, -1, True, None]
    if isinstance(value, str):
        return ["", 5, None, value + "\n"]
    if isinstance(value, list):
        return [{}, [], "x", None]
    if isinstance(value, dict):
        return [[], "x", None, {}]
    return [0, -1, "x", 1.0]


#: The variant that drops the key.
_DROP = object()


def _variants(document):
    """``(label, document)`` for each key dropped and each value replaced."""
    for path, value in _nodes(document):
        if not path:
            continue
        for replacement in [_DROP] + _wrong(value):
            variant = copy.deepcopy(document)
            parent = variant
            for step in path[:-1]:
                parent = parent[step]
            if replacement is _DROP:
                del parent[path[-1]]
                label = "drop %s" % "/".join(map(str, path))
            else:
                parent[path[-1]] = replacement
                label = "%s = %r" % ("/".join(map(str, path)), replacement)
            yield label, variant


@pytest.mark.needs_sibling_sources
def test_the_plugin_refuses_every_variant_the_helper_refuses(helper, tmp_path):
    """Red: the plugin accepts a variant of typical_03 the schema refuses,
    or reads one both accept as other data; the message lists them."""
    cfio = sys.modules[PACKAGE]
    document = json.loads(stage(tmp_path).read_text())
    path = tmp_path / "variant.json"
    refused, accepted_by_us, read_otherwise = 0, [], []
    for label, variant in _variants(document):
        path.write_text(json.dumps(variant))
        cfio._reset_version_warnings()
        try:
            theirs = helper.load_routed_geometry(path, on_warn=lambda _message: None)
        except cfio.ChipletFormatError:
            theirs = None
            refused += 1
        try:
            ours = routed_document.read_document(str(path), on_warn=lambda _message: None)
        except routed_document.DocumentRefused:
            continue
        if theirs is None:
            accepted_by_us.append(label)
        elif ours != theirs:
            read_otherwise.append(label)
    cfio._reset_version_warnings()
    # typical_03 gives 856 refused variants at the pinned chiplet-spec; far fewer
    # means the sweep stopped reaching the document, not that it got cleaner.
    assert refused >= 800, "the sweep refused only %d variants" % refused
    assert not accepted_by_us, (
        "the plugin accepts %d variant(s) the helper refuses: %s"
        % (len(accepted_by_us), "; ".join(accepted_by_us)))
    assert not read_otherwise, (
        "the readers return different data for: %s" % "; ".join(read_otherwise))
