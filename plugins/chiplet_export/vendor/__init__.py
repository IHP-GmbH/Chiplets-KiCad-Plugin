# SPDX-License-Identifier: GPL-3.0-or-later
"""Vendored, dependency-clean code from other repositories used by the plugin.

``round_trip_names.py`` is interposer-pnr's reader of an export report's net
names, a byte copy pinned in ``round_trip_names.SOURCE`` (see VENDORED.md).

``chiplet_format_io`` is the Apache-2.0 reference reader/
writer for the ``.chiplet`` format (its own SPDX header is retained verbatim).
It is vendored byte-identical to the canonical copy at
``chiplet-system-spec/reference/python/chiplet_format_io/__init__.py`` so the waist
"delegate-or-parity" rule holds and the vendored-copy identity check can
compare it against the reference. Do NOT hand-edit the vendored source; re-sync
it from the reference instead.
"""
