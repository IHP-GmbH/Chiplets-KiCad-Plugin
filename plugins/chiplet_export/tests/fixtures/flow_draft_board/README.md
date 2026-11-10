# flow_draft_board

One draft-path pair, the board and the sheet of the same run, for the
padring_die_cmos5l example: interposer_pnr's bumps, pdn, route, export and
schematic stages, with the interposer-pnr and gds_to_kicad that pair the draft
sheet with the draft board, run fresh in an ADK-Tools image on 2026-11-01 from
the elaborate outputs of the example's run of 2026-10-31. Route 38/38 nets with 0 DRC; `kicad-cli pcb drc
--schematic-parity` on the pair reports 0 issues.

`cf_padring_die_cmos5l.draft.kicad_pcb` is the draft board export wrote: three
die footprints (U1 to U3, `chiplet_dies:<top cell>`) and 32 wire-bond carrier
pads from `chiplet_carrier`, each with IO_PAD_ID its own Reference and linked
to no sheet symbol. It has no board-root property, and its `.kicad_pro` is not
kept here. Unchanged from the run; it names no host path. sha256:
`9eca915a4bbdaace4148dacd2968bab7d51490bd6b63304e09e3800e974fcc59`.

`cf_padring_die_cmos5l.draft.kicad_sch` is the system sheet the same run drew
(chiplet_to_kicad_sch), unchanged. Its title-block comment 1 holds the digest
of the run's `design.chiplet`. sha256:
`0bbf948c7161b148b46a9ad07959d492071d2ef00eebd769e3398ec668fddf8f`.

`test_board_save.py` saves copies of both the way a GUI save does;
`test_io_pad_ids.py` reads the board's carriers.
