# Tests

This suite guards the HYP -> GDS -> canonical `.chiplet` pipeline against
regressions. The tricky part is that the tests have three different runtime
needs, so "just run pytest" only covers part of the suite.

- **Need `pcbnew`** (the writers that read a live `BOARD`): run from the
  Python bundled with KiCad, i.e. inside the `kicad-builder` Docker image.
  When `pcbnew` is missing, `pytest.importorskip("pcbnew")` skips those tests
  cleanly instead of failing. Files: `test_chiplet_writer.py`,
  `test_hyperlynx_writer.py`, `test_byte_exact_writers.py`.
- **Need the `klayout.db` module** (GDS geometry generation), but not
  `pcbnew`: run on the host once the worker venv is on the path. They guard
  themselves with `pytest.importorskip("klayout.db")`. Files:
  `test_io_pads.py`, `test_via_geometry.py`, `test_boundary_annotations.py`,
  `test_pillar_manifest.py` (this one also needs the sibling PDK checkouts).
- **Stdlib + pytest only**: run anywhere. Files: `test_discovery.py`,
  `test_runner.py`, `test_orchestrator.py`, `test_connection_stacks.py`,
  `test_layer_guard.py`, `test_hyp_to_gds_decoupling.py`,
  `test_yaml_escape.py`.

Some of the host-runnable suites still skip themselves when sibling ecosystem
checkouts (the interconnect PDK, an SG13G2 PDK) are absent; they run
everywhere but may report skips rather than passes.

`conftest.py` aliases the checkout root as the `chiplet_kicad_plugin` package
when the directory has a different name. A default clone lands in
`Chiplets-KiCad-Plugin` and CI checkouts use the repo name (not a valid Python
identifier); without the alias the absolute `chiplet_kicad_plugin.*` imports in
the tests would not resolve. If you clone under the canonical
`chiplet_kicad_plugin` name, the alias is a no-op.

## Layout

