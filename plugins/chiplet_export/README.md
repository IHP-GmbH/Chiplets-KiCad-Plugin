# Chiplet Export (KiCad pcbnew plugin)

One of the plugins hosted in the `Chiplets-KiCad-Plugin` repository (it
lives at `plugins/chiplet_export`). A pcbnew action plugin for
chiplet-aware EDA flows. It drives the
HYP -> GDS -> canonical `.chiplet` pipeline in one click, replacing a
manual two-step workflow:

1. `File > Export > Chiplet...` to get an intermediate `.chiplet`.
2. `python3 hyp_to_gds.py board.hyp --update-chiplet-file board.chiplet`
   to render the interposer GDS and finalize the file.

Both steps now live behind a single dialog under
`Tools > External Plugins > Chiplet Export`. The plugin is consumed by
adk-tools as a submodule.

The same pipeline runs without the GUI as the `chiplet-export` command, for
scripts, flows and agents: [CLI.md](CLI.md) is its reference (flags, inputs,
environment, outputs, exit codes, examples).

## Status

> [!WARNING]
> Alpha. The flags, the dialog and the files it writes can still change
> between releases, and nothing below says that a design is ready to be
> made.

What is not verified:

- No test compares an exported assembly with fabricated silicon.
- The assembly DRC runs the ADK's deck and reports its verdict; a PASSED
  verdict covers the rules that deck has, nothing more. The plugin runs no
  LVS.
- The writers are checked byte for byte against the C++ exporters of the
  KiCad fork the ADK ships, and nowhere else. Other KiCad builds are not
  tested.
- The dialog's own smoke test needs a display, which no CI tier has; the
  suite exercises the same pipeline headless.

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

See `chiplet-system-spec/docs/coord_frame_contract.md` for the canonical
coordinate frame the writers must honour.

## License

GPL-3.0-or-later. See the repository root `LICENSE`. The Hyperlynx writer
(`writers/hyperlynx_writer.py`) is a derivative work of KiCad's
`pcbnew/exporters/export_hyperlynx.cpp` (Copyright (C) 2019 CERN and
KiCad Developers), originally GPL-2.0-or-later and redistributed here
under GPL-3.0-or-later per that license's "or later" clause. The vendored
`.chiplet` reader (`vendor/chiplet_format_io/`) is Apache-2.0; its license text is
the repository's `LICENSES/Apache-2.0.txt`.

## Install

### 1. Make the plugin discoverable by pcbnew

Symlink (recommended for development) or copy this plugin directory
(`plugins/chiplet_export`, not the whole repo) into the KiCad scripting
plugins folder. Linux plus KiCad 9.0:

```bash
ln -s /path/to/Chiplets-KiCad-Plugin/plugins/chiplet_export \
      ~/.config/kicad/9.0/scripting/plugins/chiplet_export
```

### 2. Set up the worker venv

`hyp_to_gds.py` needs `klayout>=0.28` and `PyYAML>=6.0`, neither of which
ships with KiCad's bundled Python. Install them into a separate venv:

