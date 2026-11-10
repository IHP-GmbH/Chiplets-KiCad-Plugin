# intm4tm2_pdk

The smallest interposer PDK root the plugin's discovery accepts: an empty
`libs.tech/klayout/` and the stack descriptor `libs.tech/intm4tm2_stack.json`
(the marker `_discover_path_var` checks), copied verbatim from the
OpenIntM4TM2 PDK (schema 1.1, which adds `beol.passivation`; sha256
`46a5879540ece79cec3267975a29af51d617e81fb77e358dfd5af67452d24117`).
Its `beol.attachment_surface_z_um` is 10.8103.

Tests point `INTERPOSER_PDK_ROOT` here so the mount plane is read from a
known file, never from whichever interposer PDK the host has, and so the
suite finalizes a `.chiplet` on a runner with no PDK at all. Refresh it only
by copying the PDK's file again: update the sha256 here, and name the PDK
commit it came from in the commit message.
