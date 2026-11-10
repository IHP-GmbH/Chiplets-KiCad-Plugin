# SPDX-License-Identifier: GPL-3.0-or-later
"""A die's full layout for the .chiplet, beside the abstract the board places.

A die whose footprint ``GDS_FILE`` is an abstract (a stand-in drawn on
layers the assembly can carry) keeps that abstract in the complete GDS and
its boundary manifest. ``hyp_to_gds.py --die-layouts FILE`` names the die's
full layout, and the finalizer writes it, not the abstract, as the die
component's ``layout`` and ``top_cell``. The component then says where the
abstract is:

    metadata:
      assembly_view: abstract
      assembly_view_layout: <the abstract, relative to the .chiplet's folder>

The sidecar is one JSON object with one key per die reference:

    {"U2": {"layout": "/abs/U2.gds", "top_cell": "U2",
            "bbox_um": [0, 0, 1000, 1000]}}

``layout`` is absolute and must exist. ``top_cell`` and ``bbox_um``
([x_min, y_min, x_max, y_max] in um) are the die contract's. The top cell is
taken as given, never resolved as the file's one parentless cell, so the
full layout may have other top cells; the named one must be one of them.

Both files must agree with the contract's box in the die's own frame: the
full layout's named top cell and the abstract's top cell each span exactly
``bbox_um``, compared in integer database units at the assembly dbu,
0.001 um. A box that matches one file and not the other refuses; the die
would be drawn in one place and described in another.

Anything else refuses, and every refusal comes before anything is written.
The grammar here needs no KLayout; the reads go through
``die_outline_gds.DieGdsReader``.
"""

import json
import os
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence, Tuple

if __package__:
    from .die_outline_gds import ASSEMBLY_DBU, DieGds, LayoutTops
else:
    from die_outline_gds import ASSEMBLY_DBU, DieGds, LayoutTops

#: The keys of one entry, all required, no others.
KEYS = ("layout", "top_cell", "bbox_um")

#: What the finalizer writes into each such component's metadata.
ASSEMBLY_VIEW = "abstract"


class DieLayoutRefused(Exception):
    """The --die-layouts sidecar, or a die it names, cannot be used.

    Nothing may be written once this is raised. Not a ``ValueError``, for
    the reason ``DieOutlineRefused`` gives. ``offenders`` lists every
    (reference, cause) pair found; a cause about the whole file has the
    file as its reference.
    """

    def __init__(self, offenders: Sequence[Tuple[str, str]]):
        self.offenders = list(offenders)
        super().__init__("\n".join(self.lines()))

    def lines(self) -> List[str]:
        return [refusal_line(ref, cause) for ref, cause in self.offenders]


def refusal_line(reference: str, cause: str) -> str:
    """The one log line a refused die gets."""
    return "Die layout refused: %s: %s" % (reference or '""', cause)


@dataclass(frozen=True)
class DieLayout:
    """One die's entry: its full layout, and the contract's cell and box."""
    ref: str
    layout: str                                  # absolute, as written
    top_cell: str
    bbox_dbu: Tuple[int, int, int, int]          # at ASSEMBLY_DBU


class _Pairs(list):
    """A JSON object as its (key, value) pairs, duplicates kept."""


def load(path: str) -> Dict[str, DieLayout]:
    """The sidecar at ``path``, checked; DieLayoutRefused otherwise.

    Every entry is checked before raising, so one run names all of them. A
    missing layout file refuses here; whether the file agrees with the
    contract is ``frame_cause``'s question.
    """
    def number(text):
        raise ValueError("%s is not a number" % text)

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle, parse_float=Decimal,
                             parse_constant=number, object_pairs_hook=_Pairs)
    except (OSError, ValueError) as exc:
        raise DieLayoutRefused([(path, "cannot read the --die-layouts "
                                       "sidecar: %s" % exc)]) from None
    if not isinstance(data, _Pairs):
        raise DieLayoutRefused([(path, "the --die-layouts sidecar is not a "
                                       "JSON object of die references")])

    offenders: List[Tuple[str, str]] = []
    counts = Counter(ref for ref, _entry in data)
    layouts: Dict[str, DieLayout] = {}
    for ref, entry in data:
        if counts[ref] > 1:
            counts[ref] = -counts[ref]   # reported; later ones are skipped
            offenders.append((ref, "%s names it %d times; a die has one full "
                                   "layout" % (path, -counts[ref])))
            continue
        if counts[ref] < 0:
            continue
        try:
            layouts[ref] = _entry(ref, entry, path)
        except _EntryError as exc:
            offenders.append((ref, str(exc)))
    if offenders:
        raise DieLayoutRefused(offenders)
    return layouts


class _EntryError(Exception):
    pass


