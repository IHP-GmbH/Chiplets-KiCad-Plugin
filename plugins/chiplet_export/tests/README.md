# Tests

This suite guards the HYP -> GDS -> canonical `.chiplet` pipeline against
regressions. The tricky part is that the tests have three different runtime
needs, so "just run pytest" only covers part of the suite.

- **Need `pcbnew`** (the writers that read a live `BOARD`): run from the
  Python bundled with KiCad, i.e. inside the `kicad-builder` Docker image.
  When `pcbnew` is missing, `pytest.importorskip("pcbnew")` skips those tests
  cleanly instead of failing. Files: `test_chiplet_writer.py`,
  `test_hyperlynx_writer.py`, `test_byte_exact_writers.py`,
  `test_cmim_extraction.py`, `test_io_pad_ids.py`. `test_board_save.py` (what a GUI save keeps of
  a flow board) runs its saves in a child process, so on a host it also runs
  with `KICAD_APPDIR` naming a KiCad AppDir. So does the last case of
  `test_plugin_registration.py` (both packages import beside KiCad's pcbnew
  without registering); its other cases use a stand-in pcbnew and run
  anywhere.
- **Need the `klayout.db` module** (GDS geometry generation), but not
  `pcbnew`: run on the host once the worker venv is on the path. They guard
  themselves with `pytest.importorskip("klayout.db")`. Files:
  `test_io_pads.py`, `test_via_geometry.py`, `test_boundary_annotations.py`,
  `test_cmim_devices.py`, `test_pillar_manifest.py` (the last two also need
  the sibling PDK checkouts), `test_die_outline_read.py`,
  `test_die_outline_finalize.py`, `test_die_layouts.py` and
  `test_attach_frame.py` (their worker runs use the vendored
  `fixtures/intm4tm2_reference.lyp` and read the mount plane from the
  fixture PDK root `fixtures/intm4tm2_pdk`, so they need no PDK).
- **Stdlib + pytest only**: run anywhere. Files: `test_discovery.py`,
  `test_runner.py`, `test_orchestrator.py`, `test_connection_stacks.py`,
  `test_layer_guard.py`, `test_hyp_to_gds_decoupling.py`,
  `test_yaml_escape.py`, `test_technology_id.py`,
  `test_die_outline_field.py` (two cases skip:
  the worker's side of the shared grammar without `klayout.db`, and the
  YAML read-back check without PyYAML).

Some of the host-runnable suites still skip themselves when sibling ecosystem
checkouts (the interconnect PDK, an SG13G2 PDK) are absent; they run
everywhere but may report skips rather than passes.

`conftest.py` puts the plugin package root (`plugins/chiplet_export`) on
`sys.path` so the absolute `chiplet_export.*` imports and the top-level
`import hyp_to_gds` resolve, and aliases that root as the `chiplet_export`
package in `sys.modules` when the directory has a different name. The plugin
dir is normally `chiplet_export` (also the KiCad symlink name), so the alias is
usually a no-op; it only matters for oddly named checkouts.

The finalizer reads the mount plane from the interposer PDK on every run, so
a test that finalizes a `.chiplet` points `INTERPOSER_PDK_ROOT` at
`fixtures/intm4tm2_pdk`, a root holding only a verbatim copy of the PDK's
stack descriptor (the `attach_frame_pdk` fixture in `conftest.py`, autouse in
the modules that finalize). The plane they see is then a known 10.8103, never
whichever PDK the host has, and they pass on a runner with none.

## Layout

