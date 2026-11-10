# SPDX-License-Identifier: GPL-3.0-or-later
"""Die outlines read from each die's own GDS, for the finalizer.

``hyp_to_gds.py --update-chiplet-file`` sizes a die from one declared
LAYER/DATATYPE of its own layout, never from the footprint courtyard and never
from a bounding box. The read:

- opens the die GDS into a layout of its own, not into the assembly layout,
  whose RenameCell import can rename the die's cells;
- resolves the top cell the way ``GDSGenerator.add_device`` does (the cell no
  other cell of the file instantiates, ``parentless_cells`` below), and
  requires exactly one;
- collects the declared layer hierarchically under that cell and merges it,
  ``db.Region(top.begin_shapes_rec(li)).merged()``;
- accepts exactly one polygon, which must be an axis-aligned rectangle with a
  nonzero width and height;
- refuses a layer whose merge has no area as one cause, whatever the KLayout
  release keeps of it.

The die GDS must have the assembly layout's dbu, 0.001 um. The complete GDS
imports a die into that layout as it is, and a file with another dbu changes
the dbu of the whole layout rather than being rescaled, so the interposer
around the die would change size.

Anything else refuses. Pieces that abut or overlap into one rectangle are one
rectangle. Boxes that touch only at a corner merge into one polygon that is not
a rectangle, and refuse. Texts are not shapes, so a layer that carries only
texts is empty.

The box stays in integer database units of the die's own layout, with that
layout's dbu as a Decimal, so width and height in micrometres are exact
products. Float arithmetic on the same product can land a unit in the last
place away from the value it describes (123457 * 0.001 is 123.45700000000001),
and a reader that compares database units exactly refuses that.

It needs klayout.db, so it runs in the worker, never in KiCad's process.
"""

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import klayout.db as db

# The LAYER/DATATYPE grammar is the footprint field's (die_outline.py), so the
# board field, the export dialog and these flags accept exactly the same text.
if __package__:
    from .die_outline import (  # noqa: F401
        MAX_GDS_NUMBER, _LAYER_TOKEN, format_layer, parse_layer,
    )
else:
    from die_outline import (  # noqa: F401
        MAX_GDS_NUMBER, _LAYER_TOKEN, format_layer, parse_layer,
    )

#: The dbu of the assembly layout hyp_to_gds builds (GDSGenerator: 1 DBU is
#: 1 nm). A die GDS is read and placed only at this dbu.
ASSEMBLY_DBU = Decimal("0.001")

#: Decimal places of the .chiplet's numeric die fields (the intermediate
#: writer's "%.6f"). An outline whose exact size needs more cannot be written
#: exactly and refuses rather than being rounded.
FIELD_DECIMALS = 6


class DieGdsError(Exception):
    """One die GDS, or one layer of it, cannot give what the finalizer needs.

    The message is the cause alone, without the die reference: several dies
    can name one file, and each of them refuses with the same words.
    """


class DieOutlineRefused(Exception):
    """One or more dies cannot be sized from a declared outline.

    Nothing may be written once this is raised. Deliberately not a
    ``ValueError``, for the reason ``hyp_to_gds.SourceRefused`` gives: a
    handler written for a malformed value must not be able to absorb it.
    ``offenders`` lists every (reference, cause) pair found, in the order they
    were found.
    """

    def __init__(self, offenders: Sequence[Tuple[str, str]]):
        self.offenders = list(offenders)
        super().__init__("\n".join(self.lines()))

    def lines(self) -> List[str]:
        return [refusal_line(ref, cause) for ref, cause in self.offenders]


def refusal_line(reference: str, cause: str) -> str:
    """The one log line a refused die gets."""
    return "Die outline refused: %s: %s" % (reference or '""', cause)


def dbu_mismatch(path: str, dbu: Decimal,
                 assembly_dbu: Decimal = ASSEMBLY_DBU) -> str:
    """The cause given for a die GDS whose dbu is not the assembly's."""
    return ("GDS %s has a dbu of %s um, not the %s um of the assembly "
            "layout; a die is imported without rescaling, so it would change "
            "the scale of the complete GDS" % (path, dbu, assembly_dbu))


