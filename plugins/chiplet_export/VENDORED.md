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

Both copies come from the same upstream state, `d9229cf` on `dev`, which is a
merge commit whose gate ran green. Pinning the gated head rather
than the last commit that happened to touch each file means the pin names a state
somebody verified, not just a state that exists.

**`vendor/chiplet_format_io/__init__.py`**
from `reference/python/chiplet_format_io/__init__.py` at commit `d9229cf`,
sha256 `91dc33a1318963342797eb5632fcd274a8dd2147a3eb8645304f2aa83a017795`.
Gated by `tests/test_vendored_copies.py`.

**`tests/fixtures/top_level_blocks_cases.json`**
from `conformance/fixtures/top_level_blocks_cases.json` at commit `d9229cf`,
sha256 `fa808d59c97c793d33ff3263360ac7adb329b8ea6e9af2c5f761151956b8af7c`,
oracle version 4.
Gated by `tests/test_top_level_block_grammar.py`
(`test_oracle_copy_is_unmodified` and `test_oracle_copy_matches_the_spec_checkout_when_present`),
which also asserts the oracle version it was written against, because a consumer
reading `refused_by` off a version 1 copy gets `None` for every row and tests
nothing while staying green.

The two gates ask different questions on purpose. The reader is pinned to the bytes of a
named commit, and is checked against that commit's blob rather than against whatever the
sibling checkout currently has, because a checkout sitting on a feature branch is not
evidence that the copy drifted. The fixture is an oracle: it is compared against the
checkout's working tree, so when the oracle moves upstream this repo goes red and has to
decide, which is the whole reason the fixture is consumed here instead of transcribed.