| File | Coverage |
|------|----------|
| `test_chiplet_writer.py` (19) | Structural invariants on the YAML emitted by `writers/chiplet_writer.py`; includes the interf_u regression that exercises boards whose PROJECT wrapper lacks `GetTextVars`, the `CONNECTION`, `DIE_THICKNESS_UM` and `DIE_OUTLINE_LAYER` field round trips on a real board, two dies on project sheets (`<id>/layers.lyp`) written as two technologies named by their folders, and the stderr warning when two different sheets give one id. |
| `test_hyperlynx_writer.py` (7) | Structural invariants on the .hyp emitted by `writers/hyperlynx_writer.py` (metric headers, BOARD / STACKUP / DEVICES blocks, GDS_FILE propagation, PADSTACK id consistency). |
| `test_byte_exact_writers.py` (3) | Byte-exact diff: `write_chiplet` / `write_hyperlynx` output vs the C++ exporters `pcbnew.ExportBoardToChipletFile` / `ExportBoardToHyperlynxFile`. The third case synthesises a project that sets `INTERPOSER_ADAPTER` / `INTERCONNECT_ADAPTER`, which no committed fixture does, so the plain diff passed for the uninteresting reason that both sides read nothing. Those two are read from the project text variables and from nowhere else on both sides, and SWIG wrapped `PROJECT` in no KiCad version, so the Python lookup returned "" while the C++ exporter honoured them: the `.chiplet` silently lost its interposer adapter and its whole `interconnect:` block. Fixture discovery walks every ancestor's `examples/*/kicad/*.kicad_pcb`; the old single fixed path meant the whole parity guard skipped in the adk-tools image. |
| `test_discovery.py` (26) | Worker Python / hyp_to_gds.py discovery chain (env var, `.venv`, project text var read through `pcbnew.ExpandTextVars` since `PROJECT` is unwrapped, PATH probe with klayout+yaml import); the not-found message names `.venv`, `pip install -r requirements.txt` and which legs are actually probed; plus `preview_worker_python`, the probe-free variant the dialog shows as a hint (it must never spawn the import probe on the UI thread). |
| `test_runner.py` (8) | Async subprocess runner: line-by-line stdout/stderr callbacks, exit code propagation, cancel via `threading.Event`, env/cwd plumbing. |
| `test_orchestrator.py` (198) | The pure helpers in `pipeline/orchestrator.py` that the dialog cannot exercise otherwise: `build_cli_args` (including the `--cmim-devices` passthrough), `build_adk_drc_argv`, `build_worker_env`, `load_interposer_adapter` / `load_interconnect_adapter`, `available_connection_types`, `connection_method_specs` + `format_connection_label` / `describe_connection_method` (the manifest-sourced dropdown labels; a method id must stay its label's prefix, and a missing manifest must degrade to bare ids rather than invent numbers), `describe_die_thickness_gaps`, `describe_interposer_body_default` (the interposer physical-body plausibility warning: the body now survives from the board stackup, so a value near the FR-4 default is flagged), `discover_dependency_root`, `discover_interposer_lyp`, `derive_interconnect_methods` + `write_ixn_methods_sidecar`, `_read_component_connections`, `describe_assembly_drc`, and `ExportOptions` / `ExportResult` DRC defaults (`discover_dependency_root` takes the worker's IntM4TM2 marker, and a set interposer root is shown as given, never replaced by the sibling, so the worker refuses a die PDK tree); and `--export-report` in `run_export`: the report is read once and the same names reach the preflight and both writers, an explicit io_pads sidecar is passed through unread, and a bad report or a refused carrier leaves no output directory. |
| `test_connection_stacks.py` (9) | `writers/connection_stacks.py`: the manifest-driven connection-stack block must reproduce the literal the writer used to hardcode (and export_chiplet.cpp still emits), plus interconnect-id validation against the manifest. Skips without the interconnect PDK manifest. |
| `test_hyp_to_gds_decoupling.py` (77) | hyp_to_gds connection-stack tables decoupled from the interconnect PDK manifest: the manifest-sourced tables must reproduce the prior IHP literals exactly while the vendor demo method becomes selectable. Each method's landing is drawn by the pad cell of its fab anchor (CuPillarPad, SolderBumpPad) or refused before the GDS, and a bump_mirror whose fab anchor API answers wrongly is a typed refusal (exit 5) for every method; the solder-bump export cases skip on an interposer PDK without SolderBumpPad. Skips without the manifest. An `INTERPOSER_PDK_ROOT` without the stack descriptor, a KLayout PDK tree that is not IntM4TM2 included, ends the run with `CARRIER_UNSUPPORTED_ROOT` from every reader (in the attach-frame cases too). |
| `test_io_pad_ids.py` (42) | Carrier pads read through interposer-pnr's shared `read_board_carriers`; each carrier reaches it as its reference, IO_PAD_ID, footprint library and sheet link, read off real boards. On a board drawn by export (a carrier from `chiplet_carrier` or carrying `IO_PAD_ID`), both carrier readers, the `.chiplet` writer and the io_pads sidecar, emit the io_pad id, per carrier even when two share a reference; a carrier without it, or two with one id, is refused by reference as a `CarrierIdError`, a carrier left out of the BOM included, and with every id lost the readers never fall back to the `J<n>` designators. A hand-drawn board (another library, no symbol links) keeps its references. A board drawn by export whose carriers link sheet symbols needs its report; one drawn by hand from its own sheet (a project library, linked) does not; a draft updated from its draft sheet reads by its own report. Net names on a real `export --form` board and report (`fixtures/sheet_net_names`): both outputs carry the planner's names, a sheet-only net is unbound; a version 6 report names carrier nets as its version 3 twin, with or without a feature this copy does not know, and one without the `sheet` feature follows no sheet; a report written for other carrier references (a draft's on a sheet board, a sheet board's on a draft) or pairing a carrier with another io_pad, a sheet's report on a board no carrier of which is linked, and a carrier net in neither map are refused by both writers with nothing written; only report refusals are `BoardReportError`; a die's sheet path alone needs no report; a board drawn against no sheet keeps each carrier's own net, the same bytes with no report and with one that follows no sheet. |
| `test_draft_net_names.py` (12) | `draft_net_names.read_export_report`, stdlib only: a real report read through the vendored interposer-pnr reader maps sheet names back to the planner's, no path is no report, a report of a board that follows no sheet has no names, version 5 names as 3 and 4 as 2 (reserved copper names no net), a 5 or a 4 with no `reserved`, a 3 with one, a 6 that lists no features and a newer version are refused, and every refusal (no file, not JSON, not an export report, a bad round_trip) names the file. The rules themselves are tested in interposer-pnr. |
| `test_io_pads.py` (6) | I/O pad geometry in `hyp_to_gds.GDSGenerator.add_io_pads` (TopMetal2 squares, `io_class` dispatch that skips `flipped_bump` / `tsv_bump`, invalid-size skip, missing file) plus `update_chiplet_file` io_pads injection and layout-ref relativization. |
| `test_cmim_devices.py` (40) | Worker side of the `cap_cmim` path: the w/l unit contract (a `um`/`u` suffix means micrometres, a bare number means metres, and every convention must land on the same plate), sidecar loading, IntM4TM2 `cmim` PCell placement (plate centred on the footprint position, origin snapped to `techParams.grid`), unplaceable devices reported to the caller rather than skipped, no empty group cell left behind, and the regression that SG13G2 via PCells survive the intm4tm2 technology rebind (with `db.Cell` handles in the via cache, `write()` died in `Cell.flatten`). A plate the PCell would refuse never reaches it: the PCell does not return None on bad parameters, it hands back a nameless empty cell that the GDS writer emits as an empty STRNAME record, which makes the whole file unreadable, and a plate large enough to pass its own coercion sends its O(w*l) via loop into an unbounded allocation. Skips without the interposer PDK. |
| `test_cmim_extraction.py` (13) | Board side of the same path: `is_cmim` on both the `Model` and `Sim.Name` spelling, `write_cmim_devices_json` normalising every field convention to `w_um`/`l_um` in the io_pads frame (micrometres, Y negated), unusable footprints skipped with no file written, the orientation recorded for the GDS frame, refs of footprints that could not be described handed back to the caller (the worker's own guard cannot see those), and a round trip of the exporter's own sidecar back through the worker's reader. |
| `test_via_geometry.py` (15) | SG13G2 via PCell bootstrap (must come up when a PDK and PCell deps are present; a silent rectangle fallback is a regression) and the JSON-honoring `_create_simple_via` rectangle fallback that reads `PDK_VIA_PARAMS`. The cut count in integer dbu: a pad equal to the k x k landing gives k and one dbu more gives k+1 (k = 1, 3, 10, METRIC and ENGLISH); a 3x3 landing draws 9 cuts and a dieless HYP of 1.9 um one-cut vias draws one cut per via, on the PCell path and the rectangle fallback alike. Skips without an SG13G2 PDK. |
| `test_die_outline_read.py` (44) | The die outline read in `die_outline_gds.py` on synthetic GDS: one merged rectangle under the one top cell (found through child cells, abutting pieces merged), and a named refusal for two shapes, corner-touching boxes, a non-rectangle, a ring, zero width, an absent layer, texts only, a missing, unreadable or directory path, several top cells, and a dbu other than the assembly's 0.001 um. Width and height are exact Decimal products on both axes and off the origin (123457 DBU is 123.457, not the float 123.45700000000001); each file is opened once per run whatever path names it, and causes name the resolved file; the flag parsers name the bad token. |
| `test_die_outline_finalize.py` (52) | The finalizer with `--die-outline-layers` / `--die-outline-exempt`: declared dies sized exactly from the outline (two layers of one file, a symlinked file, a `${VAR}` path), exempt dies keep their size and are logged, every refusal (undeclared, both, not a die, no flags, duplicate refs, missing, unreadable or wrong-dbu GDS for exempt dies too) raised before the `.chiplet` is touched and all offenders named at once. A die an `--io-pads` entry replaces needs no flag, and a shared HYP ref nothing uses is left alone. Worker runs: the boundary manifest polygon is where the outline lands in the complete GDS (face-up and flip-chip, at right angles and at 45 degrees), an exempt die keeps the cell bbox, the same holds without a `.chiplet`, the head reads the HYP `GDS_FILE` once, a device at another dbu or a die that cannot be placed leaves no complete GDS, and a refused CLI run leaves a pre-populated output directory byte-identical. |
| `test_die_layouts.py` (53) | `--die-layouts`: the sidecar read into exact 0.001 um database units, and a named refusal (`match=`) for every malformed file or entry (not JSON, not an object, NaN, a missing, unknown or repeated key, a relative, unexpanded, missing or directory layout, an empty top cell, a box that is not four numbers, not whole database units or not a box, a die named twice, an empty reference), all offenders at once. The finalizer writes the full layout relative and uncopied, the contract's top cell and exactly the two metadata keys, and without the flag writes the bytes it wrote before; it refuses, leaving the `.chiplet` untouched, a reference that is no die (absent, the interposer, an `--io-pads` pad), a full layout at another dbu, without the contract cell or with it under another cell, and a box that matches one file only (naming both files and boxes). Worker runs: the boundary and both GDS (dates aside) equal a run without the flag, the complete GDS composes the abstract, and a refused run leaves the tree byte-identical. Also the argv, the CLI's absolute path and exit 2, and the `.hyp` and the worker's argv under `run_export` with the flag and without. |
| `test_cli_reference.py` (7) | `CLI.md` against the `chiplet-export` parser: every flag has a row and every row names a flag, a required flag says `required`, a switch says `flag`/`off`, choices and string defaults appear in their cells, and the exit table has exactly the tool's four codes. `--help` (which names `CLI.md`) and a usage error run with pcbnew unimportable; the plugin README links the reference before its first section. |
| `test_attach_frame.py` (31) | The mount plane read from the interposer PDK's `beol.attachment_surface_z_um`: the value as written and its source; a named refusal (`match=`, naming `INTERPOSER_PDK_ROOT` and the key) without the PDK, for a descriptor that is not JSON, and for a key that is missing or not a finite number (a string, a bool, null, a list, NaN, Infinity); each z the exact decimal sum of the plane and the heights as written (five-decimal heights keep them; no float tail). The finalizer writes the plane and that z, refuses without the PDK leaving the `.chiplet` untouched, and takes a number a direct caller gives. Worker runs name the plane read, and a root without the descriptor leaves the tree byte-identical; without a `.chiplet` no plane is read. |
| `test_system_technology_overlay.py` (31) | `--system-chiplet` on the draft and System pair of a reference flow run of the two-die bondpad example (`fixtures/two_die_bondpad`): each component takes the technology of the System component with its id and `technologies` is the System's entries for exactly those ids, copied as written; without the flag the output is byte for byte `finalized.chiplet`, which the finalizer made before the flag existed. Every refusal (no System component, two of them, no technology, an undeclared id, a relative `layer_properties` or `stackup`, an entry that is not a mapping) names the id with `match=` and leaves the `.chiplet` untouched, all offenders at once; a System die the draft lacks and an `--io-pads` component are not errors. Worker runs write the ids, and a refused or unreadable System leaves the tree byte-identical. Also the argv without the flag, the CLI's absolute path and its exit 2 on a missing file. |
| `test_die_outline_field.py` (154) | The `DIE_OUTLINE_LAYER` footprint field (`die_outline.py`) against strict pcbnew doubles whose graphics, courtyard and bounding box raise when read: the `LAYER/DATATYPE` grammar (the worker parses with the very same objects), which footprints are read (dies sized from their GDS) and which never are (marked dies, IO pads, `cap_cmim`, BOM-excluded), one role per reference for the dialog's rows, and one refusal naming every offender, each cause once (no field, empty, malformed, no `GDS_FILE` with the BOM hint, a reference two dies share or a die shares with an IO pad or a `.hyp` device, a reference the flags, the `.hyp` or the `.chiplet` would change, checked against PyYAML itself, a partial or misdirected explicit map). Through `run_export`: the refusal comes before any output directory, workspace or writer, even for a board whose first die is fine; the argv carries the preflight's map and the exempt list derived from the markers, in both worker modes, and nothing when the worker reads no outlines; the CLI creates nothing on a refusal. Also `write_die_outline_layers` against field doubles, the real `write_chiplet` loop emitting exactly the dies the preflight names, and the regeneration driver's `--use-existing-hyp` path deriving both flags and refusing before it writes anything. |
| `test_boundary_annotations.py` (14) | The opt-in, viewer-only boundary annotation layer in hyp_to_gds: off by default, one polygon + one label per boundary when on, never the legacy 190/0 fab layer, the boundary manifest left untouched, and an annotation layer on any landing pad fab layer refused. |
| `test_layer_guard.py` (3) | Loud guard for unmapped board layers: fails the conversion when more than `UNMAPPED_FAIL_FRACTION` of trace elements sit on unmapped layers, tolerates stray layers below the threshold with one aggregate warning. |
| `test_pillar_manifest.py` (8) | The `<stem>.pillars.json` pillar manifest (schema `adk-pillar-manifest` 1.0.0): written on bump-path runs (empty `pillars` array when nothing is placed, nothing at all without the bump path), positions equal to the drawn GDS instances rebased into the canonical GDS-bbox-corner frame (the `.chiplet` frame), including auto-resolved bumps flagged `moved_by_auto_resolve`, per-die method/diameter, deterministic sorted output. Skips without the sibling PDK checkouts. |
| `test_seal_ring.py` (42) | `--seal-ring`: the PCell placed on a rectangular outline and carried into the complete GDS; each refusal (no outline, two loops, not a rectangle, under the minimum, no library or PCell, an empty cell, 39/4 off the outline) writes nothing; keep-outs clipped to 39/4; the stop for geometry in the ring band or within the space rule, passing at the rule and stopping one grid step closer; the boundary sidecar digest taken after the fill; the fill engine taken only from the resolved PDK root. The logic runs on a fake PCell library, so no PDK is needed; the real PCell cases (the ring on the outline, the minimum, a CMIM placed before the ring surviving its second technology bind) skip without one, as does the check that the clearance layers are the sealring deck's SEAL.REQ.4 layers plus Passiv. Flag off: argv and GDS bytes (timestamps zeroed) pinned to the export from before the flag. |
| `test_technology_id.py` (20) | The technology id a die's `LYP_FILE` gives (`writers/_technology.py`): the file stem for a sheet named after its technology, so today's ids do not move, and the holding folder for a project's per-technology `layers.lyp`; a `layers.lyp` with no folder above it (bare, `./`, right under a `${VAR}`) keeps `layers`, and no other name reads the folder. |
| `test_yaml_escape.py` (18) | The shared YAML scalar escaper (`writers/_yaml.py`): bare-vs-quoted classifier, escape set, and round-trips through a YAML parser (guards the C++/Python byte-locked pair at its most divergence-prone boundary). |
| `regenerate_wirebond_demo.py` | Driver script (not a pytest module): regenerates the wire-bond demo `.chiplet` through the Python pipeline; output feeds the chiplet-studio `CoordFrameContract*` gtests. Its `--use-existing-hyp` path derives `--die-outline-layers` / `--die-outline-exempt` from the board the way `run_export` does. |
| `check_complete_gds_alignment.py` | klayout regression script (not a pytest module): asserts U1's flipped cell sits over the cu-pillar array in the complete-assembly GDS. |

201 test functions across the 14 `test_*.py` files. A handful skip on hosts
without the KiCad fixtures (interf_u demo, wire-bond demo `.kicad_pcb`) or
without sibling PDK checkouts; the worker venv is expected at `.venv` (see
`requirements.txt`: `klayout>=0.28`, `PyYAML>=6.0`).

## Running

Host-runnable tests (no Docker). The stdlib-only suites run as-is; the
klayout-only suites need the worker venv's `klayout.db`, so run them through
`.venv/bin/python`:

```bash
cd plugins/chiplet_export
.venv/bin/python -m pytest \
    tests/test_discovery.py \
    tests/test_runner.py \
    tests/test_orchestrator.py \
    tests/test_connection_stacks.py \
    tests/test_layer_guard.py \
    tests/test_hyp_to_gds_decoupling.py \
    tests/test_yaml_escape.py \
    tests/test_technology_id.py \
    tests/test_io_pads.py \
    tests/test_via_geometry.py \
    tests/test_boundary_annotations.py \
    tests/test_cmim_devices.py \
    tests/test_pillar_manifest.py -v
```

Some of these skip when the interconnect/SG13G2 PDK siblings are absent; that
is expected on a lone checkout.

The pytest configuration is the repository root's `pyproject.toml`, found from
here or from the root. A bare run deselects the cases that start the klayout
application (`klayout_binary`); `-m klayout_binary` runs them. The cases that
read a sibling checkout carry `needs_sibling_sources`, and find it through the
variable that names it, `$HCDP_ROOT/<id>`, or the walk from this checkout, in
that order (the root `conftest.py`).

Byte-exact diff only (requires kicad-builder + reference files):

```bash
docker run --rm \
  -v $PWD/..:$PWD/.. \
  kicad-builder bash -c "
    cd $PWD &&
    PYTHONPATH=/tmp/pytest_lib pip install --target /tmp/pytest_lib pytest pyyaml &&
    PYTHONPATH=/tmp/pytest_lib:$PYTHONPATH \
        python3 -m pytest tests/test_byte_exact_writers.py -v
  "
```

Full suite from inside the kicad-builder Docker image:

```bash
ROOT=/path/to/checkouts   # kicad/ and chiplet_kicad_plugin/ side by side
docker run --rm \
  -v $ROOT:$ROOT \
  -e LD_LIBRARY_PATH=$ROOT/kicad/build/release/common:$ROOT/kicad/build/release/common/gal:$ROOT/kicad/build/release/pcbnew:$ROOT/kicad/build/release/api:$ROOT/kicad/build/release/pcbnew/python:$ROOT/kicad/build/release/3d-viewer/3d_cache/sg \
  -e PYTHONPATH=$ROOT/kicad/build/release/pcbnew/python:/tmp/pytest_lib \
  kicad-builder bash -c "
    pip install --target /tmp/pytest_lib pytest pyyaml &&
    cd $ROOT/chiplet_kicad_plugin/plugins/chiplet_export &&
    python3 -m pytest tests/ -v --ignore=tests/regenerate_wirebond_demo.py \
                                --ignore=tests/check_complete_gds_alignment.py
  "
```

The two `--ignore`d files are driver scripts, not pytest modules.

Round-trip regression net (driver script + chiplet-studio gtests):

```bash
# 1. Regenerate the wire-bond demo .chiplet via the Python pipeline.
#    Reuse the same LD_LIBRARY_PATH / PYTHONPATH as the full-suite block above.
#    The demo board ships a closed Edge.Cuts outline, so the full end-to-end
#    path (Hyperlynx writer -> hyp_to_gds.py) runs directly; --use-existing-hyp
#    stays available for boards still authored without an outline.
docker run --rm -v $ROOT:$ROOT -e LD_LIBRARY_PATH=... -e PYTHONPATH=... \
  kicad-builder \
  $ROOT/chiplet_kicad_plugin/plugins/chiplet_export/.venv/bin/python \
  $ROOT/chiplet_kicad_plugin/plugins/chiplet_export/tests/regenerate_wirebond_demo.py \
      --board $ROOT/adk-tools/examples/interposer_wire_bonding_demo/kicad/interposer_wire_bonding_demo.kicad_pcb \
      --connection cupillar_opt1 \
      --output-dir $ROOT/_tmp_regen

# 2. Feed the regenerated .chiplet to the chiplet-studio gtests.
cd $ROOT/chiplet-studio/build && ./tests/chiplet_tests \
    --gtest_filter='CoordFrameContract*'
```

`regenerate_wirebond_demo.py` defaults `--board` to the bundled
`examples/interposer_wire_bonding_demo/kicad/` board (resolved next to the
plugin) and also accepts `--lyp`, `--io-pads`, `--connection`, and
`--allow-drc-fail`. Its default mode exits like `chiplet-export`: 3 when the
files were written but the assembly DRC did not pass (a container without
the ADK reads NOT RUN, so pass `--allow-drc-fail` there), 0 only when it
passed. `--require-drc` is still accepted and changes nothing.
`--use-existing-hyp` runs no DRC and exits 0 when the worker does. Every
unmarked die on the board needs its
`DIE_OUTLINE_LAYER` field (plugin README, *Per-die settings*); either path
refuses a board without one before it writes anything.

Optional fixture override (point at any board, e.g. a chiplet
project under development):

```bash
export CHIPLET_WRITER_BOARD=/path/to/board.kicad_pcb
export HYPERLYNX_WRITER_BOARD=/path/to/board.kicad_pcb  # may be the same
```

## GUI smoke tests (wxPython) — deferred, for the Docker image team

The PLUG-16 source-authority regression tests distinguish selected roots from
automatic discovery. Nonblank option, environment and project selections remain
authoritative even when incomplete; an automatic checkout without an actual
manifest file is ignored. Pure resolver/preflight cases label their reader
doubles. Two separate subprocess probes use unchanged production PDK reader
bytes to check that the worker environment pins A despite a competing B; they
explicitly skip if that reader source is unavailable. They do not run the full
layout worker or prove process-wide absence of every possible PDK installation.
The no-PDK orchestration test uses writer/worker doubles and checks the warning
in the callback, durable run log and output manifest. This mode omits connection
stacks and manifest-backed interconnect method validation; export completion is
not an interconnect-verification claim.

The `ChipletExportDialog` in `dialog_chiplet_export.py` (the pcbnew action-plugin
dialog) has focused control-double coverage in `test_dialog_catalogue.py` for
typed source refusal during construction and refresh, atomic catalogue updates,
recovery, visible no-PDK warnings, and synchronous Run preflight before option
collection or board writes. The per-die outline-layer column is covered the
same way: the board's field text is shown raw (a malformed value is not
erased), exempt and non-die rows say what they are, a board the column cannot
read keeps its rows and says so, and Run turns an empty or malformed row, or a
preflight refusal, into one message box with no field write-back and no
export, or writes the rows back after the CONNECTION and thickness fields.
These tests execute the real dialog methods with a narrowly scoped wx import
shim; they do not construct native widgets or validate sizers. Other logic is
covered indirectly by the pure
helpers in `pipeline/orchestrator.py` and `pipeline/discovery.py`
(`connection_method_specs`, `format_connection_label`,
`describe_connection_method`, `describe_die_thickness_gaps`,
`describe_interposer_body_default`, `preview_worker_python`, ...), but the wx
plumbing itself — sizers, the removal of the "Canonical .chiplet" checkbox, the
per-die grid, the connection-combo labels, the worker-python hint row, the
`SetSizeHints` minimum width — still needs dedicated visual assertions.

