# Tests

Most of this runs without KiCad: `board_reader.py`, `paths.py` and all of
`apply_resize.py` except `apply_to_instance` have no `pcbnew` or `wx`
dependency, so those suites use the stand-in board and footprint objects that
`ARCHITECTURE.md` section 8 documents. `test_apply_to_instance.py` is the
exception and guards itself with `pytest.importorskip("pcbnew")`.

`conftest.py` puts the `plugins/` directory on `sys.path` so the modules can be
imported as the `resizer_passive_elements` package, which is what their
relative imports need and what pcbnew does through the install symlink.

| File | Coverage |
|------|----------|
| `test_board_reader.py` (25) | Field parsing and device dispatch. The `w`/`l` unit contract is the load-bearing part: a `um`/`u` suffix means micrometres and a bare number means metres, and reading one as the other is a silent factor-of-1e6 error. The sibling `chiplet_export` plugin reads the same fields, so the two parsers must agree. Also `_parse_capacitance_fF` suffix precedence, warnings on unusable fields (never a silent drop for a registered device), the `GetFootprints()` fallback, and that an unregistered `Model` is skipped quietly. |
| `test_paths.py` (14) | Dependency discovery and settings persistence: `INTERPOSER_PDK_ROOT` ahead of the `INTM4TM2_ROOT` alias, a set-but-invalid variable falling through instead of winning, `interposer/` as a sibling-checkout candidate, the ancestor walk reaching past the repository, the output directory defaulting next to the board and never into the PDK, and the settings JSON round trip (including a corrupt file being ignored and a board with no file on disk being reported rather than silently dropped). |
| `test_apply_resize.py` (25) | Footprint generation and naming. `Nominal` (the round value a part is called) is kept apart from `Capacitance` (what the plate computes to): the grid-snapped 8.11 um square for a nominal 100 fF gives 99.95575 fF, so naming from the recomputed value renamed the stock `CMIM_100fF` to `CMIM_99p956fF` on its second run. A nominal is honoured only while `w`/`l` still are its grid-snapped square, and a regenerated family member is checked property-for-property against the committed `CMIM_100fF.kicad_mod`. Also the capacitance labels, `w`/`l` written back in the metres form the symbol library uses, the set of fields the plugin owns, and the dimension bounds. The upper bound is a regression guard, not politeness: a bare `8.11` in the `w` field parses as metres, i.e. 8.11e6 um, and without it the generator walks its via array over that area (~1e13 iterations) on the wx thread, hanging pcbnew for good. It bounds the capacitance rather than the side, because the via count scales with `w*l` and an in-spec 100 x 50 um rectangle has to keep working. Tests touching the generator skip without the interposer PDK checkout. |
| `test_apply_to_instance.py` (5, needs pcbnew) | The only code that touches the user's board. Everything it can lose is silent until much later, so the carry-over is pinned against real pcbnew: the KIID path (without it the next "Update PCB from Schematic" stops recognising the instance as the symbol's), the locked flag, the symbol's `Datasheet` and `Sim.*` model, a custom field the user had visible staying visible, nets following the pad number, and the plugin's own fields winning over stale ones on the replaced instance. |

## Running

```bash
cd plugins/resizer_passive_elements
python3 -m pytest tests -q
```

The generator-backed tests in `test_apply_resize.py` need an interposer PDK
checkout to be discoverable (`INTERPOSER_PDK_ROOT` or a sibling `interposer/`);
they skip cleanly without one, which is what happens on the CI runner.
