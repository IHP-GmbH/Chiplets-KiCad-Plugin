# chiplet-export

Headless export of a chiplet assembly board. A KiCad `.kicad_pcb` goes in;
out come the finalized `.chiplet`, the HyperLynx `.hyp` it was made from, the
interposer and complete-assembly GDS with their sidecars, and the ADK
assembly DRC report. It runs the same pipeline as the pcbnew dialog's **Run**
button (`pipeline.orchestrator.run_export`) and folds the assembly DRC into
its exit code.

`chiplet-export --help` prints the flags and the exit table, and works
without KiCad. This page is the full reference; `tests/test_cli_reference.py`
fails when the flags table below and the parser disagree.

## Synopsis

```
chiplet-export --board BOARD --output-dir DIR
               [--require-drc | --allow-drc-fail]
               [--interposer-adapter ID] [--interconnect-adapter ID]
               [--connection METHOD]
               [--insert-metal-fill] [--fill-mode {single-pass,closure}]
               [--seal-ring] [--system-chiplet CHIPLET]
               [--nofill-regions JSON] [--die-layouts JSON]
```

`chiplet-export` is `chiplet_export_cli.py` (next to this file) run under a
Python that can `import pcbnew`, the KiCad fork's interpreter. The adk-tools
image installs it as `/usr/local/bin/chiplet-export`, which runs
`/usr/bin/python3 $ADK_TOOLS/chiplet_kicad_plugin/plugins/chiplet_export/chiplet_export_cli.py`;
its Nix tail shell provides the same command. From a checkout:

```
python3 plugins/chiplet_export/chiplet_export_cli.py --board ... --output-dir ...
```

## Flags

`<board>` below is the board file's stem; it names every output.

