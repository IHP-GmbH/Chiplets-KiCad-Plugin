# SPDX-License-Identifier: GPL-3.0-or-later
"""update_chiplet_file reads a die's top_cell from the device GDS path.

The die top_cell is read from the die GDS. A board-relative die layout (e.g.
../chiplets/die.gds) is stored in the .chiplet relative to the .chiplet's own
directory, so at export time -- when the .chiplet may sit in a throwaway output
dir far from the die GDS -- update_chiplet_file must read top_cell from the
device's already board-resolved GDS path, not the .chiplet-relative layout
(which would resolve against the wrong directory and drop top_cell).
"""
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402


@pytest.fixture(autouse=True)
def _mount_plane(attach_frame_pdk):
    """Each finalize here reads the plane from the fixture PDK root."""


_CHIPLET = """\
format_version: "1.0"
assembly:
  name: t
components:
  - id: interposer
    type: interposer
    dimensions: {width: 1000.0, height: 1000.0, thickness: 100.0}
    position: {x: 0.0, y: 0.0, z: 0.0}
  - id: U1
    type: die
    orientation: flip_chip
    layout: ../chiplets/die.gds
    dimensions: {width: 100.0, height: 100.0, thickness: 0.0}
    position: {x: 10.0, y: 10.0, z: 0.0}
"""


# The die outline every fixture die declares: _write_die_gds draws exactly one
# rectangle on it, 1 x 1 um.
_OUTLINE = (134, 0)


def _write_die_gds(db, path, top_name):
    layout = db.Layout()
    top = layout.create_cell(top_name)
    top.shapes(layout.layer(134, 0)).insert(db.Box(0, 0, 1000, 1000))
    layout.write(str(path))


def test_top_cell_read_from_device_gds_path(tmp_path):
    db = pytest.importorskip("klayout.db")
    yaml = pytest.importorskip("yaml")

    # Die GDS lives at <root>/chiplets/die.gds. The .chiplet sits in a throwaway
    # output dir nested two levels deep, so its ../chiplets/die.gds would resolve
    # to <root>/a/b/chiplets/die.gds -- a path that does not exist. The
    # .chiplet-relative fallback therefore cannot reach the die; only the device's
    # gds_file (board-resolved absolute) makes top_cell resolvable. That isolates
    # the device-GDS preference branch: drop it and this test fails.
    die_gds = tmp_path / "chiplets" / "die.gds"
    die_gds.parent.mkdir(parents=True)
    _write_die_gds(db, die_gds, "MyDie")

    out = tmp_path / "a" / "b" / "throwaway_out"
    out.mkdir(parents=True)
    chiplet = out / "t.chiplet"
    chiplet.write_text(_CHIPLET)

    dev = h.Device(ref="U1", layer="TopMetal2", x=0.0, y=0.0, rotation=0.0,
                   gds_file=str(die_gds))

    ok = h.update_chiplet_file(
        str(chiplet), "/nonexistent_interposer.gds",
        bbox=(0.0, 0.0, 1000.0, 1000.0), devices=[dev],
        die_outline_layers={"U1": _OUTLINE})
    assert ok is True

    data = yaml.safe_load(chiplet.read_text())
    die = next(c for c in data["components"] if c.get("id") == "U1")
    assert die.get("top_cell") == "MyDie"
    # Sized from the same device GDS the top cell came from.
    assert (die["dimensions"]["width"], die["dimensions"]["height"]) == (1.0,
                                                                         1.0)


@pytest.mark.parametrize("refs,names", [
    (("U1", "U2"), ("layout.gds", "layout.gds")),
    # Concatenating ref_basename would collide even with different basenames.
    (("U1_a", "U1"), ("b.gds", "a_b.gds")),
    (("U/1", "U%2F1"), ("layout.gds", "layout.gds")),
    (("../U1", "U1"), ("layout.gds", "layout.gds")),
    ((".", ".."), ("layout.gds", "layout.gds")),
])
def test_bundled_dies_keep_distinct_layouts_after_moving_output(tmp_path,
                                                              refs, names):
    import shutil
    db = pytest.importorskip("klayout.db")
    yaml = pytest.importorskip("yaml")
    out = tmp_path / "export"
    out.mkdir()
    document = yaml.safe_load(_CHIPLET)
    document["components"] = document["components"][:1]
    devices, originals = [], {}
    for index, (ref, filename) in enumerate(zip(refs, names)):
        source = tmp_path / ("source%d" % index) / filename
        source.parent.mkdir()
        top = "DRIVER" if index == 0 else "ECHO"
        _write_die_gds(db, source, top)
        originals[ref] = (source, source.read_bytes(), top)
        document["components"].append({
            "id": ref, "type": "die", "layout": "missing/" + filename,
            "position": {"x": 0, "y": 0, "z": 0},
        })
        devices.append(h.Device(ref, "TopMetal2", str(source)))
    chiplet = out / "t.chiplet"
    chiplet.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert h.update_chiplet_file(str(chiplet), "/nonexistent_interposer.gds",
                                 bbox=(0, 0, 1000, 1000), devices=devices,
                                 die_outline_layers=dict.fromkeys(refs,
                                                                  _OUTLINE))

    moved = tmp_path / "moved"
    shutil.move(str(out), str(moved))
    final = yaml.safe_load((moved / "t.chiplet").read_text())
    layouts = []
    for component in final["components"]:
        if component["type"] != "die":
            continue
        source, original_bytes, top = originals[component["id"]]
        relative = Path(component["layout"])
        assert not relative.is_absolute()
        bundled = (moved / relative).resolve()
        assert bundled.is_relative_to(moved / "chiplets")
        assert bundled.read_bytes() == original_bytes, component["id"]
        layout = db.Layout()
        layout.read(str(bundled))
        assert layout.top_cell().name == component["top_cell"] == top
        assert source.read_bytes() == original_bytes
        layouts.append(relative)
    assert len(layouts) == len(set(layouts)) == 2


