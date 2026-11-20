# Provenance

The oracle was computed once, in a container of the adk-tools image
`sha256:f608643e3ed84d945992d921ab951d9ccb2e556524ddeffdeb38d32ed00124d9`, with pcbnew 9.99.0-unknown and Python 3.12.3, from the
plugin at commit `96ef01410450b94dd7951e0d25e887d9fa205105` (its board-path code unchanged from its parent):

    python3 -B make_oracle.py compute --out oracle --report-pair <the unmasked export.json>

A second compute gave the same bytes. `--report-pair` checks that the masked
`inputs/export.json` gives the same oracle as the report the run wrote, whose
sha256 was `6b89739002d5d8c7c69fc46a0331e0ab12b16e757f416bc77ad4ee4f3796afbf`.

Inputs (sha256):

- `inputs/cf_typical_03_from_schematic.draft.kicad_pcb` `3e6e7d1d17c1a30f753c955041432d4c447c1a00bfc43291c270ed3b1ca29d90`
- `inputs/cf_typical_03_from_schematic.draft.kicad_pro` `3d935116ee350e53de2a5f05499439acfa4362997fb68b8ff4a63160d15e3ac7`
- `inputs/design.chiplet` `2ed1382f6452d556bb21c304d6fe3f9fad8cc1effd535c1276c4e4711f61da03`
- `inputs/export.json` `c78f5860605b2bce39eaf222c5344c3382113909963075044bf782230c037d58`
- `inputs/pins/PAD_FRAME_L.pins.json` `34d940dba99bc4a3c6ed0ff4f1a4fe01900f611021576926c6c859f597c192ed`
- `inputs/pins/PAD_FRAME_R.pins.json` `645c1740a0da1735ee1c65e9da06e4670968601e91b3c71b81ca11a899a8b838`

Outputs, `oracle/` (sha256):

- `U1_pins.json` `9c3cfb686a46c8636401474451105e1a74db7cdff8189410b41e3f4d3c4e544c`
- `U2_pins.json` `caadb192db60bbaaa50631180ec77b6f96490e30ebbe78ef2891c6dd82d2eea7`
- `cf_typical_03_from_schematic.draft.chiplet` `e0b6d45649f8acd35302add4dc1f74949098be68a57645755a750ab9b764ff0e`
- `counts.json` `3f212a8c037517194c36ae9d3f28aa9b74445ed57b3449756ad82535ad14f3ce`
- `die_connections.json` `19872c2ac55430dfc80fb29ea3eb3c4279beb629cf1f87de2b9dbe94bb488f00`
- `die_thicknesses.json` `350fab2b648b9e871ef2ef1addb3a34555601e45a0598b25b57726d5d15c6006`
- `io_pads.json` `c1ad87ada319aa43b69e4b7534581fdc4ccb4bb897cced285a8edbc0c0399a06`

`inputs/routed-geometry.json` and `inputs/routed-io-pads.json` are what the
interposer-pnr routed geometry writer (commit
`52e2ce04041fef2e6080c9b882793a937cf7dec6`) wrote from its own copy of this
run, `tests/data/routed_geometry_typical_03`, with the arguments its tests use
(`--layout U1=../../dies/PAD_FRAME_L/layout.gds`, the same for U2,
`--board-relative-layout`). The io_pads manifest is unchanged, sha256
`b35b256a4470812360da3959738ca32e587eaedd94ff9975ccfd1ab3be8e8ff0`. The
document, as written (sha256
`83aa669b6a4ec49ee273da47c7131371e51a4d8777cb755e6dcaa0c5a7fc90ad`), had two
changes, digests untouched:

- the absolute folder of that copy became `<RUN>` (in `inputs.pins` and
  `inputs.form`, which the export does not read);
- each `devices[].layout.path` became the GDS_FILE text this board gives the
  die, `../../../../dies/...`, as the flow passes it with `--layout`. The
  writer's copy keeps its layouts two folders up.

Result: sha256 `41cc95805d4c27311fb31cb915a6f02c33b54f2ab8bc00c11435ab896faaefb4`.

The one normalising rule: the absolute folder of `inputs/` at run time becomes
`<RUN>` in the oracle files. On this run it matched nothing.

The die layouts are in `dies/`, copied from interposer-pnr's
`tests/data/routed_geometry_typical_03/dies` (commit `dd9d941a0cc42f0562e28930dba6e7e3331004d7`):
`dies/PAD_FRAME_L/layout.gds` `36aa050b9898a580b7e18cd65d73aaa890e5ef951d5d09b1fb6da313e5863dd4` and `dies/PAD_FRAME_R/layout.gds` `3ba2845013c5475411298a6016db49c6db4c663b0fe25229747e8e44bfb8d3d9`, the
sha256 the routed geometry binds and `inputs/export.json` records. The board
names them `../../../../dies/PAD_FRAME_L/layout.gds` and
`../../../../dies/PAD_FRAME_R/layout.gds`; tests that read them stage the
document four folders below a copy of `dies/`.
