# SPDX-License-Identifier: GPL-3.0-or-later
"""--system-chiplet: technology ids taken from the System .chiplet.

The pair under fixtures/two_die_bondpad comes from a reference flow run of
the two-die bondpad example: design.chiplet is the System document the
assembly was built from, draft.chiplet the run's draft .chiplet. That one
was already finalized, by a plugin from before a null technologies became
{}, so the tests run the finalizer on it a second time. finalized.chiplet is
what that pass made of it, with these tests' inputs, before the flag
existed; it differs from draft.chiplet only by technologies: {}. Without the
flag the finalizer must still make exactly that.

With the flag, the interposer and each die take the technology of the
System component with the same id, and ``technologies`` is the System's
entries for exactly the ids bound. Every refusal names the id and leaves the
.chiplet untouched; in a worker run it comes before anything is written.
"""
import copy
import re
import shutil
import sys
from pathlib import Path

import pytest

db = pytest.importorskip("klayout.db")
yaml = pytest.importorskip("yaml")

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
for entry in (PLUGIN_ROOT, PLUGIN_ROOT.parent):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import hyp_to_gds as h  # noqa: E402
import technology_overlay as overlay  # noqa: E402
from chiplet_export.pipeline.orchestrator import (  # noqa: E402
    ExportOptions, build_cli_args,
)

from .test_die_outline_finalize import (  # noqa: E402
    OUTLINE, _board, _die, _run_worker, _tree_state,
)
from .test_export_exit import PASSED, cli  # noqa: E402,F401
from .test_seal_ring import ARGV_BEFORE  # noqa: E402


@pytest.fixture(autouse=True)
def _mount_plane(attach_frame_pdk):
    """Each finalize here reads the plane from the fixture PDK root."""


PAIR = Path(__file__).resolve().parent / "fixtures" / "two_die_bondpad"
SYSTEM = str(PAIR / "design.chiplet")
# The draft names its die layouts four directories up, as in the run.
DIES = {"U1": "CF_DRIVER_BONDPAD", "U2": "CF_ECHO_BONDPAD"}
BOUNDS = (0.0, 0.0, 2000.0, 3200.0)
REFUSING = "Refusing to export: no layout, sidecar or .chiplet was written."


def _system():
    return h._vendored_cfio().load(SYSTEM)


def _component(document, ident):
    return next(c for c in document["components"] if c["id"] == ident)


def _draft(tmp_path, edit=None):
    """The draft in a run tree, with a one-box GDS under each die's top cell
    at the path it names; ``edit`` rewrites the draft's data first."""
    for cell in DIES.values():
        path = tmp_path / "dies" / cell / "layout.gds"
        path.parent.mkdir(parents=True)
        layout = db.Layout()
        top = layout.create_cell(cell)
        top.shapes(layout.layer(1, 0)).insert(db.DBox(0, 0, 1000, 1000))
        layout.write(str(path))
    export = tmp_path / "case" / "run" / "out" / "export"
    export.mkdir(parents=True)
    chiplet = export / "cf_two_die_bondpad.draft.chiplet"
    shutil.copy(PAIR / "draft.chiplet", chiplet)
    if edit is not None:
        data = yaml.safe_load(chiplet.read_text(encoding="utf-8"))
        edit(data)
        chiplet.write_text(yaml.safe_dump(data, sort_keys=False),
                           encoding="utf-8")
    return chiplet


def _finalize(chiplet, system=None, **kwargs):
    kwargs.setdefault("die_outline_exempt", list(DIES))
    return h.update_chiplet_file(
        str(chiplet),
        str(chiplet.parent / "layout"
            / "cf_two_die_bondpad.draft_interposer.gds"),
        bbox=BOUNDS, outline_bbox=BOUNDS, system_chiplet=system, **kwargs)


def _refused(tmp_path, system, match):
    chiplet = _draft(tmp_path)
    before = chiplet.read_bytes()
    with pytest.raises(h.TechnologyRefused, match=match) as info:
        _finalize(chiplet, system)
    assert chiplet.read_bytes() == before
    return info.value


# ---------------------------------------------------------------------------
# The overlay on the pair
# ---------------------------------------------------------------------------

def test_without_the_flag_the_output_is_the_one_from_before(tmp_path):
    chiplet = _draft(tmp_path)
    assert _finalize(chiplet) is True
    assert chiplet.read_bytes() == (PAIR / "finalized.chiplet").read_bytes()


