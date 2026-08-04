# resizer passive elements plugin -- Architecture

This document is the deep-dive companion to `README.md`. The README
covers install and day-to-day use; this file explains **how the code is
put together, why it's shaped the way it is, and -- in detail -- how to
extend it to a new device type** (resistor, inductor, or anything else
that eventually gets a KiCad footprint generator in `OpenIntM4TM2`).

If you only need one takeaway: **two small dictionaries, keyed by the
footprint's hidden `Model` field, are the entire extension surface.**
Everything else in the plugin is already generic. Section 5 below is a
full worked example of adding a device type to those dictionaries.

## 1. File map

```
resizer_passive_elements/
├── __init__.py             Registers the ActionPlugin with pcbnew; defines __version__
├── action_resizer_passive_elements.py   The "resizer passive elements" entry: Run() just opens the window
├── dialog_log.py           The window: path fields + a single "Run" button + a log
├── board_reader.py         Reads supported devices off the board (DEVICE_READERS)
├── apply_resize.py         Generates footprints + swaps them onto the board (DEVICE_GENERATORS)
├── paths.py                Finds intm4tm2_tech.json / cmim_footprint_gen.py; persists overrides
├── README.md                Install/usage guide
└── ARCHITECTURE.md          This file
```

Two design rules run through every file below:

- **`board_reader.py` and `apply_resize.py` have no module-level `wx` or
  `pcbnew` import.** Only `apply_resize.apply_to_instance()` imports
  `pcbnew`, and it does so *inside the function*, right where it's
  needed. Everything else in those two files is plain Python operating
  on plain dicts and whatever `board`/`footprint` objects it's handed.
  This is why almost every behavior in this plugin could be verified
  with small hand-written stand-in objects (`FakeFootprint`, `FakeBoard`,
  `FakePad`, a `pcbnew` stub module) rather than a running KiCad --
  see Section 8.
- **`cmim_footprint_gen.py` (and everything else under `OpenIntM4TM2/`)
  is never modified, ever.** It is imported dynamically, by file path
  (`apply_resize._load_generator_module`), from wherever it currently
  lives, and called only through the small set of functions the
  original spec named as its public contract (`load_tech`,
  `cap_to_width`, `cmim_capacitance_fF`, `cap_bounds_fF`,
  `footprint_name`, `write_footprint`). Anything the generator's raw
  output doesn't get right for KiCad's rendering (see Section 4) is
  fixed by post-processing the file this plugin writes, never by
  touching the generator.

## 2. Runtime architecture

```
KiCad's Tools > External Plugins menu
        |
        v
__init__.py --------- registers -------> action_resizer_passive_elements.ResizerPassiveElementsPlugin
                                                  |
                                          Run() opens dialog_log.ResizerPassiveElementsDialog
                                                  |
                                    (nothing executes until "Run" is clicked)
                                                  |
                                                  v
                                     dialog_log._on_run()
                              chains, once each, in this order:
                              _on_scan -> _on_generate -> _on_apply -> _on_refresh
```

Each of those four phases calls into the two logic modules:

```
_on_scan / _on_generate / _on_apply
        |
        v
board_reader.find_supported_footprints(board, on_log)
        |  for each footprint on the board:
        |    look up footprint's "Model" field in DEVICE_READERS
        |    call that device type's reader -> params dict
        v
[ {reference, model, ...device-specific fields..., footprint_obj}, ... ]
        |
        v  (Generate / Apply only)
apply_resize.generate_footprint_file(params, tech, output_dir, ...)
        |  looks up params["model"] in DEVICE_GENERATORS
        |  calls that device type's generator -> writes a .kicad_mod, returns its path
        v
        |  (Apply only)
        v
apply_resize.apply_to_instance(board, footprint_obj, generated_path, params, ...)
        |  loads the generated file as a fresh FOOTPRINT (pcbnew.FootprintLoad)
        |  copies reference/position/orientation/layer/field-visibility/nets
        |  refreshes model-specific technology fields when needed
        |  adds the new footprint, then removes the old one
        v
board now has the resized/replaced instance
```

