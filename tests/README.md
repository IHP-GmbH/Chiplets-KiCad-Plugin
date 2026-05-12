# Tests

The chiplet/Hyperlynx writer tests need `pcbnew` importable, which
requires running pytest from the Python bundled with KiCad (the
kicad-builder Docker image) rather than the host's system Python.

The discovery tests are pure Python and run anywhere with stock
pytest.

## Layout

| File | Coverage | Lands in |
|------|----------|----------|
| `test_chiplet_writer.py`   | Golden-file diff vs current C++ output | Gate 47.3 |
| `test_hyperlynx_writer.py` | Golden-file diff vs current C++ output | Gate 47.4 |
| `test_discovery.py`        | Worker Python / hyp_to_gds.py discovery chain | Gate 47.5 |

## Running

Exact invocation lands in Gate 47.3 once the first golden-file test
is in place.