| Flag | Value | Default | Effect |
|------|-------|---------|--------|
| `--board` | path | required | The assembly `.kicad_pcb`. A missing file, or one pcbnew cannot read, exits 2 with nothing created. Relative paths resolve against the current directory. |
| `--output-dir` | path | required | Where the outputs go (see *Outputs*). Created only once the board's preflights pass, so a refused board leaves nothing. |
| `--require-drc` | flag | off | Accepted for older callers and changes nothing: the assembly DRC is required by default. Cannot be combined with `--allow-drc-fail`. |
| `--allow-drc-fail` | flag | off | Exit 0 instead of 3 when the files were written but the assembly DRC did not pass; the last line says which outcome it let through. |
| `--interposer-adapter` | adapter id | the `.chiplet`'s `interposer.adapter`, else `intm4tm2` | The interposer adapter the assembly DRC runs with. An id, never a path. It does not change the `.chiplet`, whose adapter comes from the board's `INTERPOSER_ADAPTER` text variable. |
| `--interconnect-adapter` | adapter id | the `.chiplet`'s `interconnect.adapter`; absent there, no interconnect axis | The interconnect adapter the assembly DRC runs with. An id, never a path. |
| `--connection` | method id | none | The assembly-wide default connection stack, a method the interconnect PDK manifest declares (for example `cupillar_opt1`). A die's own `CONNECTION` field wins over it. A Cu-pillar stack or a solder bump generates its landings from the die pads. A die whose method's landing the worker cannot draw, as `connection_methods.py` reports, is refused before the export stages anything. Empty: each die keeps its own `CONNECTION`. |
| `--insert-metal-fill` | flag | off | Stamp metal density fill on the interposer GDS with the interposer PDK's fill engine (`fill_closure.fill_stack`). Without the `klayout` binary on `PATH` the fill is skipped with a message; a PDK without the engine fails the export (exit 1). |
| `--fill-mode` | `single-pass` or `closure` | `single-pass` | `single-pass` stamps every metal once; `closure` iterates Metal4/Metal5 against the density deck until in band. Ignored without `--insert-metal-fill`. |
| `--seal-ring` | flag | off | Place the interposer PDK's `sealring` PCell on the board outline, which must be one axis-aligned rectangle. Refused with exit 1 and nothing written when the outline cannot take it or drawn geometry crowds it. |
| `--system-chiplet` | path | none | The System `.chiplet` the assembly was built from. The interposer and each die take the technology of the System component with the same id, and `technologies` becomes the System's entries for exactly those ids. Refused with exit 1 and nothing written when an id has no match there, or when the interposer's technology is not `intm4tm2` (`CARRIER_UNSUPPORTED_TECHNOLOGY`: IntM4TM2 is the only carrier modelled until carriers are read from their technology package); a missing file exits 2. Without it, the technologies are only those the writer derives from layer-properties paths the board names (the two-die example's are); a board that names none, as a flow board, gets no technology and `technologies: {}`. |
| `--nofill-regions` | path | extracted from the board | Sidecar JSON of no-fill keep-outs. Empty: read from the board's `NoMetFiller` and `<metal>.nofill` layers. |
| `--export-report` | path | none | The report interposer-pnr `export` wrote with the board. Required when a carrier pad export drew (from `chiplet_carrier`, or carrying `IO_PAD_ID`) links a sheet symbol: a board drawn against a sheet, or a draft updated from its draft sheet. A board drawn by hand from its own sheet, its carrier pads from another library and without `IO_PAD_ID`, needs none. The io_pad nets of the draft and the io_pads sidecar take the planner's names from its `round_trip` when the board follows a sheet. Refused with exit 1 and nothing written when it is not an export report, when its round_trip is newer than the vendored reader (update the plugin), when it was written for a board with other carrier references, when it describes a board that follows a sheet and no carrier links a symbol, or when a carrier's net is in neither of its maps (interposer-pnr's rules, vendored in `vendor/round_trip_names.py`); a missing file exits 2. A board follows a sheet when its report says so (round_trip version 3 or 5, or version 6 listing the `sheet` feature); a feature the vendored reader does not know is ignored, never refused, since a feature only adds a block and never renames a net. A carrier pad from `chiplet_carrier` or carrying `IO_PAD_ID` is read by its `IO_PAD_ID`, never by its reference. Never guessed from the board's path. |
| `--die-layouts` | path | none | Sidecar JSON naming, per die reference, its full layout (an absolute path) and the die contract's `top_cell` and `bbox_um`. The `.chiplet` then gives that die the full layout and that top cell, and a `metadata` entry: `assembly_view: abstract` and `assembly_view_layout`, the path of the abstract its `GDS_FILE` names, relative to the `.chiplet`'s folder. The complete GDS, its boundary and the `.hyp` keep the abstract. Refused with exit 1 and nothing written when an entry names no die, or when the full layout's `top_cell` is not one of its top cells or either file's top cell box is not `bbox_um` exactly; a missing file exits 2. See `README.md`, *Full die layouts*. |
| `-h`, `--help` | flag | | Print the usage, the flags and the exit table, then exit 0. Needs no KiCad. |

## Inputs

- The board, and the footprint fields and project text variables it
  carries. Each die footprint names its layout in `GDS_FILE` (relative paths
  resolve against the board's directory) and declares `DIE_OUTLINE_LAYER`,
  optionally `CONNECTION` and `DIE_THICKNESS_UM`; IO pads, `cap_cmim`
  capacitors and carrier pad domains have fields of their own. The board
  project's `INTERPOSER_ADAPTER` and `INTERCONNECT_ADAPTER` text variables
  name the adapters written into the `.chiplet`. `README.md` documents each
  field (*Per-die settings*, *Die outlines*, *Integrated passives*, *Carrier
  electrical pads and die pin artwork*).
- Each die's GDS, as its `GDS_FILE` names it.
- The System `.chiplet`, with `--system-chiplet`.
- The interposer export report, with `--export-report`, for a board whose export-drawn carrier pads link sheet symbols.
- The no-fill sidecar, with `--nofill-regions`.
- The die layouts sidecar, and the full layouts it names, with
  `--die-layouts`.
- An earlier export's `<board>.chiplet` in the output directory, if any: the
  previous-export content guard compares it with the digest recorded when it
  was written, and `MANIFEST.md` reports the result.
- The interposer PDK, the interconnect PDK, the SG13G2 PDK, the ADK and
  gds_to_kicad, found as *Environment* says.

## Environment

Each root is taken from its environment variable when set; otherwise the
board project's text variable of the same name where noted, then a sibling
checkout found by walking up from the plugin. The adk-tools image sets every
one of them.

| Variable | Used for | Fallback |
|----------|----------|----------|
| `KICAD_CHIPLET_PYTHON` | The worker interpreter that runs `hyp_to_gds.py` and the assembly DRC; it needs `klayout.db` and PyYAML. | `<plugin>/.venv/bin/python3`, the project text variable, then a `python3` on `PATH` that imports both |
| `INTERPOSER_PDK_ROOT` | The interposer PDK, needed by every export: the mount plane is its `beol.attachment_surface_z_um` in `libs.tech/intm4tm2_stack.json`, and an export that cannot read it is refused. Also its layer properties, PCells, seal ring and fill engine. That descriptor is how an IntM4TM2 tree is told: a set root without it (another KLayout PDK, the SG13G2 die PDK included) exits 1 with `CARRIER_UNSUPPORTED_ROOT`, never falling back to the sibling checkout. | sibling `interposer` or `OpenIntM4TM2` |
| `INTERPOSER_LYP` | The interposer layer-properties file, when not the PDK's `libs.tech/klayout/tech/intm4tm2.lyp`. | the project text variable, then the PDK's file |
| `INTERCONNECT_PDK_ROOT` | The interconnect PDK manifest: connection stacks and method ids. A selected root that is unusable refuses the export. | the project text variable, then sibling `interconnect_pdk` or `IHP-Interconnect-IntM4TM2` |
| `PDK_ROOT` | The IHP-Open-PDK root (`ihp-sg13g2` via PCells); without it vias are drawn as plain rectangles. | sibling `IHP-Open-PDK` |
| `GDS_TO_KICAD_ROOT` | The gds_to_kicad tree the worker imports `PinList` from, for dies with pin lists. | sibling `gds_to_kicad` or `gds-to-kicad` |
| `ADK_ROOT` | The ADK, for `klayout/drc/run_drc.py` (the assembly DRC). When no ADK is found the DRC is NOT RUN, which exits 3. | the project text variable, then `<plugin>/../adk` |
| `ADK_TOOLS` | The image wrapper only: the installed adk-tools layout. | `/opt/adk-tools` |

The `klayout` binary on `PATH` is needed for the metal fill and by the
assembly DRC.

## Outputs

Under `--output-dir`:

```
<board>.chiplet                          finalized assembly description
<board>.chiplet.exportcontent.sha256     digest for the previous-export guard
<board>.hyp                              the HyperLynx netlist the layout came from
MANIFEST.md                              every product, present or absent
layout/<board>_interposer.gds            interposer layout (the .chiplet references it)
layout/<board>_interposer.boundaries.json
layout/<board>_interposer.pillars.json   with a connection method
layout/<board>_interposer.fill_coverage.json   with --insert-metal-fill
layout/<board>_complete.gds              interposer plus dies
layout/<board>_complete.boundaries.json
layout/<board>_complete.pillars.json     with a connection method
layout/<board>_complete.ixn_methods.json when dies declare connection methods
chiplets/<ref>/<file>.gds                a die GDS the .chiplet cannot reach as written
reports/<board>_assembly_drc.lyrdb       when the assembly DRC ran
reports/<board>_cupillar_drc.json        with a connection method
reports/<board>_fill_density.json        with --insert-metal-fill
logs/<YYYYmmdd_HHMMSS>_<board>.log       one per run
```

The `.chiplet` is finalized in a hidden `.<board>.chiplet.staging` and
replaces the canonical file only when the layout worker succeeds. A failed
or cancelled run leaves an existing `.chiplet` as it was and keeps what it
staged as `<board>.chiplet.unfinalized`. The other files are not protected
this way. `README.md` (*Outputs*) has the details of each sidecar.

## What it prints

On stdout: `chiplet-export: <board> -> <output-dir>`, the plugin directory,
then the pipeline log with each line prefixed by `  | `. After the run, one
aligned `name: value` line per field: `exit_code`, `cancelled`, `error`
(only when set), `hyp_path`, `chiplet_path`, `interposer_gds_path`,
`complete_gds_path`, `fill_density_report` and `fill_coverage_path` (only
when produced). Then the verdict line, which is printed exactly once:

```
assembly DRC: PASSED
assembly DRC: FAILED (exit N)
assembly DRC: NOT RUN
```

followed by `assembly_drc_report: <path>` when there is one. When the
assembly DRC did not pass, the last line says what the exit code means:

```
FAIL: files written, assembly DRC not passed (<outcome>); exit 3
DRC not required (--allow-drc-fail): files written, assembly DRC not passed (<outcome>); exit 0
```

Refusals and errors go to stderr.

## Exit codes

| Code | Meaning |
|-----:|---------|
| 0 | The files were written and the assembly DRC PASSED (or did not pass, under `--allow-drc-fail`). |
| 1 | Nothing trustworthy was written: a refused board, a failed worker, a `.chiplet` that could not be replaced, or a crash before the DRC. |
| 2 | The board (or the `--system-chiplet`, `--export-report` or `--die-layouts` file) is missing, or KiCad cannot read the board; also a usage error. |
| 3 | The files were written and the assembly DRC did not pass: FAILED, REFUSED or NOT RUN, a DRC stage that raised included. |

A nonzero `run_drc.py` exit reads "failed or refused", because that exit
does not tell them apart. The table lives in `pipeline/export_exit.py`.

## Examples

A minimal export of the two-die example the adk-tools image ships (the image
sets the environment):

```
chiplet-export \
    --board /opt/adk-tools/examples/two_die_interposer/kicad/two_die_interposer.kicad_pcb \
    --output-dir /tmp/two_die_out
```

The packaging flow's `gds` stage, with every path absolute under its work
directory `$WORK`. It takes the technology ids from the System `.chiplet`
the board was built from, and runs the assembly DRC as a stage of its own,
so it does not take this run's verdict; a run with an I/O profile also
finishes the carrier:

```
chiplet-export --board $WORK/pnr/<system>.draft.kicad_pcb \
    --system-chiplet $WORK/design.chiplet \
    --export-report $WORK/pnr/export.json \
    --output-dir $WORK/out/export \
    --interposer-adapter intm4tm2 --interconnect-adapter ihp_cupillar \
    --allow-drc-fail \
    --seal-ring --insert-metal-fill --fill-mode closure
```

The least a run with technology ids needs, from a directory holding the
board and its System `.chiplet`:

```
chiplet-export --board design.kicad_pcb --system-chiplet design.chiplet \
    --output-dir out
```
