# KiCad plugins for chiplet assembly

pcbnew plugins for chiplet and interposer design on IHP SG13G2 with the
IntM4TM2 interposer. `chiplet_export` turns a KiCad assembly board into the
`.chiplet`, the interposer GDS and the complete-assembly GDS, from the dialog
or headless as `chiplet-export`. `resizer_passive_elements` resizes placed
`cap_cmim` footprints from their schematic values.

Both are alpha. What `chiplet_export` does not verify is listed in the Status
section of its page; `resizer_passive_elements` is a proof of concept.

```{toctree}
:maxdepth: 2

chiplet-export
CLI
resizer-passive-elements
reference
```