The important structural fact: `apply_to_instance()` keeps the physical
swap generic. It matches pads by *number* ("1", "2") and copies
identity/placement/nets without device-specific geometry logic. The only
model-specific part is the technology-field refresh hook: today
`cap_cmim` writes back the metadata needed by the GDS/PCell exporter.
See Section 5 for how a future device would add its own hook if needed.

## 3. Module reference

### `board_reader.py`

- `_field_text(footprint, name)` -- `GetFieldText`, defensively (returns
  `None` if the field is absent or unreadable rather than raising).
- `_parse_um`, `_parse_capacitance_fF` -- unit parsers, **specific to
  cap_cmim's own conventions** (see Section 4.1). A new device type
  should not assume these apply to its own fields.
- `_read_cap_cmim_fields(footprint, on_log) -> dict` -- the cap_cmim
  reader: reads `w`, `l`, `Capacitance`, logs a warning (never an
  abort) for anything missing or unparseable, and always returns a
  dict with at least `"reference"`, `"model"`, `"footprint_obj"`.
- `DEVICE_READERS` -- `{"cap_cmim": _read_cap_cmim_fields}`. **The
  registry.** See Section 5.
- `find_supported_footprints(board, on_log) -> list[dict]` -- iterates
  `board.Footprints()`, looks up each footprint's `Model` in
  `DEVICE_READERS`, and calls whatever reader is registered. A `Model`
  that isn't registered is silently skipped (not an error: it just
  isn't a device this plugin knows about). This function is the same
  one used by Scan, Generate and Apply -- reading is always identical
  regardless of which phase is asking.

### `apply_resize.py`

- `_load_generator_module(gen_script_path)` -- dynamically imports
  `cmim_footprint_gen.py` by path (`importlib.util`), cached by
  resolved path.
- `load_tech(tech_json_path, gen_script_path)` -- thin wrapper around
  the generator's own `load_tech()`.
- `_generate_cap_cmim_footprint(params, tech, output_dir, on_log,
  gen_script_path) -> path or None` -- the cap_cmim generator: resolves
  `w`/`l` (directly, or solved from `Capacitance` via `cap_to_width`),
  validates range and the device minimum width, names the file, calls
  `cmim_footprint_gen.write_footprint()`, then strips the redundant name
  label (Section 4.3).
- `DEVICE_GENERATORS` -- `{"cap_cmim": _generate_cap_cmim_footprint}`.
  **The other half of the registry.** See Section 5.
- `generate_footprint_file(params, tech, output_dir, on_log,
  gen_script_path) -> path or None` -- looks up `params["model"]`
  (defaulting to `"cap_cmim"` for callers that predate device types) in
  `DEVICE_GENERATORS` and dispatches. Logs a clear error and returns
  `None` for an unregistered model -- never guesses.
- `_strip_visible_name_label`, `_style_provenance_field` -- cosmetic
  post-processing (Section 4.3, 4.5). Neither is device-specific in
  mechanism, only in what they're called with.
- `_apply_cap_cmim_fields`, `_apply_technology_fields` -- model-specific
  metadata refresh after footprint replacement. Today this preserves the
  `cap_cmim` GDS/PCell export contract: `Model`, `Sim.Name`, final `w`,
  final `l`, `m`, and `Capacitance`.
- `apply_to_instance(board, footprint_obj, generated_mod_path, params,
  on_log) -> bool` -- the board-mutating step (Section 4.2). The swap is
  generic; the optional `params` dict drives model-specific metadata
  refresh.

### `dialog_log.py`

One `wx.Dialog` subclass. Builds four path fields (root folder,
tech.json, generator script, output folder), a single **Run** button,
and a read-only multiline log. `_on_run()` calls `_on_scan`,
`_on_generate`, `_on_apply`, `_on_refresh` in order; those four methods
still exist as separate, independently callable methods (useful from a
REPL or a future test), they're just not each bound to their own button
anymore. None of this file knows about device types either -- it always
just iterates whatever `find_supported_footprints()` returns.

### `paths.py`