def _entry(ref: str, entry, path: str) -> DieLayout:
    where = "its entry in %s" % path
    if not ref.strip():
        raise _EntryError("%s has an empty die reference" % path)
    if not isinstance(entry, _Pairs):
        raise _EntryError("%s is not an object with %s"
                          % (where, ", ".join(KEYS)))
    twice = sorted(key for key, count in Counter(k for k, _v in entry).items()
                   if count > 1)
    if twice:
        raise _EntryError("%s gives %s more than once"
                          % (where, ", ".join(twice)))
    fields = dict(entry)
    missing = [key for key in KEYS if key not in fields]
    if missing:
        raise _EntryError("%s has no %s" % (where, ", ".join(missing)))
    unknown = sorted(set(fields) - set(KEYS))
    if unknown:
        raise _EntryError("%s has unknown keys %s; it takes %s"
                          % (where, ", ".join(unknown), ", ".join(KEYS)))

    layout = fields["layout"]
    if not isinstance(layout, str) or not layout:
        raise _EntryError("%s: layout is not a path" % where)
    if not os.path.isabs(layout):
        raise _EntryError("layout %r in %s is a relative path; the full "
                          "layout is named absolute" % (layout, path))
    if not os.path.exists(layout):
        raise _EntryError("layout %s does not exist" % layout)
    if not os.path.isfile(layout):
        raise _EntryError("layout %s is not a file" % layout)

    top_cell = fields["top_cell"]
    if not isinstance(top_cell, str) or not top_cell:
        raise _EntryError("%s: top_cell is not a cell name" % where)

    return DieLayout(ref, layout, top_cell,
                     _box_dbu(fields["bbox_um"], where))


def _box_dbu(value, where: str) -> Tuple[int, int, int, int]:
    """bbox_um as integer database units at ASSEMBLY_DBU, exactly."""
    if (not isinstance(value, list) or isinstance(value, _Pairs)
            or len(value) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, Decimal))
                   for v in value)):
        raise _EntryError("%s: bbox_um is not four numbers [x_min, y_min, "
                          "x_max, y_max]" % where)
    units = []
    for v in value:
        try:
            scaled = Decimal(v) / ASSEMBLY_DBU
            whole = scaled == scaled.to_integral_value()
        except (InvalidOperation, ValueError):
            whole = False
        if not whole:
            raise _EntryError("%s: bbox_um value %s is not a whole number of "
                              "%s um database units" % (where, v, ASSEMBLY_DBU))
        units.append(int(scaled))
    left, bottom, right, top = units
    if left >= right or bottom >= top:
        raise _EntryError("%s: bbox_um %s is not a box: x_min must be below "
                          "x_max and y_min below y_max"
                          % (where, format_box(tuple(units))))
    return tuple(units)


def format_box(box_dbu: Optional[Tuple[int, int, int, int]]) -> str:
    """``[0, 0, 1000, 999.5]``: a box in um, each value exact."""
    if box_dbu is None:
        return "empty"
    values = []
    for v in box_dbu:
        text = format(Decimal(v) * ASSEMBLY_DBU, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        values.append(text)
    return "[%s] um" % ", ".join(values)


def frame_cause(entry: DieLayout, full: LayoutTops,
                abstract: DieGds) -> Optional[str]:
    """Why ``entry``'s files do not agree with its contract, or None.

    ``full`` is its full layout as read, ``abstract`` the die GDS the
    assembly places (read at the assembly dbu, which the die outline read
    already requires).
    """
    if full.dbu != ASSEMBLY_DBU:
        return ("the full layout %s has a dbu of %s um, not the %s um the "
                "contract box is compared in" % (full.path, full.dbu,
                                                 ASSEMBLY_DBU))
    if entry.top_cell not in full.tops:
        if entry.top_cell in full.cells:
            return ("the full layout %s has the contract's top cell %s, but "
                    "another cell instantiates it, so it is not a top cell"
                    % (full.path, entry.top_cell))
        names = sorted(full.tops)
        shown = ", ".join(names[:6]) + (", ..." if len(names) > 6 else "")
        return ("the full layout %s has no cell %s (the contract's top_cell); "
                "its top cells are %s" % (full.path, entry.top_cell,
                                          shown or "none"))
    full_box = full.tops[entry.top_cell]
    if full_box == entry.bbox_dbu and abstract.box_dbu == entry.bbox_dbu:
        return None

    def verdict(box):
        return "matches" if box == entry.bbox_dbu else "differs"

    return ("the contract's bbox_um %s must be the top cell box of both "
            "files, exactly in %s um database units: the full layout %s (top "
            "cell %s) spans %s, which %s; the abstract %s (top cell %s) spans "
            "%s, which %s"
            % (format_box(entry.bbox_dbu), ASSEMBLY_DBU, full.path,
               entry.top_cell, format_box(full_box), verdict(full_box),
               abstract.path, abstract.top_cell, format_box(abstract.box_dbu),
               verdict(abstract.box_dbu)))