def test_the_overlay_sets_each_id_and_the_catalog(tmp_path):
    chiplet = _draft(tmp_path)
    system = _system()
    assert _finalize(chiplet, system) is True
    result = h._vendored_cfio().load(str(chiplet))
    assert {c["id"]: c.get("technology") for c in result["components"]} == {
        "interposer": "intm4tm2", "U1": "sg13g2", "U2": "sg13g2"}
    assert result["technologies"] == system["technologies"]
    assert list(result["technologies"]) == ["intm4tm2", "sg13g2"]
    # Nothing else moves: without the ids it is the output from before.
    for component in result["components"]:
        del component["technology"]
    result["technologies"] = {}
    assert result == yaml.safe_load(
        (PAIR / "finalized.chiplet").read_text(encoding="utf-8"))


def test_the_keys_go_where_the_writer_puts_them(tmp_path):
    chiplet = _draft(tmp_path)
    assert _finalize(chiplet, _system()) is True
    data = yaml.safe_load(chiplet.read_text(encoding="utf-8"))
    keys = list(data)
    assert keys[keys.index("interposer") + 1] == "technologies"
    for component in data["components"]:
        keys = list(component)
        assert keys[keys.index("anchor") + 1] == "technology"


def test_anchored_paths_are_copied_as_written(tmp_path):
    system = _system()
    system["technologies"]["sg13g2"].update(
        layer_properties="${PDK_ROOT}/libs.tech/klayout/tech/sg13g2.lyp",
        stackup="/opt/pdk/sg13g2/stackup.yaml")
    chiplet = _draft(tmp_path)
    assert _finalize(chiplet, system) is True
    result = h._vendored_cfio().load(str(chiplet))
    assert result["technologies"]["sg13g2"] == {
        "description": "IHP SG13G2 130nm BiCMOS die", "dbu": 0.001,
        "layer_properties": "${PDK_ROOT}/libs.tech/klayout/tech/sg13g2.lyp",
        "stackup": "/opt/pdk/sg13g2/stackup.yaml"}


def test_a_null_entry_is_written_as_an_empty_one(tmp_path):
    system = _system()
    system["technologies"]["sg13g2"] = None
    chiplet = _draft(tmp_path)
    assert _finalize(chiplet, system) is True
    assert yaml.safe_load(chiplet.read_text(
        encoding="utf-8"))["technologies"]["sg13g2"] == {}


def test_a_system_die_the_draft_lacks_is_not_an_error(tmp_path):
    """A truncated build: U2 never reached the board. Its technology is not
    bound, so it is not in the catalog either, and its entry is not read."""
    system = _system()
    _component(system, "U2")["technology"] = "other_process"
    system["technologies"]["other_process"] = {
        "description": "only U2 uses it", "layer_properties": "relative.lyp"}
    chiplet = _draft(tmp_path, edit=lambda data: data.__setitem__(
        "components", [c for c in data["components"] if c["id"] != "U2"]))
    assert _finalize(chiplet, system, die_outline_exempt=["U1"]) is True
    result = h._vendored_cfio().load(str(chiplet))
    assert [c["id"] for c in result["components"]] == ["interposer", "U1"]
    assert sorted(result["technologies"]) == ["intm4tm2", "sg13g2"]


def test_an_io_pad_component_is_not_given_a_technology(tmp_path):
    """J1 is a wire-bond pad some tool wrote as a die (an --io-pads entry
    carries its id); the System has no J1, and that is not a refusal."""
    def add_pad(data):
        data["components"].append({
            "id": "J1", "type": "die",
            "dimensions": {"width": 100.0, "height": 100.0, "thickness": 0.0},
            "position": {"x": 10.0, "y": 10.0, "z": 0.0}})
    chiplet = _draft(tmp_path, edit=add_pad)
    assert _finalize(chiplet, _system(), io_pads=[],
                     io_pad_refs=["J1"]) is True
    pad = _component(yaml.safe_load(chiplet.read_text(encoding="utf-8")),
                     "J1")
    assert "technology" not in pad


# ---------------------------------------------------------------------------
# Refusals (update_chiplet_file raises before writing)
# ---------------------------------------------------------------------------

def test_a_draft_component_the_system_lacks_refuses(tmp_path):
    system = _system()
    system["components"] = [c for c in system["components"]
                            if c["id"] != "U2"]
    _refused(tmp_path, system,
             r"^U2: no component of that id in the System \.chiplet$")


