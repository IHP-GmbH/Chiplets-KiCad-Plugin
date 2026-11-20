# SPDX-License-Identifier: GPL-3.0-or-later
"""Where an export's inputs come from.

run_export composes the same way whatever it composes from; what differs is
where the stem, the pre-worker intermediates and the preflights come from. A
source supplies exactly those. BoardSource reads them from a KiCad board, as
the export always has.

Every reader is looked up when it is called, through sys.modules, as the
from-imports in run_export used to: callers and tests replace the writer
functions on their modules, or the modules themselves in sys.modules, and the
export must see the replacement. (``from ..writers import chiplet_writer`` would
read the package attribute instead, which a sys.modules swap does not change.) Nothing here imports pcbnew;
the readers do that themselves, when a board is read.
"""

import importlib
from pathlib import Path


class BoardSource:
    """The export's inputs, read from a KiCad board (``pcbnew.BOARD``)."""

    def __init__(self, board):
        self.board = board

    @property
    def discovery_board(self):
        """The board the discovery chain may read project text variables from."""
        return self.board

    def name(self):
        """The stem every output is named by: the board file's, else "board"."""
        board_file = ""
        try:
            board_file = self.board.GetFileName() or ""
        except Exception:
            pass
        return Path(board_file).stem or "board"

    def load_readers(self):
        """Import the board readers; both writer modules import pcbnew."""
        _module("writers.chiplet_writer")
        _module("writers.hyperlynx_writer")

    # Preflights, before any output exists.

    def validate_pad_domains(self, methods, *, load_layer_map):
        return _module("pad_domains").validate_board_pad_domains(
            self.board, methods, load_layer_map=load_layer_map)

    def preflight_die_outline_layers(self, declared):
        return _module("die_outline").preflight_die_outline_layers(
            self.board, declared)

    def preflight_carriers(self, export_report):
        return _chiplet_writer().preflight_carriers(self.board, export_report)

    # The intermediates the worker reads.

    def write_hyperlynx(self, path):
        return _module("writers.hyperlynx_writer").write_hyperlynx(self.board, path)

    def write_chiplet(self, path, *, interconnect_manifest, export_report):
        return _chiplet_writer().write_chiplet(
            self.board, path,
            interconnect_manifest=interconnect_manifest,
            export_report=export_report)

    def write_io_pads_json(self, path, *, export_report):
        return _chiplet_writer().write_io_pads_json(
            self.board, path, export_report=export_report)

    def write_nofill_regions_json(self, path):
        return _chiplet_writer().write_nofill_regions_json(self.board, path)

    def write_cmim_devices_json(self, path, *, skipped):
        return _chiplet_writer().write_cmim_devices_json(
            self.board, path, skipped=skipped)

    def write_die_pin_lists(self, pins_dir):
        return _chiplet_writer().write_die_pin_lists(self.board, pins_dir)

    def read_die_connections(self):
        return _chiplet_writer().read_die_connections(self.board)

    def read_die_thicknesses(self):
        return _chiplet_writer().read_die_thicknesses(self.board)

    def list_die_refs(self):
        return _chiplet_writer().list_die_refs(self.board)

    def body_thickness_um(self):
        """The interposer body thickness the board's stackup states, in um."""
        body_iu = self.board.GetDesignSettings().GetBoardThickness()
        return _chiplet_writer()._iu_to_um(body_iu)


def export_source(source_or_board):
    """``source_or_board`` as a source: a board is wrapped in a BoardSource."""
    if isinstance(source_or_board, BoardSource):
        return source_or_board
    return BoardSource(source_or_board)


def _module(name):
    """``chiplet_export.<name>``, as sys.modules holds it now."""
    return importlib.import_module("..%s" % name, __package__)


def _chiplet_writer():
    return _module("writers.chiplet_writer")
