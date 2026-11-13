# SPDX-License-Identifier: GPL-3.0-or-later
"""
Host-side tests for hyp_to_gds connection-stack decoupling from the interconnect
PDK manifest. hyp_to_gds has no pcbnew/wx dependency, so these run on host.

Guards 0-regression: the manifest-sourced tables must reproduce the prior IHP
literals exactly, while the vendor demo method becomes selectable.
"""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402

from .conftest import (  # noqa: E402
    FIXTURE_PDK_ROOT, sibling_workspace, use_sibling_checkouts)

# This module asserts real sibling-PDK content: interconnect manifest methods,
# interposer canonical lyp paths, gds_to_kicad walk targets. On a lone
# checkout (e.g. a bare CI runner) none of those roots resolve -- skip the
# module instead of failing on missing ecosystem checkouts.
_MISSING_ROOTS = [
    var for var in ("INTERCONNECT_PDK_ROOT", "INTERPOSER_PDK_ROOT",
                    "GDS_TO_KICAD_ROOT")
    if h._discover_path_var(var) is None
]
pytestmark = [pytest.mark.needs_sibling_sources, pytest.mark.skipif(
    bool(_MISSING_ROOTS),
    reason="needs sibling ecosystem checkouts; unresolved: %s"
           % ", ".join(_MISSING_ROOTS))]


def test_default_connection_stacks_byte_equal_to_literal():
    stacks = h.get_default_connection_stacks()
    assert list(stacks.keys()) == [
        "cupillar_opt1", "cupillar_opt2", "cupillar_opt3", "sbump_sac305"]
    assert stacks["cupillar_opt1"] == {
        "description": "PacTech Cu Pillar, Table 6.1 Option 1 (35um opening)",
        "layers": [
            {"name": "CuPillar", "material": "Cu", "height": 28.0, "diameter": 44.0},
            {"name": "SnAgCap", "material": "SnAg", "height": 16.0, "diameter": 44.0},
        ],
    }
    assert stacks["cupillar_opt2"]["layers"][0]["diameter"] == 49.0
    assert stacks["cupillar_opt3"]["layers"][1]["height"] == 19.0
    assert stacks["sbump_sac305"]["layers"] == [
        {"name": "SolderBall", "material": "SAC305", "height": 80.0, "diameter": 80.0}]
    # The non-IHP demo method must not leak into the default library.
    assert "vendorx" not in str(stacks).lower()


def test_body_diameter_ihp_identical():
    assert h._connection_to_body_diameter("cupillar_opt1") == 44
    assert h._connection_to_body_diameter("cupillar_opt2") == 49
    assert h._connection_to_body_diameter("cupillar_opt3") == 54
    # A solder bump's body is its ball.
    assert h._connection_to_body_diameter("sbump_sac305") == 80
    assert h._connection_to_body_diameter("") is None
    assert h._connection_to_body_diameter("bogus") is None


def test_body_diameter_vendor_enabled():
    """The vendor microbump is pillar-style, so it gets a body diameter."""
    assert h._connection_to_body_diameter("vendorx_microbump") == 40


def test_cli_choices_include_all_methods():
    choices = h._connection_type_cli_choices()
    for method in ("cupillar_opt1", "cupillar_opt2", "cupillar_opt3",
                   "sbump_sac305", "vendorx_microbump"):
        assert method in choices


def test_connection_to_adapter_from_manifest():
    """Each method maps to its manifest interconnect adapter."""
    assert h._connection_to_adapter("cupillar_opt1") == "ihp_cupillar"
    assert h._connection_to_adapter("cupillar_opt2") == "ihp_cupillar"
    assert h._connection_to_adapter("cupillar_opt3") == "ihp_cupillar"
    assert h._connection_to_adapter("sbump_sac305") == "ihp_sbump"
    assert h._connection_to_adapter("vendorx_microbump") == "vendorx_microbump"
    assert h._connection_to_adapter("") is None
    assert h._connection_to_adapter("bogus") is None


def test_auto_emit_sets_adapter_from_die_connection():
    """A die's connection method auto-declares its interconnect.adapter."""
    data = {"components": [{"id": "die_a", "type": "die", "connection": "cupillar_opt2"}]}
    got = h._maybe_set_interconnect_adapter(data)
    assert got == "ihp_cupillar"
    assert data["interconnect"]["adapter"] == "ihp_cupillar"
    # Solder-bump die maps to the sbump adapter.
    data2 = {"components": [{"id": "d", "type": "die", "connection": "sbump_sac305"}]}
    assert h._maybe_set_interconnect_adapter(data2) == "ihp_sbump"


def test_auto_emit_respects_explicit_adapter():
    """An adapter already declared on the .chiplet is never overwritten."""
    data = {"interconnect": {"adapter": "vendorx_microbump"},
            "components": [{"id": "die_a", "type": "die", "connection": "cupillar_opt2"}]}
    got = h._maybe_set_interconnect_adapter(data)
    assert got is None
    assert data["interconnect"]["adapter"] == "vendorx_microbump"


def test_auto_emit_skips_when_no_adapter_bearing_connection():
    """A die without an adapter-bearing connection declares nothing."""
    data = {"components": [{"id": "u1", "type": "die"},
                           {"id": "interp", "type": "interposer"}]}
    got = h._maybe_set_interconnect_adapter(data)
    assert got is None
    assert "interconnect" not in data


def test_auto_emit_declares_technology_block():
    """The declared adapter carries its PDK-backed technology identity
    (mirrors the technologies: entries; lyp stays in writer-verbatim ${VAR}
    form for the readers to expand)."""
    data = {"components": [
        {"id": "die_a", "type": "die", "connection": "cupillar_opt1"}]}
    h._maybe_set_interconnect_adapter(data)
    tech = data["interconnect"]["technology"]
    assert tech["layer_properties"] == (
        "${INTERCONNECT_PDK_ROOT}/libs.tech/klayout/tech/interconnect.lyp")
    assert tech["dbu"] == 0.001
    assert "PacTech" in tech["description"]


def test_explicit_adapter_gains_technology_block():
    """An explicit adapter is never overwritten, but its derived technology
    identity is (re)attached so files from older exports gain it."""
    data = {"interconnect": {"adapter": "vendorx_microbump"},
            "components": [
                {"id": "d", "type": "die", "connection": "cupillar_opt2"}]}
    assert h._maybe_set_interconnect_adapter(data) is None
    assert data["interconnect"]["adapter"] == "vendorx_microbump"
    assert "VendorX" in data["interconnect"]["technology"]["description"]


def test_unknown_adapter_stays_adapter_only():
    """A hand-set adapter unknown to the manifest gets no technology block."""
    data = {"interconnect": {"adapter": "acme_custom"}, "components": []}
    assert h._maybe_set_interconnect_adapter(data) is None
    assert data["interconnect"]["adapter"] == "acme_custom"
    assert "technology" not in data["interconnect"]


# ---------------------------------------------------------------------------
# Interposer PDK discovery (ecosystem convention: env var -> upward walk)
# ---------------------------------------------------------------------------

def _walk_from(tmp_path, monkeypatch, *names):
    """Walk from a workspace holding the siblings ``names`` (conftest).
    Returns the workspace."""
    start = sibling_workspace(tmp_path, h._PATH_VAR_MARKERS, *names)
    monkeypatch.setattr(h, "__file__", str(start))
    return start.parents[2]


