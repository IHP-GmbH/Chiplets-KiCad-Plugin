# Tests

The chiplet / Hyperlynx writer tests need `pcbnew` importable,
which requires running pytest from the Python bundled with KiCad
(the `kicad-builder` Docker image) rather than the host's system
Python. Tests that only need stdlib (`test_discovery.py`,
`test_runner.py`, `test_orchestrator.py`) run anywhere with stock
pytest.

When pcbnew is unavailable, `pytest.importorskip("pcbnew")` skips
the affected tests cleanly rather than failing.

## Layout

| File | Coverage |
|------|----------|
| `test_chiplet_writer.py`   | Structural invariants on the YAML emitted by `writers/chiplet_writer.py`; includes the interf_u regression that exercises boards whose PROJECT wrapper lacks `GetTextVars`. |
| `test_hyperlynx_writer.py` | Structural invariants on the .hyp emitted by `writers/hyperlynx_writer.py` (metric headers, BOARD / STACKUP / DEVICES blocks, GDS_FILE propagation, PADSTACK id consistency). |
| `test_discovery.py`        | Worker Python / hyp_to_gds.py discovery chain (env var, .venv, project text var, PATH probe with klayout+yaml import). |
| `test_runner.py`           | Async subprocess runner: line-by-line stdout/stderr callbacks, exit code propagation, cancel via `threading.Event`, env/cwd plumbing. |
| `test_orchestrator.py`     | `build_cli_args` argv builder: default toggles, complete-assembly toggle, top-cell override, connection-stack passthrough, LYP / I/O pads / cu-pillar GDS paths, worker-python override exclusion. |
| `test_byte_exact_writers.py` | Byte-exact diff: Python writers' output vs the C++ `export_chiplet.cpp` / `export_hyperlynx.cpp` reference files. |
| `test_io_pads.py`          | IO_CLASS field propagation (heritage from kicad_interposer_hyperlynx_to_gds). |
| `regenerate_wirebond_demo.py` | Driver script (not a pytest module): regenerates the wire-bond demo .chiplet through the Python pipeline; output feeds the chiplet-studio `CoordFrameContract*` gtests. |
| `check_complete_gds_alignment.py` | klayout regression script that asserts U1's flipped cell sits over the cu-pillar array in the complete-assembly GDS. |

Totals: 47 stdlib + writer assertions, 2 byte-exact
comparisons, 1 GetTextVars regression, 1 cupillar passthrough, plus
the regenerate-script-driven gtest regression net. A handful of
tests skip on hosts without the KiCad fixtures (interf_u demo,
wire-bond demo `.kicad_pcb`).

## Running

Stdlib-only tests (no Docker needed):

```bash
cd chiplet_kicad_plugin
.venv/bin/python -m pytest \
    tests/test_discovery.py \
    tests/test_runner.py \
    tests/test_orchestrator.py -v
```

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
ROOT=/home/montanares/git/heterogenic_chip_design_project
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

Round-trip regression net (driver script + chiplet-studio gtests):

```bash
# 1. Regenerate the wire-bond demo .chiplet via the Python pipeline.
docker run --rm -v $ROOT:$ROOT -e LD_LIBRARY_PATH=... -e PYTHONPATH=... \
  kicad-builder \
  $ROOT/chiplet_kicad_plugin/.venv/bin/python \
  $ROOT/chiplet_kicad_plugin/tests/regenerate_wirebond_demo.py \
      --use-existing-hyp $ROOT/kicad_designs/interposer_wire_bonding_demo/test.hyp \
      --cupillar-gds $ROOT/kicad_designs/interposer_wire_bonding_demo/cu_pillars.gds \
      --output-dir $ROOT/_tmp_regen

# 2. Feed the regenerated .chiplet to the chiplet-studio gtests.
cd $ROOT/chiplet-studio/build && ./tests/chiplet_tests \
    --gtest_filter='CoordFrameContract*'
```

Optional fixture override (point at any board, e.g. a chiplet
project under development):

```bash
export CHIPLET_WRITER_BOARD=/path/to/board.kicad_pcb
export HYPERLYNX_WRITER_BOARD=/path/to/board.kicad_pcb  # may be the same
```