def parse_outline_layers(spec: str,
                         flag: str = "--die-outline-layers"
                         ) -> Dict[str, Tuple[int, int]]:
    """``"U1=39/4,U2=206/0"`` -> ``{"U1": (39, 4), "U2": (206, 0)}``.

    Strict, unlike --die-thicknesses: an empty token, an empty reference, a
    value that is not LAYER/DATATYPE, or a reference given twice raises
    ValueError naming the offending token.
    """
    out: Dict[str, Tuple[int, int]] = {}
    for token in spec.split(","):
        ref, sep, value = token.partition("=")
        ref, value = ref.strip(), value.strip()
        if not sep or not ref:
            raise ValueError(
                "Invalid %s token %r: expected REF=LAYER/DATATYPE, for "
                "example U1=39/4." % (flag, token))
        try:
            layer = parse_layer(value)
        except ValueError:
            raise ValueError(
                "Invalid die outline layer %r for %r in %s: expected "
                "LAYER/DATATYPE, two integers from 0 to %d, for example "
                "39/4." % (value, ref, flag, MAX_GDS_NUMBER)) from None
        if ref in out:
            raise ValueError("%s names %r twice." % (flag, ref))
        out[ref] = layer
    return out


def parse_outline_exempt(spec: str,
                         flag: str = "--die-outline-exempt") -> List[str]:
    """``"U3,U4"`` -> ``["U3", "U4"]``, as strictly as the layer flag.

    A token with ``=`` is refused: it reads as a layer declaration given to
    the wrong flag, and taking it as a reference would exempt a die nobody
    named.
    """
    out: List[str] = []
    for token in spec.split(","):
        ref = token.strip()
        if not ref or "=" in ref:
            raise ValueError(
                "Invalid %s token %r: expected a die reference, for example "
                "U3." % (flag, token))
        if ref in out:
            raise ValueError("%s names %r twice." % (flag, ref))
        out.append(ref)
    return out


def parentless_cells(cells: Iterable["db.Cell"]) -> List["db.Cell"]:
    """The cells of ``cells`` that no other cell of ``cells`` instantiates.

    In the order given. ``add_device`` takes the first of these as an imported
    die's top cell; the outline read requires there to be exactly one. Both
    ask this one function, so they cannot name different cells.
    """
    cells = list(cells)
    indexes = {cell.cell_index() for cell in cells}
    return [cell for cell in cells
            if not any(parent in indexes for parent in cell.each_parent_cell())]


def exact_float(value: Decimal) -> float:
    """``value`` as the float whose shortest repr is ``value`` itself.

    That is the float yaml.dump writes back as exactly ``value``. ValueError
    when there is none, or when ``value`` needs more than FIELD_DECIMALS
    decimal places.
    """
    as_float = float(value)
    if (value.normalize().as_tuple().exponent < -FIELD_DECIMALS
            or Decimal(repr(as_float)) != value):
        raise ValueError(str(value))
    return as_float


@dataclass(frozen=True)
class DieGds:
    """One die GDS as read: the file, its one top cell and its dbu."""
    path: str        # the realpath actually read
    top_cell: str
    dbu: Decimal     # Decimal(repr(layout.dbu)), um per database unit
    # The top cell's bbox in database units (left, bottom, right, top); None
    # when the cell draws nothing.
    box_dbu: Optional[Tuple[int, int, int, int]] = None


@dataclass(frozen=True)
class LayoutTops:
    """A GDS read for its top cells alone, however many it has.

    For a die's full layout, which is never composed, so a second top cell
    is no reason to refuse it: its top cell is named, not resolved.
    """
    path: str        # the realpath actually read
    dbu: Decimal
    # {top cell name: bbox in database units, None when the cell is empty}
    tops: Dict[str, Optional[Tuple[int, int, int, int]]]
    cells: frozenset  # the name of every cell in the file


@dataclass(frozen=True)
class DieOutline:
    """One die outline: an integer box in the die top cell's own frame.

    Unrotated and unmirrored; the placement carries rotation and mirroring.
    """
    gds: DieGds
    layer: Tuple[int, int]
    box_dbu: Tuple[int, int, int, int]   # left, bottom, right, top

    @property
    def width_um(self) -> Decimal:
        return (self.box_dbu[2] - self.box_dbu[0]) * self.gds.dbu

    @property
    def height_um(self) -> Decimal:
        return (self.box_dbu[3] - self.box_dbu[1]) * self.gds.dbu

    def box_um(self) -> "db.DBox":
        """The box in micrometres, each corner from its exact product."""
        left, bottom, right, top = (float(v * self.gds.dbu)
                                    for v in self.box_dbu)
        return db.DBox(left, bottom, right, top)


