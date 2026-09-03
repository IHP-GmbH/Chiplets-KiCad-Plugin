# SPDX-License-Identifier: GPL-3.0-or-later
"""
Host-side tests for writers/connection_stacks.py (no pcbnew).

Byte-exact: the manifest-driven connection_stacks block must reproduce the
literal the writer used to hardcode (and that export_chiplet.cpp still
emits) -- guards the byte-exact writer-parity gate (47.7b).

Validation: interconnect ids (INTERCONNECT_ADAPTER text var, per-die
CONNECTION fields) are checked against the manifest at export time.
"""

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_export.writers.connection_stacks import (  # noqa: E402
    ADAPTER_ID_RE,
    _manifest_reader,
    emit_connection_stacks_block,
    emit_interconnect_block,
    validate_adapter_id,
    validate_interconnect_ids,
)

import chiplet_export.writers.connection_stacks as connection_stacks  # noqa: E402

# Manifest-dependent tests need the interconnect PDK (env var or sibling
# checkout). On a lone checkout (e.g. a bare CI runner) they skip; the
# missing-manifest behavior itself is tested via monkeypatch below.
try:
    _manifest_reader()
    _HAVE_MANIFEST = True
except ImportError:
    _HAVE_MANIFEST = False

needs_manifest = pytest.mark.skipif(
    not _HAVE_MANIFEST,
    reason="interconnect_pdk manifest not discoverable on this checkout")


# Transcribed verbatim from the pre-split literal (chiplet_writer.py:274-293
# / export_chiplet.cpp). The f.write("\n") separator the caller adds afterwards
# is NOT part of this block.
EXPECTED_BLOCK = (
    "connection_stacks:\n"
    "  cupillar_opt1:\n"
    '    description: "PacTech Cu Pillar, Table 6.1 Option 1 (35um opening)"\n'
    "    layers:\n"
    "      - {name: CuPillar, material: Cu, height: 28.0, diameter: 44.0}\n"
    "      - {name: SnAgCap, material: SnAg, height: 16.0, diameter: 44.0}\n"
    "  cupillar_opt2:\n"
    '    description: "PacTech Cu Pillar, Table 6.1 Option 2 (40um opening)"\n'
    "    layers:\n"
    "      - {name: CuPillar, material: Cu, height: 32.0, diameter: 49.0}\n"
    "      - {name: SnAgCap, material: SnAg, height: 16.0, diameter: 49.0}\n"
    "  cupillar_opt3:\n"
    '    description: "PacTech Cu Pillar, Table 6.1 Option 3 (45um opening)"\n'
    "    layers:\n"
    "      - {name: CuPillar, material: Cu, height: 42.0, diameter: 54.0}\n"
    "      - {name: SnAgCap, material: SnAg, height: 19.0, diameter: 54.0}\n"
    "  sbump_sac305:\n"
    '    description: "PacTech SAC305 solder bump (80um ball)"\n'
    "    layers:\n"
    "      - {name: SolderBall, material: SAC305, height: 80.0, diameter: 80.0}\n"
)


@needs_manifest
def test_connection_stacks_block_byte_exact():
    assert emit_connection_stacks_block() == EXPECTED_BLOCK


@needs_manifest
def test_vendorx_excluded_from_default_library():
    """The non-IHP demo method must not leak into the default emitted block."""
    block = emit_connection_stacks_block()
    assert "vendorx" not in block.lower()
    assert "VendorXBump" not in block


def test_interconnect_block_empty_when_no_adapter():
    assert emit_interconnect_block("") == ""
    assert emit_interconnect_block(None) == ""


def test_interconnect_block_mirrors_interposer():
    assert emit_interconnect_block("ihp_cupillar") == (
        'interconnect:\n  adapter: "ihp_cupillar"\n\n'
    )


# ---------------------------------------------------------------------------
# Interconnect-id validation at export time
# ---------------------------------------------------------------------------

def test_validate_noop_when_nothing_requested():
    """No adapter, no methods: never touches the manifest, never raises."""
    validate_interconnect_ids()
    validate_interconnect_ids(adapter="", die_methods=[])


@needs_manifest
def test_validate_known_ids_pass():
    validate_interconnect_ids(adapter="vendorx_microbump",
                              die_methods=["cupillar_opt2"])


