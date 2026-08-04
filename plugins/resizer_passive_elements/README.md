# resizer passive elements plugin

A pcbnew action plugin, proof of concept, for resizing `cap_cmim` MIM
capacitor footprints already placed on a KiCad board so their pads match
whatever `w`/`l`/`Capacitance` the schematic actually asks for --
without hand-maintaining one `.kicad_mod` per possible value.

It wraps `libs.tech/kicad/scripts/cmim_footprint_gen.py` from the shared
`OpenIntM4TM2` PDK repo (untouched, imported as-is) to regenerate the
exact plate geometry the KLayout `cmim` PCell would produce for the
same `w`/`l`, then replaces the placed instance with that generated
footprint in the same run. The replacement preserves placement, nets,
and the technology metadata needed by the GDS/PCell exporter, so the
generated file, the board view, and the export contract stay aligned.

See **`ARCHITECTURE.md`** for a deep dive into how the code is put together --
in particular, exactly how to add support for a new device type (resistor,
inductor, ...) once its PCell/generator exists.

## Status

Proof of concept. One `Tools > External Plugins` entry opens a single
window with a single **Run** button (chains Scan, Generate, Apply and
Refresh) and a tall shared log. No automatic/reactive execution: nothing
runs until Run is clicked. Only `cap_cmim` is supported; resistor/
inductor devices don't have a PCell in the PDK yet.

## Install

This plugin is hosted in the `Chiplets-KiCad-Plugin` repository under
`plugins/resizer_passive_elements`. For development, make only this plugin
directory discoverable by pcbnew; do not copy or symlink the whole repository.

### 1. Make the plugin discoverable by pcbnew

Symlink (recommended for development) or copy this plugin directory into the
KiCad scripting plugins folder. Linux plus KiCad 9.0:

```bash
ln -s /path/to/Chiplets-KiCad-Plugin/plugins/resizer_passive_elements \
      ~/.config/kicad/9.0/scripting/plugins/resizer_passive_elements
```

Windows: `%APPDATA%\kicad\9.0\scripting\plugins\resizer_passive_elements`.

### 2. Restart pcbnew

Restart pcbnew or run `Tools > External Plugins > Refresh Plugins`. The new
entry appears under `Tools > External Plugins` as
`Chiplet / resizer passive elements > resizer passive elements`.

ADK-Tools consumes this repository from its own integration branch. Docker image
wiring belongs in ADK-Tools after this plugin lands here.

### Point it at the shared OpenIntM4TM2 checkout

No separate Python environment is needed: `cmim_footprint_gen.py` is
stdlib-only, so it runs directly inside KiCad's bundled Python. The
plugin needs to *find* two files from the shared `OpenIntM4TM2` checkout
-- `intm4tm2_tech.json` and `cmim_footprint_gen.py` -- and tries, for
each, in this order (first hit wins):

1. Environment variable `INTERPOSER_PDK_ROOT`, the ecosystem-wide name also
   used by `chiplet_export` and `hyp_to_gds`, so one variable configures every
   tool. `INTM4TM2_ROOT` stays accepted as an alias (it is what the ADK-Tools
   Docker image sets, as `/opt/adk-tools/OpenIntM4TM2`). A variable that is
   set but does not contain the file falls through to the next leg rather
   than failing the lookup.
2. The root saved from a previous session (see *Saved settings* below), then
   the same names as KiCad project text variables.
