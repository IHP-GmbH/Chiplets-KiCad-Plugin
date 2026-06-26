# Chiplet KiCad Plugin

A pcbnew action plugin for chiplet-aware EDA flows. It drives the
HYP -> GDS -> canonical `.chiplet` pipeline in one click, replacing a
manual two-step workflow:

1. `File > Export > Chiplet...` to get an intermediate `.chiplet`.
2. `python3 hyp_to_gds.py board.hyp --update-chiplet-file board.chiplet`
   to render the interposer GDS and finalize the file.

Both steps now live behind a single dialog under
`Tools > External Plugins > Chiplet Export`. The plugin is consumed by
adk-tools as a submodule.

## Status

The plugin is the sole entry point for chiplet export. The legacy C++
menu actions (`File > Export > Chiplet...` and
`File > Export > Hyperlynx...`) have been removed from the KiCad fork.
The headless C++ functions (`ExportBoardToChipletFile`,
`ExportBoardToHyperlynxFile`) remain available via SWIG and back the
plugin's byte-exact regression tests.

Verification coverage:

- Byte-exact writer parity against the C++ exporters
  (`tests/test_byte_exact_writers.py`).
- Round-trip regression vs the wire-bond demo `.chiplet`
  (`tests/regenerate_wirebond_demo.py` plus chiplet-studio
  `CoordFrameContract*` gtests).
- Live pcbnew smoke (interf_u demo) and chiplet-studio visual smoke.

See `chiplet-studio/docs/coord_frame_contract.md` for the canonical
coordinate frame the writers must honour.

## License

GPL-3.0-or-later. See `LICENSE`. The Hyperlynx writer
(`writers/hyperlynx_writer.py`) is a derivative work of KiCad's
`pcbnew/exporters/export_hyperlynx.cpp` (Copyright (C) 2019 CERN and
KiCad Developers), originally GPL-2.0-or-later and redistributed here
under GPL-3.0-or-later per that license's "or later" clause.

## Install

### 1. Make the plugin discoverable by pcbnew

Symlink (recommended for development) or copy this directory into the
KiCad scripting plugins folder. Linux plus KiCad 9.0:

```bash
ln -s /path/to/chiplet_kicad_plugin \
      ~/.config/kicad/9.0/scripting/plugins/chiplet_kicad_plugin
```

### 2. Set up the worker venv

`hyp_to_gds.py` needs `klayout>=0.28` and `PyYAML>=6.0`, neither of which
ships with KiCad's bundled Python. Install them into a separate venv:

```bash
python3 -m venv /path/to/chiplet_kicad_plugin/.venv
/path/to/chiplet_kicad_plugin/.venv/bin/pip install -r \
    /path/to/chiplet_kicad_plugin/requirements.txt
```

The plugin auto-detects `.venv/bin/python3` next to itself. To use a
different interpreter, set `KICAD_CHIPLET_PYTHON` to its absolute path
before launching KiCad, or define it as a project text variable.

### 3. Restart pcbnew

The action appears as `Tools > External Plugins > Chiplet Export`.

## How to use

1. Open the chiplet design in pcbnew.
2. `Tools > External Plugins > Chiplet Export`.
3. Fill in the dialog (sections below), then click **Run**. Log lines
   stream into the dialog as the worker runs; **Cancel** terminates the
   worker subprocess.

### Output directory

Where the canonical artifacts land. Defaults to the directory holding
the loaded `.kicad_pcb`.

### Outputs

Tick what you want produced:

- *Canonical .chiplet* (default ON): the GDS-bbox-corner-anchored
  assembly file consumed by chiplet-studio.
- *Interposer GDS* (default ON): the interposer-only layout.
- *Complete assembly GDS* (default OFF): interposer plus all chiplet
  instances flattened into one GDS.
- *Keep intermediate .hyp* (default OFF): copy the metric Hyperlynx file
  used to drive the pipeline.