@dataclass
class DieOutlinePlan:
    """What the finalizer does with each die: size it, or leave it exempt."""
    outlines: Dict[str, DieOutline] = field(default_factory=dict)
    exempt: Dict[str, DieGds] = field(default_factory=dict)

    def gds(self, reference: str) -> Optional[DieGds]:
        """The GDS read for ``reference``; None for a die the plan leaves out
        (an io pad stand-in, see resolve_die_outlines)."""
        outline = self.outlines.get(reference)
        return (outline.gds if outline is not None
                else self.exempt.get(reference))


def _box_dbu(cell) -> Optional[Tuple[int, int, int, int]]:
    box = cell.bbox()
    return None if box.empty() else (box.left, box.bottom, box.right,
                                     box.top)


def _read(real: str):
    """Read ``real`` into a fresh layout, or say why it cannot be."""
    if not os.path.exists(real):
        raise DieGdsError("GDS %s does not exist" % real)
    if not os.path.isfile(real):
        raise DieGdsError("GDS %s is not a file" % real)
    layout = db.Layout()
    try:
        layout.read(real)
    except Exception as exc:  # KLayout raises RuntimeError for a bad stream.
        raise DieGdsError("cannot read GDS %s: %s" % (real, exc)) from None
    return layout


def _open_tops(real: str) -> LayoutTops:
    """Read ``real`` for its dbu and its top cells' boxes; any dbu."""
    layout = _read(real)
    cells = list(layout.each_cell())
    return LayoutTops(real, Decimal(repr(layout.dbu)),
                      {cell.name: _box_dbu(cell)
                       for cell in parentless_cells(cells)},
                      frozenset(cell.name for cell in cells))


def _open(real: str, dbu: Optional[Decimal]):
    """Read ``real`` into a fresh layout; (layout, top cell, DieGds).

    ``dbu`` is the one the file must have; None accepts any. Every cause
    names ``real``, the resolved path, since several spellings of one file
    share what is read from it.
    """
    layout = _read(real)
    file_dbu = Decimal(repr(layout.dbu))
    if dbu is not None and file_dbu != dbu:
        raise DieGdsError(dbu_mismatch(real, file_dbu, dbu))
    tops = parentless_cells(layout.each_cell())
    if not tops:
        raise DieGdsError("GDS %s holds no cell" % real)
    if len(tops) > 1:
        names = [cell.name for cell in tops]
        shown = ", ".join(names[:6]) + (", ..." if len(names) > 6 else "")
        raise DieGdsError(
            "GDS %s has %d top cells (%s); exactly one top cell is required"
            % (real, len(names), shown))
    top = tops[0]
    return layout, top, DieGds(real, top.name, file_dbu, _box_dbu(top))


def _read_outline(layout, top, gds: DieGds,
                  layer: Tuple[int, int]) -> DieOutline:
    name = format_layer(layer)
    where = "under top cell %s in %s" % (gds.top_cell, gds.path)
    # find_layer, never layer(): layer() creates an absent layer, which would
    # turn "not in this file" into "in this file and empty".
    index = layout.find_layer(*layer)
    if index is None:
        raise DieGdsError(
            "die outline layer %s is not in %s; declare the layer this GDS "
            "draws its die outline on" % (name, gds.path))
    raw = db.Region(top.begin_shapes_rec(index))
    polygons = list(raw.merged().each())
    # Area is judged on the merged polygons, not on the shapes drawn: a
    # self-intersecting polygon reports no area of its own and merges to real
    # area. A layer whose merge has none is one refusal, since what merged()
    # makes of it moved between releases (0.30.5 keeps a lone zero-area shape
    # as a degenerate box, 0.30.12 drops it).
    if all(polygon.area() == 0 for polygon in polygons):
        # The shapes drawn, not is_empty(): over a layer of texts only the
        # region is not empty and yields no shape.
        drawn = db.Box()
        for shape in raw.each():
            drawn += shape.bbox()
        if drawn.empty():
            raise DieGdsError(
                "die outline layer %s carries no shape %s (texts do not count)"
                % (name, where))
        raise DieGdsError(
            "die outline layer %s carries only shapes of zero area (%d x %d "
            "DBU across them) %s" % (name, drawn.width(), drawn.height(), where))
    if len(polygons) > 1:
        raise DieGdsError(
            "die outline layer %s merges to %d separate shapes %s; the die "
            "outline must be one rectangle and which one is the die is not "
            "decidable. No bounding box is substituted."
            % (name, len(polygons), where))
    polygon = polygons[0]
    if not polygon.is_box():
        detail = "%d vertices, %d holes" % (polygon.num_points_hull(),
                                            polygon.holes())
        if not polygon.is_rectilinear():
            detail += ", not Manhattan"
        raise DieGdsError(
            "die outline layer %s merges to one shape that is not a rectangle "
            "(%s) %s; width and height need one axis-aligned rectangle. No "
            "bounding box is substituted." % (name, detail, where))
    box = polygon.bbox()
    outline = DieOutline(gds, tuple(layer),
                         (box.left, box.bottom, box.right, box.top))
    for size in (outline.width_um, outline.height_um):
        try:
            exact_float(size)
        except ValueError:
            raise DieGdsError(
                "die outline layer %s gives %s x %s um (dbu %s) %s, which the "
                ".chiplet cannot carry exactly in %d decimal places"
                % (name, outline.width_um, outline.height_um, gds.dbu, where,
                   FIELD_DECIMALS)) from None
    return outline


