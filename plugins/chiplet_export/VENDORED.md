# Vendored copies

Files in this plugin that are another repository's bytes, copied in. Each entry names
the upstream revision (a commit, or a release tag where the upstream is not public) and
the sha256 of the copy, and each is gated by a test, so a local
edit fails the suite instead of drifting quietly into a second, diverging original.

Re-vendor with a plain copy of the upstream file. The same commit updates the revision and
sha256 in the copy's SOURCE file, which the gates read the pin from, and in its entry here.
For the reader it also updates the release in its entry and in the heading of its standing
rules, any rule the new copy changed, and its table of moves.
`tests/test_vendored_copies.py` holds the pin, the release and each rule's value to the
copy. Never edit a vendored file in place: change it upstream and re-vendor. A copy edited locally is no longer a copy, and
what gets edited away first is exactly the property that made it trustworthy.

## chiplet-system-spec

Upstream `git@github.com:IHP-GmbH/chiplet-system-spec.git`. The gates look for a checkout
named `chiplet-spec` beside this repository (clone it under that name), or at
`CHIPLET_SPEC_ROOT`, and skip where there is none, so the suite still runs standalone.
Under a tiered runner, the checkout is the commit the pin set names, and a copy whose
commit is any other fails: when the pin moves, the copies are re-vendored with it.

Both copies follow one pin, the commit `vendor/chiplet_format_io.SOURCE` names: a tiered
runner checks out that one commit, so the reader and the oracle cannot name different
ones, and both gates read it from that file. Each copy keeps its own sha256 and its own
gate, and the two gates below ask different questions. A pin names a commit on `dev`, the
release commit `dev` fast-forwards to when it is published, or a commit on a release branch
that is kept and never rebased, squashed or deleted; never the head of a branch that merges,
because a pin has to name a commit that survives whatever the merge does.

**`vendor/chiplet_format_io/__init__.py`**
from `reference/python/chiplet_format_io/__init__.py` at commit `48a3052`,
sha256 `256191bb3b7a9538cc5c7ace9a48b8f9c967bb66be0e9ed47a389d8a1af879da`,
reader release 1.22.0.
Gated by `tests/test_vendored_copies.py`. `vendor/chiplet_format_io.SOURCE` states the
same pin in the format gds_to_kicad's copy uses, and the gate checks it too.
Its license is the repository's `LICENSES/Apache-2.0.txt`, the bytes of upstream's
`LICENSES/Apache-2.0.txt` at the same commit (no NOTICE upstream), as gds_to_kicad carries
it. Not gated: re-copy it only if upstream changes it. It sits outside the package directory
because adk-tools' `ci/check_vendored_reader.py` holds every file under a copy to a file of
the reference, and the reference directory carries no license.

What this copy holds the plugin to, at 1.22.0:

- A document of format `1.5` or below reads without the newer-minor warning
  (`ACCEPTED_FORMAT_VERSIONS`). The assembly stamp `SUPPORTED_FORMAT_VERSION` is `1.0`.
  The exporter stamps `1.0`; the finalizer keeps the stamp of the file it re-emits, an
  accepted higher minor included (`check_format_version` in `hyp_to_gds.py`).
- `assembly.units` is closed to `um`, which is what the writer emits.
- A source document (one with a top-level `system` block) is refused unless the caller
  passes `allow_source=True`. The plugin never passes it: every read here is of an
  assembly, including the `--system-chiplet` input, which is the elaborated
  `design.chiplet`.
- An assembly that carries a top-level `unplaced` block is refused. That block belongs to
  a source; the writer emits none.
- A closed-vocabulary member newer than the document's stamp is refused (`MEMBER_SINCE`;
  today only `solder_bump`, since 1.1). The writer emits no `solder_bump` interface.
- The plugin calls neither `split_top_level_keys` nor the root block capture
  (`capture_top_level_blocks`).

Each move of the pin since 1.4.0, newest first. "Frontier identical" means the two
readers refuse the same oracle documents, which
`test_a_reader_whose_answers_changed_moved_more_than_the_patch` checks on every re-vendor;
on those rows the refusal messages were compared too, and match. The corpus is the
line-break and grammar oracle, so that is the frontier it covers.