| File | Coverage |
|------|----------|
| `test_chiplet_writer.py` (11) | Structural invariants on the YAML emitted by `writers/chiplet_writer.py`; includes the interf_u regression that exercises boards whose PROJECT wrapper lacks `GetTextVars`. |
| `test_hyperlynx_writer.py` (7) | Structural invariants on the .hyp emitted by `writers/hyperlynx_writer.py` (metric headers, BOARD / STACKUP / DEVICES blocks, GDS_FILE propagation, PADSTACK id consistency). |
| `test_byte_exact_writers.py` (2) | Byte-exact diff: `write_chiplet` / `write_hyperlynx` output vs the C++ exporters `pcbnew.ExportBoardToChipletFile` / `ExportBoardToHyperlynxFile`. |
| `test_discovery.py` (23) | Worker Python / hyp_to_gds.py discovery chain (env var, `.venv`, project text var, PATH probe with klayout+yaml import); the not-found message names `.venv`, `pip install -r requirements.txt` and which legs are actually probed; plus `preview_worker_python`, the probe-free variant the dialog shows as a hint (it must never spawn the import probe on the UI thread). |
| `test_runner.py` (8) | Async subprocess runner: line-by-line stdout/stderr callbacks, exit code propagation, cancel via `threading.Event`, env/cwd plumbing. |
| `test_orchestrator.py` (76) | The pure helpers in `pipeline/orchestrator.py` that the dialog cannot exercise otherwise: `build_cli_args`, `build_adk_drc_argv`, `build_worker_env`, `load_interposer_adapter` / `load_interconnect_adapter`, `available_connection_types`, `connection_method_specs` + `format_connection_label` / `describe_connection_method` (the manifest-sourced dropdown labels; a method id must stay its label's prefix, and a missing manifest must degrade to bare ids rather than invent numbers), `describe_die_thickness_gaps`, `describe_interposer_body_default` (the interposer physical-body plausibility warning: the body now survives from the board stackup, so a value near the FR-4 default is flagged), `discover_dependency_root`, `discover_interposer_lyp`, `derive_interconnect_methods` + `write_ixn_methods_sidecar`, `_read_component_connections`, `describe_assembly_drc`, and `ExportOptions` / `ExportResult` DRC defaults. |
| `test_connection_stacks.py` (9) | `writers/connection_stacks.py`: the manifest-driven connection-stack block must reproduce the literal the writer used to hardcode (and export_chiplet.cpp still emits), plus interconnect-id validation against the manifest. Skips without the interconnect PDK manifest. |
| `test_hyp_to_gds_decoupling.py` (37) | hyp_to_gds connection-stack tables decoupled from the interconnect PDK manifest: the manifest-sourced tables must reproduce the prior IHP literals exactly while the vendor demo method becomes selectable. Skips without the manifest. |
| `test_io_pads.py` (6) | I/O pad geometry in `hyp_to_gds.GDSGenerator.add_io_pads` (TopMetal2 squares, `io_class` dispatch that skips `flipped_bump` / `tsv_bump`, invalid-size skip, missing file) plus `update_chiplet_file` io_pads injection and layout-ref relativization. |
| `test_via_geometry.py` (5) | SG13G2 via PCell bootstrap (must come up when a PDK and PCell deps are present; a silent rectangle fallback is a regression) and the JSON-honoring `_create_simple_via` rectangle fallback that reads `PDK_VIA_PARAMS`. Skips without an SG13G2 PDK. |
| `test_boundary_annotations.py` (6) | The opt-in, viewer-only boundary annotation layer in hyp_to_gds: off by default, one polygon + one label per boundary when on, never the legacy 190/0 fab layer, and the boundary manifest left untouched. |
| `test_layer_guard.py` (3) | Loud guard for unmapped board layers: fails the conversion when more than `UNMAPPED_FAIL_FRACTION` of trace elements sit on unmapped layers, tolerates stray layers below the threshold with one aggregate warning. |
| `test_pillar_manifest.py` (8) | The `<stem>.pillars.json` pillar manifest (schema `adk-pillar-manifest` 1.0.0): written on bump-path runs (empty `pillars` array when nothing is placed, nothing at all without the bump path), positions equal to the drawn GDS instances rebased into the canonical GDS-bbox-corner frame (the `.chiplet` frame), including auto-resolved bumps flagged `moved_by_auto_resolve`, per-die method/diameter, deterministic sorted output. Skips without the sibling PDK checkouts. |
| `test_yaml_escape.py` (18) | The shared YAML scalar escaper (`writers/_yaml.py`): bare-vs-quoted classifier, escape set, and round-trips through a YAML parser (guards the C++/Python byte-locked pair at its most divergence-prone boundary). |
| `regenerate_wirebond_demo.py` | Driver script (not a pytest module): regenerates the wire-bond demo `.chiplet` through the Python pipeline; output feeds the chiplet-studio `CoordFrameContract*` gtests. |
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
cd chiplet_kicad_plugin
.venv/bin/python -m pytest \
    tests/test_discovery.py \
    tests/test_runner.py \
    tests/test_orchestrator.py \
    tests/test_connection_stacks.py \
    tests/test_layer_guard.py \
    tests/test_hyp_to_gds_decoupling.py \
    tests/test_yaml_escape.py \
    tests/test_io_pads.py \
    tests/test_via_geometry.py \
    tests/test_boundary_annotations.py \
    tests/test_pillar_manifest.py -v
```

Some of these skip when the interconnect/SG13G2 PDK siblings are absent; that
is expected on a lone checkout.

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
ROOT=${HOME}/git/heterogenic_chip_design_project
docker run --rm \
  -v $ROOT:$ROOT \
  -e LD_LIBRARY_PATH=$ROOT/kicad/build/release/common:$ROOT/kicad/build/release/common/gal:$ROOT/kicad/build/release/pcbnew:$ROOT/kicad/build/release/api:$ROOT/kicad/build/release/pcbnew/python:$ROOT/kicad/build/release/3d-viewer/3d_cache/sg \
  -e PYTHONPATH=$ROOT/kicad/build/release/pcbnew/python:/tmp/pytest_lib \
  kicad-builder bash -c "
    pip install --target /tmp/pytest_lib pytest pyyaml &&
    cd $ROOT/chiplet_kicad_plugin &&
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
  $ROOT/chiplet_kicad_plugin/.venv/bin/python \
  $ROOT/chiplet_kicad_plugin/tests/regenerate_wirebond_demo.py \
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
`--require-drc`.

Optional fixture override (point at any board, e.g. a chiplet
project under development):

```bash
export CHIPLET_WRITER_BOARD=/path/to/board.kicad_pcb
export HYPERLYNX_WRITER_BOARD=/path/to/board.kicad_pcb  # may be the same
```