Resolution chain for `intm4tm2_tech.json` and `cmim_footprint_gen.py`:
environment variable (`INTERPOSER_PDK_ROOT`, or its `INTM4TM2_ROOT` alias) ->
the root saved from a previous session -> project text variable -> sibling
checkout on disk (any ancestor directory holding `interposer/` or
`OpenIntM4TM2/`) -> hardcoded `/work/OpenIntM4TM2` Docker fallback.

The dialog's four path fields persist to a `.resizer_passive_elements.json`
next to the board, not to project text variables: KiCad's Python bindings
never wrapped `PROJECT`, so `board.GetProject()` returns an object with no
`GetTextVars` and writing them is impossible from here. Reading them is
possible through `pcbnew.ExpandTextVars`, which the sibling `chiplet_export`
plugin uses; this plugin keeps the read leg for the roots and owns its own
store for the rest.

### `action_resizer_passive_elements.py` / `__init__.py`

Standard KiCad `ActionPlugin` boilerplate. `__init__.py` also defines
`__version__`, logged as the first line whenever the dialog scans the
board -- specifically so a rebuilt/redeployed environment (e.g. after a
Docker image rebuild) can be confirmed to actually be running the
current code. Python caches imported modules, so neither "Refresh
Plugins" nor even a fresh KiCad process against an unchanged mount
guarantees the `.py` files on disk actually changed underneath it; the
printed version number is the cheap, unambiguous check.

## 4. Design decisions and why

These are the non-obvious choices baked into the current code, in case
a future change looks like it should "obviously" be done differently --
each of these was a real, verified failure mode along the way.

### 4.1 Two "w"/"l" formats, and why both are real

A footprint instance's `w`/`l` fields can read `"8.11um"` (micrometres,
suffixed) **or** `"8.11e-6"` (bare number, in **metres**). Both
genuinely occur: the former is what `cmim_footprint_gen.py`'s own hidden
properties write; the latter is how the `cap_cmim` symbol library stores
"w"/"l" internally (confirmed against
`libs.tech/klayout/intm4tm2_tests/test_cmim_kicad_symbol.py`, which
reads the same fields via `float(props["w"]) * 1e6`), and is exactly
what ends up on a footprint instance right after "Update PCB from
Schematic" copies the symbol's properties over verbatim. Treating a bare
number as already-micrometres (an earlier bug here) silently produces a
~0x0um footprint instead of the intended size -- `_parse_um` in
`board_reader.py` disambiguates purely by the presence/absence of a unit
suffix.

### 4.2 Full footprint replacement, not in-place pad resize

An earlier version of `apply_to_instance()` only called `pad.SetSize()`
on the two pads of the already-placed instance, leaving the instance's
footprint identity (what "Footprint Properties" shows) untouched. That
was safer (no `pcbnew.FootprintLoad`, no net-copying) but confusing to
verify: the `Footprint` field kept reading e.g. `intm4tm2:CMIM_100fF`
even after the pads underneath had a completely different size. The
current version replaces the whole footprint object instead, so
`Footprint` correctly reads the generated name after a resize. This
requires care to not regress the one thing the simpler approach
guaranteed for free: pad-to-net connectivity. `apply_to_instance()`
copies nets strictly by pad **number** ("1"->"1" PLUS, "2"->"2" MINUS),
never by index/position, and adds the new footprint to the board
*before* removing the old one, so a failure partway through never
leaves the board with neither copy of the part.

### 4.3 Stripping generator output: by exact content, never "first match"

`cmim_footprint_gen.py`'s `build_footprint()` has, at different points
in its own history, placed extra visible `fp_text` elements at the
footprint's origin. An older version placed one duplicating the `Value`
property's text (the footprint's own name) -- redundant, and rendered
enormous at that position relative to the tiny device. A newer version
instead places three *intentional* polarity-marker texts there
(`"+ TopMetal1"`, `"- Metal5"`, `"PLUS=In1.Cu  MINUS=In2.Cu"`), which
must never be removed -- they're the layer legend, confirmed explicitly
relevant by the project supervisor.