def test_interposer_pdk_python_found_via_walk(tmp_path, monkeypatch):
    """With no env override, the upward walk finds the sibling checkout."""
    _walk_from(tmp_path, monkeypatch, "INTERPOSER_PDK_ROOT")
    monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
    found = h._find_interposer_pdk_python()
    assert found is not None
    assert (found / "bump_mirror.py").is_file()
    assert found.parts[-3:] == ("libs.tech", "klayout", "python")


def _refusal(root):
    """The root refusal's id, the path given and the marker looked for."""
    return (r"^CARRIER_UNSUPPORTED_ROOT: \$INTERPOSER_PDK_ROOT is %s, which "
            r"has no libs\.tech/intm4tm2_stack\.json," % re.escape(str(root)))


def test_interposer_pdk_env_override_wins(tmp_path, monkeypatch):
    """INTERPOSER_PDK_ROOT pointing at a valid root takes precedence; a
    root without the marker is refused, naming it, and never replaced by
    the walk (which would draw with the sibling checkout)."""
    _walk_from(tmp_path, monkeypatch, "INTERPOSER_PDK_ROOT")
    fake = tmp_path / "pdk" / "libs.tech" / "klayout" / "python"
    fake.mkdir(parents=True)
    (fake / "bump_mirror.py").write_text("# stub\n")
    (tmp_path / "pdk" / "libs.tech" / "intm4tm2_stack.json").write_text("{}\n")
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(tmp_path / "pdk"))
    assert h._find_interposer_pdk_python() == fake

    bogus = tmp_path / "nonexistent"
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(bogus))
    with pytest.raises(SystemExit) as exc:
        h._find_interposer_pdk_python()
    assert re.match(_refusal(bogus), str(exc.value))


def test_a_klayout_pdk_tree_that_is_not_intm4tm2_is_refused(tmp_path, monkeypatch):
    """Every KLayout PDK tree has libs.tech/klayout/, the SG13G2 die PDK
    included, and it shares IntM4TM2's metal names: every reader of the
    variable refuses it, none draws with it."""
    die_pdk = tmp_path / "ihp-sg13g2"
    (die_pdk / "libs.tech" / "klayout" / "tech").mkdir(parents=True)
    (die_pdk / "libs.tech" / "klayout" / "python" / "sg13g2_pycell_lib").mkdir(
        parents=True)
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(die_pdk))
    with pytest.raises(h._layer_map.CarrierRootRefused) as refused:
        h._layer_map.discover_path_var("INTERPOSER_PDK_ROOT")
    assert re.match(_refusal(die_pdk), str(refused.value))
    for reader in (lambda: h._discover_path_var("INTERPOSER_PDK_ROOT"),
                   h._find_interposer_pdk_python, h._find_interposer_template,
                   lambda: h._expand_path_vars("${INTERPOSER_PDK_ROOT}/x")):
        with pytest.raises(SystemExit) as exc:
            reader()
        assert re.match(_refusal(die_pdk), str(exc.value))


def test_an_intm4tm2_root_without_bump_mirror_names_it(monkeypatch):
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(FIXTURE_PDK_ROOT))
    with pytest.raises(SystemExit) as exc:
        h._find_interposer_pdk_python()
    assert re.match(r"^ERROR: the interposer PDK at %s is an IntM4TM2 tree "
                    r"without libs\.tech/klayout/python/bump_mirror\.py,"
                    % re.escape(str(FIXTURE_PDK_ROOT)), str(exc.value))


def test_interconnect_pdk_resolves_live_not_fallback(tmp_path, monkeypatch):
    """The interconnect PDK probes resolve the real sibling checkout under
    its IHP layout (libs.tech/klayout/python). Guards against a silent
    fall-back to builtin tables if the layout moves again: the manifest
    reader must import via the walk alone."""
    _walk_from(tmp_path, monkeypatch, "INTERCONNECT_PDK_ROOT")
    monkeypatch.delenv("INTERCONNECT_PDK_ROOT", raising=False)
    cands = h._interconnect_python_candidates()
    hit = [c for c in cands if (c / "interconnect_manifest.py").is_file()]
    assert hit, "walk did not locate interconnect_pdk/libs.tech/klayout/python"
    assert hit[0].parts[-3:] == ("libs.tech", "klayout", "python")
    assert hit[0].parts[-4] in h._PATH_VAR_MARKERS[
        "INTERCONNECT_PDK_ROOT"][0]
    assert h._import_interconnect_manifest() is not None


def test_method_bodies_resolved_from_manifest(monkeypatch):
    """The 3D body layers handed to bump_mirror are method-resolved from the
    manifest -- a vendor method selects its own layers, never an assumed IHP
    cu-pillar pair."""
    use_sibling_checkouts("INTERCONNECT_PDK_ROOT")
    im = h._import_interconnect_manifest()
    assert im.layers_3d("cupillar_opt1") == [
        ("CuPillar", 500, 35), ("SnAgCap", 501, 35)]
    assert im.layers_3d("vendorx_microbump") == [
        ("VendorXBumpCu", 510, 35), ("VendorXBumpCap", 511, 35)]


# ---------------------------------------------------------------------------
# ${VAR} path expansion (env -> sibling-checkout walk -> loud failure)
# ---------------------------------------------------------------------------

def test_expand_path_vars_passthrough():
    """Paths without ${ are untouched; empty/None inputs too."""
    assert h._expand_path_vars("/abs/path/file.gds") == "/abs/path/file.gds"
    assert h._expand_path_vars("rel/file.gds") == "rel/file.gds"
    assert h._expand_path_vars("") == ""
    assert h._expand_path_vars(None) is None


def test_expand_path_vars_walk(tmp_path, monkeypatch):
    """${GDS_TO_KICAD_ROOT} resolves via the sibling walk when env is unset."""
    _walk_from(tmp_path, monkeypatch, "GDS_TO_KICAD_ROOT")
    monkeypatch.delenv("GDS_TO_KICAD_ROOT", raising=False)
    got = h._expand_path_vars("${GDS_TO_KICAD_ROOT}/pdks/sg13g2.lyp")
    assert "${" not in got
    assert got.endswith("/gds_to_kicad/pdks/sg13g2.lyp")
    assert Path(got).is_file()


def test_expand_path_vars_env_wins(tmp_path, monkeypatch):
    """A valid env root takes precedence; a bogus one is refused, naming the
    marker it lacks, and never replaced by the walk."""
    workspace = _walk_from(tmp_path, monkeypatch, "INTERPOSER_PDK_ROOT")
    fake = tmp_path / "pdk"
    (fake / "libs.tech").mkdir(parents=True)
    (fake / "libs.tech" / "intm4tm2_stack.json").write_text("{}\n")
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(fake))
    got = h._expand_path_vars(
        "${INTERPOSER_PDK_ROOT}/libs.tech/klayout/tech/intm4tm2.lyp")
    assert got.startswith(str(fake))

    bogus = tmp_path / "nonexistent"
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(bogus))
    with pytest.raises(SystemExit) as exc:
        h._expand_path_vars("${INTERPOSER_PDK_ROOT}/x")
    assert re.match(_refusal(bogus), str(exc.value))
    assert not (workspace / "interposer" / "x").exists()