@needs_manifest
def test_validate_unknown_adapter_fails_listing_valid():
    with pytest.raises(ValueError) as exc:
        validate_interconnect_ids(adapter="ihp_cupillar_typo")
    msg = str(exc.value)
    assert "ihp_cupillar_typo" in msg
    assert "ihp_cupillar" in msg  # the valid set is listed


@needs_manifest
def test_validate_unknown_method_fails_listing_valid():
    with pytest.raises(ValueError) as exc:
        validate_interconnect_ids(die_methods=["cupillar_opt9"])
    msg = str(exc.value)
    assert "cupillar_opt9" in msg
    assert "cupillar_opt1" in msg  # the valid set is listed


def test_validate_warns_without_manifest(monkeypatch, capsys):
    """Undiscoverable manifest degrades to one warning (machine-local
    .chiplet; studio and the assembly DRC re-validate downstream)."""
    def _raise():
        raise ImportError("interconnect_pdk reader not found")

    monkeypatch.setattr(connection_stacks, "_manifest_reader", _raise)
    connection_stacks.validate_interconnect_ids(adapter="anything")
    err = capsys.readouterr().err
    assert "skipping adapter/method validation" in err


# ---------------------------------------------------------------------------
# validate_adapter_id: the producer-validates gate.
#
# A .chiplet is where adapter ids enter the ecosystem, so the authoritative
# check is at emit. These run without pcbnew and without the interconnect PDK,
# deliberately: the shape gate must be exercised on a bare CI runner, which is
# exactly where the membership check below it is skipped.
# ---------------------------------------------------------------------------

VALID_IDS = [
    "intm4tm2",            # the interposer default
    "ihp_cupillar",        # the three real interconnect adapters
    "ihp_sbump",
    "vendorx_microbump",
    "a",                   # single char, minimal
    "_leading_underscore",
    "A1",
    "with.dots",
    "with-dashes",
    "mixed_1.2-3",
]

# Every one of these is either a path, reaches for one, or is a substitution
# token. The regex has to reject all of them without knowing what a path is.
INVALID_IDS = [
    "/etc/passwd",             # absolute path
    "./local",                 # explicit relative
    "../escape",               # traversal
    "..",                      # bare traversal
    "a/b",                     # any separator at all
    "~/adapters/x",            # home expansion
    "-leading-dash",           # leading dash
    ".leading-dot",            # leading dot / hidden file
    "${HOME}",                 # text-var substitution left unexpanded
    "$HOME",
    "has space",
    "trailing\n",              # newline would break the YAML line
    "quote\"break",            # would escape the emitted double-quoted scalar
    "semi;colon",
    "C:\\adapters\\x",         # windows path
]


@pytest.mark.parametrize("value", VALID_IDS)
def test_validate_adapter_id_accepts_well_formed(value):
    validate_adapter_id(value, "test")
    assert ADAPTER_ID_RE.match(value)


@pytest.mark.parametrize("value", INVALID_IDS)
def test_validate_adapter_id_rejects_paths_and_junk(value):
    with pytest.raises(ValueError) as exc:
        validate_adapter_id(value, "INTERPOSER_ADAPTER text variable")
    # The message has to name the offending value and where it came from,
    # otherwise the user cannot find which text variable to fix.
    assert repr(value) in str(exc.value)
    assert "INTERPOSER_ADAPTER text variable" in str(exc.value)


@pytest.mark.parametrize("value", ["", None])
def test_validate_adapter_id_treats_empty_as_unset(value):
    """Empty means "not set"; defaulting is the caller's job, not ours."""
    validate_adapter_id(value, "test")


def test_shape_gate_still_fires_when_the_manifest_is_gone(monkeypatch):
    """The hole this closes.

    validate_interconnect_ids degrades to a warning when the interconnect PDK
    is undiscoverable, so on a host without the PDK it waves everything
    through. The shape gate must not degrade with it, or a path-shaped
    INTERCONNECT_ADAPTER reaches the emitted document unchecked.
    """
    def _raise():
        raise ImportError("interconnect_pdk reader not found")

    monkeypatch.setattr(connection_stacks, "_manifest_reader", _raise)

    # Membership check: degrades, accepts anything (existing behaviour).
    connection_stacks.validate_interconnect_ids(adapter="/etc/passwd")

    # Shape check: does not degrade.
    with pytest.raises(ValueError):
        validate_adapter_id("/etc/passwd", "INTERCONNECT_ADAPTER text variable")
