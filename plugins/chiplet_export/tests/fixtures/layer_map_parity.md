# Layer reader parity inputs

`layer_map_frozen.py.txt` contains verbatim definitions from the plugin's
`plugins/chiplet_export/hyp_to_gds.py` as of 2026-09-20 (that file's SHA-256:
`6b30087599118d057c9915bd33dabacfbf0f77abc657e9eb3d39c423d5141350`), with stdlib
imports added.
It is a frozen test oracle, never a production reader.

`intm4tm2_reference.lyp` is the original byte sequence of OpenIntM4TM2's
`libs.tech/klayout/tech/intm4tm2.lyp` as the ADK image pinned it. SHA-256:
`96fe744ba8ed4817a7e79800d68027d4782b649c208d9996b5de4a6fd6c823d8`.
The ADK image copies that pinned submodule to
`/opt/adk-tools/OpenIntM4TM2` and sets `INTERPOSER_PDK_ROOT` to that location.
Control B selected the LYP there without `-l` and reported 187 map entries.

The controls compare the old/shared full dictionaries, duplicate and purpose
lookup behavior, and default/explicit/variable/env/walk path selection using
this exact LYP. The carrier preflight integration control verifies the same
bytes and resolved path are handed to the worker through `-l`. The integration
owner separately hashes the in-image LYP before the native reference run.
These controls do not claim native GDS or DRC parity.