def test_expand_path_vars_unknown_var_is_loud(monkeypatch):
    """An unresolvable variable must hard-fail naming the variable."""
    import pytest
    monkeypatch.delenv("NO_SUCH_ECOSYSTEM_ROOT", raising=False)
    with pytest.raises(SystemExit) as exc:
        h._expand_path_vars("${NO_SUCH_ECOSYSTEM_ROOT}/foo.gds")
    assert "NO_SUCH_ECOSYSTEM_ROOT" in str(exc.value)


def test_default_lyp_resolves_canonical(tmp_path, monkeypatch):
    """With the monorepo present, the default lyp is the interposer PDK's
    canonical copy (the .lyp belongs to the PDK; the plugin keeps no copy)."""
    _walk_from(tmp_path, monkeypatch, "INTERPOSER_PDK_ROOT")
    monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
    got = h._find_default_lyp()
    assert got.endswith("libs.tech/klayout/tech/intm4tm2.lyp")
    assert Path(got).parts[-5] in h._PATH_VAR_MARKERS[
        "INTERPOSER_PDK_ROOT"][0]
    assert Path(got).is_file()


# ---------------------------------------------------------------------------
# Per-die connection methods (--die-connections)
# ---------------------------------------------------------------------------

MIXED_CHIPLET = """\
format_version: "1.0"
assembly:
  name: mixed
components:
  - id: interposer
    type: interposer
    dimensions: {width: 1000.0, height: 1000.0, thickness: 100.0}
    position: {x: 0.0, y: 0.0, z: 0.0}
  - id: U1
    type: die
    orientation: flip_chip
    layout: u1.gds
    dimensions: {width: 100.0, height: 100.0, thickness: 0.0}
    position: {x: 10.0, y: 10.0, z: 0.0}
  - id: U2
    type: die
    orientation: flip_chip
    layout: u2.gds
    dimensions: {width: 100.0, height: 100.0, thickness: 0.0}
    position: {x: 200.0, y: 10.0, z: 0.0}
"""

# The finalizer sizes every die from the outline its own GDS declares. Each
# fixture die draws one 100 x 100 um rectangle on this layer, the size
# MIXED_CHIPLET already gives it.
MIXED_OUTLINE = (39, 4)


def _write_mixed_dies(tmp_path):
    from klayout import db
    for name in ("u1", "u2"):
        ly = db.Layout()
        ly.dbu = 0.001
        top = ly.create_cell(name.upper())
        top.shapes(ly.layer(*MIXED_OUTLINE)).insert(
            db.Box(0, 0, 100000, 100000))
        ly.write(str(tmp_path / (name + ".gds")))


def _update_mixed(tmp_path, **kwargs):
    import yaml
    p = tmp_path / "mixed.chiplet"
    p.write_text(MIXED_CHIPLET)
    _write_mixed_dies(tmp_path)
    kwargs.setdefault("die_outline_layers",
                      dict.fromkeys(("U1", "U2"), MIXED_OUTLINE))
    # The mount plane comes from the fixture descriptor (10.8103), not from
    # whichever interposer PDK the host has.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("INTERPOSER_PDK_ROOT", str(FIXTURE_PDK_ROOT))
        ok = h.update_chiplet_file(
            str(p), "/nonexistent.gds", bbox=(0.0, 0.0, 1000.0, 1000.0),
            **kwargs)
    assert ok is True
    return yaml.safe_load(p.read_text())


def _die(data, ref):
    return next(c for c in data["components"] if c.get("id") == ref)


def test_connection_stack_from_manifest_resolves_non_default_methods():
    """Methods outside the default library (vendorx) resolve individually."""
    stack = h._connection_stack_from_manifest("vendorx_microbump")
    assert [l["name"] for l in stack["layers"]] == [
        "VendorXBumpCu", "VendorXBumpCap"]
    assert h._connection_stack_from_manifest("bogus") is None


def test_update_chiplet_per_die_override_wins_over_global(tmp_path):
    """U1 keeps the assembly default; U2's override selects its own method,
    whose stack is injected beyond the default library, and each die's z
    comes from its own stack."""
    data = _update_mixed(
        tmp_path, connection_type="cupillar_opt1",
        die_connections={"U2": "vendorx_microbump"})
    assert _die(data, "U1")["connection"] == "cupillar_opt1"
    assert _die(data, "U2")["connection"] == "vendorx_microbump"
    assert "vendorx_microbump" in data["connection_stacks"]
    # opt1 stack 28+16=44 on 10.8103; vendorx 18+6=24 on 10.8103
    assert _die(data, "U1")["position"]["z"] == 54.8103
    assert _die(data, "U2")["position"]["z"] == 34.8103


def test_update_chiplet_per_die_without_global(tmp_path):
    """die_connections alone (no --connection-type) sets only listed dies."""
    data = _update_mixed(
        tmp_path, connection_type="",
        die_connections={"U2": "cupillar_opt2"})
    assert "connection" not in _die(data, "U1")
    assert _die(data, "U2")["connection"] == "cupillar_opt2"
    assert _die(data, "U2")["position"]["z"] == 58.8103


def test_update_chiplet_per_die_unknown_method_warned_and_skipped(
        tmp_path, capsys):
    """An unknown per-die method is warned and skipped; other dies and the
    legacy global behaviour are unaffected."""
    data = _update_mixed(
        tmp_path, connection_type="cupillar_opt1",
        die_connections={"U2": "bogus_method"})
    err = capsys.readouterr().err
    assert "bogus_method" in err
    assert _die(data, "U1")["connection"] == "cupillar_opt1"
    assert "connection" not in _die(data, "U2")
    assert "bogus_method" not in data.get("connection_stacks", {})


def test_update_chiplet_legacy_global_unchanged(tmp_path):
    """No die_connections: byte-equal behaviour to the prior global path."""
    data = _update_mixed(tmp_path, connection_type="cupillar_opt1")
    assert _die(data, "U1")["connection"] == "cupillar_opt1"
    assert _die(data, "U2")["connection"] == "cupillar_opt1"
    assert sorted(data["connection_stacks"]) == [
        "cupillar_opt1", "cupillar_opt2", "cupillar_opt3", "sbump_sac305"]


# ---------------------------------------------------------------------------
# Per-die physical thickness (--die-thicknesses)
# ---------------------------------------------------------------------------

def test_update_chiplet_die_thickness_set_per_die(tmp_path):
    """Listed dies get dimensions.thickness; unlisted dies keep the
    intermediate writer's 0.0 placeholder. position.z stays the z-mounting
    result: the die body extends up from the seating plane, so thickness
    must not shift z."""
    data = _update_mixed(
        tmp_path, connection_type="cupillar_opt1",
        die_thicknesses={"U1": 750.0})
    assert _die(data, "U1")["dimensions"]["thickness"] == 750.0
    assert _die(data, "U2")["dimensions"]["thickness"] == 0.0
    assert _die(data, "U1")["position"]["z"] == 54.8103
    assert _die(data, "U2")["position"]["z"] == 54.8103


def test_update_chiplet_die_thickness_without_connections(tmp_path):
    """Thickness applies independently of any connection selection."""
    data = _update_mixed(
        tmp_path, die_thicknesses={"U1": 250.0, "U2": 750.0})
    assert _die(data, "U1")["dimensions"]["thickness"] == 250.0
    assert _die(data, "U2")["dimensions"]["thickness"] == 750.0


