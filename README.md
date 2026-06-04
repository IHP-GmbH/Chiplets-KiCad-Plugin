# Chiplet KiCad Plugin

KiCad pcbnew action plugin for chiplet-aware EDA flows. Drives the
HYP → GDS → canonical `.chiplet` pipeline in one click.

Replaces the manual two-step workflow:

1. `File > Export > Chiplet...` (intermediate `.chiplet`)
2. `python3 hyp_to_gds.py --hyp board.hyp --update-chiplet-file board.chiplet`

with a single dialog under `Tools > External Plugins > Chiplet Export`.

## Status

The plugin is the sole entry point for chiplet export. The legacy
C++ menu actions (`File > Export > Chiplet...` and
`File > Export > Hyperlynx...`) have been removed from the kicad fork.
The headless C++ functions (`ExportBoardToChipletFile`,
`ExportBoardToHyperlynxFile`) remain available via SWIG for the
plugin's byte-exact regression tests.

Verification coverage:

- Byte-exact writer parity against the C++ exporters
  (`tests/test_byte_exact_writers.py`).
- Round-trip regression vs the wire-bond demo .chiplet
  (`tests/regenerate_wirebond_demo.py` + chiplet-studio
  `CoordFrameContract*` gtests).
- Live pcbnew smoke (interf_u demo) and chiplet-studio visual smoke.

See `chiplet-studio/docs/coord_frame_contract.md` for the canonical
coordinate frame the writers must honour.

## License

GPL-2.0-or-later. See `LICENSE`. The Hyperlynx writer
(`writers/hyperlynx_writer.py`) is a derivative work of KiCad's
`pcbnew/exporters/export_hyperlynx.cpp`, also GPL-2.0-or-later
(Copyright (C) 2019 CERN and KiCad Developers).

## Install

### 1. Make the plugin discoverable by pcbnew

Symlink (recommended for development) or copy this directory into
the KiCad scripting plugins folder. Linux + KiCad 9.0:

```bash
ln -s /path/to/chiplet_kicad_plugin \
      ~/.config/kicad/9.0/scripting/plugins/chiplet_kicad_plugin
```

### 2. Set up the worker venv

`hyp_to_gds.py` requires `klayout` and `PyYAML`, neither of which is
part of the Python bundled with KiCad. Use a separate venv:

```bash
python3 -m venv /path/to/chiplet_kicad_plugin/.venv
/path/to/chiplet_kicad_plugin/.venv/bin/pip install -r \
    /path/to/chiplet_kicad_plugin/requirements.txt
```

The plugin auto-detects `.venv/bin/python3` next to itself. To use a
different interpreter, set `KICAD_CHIPLET_PYTHON` to its absolute
path before launching KiCad.

### 3. Restart pcbnew

The action appears as `Tools > External Plugins > Chiplet Export`.

## How to use

1. Open the chiplet design in pcbnew.
2. `Tools > External Plugins > Chiplet Export`.
3. In the dialog:
   - **Output directory**: where the canonical artifacts land
     (defaults to the directory holding the loaded `.kicad_pcb`).
   - **Outputs**: tick what you want produced.
     - *Canonical .chiplet* (default ON): GDS-bbox-corner-anchored
       assembly file consumed by chiplet-studio.
     - *Interposer GDS* (default ON): the interposer-only layout.
     - *Complete assembly GDS* (default OFF): interposer plus all
       chiplet instances flattened into a single GDS.
     - *Keep intermediate .hyp* (default OFF): copy the metric
       Hyperlynx file used to drive the pipeline.
   - **PDK roots**: the interposer PDK, interconnect PDK and ADK
     checkouts the pipeline will use. Pre-filled by the discovery
     chain (environment variable, project text variable, sibling
     checkout) so the provenance of every dependency is visible;
     edit a path to export against a different checkout -- e.g. a
     vendor's interconnect PDK or a pinned release. Changing the
     interconnect PDK re-reads the connection-stack list from that
     PDK's manifest. Overrides reach the worker as the matching
     environment variables (`INTERPOSER_PDK_ROOT`,
     `INTERCONNECT_PDK_ROOT`, `ADK_ROOT`).
   - **Pipeline options**:
     - *Top cell* (default `TOP`): the top-level cell name written
       into the interposer GDS.
     - *Connection stack* (optional): the methods declared by the
       selected interconnect PDK's manifest (e.g. `cupillar_opt1/2/3`,
       `sbump_sac305`, vendor methods). Empty means the writer keeps
       the dies' existing connection field untouched.
     - *LYP override* (optional): use a custom KLayout layer
       properties file instead of the built-in `intm4tm2.lyp`.
     - *I/O pads JSON* (optional): sidecar JSON from
       `kicad_pcb_to_iopads.py`; pads are rendered in the
       interposer GDS and injected under the interposer component.
     - *Cu-pillar GDS* (optional): pre-generated cu-pillar layout
       (typically produced by `bump_mirror.py`) merged into the
       interposer GDS so the chiplet-studio Detailed render shows
       the pillar caps under each flip-chip die.
   - **Worker Python override** (optional): bypass the discovery
     chain by pointing at a specific interpreter.
