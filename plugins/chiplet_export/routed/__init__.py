# SPDX-License-Identifier: GPL-3.0-or-later
"""The export's intermediates from the routed documents, without a board.

The board path computes six things from a loaded board before it starts the
worker: the intermediate .chiplet, the die pin lists, the die connections and
thicknesses, and the CMIM and no-fill sidecars. This package computes the same
from the routed-geometry document, the elaborated .chiplet and the routed
io_pads manifest. The .chiplet goes through the board path's own emitter
(writers.intermediate_chiplet), so both paths write it with one writer.

Nothing here imports pcbnew, directly or through chiplet_export_action,
writers.chiplet_writer, writers.hyperlynx_writer or pipeline.orchestrator.
"""

from .document import RoutedRefused, read_document
from .intermediates import (
    BOARD_EDGE_STROKE_UM, PreparedIntermediates, RoutedIntermediates,
    die_connections, die_pin_lists, die_thicknesses, facts_from_documents,
    prepare_intermediates, write_intermediates, write_pin_lists,
)

__all__ = [
    "BOARD_EDGE_STROKE_UM", "PreparedIntermediates", "RoutedIntermediates",
    "RoutedRefused", "die_connections", "die_pin_lists", "die_thicknesses",
    "facts_from_documents", "prepare_intermediates", "read_document",
    "write_intermediates", "write_pin_lists",
]