def test_update_chiplet_die_thickness_never_touches_interposer(tmp_path):
    """die_thicknesses only targets die components. The interposer keeps its
    physical body (dimensions.thickness from the board stackup) untouched -- an
    "interposer" entry in the map is ignored by construction -- and the
    die-attachment surface is carried separately as attachment_surface_z."""
    data = _update_mixed(
        tmp_path, die_thicknesses={"interposer": 999.0, "U1": 750.0})
    interposer = next(c for c in data["components"]
                      if c.get("type") == "interposer")
    # Physical body kept from the fixture (100.0): not the 999.0 map entry, and
    # no longer overwritten by the attachment surface.
    assert interposer["dimensions"]["thickness"] == 100.0
    # The BEOL-top die-attachment surface is a separate component-level field.
    assert interposer["attachment_surface_z"] == 10.8103
    assert _die(data, "U1")["dimensions"]["thickness"] == 750.0


def test_update_chiplet_emits_attachment_surface_z_and_keeps_body(tmp_path):
    """The interposer emits attachment_surface_z (the die-attachment surface)
    as a component-level field and keeps dimensions.thickness as the physical
    body; a non-default surface flows through to each die's z."""
    data = _update_mixed(
        tmp_path, connection_type="cupillar_opt1", attachment_surface_z=20.0)
    interposer = next(c for c in data["components"]
                      if c.get("type") == "interposer")
    assert interposer["attachment_surface_z"] == 20.0
    assert interposer["dimensions"]["thickness"] == 100.0  # body left untouched
    # die z = attachment_surface_z + stack (cupillar_opt1 = 28 + 16 = 44)
    assert _die(data, "U1")["position"]["z"] == 20.0 + 44.0


def test_update_chiplet_no_thickness_keeps_placeholder(tmp_path):
    """Without die_thicknesses the die blocks are byte-equal to the prior
    behaviour (placeholder 0.0 survives)."""
    data = _update_mixed(tmp_path, connection_type="cupillar_opt1")
    assert _die(data, "U1")["dimensions"]["thickness"] == 0.0
    assert _die(data, "U2")["dimensions"]["thickness"] == 0.0


# ---------------------------------------------------------------------------
# Per-die 3D bodies in the generated GDS (mixed methods, one export)
# ---------------------------------------------------------------------------

MIXED_HYP = """\
{VERSION=2.14}
{UNITS=METRIC LENGTH}

{BOARD "synthetic"
  (PERIMETER_SEGMENT X1=0.000000 Y1=0.000000 X2=0.002000 Y2=0.000000)
  (PERIMETER_SEGMENT X1=0.002000 Y1=0.000000 X2=0.002000 Y2=-0.001000)
  (PERIMETER_SEGMENT X1=0.002000 Y1=-0.001000 X2=0.000000 Y2=-0.001000)
  (PERIMETER_SEGMENT X1=0.000000 Y1=-0.001000 X2=0.000000 Y2=0.000000)
}

{STACKUP
  (SIGNAL T=3.5e-05 P=0 C=1.724e-08 L="TopMetal2" M=COPPER)
}

{DEVICES
  (? REF="U1" L="TopMetal2" X=0.000200 Y=-0.000500 R=0.00 GDS_FILE="u1.gds")
  (? REF="U2" L="TopMetal2" X=0.001500 Y=-0.000500 R=0.00 GDS_FILE="u2.gds")
}

{NET="n1"
  (SEG X1=0.000100 Y1=-0.000100 X2=0.001900 Y2=-0.000100 W=0.0000040000 L="TopMetal2")
}
"""


def _write_pin_list(tmp_path, ref):
    import json
    pins = []
    for i, (x_um, y_um) in enumerate([(-100.0, 0.0), (100.0, 0.0)]):
        pins.append({
            "name": "p%d" % i, "type": "passive", "pad_index": i,
            "center_x_dbu": x_um * 1000.0, "center_y_dbu": y_um * 1000.0,
            "width_dbu": 60000.0, "height_dbu": 60000.0,
        })
    p = tmp_path / ("%s_pins.json" % ref)
    p.write_text(json.dumps(
        {"version": 1, "chiplet_name": ref, "dbu_um": 0.001, "pins": pins}))
    return str(p)


def _cell_layers(gds_path, cell_name):
    """{(layer, datatype)} with shapes under `cell_name` (flat)."""
    from klayout import db
    layout = db.Layout()
    layout.read(gds_path)
    cell = None
    for ci in range(layout.cells()):
        if layout.cell(ci).name == cell_name:
            cell = layout.cell(ci)
            break
    assert cell is not None, "cell %s not in %s" % (cell_name, gds_path)
    found = set()
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        if not cell.begin_shapes_rec(li).at_end():
            found.add((info.layer, info.datatype))
    return found


def test_mixed_methods_draw_each_dies_own_bodies(tmp_path, monkeypatch):
    """One export, two dies, two methods: U1 (default cupillar_opt1) gets
    IHP bodies 500/501, U2 (vendorx override) gets 510/511 -- in the SAME
    GDS, each under its own CUPILLARS_<ref> cell."""
    use_sibling_checkouts("INTERPOSER_PDK_ROOT",
                          "INTERCONNECT_PDK_ROOT")
    hyp = tmp_path / "mixed.hyp"
    hyp.write_text(MIXED_HYP)
    out = tmp_path / "mixed_interposer.gds"
    ok = h.convert_hyp_to_gds(
        hyp_path=str(hyp), output_path=str(out),
        lyp_path=h._find_default_lyp(),
        pad_locations={"U1": _write_pin_list(tmp_path, "U1"),
                       "U2": _write_pin_list(tmp_path, "U2")},
        connection_type="cupillar_opt1",
        die_connections={"U2": "vendorx_microbump"},
    )
    assert ok is True
    u1 = _cell_layers(str(out), "CUPILLARS_U1")
    u2 = _cell_layers(str(out), "CUPILLARS_U2")
    assert {(500, 35), (501, 35)} <= u1
    assert not ({(510, 35), (511, 35)} & u1)
    assert {(510, 35), (511, 35)} <= u2
    assert not ({(500, 35), (501, 35)} & u2)


def test_single_method_export_unchanged_by_die_connections_param(
        tmp_path, monkeypatch):
    """Without die_connections the global method drives every die (legacy)."""
    use_sibling_checkouts("INTERPOSER_PDK_ROOT",
                          "INTERCONNECT_PDK_ROOT")
    hyp = tmp_path / "single.hyp"
    hyp.write_text(MIXED_HYP)
    out = tmp_path / "single_interposer.gds"
    ok = h.convert_hyp_to_gds(
        hyp_path=str(hyp), output_path=str(out),
        lyp_path=h._find_default_lyp(),
        pad_locations={"U1": _write_pin_list(tmp_path, "U1"),
                       "U2": _write_pin_list(tmp_path, "U2")},
        connection_type="cupillar_opt1",
    )
    assert ok is True
    for ref in ("U1", "U2"):
        layers = _cell_layers(str(out), "CUPILLARS_%s" % ref)
        assert {(500, 35), (501, 35)} <= layers
        assert not ({(510, 35), (511, 35)} & layers)


def _per_anchor_pads(bm):
    """True when the loaded bump_mirror draws one pad cell per fab anchor."""
    return hasattr(bm, "fab_anchor_openings")


