# SPDX-License-Identifier: GPL-2.0-or-later
"""
Hyperlynx .hyp writer.

Replicates kicad/pcbnew/exporters/export_hyperlynx.cpp (metric/meters
variant) in pure Python via the pcbnew SWIG bindings. Used by the
chiplet export pipeline as the .hyp input for hyp_to_gds.py.

Implementation lands in Gate 47.4. This port is a derivative work of
KiCad's GPL-2.0-or-later Hyperlynx exporter
(Copyright (C) 2019 CERN and KiCad Developers). See LICENSE.
"""


def write_hyperlynx(board, output_path):
    """Write `board` to `output_path` as a metric Hyperlynx .hyp file.

    Args:
        board: pcbnew.BOARD instance.
        output_path: Filesystem path for the .hyp output.

    Returns:
        True on success, False otherwise.
    """
    raise NotImplementedError("Gate 47.4 placeholder")