`test_dialog_catalogue_wx.py` adds four native-widget smoke cases: construct a
real dialog on a fresh `pcbnew.BOARD` with a selected data-only manifest and no
reader; recover through the real refresh callback using a labelled reader
double; recover from a candidate-inspection permission error during construction;
and refuse a Run click before the root-change debounce without board writes or
export. It checks editable source controls and an enabled Close
button, and destroys the dialog and stops its timer without entering a modal
loop. Deferred window deletions are drained before the fixture destroys its
owned wx application. The standalone bootstrap temporarily suppresses KiCad
action registration during the initial package import because no KiCad
application registry exists in that process. It restores registration immediately,
verifies that the imported package belongs to this checkout, then starts pytest.
Production registration is unchanged and is not covered by this smoke. Direct
pytest in a capable environment can register the parent package before any
test-module guard executes, so use the bootstrap for this standalone run.
The recovery double tests GUI lifecycle, not supported-reader conformance.
On hosts missing wx, pcbnew, or a Linux display, this file reports an explicit
skip. In a capable image, run from the repository root:

```bash
xvfb-run -a python3 scripts/run_dialog_catalogue_wx.py -vv -s
```

Acceptance requires all four selected cases to run and the process to exit 0;
skips, passing assertions followed by a shutdown crash, and timeout termination
are not native GUI acceptance. The bootstrap preserves pytest's exit code and
normal interpreter shutdown. This smoke does not replace the broader visual
checks below.