@pytest.mark.parametrize("method", [
    "cupillar_opt1", "cupillar_opt2", "cupillar_opt3", "vendorx_microbump"])
def test_pillar_anchored_methods_are_drawable(method):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    cell = h._check_landing_anchor(method, h._import_bump_mirror())
    assert (cell.anchor, cell.pcell, cell.group_prefix, cell.table_6_1) == (
        "ihp_cupillar_anchor", "CuPillarPad", "CUPILLARS_", True)


def test_a_solder_bump_is_drawable_where_its_pad_cell_exists():
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    bm = h._import_bump_mirror()
    if not _per_anchor_pads(bm):
        pytest.skip("this interposer PDK has no SolderBumpPad")
    cell = h._check_landing_anchor("sbump_sac305", bm)
    assert (cell.anchor, cell.pcell, cell.group_prefix, cell.table_6_1,
            cell.generator_kwargs) == (
        "ihp_sbump_anchor", "SolderBumpPad", "SBUMPS_", False,
        {"fab_anchor_id": "ihp_sbump_anchor"})


def test_a_pre_anchor_bump_mirror_draws_cupillarpad_without_the_new_keyword():
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    cell = h._check_landing_anchor("cupillar_opt1", CUPILLAR_ONLY_BM)
    assert cell == h.LandingCell(
        "ihp_cupillar_anchor", "CuPillarPad", "CUPILLARS_",
        frozenset({(9, 35), (41, 35), (99, 35)}), True, {})


# A bump_mirror before per-anchor pad cells: CuPillarPad only.
CUPILLAR_ONLY_BM = SimpleNamespace(CUPILLAR_FAB_LAYERS={
    "TopMetal2": (134, 0), "Passiv:pillar": (9, 35),
    "dfpad:pillar": (41, 35), "Recog:pillar": (99, 35)})


def _per_anchor_bm(openings):
    """A bump_mirror with a pad cell for each anchor of ``openings``."""
    def fab_anchor(anchor):
        if anchor not in openings:
            raise ValueError("no interposer pad for fab anchor %r" % anchor)
        return SimpleNamespace(pcell="Pad_" + anchor, cell_prefix="PAD",
                               table_6_1=False)

    def fab_anchor_openings(anchor):
        fab_anchor(anchor)
        return openings[anchor]
    return SimpleNamespace(fab_anchor=fab_anchor,
                           fab_anchor_openings=fab_anchor_openings)


@pytest.mark.parametrize("bm,method,reason", [
    (CUPILLAR_ONLY_BM, "sbump_sac305",
     "ihp_sbump_anchor opens 9/36, 41/36, 99/36, but the pad generator "
     "draws 9/35, 41/35, 99/35"),
    (_per_anchor_bm({}), "sbump_sac305",
     "sbump_sac305: no interposer pad for fab anchor 'ihp_sbump_anchor'"),
    (_per_anchor_bm({"ihp_sbump_anchor": {"passiv": (9, 36)}}), "sbump_sac305",
     "ihp_sbump_anchor opens 9/36, 41/36, 99/36, but the pad generator "
     "draws 9/36;"),
    (_per_anchor_bm({"ihp_sbump_anchor": [(9, 36)]}), "sbump_sac305",
     "no opening table for fab anchor ihp_sbump_anchor"),
])
def test_a_pad_cell_that_cannot_draw_the_anchor_refuses(bm, method, reason):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    with pytest.raises(ValueError, match=reason):
        h._check_landing_anchor(method, bm)


@pytest.mark.parametrize("anchor", [None, "", 7, ["ihp_sbump_anchor"]])
def test_a_malformed_fab_anchor_is_refused_before_bump_mirror_is_asked(anchor):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    import copy
    manifest = copy.deepcopy(h._import_interconnect_manifest().load_manifest())
    manifest["methods"]["sbump_sac305"]["fab_anchor"] = anchor
    asked = []
    bm = SimpleNamespace(fab_anchor_openings=lambda a: asked.append(a) or {})
    with pytest.raises(ValueError, match="fab_anchor must be a nonempty string"):
        h._check_landing_anchor("sbump_sac305", bm, manifest)
    assert asked == []


def test_the_anchor_check_asks_the_pad_cell_of_the_methods_own_anchor():
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    bm = _per_anchor_bm({
        "ihp_cupillar_anchor": {"passiv": (9, 35), "dfpad": (41, 35),
                                "recog": (99, 35)},
        "ihp_sbump_anchor": {"passiv": (9, 36), "dfpad": (41, 36),
                             "recog": (99, 36)}})
    assert h._check_landing_anchor("sbump_sac305", bm).pcell == "Pad_ihp_sbump_anchor"
    assert h._check_landing_anchor("cupillar_opt1", bm).pcell == "Pad_ihp_cupillar_anchor"


@pytest.mark.parametrize("bm,reason", [
    (None, "needs bump_mirror"),
    (SimpleNamespace(), "no pad-cell layer table"),
])
def test_an_anchor_check_without_the_pad_cell_table_refuses(bm, reason):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    with pytest.raises(ValueError, match=reason):
        h._check_landing_anchor("cupillar_opt1", bm)


def test_an_unknown_method_is_left_to_the_body_check():
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    assert h._check_landing_anchor("bogus", None) is None


def test_an_unreadable_manifest_is_a_typed_refusal(monkeypatch):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    im = h._import_interconnect_manifest()

    def unreadable(*args, **kwargs):
        raise RuntimeError("torn")
    monkeypatch.setattr(im, "load_manifest", unreadable)
    with pytest.raises(h.SourceRefused, match="fab anchor of 'cupillar_opt1'"):
        h._check_landing_anchor("cupillar_opt1", h._import_bump_mirror())


def test_an_unreadable_manifest_refuses_the_method_list(monkeypatch):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    im = h._import_interconnect_manifest()

    def unreadable(*args, **kwargs):
        raise RuntimeError("torn")
    monkeypatch.setattr(im, "load_manifest", unreadable)
    with pytest.raises(h.SourceRefused, match="the method list could not"):
        h.connection_method_availability()


def test_the_body_diameter_comes_from_the_manifest_given():
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    import copy
    manifest = copy.deepcopy(h._import_interconnect_manifest().load_manifest())
    manifest["methods"]["cupillar_opt1"]["body_diameter_um"] = 123.0
    assert h._connection_to_body_diameter("cupillar_opt1", manifest) == 123.0
    assert h._connection_to_body_diameter("cupillar_opt1") != 123.0


def _landing_reads(tmp_path, monkeypatch, refs):
    """(landing checks, manifest reads) of one cupillar_opt1 export with pad
    locations for ``refs``."""
    im = h._import_interconnect_manifest()
    loaded, checks, landings = [], [], []
    load, check = im.load_manifest, h._check_landing_anchor
    landing = h._method_landing

    def counted_load(*args, **kwargs):
        loaded.append(load(*args, **kwargs))
        return loaded[-1]

    def recorded_check(method, bm, manifest=None):
        checks.append((method, manifest))
        return check(method, bm, manifest)

    def recorded_landing(bm, im, method, cell, manifest):
        landings.append(manifest)
        return landing(bm, im, method, cell, manifest)
    out = tmp_path / ("%d.gds" % len(refs))
    hyp = tmp_path / "pillar.hyp"
    hyp.write_text(MIXED_HYP)
    with monkeypatch.context() as patch:
        patch.setattr(im, "load_manifest", counted_load)
        patch.setattr(h, "_check_landing_anchor", recorded_check)
        patch.setattr(h, "_method_landing", recorded_landing)
        assert h.convert_hyp_to_gds(
            hyp_path=str(hyp), output_path=str(out),
            lyp_path=h._find_default_lyp(),
            pad_locations={ref: _write_pin_list(tmp_path, ref)
                           for ref in refs},
            connection_type="cupillar_opt1") is True
    for method, manifest in checks:
        assert any(manifest is read for read in loaded), method
    # The landings are drawn from the read the checks used.
    assert landings and all(manifest is checks[0][1] for manifest in landings)
    return [method for method, _ in checks], len(loaded)


