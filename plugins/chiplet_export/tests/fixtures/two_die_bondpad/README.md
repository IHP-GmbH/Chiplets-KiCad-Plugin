# two_die_bondpad

The `.chiplet` pair of a reference flow run of the two-die bondpad example:

- `design.chiplet`: the System document the assembly was built from. It
  names a technology on every component and declares `intm4tm2` and
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

One refresh since the copy: the mount plane is now the interposer PDK's
`beol.attachment_surface_z_um` (10.8103 in `../intm4tm2_pdk/libs.tech/intm4tm2_stack.json`)
instead of the 13.83 the run wrote. In all three files the plane
(`thickness` of the System interposer, `attachment_surface_z` of the draft
and finalized one) moved from 13.83 to 10.8103 and each die's z, the plane
plus the 44 um `cupillar_opt1` stack, from 57.83 to 54.8103. Nothing else
changed.
