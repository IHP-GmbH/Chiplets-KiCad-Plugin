# SPDX-License-Identifier: GPL-3.0-or-later
"""Vendored, dependency-clean third-party packages used by the plugin.

Only ``chiplet_format_io`` lives here today: the Apache-2.0 reference reader/
writer for the ``.chiplet`` format (its own SPDX header is retained verbatim).
It is vendored byte-identical to the canonical copy at
``chiplet-spec/reference/python/chiplet_format_io/__init__.py`` so the waist
"delegate-or-parity" rule holds and the Lane 2 vendored-copy identity check can
compare it against the reference. Do NOT hand-edit the vendored source; re-sync
it from the reference instead.
"""