def test_an_export_checks_each_method_once_on_one_manifest(
        tmp_path, monkeypatch):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    one_checks, one_reads = _landing_reads(tmp_path, monkeypatch, ["U1"])
    two_checks, two_reads = _landing_reads(tmp_path, monkeypatch, ["U1", "U2"])
    assert one_checks == two_checks == ["cupillar_opt1"]
    # A second die on the same method reads the manifest no more often.
    assert two_reads == one_reads


PILLAR_TABLE = {"passiv": (9, 35), "dfpad": (41, 35), "recog": (99, 35)}


def _broken_bm(fab_anchor=None, openings=None):
    """A bump_mirror whose fab anchor API answers wrongly."""
    def default_anchor(anchor):
        return SimpleNamespace(pcell="CuPillarPad", cell_prefix="CUPILLAR",
                               table_6_1=True)
    return SimpleNamespace(
        fab_anchor=fab_anchor or default_anchor,
        fab_anchor_openings=openings or (lambda anchor: dict(PILLAR_TABLE)))


def _raise(exc):
    def raiser(*args):
        raise exc
    return raiser


# The pad cell of an interposer between its per-anchor openings and its
# per-anchor cell names: a FabAnchor without cell_prefix or table_6_1.
NAMELESS_PAD_BM = _broken_bm(
    fab_anchor=lambda anchor: SimpleNamespace(pcell="CuPillarPad", rules="Padc",
                                              layers={}))


@pytest.mark.parametrize("bm,kind", [
    (NAMELESS_PAD_BM, "AttributeError"),
    (_broken_bm(fab_anchor=_raise(KeyError("ihp_cupillar_anchor"))), "KeyError"),
    (_broken_bm(openings=_raise(TypeError("takes 0 arguments"))), "TypeError"),
    (_broken_bm(openings=lambda a: {"passiv": [9, 35]}), "TypeError"),
])
def test_a_bump_mirror_that_answers_wrongly_is_a_typed_refusal(bm, kind):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    with pytest.raises(h.SourceRefused, match="fab anchor API is unusable \\(%s"
                       % kind) as refused:
        h._check_landing_anchor("cupillar_opt1", bm)
    assert refused.value.exit_code == h.EXIT_SOURCE_UNREACHABLE


def test_a_bump_mirror_that_answers_wrongly_refuses_every_method(
        monkeypatch, capsys):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    import connection_methods
    monkeypatch.setattr(h, "_import_bump_mirror", lambda: NAMELESS_PAD_BM)
    availability = h.connection_method_availability()
    assert availability
    assert all("cell_prefix" in reason for reason in availability.values())
    assert connection_methods.main([]) == 0
    reported = connection_methods.parse_report(capsys.readouterr().out)
    assert reported == availability


def test_an_export_on_a_bump_mirror_that_answers_wrongly_is_refused(
        tmp_path, monkeypatch):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    monkeypatch.setattr(h, "_import_bump_mirror", lambda: NAMELESS_PAD_BM)
    hyp = tmp_path / "pillar.hyp"
    hyp.write_text(MIXED_HYP)
    out = tmp_path / "pillar_interposer.gds"
    with pytest.raises(h.SourceRefused, match="cell_prefix"):
        h.convert_hyp_to_gds(
            hyp_path=str(hyp), output_path=str(out),
            lyp_path=h._find_default_lyp(),
            pad_locations={"U1": _write_pin_list(tmp_path, "U1"),
                           "U2": _write_pin_list(tmp_path, "U2")},
            connection_type="cupillar_opt1")
    assert not out.exists()


@pytest.mark.parametrize("global_method,per_die", [
    ("sbump_sac305", {}),
    ("cupillar_opt1", {"U2": "sbump_sac305"}),
])
def test_a_solder_bump_die_is_refused_before_the_gds(
        tmp_path, capsys, monkeypatch, global_method, per_die):
    """A bump_mirror without the solder-bump pad cell: the die is refused,
    with or without carrier metadata, instead of exported without landings."""
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    monkeypatch.setattr(h, "_import_bump_mirror", lambda: CUPILLAR_ONLY_BM)
    hyp = tmp_path / "sbump.hyp"
    hyp.write_text(MIXED_HYP)
    out = tmp_path / "sbump_interposer.gds"
    ok = h.convert_hyp_to_gds(
        hyp_path=str(hyp), output_path=str(out),
        lyp_path=h._find_default_lyp(),
        pad_locations={"U1": _write_pin_list(tmp_path, "U1"),
                       "U2": _write_pin_list(tmp_path, "U2")},
        connection_type=global_method, die_connections=per_die,
    )
    assert ok is False
    assert not out.exists()
    err = capsys.readouterr().err
    assert "Cannot draw the landing of" in err
    assert "ihp_sbump_anchor opens 9/36, 41/36, 99/36" in err
    assert "cannot draw that landing" in err


SBUMP_OPENINGS = {(9, 36), (41, 36), (99, 36)}
PILLAR_OPENINGS = {(9, 35), (41, 35), (99, 35)}


@pytest.mark.parametrize("global_method,per_die", [
    ("sbump_sac305", {}),
    ("cupillar_opt1", {"U2": "sbump_sac305"}),
])
def test_a_solder_bump_die_lands_on_the_solder_bump_pad(
        tmp_path, capsys, monkeypatch, global_method, per_die):
    """With the SolderBumpPad cell the export draws the solder-bump openings
    under each sbump die, in its own SBUMPS_<ref> group, and keeps the
    Cu-pillar openings for a cupillar die in the same GDS."""
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    if not _per_anchor_pads(h._import_bump_mirror()):
        pytest.skip("this interposer PDK has no SolderBumpPad")
    checked = []
    check = h._check_intm4tm2_cni
    monkeypatch.setattr(h, "_check_intm4tm2_cni",
                        lambda pcell, *a: checked.append(pcell) or check(pcell, *a))
    hyp = tmp_path / "sbump.hyp"
    hyp.write_text(MIXED_HYP)
    out = tmp_path / "sbump_interposer.gds"
    ok = h.convert_hyp_to_gds(
        hyp_path=str(hyp), output_path=str(out),
        lyp_path=h._find_default_lyp(),
        pad_locations={"U1": _write_pin_list(tmp_path, "U1"),
                       "U2": _write_pin_list(tmp_path, "U2")},
        connection_type=global_method, die_connections=per_die,
    )
    assert ok is True
    # Each pad cell is checked to come from the IntM4TM2 library in use.
    assert "SolderBumpPad" in checked
    assert ("CuPillarPad" in checked) == bool(per_die)
    u2 = _cell_layers(str(out), "SBUMPS_U2")
    assert SBUMP_OPENINGS | {(134, 0)} <= u2
    assert not (PILLAR_OPENINGS & u2)
    if per_die:
        u1 = _cell_layers(str(out), "CUPILLARS_U1")
        assert PILLAR_OPENINGS | {(134, 0)} <= u1
        assert not (SBUMP_OPENINGS & u1)
    else:
        assert SBUMP_OPENINGS <= _cell_layers(str(out), "SBUMPS_U1")
    # Placed and checked against the solder-bump rule set (Padb) and the
    # method's own opening and pitch, not the Cu-pillar Option 2 defaults.
    import json
    drc = json.loads((tmp_path / "sbump_cupillar_drc.json").read_text())
    params = drc["methods"]["sbump_sac305"]["params"]
    assert params["Padb_a_diameter_um"] == 60.0
    assert params["Padb_c_min_enclosure_um"] == 10.0
    assert params["Padb_e_min_pitch_um"] == 130.0
    assert drc["summary"]["passed"] is True
    printed = capsys.readouterr()
    assert "Table 6.1" not in printed.out + printed.err
    # The report holds every method's landings, not just the Cu pillar's.
    assert "Landing DRC report:" in printed.out


