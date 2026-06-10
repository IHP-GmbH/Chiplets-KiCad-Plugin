# SPDX-License-Identifier: GPL-2.0-or-later
"""
Loud guard for unmapped board layers in hyp_to_gds.

A board whose copper layers keep the KiCad default names (F.Cu, In1.Cu, ...)
maps no trace onto any PDK metal; the conversion used to emit a near-empty
GDS with exit 0. The guard fails the conversion when more than
UNMAPPED_FAIL_FRACTION of the trace elements sit on unmapped layers, and
tolerates stray layers below the threshold with one aggregate warning.

No pcbnew/wx dependency: runs on host.
"""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402


def _write_hyp(path, layer_segments):
    """Synthetic board: a perimeter plus N parallel segments per layer.

    layer_segments: list of (layer_name, segment_count).
    """
    lines = [
        "{VERSION=2.14}",
        "{UNITS=METRIC LENGTH}",
        "",
        '{BOARD "synthetic"',
        "  (PERIMETER_SEGMENT X1=0.000000 Y1=0.000000 X2=0.002000 Y2=0.000000)",
        "  (PERIMETER_SEGMENT X1=0.002000 Y1=0.000000 X2=0.002000 Y2=-0.001000)",
        "  (PERIMETER_SEGMENT X1=0.002000 Y1=-0.001000 X2=0.000000 Y2=-0.001000)",
        "  (PERIMETER_SEGMENT X1=0.000000 Y1=-0.001000 X2=0.000000 Y2=0.000000)",
        "}",
        "",
        "{STACKUP",
    ]
    for layer, _ in layer_segments:
        lines.append(
            '  (SIGNAL T=3.5e-05 P=0 C=1.724e-08 L="%s" M=COPPER)' % layer)
    lines.append("}")
    lines.append("")
    n = 0
    for layer, count in layer_segments:
        lines.append('{NET="n_%s"' % layer.replace(".", "_"))
        for _ in range(count):
            y = -0.000100 - 0.000010 * n
            n += 1
            lines.append(
                "  (SEG X1=0.000100 Y1=%.6f X2=0.001900 Y2=%.6f "
                'W=0.0000040000 L="%s")' % (y, y, layer))
        lines.append("}")
    path.write_text("\n".join(lines) + "\n")


def test_all_segments_unmapped_fails_loudly(tmp_path, capsys):
    """KiCad default copper names: refuse the near-empty GDS, name the fix."""
    hyp = tmp_path / "default_names.hyp"
    _write_hyp(hyp, [("F.Cu", 12), ("B.Cu", 8)])
    out = tmp_path / "default_names.gds"

    ok = h.convert_hyp_to_gds(
        hyp_path=str(hyp), output_path=str(out),
        lyp_path=h._find_default_lyp())

    assert ok is False
    assert not out.exists()
    err = capsys.readouterr().err
    assert "F.Cu" in err
    assert "B.Cu" in err
    assert "TopMetal2" in err  # the PDK names the user must adopt
    assert "template" in err   # pointer to the starting point


def test_minor_unmapped_layer_warns_but_succeeds(tmp_path, capsys):
    """A stray layer below the threshold stays tolerated (one warning)."""
    hyp = tmp_path / "stray.hyp"
    _write_hyp(hyp, [("TopMetal2", 9), ("F.Cu", 1)])
    out = tmp_path / "stray.gds"

    ok = h.convert_hyp_to_gds(
        hyp_path=str(hyp), output_path=str(out),
        lyp_path=h._find_default_lyp())

    assert ok is True
    assert out.exists()
    out_text = capsys.readouterr().out
    assert "skipped 1 of 10 trace element(s)" in out_text


def test_fully_mapped_no_aggregate_warning(tmp_path, capsys):
    """A clean board emits neither the guard error nor the aggregate warning."""
    hyp = tmp_path / "clean.hyp"
    _write_hyp(hyp, [("TopMetal2", 10)])
    out = tmp_path / "clean.gds"

    ok = h.convert_hyp_to_gds(
        hyp_path=str(hyp), output_path=str(out),
        lyp_path=h._find_default_lyp())

    assert ok is True
    assert out.exists()
    captured = capsys.readouterr()
    assert "skipped" not in captured.out
    assert "ERROR" not in captured.err