3. A sibling checkout on disk: any ancestor directory of the install
   location containing `interposer/` (the ecosystem's own checkout name) or
   `OpenIntM4TM2/`.
4. Hardcoded last resort: `/work/OpenIntM4TM2` (the project's Docker
   bind-mount convention). Only used once 1-3 all fail.

If none of those resolve (for example, a checkout mounted at some other
arbitrary path), the window's *intm4tm2_tech.json* and
*cmim_footprint_gen.py* fields open empty. Two ways to fill them in by
hand, right there in the window, before clicking Run:

- **OpenIntM4TM2 root folder** (top field): point it at the checkout's
  root (the folder that contains `libs.tech`) and both file fields below
  auto-fill the instant it finds them there -- typing or using its "..."
  folder picker both trigger the auto-fill.
- Or fill in **`intm4tm2_tech.json`** and **`cmim_footprint_gen.py`**
  individually with their own "..." file pickers, if the checkout
  doesn't follow the standard `libs.tech/...` layout the root-folder
  auto-fill expects.

## How to use

Open the board in pcbnew, run `Tools > External Plugins > resizer passive elements`.
One window opens with the four path fields at the top, a single **Run**
button below them, and a tall log filling the rest (sized generously by
default so it rarely needs manual resizing). **Check/edit the path
fields first** -- Run reads them fresh at the moment it's clicked, not
before, so there's no stale configuration from a previous run. The
window stays open across clicks, so click Run again any number of times
and keep reading the same log.

Clicking **Run** does all of the following, in order, every time:

1. **Scan** -- read-only. Lists every `cap_cmim` footprint on the board
   with its `w`/`l`/`Capacitance` fields, and warns (without stopping)
   about any capacitor with a missing or unparseable field.
2. **Generate** -- writes one `.kicad_mod` per capacitor into the
   configured local output folder (see below). Always (re)writes the
   file, even if one with the same name is already there -- a same-named
   file is never assumed to already be correct, since it could predate
   an update to the shared `cmim_footprint_gen.py` (e.g. one that adds
   new graphics), and writing a small text file is cheap enough that
   there's no reason to risk serving stale content just to skip it.
3. **Apply** -- the only part that modifies the board. **Replaces each
   placed instance's footprint** with the one just generated (loaded
   directly from the local output folder, no library-table registration
   needed). Reference, position, orientation and layer are carried over
   from the instance being replaced; net connections are copied across
   strictly by pad **number** ("1"->"1" PLUS, "2"->"2" MINUS), never by
   index, so pad-to-net connectivity never flips. Field **visibility**
   is carried over by field name -- e.g. if "Update PCB from Schematic"
   had made `Capacitance` visible, it stays visible after the swap too.
   For `cap_cmim`, the technology field text is then written explicitly:
   `Model=cap_cmim`, `Sim.Name=cap_cmim` when needed, final `w`/`l` in
   metres, `m`, final `Capacitance` and `Nominal` (see *Naming* below).
   This matters for GDS export:
   the resized footprint must still be recognizable as a parametric CMIM
   device, not just as visually correct pads. Afterwards, the `Footprint`
   shown in Footprint Properties for that instance will read the
   generated name (e.g. `CMIM_100fF`), so a resize is visually obvious,
   not just a same-named footprint with different pads underneath.
   **Save the board afterwards** (Ctrl+S) -- the plugin cannot reliably
   mark the board "modified" through the scripting API on every KiCad
   build, so it reminds you in the log if it couldn't.
4. **Refresh** -- forces a PCB editor redraw, in case a pad-size change
   from step 3 doesn't show up immediately.

### Naming

Generated filenames/footprint names are capacitance-keyed
(`CMIM_<label>`, e.g. `CMIM_100fF.kicad_mod`, `CMIM_1p5pF.kicad_mod`),
matching the display style of the official discrete-family footprints
committed in `OpenIntM4TM2` (`CMIM_10fF` ... `CMIM_5pF`).

Which capacitance names the part is not a detail. The PDK keeps two separate
properties and so does this plugin: `Nominal` is the round value the part is
*called* (`100fF`) and `Capacitance` is what the drawn plate actually *is*
(`99.96fF`). They differ because of the placement grid: the exact side for
100 fF is 8.111807 um, the 5 nm grid forces 8.110, and that costs 0.044%.
So `CMIM_100fF` really is the 100 fF part; naming it after its own recomputed
value would rename it `CMIM_99p956fF` on the next run, and again after that.

The rule: a part keeps its `Nominal` name for as long as `w`/`l` still are the
grid-snapped square for that nominal. The moment you resize it, the label no
longer describes the device, so it is dropped and the footprint is named for
the capacitance it now has (a 10x10 um plate becomes `CMIM_151p6fF`, and the
log says the nominal was dropped). A resized part never keeps the name of the
value it used to have.

The name is deliberately *not* `cmim_footprint_gen.py`'s own dimension-keyed
default (`footprint_name(w, l)`, e.g. `CMIM_8p11x8p11um`), which at the same
(tiny, plate-proportional) font size is almost twice as long and visibly
overruns the device's outline. If the generator's raw output ever
includes a `fp_text` label whose text is exactly the footprint's own
name (older versions placed a redundant one at the origin, duplicating
the Value property), this plugin strips it -- see
`apply_resize._strip_visible_name_label()`. Matching is by exact text
content, not "the first `fp_text` in the file": a newer
`cmim_footprint_gen.py` adds its own intentional polarity markers
(`+ TopMetal1`, `- Metal5`, `PLUS=In1.Cu  MINUS=In2.Cu`) as `fp_text`
elements too, and those must never be touched. Reference and Value
silkscreen/fab texts (offset clear of the device) are otherwise left as
the generator produces them.