```bash
python3 -m venv /path/to/Chiplets-KiCad-Plugin/plugins/chiplet_export/.venv
/path/to/Chiplets-KiCad-Plugin/plugins/chiplet_export/.venv/bin/pip install -r \
    /path/to/Chiplets-KiCad-Plugin/plugins/chiplet_export/requirements.txt
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

Every run writes the canonical `.chiplet` (the GDS-bbox-corner-anchored
assembly file consumed by chiplet-studio) and the interposer GDS it
references. Neither is a toggle: `hyp_to_gds.py` produces both on every
run regardless, and the `.chiplet` is unusable until the same run
finalizes it, so an opt-out could only have thrown away work already
done — and did, silently, when the `.chiplet` was skipped while the
assembly DRC still ran. `ExportOptions.emit_chiplet` survives as a
headless escape hatch.

The worker finalizes a hidden staging file next to the `.chiplet`,
`.<board>.chiplet.staging`, and the canonical `.chiplet` is replaced by it
only when the layout worker succeeds. A staging file left by an export
that was killed is removed at the start of the next one, and the log
names it; two exports into one output directory at the same time are not
supported. A run whose layout worker fails or is cancelled leaves an
existing `.chiplet` as it was (and creates none where there was none)
and keeps what it staged as `<board>.chiplet.unfinalized`. The same
happens, reported as an error, when the `.chiplet` changed on disk while
the worker ran. A `.chiplet` that is not writable is refused before the
worker starts. The replace comes before the assembly DRC, so a cancel
during the DRC does not undo it; the log says when the `.chiplet` was
replaced. The `.hyp`, the GDS layouts under `layout/`, the die GDS
bundled under `chiplets/<ref>/`, the DRC output under `reports/` and
`MANIFEST.md` are not protected this way: a failed run can still
overwrite them.

Tick what you additionally want produced:

- *Complete assembly GDS* (default OFF): interposer plus all chiplet
  instances flattened into one GDS. Required by the assembly DRC.
- *Annotate chiplet boundaries (viewer-only layer)* (default OFF): paint
  each chiplet boundary and instance label onto annotation layer
  `1000/0` for eyeball inspection in KLayout. No DRC rule reads this
  layer; the assembly contract stays in the `.boundaries.json` manifest.
- *Insert metal density fill (interposer GDS)* (default OFF): stamp
  floating dummy metal on the interposer BEOL to meet the IntM4TM2 CMP
  density rules. See *Metal density fill* below.

The Hyperlynx `.hyp` that drives the pipeline is always written to the
output directory (next to the `.chiplet`), so other tools can consume the
exact netlist the layout was generated from.

Each free track end is square in the GDS. The copper runs w/2 past the
end along the track, which is the rectangle a PDN stripe reserves. An end
that another track of its net runs through, as wide or wider, is drawn
flush instead: the board's round cap there lies inside that track. The
board draws a free end as a round cap, so at a free end with nothing under
it the GDS holds a little more copper than KiCad shows: at most
(sqrt(2) - 1) * w/2 at the cap's corners, 0.34 um on a 1.64 um track and
4.1 um on a 20 um stripe. A clearance KiCad passes can still fail the
KLayout deck by that margin, never the other way round, and the deck on
the GDS is the judge. Where two nets' tracks touch on one layer, which on
a board that passes its own clearance check means a square end, the export
refuses and names each place, since merged copper is a short no spacing
rule reports; widen that clearance. An end that reaches another net's pad,
via or die copper is not checked here: the export runs no LVS, so that is
left to an LVS on the GDS. A bend keeps its mitred corner.

Machine-readable sidecars land next to each generated GDS:

- `<stem>.boundaries.json`: one mechanical boundary polygon per placed
  chiplet (the PDK-agnostic assembly-DRC contract). A die with a declared
  outline gets that outline, where it lands at any rotation, as its
  boundary; an exempt die gets its placed cell's bounding box over all
  layers (see *Die outlines*).
- `<stem>.pillars.json` (schema `adk-pillar-manifest`, version `1.0.0`):
  the as-drawn Cu-pillar/bump centers — one record per placed bump with
  device reference, pin name, connection method, x/y in the canonical
  interposer GDS-bbox-corner frame (the same frame the `.chiplet` die
  positions and io_pads use, so manifest-level checks can compare the two
  sidecars directly; y-up, micrometers, after collision auto-resolve;
  bumps the auto-resolve shifted are flagged `moved_by_auto_resolve`) and
  the method's body diameter. Written whenever the bump-generation path runs,
  including runs that place zero bumps (empty `pillars` array). The x/y
  values are authoritative for manifest-level checks; the GDS remains
  the fabrication ground truth.
- `<board>_cupillar_drc.json`: the complete Cu-pillar DRC report
  (per-method parameters, per-device results), so warn-and-continue
  violations survive past the dialog log.

Readers of both manifests exact-match the `version` string; producer and
readers are bumped together.

### Metal density fill

Aluminium-BEOL interposers must meet a fab CMP-planarity density window, so
each metal needs floating dummy fill. The fill is **not** generated in KiCad:
KiCad copper zones are net-connected pours that land as real metal, and they
cannot target the IntM4TM2 density bands. Instead the plugin invokes the
interposer PDK's own fill engine (`fill_closure.fill_stack`) on the exported
GDS, so the density rules live in one place (the PDK, checked against the same
sign-off deck) and never diverge. The tiles are regenerated on every export;
you do not hand-edit them.

What you edit in KiCad are the two *inputs* the fill respects:

- **Real metal** you draw becomes drawn metal in the GDS, and the fill keeps
  clear of it automatically.
- **No-fill (keep-out) regions**, drawn as graphic polygons (or keepout
  zones) on dedicated **non-copper** layers. Never author a keep-out on a
  copper layer: it would export as real metal through the `.hyp`. Rename user
  layers to these names (case-sensitive):

  | KiCad layer   | keeps fill out of |
  |---------------|-------------------|
  | `NoMetFiller` | all metals (global)|
  | `M4.nofill`   | Metal4 only       |
  | `M5.nofill`   | Metal5 only       |
  | `TM1.nofill`  | TopMetal1 only    |
  | `TM2.nofill`  | TopMetal2 only    |

  These are read straight off the board into a sidecar and painted onto the
  GDS keep-out datatypes (`160/0`, `<metal>/23`), which the PDK generators
  already subtract. No layer means no keep-out; you can add them incrementally.

Two modes: *Single pass (fast)* stamps every metal once; *Density closure*
iterates Metal4/Metal5 against the sign-off density deck until in band
(slower, and it runs the deck). Fill runs on the interposer GDS only (the
complete assembly GDS is left unfilled). Requires an `INTERPOSER_PDK_ROOT`
checkout that includes the fill engine, and the `klayout` binary on `PATH`
(same dependency as the assembly DRC); without the binary the step soft-skips
with a message rather than failing the export.

Read-back: a per-metal density report lands at
`reports/<board>_fill_density.json`, and a coarse coverage map
(`layout/<board>_interposer.fill_coverage.json`) is painted onto a KiCad
read-back layer (`FillCoverage` if that user layer exists, else `Dwgs.User`)
for a canvas glance. The read-back is viewer-only; the GDS is the ground truth.

The engine is the `libs.tech/klayout/python/fill_closure.py` of the interposer
PDK: under `INTERPOSER_PDK_ROOT` when that is set (a root without it is an
error, with no fallback), else of the sibling checkout. A filling run prints
`Fill engine: <path>` with the file it imported. The boundary sidecar is
written after the fill, so its `assembly_gds_sha256` is the digest of the
filled bytes; a fill that stops leaves no sidecar.

### Seal ring

`--seal-ring` (on `chiplet-export`, `ExportOptions.seal_ring`, and the worker)
places the interposer PDK's `sealring` PCell on the board outline, which must
be one axis-aligned rectangle; the PCell's `EdgeSeal.boundary` (39/4) is then
the outline itself. Off by default. The export prints
`Seal ring: drawn on the board outline (x0, y0)-(x1, y1) um, l x w um`, and
`Seal ring: carried into the complete GDS` after the complete GDS.

With the ring drawn, no-fill keep-outs are clipped to 39/4, and the export
stops before writing (exit 1, `ERROR: seal ring refused: ...`) when the
outline is missing, not a rectangle, or under the PCell minimum, when the
PCell cannot be loaded, or when the export's own fabricated layers sit in the
ring band or closer to the ring than their space rule (`Mn_b`, `TM1_b`,
`TM2_b` for the drawn metals, one grid step for the rest). Only the basic
space is checked; the wide-metal space (`TM2_bR`) is left to the full deck.

### Technologies from the System .chiplet

A plain export has only the technologies the writer derives from
layer-properties paths the board names (the two-die example's are); a board
that names none, as a flow board, gets the interposer and dies without
`technology:` and an empty `technologies: {}`. A die's technology id is its
`LYP_FILE` stem (`sg13g2.lyp` gives `sg13g2`), except for a project's own
sheet, `inputs/tech/<id>/layers.lyp`, which takes the folder that holds it.
Two dies whose different sheets give one id share one entry, and the
export prints a warning naming both dies, both sheets and the one it kept.
The kept sheet is the one met last in pcbnew's footprint order, as in the
C++ exporter, not the die added last.

`--system-chiplet <design.chiplet>` (on `chiplet-export`,
`ExportOptions.system_chiplet`, and the worker, with
`--update-chiplet-file`) names the System `.chiplet` the assembly was built
from. The finalizer then gives the interposer and each die the technology of
the System component with the same id, and writes `technologies` as the
System's entries for exactly the ids it bound, copied as written. Nothing is
inferred and no `.lyp` is looked up. The export prints
`Technologies from <path>:` and one `<id>: <technology>` line per component.
A source `.chiplet` (one with a top-level `system` block) is refused as
`--system-chiplet` and nothing is written; pass the elaborated `design.chiplet`.

The export stops before writing (exit 1, `Technology refused: <id>: ...`)
when a component has no System component of that id (or more than one), when
the System component has no technology, when its technology is not declared
in the System's `technologies`, or when a bound entry's `layer_properties` or
`stackup` is a relative path (anchored means absolute or starting with
`${`). A System die the board does not have is not an error, and a
wire-bond pad component (an `--io-pads` reference) is left alone. Without the
flag the output is unchanged.

### Full die layouts

A die's footprint `GDS_FILE` may name an abstract: a stand-in drawn on
layers the assembly can carry, for a die whose own layout is in another
technology. The complete GDS, its boundary and the `.hyp` place that
abstract. `--die-layouts <file>` (on `chiplet-export`,
`ExportOptions.die_layouts_json`, and the worker, which refuses it without
`--update-chiplet-file`) names each such die's full layout, with the die
contract's top cell and box:

```json
{"U2": {"layout": "/abs/U2.gds", "top_cell": "U2", "bbox_um": [0, 0, 1000, 1000]}}
```

The `.chiplet` then gives the die that layout (relative to the `.chiplet`
when the two share a root, and never copied under `chiplets/`) and that
top cell, taken as given rather than read as the file's one top cell. Its
metadata names the abstract the assembly places:

```yaml
metadata:
  assembly_view: abstract
  assembly_view_layout: ../abstract/U2_abstract.gds
```

`assembly_view_layout` is the abstract's path relative to the `.chiplet`'s
folder. The complete GDS, its boundary and the `.hyp` are the same with
or without the flag.

The export stops before writing (exit 1, `Die layout refused: <ref>: ...`)
when the sidecar is not a JSON object of entries with exactly `layout`,
`top_cell` and `bbox_um`, names a die twice, names a reference that is no
die of the `.chiplet`, or gives a relative or missing layout. It also stops
when a file does not agree with the contract: a full layout whose dbu is
not 0.001 um, a `top_cell` that is not one of its top cells (other top
cells may be there), or a top cell box, of the full layout or of the
abstract, that is not `bbox_um` exactly in 0.001 um database units. That
message names both files, both boxes and which one differs.

A KiCad GUI export of the same signed board still writes the abstract: the
dialog has no die layouts input.

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

Every export needs the interposer PDK. The plane the dies mount on (the
`.chiplet`'s `attachment_surface_z`) is its `beol.attachment_surface_z_um`,
read from `libs.tech/intm4tm2_stack.json` on each run; there is no value
written from memory. Without the PDK, or with a descriptor that does not
give that key as a number, the export is refused before anything is
written, and the message names `INTERPOSER_PDK_ROOT` and the key.

### Pipeline options

- *Top cell* (default `INTERPOSER`): the top-level cell name written
  into the interposer GDS.
- *Connection stack (default)* (optional): the methods declared by the
  selected interconnect PDK's manifest (for example `cupillar_opt1/2/3`,
  `sbump_sac305`, vendor methods). Empty means the writer keeps the
  dies' existing connection field untouched. This is the assembly-wide
  default; per-die rows below override it.

  A method whose landing the worker cannot draw is labelled
  `(unavailable)` here and in the per-die rows, its reason replaces the
  grey line, and Run stays disabled while a die uses it. The dialog asks
  the worker with `connection_methods.py`, the same command chiplet-export
  runs before it stages anything, and logs the command it ran. The query
  runs in the background, again whenever the worker or a PDK root changes;
  until it answers, or when it fails, nothing is marked and the export
  makes the same check at Run. The dialog checks every die row, while the
  export checks only the dies whose pins it draws.

  Each entry is labelled with the numbers the choice actually turns on —
  `cupillar_opt1 - 75um pitch, 44um dia` — and the grey line underneath
  spells the selected method out in full (pitch, minimum spacing,
  passivation opening, body diameter, stack height by layer, vendor).
  Pitch and spacing are the very rules the assembly DRC will check the
  design against. Every number comes from the interconnect PDK manifest:
  when no manifest is readable the dropdown falls back to bare method
  ids rather than showing values it cannot source.
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
- *Integrated MIM capacitors*: auto-extracted from the board's `cap_cmim`
  footprints and drawn into the interposer GDS from the IntM4TM2 `cmim`
  PCell. See *Integrated passives* below.

### Integrated passives (cap_cmim)

A footprint whose `Model` (or `Sim.Name`) field is `cap_cmim` is an
interposer-integrated MIM capacitor, not a die. On Run its parameters are
extracted into a `<board>_cmim_devices.json` sidecar and passed to the worker
as `--cmim-devices`, which instantiates the IntM4TM2 `cmim` PCell at each
footprint position. The device therefore has no `layout:` of its own and does
not appear as a component in the `.chiplet`: its geometry is part of the
interposer.

Read from the footprint: `w`, `l` (plate dimensions) and `m` (multiplier,
default 1). Both board conventions for `w`/`l` are accepted, because both
genuinely occur: a `um`/`u` suffix (`57.68um`, what the PDK's own
`cmim_footprint_gen.py` stamps into the `intm4tm2.pretty` footprints) means
micrometres, and a bare number (`5.768e-5`, how `cap_cmim.kicad_sym` stores
it) means metres. The sidecar normalises both to micrometres in `w_um`/`l_um`.

The footprint position is the centre of the MIM plate; the PCell draws that
plate from its own origin, so the placed instance sits at the plate's
lower-left corner, snapped to the technology grid (`techParams.grid` of the
interposer PDK).

This path needs the interposer PDK: `INTERPOSER_PDK_ROOT` must resolve, since
the PCell comes from `libs.tech/klayout/python/intm4tm2_pycell_lib`. A device
that cannot be placed (PDK unavailable, or `w`/`l` the PCell rejects) **fails
the export** naming the refs, rather than shipping an interposer GDS with a
capacitor silently missing.

### Per-die settings

One row per die footprint (a footprint with a `GDS_FILE`), shown only
when the board has die footprints. Each setting is initialized from that
footprint's fields and written back to them on Run, so the board stays
the source of truth.

**Interconnect method** (`CONNECTION` field). A die left on *(use
default)* follows the assembly-wide *Connection stack* above; an
explicit selection gives that die its own connection stack, 3D bodies
and DRC numbers. A value the current PDK does not declare is kept in the
list rather than dropped, so pointing at another checkout never silently
rewrites a board's selection.

**Outline layer** (`DIE_OUTLINE_LAYER` field). The layer the die's own
GDS draws its outline on, as `LAYER/DATATYPE`: two integers from 0 to
65535, nothing else (`39/4`, not `39 / 4` or `39.0/4`). The export sizes
the die from the one rectangle on that layer, both its `.chiplet`
width/height and its boundary in the complete GDS (see *Die outlines*).
There is no default. Every die the `.chiplet` exports needs the field,
unless it is marked with `CHIPLET_PAD_DOMAINS`: a marked die is exempt,
keeps the size archived with its PinList (see *Carrier electrical pads
and die pin artwork*), and its field is never read. The `.chiplet`
exports every footprint that is not excluded from the bill of materials,
an IO pad (`IO_CLASS`) or a `cap_cmim` capacitor as a die, so an
ordinary part (a resistor, a connector, a logo) refuses as a die without
a `GDS_FILE` until it is excluded from the bill of materials. Exclude it
on its symbol in the schematic: *Update PCB from Schematic* copies the
attribute from the symbol and undoes a change made only on the
footprint. A part that is only on the board is excluded on its
footprint.

The dialog's *Outline layer (L/D)* column edits the field. It shows the
field text exactly as the board has it, a malformed value included, and
a `layer/datatype` hint when there is none; a marked die's row says it
is exempt and keeps its archived PinList size, and a footprint the
`.chiplet` does not export as a die says so. Run checks the column
before it changes anything: an empty or malformed row, and anything
else the export would refuse (below), stops it with one message box
naming every offender, each cause once with the references it applies
to, with no field written back and no export started. Once the check passes, the rows are written back to the fields
like the others.

`run_export` runs the same check on the board, for every die at once,
before the output directory or any file exists, so a headless run
(`chiplet_export_cli.py`, `tests/regenerate_wirebond_demo.py`) refuses
the same way. The refusal is one line, naming the references first and
then each cause once, with the references it applies to (wrapped here):

```
Die outline layer refused: U3, J1, J2: U3: no DIE_OUTLINE_LAYER field.
Set DIE_OUTLINE_LAYER to the LAYER/DATATYPE its GDS draws the die
outline on (39/4 for SG13G2 dies); J1, J2: no GDS_FILE field, so there
is no GDS to read its die outline from. If it is not a die, set
'Exclude from bill of materials' on its symbol in the schematic ...
```

It refuses a die without the field, with an empty or malformed value,
or without a `GDS_FILE`. It refuses a reference two dies share, or a
die shares with an IO pad (`IO_CLASS`) or with any other footprint that
has a `GDS_FILE`, since the worker finds those by reference too. It
refuses a reference that is empty, contains `,` or `=`, or starts or
ends with whitespace, and one the `.hyp` or the `.chiplet` would change:
a double quote, a tab or a line break, which the `.hyp` rewrites, or a
token the `.chiplet` writes unquoted and YAML reads back as something
else (`007` is 7, `1_0` is 10, `1.50` is 1.5). The worker then reads the
declared layer (*Die outlines*) and refuses what the GDS itself gets
wrong.

Known dies:

| Die | `DIE_OUTLINE_LAYER` | Note |
|---|---|---|
| `Metal_Test` | `39/4` | SG13G2 `EdgeSeal.boundary`, one rectangle. Not `189/4`, which `Metal_Test` does not draw. |
| `CF_DRIVER_BONDPAD` | `39/4` | SG13G2 `EdgeSeal.boundary` |
| `CF_ECHO_BONDPAD` | `39/4` | SG13G2 `EdgeSeal.boundary` |
| `FMD_QNC_OTA_TIA` | `39/4` | SG13G2 `EdgeSeal.boundary` |
| `Interposer_ANT_CuPi` | `39/4` | SG13G2 `EdgeSeal.boundary` |
| `tv1_hub` | `206/0` | |
| `tv1_spoke` | `206/0` | |

**Die thickness** (`DIE_THICKNESS_UM` field). The physical z-extent of
the *silicon die body*, written to the `.chiplet`'s
`components[].dimensions.thickness`. 750 um is a standard SG13G2 die,
shown as a placeholder — a hint only: an untouched field is never
stamped onto a board that never declared one.

This is **not** the interconnect thickness. A method's stack height
(24–80 um across the shipped methods) comes from the interconnect PDK
manifest and lands in `position.z = attachment_surface_z +
stack_height`, the exact decimal sum of the values as written (10.8103 +
28 + 16 is written 54.8103, and a height with five decimals keeps them);
it is fab data and deliberately not editable here. The
die body extends upward from that seating plane and is independent of
it.

Leaving it empty is not free: the die exports with `thickness: 0.0`,
which `adk/openroad/chiplet2dbx.py` rejects outright, and which
chiplet-studio (200 um) and `assembly_multiphysics` (250 um) each
silently replace with a different guess. The export logs a warning
naming every die that ships a zero, and a second one for values outside
~50–2000 um, which are almost always millimetres or nanometres typed
into a micrometer field.

### Die outlines

The worker sizes every die of the `.chiplet` from one layer of the die's
own GDS, never from the footprint courtyard and never from a bounding
box. Two flags say which:

```
hyp_to_gds.py board.hyp --update-chiplet-file board.chiplet \
    --die-outline-layers U1=39/4,U2=39/4 --die-outline-exempt U3
```

- `--die-outline-layers REF=LAYER/DATATYPE[,REF=LAYER/DATATYPE...]`: the
  layer each die draws its outline on. LAYER and DATATYPE are integers
  from 0 to 65535. SG13G2 dies drawn with an edge seal declare `39/4`,
  the edge seal boundary.
- `--die-outline-exempt REF[,REF...]`: dies that keep the width/height
  already in the `.chiplet` (for a footprint carrying
  `CHIPLET_PAD_DOMAINS`, its archived die boundary). The log names each
  one as exempt.

Every `.chiplet` component of type `die` must be in exactly one of the
two. The exception is a die component whose id is the reference of an
`--io-pads` entry: that is a wire-bond pad a tool other than this plugin
wrote as a die, the interposer's `io_pads` replace it, and it needs no
flag (a flag naming it refuses). Parsing is strict: an empty token, a
value that is not `LAYER/DATATYPE`, a reference given twice, or a `=` in
an exemption is an error naming the token.

With `--with-chiplets` and no `--update-chiplet-file` there is no list
of dies, so the flags only choose boundaries: a reference they name must
be a HYP device, and a device neither names keeps its cell bounding box
(the log says so). Without either of those two flags the outline flags
are ignored, with a warning.

The die GDS must have a dbu of 0.001 um, the assembly layout's. The
complete GDS imports a die into that layout without rescaling, and a file
with another dbu would change the scale of everything already drawn. The
read opens the die GDS on its own, takes its one top cell (the cell
no other cell instantiates, as the complete GDS import does), collects
the declared layer through the whole hierarchy under it and merges it.
The result must be exactly one axis-aligned rectangle with a nonzero
width and height. Abutting or overlapping pieces that merge into one
rectangle count as one; boxes touching only at a corner do not; texts
are ignored. `dimensions.width`/`height` become that rectangle's size in
the die GDS top cell's frame (not swapped for rotation; `rotation.z` and
`orientation` carry the placement), computed exactly from the integer
database units and the die layout's dbu, so 123457 DBU at dbu 0.001 is
written as `123.457`. In the complete GDS the same rectangle, placed
with the die, is the die's boundary in `<stem>.boundaries.json`.

A die that cannot be sized refuses the whole run, before the interposer
GDS, any sidecar or the `.chiplet` is written. Each offending die gets
one line, `Die outline refused: <ref>: <cause>`, all of them in one
run, and the worker exits 1. The causes:

- the die is in neither flag (or no flag was given at all, as when an
  intermediate `.chiplet` is finalized by hand without them), or in both;
- a flag names a reference that is not a die of the `.chiplet`;
- two `.chiplet` dies share an id, or two HYP devices share a reference
  that is a die or that a flag names (two `REF**` footprints that are
  neither are left alone);
- the die has no GDS (no HYP device with a `GDS_FILE` for it and no
  `layout` on the component), or its GDS is missing, unreadable, has a
  dbu other than 0.001 um, holds no cell or more than one top cell. This
  applies to exempt dies too;
- the declared layer is not in the GDS, carries no shape under the top
  cell, carries only shapes of zero area, merges to more than one shape, or
  merges to one shape that is not a rectangle.

A device the read does not cover (no `.chiplet` die and no flag names it)
is still placed in the complete GDS. If its GDS has another dbu, the run
fails there and no complete GDS is written, although the interposer GDS
and the `.chiplet` already are. So does a die that was read but cannot be
placed.

`run_export` derives both flags from the board, in the preflight
described under *Per-die settings*: `--die-outline-layers` from each
die's `DIE_OUTLINE_LAYER` field, `--die-outline-exempt` from the
`CHIPLET_PAD_DOMAINS` markers. Its log names both. A non-empty
`ExportOptions.die_outline_layers` (`{ref: "L/D"}` or `{ref: (layer,
datatype)}`) replaces the board fields wholesale, as the dialog's column
does, and goes through the same preflight. `ExportOptions.die_outline_exempt`
is not an input: `run_export` overwrites it with the marked dies.
`chiplet_export_cli.py` has no option for either; it reads the board.

### Worker Python

Which interpreter runs `hyp_to_gds.py` and the ADK DRC. The field shows
the interpreter the discovery chain resolved (env var, plugin `.venv`,
project text variable) as a placeholder, so an empty field reads as
provenance rather than as something missing. Type or browse a path to
override it — the only override that takes effect without restarting
KiCad, which matters when this checkout has no `.venv` (installs from
the `adk-tools` submodule) or the auto-detected one is incomplete. A
path that is not an executable file is rejected before the run starts.

### Assembly DRC

When *Complete assembly GDS* is enabled, the export runs the ADK's
`run_drc.py` (under the ADK root) over the complete GDS after
`hyp_to_gds.py` finishes. The verdict, `assembly DRC: PASSED / FAILED /
NOT RUN`, plus the report path is appended to the dialog log. A DRC
failure does not invalidate the exported artifacts: in the dialog and in
`ExportResult` the export exit code stays 0 and the status line flags the
failed deck separately. The headless `chiplet-export` does fold the DRC
into its exit code (see [CLI.md](CLI.md)).

## Troubleshooting

**"Could not locate a usable worker Python"**

The plugin tried every discovery step and none worked. Fix one of:

- Create the recommended `.venv` (see [Install section 2](#2-set-up-the-worker-venv)).
- Set `KICAD_CHIPLET_PYTHON=/absolute/path/to/python` in the shell that
  launches KiCad.
- Set `KICAD_CHIPLET_PYTHON` as a project text variable in
  *Board Setup > Text Variables*. This leg reads the project through
  `pcbnew.ExpandTextVars`, since KiCad's Python bindings never wrapped
  `PROJECT` and `board.GetProject().GetTextVars()` cannot be called at all.
- Point the dialog's *Worker Python* field at one directly (takes effect
  without restarting KiCad; note a present-but-broken `.venv` wins over
  the project text variable, so this is the way past it).

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
plugins/chiplet_export/
├── __init__.py                Registers the ActionPlugin with pcbnew
├── chiplet_export_action.py   ActionPlugin subclass + Run() entry
├── chiplet_export_cli.py      The chiplet-export command (CLI.md)
├── CLI.md                     chiplet-export reference
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
│                              --cmim-devices, --die-outline-layers,
│                              --die-outline-exempt, manifest-sourced
│                              --connection-type, the .boundaries.json /
│                              .pillars.json sidecars)
├── die_outline_gds.py         Die outline read from each die's GDS
│                              (worker side, klayout.db)
├── die_layouts.py             Full die layouts sidecar and frame check
│                              (worker side, --die-layouts)
├── technology_overlay.py      Technology ids from the System .chiplet
│                              (worker side, --system-chiplet)
├── tests/                     pytest suite (see tests/README.md)
└── requirements.txt           Worker venv deps

(LICENSE and CI live at the repository root, one level above plugins/)
```

## Headless usage

From a shell, use `chiplet-export`; [CLI.md](CLI.md) documents it in full,
exit codes included.

From Python, the pipeline runs without the dialog through
`pipeline.orchestrator.run_export(board, options, plugin_dir)`:

```python
import pcbnew
from chiplet_export.pipeline.orchestrator import (
    ExportOptions, run_export,
)

board = pcbnew.LoadBoard("/path/to/board.kicad_pcb")
options = ExportOptions(
    output_dir="/tmp/chiplet_out",
    emit_chiplet=True,
    emit_complete_gds=True,
    top_cell="INTERPOSER",
    lyp_override="/path/to/intm4tm2.lyp",
    io_pads_json="",
    cupillar_gds="/path/to/cu_pillars.gds",
)
result = run_export(
    board, options,
    plugin_dir="/path/to/Chiplets-KiCad-Plugin/plugins/chiplet_export",
    on_log=print,
)
assert result.exit_code == 0 and not result.error
```

`result.exit_code` is the worker's; it does not carry the assembly DRC,
which is in `result.assembly_drc_exit_code` (-1 NOT RUN, 0 PASSED).

`chiplet_export_cli.py` wraps this call for a shell and exits with its own
table (see [CLI.md](CLI.md)).

`tests/regenerate_wirebond_demo.py` is a full worked example; its default
mode uses the same table and flags.

## Read-only board observation

Use the independent observer to capture placement, raw footprint properties,
physical pads and their assigned nets. Run it with a Python that has the KiCad
`pcbnew` bindings:

```bash
python3 plugins/chiplet_export/board_snapshot_cli.py \
  --board /path/to/source.kicad_pcb \
  --output /path/to/new-observation.json
```

The source PCB is never saved. The output must be new: existing files, including
earlier observations, are refused. Publication is atomic and exclusive, and the
source SHA256 is checked before loading and after observation. A failure returns
exit code 1 and leaves no partial observation. `--help` needs no KiCad installation.
This command does not invoke the export pipeline, load GDS, or infer a System
revision. It only observes a saved board.

The complete observation-to-System workflow, with the revision commands, supported
edits and rerouting procedure, is documented with the packaging flow.

The JSON contract is `format: chiplet_kicad.board_observation`, `version: 1`,
`units: mm`, `frame: kicad_y_down`. `source` contains the absolute PCB `path` and
`sha256`; `nets` is a list of `{code, name}` records, including code zero for
unassigned pads. The CLI also records `producer: {tool: pcbnew, version: ...}`
using `pcbnew.GetBuildVersion()`; an unavailable version refuses explicitly.
Each footprint records `reference`, `uuid`, `at_mm`,
`rotation_deg`, `layer`, raw string `properties`, and a `pads` list. Each pad
records `number`, `uuid`, `at_mm`, `size_mm`, `rotation_deg`, `shape`, `layers`,
`net_code` and `net_name`.

Both footprint and pad positions are **absolute, global KiCad coordinates**,
with Y down. Angles are raw KiCad API values. No grid snapping, recentering,
rotation normalization or tolerance is applied; consumers own those decisions.
`ORIENTATION` stays a raw property, including absence or an empty value; a
footprint on B.Cu does not imply flip-chip attachment. All numbers are finite.
`layer` and `layers` use `board.GetLayerName()` verbatim: they may be custom
names such as `TopMetal2`, rather than serialized layer tokens such as `F.Cu`.

Pads remain a list, including repeated numbers, empty numbers, unassigned pads
and singleton nets. Presentation ordering carries no identity. Pad `uuid` is
always `null`: `LoadBoard` can generate new UUIDs for source pads that
have none, and the API cannot identify their provenance. Footprint UUIDs are raw
API observations and also must not be assumed to prove authored identity. Match
physical pads against the original pin inventory in the consuming engine.

`outline` captures board and footprint Edge.Cuts shapes in global coordinates:

| `shape` | Geometry keys (all lengths in mm) |
| --- | --- |
| `segment`, `rect` | `start_mm`, `end_mm`, `width_mm` |
| `arc` | `start_mm`, `mid_mm`, `end_mm`, `width_mm` |
| `circle` | `center_mm`, `radius_mm`, `width_mm` |

Unsupported Edge.Cuts objects, polygons and Beziers refuse explicitly. Supported
pad shapes are `rect`, `circle` and `oval`. Pads must be SMD, have no drill or
copper offset, and use a uniform `NORMAL` padstack. Unsupported parameters and
missing inspection APIs refuse with the footprint reference, pad number and
inspection context; they are never assumed to be zero or normal. Shapes requiring
additional geometry parameters also refuse. The observer does not validate
outline closure or infer connectivity from tracks, vias or zones. Immutable-geometry checks and logical
reconciliation belong to the consuming engine.

The in-memory entry point is `board_snapshot.observe_board(board)`; the CLI adds
source path/digest and producer-version provenance. Run the hermetic tests without KiCad or pytest:

```bash
python3 -m unittest discover -s plugins/chiplet_export/tests \
  -p 'test_board_snapshot*.py' -v
```

## Carrier electrical pads and die pin artwork

New routed footprints use `CHIPLET_PAD_DOMAINS` version 1. Their electrical
KiCad pads represent **carrier landings**, sized by the selected interconnect
manifest's passivation opening plus twice its metal enclosure. Die pin openings
are separate, non-electrical F.Fab artwork. Equal layer names in different dies
and the carrier do not make their metals one electrical plane.

Pad numbers and net assignments keep their ordinary KiCad meaning. The hidden
record preserves the complete original die PinList and an explicit physical
pin-index mapping; it is not a second editable netlist. Moving or rotating the
whole footprint is supported by the corresponding flow revision contract.
Editing pad geometry, attachment method, or the hidden record requires
regeneration, not a manual size adjustment to clear a DRC marker.
Marked footprints also require the original `die_bbox_dbu`: exported die
width/height come from that archived boundary, in die-local coordinates. Editing
F.CrtYd, artwork, or text cannot resize the physical die or swap its dimensions
under rotation. The intermediate `.chiplet` still carries an unmarked
footprint's courtyard size, but the worker replaces it with the outline read
from the die's own GDS, or refuses (see *Die outlines*). A marked die keeps its
archived size: the export passes it as exempt and never reads its
`DIE_OUTLINE_LAYER`.

The export preflight checks every marked footprint. Explicit pin-location or
pre-generated pillar overrides are refused when any marked footprint is present:
those options replace the checked geometry rather than augment it. A per-die
method override is accepted only when the effective method for each marked die
is unchanged. To use a different attachment, regenerate the board with that
method; do not strip its metadata. Legacy-only boards retain their override
semantics. Invalid/unsupported
metadata, stale manifest dimensions, ambiguous physical pins, and changed
electrical geometry refuse before any output is created. Repeated terminal
names are matched by their rigid local positions, never pad enumeration or
synthesized UUIDs. The observation CLI remains a raw geometry/property observer.

For marked footprints, preflight also resolves the current board layer name
and the declared carrier metal through the worker's selected LYP. A renamed
layer that resolves to another metal, an unknown name, or a conflicting LYP
definition refuses before any output is touched. The same resolved LYP path
is then passed to the worker. Check the board's copper layer names and selected
LYP when this check fails; changing a KiCad layer's name does not change its
internal layer identifier. Unmarked exports retain the existing layer policy.

The supported fabrication profile is circular TopMetal2 landings, mapped to
the canonical KiCad copper layer recorded by the producer. The record's anchor
must be the selected manifest method's `fab_anchor`, and the worker draws a
landing only when the UBM openings that anchor declares in the manifest's
`fab_anchors` table are the ones the interposer PDK's pad cell draws. Today
that cell is CuPillarPad, so `ihp_cupillar_anchor` methods export and an
`ihp_sbump_anchor` method (`sbump_sac305`) is refused before the GDS is
written instead of losing its landings, whenever the worker draws the pads
from pin locations. `connection_methods.py` runs that check for every method
and prints the result as JSON; the dialog and chiplet-export use it to refuse
such a die before the export stages anything. A pre-generated `--cupillar-gds` is merged as given; for
marked boards the preflight refuses that override. For every method the
landing diameter is `passiv_opening_um + 2 * tm2_enclosure_um` from the
manifest. A solder bump's `body_diameter_um` is the ball, a 3D body only, and
neither number is derived from the other. The HYP
writer deliberately exports the **carrier** padstack geometry; the die pin-list
writer instead retains original die dimensions and physical indices. Its known
geometry fields are normalized to nm in footprint-local GDS Y-up coordinates,
with flip-chip X mirroring applied once. The downstream pillar generator applies
the footprint rotation. Unknown source geometry fields are preserved only in
the immutable embedded original, not copied into a differently framed sidecar.

Unmarked historical footprints retain their old pad-derived extraction, with
an explicit legacy/no-carrier-qualification diagnostic. Removing all metadata
from a standalone board is indistinguishable from a genuinely historical board;
the automated flow's pinned export receipt is required to detect that deletion.
Do not interpret legacy export success as carrier-domain validation. Recorded
source hashes are provenance; this plugin does not verify source-file contents
by following shared-filesystem paths. Unit/API-double tests are not native
save/reload, PCell, full DRC, or LVS evidence.

## References

- `chiplet-system-spec/docs/coord_frame_contract.md`: the canonical
  coordinate frame the writers honour (GDS-bbox-corner, y-up, um) and
  the `_metadata.finalize_required` intermediate-frame marker.
- `tests/README.md`: test layout, byte-exact parity, round-trip
  regression.
