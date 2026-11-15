# intm4tm2_pdk

The smallest interposer PDK root the plugin's discovery accepts: an empty
`libs.tech/klayout/`, the technology package manifest
`libs.tech/adk/technology.json` (the marker of a carrier root) and the stack
descriptor its `stack.path` names, `libs.tech/intm4tm2_stack.json`. Both are
copied verbatim from the OpenIntM4TM2 PDK; the commit that copied them names
the PDK commit they came from. `tests/test_carrier_package.py` holds them to
the sha256 below:

- `libs.tech/adk/technology.json`, sha256
  `c6b69da5b1a8df570f6ebeb309580151915ccb423d3abb05eee5d49d7f9c7603`. It also
  names members this root does not hold (the layer properties, the routing
  variants); a test that needs one of them reads the PDK, not this root.
- `libs.tech/intm4tm2_stack.json` (schema 1.1, which adds `beol.passivation`),
  sha256 `46a5879540ece79cec3267975a29af51d617e81fb77e358dfd5af67452d24117`.
  Its `beol.attachment_surface_z_um` is 10.8103.

Tests point `INTERPOSER_PDK_ROOT` here so the mount plane is read from a
known file, never from whichever interposer PDK the host has, and so the
suite finalizes a `.chiplet` on a runner with no PDK at all. Refresh it only
by copying the PDK's files again: update the sha256 here, and name the PDK
commit they came from in the commit message.