- *Annotate chiplet boundaries (viewer-only layer)* (default OFF): paint
  each chiplet boundary and instance label onto annotation layer
  `1000/0` for eyeball inspection in KLayout. No DRC rule reads this
  layer; the assembly contract stays in the `.boundaries.json` manifest.

### PDK roots

The interposer PDK, interconnect PDK and ADK checkouts the pipeline
will use. Each field is pre-filled by the discovery chain (environment
variable, project text variable, sibling checkout) so the provenance of
every dependency is visible. Edit a path to export against a different
checkout, for example a vendor's interconnect PDK or a pinned release.
Changing the interconnect PDK re-reads the connection-stack list from
that PDK's manifest. Overrides reach the worker as the matching
environment variables: `INTERPOSER_PDK_ROOT`, `INTERCONNECT_PDK_ROOT`,
`ADK_ROOT`.

### Pipeline options

- *Top cell* (default `INTERPOSER`): the top-level cell name written
  into the interposer GDS.
- *Connection stack (default)* (optional): the methods declared by the
  selected interconnect PDK's manifest (for example `cupillar_opt1/2/3`,
  `sbump_sac305`, vendor methods). Empty means the writer keeps the
  dies' existing connection field untouched. This is the assembly-wide
  default; per-die rows below override it.
- *Interposer technology LYP* (optional): the KLayout layer-properties
  file of the interposer technology. Pre-filled with the discovered
  default via `discover_interposer_lyp()`, which follows the
  `INTERPOSER_LYP` env var, then the `INTERPOSER_LYP` project text
  variable, then the PDK's canonical
  `libs.tech/klayout/tech/intm4tm2.lyp`. The `.lyp` belongs to the
  interposer PDK, not the plugin, so there is no bundled copy: when none
  resolves the field is left blank and the export errors asking you to set
  `INTERPOSER_PDK_ROOT` or pick a file. Replace it only when the interposer
  uses a different technology. Do not point it at the interconnect `.lyp`
  (bump layers only); that one is consumed automatically via the
  `.chiplet`.
- *I/O pads*: auto-extracted from the loaded board's IO_CLASS footprints,
  rendered in the interposer GDS and injected under the interposer
  component. An explicit sidecar JSON from `kicad_pcb_to_iopads.py` can be
  supplied through the headless `ExportOptions.io_pads_json`.
- *Cu-pillars*: auto-generated from each flip-chip die's footprint pads when
  the die's connection is a cu-pillar stack. A pre-generated cu-pillar GDS,
  typically from `bump_mirror.py`, can be supplied through the headless
  `ExportOptions.cupillar_gds`.

### Per-die connection

One choice per die footprint, shown only when the board has die
footprints. Each row is initialized from that footprint's `CONNECTION`
field and written back to it on Run, so the board stays the source of
truth for per-die method selection. A die left on the default follows
the assembly-wide *Connection stack* above; an explicit selection gives
that die its own connection stack, 3D bodies and DRC numbers.

### Worker Python override

Optional. Bypass the discovery chain by pointing at a specific
interpreter.

### Assembly DRC

When *Complete assembly GDS* is enabled, the export runs the ADK's
`run_drc.py` (under the ADK root) over the complete GDS after
`hyp_to_gds.py` finishes. The verdict, `assembly DRC: PASSED / FAILED /
NOT RUN`, plus the report path is appended to the dialog log. A DRC
failure does not invalidate the exported artifacts: the export exit code
stays 0 and the status line flags the failed deck separately.

## Troubleshooting

**"Could not locate a Python interpreter with klayout + PyYAML"**

The plugin tried every discovery step and none worked. Fix one of:

