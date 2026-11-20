# SPDX-License-Identifier: GPL-3.0-or-later
"""The export's inputs from the routed documents: the routed-geometry
document, the elaborated .chiplet and the routed io_pads manifest.

RoutedSource supplies run_export what BoardSource supplies from a board, so
the export composes from the routed documents through the same body. Every
refusal is made in ``preflight``, before the export creates its output
folder; the methods the body calls afterwards hand back what the preflight
computed and refuse nothing, because the body turns most of their exceptions
into warnings.

No pcbnew, and no kicad-cli: nothing here or in what it imports loads KiCad.
No PyYAML either: the documents are read by routed_inputs.py under the
worker interpreter, which has it; the CLI's interpreter has not.
"""

import os
import shutil
import warnings

from ..pipeline.export_source import ExportSource
from .document import RoutedRefused, bound_files
from ..writers.connection_stacks import (
    validate_adapter_id, validate_interconnect_ids)
from .intermediates import IO_PADS_FRAME, prepare_intermediates, write_pin_lists

#: The worker-tier script that reads the three inputs (routed_inputs.py).
INPUTS_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "routed_inputs.py")
INPUTS_TIMEOUT_SECONDS = 120

def output_stem_problem(name):
    """Why name cannot name the outputs, or None: it is joined to the
    workspace and the output folder, so it must be a file stem, not a path."""
    if not name:
        return "the routed path needs the output name (--name)"
    if name in (".", "..") or "/" in name or "\\" in name or os.sep in name:
        return "the output name (--name) must be a file stem, not a path: %r" % name
    return None


