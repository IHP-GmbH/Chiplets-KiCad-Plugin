# two_die_bondpad

The `.chiplet` pair of a reference flow run of the two-die bondpad example:

- `design.chiplet`: the System document the assembly was built from,
  regenerated from its spec since (below). It names a technology on every component and declares `intm4tm2` and
  `sg13g2` under `technologies`.
- `draft.chiplet`: the run's draft `.chiplet`, already finalized by a
  plugin from before a null `technologies` became `{}`. It carries the
  layouts, top cells, thicknesses and z values, `technologies: null` and no
  component technology.

`finalized.chiplet` is what the finalizer made of `draft.chiplet` on a
second pass, with the inputs `test_system_technology_overlay.py` gives it,
at the commit before `--system-chiplet` existed. It differs from
`draft.chiplet` only by `technologies: {}`. Without the flag the finalizer
must still produce it byte for byte, so do not regenerate it with the
current code.

Two refreshes since the copy. First, the mount plane became the interposer
PDK's `beol.attachment_surface_z_um` (10.8103 in
`../intm4tm2_pdk/libs.tech/intm4tm2_stack.json`) instead of the 13.83 the run
wrote: the System interposer's `dimensions.thickness` and the draft and
finalized `attachment_surface_z` went from 13.83 to 10.8103, and each die's z,
the plane plus the 44 um `cupillar_opt1` stack, from 57.83 to 54.8103. The
System's thickness is the body; it equals the plane only in this fixture.

Then `design.chiplet` was regenerated, not edited: `emit(spec.system)` of
packaging-flow's `tests/fixtures/specs/ready_pair.py`, with a chiplet-spec
writer that writes the mount plane and the die inputs from packaging-flow
`examples/two_die_ready/inputs/dies`. That added the interposer's
`attachment_surface_z: 10.8103` and four path lines, `layer_properties` and
`stackup` under both `technologies` entries, which the writer emits and the
first capture predates. The same emit with the writer from before the plane
was written differs from this file by the plane line alone. With `--system-chiplet` the four path lines reach the
finalized document, from the System's catalog through
`technology_overlay.plan`; the System's plane does not.
