# SPDX-License-Identifier: GPL-3.0-or-later
"""Test bootstrap: make ``chiplet_export.*`` and top-level ``hyp_to_gds``
imports resolve no matter what the checkout directory is called or how
pytest is invoked.

The canonical package dirname is ``chiplet_export`` (it lives at
``plugins/chiplet_export`` in this repo and is symlinked under that name into
KiCad's plugins folder). A raw clone of the upstream repository lands in
``Chiplets-KiCad-Plugin`` and CI checkouts use the repository name, neither of
which is the package name. Two shims make the absolute imports resolve:

1. Put the package root on ``sys.path`` so the top-level ``import hyp_to_gds``
   used by several tests resolves, and so ``chiplet_export`` is importable when
   the directory really is named ``chiplet_export``.
2. If the directory is named something else, alias the package root as
   ``chiplet_export`` in ``sys.modules`` before any test module imports.
"""

import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if (_ROOT.name != "chiplet_export"
        and "chiplet_export" not in sys.modules):
    _pkg = types.ModuleType("chiplet_export")
    _pkg.__path__ = [str(_ROOT)]
    sys.modules["chiplet_export"] = _pkg


# An interposer PDK root holding only the technology package manifest and
# the stack descriptor it names, both copied verbatim from the PDK
# (fixtures/intm4tm2_pdk/README.md), so a test reads the mount plane from a
# known file and never from the host's PDK.
FIXTURE_PDK_ROOT = Path(__file__).resolve().parent / "fixtures" / "intm4tm2_pdk"
PACKAGE_MANIFEST = FIXTURE_PDK_ROOT.joinpath("libs.tech", "adk", "technology.json")
STACK_DESCRIPTOR = FIXTURE_PDK_ROOT / "libs.tech" / "intm4tm2_stack.json"


def make_pdk_root(base, descriptor=STACK_DESCRIPTOR, manifest=PACKAGE_MANIFEST):
    """An interposer PDK root holding ``manifest`` as its technology package
    manifest, the marker the discovery accepts it by, and ``descriptor`` at
    the stack path that manifest names (the fixture's when there is none).
    Either is left out when None (no manifest: a root the discovery
    refuses)."""
    root = Path(base) / "intm4tm2_pdk"
    (root / "libs.tech" / "klayout").mkdir(parents=True, exist_ok=True)
    stack = json.loads(PACKAGE_MANIFEST.read_text(encoding="utf-8"))
    if manifest is not None:
        (root / "libs.tech" / "adk").mkdir(exist_ok=True)
        shutil.copyfile(str(manifest),
                        str(root / "libs.tech" / "adk" / "technology.json"))
        stack = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if descriptor is not None:
        target = root.joinpath(*stack["stack"]["path"].split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(descriptor), str(target))
    return root


def write_carrier_package(root, *, ident="intm4tm2",
                          lyp="libs.tech/klayout/tech/carrier.lyp",
                          stack="libs.tech/carrier_stack.json"):
    """A schema-valid technology package manifest at ``root``: the carrier
    role, ``ident``, and the member paths ``lyp`` and ``stack`` as given
    (the member files are the caller's). Returns the manifest."""
    manifest = Path(root).joinpath("libs.tech", "adk", "technology.json")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "schema": "technology-package", "schema_version": "1.0",
        "id": ident, "roles": ["carrier"], "revision": "test",
        "layer_properties": {"lyp": lyp,
                             "lyt": "libs.tech/klayout/tech/carrier.lyt"},
        "stack": {"path": stack},
    }), encoding="utf-8")
    return manifest


def write_io_pads(path, pads, **envelope):
    """An io pad manifest at ``path`` as the export writes it: the
    envelope hyp_to_gds requires, any of its keys replaced by ``envelope``
    (None drops it), then ``pads``. Returns the path."""
    from chiplet_export import io_pads_manifest
    data = {"schema": io_pads_manifest.SCHEMA,
            "version": io_pads_manifest.VERSION,
            "frame": io_pads_manifest.FRAME}
    for key, value in envelope.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    data["io_pads"] = pads
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    return Path(path)


@pytest.fixture
def attach_frame_pdk(monkeypatch):
    """INTERPOSER_PDK_ROOT set to the fixture PDK root."""
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(FIXTURE_PDK_ROOT))
    return FIXTURE_PDK_ROOT