def read_in_worker(python, document_path, chiplet_path, io_pads_path, *,
                   on_warn=None, cancel_event=None):
    """(document, chiplet, io_pads), read by routed_inputs.py under the worker
    interpreter ``python``: this process may not import the vendored reader
    (it imports PyYAML). The reads' warnings go to ``on_warn``, or to the
    warnings channel as the readers' own default does. RoutedRefused when the
    reads refuse, or the script fails, times out, is cancelled through
    ``cancel_event`` or prints no report of its version."""
    import subprocess
    from ..pipeline.runner import run_async
    from ..routed_inputs import ReportError, parse_report
    command = [python, INPUTS_SCRIPT, document_path, chiplet_path,
               io_pads_path, IO_PADS_FRAME]
    shown = "%s %s" % (python, os.path.basename(INPUTS_SCRIPT))
    try:
        run = run_async(command, cancel_event=cancel_event,
                        timeout=INPUTS_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RoutedRefused("%s cannot read the routed inputs: %s"
                            % (shown, exc)) from exc
    if run.cancelled:
        raise RoutedRefused("%s: cancelled" % shown)
    if run.timed_out:
        raise RoutedRefused("%s timed out after %d s"
                            % (shown, INPUTS_TIMEOUT_SECONDS))
    if run.exit_code != 0:
        lines = (run.stderr or "").strip().splitlines()
        raise RoutedRefused("%s exited %s: %s" % (
            shown, run.exit_code, lines[-1] if lines else "no message"))
    try:
        inputs, messages, refused = parse_report(run.stdout)
    except ReportError as exc:
        raise RoutedRefused("%s printed %s" % (shown, exc)) from exc
    for message in messages:
        if on_warn is not None:
            on_warn(message)
        else:
            warnings.warn(message, stacklevel=2)
    if refused is not None:
        raise RoutedRefused(refused)
    return inputs


class RoutedSource(ExportSource):
    """The export's inputs from the routed documents, without a board."""

    #: The worker composes from the document; no .hyp is written or published.
    writes_hyp = False
    #: No KiCad project: discovery reads no project text variables.
    discovery_board = None
    origin = field_origin = "routed geometry"

    def __init__(self, document, chiplet, io_pads, *, name,
                 interposer_adapter=None, interconnect_adapter=None):
        """``interposer_adapter`` is what the intermediate .chiplet names;
        the board path reads it from the project's text variables.
        ``interconnect_adapter`` is the --interconnect-adapter flag and keeps
        its board-path meaning: checked here, it steers the assembly DRC, and
        it is not written into the intermediate. On the board path that block
        comes only from an INTERCONNECT_ADAPTER text variable, which a flow
        draft's project does not set, so the worker adds the block from the
        die connections on both paths, at the same place in the file. Both
        must be given (None is refused by the preflight, never defaulted):
        the interposer adapter not empty, the interconnect adapter empty for
        none, as on the board path."""
        problem = output_stem_problem(name)
        if problem:
            raise RoutedRefused(problem)
        self._interposer_adapter = interposer_adapter
        self._interconnect_adapter = interconnect_adapter
        self.files = {}
        self.document_path = os.path.abspath(document)
        self.chiplet_path = os.path.abspath(chiplet)
        self.io_pads_path = os.path.abspath(io_pads)
        self._name = name
        self._prepared = None

    @property
    def routed_geometry(self):
        return self.document_path

    def name(self):
        return self._name

    def describe(self):
        return ("Routed geometry: %s (the board is not opened)"
                % self.document_path)

    def preflight(self, interconnect_manifest, *, worker_python, on_warn=None,
                  cancel_event=None):
        """Read and check the documents and compute every intermediate.

        The documents are read under the worker interpreter
        ``worker_python()`` (read_in_worker); the rest is computed here.
        Refuses (RoutedRefused) an adapter not given, what
        prepare_intermediates refuses (the stack and nets included), a die
        layout or pin list whose bytes are not the ones the document binds,
        and a device reference the worker's flags cannot carry. Writes
        nothing.
        """
        if self._interposer_adapter is None or self._interconnect_adapter is None:
            raise RoutedRefused(
                "the routed path takes both adapters explicitly: give "
                "--interposer-adapter and --interconnect-adapter (empty for "
                "no interconnect adapter)")
        if not self._interposer_adapter:
            raise RoutedRefused("the interposer adapter is empty")
        try:
            validate_adapter_id(self._interconnect_adapter, "interconnect adapter")
            validate_interconnect_ids(adapter=self._interconnect_adapter,
                                      manifest=interconnect_manifest)
            inputs = read_in_worker(worker_python(), self.document_path,
                                    self.chiplet_path, self.io_pads_path,
                                    on_warn=on_warn, cancel_event=cancel_event)
            prepared = prepare_intermediates(
                *inputs, board_name=self._name, interconnect_manifest=interconnect_manifest,
                interposer_adapter=self._interposer_adapter,
                interconnect_adapter="",
                method_specs=interconnect_manifest.methods)
        except ValueError as exc:
            # An adapter id the board path refuses too.
            raise RoutedRefused(str(exc)) from exc
        self.files = bound_files(prepared.document, self.document_path)
        # The worker's per-die flags carry every device reference.
        from ..die_outline import _reference_problem
        for device in prepared.document["devices"]:
            problem = _reference_problem(device["ref"])
            if problem:
                raise RoutedRefused("device %r: %s" % (device["ref"], problem))
        self._prepared = prepared

    @property
    def prepared(self):
        if self._prepared is None:
            raise RuntimeError("RoutedSource used before its preflight")
        return self._prepared

    # The preflights the body calls, before any output exists.

    def validate_pad_domains(self, methods, *, load_layer_map):
        """{ref: method} of every device, after its carrier layer check: the
        archive's carrier.layer must be the document's top metal, the one
        every carrier pad lands on, and resolve in the worker LYP. The LYP is
        loaded at the first device, so its refusal is a refusal too."""
        from ..pad_domains import PadDomainError
        from ..layer_map import LayerMapError
        prepared = self.prepared
        top = prepared.document["stack"][-1]["metal"]
        layer_map = None
        for device in prepared.document["devices"]:
            ref = device["ref"]
            carrier = device["pad_domains"]["carrier"]
            try:
                if carrier["layer"] != top:
                    raise PadDomainError(
                        "carrier.layer is '%s', but the top metal of the routed stack "
                        "is '%s'. The routed path lands carrier pads on the top metal "
                        "only." % (carrier["layer"], top))
                if layer_map is None:
                    layer_map = load_layer_map()
                layer_map.get_layer(top)  # the top metal resolves in the LYP
            except (LayerMapError, KeyError, TypeError, PadDomainError) as exc:
                raise PadDomainError("%s carrier layer: %s" % (ref, exc)) from exc
        return dict(prepared.die_connections)

    def preflight_die_outline_layers(self, declared):
        """Every die keeps its archived size: all are exempt, none is sized
        from an outline layer. A declared outline layer is refused, as the
        board path refuses one for a marked die."""
        from ..die_outline import DieOutlineLayerRefused
        if declared:
            raise DieOutlineLayerRefused(
                "outline layers were given for %s, but every die of the routed "
                "geometry keeps its archived size" % ", ".join(sorted(declared)))
        return {}, sorted(self.prepared.pin_lists)

    def preflight_carriers(self, export_report):
        """The routed io_pads manifest already carries the planner's names;
        the preflight read and checked it."""
        self.prepared

    # The intermediates the worker reads.

    def write_chiplet(self, path, *, interconnect_manifest, export_report):
        from ..writers.intermediate_chiplet import emit_intermediate_chiplet
        prepared = self.prepared
        emit_intermediate_chiplet(prepared.facts, path, prepared.stacks)
        return True

    def write_io_pads_json(self, path, *, export_report):
        """The routed io_pads manifest, as given, for the worker."""
        shutil.copyfile(self.io_pads_path, path)
        return len(self.prepared.io_pads)

    def write_nofill_regions_json(self, path):
        """None: the routed documents carry no no-fill region (flow-authored
        keep-outs come as --nofill-regions on both paths)."""
        return 0

    def write_cmim_devices_json(self, path, *, skipped):
        """None: the routed path draws no integrated capacitor."""
        return 0

    def write_die_pin_lists(self, pins_dir):
        return write_pin_lists(self.prepared.pin_lists, pins_dir)

    def read_die_connections(self):
        return dict(self.prepared.die_connections)

    def read_die_thicknesses(self):
        return dict(self.prepared.die_thicknesses)

    def list_die_refs(self):
        return [device["ref"] for device in self.prepared.document["devices"]]

    def body_thickness_um(self):
        return self.prepared.document["interposer"]["body_thickness_um"]