`_strip_visible_name_label()` therefore matches by **exact text content
equal to the footprint's own `name`**, never "the first `fp_text` found
in the file". An earlier, cruder version of this function did match by
position and deleted a polarity marker by mistake the moment the
generator was updated to add them. Content-matching makes the function
a safe no-op against a generator version that never had the duplicate
in the first place (i.e. today's).

### 4.4 Field visibility is carried over; technology text is refreshed

"Update PCB from Schematic" copies each field's *visibility* from the
schematic symbol onto the footprint instance -- this is how
`Capacitance` (hidden by default in `cmim_footprint_gen.py`'s own
output) ends up shown on a normally-synced board: the symbol's own
`Capacitance` property isn't hidden. Because `pcbnew.FootprintLoad()`
returns a plain, freshly-loaded footprint with none of that
instance-specific visibility history, `apply_to_instance()` explicitly
carries visibility over, by field **name**, from the old instance to the
new one.

For ordinary generated fields, the old text is not copied because it can
be stale after a resize. For `cap_cmim` technology fields, the text is
then deliberately refreshed from the final params: `Model` is forced to
`cap_cmim`, `Sim.Name` is preserved or defaulted to `cap_cmim`, `w` and
`l` are written in metres, `m` is preserved or defaulted to `1`, and
`Capacitance` is updated from the final computed value when available.
That keeps the board compatible with the downstream GDS/PCell exporter,
which must recognize the resized footprint as a parametric CMIM device.
`"Reference"`/`"Value"` are excluded from visibility carryover; they're
set explicitly (Reference) or left as the generator's own correct output
(Value) instead.

### 4.5 A newly-created field defaults to giant and visible

Calling `footprint.SetField("SomeNewField", value)` for a field that
doesn't already exist creates it visible, on `F.SilkS`, at KiCad's
default 1.27 mm text size -- reasonable on a normal PCB, but on a
device whose whole extent is a few hundredths of a millimetre, that
text sprawls across the entire view. This bit the plugin's own
`CMIM_GENERATED_FILE` provenance field (recording which generated file
an instance's footprint came from), and the same issue applies to hidden
machine-readable technology fields when they are created or refreshed.
`_style_provenance_field()` fixes it the same way the sibling
`chiplet_export` plugin fixes its own machine-managed fields
(`writers/chiplet_writer.py`, `_style_managed_field`): move it to
`F.Fab`, hide it, shrink its text.

### 4.6 Never cache a generated file by "already exists"

An earlier version skipped rewriting a `.kicad_mod` if a file with the
expected name already existed in the output folder, on the assumption
that `cmim_footprint_gen.py` is pure -- same inputs, same output, so
regenerating would be wasted work. That assumption only holds for a
*fixed version* of that generator. The generator is not under this
plugin's control and did change (adding the polarity-marker graphics
from 4.3), so a file cached from before that change would silently keep
serving the old content forever, indistinguishable from a real bug
until directly compared against a fresh reference file. Since writing a
small text file is cheap, `generate_footprint_file()` now always
(re)writes it. There is no longer a "reuse" code path to go stale.

### 4.7 One "Run" button, not four

The four phases (Scan, Generate, Apply, Refresh) were originally four
separate buttons, deliberately, to make each one easy to verify in
isolation while the plugin was still being built and debugged. Once the
whole chain was confirmed working end-to-end, the individual buttons
were replaced with a single "Run" that calls the same four methods in
sequence -- the phase methods (`_on_scan` etc.) are unchanged and still
independently callable, they're just not each exposed as their own
button anymore.

## 5. Extending the plugin: adding a new device type

This is the part to read carefully before a resistor or inductor PCell
and generator actually ship. **Nothing about the orchestration, the
dialog, or the board-mutation step needs to change.** The entire
extension surface is two dictionaries, one function pair per device
type.

### 5.1 What you need in hand before starting

- The exact string the new device's footprint carries in its hidden
  `Model` field (e.g. `"res_poly"`, `"ind_spiral"` -- whatever the PDK
  actually emits). This is the dictionary key on both sides.
- Which fields that device's footprint/symbol actually carries, and
  their unit conventions. **Do not assume cap_cmim's conventions
  transfer.** `w`/`l` on a resistor might mean trace width/length rather
  than plate width/length; a resistance value might be given in ohms
  with SI suffixes, stored in a totally different format than
  `Capacitance`'s `"100fF"` style; the metres-vs-micrometres split
  documented in 4.1 might not exist at all for a device whose symbol
  library was authored differently.
- The new device's own generator script's public function names and
  signatures (its own `load_tech`/equivalent, its own
  width/geometry-solving function, its own `write_footprint`-equivalent).
  Do not assume it mirrors `cmim_footprint_gen.py`'s shape.
- Whether the device is two-terminal with pads numbered `"1"`/`"2"` the
  same way. If so, the physical replacement path in `apply_to_instance()`
  needs no changes. If the device needs export metadata preserved on the
  placed footprint, add a small `_apply_<device>_fields()` hook and call
  it from `_apply_technology_fields()`. If a
  future device has a different pad count/numbering convention, that
  function's pad-matching logic (`"1"`/`"2"` hardcoded in a couple of
  places) would need generalizing -- not a concern for a two-terminal
  resistor or inductor, but worth flagging if something more exotic ever
  shows up.