@pytest.mark.parametrize("value", ["absent", None, "", 7],
                         ids=["absent", "null", "empty", "not-a-string"])
def test_a_system_component_without_technology_refuses(tmp_path, value):
    system = _system()
    if value == "absent":
        del _component(system, "U1")["technology"]
    else:
        _component(system, "U1")["technology"] = value
    _refused(tmp_path, system,
             r"^U1: its System component has no technology$")


def test_an_undeclared_technology_refuses(tmp_path):
    system = _system()
    _component(system, "U1")["technology"] = "custom_process"
    _refused(tmp_path, system,
             r"^U1: technology 'custom_process' is not declared in the "
             r"System's technologies$")


def test_a_carrier_other_than_intm4tm2_refuses(tmp_path):
    # Declared, so only the carrier rule refuses it: until a carrier is read
    # from its technology package, IntM4TM2 is the only one.
    system = _system()
    system["technologies"]["gf180mcu"] = {}
    _component(system, "interposer")["technology"] = "gf180mcu"
    _refused(tmp_path, system,
             r"^interposer: CARRIER_UNSUPPORTED_TECHNOLOGY: the carrier's "
             r"technology is 'gf180mcu'")


def test_a_system_without_a_catalog_refuses_every_id(tmp_path):
    system = _system()
    del system["technologies"]
    refusal = _refused(tmp_path, system, r"^interposer: technology "
                                         r"'intm4tm2' is not declared")
    assert [ident for ident, _cause in refusal.offenders] == [
        "interposer", "U1", "U2"]


@pytest.mark.parametrize("field,value", [
    ("layer_properties", "tech/sg13g2.lyp"),
    ("stackup", "stackup.yaml"),
    ("layer_properties", "$PDK_ROOT/sg13g2.lyp"),
])
def test_a_relative_resource_path_refuses(tmp_path, field, value):
    system = _system()
    system["technologies"]["sg13g2"][field] = value
    _refused(tmp_path, system,
             r"^sg13g2: %s '%s' is a relative path, which cannot be "
             r"anchored to the exported \.chiplet$"
             % (field, re.escape(value)))


def test_a_duplicated_system_id_refuses(tmp_path):
    system = _system()
    system["components"].append(copy.deepcopy(_component(system, "U1")))
    _refused(tmp_path, system, r"^U1: more than one component of that id in "
                               r"the System \.chiplet$")


def test_an_entry_that_is_not_a_mapping_refuses(tmp_path):
    system = _system()
    system["technologies"]["sg13g2"] = "sg13g2"
    _refused(tmp_path, system, r"^sg13g2: its System entry is not a mapping$")


def test_every_offender_is_named_in_one_refusal(tmp_path):
    system = _system()
    system["components"] = [c for c in system["components"]
                            if c["id"] != "U2"]
    system["technologies"]["intm4tm2"]["stackup"] = "stackup.yaml"
    refusal = _refused(tmp_path, system, r"^U2: no component")
    assert refusal.lines() == [
        "Technology refused: U2: no component of that id in the System "
        ".chiplet",
        "Technology refused: intm4tm2: stackup 'stackup.yaml' is a relative "
        "path, which cannot be anchored to the exported .chiplet"]


# ---------------------------------------------------------------------------
# Worker runs
# ---------------------------------------------------------------------------

def _worker(tmp_path, system):
    hyp, _unused, out, chiplet = _board(tmp_path)
    flags = ["--die-outline-layers", "U1=%d/%d" % OUTLINE,
             "--system-chiplet", system]
    return out, chiplet, lambda: _run_worker(tmp_path, hyp, out, chiplet,
                                             flags)


def test_worker_writes_the_ids(tmp_path):
    """The board has the interposer and U1; the System's U2 is not there."""
    _out, chiplet, run = _worker(tmp_path, SYSTEM)
    result = run()
    assert result.returncode == 0, result.stderr
    assert "Technologies from %s:\n  interposer: intm4tm2\n  U1: sg13g2\n" % (
        SYSTEM) in result.stdout
    data = h._vendored_cfio().load(chiplet)
    assert _die(chiplet, "U1")["technology"] == "sg13g2"
    assert _component(data, "interposer")["technology"] == "intm4tm2"
    assert data["technologies"] == _system()["technologies"]