def test_a_landing_names_its_pad_cell():
    """No default pad cell: a call that does not name it would check the
    Cu-pillar cell's cni while the generator draws another anchor's."""
    class Generator:
        def add_device_bumps(self, *args):
            raise AssertionError("drew without a pad cell")

    with pytest.raises(TypeError, match="pcell"):
        h._add_device_pillars(SimpleNamespace(), Generator(), "U1",
                              [], 44.0)
    with pytest.raises(TypeError):
        h._add_device_pillars(SimpleNamespace(), Generator(), "U1",
                              [], 44.0, "SolderBumpPad")


def test_the_landing_is_drawn_from_the_manifest_the_checks_read(monkeypatch):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    im, bm = h._import_interconnect_manifest(), h._import_bump_mirror()
    manifest = im.load_manifest()
    cell = h._check_landing_anchor("cupillar_opt1", bm, manifest)

    def reread(*args, **kwargs):
        raise AssertionError("the landing read the manifest again")
    monkeypatch.setattr(im, "load_manifest", reread)
    _generator, _params, diameter = h._method_landing(
        bm, im, "cupillar_opt1", cell, manifest)
    assert diameter == (
        manifest["methods"]["cupillar_opt1"]["body_diameter_um"])


def test_a_method_without_fab_params_refuses_the_export(
        tmp_path, capsys, monkeypatch):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    im = h._import_interconnect_manifest()

    def no_fab_params(method, *args, **kwargs):
        raise KeyError("%s declares no fab_params" % method)
    monkeypatch.setattr(im, "fab_params", no_fab_params)
    hyp = tmp_path / "nofab.hyp"
    hyp.write_text(MIXED_HYP)
    out = tmp_path / "nofab_interposer.gds"
    ok = h.convert_hyp_to_gds(
        hyp_path=str(hyp), output_path=str(out),
        lyp_path=h._find_default_lyp(),
        pad_locations={"U1": _write_pin_list(tmp_path, "U1")},
        connection_type="cupillar_opt1")
    assert ok is False
    assert ("Cannot draw carrier landing for cupillar_opt1: "
            "'cupillar_opt1 declares no fab_params'") in capsys.readouterr().err


def test_the_methods_command_agrees_with_the_worker_on_the_live_pdks():
    """connection_methods.py run as the dialog and run_export run it."""
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    from chiplet_export.pipeline import orchestrator
    availability, shown = orchestrator.query_connection_availability(
        sys.executable, str(PLUGIN_ROOT), orchestrator.ExportOptions())
    assert shown.endswith("%s %s" % (sys.executable,
                                     PLUGIN_ROOT / "connection_methods.py"))
    assert availability == h.connection_method_availability()
    refused = {m for m, reason in availability.items() if reason}
    if _per_anchor_pads(h._import_bump_mirror()):
        assert refused == set()
    else:
        assert refused == {"sbump_sac305"}
        assert "ihp_sbump_anchor opens 9/36, 41/36, 99/36" in availability["sbump_sac305"]
    for method in ("cupillar_opt1", "cupillar_opt2", "cupillar_opt3",
                   "vendorx_microbump"):
        assert availability[method] is None


def test_without_the_pad_cell_every_method_is_unavailable(monkeypatch):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    monkeypatch.setattr(h, "_import_bump_mirror", lambda: None)
    availability = h.connection_method_availability()
    assert availability
    assert all("needs bump_mirror" in reason for reason in availability.values())


def test_a_method_whose_entry_is_unusable_is_refused_alone(monkeypatch):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    check = h._check_landing_anchor

    def torn_entry(method, *args, **kwargs):
        if method == "cupillar_opt2":
            raise h.SourceRefused("cupillar_opt2: torn entry")
        return check(method, *args, **kwargs)
    monkeypatch.setattr(h, "_check_landing_anchor", torn_entry)
    availability = h.connection_method_availability()
    assert availability["cupillar_opt2"] == "cupillar_opt2: torn entry"
    assert availability["cupillar_opt1"] is None


def test_the_methods_command_prints_only_json_on_stdout(monkeypatch, capsys):
    import connection_methods

    def noisy():
        print("a PDK module talking while it loads")
        return {"cupillar_opt1": None}
    monkeypatch.setattr(h, "connection_method_availability", noisy)
    assert connection_methods.main([]) == 0
    out = capsys.readouterr()
    assert connection_methods.parse_report(out.out) == {"cupillar_opt1": None}
    assert "talking" in out.err


def test_an_unusable_method_list_is_a_typed_refusal(monkeypatch, capsys):
    use_sibling_checkouts("INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT")
    import connection_methods
    im = h._import_interconnect_manifest()

    def torn(*args, **kwargs):
        raise RuntimeError("torn")
    monkeypatch.setattr(im, "list_methods", torn)
    with pytest.raises(h.SourceRefused, match="the method list"):
        h.connection_method_availability()
    assert connection_methods.main([]) == h.EXIT_SOURCE_UNREACHABLE
    out = capsys.readouterr()
    assert out.out == "" and "the method list" in out.err


# ---------------------------------------------------------------------------
# Board outline -> prBoundary 235/0 (interposer extent = Edge.Cuts, not copper)
# ---------------------------------------------------------------------------

def _outline_dbbox(gds_path):
    """DBox of prBoundary 235/0 in the top cell, or None when absent."""
    from klayout import db
    layout = db.Layout()
    layout.read(gds_path)
    idx = layout.find_layer(*h.GDSGenerator.PRBOUNDARY_LAYER)
    if idx is None:
        return None
    bb = layout.top_cell().dbbox(idx)
    return None if bb.empty() else bb


def test_parser_reads_board_perimeter(tmp_path):
    """The {BOARD section's PERIMETER_SEGMENTs land in perimeter_segments."""
    hyp = tmp_path / "p.hyp"
    hyp.write_text(MIXED_HYP)
    p = h.HYPParser(str(hyp))
    p.parse()
    assert len(p.perimeter_segments) == 4
    s0 = p.perimeter_segments[0]
    assert (s0.x1, s0.y1, s0.x2, s0.y2) == (0.0, 0.0, 0.002, 0.0)


