# Chiplets-KiCad-Plugin

KiCad (pcbnew) plugins for the IHP Open ADK, the design flow for heterogeneous
chiplet integration on IHP SG13G2 dies and the IntM4TM2 silicon interposer.
[`chiplet_export`](plugins/chiplet_export/README.md) turns an assembly board
into the `.chiplet` description, the interposer and assembly GDS and the
assembly DRC report; [`resizer_passive_elements`](plugins/resizer_passive_elements/README.md)
resizes placed passive devices.

## Use

In the ADK-Tools image the export runs headless:

```bash
chiplet-export --board my_assembly.kicad_pcb --output-dir out
```

For the pcbnew dialog, link `plugins/chiplet_export` into KiCad's scripting
plugins folder. [`CONTRIBUTING.md`](CONTRIBUTING.md) describes the layout and
how a plugin is added.

## Documentation

The full documentation is at
https://ihp-open-adk-docs.readthedocs.io/en/latest/tools/02_chiplet_export_plugin.html

## Release

Released as part of the IHP Open ADK. The release page lists the commit of every
repository in each release:
https://ihp-open-adk-docs.readthedocs.io/en/latest/install/00_release.html

## License

GPL-3.0-or-later. See `LICENSE`. `chiplet_export` vendors a GPL-2.0-or-later
HyperLynx writer derived from KiCad, redistributed under the "or later" clause.