### 5.2 Step-by-step, with a worked (hypothetical) resistor example

**Step 1 -- `board_reader.py`: write a reader.**

```python
def _read_resistor_fields(footprint, on_log):
    """params dict for one res_poly footprint instance.

    Returns at least {"reference", "model", "footprint_obj"}, plus
    whatever this device's own fields resolve to -- shaped however
    makes sense for a resistor, NOT forced into cap_cmim's w_um/l_um/
    capacitance_fF shape.
    """
    def log(message):
        if on_log is not None:
            on_log(message)

    try:
        reference = footprint.GetReference()
    except Exception:
        reference = "?"

    r_text = _field_text(footprint, "R")          # example field name
    r_ohms = _parse_resistance(r_text)             # a NEW parser, written
                                                    # for this device's own
                                                    # unit conventions --
                                                    # do not reuse _parse_um
                                                    # or _parse_capacitance_fF
    if r_text is None:
        log('{}: warning: missing "R" field'.format(reference))
    elif r_ohms is None:
        log('{}: warning: "R" field ("{}") could not be parsed as a '
            'number'.format(reference, r_text))

    return {
        "reference": reference,
        "model": "res_poly",
        "r_ohms": r_ohms,
        "footprint_obj": footprint,
    }
```

**Step 2 -- register it:**

```python
DEVICE_READERS = {
    "cap_cmim": _read_cap_cmim_fields,
    "res_poly": _read_resistor_fields,
}
```

That's the entire change needed for Scan to start listing resistors.
`find_supported_footprints()` itself does not change.

**Step 3 -- `apply_resize.py`: write a generator.**

```python
def _generate_resistor_footprint(params, tech, output_dir, on_log=None,
                                  gen_script_path=None):
    """Write the .kicad_mod for one res_poly resistor's params.

    Mirrors _generate_cap_cmim_footprint's shape and error-handling
    style, but calls THIS device's own generator script/functions --
    never cmim_footprint_gen.py's, even if the geometry math looks
    superficially similar.
    """
    def log(message):
        if on_log is not None:
            on_log(message)

    reference = params.get("reference", "?")
    r_ohms = params.get("r_ohms")
    if r_ohms is None:
        log('{}: ERROR: missing "R" (resistance cannot be resolved)'
            .format(reference))
        return None

    # ... load that device's own generator module, validate range,
    #     compute geometry, name the file, write it, strip/style
    #     whatever that generator's raw output needs (if anything) ...

    return out_path
```

**Step 4 -- register it:**

```python
DEVICE_GENERATORS = {
    "cap_cmim": _generate_cap_cmim_footprint,
    "res_poly": _generate_resistor_footprint,
}
```

That's enough for generation. `generate_footprint_file()` does not
change. The physical replacement path in `apply_to_instance()` also does
not change when the new device uses the same pad-number convention: it
still matches pads "1"/"2" by number, regardless of what device produced
them. If the new device must preserve machine-readable fields for a
downstream exporter, add a small `_apply_<device>_fields()` helper and
route it from `_apply_technology_fields()`, just like `cap_cmim` does.
`dialog_log.py` does not change -- Run already iterates whatever
`find_supported_footprints()` returns and calls
`generate_footprint_file()`/`apply_to_instance()` on each, generically.

