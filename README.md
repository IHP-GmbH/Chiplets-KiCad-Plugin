# Chiplet KiCad Plugin

KiCad pcbnew action plugin for chiplet-aware EDA flows. Drives the
HYP → GDS → canonical `.chiplet` pipeline in one click.

Replaces the manual two-step workflow:

1. `File > Export > Chiplet...` (intermediate `.chiplet`)
2. `python3 hyp_to_gds.py --hyp board.hyp --update-chiplet-file board.chiplet`

with a single dialog under `Tools > External Plugins > Chiplet Export`.

## Status

Iteration 1 in progress (TaskList #47). The legacy C++ menu actions
(`File > Export > Chiplet...` and `File > Export > Hyperlynx...`)
remain functional and are the production path until the plugin
clears its end-to-end smoke test.

See the project CHANGELOG for the current gate.

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

## Troubleshooting

To be filled in once the end-to-end smoke test lands.

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
│   └── runner.py              Async subprocess wrapper
├── hyp_to_gds.py              GDS pipeline worker (imported from
│                              kicad_designs/kicad_interposer_hyperlynx_to_gds
│                              once Gate 47.2 lands)
├── tests/                     Golden-file + unit tests
├── requirements.txt           Worker venv deps
└── LICENSE
```