The `CMIM_GENERATED_FILE` provenance field "Apply to Board" stamps onto
the replaced instance (recording which generated file it came from) gets
the same treatment for a different reason: KiCad creates any brand-new
footprint field visible on F.SilkS at 1.27 mm by default, which on these
um-scale devices renders as a giant label sprawling across the view.
`apply_resize._style_provenance_field()` moves it to F.Fab, hides it, and
shrinks its text -- the same fix the sibling chiplet_export plugin uses
for its own machine-managed fields. The hidden `Model`, `Sim.Name`, `w`,
`l`, `m`, and `Capacitance` fields are also styled this way after they
are refreshed, because they are machine-readable export metadata rather
than board artwork.

### The four configurable paths

- **OpenIntM4TM2 root folder**: optional convenience field. Point it at
  a checkout root and the two fields below auto-fill from it (only what
  it actually finds there -- it never clears a field that already has a
  value). Leave it empty and fill the two file fields directly if your
  checkout isn't laid out the standard way.
- **`intm4tm2_tech.json`**: the process-constants file
  `cmim_footprint_gen.py` reads. Pre-filled by the discovery chain above
  or the root-folder auto-fill; always editable/browsable directly too.
- **`cmim_footprint_gen.py`**: the generator script itself (imported,
  never modified). Same pre-fill/override behavior as the tech.json
  field above.
- **Local output `.pretty` folder**: where generated `.kicad_mod` files
  are written. Defaults to `local_footprints.pretty` next to the
  currently open `.kicad_pcb` -- **never** the shared
  `OpenIntM4TM2/libs.tech/kicad/footprints/intm4tm2.pretty`, and never
  touched by the root-folder auto-fill (it isn't part of the shared
  checkout). Change it only if you deliberately want output elsewhere.

### Saved settings

Whichever values are in these four fields when you close the window are saved
to `.resizer_passive_elements.json` next to the open `.kicad_pcb`, and
pre-fill the window next time you open it, so you only configure this once per
project -- including on a Docker image where auto-discovery can't find the
checkout on its own. The saved root also feeds the discovery chain above.

The file holds absolute, machine-local paths, so it is worth adding both it
and the output folder to the project's `.gitignore`:

```gitignore
.resizer_passive_elements.json
local_footprints.pretty/
```

Project text variables would be the natural home for this, and the plugin
still reads them (`CMIM_INTM4TM2_ROOT_DIR`, `CMIM_TECH_JSON`,
`CMIM_GEN_SCRIPT`, `CMIM_OUTPUT_DIR`), but KiCad's Python bindings do not
expose them: `BOARD.GetProject()` hands back an opaque object with no
`GetTextVars`, so writing them would be a silent no-op. If the board has never
been saved to disk there is nowhere to put the file, and the window says so in
the log instead of pretending the settings were kept.

## What it deliberately does not do (out of scope for this PoC)

- No working support for resistor/inductor devices yet -- see "Adding a
  new device type" below for what's already in place for when their
  PCells/generators do.
- Never changes the pad type (`smd` stays `smd`).
- Never modifies `cmim_footprint_gen.py`, nor anything inside
  `OpenIntM4TM2` (the generator, the shared `.pretty`, the symbols).
- Never writes into the shared `OpenIntM4TM2` checkout by default.
- No automatic/reactive execution (nothing runs on save or on "Update
  PCB from Schematic"); Run is a manual button press inside the window,
  never triggered by anything else.
- Does not register the local output folder in KiCad's library table
  (`fp-lib-table`); the Apply phase loads the generated footprint
  directly from the folder path (`pcbnew.FootprintLoad`) and doesn't
  need it registered anywhere.

## Adding a new device type (resistor, inductor, ...)

See `ARCHITECTURE.md` section 5 for the full worked example (a complete
hypothetical resistor reader/generator pair) and the one open question
this design doesn't solve yet (a device needing a genuinely separate
generator script/tech source). Summary:

Two registries, dispatched by the footprint's hidden `Model` field,
are the only places that know about `cap_cmim` specifically:

- `board_reader.DEVICE_READERS`: `{"cap_cmim": _read_cap_cmim_fields}`.
  A reader turns one placed footprint into a params dict (always at
  least `"reference"` and `"model"`; `_read_cap_cmim_fields` also adds
  `"w_um"`, `"l_um"`, `"capacitance_fF"`, parsed with cap_cmim's own unit
  rules). `find_supported_footprints()` just looks up the matched
  `Model` in this dict and calls whatever reader is registered -- it
  never hard-codes a device.
- `apply_resize.DEVICE_GENERATORS`: `{"cap_cmim": _generate_cap_cmim_footprint}`.
  A generator turns a params dict into a written `.kicad_mod` (or `None`
  + a logged reason). `generate_footprint_file()` dispatches on
  `params["model"]` the same way.

