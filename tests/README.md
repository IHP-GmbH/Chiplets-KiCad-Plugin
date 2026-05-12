# Tests

The chiplet / Hyperlynx writer tests need `pcbnew` importable,
which requires running pytest from the Python bundled with KiCad
(the `kicad-builder` Docker image) rather than the host's system
Python. Tests that only need stdlib (e.g. discovery) run anywhere
with stock pytest.

When pcbnew is unavailable, `pytest.importorskip("pcbnew")` skips
the affected tests cleanly rather than failing.

## Layout

| File | Coverage | Status |
|------|----------|--------|
| `test_chiplet_writer.py`   | Structural invariants on the YAML emitted by `writers/chiplet_writer.py` | Gate 47.3 |
| `test_hyperlynx_writer.py` | Structural invariants on the .hyp emitted by `writers/hyperlynx_writer.py` (metric headers, BOARD / STACKUP / DEVICES blocks, GDS_FILE propagation, PADSTACK id consistency) | Gate 47.4 |
| `test_discovery.py`        | Worker Python / hyp_to_gds.py discovery chain | Gate 47.5 |
| `test_io_pads.py`          | IO_CLASS field propagation (heritage from kicad_interposer_hyperlynx_to_gds) | imported with Gate 47.2 |
| `check_complete_gds_alignment.py` | klayout regression script for canonical .chiplet output | imported with Gate 47.2 |

Byte-exact equivalence vs the C++ exporters lives in Gate 47.7
(critical + functional verification), where both pipelines run
side-by-side in the kicad-builder Docker session.

## Running

From inside the kicad-builder Docker image:

```bash
docker run --rm \
  -v ${HOME}/git/heterogenic_chip_design_project:/work \
  kicad-builder bash -c "
    cd /work/chiplet_kicad_plugin &&
    python3 -m pytest tests/ -v
  "
```

Optional fixture override (point at any board, e.g. a chiplet
project under development):

```bash
export CHIPLET_WRITER_BOARD=/path/to/board.kicad_pcb
export HYPERLYNX_WRITER_BOARD=/path/to/board.kicad_pcb  # may be the same
```