def sibling_workspace(tmp_path, markers, *names):
    """A start path for a case that asserts the sibling walk itself.

    The path lies in a workspace that holds the siblings ``names`` and
    nothing else, so the walk up from it finds them and never what lies
    above the checkout (a tiered runner refuses that walk, see the root
    conftest). ``markers`` maps a variable to its accepted directory names
    and marker subpath, as the discovery under test does. Each sibling is
    the root its variable names; when the variable is unset, outside a
    tiered runner, it is the checkout the walk finds from here. It is
    linked under its first accepted name. A sibling whose marker is missing
    skips the case, naming the file.
    """
    ws = tmp_path / "workspace"
    start = ws / "plugin" / "pipeline" / "module.py"
    start.parent.mkdir(parents=True)
    for name in names:
        dirnames, sub = markers[name]
        root = os.environ.get(name)
        if not root and not os.environ.get("HCDP_CI"):
            root = next((str(base / d) for base in Path(__file__).resolve().parents
                         for d in dirnames if (base / d).joinpath(*sub).exists()),
                        None)
        marker = Path(root or "${%s}" % name).joinpath(*sub)
        if not root or not marker.exists():
            pytest.skip("no sibling for the walk: %s is missing" % marker)
        (ws / dirnames[0]).symlink_to(Path(root).resolve(), target_is_directory=True)
    return start


def use_sibling_checkouts(*names):
    """The siblings ``names`` for a case that reads their trees.

    A set variable is the sibling, used as it is: it must hold the marker
    the discovery checks, or the discovery would walk past it to whatever
    lies beside the checkout, so a missing marker skips the case, naming the
    file. An unset variable leaves the discovery to walk to the checkouts
    beside this one on a host; a tiered runner refuses that walk, so there
    the case skips, naming the variable.
    """
    from chiplet_export.layer_map import PATH_VAR_MARKERS
    for name in names:
        value = os.environ.get(name)
        if value:
            marker = Path(value).joinpath(*PATH_VAR_MARKERS[name][1])
            if not marker.exists():
                pytest.skip("no sibling: %s is missing" % marker)
        elif os.environ.get("HCDP_CI"):
            pytest.skip("no sibling: %s is unset" % name)


#: chiplet-spec's pin in this repo, in the format gds_to_kicad's copies use:
#: line one names the commit and the reader's upstream directory, each further
#: line the sha256 of a copied file. Both chiplet-spec copies follow it (the
#: reader and the top-level block oracle), so their gates read it from here
#: and a re-vendor rewrites one file, not a constant per gate.
SPEC_SOURCE = _ROOT / "vendor" / "chiplet_format_io.SOURCE"


def chiplet_spec_pin():
    """``(commit, upstream directory, {file: sha256})`` as SPEC_SOURCE states them."""
    head, *digests = SPEC_SOURCE.read_text(encoding="utf-8").splitlines()
    marker, repo, commit, upstream = head.split()
    if (marker, repo) != ("#", "chiplet-spec"):
        raise ValueError("%s: line one must read '# chiplet-spec <commit> <dir>'"
                         % SPEC_SOURCE)
    return commit, upstream, {name: sha for sha, name in (d.split() for d in digests)}


def spec_root():
    """CHIPLET_SPEC_ROOT (or HCDP_ROOT/chiplet-spec, see the root conftest),
    else the sibling walk, which a tiered runner (HCDP_CI) refuses."""
    env = os.environ.get("CHIPLET_SPEC_ROOT")
    if env and os.path.isdir(env):
        return env
    if os.environ.get("HCDP_CI"):
        return None
    sibling = _ROOT.parents[2] / "chiplet-spec"
    return str(sibling) if (sibling / ".git").exists() else None


def spec_blob_at_pin(root, commit, path):
    """The bytes of chiplet-spec's ``path`` at ``commit``, the pin a vendored
    copy names.

    A tiered runner exports the pinned chiplet-spec one commit deep, so there
    HEAD must be ``commit``: a constant naming anything else is a copy nobody
    re-vendored when the pin moved, and the case fails. A host checkout may sit
    on any commit; there the blob is read from its history, and the case skips
    when that history does not have it. Either way the commit is read, never
    the working tree.
    """
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    ci = bool(os.environ.get("HCDP_CI"))
    if ci and not head.startswith(commit):
        pytest.fail("the pinned chiplet-spec is %s and the copy names %s. "
                    "Re-vendor at the pin." % (head[:12] or "?", commit))
    rev = "HEAD" if head.startswith(commit) else commit
    proc = subprocess.run(["git", "-C", str(root), "show", "%s:%s" % (rev, path)],
                          capture_output=True)
    if proc.returncode != 0:
        if ci:
            pytest.fail("the pinned chiplet-spec has no %s" % path)
        pytest.skip("chiplet-spec checkout does not have %s:%s" % (commit, path))
    return proc.stdout