4. **Run**. Log lines stream into the dialog. **Cancel** terminates
   the worker subprocess.

## Troubleshooting

**"Could not locate a Python interpreter with klayout + PyYAML"**

The plugin tried every discovery step and none worked. Fix one of:

- Create the recommended `.venv` (see [Install §2](#2-set-up-the-worker-venv)).
- Set `KICAD_CHIPLET_PYTHON=/absolute/path/to/python` in the shell
  that launches KiCad.
- Set `KICAD_CHIPLET_PYTHON` as a project text variable in
  *Board Setup > Text Variables*.

**The Run button is disabled / never enables**

A previous run is still in flight. Click **Cancel** to terminate it.

**Output directory is empty / no chiplet appears**

Re-check the dialog status line. Exit codes other than 0 indicate
`hyp_to_gds.py` failed; the live log preserves stderr under
`[stderr]` prefix.

**Plugin not visible under External Plugins**

KiCad scans `~/.config/kicad/9.0/scripting/plugins/` at startup.
Verify the symlink, then in pcbnew use
*Tools > External Plugins > Refresh Plugins*. If registration
failed, KiCad's stdout logs the import error.

**"Hyperlynx writer aborted (most commonly: the board has no closed
Edge.Cuts outline)"**

The Hyperlynx writer needs a closed board outline to derive units
and bounding box. Add an Edge.Cuts polygon enclosing the design and
retry.

**Cu-pillars missing in the chiplet-studio Detailed render**

The interposer GDS the plugin produces only contains routing layers
unless you also supply the cu-pillar GDS. Generate it once with
`bump_mirror.py` (or your project equivalent) and select it in the
dialog's *Cu-pillar GDS* picker before clicking Run.

**Run hangs with no log output / dialog freezes**

Open *Tools > External Plugins > Refresh Plugins*; the Python
traceback of the failed worker prints to KiCad's stdout. The dialog
also streams traceback lines into the log control on writer crashes.

## Repository layout

```
chiplet_kicad_plugin/
├── __init__.py                Registers the ActionPlugin with pcbnew
├── chiplet_export_action.py   ActionPlugin subclass + Run() entry
├── dialog_chiplet_export.py   wxPython dialog (options + log)
├── writers/
│   ├── chiplet_writer.py      Python port of export_chiplet.cpp
│   └── hyperlynx_writer.py    Python port of export_hyperlynx.cpp
├── pipeline/
│   ├── discovery.py           Locate worker Python + hyp_to_gds.py
│   ├── orchestrator.py        ExportOptions, build_cli_args, run_export
│   └── runner.py              Async subprocess wrapper
├── hyp_to_gds.py              GDS pipeline worker (verbatim copy of
│                              the hyp_to_gds worker from
│                              kicad_designs/kicad_interposer_hyperlynx_to_gds)
├── tests/                     pytest suite (see tests/README.md)
├── requirements.txt           Worker venv deps
└── LICENSE
```

## Headless usage

The pipeline is available without the dialog through
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
    top_cell="TOP",
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

- `chiplet-studio/docs/coord_frame_contract.md` — canonical
  coordinate frame the writers honour (GDS-bbox-corner, y-up, µm)
  and the `_metadata.finalize_required` intermediate-frame marker.
- `tests/README.md` — test layout, byte-exact parity, round-trip
  regression.