def test_convert_draws_outline_on_prboundary(tmp_path, monkeypatch):
    """The board outline (2000x1000 um in MIXED_HYP) is drawn as a closed
    polygon on prBoundary 235/0 of the generated interposer GDS."""
    use_sibling_checkouts("INTERPOSER_PDK_ROOT")
    hyp = tmp_path / "o.hyp"
    hyp.write_text(MIXED_HYP)
    out = tmp_path / "o_interposer.gds"
    ok = h.convert_hyp_to_gds(
        hyp_path=str(hyp), output_path=str(out),
        lyp_path=h._find_default_lyp())
    assert ok is True
    bb = _outline_dbbox(str(out))
    assert bb is not None
    assert (bb.left, bb.bottom, bb.right, bb.top) == (0.0, -1000.0, 2000.0, 0.0)


def test_open_outline_warns_and_draws_nothing(tmp_path, monkeypatch, capsys):
    """A perimeter that does not close draws no prBoundary (all-or-nothing:
    a partial outline would understate the extent) and warns loudly."""
    use_sibling_checkouts("INTERPOSER_PDK_ROOT")
    open_hyp = MIXED_HYP.replace(
        "  (PERIMETER_SEGMENT X1=0.000000 Y1=-0.001000 "
        "X2=0.000000 Y2=0.000000)\n", "")
    assert open_hyp != MIXED_HYP  # the fixture line must exist
    hyp = tmp_path / "open.hyp"
    hyp.write_text(open_hyp)
    out = tmp_path / "open_interposer.gds"
    ok = h.convert_hyp_to_gds(
        hyp_path=str(hyp), output_path=str(out),
        lyp_path=h._find_default_lyp())
    assert ok is True
    assert "does not close" in capsys.readouterr().err
    assert _outline_dbbox(str(out)) is None


def test_offboard_geometry_warns(tmp_path, monkeypatch, capsys):
    """Copper sticking out of the board outline is reported loudly (the
    interposer keeps the outline size; the leak is a design error)."""
    use_sibling_checkouts("INTERPOSER_PDK_ROOT")
    leaky = MIXED_HYP.replace("X2=0.001900", "X2=0.002500")
    assert leaky != MIXED_HYP
    hyp = tmp_path / "leak.hyp"
    hyp.write_text(leaky)
    out = tmp_path / "leak_interposer.gds"
    ok = h.convert_hyp_to_gds(
        hyp_path=str(hyp), output_path=str(out),
        lyp_path=h._find_default_lyp())
    assert ok is True
    err = capsys.readouterr().err
    assert "outside the board outline" in err
    assert "right" in err


# The interposer-outline tests below assert interposer fields only, so their
# .chiplet has no dies (a die would need its own GDS and outline).
INTERPOSER_ONLY_CHIPLET = """\
format_version: "1.0"
assembly:
  name: mixed
components:
  - id: interposer
    type: interposer
    dimensions: {width: 1000.0, height: 1000.0, thickness: 100.0}
    position: {x: 0.0, y: 0.0, z: 0.0}
"""


def test_update_chiplet_prefers_outline_dims_keeps_full_bbox_position(
        tmp_path):
    """Dimensions come from the outline (the fab extent); position stays the
    full-bbox center (anchor: bbox_center mesh contract). Distinguishable
    only when copper leaks outside the outline."""
    import yaml
    from klayout import db
    gds = tmp_path / "i.gds"
    ly = db.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    top.shapes(ly.layer(134, 0)).insert(db.DBox(0.0, 0.0, 3000.0, 1000.0))
    top.shapes(ly.layer(*h.GDSGenerator.PRBOUNDARY_LAYER)).insert(
        db.DBox(0.0, 0.0, 2000.0, 1000.0))
    ly.write(str(gds))
    p = tmp_path / "d.chiplet"
    p.write_text(INTERPOSER_ONLY_CHIPLET)
    assert h.update_chiplet_file(str(p), str(gds)) is True  # bbox from file
    data = yaml.safe_load(p.read_text())
    ip = next(c for c in data["components"] if c["id"] == "interposer")
    assert ip["dimensions"]["width"] == 2000.0   # outline, not copper
    assert ip["dimensions"]["height"] == 1000.0
    assert ip["position"]["x"] == 1500.0         # full-bbox center
    assert ip["position"]["y"] == 500.0
    assert ip["anchor"] == "bbox_center"


def test_update_chiplet_falls_back_to_full_bbox_without_outline(tmp_path):
    """A GDS without prBoundary keeps the legacy drawn-geometry sizing."""
    import yaml
    from klayout import db
    gds = tmp_path / "i.gds"
    ly = db.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    top.shapes(ly.layer(134, 0)).insert(db.DBox(0.0, 0.0, 3000.0, 1000.0))
    ly.write(str(gds))
    p = tmp_path / "d.chiplet"
    p.write_text(INTERPOSER_ONLY_CHIPLET)
    assert h.update_chiplet_file(str(p), str(gds)) is True
    data = yaml.safe_load(p.read_text())
    ip = next(c for c in data["components"] if c["id"] == "interposer")
    assert ip["dimensions"]["width"] == 3000.0
    assert ip["dimensions"]["height"] == 1000.0
    assert ip["position"]["x"] == 1500.0
    assert ip["position"]["y"] == 500.0


def test_update_chiplet_missing_outline_warns_loudly(tmp_path, capsys):
    """A GDS without any prBoundary must not degrade silently: the consumer
    warns (stderr) that dimensions came from copper, not the fab outline, and
    that placement can shift. With no legacy outline either, it points at
    Edge.Cuts."""
    from klayout import db
    gds = tmp_path / "i.gds"
    ly = db.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    top.shapes(ly.layer(134, 0)).insert(db.DBox(0.0, 0.0, 3000.0, 1000.0))
    ly.write(str(gds))
    p = tmp_path / "d.chiplet"
    p.write_text(INTERPOSER_ONLY_CHIPLET)
    assert h.update_chiplet_file(str(p), str(gds)) is True
    err = capsys.readouterr().err
    assert "prBoundary 235/0 outline not found" in err
    assert "can shift" in err
    assert "Edge.Cuts" in err
    assert "legacy" not in err  # nothing on 189/0 -> no migration hint


def test_update_chiplet_missing_outline_flags_legacy_189(tmp_path, capsys):
    """A pre-migration GDS carries the outline on 189/0. The consumer looks on
    235/0, degrades, and names the legacy layer + the fix (regenerate)."""
    import yaml
    from klayout import db
    gds = tmp_path / "i.gds"
    ly = db.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    top.shapes(ly.layer(134, 0)).insert(db.DBox(0.0, 0.0, 3000.0, 1000.0))
    top.shapes(ly.layer(189, 0)).insert(db.DBox(0.0, 0.0, 2000.0, 1000.0))
    ly.write(str(gds))
    p = tmp_path / "d.chiplet"
    p.write_text(INTERPOSER_ONLY_CHIPLET)
    assert h.update_chiplet_file(str(p), str(gds)) is True
    err = capsys.readouterr().err
    assert "prBoundary 235/0 outline not found" in err
    assert "189/0" in err
    assert "Regenerate" in err
    # dimensions still fall back to the drawn-geometry bbox (235/0 is absent)
    data = yaml.safe_load(p.read_text())
    ip = next(c for c in data["components"] if c["id"] == "interposer")
    assert ip["dimensions"]["width"] == 3000.0
