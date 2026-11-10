# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen worker/PDK controls for the shared-reader move; no native imports."""

import hashlib
from pathlib import Path
import sys
import types

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chiplet_export import layer_map as shared

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_LYP = FIXTURES / "intm4tm2_reference.lyp"
REFERENCE_SHA256 = "96fe744ba8ed4817a7e79800d68027d4782b649c208d9996b5de4a6fd6c823d8"


@pytest.fixture
def old_worker():
    path = FIXTURES / "layer_map_frozen.py.txt"
    module = types.ModuleType("frozen_layer_map")
    module.__file__ = str(path)
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return module


def test_image_default_lyp_full_map_matches_old_worker(old_worker):
    # Exact source blob of the ADK image's LYP, not the movable host PDK checkout.
    assert hashlib.sha256(REFERENCE_LYP.read_bytes()).hexdigest() == REFERENCE_SHA256
    old = old_worker.LayerMap(str(REFERENCE_LYP))
    assert len(old.layers) == 187  # Independently reported by Control B.
    new = shared.LayerMap(str(REFERENCE_LYP))
    assert new.layers == old.layers
    strict = shared.LayerMap(str(REFERENCE_LYP), strict=True)
    assert strict.layers == old.layers
    for name in old.layers:
        if ":" not in name:
            assert new.get_layer(name) == old.get_layer(name)
            assert strict.get_layer(name) == old.get_layer(name)


def test_duplicate_and_purpose_behavior_matches_old_worker(old_worker, tmp_path):
    lyp = tmp_path / "duplicate.lyp"
    lyp.write_text("""<layer-properties>
      <properties><name>Carrier.drawing</name><source>134/0@1</source></properties>
      <properties><name>Carrier.drawing</name><source>50/0@2</source></properties>
      <properties><name>Carrier.label</name><source>50/1</source></properties>
      <properties><name>Plain</name><source>67</source></properties>
      <properties><name>Skipped.drawing</name><source>bad</source></properties>
      <properties><name>F.Cu</name><source>134/0</source></properties>
    </layer-properties>""")
    old, new = old_worker.LayerMap(str(lyp)), shared.LayerMap(str(lyp))
    assert old.layers == new.layers
    assert old.get_layer("Carrier") == new.get_layer("Carrier") == (50, 0)
    assert old.get_layer("Carrier", "label") == new.get_layer("Carrier", "label") == (50, 1)
    assert old.get_layer("Carrier", "unlisted") == new.get_layer("Carrier", "unlisted") == (50, 0)
    assert old.get_layer("Plain") == new.get_layer("Plain") == (67, 0)
    for reader in (old, new):
        for unknown in ("Skipped", "F.Cu", "Unknown"):
            with pytest.raises(KeyError):
                reader.get_layer(unknown)


@pytest.mark.parametrize("bad_source", [
    "", "<source/>", "<source> </source>", "<source>134/0/50</source>",
    "<source>134/0@bad</source>", "<source>134/0@</source>",
])
@pytest.mark.parametrize("bad_first", [False, True])
def test_legacy_malformed_duplicate_policy_matches_old_worker(
        old_worker, tmp_path, bad_source, bad_first):
    good = "<properties><name>TopMetal2.drawing</name><source>50/0</source></properties>"
    bad = "<properties><name>TopMetal2.drawing</name>%s</properties>" % bad_source
    lyp = tmp_path / "legacy.lyp"
    lyp.write_text("<layer-properties>" + (bad + good if bad_first else good + bad)
                   + "</layer-properties>")
    old, new = old_worker.LayerMap(str(lyp)), shared.LayerMap(str(lyp))
    assert old.layers == new.layers
    assert old.get_layer("TopMetal2") == new.get_layer("TopMetal2")


@pytest.mark.parametrize("selection", ["default", "explicit", "variable", "walk", "invalid-env"])
def test_lyp_selection_uses_old_worker_path_and_same_bytes(old_worker, tmp_path, monkeypatch, selection):
    # Mirror the image's OpenIntM4TM2 layout with the exact image-source bytes.
    root = tmp_path / "OpenIntM4TM2"
    lyp = root / "libs.tech/klayout/tech/intm4tm2.lyp"
    lyp.parent.mkdir(parents=True)
    lyp.write_bytes(REFERENCE_LYP.read_bytes())
    tooling = root / "libs.tech/klayout/python"
    tooling.mkdir()
    (tooling / "bump_mirror.py").write_text("# discovery marker\n")
    # The IntM4TM2 tree marker; the old worker did not look for it.
    (root / "libs.tech/intm4tm2_stack.json").write_text("{}\n")
    worker = str(tmp_path / "chiplet_kicad_plugin/hyp_to_gds.py")
    old_worker.__file__ = worker
    if selection == "walk":
        monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
    else:
        monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(
            tmp_path / "missing" if selection == "invalid-env" else root))
    override = None
    if selection == "explicit":
        override = str(lyp)
    elif selection == "variable":
        override = "${INTERPOSER_PDK_ROOT}/libs.tech/klayout/tech/intm4tm2.lyp"
    old_path = old_worker._expand_path_vars(
        override if override is not None else old_worker._find_default_lyp())
    if selection == "invalid-env":
        # The one departure: the old worker walked past a set root that is not
        # an IntM4TM2 tree, and the shared reader refuses it.
        assert old_path == str(lyp)
        with pytest.raises(shared.CarrierRootRefused):
            shared.resolve_lyp_path(override, start=worker)
        return
    new_path = shared.resolve_lyp_path(override, start=worker)
    assert old_path == new_path == str(lyp)
    assert Path(new_path).samefile(old_path)
    assert hashlib.sha256(Path(new_path).read_bytes()).hexdigest() == REFERENCE_SHA256