`dialog_log.py` (the UI/orchestration) does not know about specific
device types. `apply_resize.apply_to_instance()` is still generic for
identity, placement, and net transfer, but it has a small per-model
technology-field hook so `cap_cmim` replacements keep the metadata that
the GDS/PCell exporter needs. A new two-terminal device can reuse the
same pad-number replacement path, but it may need its own metadata hook
if its generated footprint must preserve fields for downstream export.

Adding resistor support once its PCell/generator exists means, in
`board_reader.py`:

1. Write `_read_resistor_fields(footprint, on_log) -> dict`, shaped like
   `_read_cap_cmim_fields`, parsing whatever fields that device actually
   carries (do not assume cap_cmim's "w"/"l"/capacitance parsing rules
   apply -- write its own).
2. Add `"res_<whatever the PCell's Model string turns out to be>":
   _read_resistor_fields` to `DEVICE_READERS`.

...and in `apply_resize.py`:

3. Write `_generate_resistor_footprint(params, tech, output_dir, on_log,
   gen_script_path) -> path or None`, shaped like
   `_generate_cap_cmim_footprint`, calling that device's own generator
   script's own functions (not cmim_footprint_gen.py's).
4. Add the same Model string mapping to `DEVICE_GENERATORS`.
5. If the downstream export flow needs fields on the placed footprint,
   add a small model-specific updater beside `_apply_cap_cmim_fields()`
   and dispatch it from `_apply_technology_fields()`.

Not yet solved, because there's nothing concrete to design it against:
if the resistor's generator turns out to need a genuinely separate
script/tech-file pair rather than another entry point in
`cmim_footprint_gen.py`, the dialog's single "cmim_footprint_gen.py" /
"intm4tm2_tech.json" fields (and the single `tech` object
`generate_footprint_file()` receives today) won't be enough -- that will
need its own path field(s) and a per-model `(tech, gen_script_path)`
lookup at that point.

## Troubleshooting

**"cmim_footprint_gen.py not found" / "could not load intm4tm2_tech.json"**

The interposer PDK checkout wasn't found. Set `INTERPOSER_PDK_ROOT` (or its
`INTM4TM2_ROOT` alias) to the checkout root, or fill in the
`intm4tm2_tech.json` field by hand in the window; the window remembers it.

**"ERROR: ... geometry cannot be computed"** (Scan/Generate/Apply)

That capacitor's footprint is missing both `w`/`l` and `Capacitance`, or
none of them parsed as a number. Fix the field on the symbol/footprint
instance and re-run "Update PCB from Schematic" in the schematic editor
first (this plugin never edits the schematic).

**A value out of the device's valid range (~2.13 fF .. 8 pF)**

`cmim_footprint_gen.py` rejects it and the plugin logs that error
verbatim against the capacitor's reference -- it is not translated or
hidden.

**"... is below the device minimum (Wmin=...)"**

The requested `w`/`l` is smaller than the device's minimum plate width
(~1.14 um): the TopMetal1 via array can't fit a single via, so pad "1"
(PLUS) would come out 0x0. Check the schematic's `w`/`l` field -- remember
it's stored in metres there (e.g. `8.11e-6`), not micrometres.

**Apply to Board ran but the board doesn't ask to save on close**

See step 3 above -- save manually (Ctrl+S). The log line "Note: save the
board manually..." confirms this happened.

**Plugin not visible under External Plugins**

Verify the install path, then
`Tools > External Plugins > Refresh Plugins`. A failed import prints to
KiCad's stdout/console.

## Repository layout

```
resizer_passive_elements/
├── __init__.py             Registers the single ActionPlugin with pcbnew
├── action_resizer_passive_elements.py   The "resizer passive elements" entry: Run() just opens the window
├── dialog_log.py           The window: path fields + a single Run button + a tall log
├── board_reader.py         find_supported_footprints() -- shared, no wx/pcbnew-heavy deps
├── apply_resize.py         generate_footprint_file() / apply_to_instance() -- shared
├── paths.py                Path discovery (OpenIntM4TM2 checkout) + persistence
└── README.md
```

## Headless usage

`board_reader.py` and `apply_resize.py` have no module-level `wx` or
`pcbnew` import (only `apply_to_instance` imports `pcbnew` lazily, at
the point it actually needs a live pad object), so the read/generate
logic can be exercised without a running KiCad:

```python
from resizer_passive_elements.apply_resize import generate_footprint_file, load_tech

tech = load_tech("/path/to/OpenIntM4TM2/libs.tech/klayout/python/intm4tm2_pycell_lib/intm4tm2_tech.json")
params = {"reference": "C3", "w_um": 8.11, "l_um": 8.11, "capacitance_fF": None}
path = generate_footprint_file(params, tech, "/tmp/local_footprints.pretty", on_log=print)
```