def test_resolvable_layout_reference_does_not_create_a_bundle(tmp_path,
                                                             monkeypatch):
    db = pytest.importorskip("klayout.db")
    yaml = pytest.importorskip("yaml")
    source = tmp_path / "layout.gds"
    _write_die_gds(db, source, "DIRECT")
    out = tmp_path / "export"
    out.mkdir()
    chiplet = out / "t.chiplet"
    chiplet.write_text(_CHIPLET.replace("../chiplets/die.gds", "../layout.gds"))

    def no_copy(*args, **kwargs):
        pytest.fail("a resolvable source must not be bundled")

    monkeypatch.setattr(h.shutil, "copy2", no_copy)
    assert h.update_chiplet_file(
        str(chiplet), "/nonexistent_interposer.gds", bbox=(0, 0, 1000, 1000),
        devices=[h.Device("U1", "TopMetal2", str(source))],
        die_outline_layers={"U1": _OUTLINE})
    final = yaml.safe_load(chiplet.read_text())
    die = next(c for c in final["components"] if c["id"] == "U1")
    assert die["layout"] == "../layout.gds"
    assert die["top_cell"] == "DIRECT"
    assert not (out / "chiplets").exists()


def test_absolute_die_layout_in_the_workspace_is_written_relative(
        tmp_path, monkeypatch):
    # One workspace, mounted at two roots (a container and its host). The die
    # sits beside the output dir, not under it, recorded by its absolute path,
    # which opens from one mount only. The .chiplet names it relative to
    # itself instead, without copying it, so it opens from both.
    db = pytest.importorskip("klayout.db")
    yaml = pytest.importorskip("yaml")
    workspace = tmp_path / "work"
    source = workspace / "dies" / "U1" / "layout.gds"
    source.parent.mkdir(parents=True)
    _write_die_gds(db, source, "DIE_U1")
    out = workspace / "out"
    interposer = out / "layout" / "interposer.gds"
    interposer.parent.mkdir(parents=True)
    _write_die_gds(db, interposer, "INTERPOSER")
    chiplet = out / "t.chiplet"
    chiplet.write_text(_CHIPLET.replace("../chiplets/die.gds", str(source)))

    def no_copy(*args, **kwargs):
        pytest.fail("a die inside the workspace must not be bundled")

    monkeypatch.setattr(h.shutil, "copy2", no_copy)
    assert h.update_chiplet_file(
        str(chiplet), str(interposer), bbox=(0, 0, 1000, 1000),
        devices=[h.Device("U1", "TopMetal2", str(source))],
        die_outline_layers={"U1": _OUTLINE})
    final = yaml.safe_load(chiplet.read_text())
    layouts = {c["id"]: c["layout"] for c in final["components"]}
    assert layouts == {
        "interposer": str(Path("layout") / "interposer.gds"),
        "U1": str(Path("..") / "dies" / "U1" / "layout.gds")}
    assert not (out / "chiplets").exists()

    remounted = tmp_path / "remounted"
    workspace.rename(remounted)
    for ref, layout in sorted(layouts.items()):
        assert (remounted / "out" / layout).is_file(), ref


def test_a_layout_sharing_only_the_root_stays_absolute(tmp_path):
    # Another mount (/opt) shares nothing with the output dir but "/": no
    # relative reference would survive a remount there, so keep it absolute.
    die = "/opt/dies/U1/layout.gds"
    assert h._layout_ref(die, str(tmp_path / "out")) == die


def test_a_symlinked_layout_is_named_by_the_link(tmp_path):
    # The link sits in the workspace, its target outside it. Only the link is
    # reachable through a mount of the workspace, so the reference follows the
    # link: resolving it first would name the target instead.
    target = tmp_path / "outside" / "die.gds"
    target.parent.mkdir()
    target.write_bytes(b"")
    link = tmp_path / "work" / "dies" / "U1.gds"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    assert h._layout_ref(str(link), str(tmp_path / "work" / "out")) == str(
        Path("..") / "dies" / "U1.gds")


def test_empty_die_id_is_refused_by_the_loader_before_any_bundle(tmp_path,
                                                                  capsys):
    db = pytest.importorskip("klayout.db")
    source = tmp_path / "layout.gds"
    _write_die_gds(db, source, "UNNAMED")
    chiplet = tmp_path / "t.chiplet"
    chiplet.write_text(_CHIPLET.replace("id: U1", 'id: ""'))
    before = chiplet.read_bytes(), source.read_bytes()
    # The guarded .chiplet load already refuses an empty id, before the die
    # outline read and before the bundling guard behind it. Pin that reason,
    # so a failure for any other cause cannot pass this test.
    assert not h.update_chiplet_file(
        str(chiplet), "/nonexistent_interposer.gds", bbox=(0, 0, 1000, 1000),
        devices=[h.Device("", "TopMetal2", str(source))],
        die_outline_layers={"": _OUTLINE})
    assert "component[1] missing required 'id'" in capsys.readouterr().err
    assert not (tmp_path / "chiplets").exists()
    assert (chiplet.read_bytes(), source.read_bytes()) == before
