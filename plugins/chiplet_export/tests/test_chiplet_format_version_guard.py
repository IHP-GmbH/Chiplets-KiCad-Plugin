# SPDX-License-Identifier: GPL-3.0-or-later
"""H-B in the plugin: the vendored guarded loader and the writer's stamped version.

Two things the plugin owns:

1. hyp_to_gds routes its two ``.chiplet`` reads through the vendored
   ``chiplet_format_io`` (the single guarded path). hyp_to_gds itself imports
   ``klayout.db`` at module load, so it cannot be imported on a host without
   KLayout; here we exercise the vendored reader the exact way the worker's
   ``_vendored_cfio()`` helper reaches it, which is what those reads depend on.

2. The from-scratch KiCad writer is a LOSSY writer, so it stamps the supported
   version as a bare literal. It is byte-exact-locked to the KiCad fork's
   ``export_chiplet.cpp``; this pins the literal so a drift is caught without
   needing pcbnew.
"""
import sys
import warnings
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

CHIPLET_EXPORT_ROOT = Path(__file__).resolve().parents[1]
VENDOR = CHIPLET_EXPORT_ROOT / "vendor"


@pytest.fixture()
def cfio():
    # Reach the vendored reader exactly as hyp_to_gds._vendored_cfio() does.
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    import chiplet_format_io as _cfio
    _cfio._reset_version_warnings()
    return _cfio


def test_vendored_reader_has_the_tolerant_policy(cfio):
    assert "check_format_version" in cfio.__all__
    assert cfio.SUPPORTED_FORMAT_VERSION == "1.0"


def test_vendored_reader_accepts_intermediate(cfio):
    doc = ('format_version: "1.0"\n_metadata:\n  finalize_required: true\n'
           'assembly:\n  name: a\n')
    # the finalizer reads with allow_intermediate=True
    data = cfio.loads(doc, allow_intermediate=True)
    assert data["assembly"]["name"] == "a"
    # and refuses it without the flag (the guard the bare safe_load never had)
    with pytest.raises(cfio.ChipletFormatError):
        cfio.loads(doc)


def test_vendored_reader_rejects_higher_major(cfio):
    with pytest.raises(cfio.ChipletFormatError):
        cfio.loads('format_version: "2.0"\nassembly:\n  name: a\n')


def test_vendored_finalizer_write_stamp_preserves_higher_minor(cfio):
    # The plugin finalizer stamps data['format_version'] via check_format_version
    # right before dumping; mimic that on a higher-minor input.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert cfio.check_format_version("1.1") == "1.1"   # preserved, not "1.0"
        assert cfio.check_format_version("1.0") == "1.0"   # equal -> supported


def test_kicad_writer_literal_is_pinned():
    # Byte-exact-coordinated with kicad/pcbnew/exporters/export_chiplet.cpp: both
    # emit `format_version: "1.0"` verbatim. The plugin writer builds YAML from
    # board state (a lossy, from-scratch writer), so it stamps the supported
    # version rather than echoing any input. If the format baseline ever moves,
    # move BOTH writers and this pin together.
    src = (CHIPLET_EXPORT_ROOT / "writers" / "chiplet_writer.py").read_text(
        encoding="utf-8")
    assert 'f.write(\'format_version: "1.0"\\n\\n\')' in src