This needs a **wxPython + display** environment, which the host worker venv
(`.venv`) does not have. It is handed to the team that builds/tests the Docker
images because the pieces already live there:

- **`import wx` works in `kicad-builder` today** (wxPython **4.2.1** is in the
  image). Only a virtual display is missing for constructing widgets.
- **Level 1 — import smoke (runs green in `kicad-builder` right now).** Catches
  import/syntax/symbol-wiring breakage without any display. Proven recipe:

  ```bash
  ROOT=/path/to/checkouts   # kicad/ and chiplet_kicad_plugin/ side by side
  docker run --rm -v $ROOT:$ROOT \
    -e LD_LIBRARY_PATH=$ROOT/kicad/build/release/common:$ROOT/kicad/build/release/common/gal:$ROOT/kicad/build/release/pcbnew:$ROOT/kicad/build/release/api:$ROOT/kicad/build/release/pcbnew/python:$ROOT/kicad/build/release/3d-viewer/3d_cache/sg \
    -e PYTHONPATH=$ROOT/chiplet_kicad_plugin/plugins:$ROOT/kicad/build/release/pcbnew:$ROOT/kicad/build/release/pcbnew/python \
    kicad-builder python3 -c "import wx, pcbnew; import chiplet_export.dialog_chiplet_export as d; assert hasattr(d, 'ChipletExportDialog')"
  ```

  Note: the dialog uses a **relative** import (`from .pipeline.orchestrator
  import ...`), so it must be imported as `chiplet_export.dialog_chiplet_export`
  with `$ROOT/chiplet_kicad_plugin/plugins` (the parent of the plugin package)
  on `PYTHONPATH`, **not** as a top-level `import dialog_chiplet_export`. This
  historical import-only recipe does not establish safe standalone lifecycle:
  `action_plugin.cpp ... assert "PgmOrNull()" failed` indicates attempted
  registration without the KiCad application host. Use the bootstrap above for
  the native dialog smoke; that assertion is not accepted as benign evidence.

- **Level 2 — headless construct smoke (needs one addition to the image).**
  `kicad-builder` has **no `xvfb`** (`xvfb-run` is absent), so a `wx.App` +
  `ChipletExportDialog(...)` cannot be constructed. The action item for the
  Docker team is to add `xvfb` (`apt-get install -y xvfb`) to the image, then run
  the construction under `xvfb-run -a python3 ...`, load the wire-bond demo board
  (`kicad_designs/interposer_wire_bonding_demo/...`) via `pcbnew.LoadBoard`,
  construct `ChipletExportDialog`, and assert the UX invariants from the dialog
  cleanup: no "Canonical .chiplet" checkbox (`_cb_chiplet` gone), the connection
  combo labels carry PDK specs (id stays the label prefix), the per-die grid has
  header cells, the worker-python hint row is present, and the dialog's minimum
  width did not grow versus a baseline.

Until the image gains `xvfb`, wire Level 1 into the Docker test flow as-is — it
is a real, green build-gate on the dialog today.