### 5.3 The one thing this design does *not* yet solve

`generate_footprint_file()` is handed a single `tech` object and a
single `gen_script_path`, resolved once per Run from the dialog's one
"`cmim_footprint_gen.py`"/"`intm4tm2_tech.json`" field pair, and passed
unchanged to whichever generator function gets dispatched to. This is
fine as long as a new device's generator is **another entry point in
the same `cmim_footprint_gen.py`**, reading the **same**
`intm4tm2_tech.json`.

If a new device instead ships as a genuinely separate script and/or
tech-constants file, this dispatch would be handing that device's
generator function the *wrong* `tech`/`gen_script_path` (cap_cmim's).
At that point:

- `dialog_log.py` needs its own path field(s) for the new device's
  script/tech source (following the existing `_on_browse_*` /
  `paths.py` patterns for the current four fields).
- `generate_footprint_file()` needs to look up the right
  `(tech, gen_script_path)` pair *per model* instead of reusing the one
  pair it resolves today.

This isn't implemented speculatively because there's nothing concrete
yet to design it against -- doing so before seeing the actual resistor
generator's shape risks guessing wrong (as happened more than once
during this plugin's own development; see Section 4). Revisit this
section once that generator exists.

## 6. Explicit non-goals (still, even with the registry in place)

- No resistor/inductor support actually works yet -- the registries
  above are the *mechanism*, not an implementation. Both are empty of
  real entries beyond `cap_cmim` until a real PCell/generator exists to
  write a reader/generator pair against.
- Still no automatic/reactive execution. "Run" is a manual click, full
  stop -- adding a device type does not change this.
- Still never touches the schematic, and still never writes into the
  shared `OpenIntM4TM2` checkout. A new device's reader/generator pair
  must follow the same rule cap_cmim's does: read board fields, import
  (never edit) that device's own external generator script.

## 7. Design invariants that must survive any future change

If you're modifying this plugin later and need a quick checklist of
"don't break this":

1. `cmim_footprint_gen.py` (and any future device's generator script)
   is imported, never copied or edited.
2. `apply_to_instance()` matches pads by **number**, never by index or
   position -- this is what guarantees PLUS/MINUS (or a future device's
   equivalent) can never silently invert.
3. Nothing runs without an explicit click of "Run".
4. Every field/warning/error is logged with the capacitor's/device's
   **reference**, so multi-device-on-one-board logs stay attributable.
5. A missing or unparseable field on one device never aborts processing
   of the others.
6. Nothing is written into the shared `OpenIntM4TM2` checkout by
   default, ever.

## 8. Testing without a running KiCad

Because `board_reader.py` and `apply_resize.py` (aside from
`apply_to_instance`'s lazy `import pcbnew`) have no `pcbnew`/`wx`
dependency, most of this plugin's logic was verified during development
with plain Python and hand-written stand-ins, e.g.:

```python
class FakeFootprint:
    def __init__(self, fields, ref="C1"):
        self._fields = fields
        self._ref = ref
    def HasField(self, name):
        return name in self._fields
    def GetFieldText(self, name):
        return self._fields[name]
    def GetReference(self):
        return self._ref

class FakeBoard:
    def __init__(self, footprints):
        self._footprints = footprints
    def Footprints(self):
        return self._footprints

board = FakeBoard([FakeFootprint({"Model": "cap_cmim", "w": "8.11e-6",
                                   "l": "8.11e-6", "Capacitance": "100 fF"})])
found = board_reader.find_supported_footprints(board, on_log=print)
```

For `apply_to_instance()`, a small fake `pcbnew` module (stub
`VECTOR2I`/`FromMM`/`ToMM`/`FootprintLoad`, fake `FOOTPRINT`/`PAD`/
`FIELD` classes with the handful of methods actually used) installed via
`sys.modules["pcbnew"] = fake_pcbnew` before importing `apply_resize` is
enough to exercise the full replace-and-swap logic, including net
preservation and field-visibility carryover, without KiCad at all. A new
device's reader/generator pair should be testable the same way before
ever touching a real board.