- Create the recommended `.venv` (see [Install section 2](#2-set-up-the-worker-venv)).
- Set `KICAD_CHIPLET_PYTHON=/absolute/path/to/python` in the shell that
  launches KiCad.
- Set `KICAD_CHIPLET_PYTHON` as a project text variable in
  *Board Setup > Text Variables*.

**The Run button is disabled / never enables**

A previous run is still in flight. Click **Cancel** to terminate it.

**Output directory is empty / no chiplet appears**

Re-check the dialog status line. Exit codes other than 0 indicate
`hyp_to_gds.py` failed; the live log preserves stderr under a `[stderr]`
prefix.

**Plugin not visible under External Plugins**

KiCad scans `~/.config/kicad/9.0/scripting/plugins/` at startup. Verify
the symlink, then in pcbnew use
*Tools > External Plugins > Refresh Plugins*. If registration failed,
KiCad's stdout logs the import error.

**"Hyperlynx writer aborted (most commonly: the board has no closed
Edge.Cuts outline)"**

The Hyperlynx writer needs a closed board outline to derive units and
bounding box. Add an Edge.Cuts polygon enclosing the design and retry.

**Cu-pillars missing in the chiplet-studio Detailed render**

Cu-pillars are generated only when a die's connection is a cu-pillar stack
(set via the per-die *Connection* row or the assembly-wide *Connection
stack*). Confirm the die has a cu-pillar method selected, then re-run. A
pre-generated cu-pillar GDS can instead be supplied through the headless
`ExportOptions.cupillar_gds`.

**Run hangs with no log output / dialog freezes**

Open *Tools > External Plugins > Refresh Plugins*; the Python traceback
of the failed worker prints to KiCad's stdout. The dialog also streams
traceback lines into the log control on writer crashes.

## Repository layout

```
chiplet_kicad_plugin/
├── __init__.py                Registers the ActionPlugin with pcbnew
├── chiplet_export_action.py   ActionPlugin subclass + Run() entry
├── dialog_chiplet_export.py   wxPython dialog (options + log)
├── writers/
│   ├── chiplet_writer.py      Python port of export_chiplet.cpp
│   ├── connection_stacks.py   Connection-stack ids + interconnect validation
│   └── hyperlynx_writer.py    Python port of export_hyperlynx.cpp
├── pipeline/
│   ├── discovery.py           Locate worker Python + hyp_to_gds.py
│   ├── orchestrator.py        ExportOptions, build_cli_args, run_export
│   └── runner.py              Async subprocess wrapper
├── hyp_to_gds.py              GDS pipeline worker (vendored from the
│                              kicad_interposer_hyperlynx_to_gds project
│                              and extended with plugin-specific flags:
│                              --annotate-boundaries, --die-connections,
│                              manifest-sourced --connection-type, the
│                              .boundaries.json scheme)
├── tests/                     pytest suite (see tests/README.md)
├── requirements.txt           Worker venv deps
└── LICENSE
```

## Headless usage

The pipeline runs without the dialog through
`pipeline.orchestrator.run_export(board, options, plugin_dir)`:

```python
import pcbnew
from chiplet_kicad_plugin.pipeline.orchestrator import (
    ExportOptions, run_export,
)

board = pcbnew.LoadBoard("/path/to/board.kicad_pcb")
options = ExportOptions(
    output_dir="/tmp/chiplet_out",
    emit_chiplet=True,
    emit_interposer_gds=True,
    emit_complete_gds=True,
    top_cell="INTERPOSER",
    lyp_override="/path/to/intm4tm2.lyp",
    io_pads_json="",
    cupillar_gds="/path/to/cu_pillars.gds",
)
result = run_export(
    board, options,
    plugin_dir="/path/to/chiplet_kicad_plugin",
    on_log=print,
)
assert result.exit_code == 0 and not result.error
```

`tests/regenerate_wirebond_demo.py` is a full worked example.

## References

- `chiplet-studio/docs/coord_frame_contract.md`: the canonical
  coordinate frame the writers honour (GDS-bbox-corner, y-up, um) and
  the `_metadata.finalize_required` intermediate-frame marker.
- `tests/README.md`: test layout, byte-exact parity, round-trip
  regression.