| date | move | what changed for this plugin |
|---|---|---|
| 2026-11-10 | pin moved within 1.22.0, off a commit that never reached the published `dev`, onto the `dev` commit with the same tree | None: identical tree, so reader and oracle bytes are unchanged. |
| 2026-11-09 | pin moved within 1.22.0, onto the mount-plane commits that `dev` fast-forwards to | None: mount-plane conformance cases, the description, docs and reader tests only. Reader and oracle bytes unchanged. |
| 2026-11-08 | 1.21.0 to 1.22.0, the merge of the v2026.11 release into dev | Release number only. `technology_package` (the technology package validator) stays unvendored and unread here. Oracle bytes unchanged. |
| 2026-11-06 | pin moved within 1.21.0, onto the v2026.11 release branch | None: the chiplet pad vocabulary's version, under `schemas/`, only. Reader and oracle bytes unchanged. |
| 2026-11-03 | 1.22.0 back to 1.21.0 | Release number only: this release keeps the 1.21.0 pin, so the copy returns to it. Oracle bytes unchanged. |
| 2026-11-01 | 1.21.0 to 1.22.0, a dev commit after the release commit | Release number only. Adds `technology_package` (the technology package validator), which this plugin does not vendor or read. Oracle bytes unchanged. |
| 2026-10-31 | pin moved within 1.21.0 | None: the source elaborator's carrier refusal and its test, under `description/`, only. Reader and oracle bytes unchanged. |
| 2026-10-30 | 1.20.0 to 1.21.0 | Release number only. Adds `sheet_names` (the names a KiCad sheet carries verbatim), which this plugin does not vendor or read. Oracle bytes unchanged. |
| 2026-10-30 | 1.19.0 to 1.20.0, a dev commit after the release commit | Format 1.5 (a rail's routing requirement in the source, never read here): `ACCEPTED_FORMAT_VERSIONS` `1.4` to `1.5`. Oracle bytes unchanged. |
| 2026-10-29 | 1.18.0 to 1.19.0 | Release number only (ihp-sg13cmos5l rows of `ready_die_names`, not vendored). Oracle bytes unchanged. |
| 2026-10-29 | 1.17.0 to 1.18.0 | Format 1.4 (a die's `admission` file in the source, never read here): `ACCEPTED_FORMAT_VERSIONS` `1.3` to `1.4`. Oracle bytes unchanged. |
| 2026-10-29 | pin moved within 1.17.0 | None: the connectivity-form reader, its test and `docs/VERSION_POLICY.md` only. |
| 2026-10-28 | pin moved within 1.17.0 | None: `README.md` and the format and coordinate-frame documents only. |
| 2026-10-28 | pin moved within 1.17.0 | None: `conformance/manifest.yaml` and `conformance/run_conformance.py` only. |
| 2026-10-27 | 1.16.0 to 1.17.0 | Version only. `footprint_ids.py` is added beside the reader; this copy does not import it and the plugin does not vendor it. |
| 2026-10-27 | 1.14.1 to 1.16.0 | Formats 1.2 (a power rail's volts) and 1.3 (the unplaced source): `ACCEPTED_FORMAT_VERSIONS` `1.1` to `1.3`, and an assembly's `unplaced` block is refused. Frontier identical. |
| 2026-10-25 | pin moved within 1.14.1, cut again | None: cut again for its own description tests. |
| 2026-10-24 | pin moved within 1.14.1, cut again | None: cut so its tree cites only published commits. |
| 2026-10-24 | 1.14.0 to 1.14.1 | One message: the source-document refusal names the `chiplet_system_description` package. Frontier identical. |
| 2026-10-19 | 1.13.0 to 1.14.0 | The format 1.1 reader: `assembly.units` closed to `um`, a source document refused without `allow_source`, `ACCEPTED_FORMAT_VERSIONS` `1.0` to `1.1`, `MEMBER_SINCE` refusals. Frontier identical. |
| 2026-10-17 | 1.5.0 to 1.13.0 | Additive: the root block capture API and the version. Frontier identical. |
| 2026-09-05 | 1.4.0 to 1.5.0 | Additive: `split_top_level_keys` and `TopLevelKeys`. |

**`tests/fixtures/top_level_blocks_cases.json`**
from `conformance/fixtures/top_level_blocks_cases.json` at commit `48a3052`,
sha256 `45c37f47f142d88c63dfd3056a3dceba07afe6f2035d25243b7f87f2c37bccd0`,
oracle version 5. Its commit is the reader's pin.
Gated by `tests/test_top_level_block_grammar.py`
(`test_oracle_copy_is_unmodified`, `test_the_oracle_pin_carries_those_bytes` and
`test_oracle_copy_matches_the_spec_checkout_when_present`),
which also asserts the oracle version it was written against, because a consumer
reading `refused_by` off a version 1 copy gets `None` for every row and tests
nothing while staying green.

The two gates ask different questions on purpose. The reader is pinned to the bytes of a
named commit, and is checked against that commit's blob rather than against whatever the
sibling checkout currently has, because a checkout sitting on a feature branch is not
evidence that the copy drifted. The fixture is an oracle: it is also compared against the
checkout's working tree, so when the oracle moves upstream this repo goes red and has to
decide, which is the whole reason the fixture is consumed here instead of transcribed.

## interposer-pnr

**`vendor/round_trip_names.py`**
from `src/interposer_pnr/round_trip_names.py` at release `v2026.11.1`,
sha256 `e7a11170121cc17dcbdefa6ffb0efdf5319c6474950ce2751bcb2ce34e6d1060`.
interposer-pnr's reader of an export report's net names and its rules for reading a
board's carriers against a report (`read_board_carriers`: each carrier's io_pad id, and
whether the board follows a sheet), by which the draft's io_pad ids and nets are named
(`draft_net_names.py`, `writers/chiplet_writer.py`); gds_to_kicad vendors this file too,
so both readers keep one rule. The pin
is also in `vendor/round_trip_names.SOURCE`, in the format gds_to_kicad's copy uses.
This copy reads round_trip version 6, whose record lists its features; a
report names carrier nets by the `sheet` feature, and a feature it does not know is ignored.
Gated by `tests/test_vendored_copies.py`, on the local copy only: no gate here reads
interposer-pnr.
