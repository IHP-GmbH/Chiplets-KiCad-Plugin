# SPDX-License-Identifier: GPL-3.0-or-later
"""
Host-side tests for hyp_to_gds connection-stack decoupling from the interconnect
PDK manifest. hyp_to_gds has no pcbnew/wx dependency, so these run on host.

Guards 0-regression: the manifest-sourced tables must reproduce the prior IHP
literals exactly, while the vendor demo method becomes selectable.
"""

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402

# This module asserts real sibling-PDK content: interconnect manifest methods,
# interposer canonical lyp paths, gds_to_kicad walk targets. On a lone
# checkout (e.g. a bare CI runner) none of those roots resolve -- skip the
# module instead of failing on missing ecosystem checkouts.
_MISSING_ROOTS = [
    var for var in ("INTERCONNECT_PDK_ROOT", "INTERPOSER_PDK_ROOT",
                    "GDS_TO_KICAD_ROOT")
    if h._discover_path_var(var) is None
]
pytestmark = pytest.mark.skipif(
    bool(_MISSING_ROOTS),
    reason="needs sibling ecosystem checkouts; unresolved: %s"
           % ", ".join(_MISSING_ROOTS))


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
    # Solder bump -> no single pillar body -> None (skip pillar gen).
    assert h._connection_to_body_diameter("sbump_sac305") is None
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

def test_interposer_pdk_python_found_via_walk():
    """With no env override, the upward walk finds the sibling checkout."""
    import os
    old = os.environ.pop("INTERPOSER_PDK_ROOT", None)
    try:
        found = h._find_interposer_pdk_python()
        assert found is not None
        assert (found / "bump_mirror.py").is_file()
        assert found.parts[-3:] == ("libs.tech", "klayout", "python")
    finally:
        if old is not None:
            os.environ["INTERPOSER_PDK_ROOT"] = old


def test_interposer_pdk_env_override_wins(tmp_path, monkeypatch):
    """INTERPOSER_PDK_ROOT pointing at a valid root takes precedence; a
    bogus root falls through to the walk instead of failing."""
    fake = tmp_path / "pdk" / "libs.tech" / "klayout" / "python"
    fake.mkdir(parents=True)
    (fake / "bump_mirror.py").write_text("# stub\n")
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(tmp_path / "pdk"))
    assert h._find_interposer_pdk_python() == fake

    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(tmp_path / "nonexistent"))
    found = h._find_interposer_pdk_python()
    assert found is not None and found != fake  # walk found the real one


@pytest.mark.skipif(
    not any((c / "interconnect_manifest.py").is_file()
            for c in h._interconnect_python_candidates()),
    reason="interconnect_pdk checkout not discoverable (lone checkout)")
def test_interconnect_pdk_resolves_live_not_fallback(monkeypatch):
    """The interconnect PDK probes resolve the real sibling checkout under
    its IHP layout (libs.tech/klayout/python). Guards against a silent
    fall-back to builtin tables if the layout moves again: the manifest
    reader must import via the walk alone."""
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
    monkeypatch.delenv("INTERCONNECT_PDK_ROOT", raising=False)
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


def test_expand_path_vars_walk(monkeypatch):
    """${GDS_TO_KICAD_ROOT} resolves via the sibling walk when env is unset."""
    monkeypatch.delenv("GDS_TO_KICAD_ROOT", raising=False)
    got = h._expand_path_vars("${GDS_TO_KICAD_ROOT}/pdks/sg13g2.lyp")
    assert "${" not in got
    assert got.endswith("/gds_to_kicad/pdks/sg13g2.lyp")
    assert Path(got).is_file()


def test_expand_path_vars_env_wins(tmp_path, monkeypatch):
    """A valid env root takes precedence; a bogus one falls through to walk."""
    fake = tmp_path / "pdk"
    (fake / "libs.tech" / "klayout").mkdir(parents=True)
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(fake))
    got = h._expand_path_vars(
        "${INTERPOSER_PDK_ROOT}/libs.tech/klayout/tech/intm4tm2.lyp")
    assert got.startswith(str(fake))

    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(tmp_path / "nonexistent"))
    got = h._expand_path_vars("${INTERPOSER_PDK_ROOT}/x")
    assert "${" not in got
    assert not got.startswith(str(tmp_path))


