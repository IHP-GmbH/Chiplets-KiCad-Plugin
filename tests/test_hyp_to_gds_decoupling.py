# SPDX-License-Identifier: GPL-2.0-or-later
"""
Host-side tests for hyp_to_gds connection-stack decoupling from the interconnect
PDK manifest. hyp_to_gds has no pcbnew/wx dependency, so these run on host.

Guards 0-regression: the manifest-sourced tables must reproduce the prior IHP
literals exactly, while the vendor demo method becomes selectable.
"""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402


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