class DieGdsReader:
    """One run's die GDS reads, memoized.

    A file is opened at most once however many dies name it; its top cell is
    resolved once, and each outline is kept by (realpath, top cell, layer,
    datatype), so two dies on one file and one layer share one read. A file or
    layer that cannot be read keeps its cause, so every die naming it refuses
    with the same words without a second attempt. One reader per run: the
    finalizer's head reads through it, and the .chiplet update and
    ``add_device`` reuse what it holds.

    ``dbu`` is the dbu every file must have (a cause otherwise); None accepts
    any, for a GDS that is only asked for its top cell. It does not apply to
    ``tops``, a full layout's read, whose dbu the caller compares itself.
    """

    def __init__(self, dbu: Optional[Decimal] = ASSEMBLY_DBU):
        self._dbu = dbu
        self._gds: Dict[str, object] = {}        # realpath -> DieGds | cause
        self._outlines: Dict[tuple, object] = {}  # key -> DieOutline | cause
        self._tops: Dict[str, object] = {}       # realpath -> LayoutTops | cause

    def tops(self, path: str) -> LayoutTops:
        """``path`` read once for its top cells, whatever their number."""
        real = os.path.realpath(path)
        if real not in self._tops:
            try:
                self._tops[real] = _open_tops(real)
            except DieGdsError as exc:
                self._tops[real] = str(exc)
        known = self._tops[real]
        if isinstance(known, str):
            raise DieGdsError(known)
        return known

    def load(self, path: str,
             layers: Iterable[Tuple[int, int]] = ()) -> None:
        """Open ``path`` once and read every layer of ``layers`` not read yet."""
        layers = [tuple(layer) for layer in layers]
        real = os.path.realpath(path)
        known = self._gds.get(real)
        if isinstance(known, str):
            return
        if known is not None and all(
                (real, known.top_cell) + layer in self._outlines
                for layer in layers):
            return
        try:
            layout, top, gds = _open(real, self._dbu)
        except DieGdsError as exc:
            self._gds[real] = str(exc)
            return
        self._gds[real] = gds
        for layer in layers:
            key = (real, gds.top_cell) + layer
            if key in self._outlines:
                continue
            try:
                self._outlines[key] = _read_outline(layout, top, gds, layer)
            except DieGdsError as exc:
                self._outlines[key] = str(exc)

    def gds(self, path: str) -> DieGds:
        self.load(path)
        return self._loaded_gds(path)

    def outline(self, path: str, layer: Tuple[int, int]) -> DieOutline:
        self.load(path, [layer])
        gds = self._loaded_gds(path)
        known = self._outlines[(gds.path, gds.top_cell) + tuple(layer)]
        if isinstance(known, str):
            raise DieGdsError(known)
        return known

    def _loaded_gds(self, path: str) -> DieGds:
        known = self._gds[os.path.realpath(path)]
        if isinstance(known, str):
            raise DieGdsError(known)
        return known


def read_top_cell(path: str) -> str:
    """The one top cell of ``path``, resolved as a die's is; DieGdsError else.

    Any dbu: this is the interposer GDS's top cell, not a die read.
    """
    return DieGdsReader(dbu=None).gds(path).top_cell