def test_expand_path_vars_unknown_var_is_loud(monkeypatch):
    """An unresolvable variable must hard-fail naming the variable."""
    import pytest
    monkeypatch.delenv("NO_SUCH_ECOSYSTEM_ROOT", raising=False)
    with pytest.raises(SystemExit) as exc:
        h._expand_path_vars("${NO_SUCH_ECOSYSTEM_ROOT}/foo.gds")
    assert "NO_SUCH_ECOSYSTEM_ROOT" in str(exc.value)


def test_default_lyp_resolves_canonical(monkeypatch):
    """With the monorepo present, the default lyp is the interposer PDK's
    canonical copy (the .lyp belongs to the PDK; the plugin keeps no copy)."""
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
name: mixed
components:
  - id: interposer
    type: interposer
    dimensions: {width: 1000.0, height: 1000.0, thickness: 100.0}
    position: {x: 0.0, y: 0.0, z: 0.0}
  - id: U1
    type: die
    orientation: flip_chip
    dimensions: {width: 100.0, height: 100.0, thickness: 0.0}
    position: {x: 10.0, y: 10.0, z: 0.0}
  - id: U2
    type: die
    orientation: flip_chip
    dimensions: {width: 100.0, height: 100.0, thickness: 0.0}
    position: {x: 200.0, y: 10.0, z: 0.0}
"""


def _update_mixed(tmp_path, **kwargs):
    import yaml
    p = tmp_path / "mixed.chiplet"
    p.write_text(MIXED_CHIPLET)
    ok = h.update_chiplet_file(
        str(p), "/nonexistent.gds", bbox=(0.0, 0.0, 1000.0, 1000.0), **kwargs)
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
    # opt1 stack 28+16=44 on 13.83; vendorx 18+6=24 on 13.83
    assert _die(data, "U1")["position"]["z"] == 13.83 + 44.0
    assert _die(data, "U2")["position"]["z"] == 13.83 + 24.0


def test_update_chiplet_per_die_without_global(tmp_path):
    """die_connections alone (no --connection-type) sets only listed dies."""
    data = _update_mixed(
        tmp_path, connection_type="",
        die_connections={"U2": "cupillar_opt2"})
    assert "connection" not in _die(data, "U1")
    assert _die(data, "U2")["connection"] == "cupillar_opt2"
    assert _die(data, "U2")["position"]["z"] == 13.83 + 48.0


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
    assert _die(data, "U1")["position"]["z"] == 13.83 + 44.0
    assert _die(data, "U2")["position"]["z"] == 13.83 + 44.0


def test_update_chiplet_die_thickness_without_connections(tmp_path):
    """Thickness applies independently of any connection selection."""
    data = _update_mixed(
        tmp_path, die_thicknesses={"U1": 250.0, "U2": 750.0})
    assert _die(data, "U1")["dimensions"]["thickness"] == 250.0
    assert _die(data, "U2")["dimensions"]["thickness"] == 750.0


def test_update_chiplet_die_thickness_never_touches_interposer(tmp_path):
    """The interposer's thickness encodes the attachment-surface z (the
    z-mounting fallback surface) -- die_thicknesses only targets components
    of type die, so an interposer entry is ignored by construction."""
    data = _update_mixed(
        tmp_path, die_thicknesses={"interposer": 999.0, "U1": 750.0})
    interposer = next(c for c in data["components"]
                      if c.get("type") == "interposer")
    assert interposer["dimensions"]["thickness"] == 13.83
    assert _die(data, "U1")["dimensions"]["thickness"] == 750.0


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
    monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
    monkeypatch.delenv("INTERCONNECT_PDK_ROOT", raising=False)
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
    monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
    monkeypatch.delenv("INTERCONNECT_PDK_ROOT", raising=False)
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
    monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
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
    monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
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
    monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
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
    p.write_text(MIXED_CHIPLET)
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
    p.write_text(MIXED_CHIPLET)
    assert h.update_chiplet_file(str(p), str(gds)) is True
    data = yaml.safe_load(p.read_text())
    ip = next(c for c in data["components"] if c["id"] == "interposer")
    assert ip["dimensions"]["width"] == 3000.0
    assert ip["dimensions"]["height"] == 1000.0
    assert ip["position"]["x"] == 1500.0
    assert ip["position"]["y"] == 500.0
