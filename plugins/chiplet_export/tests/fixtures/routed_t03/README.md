# routed_t03

The two-die typical_03 example as the packaging flow's export row drew it, and
what the export computes from that board before it starts the worker. The
routed path, which has no board, must compute the same.

`inputs/` (copied from one flow run, unchanged unless noted):

- `cf_typical_03_from_schematic.draft.kicad_pcb` and its `.kicad_pro`: the
  draft board and project the export row wrote.
- `export.json`: that row's report. Its run paths are replaced by
  `make_oracle.py mask-report`; the export reads none of them, and the oracle
  run checks that the original gives the same bytes.
- `design.chiplet`: the elaborated system the run placed and routed.
- `pins/`: the two die pin lists the run passed with `--pins`.

`make_oracle.py` runs the board-path functions on that board with real
pcbnew and writes `oracle/`. See PROVENANCE.md for the image, the commit and
every digest.
