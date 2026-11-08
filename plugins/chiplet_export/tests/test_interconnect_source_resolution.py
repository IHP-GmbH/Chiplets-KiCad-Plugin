# SPDX-License-Identifier: GPL-3.0-or-later
"""Source authority at resolution, preflight, and the worker environment seam.

Pure preflight cases use an explicitly labelled JSON reader double. The final
probe executes the actual PDK reader in a fresh process when its source is
available; absence of that optional sibling is an explicit skip, not evidence
that a subprocess stub exercised the production reader.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from chiplet_export.pipeline import discovery, orchestrator


def _manifest(root, marker="selected"):
    path = root / "manifest" / "interconnect_methods.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "1.0", "methods": {marker: {}},
    }), encoding="utf-8")
    return path


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    near = tmp_path / "near"
    start = near / "plugins" / "source.py"
    start.parent.mkdir(parents=True)
    monkeypatch.setattr(orchestrator, "__file__", str(start))
    monkeypatch.delenv("INTERCONNECT_PDK_ROOT", raising=False)
    monkeypatch.setattr(discovery, "_lookup_text_var", lambda board, name: "")
    return SimpleNamespace(
        start=start, a=near / "interconnect_pdk", b=tmp_path / "interconnect_pdk")


@pytest.fixture
def json_reader_double(monkeypatch):
    """Trace preflight reads without depending on a sibling PDK installation."""
    reads = []

    def load_manifest(path):
        reads.append(Path(path))
        return json.loads(Path(path).read_text(encoding="utf-8"))

    monkeypatch.setattr(orchestrator, "_interconnect_reader",
                        lambda: SimpleNamespace(load_manifest=load_manifest))
    return reads


@pytest.mark.parametrize("selection", ["explicit", "environment", "project"])
@pytest.mark.parametrize("state", ["missing", "empty_manifest", "malformed"])
def test_selected_bad_source_never_falls_back(
        source_tree, monkeypatch, json_reader_double, selection, state):
    a, b = source_tree.a, source_tree.b
    _manifest(b, "competitor")
    if state == "empty_manifest":
        (a / "manifest").mkdir(parents=True)
    elif state == "malformed":
        _manifest(a).write_text("{broken", encoding="utf-8")
    explicit = ""
    project = str(b)
    if selection == "explicit":
        explicit = str(a)
        monkeypatch.setenv("INTERCONNECT_PDK_ROOT", str(b))
    elif selection == "environment":
        monkeypatch.setenv("INTERCONNECT_PDK_ROOT", str(a))
    else:
        project = str(a)
    monkeypatch.setattr(discovery, "_lookup_text_var", lambda board, name: project)

    selected = orchestrator.discover_interconnect_root(
        board=object(), start=str(source_tree.start), explicit_root=explicit)
    assert selected == str(a)
    with pytest.raises(orchestrator.InterconnectSourceRefused) as error:
        orchestrator._preflight_interconnect_manifest(explicit, board=object())
    assert str(a) in str(error.value)
    assert json_reader_double == (
        [a / "manifest" / "interconnect_methods.json"] if state == "malformed" else [])


def test_automatic_empty_nearer_checkout_does_not_shadow_manifest(
        source_tree, json_reader_double):
    (source_tree.a / "manifest").mkdir(parents=True)
    expected = _manifest(source_tree.b, "competitor")
    assert orchestrator.discover_interconnect_root(
        start=str(source_tree.start)) == str(source_tree.b)
    snapshot = orchestrator._preflight_interconnect_manifest()
    assert snapshot.root == str(source_tree.b)
    assert set(snapshot.methods) == {"competitor"}
    assert json_reader_double == [expected]


def test_only_empty_automatic_checkout_is_genuine_absence(
        source_tree, json_reader_double):
    (source_tree.a / "manifest").mkdir(parents=True)
    assert orchestrator.discover_interconnect_root(start=str(source_tree.start)) == ""
    snapshot = orchestrator._preflight_interconnect_manifest()
    assert snapshot.manifest is None and snapshot.root == ""
    assert json_reader_double == []


@pytest.mark.parametrize("fault", ["malformed", "unreadable"])
def test_automatic_present_unusable_manifest_refuses_before_competitor(
        source_tree, monkeypatch, json_reader_double, fault):
    selected_path = _manifest(source_tree.a)
    _manifest(source_tree.b, "competitor")
    if fault == "malformed":
        selected_path.write_text("{broken", encoding="utf-8")
    else:
        # Permission bits are unreliable under privileged test runners. Inject
        # the reader's PermissionError explicitly; discovery remains real.
        def denied(path):
            json_reader_double.append(Path(path))
            raise PermissionError("controlled unreadable manifest")
        monkeypatch.setattr(orchestrator, "_interconnect_reader",
                            lambda: SimpleNamespace(load_manifest=denied))
    assert orchestrator.discover_interconnect_root(
        start=str(source_tree.start)) == str(source_tree.a)
    with pytest.raises(orchestrator.InterconnectSourceRefused) as error:
        orchestrator._preflight_interconnect_manifest()
    assert str(source_tree.a) in str(error.value)
    assert json_reader_double == [selected_path]


def test_blank_explicit_and_environment_allow_project_selection(
        source_tree, monkeypatch):
    monkeypatch.setenv("INTERCONNECT_PDK_ROOT", " \t")
    monkeypatch.setattr(discovery, "_lookup_text_var",
                        lambda board, name: str(source_tree.a))
    assert orchestrator.discover_interconnect_root(
        board=object(), start=str(source_tree.start), explicit_root=" \t") == str(source_tree.a)


def test_candidate_inspection_error_defers_refusal_until_preflight(
        source_tree, monkeypatch, json_reader_double):
    selected_path = _manifest(source_tree.a)
    _manifest(source_tree.b, "competitor")
    is_file = Path.is_file

    def denied(path):
        if path == selected_path:
            raise PermissionError("controlled candidate inspection refusal")
        return is_file(path)

    monkeypatch.setattr(Path, "is_file", denied)
    # GUI prefill needs a recoverable path, not an exception before controls
    # exist. B must not replace a source whose presence cannot be inspected.
    assert orchestrator.discover_interconnect_root() == str(source_tree.a)
    with pytest.raises(orchestrator.InterconnectSourceRefused) as error:
        orchestrator._preflight_interconnect_manifest()
    assert "cannot inspect" in str(error.value)
    assert str(selected_path) in str(error.value)
    assert json_reader_double == []


def _actual_reader_source():
    candidates = []
    configured = os.environ.get("INTERCONNECT_PDK_ROOT")
    if configured:
        candidates.append(Path(configured))
    if not os.environ.get("HCDP_CI"):  # a tiered runner refuses the walk
        for parent in Path(__file__).resolve().parents:
            candidates.extend(parent / name for name in
                              ("interconnect_pdk", "IHP-Interconnect-IntM4TM2"))
    for root in candidates:
        source = root / "libs.tech" / "klayout" / "python" / "interconnect_manifest.py"
        if source.is_file():
            return source
    pytest.skip("actual interconnect_manifest reader source is not installed")


@pytest.mark.needs_sibling_sources
@pytest.mark.parametrize("malformed", [False, True])
def test_worker_environment_pins_source_with_actual_reader(tmp_path, malformed):
    source = _actual_reader_source()
    a, b = tmp_path / "selected", tmp_path / "competitor"
    selected_path = _manifest(a, "selected")
    _manifest(b, "competitor")
    if malformed:
        selected_path.write_text("{broken", encoding="utf-8")
    # Copy unchanged production reader bytes into the competing checkout. Its
    # own-repo fallback now genuinely points at B in the fresh process.
    copied = b / "libs.tech" / "klayout" / "python" / source.name
    copied.parent.mkdir(parents=True)
    shutil.copyfile(source, copied)
    assert copied.read_bytes() == source.read_bytes()
    base = dict(os.environ, INTERCONNECT_PDK_ROOT=str(b))
    env = orchestrator.build_worker_env(
        orchestrator.ExportOptions(interconnect_pdk_root=str(a)), base_env=base)
    assert env["INTERCONNECT_PDK_ROOT"] == str(a)
    assert base["INTERCONNECT_PDK_ROOT"] == str(b)
    script = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('interconnect_manifest', sys.argv[1])
reader = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = reader
spec.loader.exec_module(reader)
try:
    result = reader.load_manifest()
except Exception as error:
    print(json.dumps({'error': type(error).__name__, 'message': str(error)}))
    sys.exit(5)
print(json.dumps({'methods': sorted(result['methods'])}))
"""
    result = subprocess.run([sys.executable, "-I", "-c", script, str(copied)],
                            env=env, cwd=tmp_path, capture_output=True, text=True)
    output = json.loads(result.stdout)
    if malformed:
        assert result.returncode == 5, result.stderr
        assert output["error"] == "ValueError"
        assert str(selected_path) in output["message"]
        assert "Failed to parse manifest" in output["message"]
    else:
        assert result.returncode == 0, result.stderr
        assert output == {"methods": ["selected"]}
