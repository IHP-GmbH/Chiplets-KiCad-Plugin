# Vendored copies

Files in this plugin that are another repository's bytes, copied in. Each entry names
the upstream commit and the sha256 of the copy, and each is gated by a test, so a local
edit fails the suite instead of drifting quietly into a second, diverging original.

Re-vendor with a plain copy of the upstream file, then update the commit and the sha256
here and in the gate, in the same commit as the copy. Never edit a vendored file in
place: change it upstream and re-vendor. A copy edited locally is no longer a copy, and
what gets edited away first is exactly the property that made it trustworthy.

## chiplet-spec

Upstream `git@github.com:IHP-GmbH/chiplet-spec.git`. The gates look for a checkout
beside this repository, or at `CHIPLET_SPEC_ROOT`, and skip where there is none, so the
suite still runs standalone.

**`vendor/chiplet_format_io/__init__.py`**
from `reference/python/chiplet_format_io/__init__.py` at commit `a5cd3ef`
("Export a reader release so vendored copies are gateable"),
sha256 `dfc2497f8d4a2fc42da590b25e72536fc87c41b400bd113da61bab999262d3ab`.
Gated by `tests/test_vendored_copies.py`.

**`tests/fixtures/top_level_blocks_cases.json`**
from `conformance/fixtures/top_level_blocks_cases.json` at commit `8a2e6be`,
sha256 `20a80fc0623346643479cebeab697cc3883060ffe2e9a305fa9e44add64e0de3`.
Gated by `tests/test_top_level_block_grammar.py`
(`test_oracle_copy_is_unmodified` and `test_oracle_copy_matches_the_spec_checkout_when_present`).

The two gates ask different questions on purpose. The reader is pinned to the bytes of a
named commit, and is checked against that commit's blob rather than against whatever the
sibling checkout currently has, because a checkout sitting on a feature branch is not
evidence that the copy drifted. The fixture is an oracle: it is compared against the
checkout's working tree, so when the oracle moves upstream this repo goes red and has to
decide, which is the whole reason the fixture is consumed here instead of transcribed.