def _write_system(tmp_path, edit):
    system = _system()
    edit(system)
    path = tmp_path / "system" / "design.chiplet"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(system, sort_keys=False), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("edit,line", [
    (lambda s: _component(s, "U1").pop("technology"),
     "Technology refused: U1: its System component has no technology"),
    (lambda s: s["technologies"]["intm4tm2"].__setitem__(
        "layer_properties", "intm4tm2.lyp"),
     "Technology refused: intm4tm2: layer_properties 'intm4tm2.lyp' is a "
     "relative path"),
], ids=["no-technology", "relative-path"])
def test_worker_refusal_writes_nothing(tmp_path, edit, line):
    system = _write_system(tmp_path, edit)
    _out, _chiplet, run = _worker(tmp_path, system)
    before = _tree_state(str(tmp_path))
    result = run()
    assert result.returncode == 1, result.stderr
    assert _tree_state(str(tmp_path)) == before
    assert line in result.stderr
    assert REFUSING in result.stderr


def test_worker_refuses_a_system_it_cannot_read(tmp_path):
    missing = str(tmp_path / "absent.chiplet")
    _out, _chiplet, run = _worker(tmp_path, missing)
    before = _tree_state(str(tmp_path))
    result = run()
    assert result.returncode == 1, result.stderr
    assert _tree_state(str(tmp_path)) == before
    assert "ERROR: cannot read the System .chiplet %s" % missing in (
        result.stderr)
    assert REFUSING in result.stderr


def test_entry_point_reports_a_raised_refusal(monkeypatch, capsys):
    def refuse():
        raise h.TechnologyRefused([("U1", "a cause")])
    monkeypatch.setattr(h, "main", refuse)
    assert h._main_reporting_refusals() == 1
    assert "Technology refused: U1: a cause" in capsys.readouterr().err


def test_the_flag_is_reported_ignored_without_a_chiplet(tmp_path, capsys):
    hyp, _unused, out, _chiplet = _board(tmp_path, chiplet=False)
    assert h.convert_hyp_to_gds(
        hyp_path=hyp, output_path=str(out / "layout" / "demo_interposer.gds"),
        lyp_path=str(Path(__file__).resolve().parent / "fixtures"
                     / "intm4tm2_reference.lyp"),
        system_chiplet_path=SYSTEM) is True
    err = capsys.readouterr().err
    assert ("Warning: --system-chiplet is ignored without "
            "--update-chiplet-file.") in err


# ---------------------------------------------------------------------------
# Plan, argv and CLI
# ---------------------------------------------------------------------------

def test_plan_skips_the_pads_it_is_given():
    document = {"components": [{"id": "interposer"}, {"id": "J1"}]}
    system = {"components": [{"id": "interposer", "technology": "intm4tm2"}],
              "technologies": {"intm4tm2": {"dbu": 0.001}}}
    assert overlay.plan(document, system, pads=["J1"]) == (
        {"interposer": "intm4tm2"}, {"intm4tm2": {"dbu": 0.001}})
    with pytest.raises(overlay.TechnologyRefused, match=r"^J1: no component"):
        overlay.plan(document, system)


def test_the_argv_without_the_flag_is_the_argv_from_before():
    off = build_cli_args("hyp_to_gds.py", "in.hyp", "demo",
                         ExportOptions(output_dir="/out"))
    on = build_cli_args("hyp_to_gds.py", "in.hyp", "demo",
                        ExportOptions(output_dir="/out",
                                      system_chiplet="/run/design.chiplet"))
    assert off == ARGV_BEFORE
    assert on == off + ["--system-chiplet", "/run/design.chiplet"]


def test_cli_passes_the_system_chiplet_absolute(cli, tmp_path, monkeypatch):
    (tmp_path / "design.chiplet").write_text("format_version: '1.0'\n",
                                             encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(PASSED, "--system-chiplet", "design.chiplet") == 0
    assert cli.exports[-1].system_chiplet == str(tmp_path / "design.chiplet")
    assert cli.main(PASSED) == 0
    assert cli.exports[-1].system_chiplet == ""


def test_cli_refuses_a_missing_system_chiplet(cli, tmp_path, capsys):
    missing = str(tmp_path / "absent.chiplet")
    assert cli.main(PASSED, "--system-chiplet", missing) == 2
    assert cli.exports == []
    assert ("chiplet-export: System .chiplet not found: %s" % missing
            in capsys.readouterr().err)
