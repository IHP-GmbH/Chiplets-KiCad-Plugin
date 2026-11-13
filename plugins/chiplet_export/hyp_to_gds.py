#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
HYP to GDS Converter

Converts KiCad HYP (Hyperlynx) files to GDSII format.
Extracts trace segments from HYP and generates polygons on appropriate PDK layers.
"""

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import warnings
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple
from urllib.parse import quote

try:
    from .carrier_profile import check_anchor_drawable, resolve_carrier_profile
    from .connection_methods import resolve_die_methods
except ImportError:  # Standalone worker script, outside the GUI package.
    from carrier_profile import check_anchor_drawable, resolve_carrier_profile
    from connection_methods import resolve_die_methods

try:
    from . import layer_map as _layer_map
    from .output_files import create_output_temp
except ImportError:  # Standalone worker script.
    import layer_map as _layer_map
    from output_files import create_output_temp

try:
    import klayout.db as db
except ImportError:
    print("Error: KLayout Python module not found.", file=sys.stderr)
    print("Please install with: pip install klayout", file=sys.stderr)
    sys.exit(1)

# After the klayout guard, since this module imports klayout.db itself.
try:
    from . import die_outline_gds as _die_outline
except ImportError:  # Standalone worker script.
    import die_outline_gds as _die_outline
DieGdsError = _die_outline.DieGdsError
DieOutlineRefused = _die_outline.DieOutlineRefused
try:
    from . import die_layouts as _die_layouts
except ImportError:  # Standalone worker script.
    import die_layouts as _die_layouts
DieLayoutRefused = _die_layouts.DieLayoutRefused
try:
    from . import technology_overlay as _technology_overlay
except ImportError:  # Standalone worker script.
    import technology_overlay as _technology_overlay
TechnologyRefused = _technology_overlay.TechnologyRefused


def _vendored_cfio():
    """Import the vendored ``chiplet_format_io`` (the single guarded read/write
    path for ``.chiplet``). Kept lazy and path-robust so this worker script
    resolves it whether run as ``python hyp_to_gds.py`` or imported. Fixes in the
    reader belong upstream in chiplet-spec and come back as a re-vendor; do not
    edit ``vendor/chiplet_format_io`` in place."""
    vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    import chiplet_format_io as cfio
    return cfio


def _chiplet_merge():
    """Import the GUI tier's top-level block grammar, BY FILE PATH.

    By path and not as a package: importing ``chiplet_export.pipeline...`` runs
    ``chiplet_export/__init__.py``, which registers the KiCad action plugin
    whenever pcbnew is importable, so a CLI conversion would grow a side effect.
    The module is stdlib-only, so the worker inherits nothing from it.

    Imported and not reimplemented: the check below asks whether the key list
    the ownership filter USED matches the parser's. A second implementation
    that happens to agree with PyYAML would answer a different question.
    """
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "pipeline", "chiplet_merge.py")
    spec = importlib.util.spec_from_file_location("_chiplet_merge_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _key_multiset_difference(left, right):
    """Keys in ``left`` that ``right`` does not also have, counting repeats."""
    rest = list(right)
    out = []
    for key in left:
        if key in rest:
            rest.remove(key)
        else:
            out.append(key)
    return out


def chiplet_top_level_key_parity_error(chiplet_path: str) -> Optional[str]:
    """None when both parsers see the same top-level keys; else a message.

    The second net under the GUI-tier layout refusal, and not a superset of it:
    a quoted duplicate of an OWNED key passes this check (both parsers see one
    ``components``) and is caught upstream, while a phantom key the grammar
    invented inside a multi-line scalar passes upstream and is caught only
    here. Each closes one direction of the same disagreement.
    """
    import yaml
    merge = _chiplet_merge()
    try:
        text = merge.read_document(chiplet_path)
    except (OSError, UnicodeDecodeError) as exc:
        return f"{chiplet_path} cannot be read as UTF-8 text: {exc}"
    # compose, not safe_load. safe_load builds a dict, and a dict COLLAPSES a
    # repeated top-level key to one entry, which is exactly the shape being
    # checked for: a break character PyYAML reads as a line break but the LF
    # grammar reads as content lets a second `components:` ride inside a
    # carried foreign block, and after collapsing, both sides report one
    # `components` and this check says AGREE. Measured: with safe_load all four
    # break characters (CR, NEL, U+2028, U+2029) passed. compose keeps the node
    # tree, so duplicates survive to be compared.
    try:
        node = yaml.compose(text)
    except yaml.YAMLError as exc:
        return (f"{chiplet_path} is not valid YAML, so the block ownership "
                f"decided during export cannot be checked.\n  {exc}")
    if node is None or not isinstance(node, yaml.MappingNode):
        return (f"{chiplet_path} has no mapping at the top level, so it has no "
                f"top-level blocks to own.")
    document = [key.value for key, _value in node.value]

    # compose does not resolve scalars to Python objects, so the non-string
    # key check has to read the resolved TAG rather than the value's type:
    # `on:` composes to the string "on" carrying the bool tag, and it is the
    # tag that tells us the grammar and the parser disagree about the key's
    # identity.
    _STR_TAG = "tag:yaml.org,2002:str"
    unreadable = [(key.value, getattr(key, "tag", _STR_TAG))
                  for key, _v in node.value
                  if getattr(key, "tag", _STR_TAG) != _STR_TAG]
    if unreadable:
        # Name the resolved type, not just the key: "on" reads as a bool and
        # "9" as an int, and a user who cannot see WHICH type has no way to
        # know why quoting the key is the fix.
        shown = ", ".join(f"{v!r} ({t.rsplit(':', 1)[-1]})" for v, t in unreadable)
        return (f"{chiplet_path} has a top-level key YAML does not read as "
                f"text: {shown}. The grammar and the parser disagree about "
                f"that key's identity, so ownership cannot be decided. Quote "
                f"the key.")

    grammar = sorted(merge.top_level_key_lines(text))
    parser = sorted(document)
    if grammar != parser:
        only_grammar = _key_multiset_difference(grammar, parser)
        only_parser = _key_multiset_difference(parser, grammar)
        lines = [
            f"{chiplet_path}: the text grammar and the YAML parser disagree "
            f"about the top-level keys, so the block ownership decided during "
            f"export cannot be trusted. No GDS was written.",
            f"  grammar sees: {', '.join(grammar) or '(none)'}",
            f"  parser sees : {', '.join(parser) or '(none)'}",
        ]
        if only_grammar:
            lines.append(f"  only in the grammar: {', '.join(only_grammar)}")
        if only_parser:
            lines.append(f"  only in the parser : {', '.join(only_parser)}")
        return "\n".join(lines)
    return None


# Chiplet mechanical boundaries are ADK assembly metadata, not fabrication
# geometry. They are emitted to a <gds>.boundaries.json manifest (see
# GDSGenerator._write_boundary_manifest) and live in NO PDK layer namespace,
# so the assembly contract is independent of any process's layer numbers.
# This replaces the historical practice of stamping the boundary on the
# exchange0 layer (190/0), which is a real IHP SG13G2 fab layer.
#
# For human inspection only, --annotate-boundaries paints the same polygons
# (plus an instance label) onto an annotation GDS layer (default 1000/0). That
# layer is read by NO DRC rule and carries no contract: it cannot produce a
# false "0 violations" nor alias a fab layer. It is opt-in and off by default,
# so the production GDS stays free of synthetic geometry.


# Manufacturing grid of the IntM4TM2 interposer process, in nanometers.
#
# The interposer PDK's own deck is the authority: rule_decks/3_1_offgrid.drc
# defines GRID = 5.nm and runs in the default deck set, so every vertex this
# exporter draws has to sit on it or the carrier fails its foundry DRC. The
# value is duplicated here rather than parsed out of a Ruby deck at import
# time; tests/test_manufacturing_grid.py pins the two together and fails if
# the deck moves.
#
# The companion constraint comes from rule_decks/3_2_angle.drc: Metal4,
# Metal5, TopMetal1 and TopMetal2 accept only 0/45/90 degree edges. On-grid
# and exactly-45 cannot both hold for the *outline* of a diagonal wire of an
# arbitrary width (the perpendicular offset is width/(2*sqrt(2)), irrational
# for any round width), which is why traces are emitted fractured, one
# on-grid quad per segment, instead of as one path polygon. Both angle and
# offgrid rules read the raw, as-drawn polygons, so each quad is judged on
# its own and the merged copper is unaffected.
MANUFACTURING_GRID_NM = 5


class SealRingRefused(Exception):
    """The seal ring was asked for and cannot be drawn, or cannot be kept.

    Raised before the GDS is written, never after: an interposer that was
    asked for a ring and ships without one, or with metal crowding it, is not
    a result to hand on. The message is the reason, one line per cause.
    """


class TrackEndRefused(Exception):
    """Two nets' traces touch in the GDS, in practice at a square track end.

    Raised before the GDS is written. The message is the reason, then one line
    per place, then the advice.
    """


class IntM4TM2Refused(RuntimeError):
    """An IntM4TM2 PCell would run on a cni API that is not the interposer's.

    A RuntimeError, so the pad and pillar paths stop the export the way any
    other PCell failure does; the seal ring and CMIM paths turn it into
    their own refusal.
    """


# Both PCell libraries run on the pycell4klayout-api, and each PDK ships its
# own copy under the same top-level names. Python keeps one module per name,
# so the PDK imported first would serve both, and PCellWrapper keeps its Tcl
# interpreter, callback map and tech at class level. SG13G2 is imported here
# only because IntM4TM2 ships no via_stack for the interposer vias; once the
# interposer PDK has one, SG13G2 is no longer loaded and this scope goes away.
_CNI_PACKAGES = ("cni", "pypreprocessor")
_intm4tm2_modules: Dict[str, object] = {}
_intm4tm2_scope_depth = 0


def _cni_module(name: str, module: object) -> bool:
    """True for a sys.modules entry both PDKs register under the same name:
    the cni API, its preprocessor, and a preprocessed PCell module, which
    either library registers top-level as '<pcell>_code'."""
    if name.split(".", 1)[0] in _CNI_PACKAGES:
        return True
    return ("." not in name and name.endswith("_code")
            and str(getattr(module, "__file__", "")).endswith("_pre.py"))


def _cni_portion(entry: str) -> bool:
    """True when this sys.path entry holds a namespace portion of a cni
    package. Portions merge across sys.path, so a foreign one is not shadowed
    by putting the interposer's first; it has to leave the path."""
    for name in _CNI_PACKAGES:
        package = os.path.join(entry or os.curdir, name)
        if (os.path.isdir(package)
                and not os.path.isfile(os.path.join(package, "__init__.py"))):
            return True
    return False


@contextlib.contextmanager
def _intm4tm2_scope():
    """Run the enclosed IntM4TM2 PCell work on the interposer PDK's cni.

    On entry the cni modules already loaded (SG13G2's) are set aside, the
    interposer's python dirs go first on sys.path with every other cni
    portion removed, and the interposer's own cni modules from an earlier
    entry come back. On exit, refusals included, it is the reverse, and
    sys.path is restored exactly. Re-entrant: a nested scope is a no-op.
    """
    global _intm4tm2_scope_depth
    if _intm4tm2_scope_depth:
        yield
        return
    _intm4tm2_scope_depth = 1
    saved_path = list(sys.path)
    foreign = {}
    try:
        for name, module in list(sys.modules.items()):
            if _cni_module(name, module):
                foreign[name] = sys.modules.pop(name)
        root = _discover_path_var("INTERPOSER_PDK_ROOT")
        if root:
            python_dir = Path(root) / "libs.tech" / "klayout" / "python"
            api_dir = python_dir / "pycell4klayout-api" / "source" / "python"
            ours = [str(python_dir), str(api_dir)]
            sys.path[:] = ours + [entry for entry in saved_path
                                  if entry not in ours
                                  and not _cni_portion(entry)]
        sys.modules.update(_intm4tm2_modules)
        yield
    finally:
        for name, module in list(sys.modules.items()):
            if _cni_module(name, module):
                _intm4tm2_modules[name] = sys.modules.pop(name)
        sys.modules.update(foreign)
        sys.path[:] = saved_path
        _intm4tm2_scope_depth = 0


def _check_intm4tm2_cni(pcell: str, library: str = "IntM4TM2") -> None:
    """Refuse an IntM4TM2 PCell whose library runs on a foreign cni.

    Called inside _intm4tm2_scope, once the library is registered and before
    `pcell` is created. The declaration must be an instance of the
    PCellWrapper of the cni installed in the scope, loaded from the
    interposer PDK. Its file comes from the code object, since both copies
    are named cni.dlo. The usual cause is a library registered earlier in
    the process, outside the scope. Only cni declarations are checked.
    """
    lib = db.Library.library_by_name(library, "intm4tm2")
    decl = lib.layout().pcell_declaration(pcell) if lib is not None else None
    cls = type(decl)
    if (cls.__module__, cls.__name__) != ("cni.dlo", "PCellWrapper"):
        return
    code = getattr(vars(cls).get("produce"), "__code__", None)
    declared = code.co_filename if code is not None else None
    installed = sys.modules.get("cni.dlo")
    root = _discover_path_var("INTERPOSER_PDK_ROOT")
    if root and declared and getattr(installed, "PCellWrapper", None) is cls:
        root = os.path.realpath(root)
        try:
            common = os.path.commonpath([root, os.path.realpath(declared)])
        except ValueError:  # different drives
            common = None
        if common == root:
            return
    raise IntM4TM2Refused(
        "the IntM4TM2 %s PCell would run on a cni API from outside the "
        "interposer PDK at %s: its library was registered with %s, and the "
        "cni in use is %s. Something registered IntM4TM2 before this export "
        "did; run the export in a fresh process."
        % (pcell, root, declared, getattr(installed, "__file__", None)))


@dataclass
class TraceSegment:
    """Represents a single trace segment from the HYP file."""
    net_name: str
    x1: float  # inches
    y1: float  # inches
    x2: float  # inches
    y2: float  # inches
    width: float  # inches
    layer: str  # e.g., "Metal4", "TopMetal1"


@dataclass
class TraceArc:
    """Represents a trace arc (curve) from the HYP file."""
    net_name: str
    x1: float  # start point (inches)
    y1: float
    x2: float  # end point (inches)
    y2: float
    xc: float  # center point (inches)
    yc: float
    radius: float  # inches
    width: float  # inches
    layer: str


@dataclass
class ChainedTrace:
    """One chained centreline of a net on a layer, as the exporter draws it.

    ``free`` says which of its two ends get the w/2 square end; see
    free_ends.
    """
    layer: str
    net: str
    width_um: float
    points: List[Tuple[float, float]]  # micrometres
    free: Tuple[bool, bool] = (True, True)


def _on_segment(p: Tuple[int, int], a: Tuple[int, int],
                b: Tuple[int, int]) -> bool:
    """Whether p is within one unit of the segment a-b, its ends included.

    Integer coordinates; |cross| / length is the distance off the line and
    dot / length the position along it.
    """
    ux, uy = b[0] - a[0], b[1] - a[1]
    vx, vy = p[0] - a[0], p[1] - a[1]
    len2 = ux * ux + uy * uy
    if len2 == 0:
        return abs(vx) <= 1 and abs(vy) <= 1
    cross, dot = ux * vy - uy * vx, ux * vx + uy * vy
    reach = math.isqrt(len2)
    return cross * cross <= len2 and -reach <= dot <= len2 + reach


def free_ends(traces: List[ChainedTrace],
              dbu: float) -> List[Tuple[bool, bool]]:
    """Which ends of each trace of one net on one layer are free.

    An end is not free when another of these traces runs through it: one
    wider, or one as wide that passes the end point rather than ending on
    it. That trace's centreline runs through the point at least as wide, so
    the board's round cap there is already its copper, and the end is drawn
    as it is, without the w/2 square end. Extending it would only push square
    copper out of the corner it lands in, and since two traces snapped from
    different starts can reach the same board point a grid step apart, the
    edges of that copper would cross the other trace's flank off the grid.

    Two ends of the same width meeting each other stay free, as do the two
    ends of a closed loop: nothing passes through those points, so the
    square ends are what covers them.

    "Runs through" is judged on the board's coordinates, rounded to ``dbu``,
    within one unit, so it does not depend on how either trace was snapped.
    Cost: each end is tested against every trace of its net whose bounding
    box holds it, so the work grows with the square of the traces per net
    and layer when they overlap; a few thousand traces take under a second.
    """
    lines = [[(int(round(x / dbu)), int(round(y / dbu))) for x, y in t.points]
             for t in traces]
    boxes = [(min(x for x, _ in line) - 1, min(y for _, y in line) - 1,
              max(x for x, _ in line) + 1, max(y for _, y in line) + 1)
             for line in lines]

    def near(p, q) -> bool:
        return abs(p[0] - q[0]) <= 1 and abs(p[1] - q[1]) <= 1

    def covered(i: int, end: int) -> bool:
        p, width = lines[i][end], traces[i].width_um
        for j, line in enumerate(lines):
            if j == i or traces[j].width_um < width:
                continue
            x0, y0, x1, y1 = boxes[j]
            if not (x0 <= p[0] <= x1 and y0 <= p[1] <= y1):
                continue
            if traces[j].width_um == width and (near(p, line[0])
                                                or near(p, line[-1])):
                continue
            if any(_on_segment(p, a, b) for a, b in zip(line, line[1:])):
                return True
        return False

    return [(not covered(i, 0), not covered(i, -1))
            for i in range(len(traces))]


@dataclass
class PerimeterSegment:
    """One BOARD-section PERIMETER_SEGMENT (the board outline / Edge.Cuts)."""
    x1: float  # HYP units (inches or meters)
    y1: float
    x2: float
    y2: float


@dataclass
class Padstack:
    """Represents a via padstack definition from HYP file."""
    index: int
    drill: float  # inches (drill diameter)
    layers: List[str]  # List of layer names from top to bottom
    pad_width: float = 0.0  # inches (pad size from first layer)
    pad_height: float = 0.0  # inches (pad size from first layer)


@dataclass
class Via:
    """Represents a via instance from HYP file."""
    net_name: str
    x: float  # inches
    y: float  # inches
    padstack_index: int


@dataclass
class Pin:
    """Represents a pin from HYP file (used for device positioning)."""
    x: float  # Position in HYP units (inches or meters)
    y: float
    ref: str  # Reference designator (e.g., "REF**.1")
    padstack_index: int


@dataclass
class Device:
    """Represents a device/chiplet from HYP DEVICES section."""
    ref: str           # Reference designator (e.g., "REF**")
    layer: str         # Layer name (e.g., "TopMetal2")
    gds_file: str      # Path to external GDS file
    x: float = 0.0     # Position (calculated from PIN centroid)
    y: float = 0.0
    rotation: float = 0.0


class LayerMap(_layer_map.LayerMap):
    """Worker error reporting around the shared, stdlib-only LYP reader."""

    def __init__(self, lyp_path: str):
        try:
            super().__init__(lyp_path)
        except _layer_map.LayerMapError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)


class HYPParser:
    """Parses KiCad HYP (Hyperlynx) files."""

    # Regex patterns for parsing (support negative values and scientific notation)
    SEG_PATTERN = re.compile(
        r'\(SEG\s+X1=(-?[\d.eE+-]+)\s+Y1=(-?[\d.eE+-]+)\s+X2=(-?[\d.eE+-]+)\s+Y2=(-?[\d.eE+-]+)\s+'
        r'W=(-?[\d.eE+-]+)\s+L="([^"]+)"\)'
    )
    ARC_PATTERN = re.compile(
        r'\(ARC\s+X1=(-?[\d.eE+-]+)\s+Y1=(-?[\d.eE+-]+)\s+X2=(-?[\d.eE+-]+)\s+Y2=(-?[\d.eE+-]+)\s+'
        r'XC=(-?[\d.eE+-]+)\s+YC=(-?[\d.eE+-]+)\s+R=(-?[\d.eE+-]+)\s+W=(-?[\d.eE+-]+)\s+L="([^"]+)"\)'
    )
    NET_PATTERN = re.compile(r'\{NET="([^"]+)"')
    VIA_PATTERN = re.compile(r'\(VIA\s+X=(-?[\d.eE+-]+)\s+Y=(-?[\d.eE+-]+)\s+P=(\d+)\)')
    PADSTACK_PATTERN = re.compile(r'\{PADSTACK=(\d+),\s*(-?[\d.eE+-]+)')
    # Pattern to extract layer name, width, and height from padstack layer entry
    # Format: ("LayerName", type, width, height, rotation, material)
    PADSTACK_LAYER_PATTERN = re.compile(r'\("([^"]+)",\s*\d+,\s*(-?[\d.eE+-]+),\s*(-?[\d.eE+-]+)')

    # Pattern for DEVICES with GDS_FILE (new format with position)
    # Format: (? REF="U1" L="Top" X=0.005000000 Y=0.010000000 R=90.00 GDS_FILE="/path.gds")
    DEVICE_PATTERN = re.compile(
        r'\(\?\s+REF="([^"]+)"\s+L="([^"]+)"\s+'
        r'X=(-?[\d.eE+-]+)\s+Y=(-?[\d.eE+-]+)\s+'
        r'R=(-?[\d.eE+-]+)\s+'
        r'GDS_FILE="([^"]+)"\)'
    )
    # Groups: 1=REF, 2=Layer, 3=X, 4=Y, 5=R, 6=GDS_FILE

    # Pattern for PINs that reference devices
    # Format: (PIN X=0.158... Y=-0.101... R="REF**.1" P=0)
    PIN_PATTERN = re.compile(
        r'\(PIN\s+X=(-?[\d.eE+-]+)\s+Y=(-?[\d.eE+-]+)\s+R="([^"]+)"\s+P=(\d+)\)'
    )

    # Board outline from the {BOARD section (KiCad exports Edge.Cuts here).
    # PERIMETER_SEGMENT appears only inside {BOARD per the HYP spec, so a
    # global scan is safe. PERIMETER_ARC is not supported (KiCad polygonizes
    # the outline before export); its presence is counted and warned about.
    PERIMETER_SEGMENT_PATTERN = re.compile(
        r'\(PERIMETER_SEGMENT\s+X1=(-?[\d.eE+-]+)\s+Y1=(-?[\d.eE+-]+)\s+'
        r'X2=(-?[\d.eE+-]+)\s+Y2=(-?[\d.eE+-]+)\)'
    )
    PERIMETER_ARC_PATTERN = re.compile(r'\(PERIMETER_ARC\b')

    # The {BOARD "<path>"} header records the source board file (KiCad
    # board.GetFileName()); its directory is the base for resolving a
    # board-relative die GDS_FILE (see _parse_devices).
    BOARD_HEADER_PATTERN = re.compile(r'\{BOARD\s+"([^"]*)"')

    def __init__(self, hyp_path: str):
        self.hyp_path = hyp_path
        self.segments: List[TraceSegment] = []
        self.arcs: List[TraceArc] = []
        self.vias: List[Via] = []
        self.padstacks: Dict[int, Padstack] = {}
        self.devices: List[Device] = []
        self.pins: List[Pin] = []
        self.units: str = "ENGLISH"  # Default to inches
        self.stackup_layers: List[str] = []  # Layer order from STACKUP (top to bottom)
        self.perimeter_segments: List[PerimeterSegment] = []  # board outline
        self.board_path: str = ""  # source board file from the {BOARD "..."} header

    def parse(self) -> None:
        """Parse the HYP file and extract all geometry."""
        try:
            with open(self.hyp_path, 'r') as f:
                content = f.read()
        except FileNotFoundError:
            print(f"Error: Could not find {self.hyp_path}", file=sys.stderr)
            sys.exit(1)

        try:
            self._parse_units(content)
            self._parse_stackup(content)
            self._parse_board_perimeter(content)
            self._parse_board_path(content)
            self._parse_padstacks(content)
            self._parse_devices(content)
            self._parse_segments_and_vias(content)
        except (ValueError, IndexError) as exc:
            print(f"Error: malformed HYP data in {self.hyp_path}: {exc}",
                  file=sys.stderr)
            sys.exit(1)
        # Device positions now come directly from HYP file (no centroid calculation needed)

    def _parse_units(self, content: str) -> None:
        """Parse the {UNITS=...} header declaration.

        Anchored to the section-header token so an ENGLISH/METRIC substring
        elsewhere in the file cannot flip the units (an ENGLISH false match on
        a METRIC board scales every coordinate by 25400x).
        """
        m = re.search(r'\{UNITS=(ENGLISH|METRIC)\b', content)
        if m:
            self.units = m.group(1)  # ENGLISH = inches, METRIC = metres

    def _parse_stackup(self, content: str) -> None:
        """Parse STACKUP section to get layer order (top to bottom)."""
        stackup_match = re.search(r'\{STACKUP(.*?)\}', content, re.DOTALL)
        if stackup_match:
            for line in stackup_match.group(1).split('\n'):
                if 'SIGNAL' in line:
                    layer_match = re.search(r'L="([^"]+)"', line)
                    if layer_match:
                        self.stackup_layers.append(layer_match.group(1))

    def _parse_board_perimeter(self, content: str) -> None:
        """Parse the board outline (BOARD-section PERIMETER_SEGMENTs).

        KiCad's Hyperlynx export writes the Edge.Cuts outline here. The
        segments are later chained into closed loops and drawn on the
        prBoundary layer (see GDSGenerator.add_board_outline).
        """
        for m in self.PERIMETER_SEGMENT_PATTERN.finditer(content):
            self.perimeter_segments.append(PerimeterSegment(
                x1=float(m.group(1)),
                y1=float(m.group(2)),
                x2=float(m.group(3)),
                y2=float(m.group(4)),
            ))
        n_arcs = len(self.PERIMETER_ARC_PATTERN.findall(content))
        if n_arcs:
            print(f"Warning: {n_arcs} PERIMETER_ARC entries are not "
                  f"supported; the board outline will not be drawn",
                  file=sys.stderr)
            self.perimeter_segments = []

    def _parse_padstacks(self, content: str) -> None:
        """Parse PADSTACK definitions."""
        # Split content into blocks
        lines = content.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]
            padstack_match = self.PADSTACK_PATTERN.search(line)
            if padstack_match:
                index = int(padstack_match.group(1))
                drill = float(padstack_match.group(2))
                layers = []
                pad_width = 0.0
                pad_height = 0.0

                # Parse layer entries until we hit closing brace
                i += 1
                while i < len(lines) and '}' not in lines[i]:
                    layer_match = self.PADSTACK_LAYER_PATTERN.search(lines[i])
                    if layer_match:
                        layers.append(layer_match.group(1))
                        # Capture pad dimensions from first layer entry
                        if pad_width == 0.0:
                            pad_width = float(layer_match.group(2))
                            pad_height = float(layer_match.group(3))
                    i += 1

                self.padstacks[index] = Padstack(
                    index=index,
                    drill=drill,
                    layers=layers,
                    pad_width=pad_width,
                    pad_height=pad_height
                )
            i += 1

    def _parse_segments_and_vias(self, content: str) -> None:
        """Parse all NET/SEG/VIA entries."""
        current_net = ""

        for line in content.split('\n'):
            # Check for new NET block
            net_match = self.NET_PATTERN.search(line)
            if net_match:
                current_net = net_match.group(1)

            # Check for SEG entries
            seg_match = self.SEG_PATTERN.search(line)
            if seg_match:
                segment = TraceSegment(
                    net_name=current_net,
                    x1=float(seg_match.group(1)),
                    y1=float(seg_match.group(2)),
                    x2=float(seg_match.group(3)),
                    y2=float(seg_match.group(4)),
                    width=float(seg_match.group(5)),
                    layer=seg_match.group(6)
                )
                self.segments.append(segment)

            # Check for ARC entries
            arc_match = self.ARC_PATTERN.search(line)
            if arc_match:
                arc = TraceArc(
                    net_name=current_net,
                    x1=float(arc_match.group(1)),
                    y1=float(arc_match.group(2)),
                    x2=float(arc_match.group(3)),
                    y2=float(arc_match.group(4)),
                    xc=float(arc_match.group(5)),
                    yc=float(arc_match.group(6)),
                    radius=float(arc_match.group(7)),
                    width=float(arc_match.group(8)),
                    layer=arc_match.group(9)
                )
                self.arcs.append(arc)

            # Check for VIA entries
            via_match = self.VIA_PATTERN.search(line)
            if via_match:
                via = Via(
                    net_name=current_net,
                    x=float(via_match.group(1)),
                    y=float(via_match.group(2)),
                    padstack_index=int(via_match.group(3))
                )
                self.vias.append(via)

            # Check for PIN entries (for device positioning)
            pin_match = self.PIN_PATTERN.search(line)
            if pin_match:
                pin = Pin(
                    x=float(pin_match.group(1)),
                    y=float(pin_match.group(2)),
                    ref=pin_match.group(3),
                    padstack_index=int(pin_match.group(4))
                )
                self.pins.append(pin)

    def _parse_board_path(self, content: str) -> None:
        """Capture the source board file path from the {BOARD "..."} header.

        Its directory anchors a board-relative die GDS_FILE (e.g.
        ../chiplets/die.gds), so the die layout resolves against the board's
        own location instead of the process CWD (the .hyp lives in a temp dir).
        """
        m = self.BOARD_HEADER_PATTERN.search(content)
        if m:
            self.board_path = m.group(1)

    def _parse_devices(self, content: str) -> None:
        """Parse DEVICES section for GDS_FILE entries with position."""
        board_dir = os.path.dirname(self.board_path) if self.board_path else ""
        for match in self.DEVICE_PATTERN.finditer(content):
            gds_file = match.group(6)
            # Resolve a board-relative die GDS_FILE (e.g. ../chiplets/die.gds)
            # against the source board's directory, so the die layout is found
            # wherever the export runs -- the .hyp itself lives in a temp dir,
            # and the importer otherwise resolves a relative path against the
            # process CWD. Absolute paths and ${VAR} ecosystem-root refs are
            # left as-is (${VAR} is expanded later, in add_device); an empty
            # board_dir (e.g. a synthetic {BOARD "name"} with no directory)
            # also leaves the path untouched, preserving CWD-relative behavior.
            if (board_dir and gds_file and "${" not in gds_file
                    and not os.path.isabs(gds_file)):
                gds_file = os.path.normpath(os.path.join(board_dir, gds_file))
            device = Device(
                ref=match.group(1),
                layer=match.group(2),
                x=float(match.group(3)),       # Position X from HYP
                y=float(match.group(4)),       # Position Y from HYP
                rotation=float(match.group(5)),  # Rotation in degrees
                gds_file=gds_file
            )
            self.devices.append(device)


# Fraction of trace elements on unmapped layers above which the conversion
# fails instead of writing a near-empty GDS (board copper named with KiCad
# defaults instead of the PDK metals is the classic cause). Below the
# threshold stray layers are tolerated with an aggregate warning.
UNMAPPED_FAIL_FRACTION = 0.5

# <stem>.pillars.json sidecar contract (as-drawn Cu-pillar/bump centers).
# Readers exact-match the version string (same policy as the boundary
# manifest); bump producers and readers together.
#
# 1.1.0 adds the optional "methods" block: the per-method attachment rules the
# pillars were placed and checked against, keyed by method id, with the same
# field names the assembly DRC uses (IXN_spacing, IXN_pitch, IXN_pad_size).
# It belongs here rather than in a sidecar of its own because those numbers are
# what drives auto-resolve, so they are the reason a record carries
# moved_by_auto_resolve. It also makes the carrier self-describing: the
# interposer GDS has the pad openings but nothing in it says which attachment
# method they belong to, and the deck that checks bump-to-bump rules per method
# is the assembly one, which needs the die boundaries the carrier does not
# carry.
PILLAR_MANIFEST_SCHEMA = "adk-pillar-manifest"
PILLAR_MANIFEST_VERSION = "1.1.0"

# IntM4TM2 constants for the cmim device, used only when the PDK checkout
# cannot be resolved; the live values are techParams in
# intm4tm2_pycell_lib/intm4tm2_tech.json (see GDSGenerator._intm4tm2_tech).
_INTM4TM2_TECH_FALLBACK = {
    "grid_um": 0.005,            # placement grid
    "min_lw_um": 1.14,           # cmim_minLW
    "max_lw_um": 1000.0,         # cmim_maxLW
    "max_c_fF": 8000.0,          # cmim_maxC
    "area_fF_per_um2": 1.5,      # cmim_caspec
    "perim_fF_per_um": 0.04,     # cmim_cpspec
}

# SI suffixes as the interposer PDK writes its tech values ("1.14u", "8p").
_SI_SUFFIX = {
    "y": 1e-24, "z": 1e-21, "a": 1e-18, "f": 1e-15, "p": 1e-12,
    "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
}


def _si_num(value) -> float:
    """Parse a tech value that may be a number or an SI-suffixed string.

    Mirrors _num() in the PDK's cmim_footprint_gen.py: 0.36 -> 0.36,
    "1.5m" -> 1.5e-3, "8p" -> 8e-12, "1.14u" -> 1.14e-6.
    """
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        raise ValueError("empty tech value")
    suffix = text[-1]
    if suffix in _SI_SUFFIX and not suffix.isdigit():
        return float(text[:-1]) * _SI_SUFFIX[suffix]
    return float(text)

# The interposer's die-attachment surface (BEOL top), in micrometers: the
# plane dies mount on, in the interposer's local frame. It is the interposer
# PDK's own value, read from its stack descriptor on every finalize and never
# written from memory: a run that cannot read it is refused
# (AttachFrameRefused). It is a distinct quantity from the interposer's
# physical body thickness (dimensions.thickness), which comes from the KiCad
# board stackup.
ATTACH_FRAME_DESCRIPTOR = ("libs.tech", "intm4tm2_stack.json")
ATTACH_FRAME_KEY = "beol.attachment_surface_z_um"


class AttachFrameRefused(Exception):
    """The interposer PDK's mount plane cannot be read; nothing is written.

    Not a ``ValueError``, for the reason ``DieOutlineRefused`` gives. The
    message names INTERPOSER_PDK_ROOT and the key.
    """


def _as_written(value) -> Decimal:
    """A number as its text writes it: an int, or a float's shortest repr."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    return Decimal(repr(float(value)))


@dataclass(frozen=True)
class AttachFrame:
    """The mount plane, exactly as the descriptor writes it."""
    z_um: Decimal
    source: str     # the descriptor read, or "caller"

    def at(self, *heights) -> float:
        """A z on this plane: the exact decimal sum of the plane and each
        height as written, emitted as the shortest text that reads back.

        10.8103 + 7.77777 is 18.58807, never the 18.588070000000002 float
        addition gives, and a height of 18.00005 gives 28.81035: nothing is
        rounded away.
        """
        return float(self.z_um + sum((_as_written(h) for h in heights),
                                     Decimal(0)))

    @classmethod
    def given(cls, z_um) -> 'AttachFrame':
        """A plane a direct caller passes as a number."""
        return cls(_as_written(z_um), "caller")


def read_attach_frame() -> AttachFrame:
    """The mount plane from the interposer PDK; AttachFrameRefused otherwise.

    The root comes from INTERPOSER_PDK_ROOT through the usual discovery
    (environment, then a sibling checkout). There is no fallback value.
    """
    where = "%s in %s" % (ATTACH_FRAME_KEY, "/".join(ATTACH_FRAME_DESCRIPTOR))
    root = _discover_path_var("INTERPOSER_PDK_ROOT")
    if not root:
        raise AttachFrameRefused(
            "the interposer PDK was not found (set INTERPOSER_PDK_ROOT to an "
            "OpenIntM4TM2 checkout); the mount plane is its %s and is not "
            "written from memory" % where)
    path = os.path.join(root, *ATTACH_FRAME_DESCRIPTOR)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle, parse_float=Decimal)
    except (OSError, ValueError) as exc:
        raise AttachFrameRefused(
            "cannot read %s under INTERPOSER_PDK_ROOT=%s: %s"
            % (where, root, exc)) from None
    beol = data.get("beol") if isinstance(data, dict) else None
    value = beol.get("attachment_surface_z_um") if isinstance(beol, dict) \
        else None
    # NaN and Infinity load as floats, not Decimal, so they fail here too.
    number = ((isinstance(value, int) and not isinstance(value, bool))
              or (isinstance(value, Decimal) and value.is_finite()))
    if not number:
        raise AttachFrameRefused(
            "%s has no %s that is a number of um (found %r), under "
            "INTERPOSER_PDK_ROOT=%s" % (path, ATTACH_FRAME_KEY, value, root))
    return AttachFrame(_as_written(value), path)


class GDSGenerator:
    """Generates GDS file from parsed HYP data using KLayout API."""

    # Conversion constants
    INCH_TO_UM = 25400.0  # 1 inch = 25,400 micrometers
    METER_TO_UM = 1e6     # 1 meter = 1,000,000 micrometers

    # Metal layer order for via_stack mapping
    METAL_LAYERS = ['Metal1', 'Metal2', 'Metal3', 'Metal4', 'Metal5', 'TopMetal1', 'TopMetal2']

    # Via layers between metal layers
    VIA_LAYERS = {
        ('Metal4', 'Metal5'): 'Via4',
        ('Metal5', 'TopMetal1'): 'TopVia1',
        ('TopMetal1', 'TopMetal2'): 'TopVia2',
    }

    # Default PDK via parameters (in micrometers), used as fallback when
    # interposer_tech_default.json is not available. Values must match
    # sg13g2_tech.json: Vn_a/Vn_b/Vn_c1, TV1_a/TV1_b/TV1_d, TV2_a/TV2_b/TV2_d
    _DEFAULT_VIA_PARAMS = {
        'Vn': {'size': 0.19, 'sep': 0.22, 'enc': 0.05},   # Via1-Via4
        'TV1': {'size': 0.42, 'sep': 0.42, 'enc': 0.42},  # TopVia1
        'TV2': {'size': 0.9, 'sep': 1.06, 'enc': 0.5},    # TopVia2
    }

    @staticmethod
    def _load_via_params(tech_json_path: Optional[str] = None) -> Dict:
        """Load PDK via parameters from interposer_tech_default.json.

        Maps JSON keys to the internal format:
          Vn:  size=Vn_a, sep=Vn_b, enc=Vn_c1
          TV1: size=TV1_a, sep=TV1_b, enc=TV1_d
          TV2: size=TV2_a, sep=TV2_b, enc=TV2_d

        Falls back to _DEFAULT_VIA_PARAMS if the file is missing or
        does not contain the required keys.
        """
        if tech_json_path is None:
            return dict(GDSGenerator._DEFAULT_VIA_PARAMS)

        try:
            with open(tech_json_path, 'r') as f:
                data = json.load(f)
            rules = data.get('rules', {})

            # Map JSON parameter names to internal via param dict
            param_map = {
                'Vn':  {'size': 'Vn_a',  'sep': 'Vn_b',  'enc': 'Vn_c1'},
                'TV1': {'size': 'TV1_a', 'sep': 'TV1_b', 'enc': 'TV1_d'},
                'TV2': {'size': 'TV2_a', 'sep': 'TV2_b', 'enc': 'TV2_d'},
            }

            result = {}
            for via_type, keys in param_map.items():
                if all(k in rules for k in keys.values()):
                    result[via_type] = {
                        'size': rules[keys['size']],
                        'sep': rules[keys['sep']],
                        'enc': rules[keys['enc']],
                    }
                else:
                    missing = [k for k in keys.values() if k not in rules]
                    print(f"Warning: {tech_json_path} missing keys {missing} for {via_type}, "
                          f"using defaults", file=sys.stderr)
                    result[via_type] = dict(GDSGenerator._DEFAULT_VIA_PARAMS[via_type])

            return result
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
            print(f"Warning: Could not load via params from {tech_json_path}: {e}, "
                  f"using defaults", file=sys.stderr)
            return dict(GDSGenerator._DEFAULT_VIA_PARAMS)

    @staticmethod
    def _load_notch_space(tech_json_path: Optional[str] = None) -> Dict[str, float]:
        """Load the minimum-space values the notch heal fills to.

        Same file and same failure policy as _load_via_params: a missing file
        or a missing key falls back to _DEFAULT_NOTCH_SPACE rather than
        aborting, because a heal that does not run leaves the layout exactly
        as it was and the carrier DRC still reports what it finds.
        """
        defaults = dict(GDSGenerator._DEFAULT_NOTCH_SPACE)
        if tech_json_path is None:
            return defaults
        try:
            with open(tech_json_path, 'r') as f:
                rules = json.load(f).get('rules', {})
        except (OSError, json.JSONDecodeError) as e:
            print(f"Warning: could not load notch-space rules from "
                  f"{tech_json_path}: {e}, using defaults", file=sys.stderr)
            return defaults
        missing = [k for k in defaults if k not in rules]
        if missing:
            print(f"Warning: {tech_json_path} missing keys {missing} for the "
                  f"notch heal, using defaults for those", file=sys.stderr)
        return {k: float(rules.get(k, v)) for k, v in defaults.items()}

    def __init__(self, layer_map: LayerMap, cell_name: str = "INTERPOSER", units: str = "ENGLISH",
                 stackup_order: List[str] = None, tech_json_path: Optional[str] = None,
                 annotate_boundaries: bool = False,
                 boundary_viz_layer: Tuple[int, int] = (1000, 0),
                 grid_nm: int = MANUFACTURING_GRID_NM):
        self.layer_map = layer_map
        self.units = units
        self.stackup_order = stackup_order or []  # Layer order from HYP STACKUP (top to bottom)
        self.PDK_VIA_PARAMS = self._load_via_params(tech_json_path)
        self.PDK_NOTCH_SPACE = self._load_notch_space(tech_json_path)
        self.layout = db.Layout()
        self.layout.dbu = 0.001  # 1 DBU = 1 nm (0.001 um) - Changed from 0.0001 to match standard GDS
        # Manufacturing grid in database units. 0 (or 1) disables snapping,
        # which is only ever useful for debugging what the raw conversion
        # produced; every real export has to be on grid.
        self.grid_dbu = max(0, int(round(grid_nm * 0.001 / self.layout.dbu)))
        # Cells whose geometry is NOT ours to move: imported chiplet dies and
        # everything they instantiate. A third-party die is delivered as-is,
        # so the grid pass leaves both its shapes and its placement alone.
        self._foreign_cells: set = set()
        # Trace copper per (layer, net), filled by add_segments_as_paths.
        self.trace_copper: Dict[Tuple[str, str], List[db.Polygon]] = {}
        # Trace segments that are neither orthogonal nor 45 degrees; they
        # cannot be drawn legally on this process and the count drives a
        # warning at write() rather than a silent reshaping.
        self._odd_angle_segments = 0
        self.top_cell = self.layout.create_cell(cell_name)
        self.routing_cell = self.layout.create_cell(f"{cell_name}_ROUTING")
        self.top_cell.insert(db.DCellInstArray(self.routing_cell, db.DTrans()))
        self._gds_layers: Dict[str, int] = {}  # Cache for layer indices
        # Via PCell cache. Cell INDICES, not db.Cell handles: binding the
        # layout to another technology (add_cmim_devices) drops the library
        # proxies these came from and every held handle with them, while the
        # cells themselves survive and stay reachable by index.
        self._via_cells: Dict[str, int] = {}
        # IntM4TM2 device constants (grid, dimension and capacitance
        # limits), resolved from the PDK on first use.
        self._intm4tm2_tech_cache: Optional[Dict[str, float]] = None
        self._via_group_cells: Dict[str, db.Cell] = {}  # metal_pair -> group cell
        self._boundary_records: List[dict] = []  # chiplet boundaries -> manifest
        # As-drawn Cu-pillar records -> <stem>.pillars.json. None means the
        # bump-generation path never ran (no manifest); an empty list means it
        # ran and placed nothing (manifest with an empty pillars array).
        # Records accumulate in the raw drawing frame; the manifest writer
        # rebases them into the canonical GDS-bbox-corner frame (see
        # _write_pillar_manifest and _pillar_frame_origin).
        self._pillar_records: Optional[List[dict]] = None
        # Per-method attachment rules the pillars were checked against ->
        # the manifest's "methods" block (see record_interconnect_rules).
        self._pillar_method_rules: Optional[Dict[str, dict]] = None
        # Lower-left corner (x_min, y_min, um) of the interposer top-cell
        # bbox, captured at the FIRST pillar-manifest write (the interposer
        # GDS write, before chiplet instances are added). Manifest x/y are
        # rebased by this origin so they live in the same canonical
        # GDS-bbox-corner frame as the .chiplet positions and io_pads
        # (chiplet-studio coord frame contract); the later complete-GDS
        # manifest reuses it so both sidecars share one frame.
        self._pillar_frame_origin: Optional[Tuple[float, float]] = None
        # Opt-in, viewer-only annotation of the boundaries. No DRC rule reads
        # boundary_viz_layer; the contract lives in the manifest, not the GDS.
        self._annotate_boundaries = bool(annotate_boundaries)
        self._boundary_viz_layer = boundary_viz_layer
        # Unmapped-layer accounting for the loud guard in convert_hyp_to_gds:
        # trace elements whose board layer the LYP does not map, per layer,
        # plus the count that did land on mapped layers.
        self._unmapped_layers: Dict[str, int] = {}
        self._mapped_trace_elements = 0
        # (ring cell index, instance Trans) once add_seal_ring drew it, and
        # whether write() has already cleared the layout against it.
        self._seal_ring: Optional[Tuple[int, 'db.Trans']] = None
        self._seal_ring_cleared = False
        self._pcells_available = self._check_pcells_available()

    # Subpath from $PDK_ROOT to the KLayout tech of the SG13G2 base PDK
    # (where the SG13_dev via_stack PCell library lives).
    _SG13G2_KLAYOUT_SUBPATH = ("ihp-sg13g2", "libs.tech", "klayout")

    def _try_create_test_pcell(self) -> bool:
        """Probe the SG13_dev via_stack PCell on this generator's layout."""
        try:
            test_cell = self.layout.create_cell("via_stack", "SG13_dev", {
                "b_layer": "Metal4",
                "t_layer": "Metal5",
                "vn_columns": 1,
                "vn_rows": 1,
            })
            if test_cell:
                # Clean up test cell
                self.layout.delete_cell(test_cell.cell_index())
                return True
        except Exception:
            pass
        return False

    def _bootstrap_sg13g2_pcells(self) -> bool:
        """Register the SG13G2 PCell library in this Python process.

        Replicates what the PDK's tech/pymacros/autorun.lym does inside a
        KLayout session, so the PCell path also works under plain python
        (the standalone klayout module loads no technologies or PCell
        libraries on its own): extend sys.path with the PDK python dirs
        and import sg13g2_pycell_lib -- the import registers the Library
        'SG13_dev'. That library is technology-bound, so additionally
        register the 'sg13g2' db.Technology and bind this generator's
        layout to it; without the binding the create_cell lookup returns
        None even with the library registered.

        PDK discovery follows the ecosystem convention: $PDK_ROOT env
        first, then a sibling IHP-Open-PDK checkout (_discover_path_var).
        Returns False quietly when the PDK is absent -- the rectangle
        fallback is the designed degradation. A found-but-broken PDK
        (e.g. missing tkinter/psutil deps) reports the cause on stderr.
        """
        pdk_root = _discover_path_var("PDK_ROOT")
        if not pdk_root:
            return False
        pdk_klayout = Path(pdk_root).joinpath(*self._SG13G2_KLAYOUT_SUBPATH)
        if not pdk_klayout.is_dir():
            return False
        python_dir = pdk_klayout / "python"
        cni_dir = python_dir / "pycell4klayout-api" / "source" / "python"
        for entry in (str(python_dir), str(cni_dir)):
            if entry not in sys.path:
                sys.path.insert(0, entry)
        try:
            if "sg13g2" not in db.Technology.technology_names():
                tech = db.Technology.create_technology("sg13g2")
                lyt = pdk_klayout / "tech" / "sg13g2.lyt"
                if lyt.is_file():
                    tech.load(str(lyt))
            if "SG13_dev" not in db.Library.library_names():
                import sg13g2_pycell_lib  # noqa: F401  (registers SG13_dev)
            self.layout.technology_name = "sg13g2"
            return True
        except (Exception, SystemExit) as exc:
            # SystemExit too: sg13g2_pycell_lib sys.exit(1)s when a PCell
            # module fails to load (e.g. psutil missing); a broken PDK
            # install must degrade to the fallback, not kill the export.
            print(f"Note: SG13G2 PCell bootstrap failed ({exc}); "
                  f"falling back to simple via rectangles", file=sys.stderr)
            return False

    def _check_pcells_available(self) -> bool:
        """Check if PDK PCells are available, bootstrapping if needed.

        Inside a KLayout session with the PDK on KLAYOUT_PATH the first
        probe may already succeed; otherwise self-register the library
        from $PDK_ROOT (or a sibling IHP-Open-PDK checkout) and retry.
        """
        if self._try_create_test_pcell():
            return True
        if self._bootstrap_sg13g2_pcells():
            return self._try_create_test_pcell()
        return False

    def _get_or_create_via_group(self, sorted_layers: List[str]) -> db.Cell:
        """Get or create a group cell for a metal pair (e.g., Metal4_Metal5)."""
        group_key = '_'.join(sorted_layers)
        if group_key not in self._via_group_cells:
            group_cell = self.layout.create_cell(f"VIAS_{group_key}")
            self.routing_cell.insert(db.DCellInstArray(group_cell, db.DTrans()))
            self._via_group_cells[group_key] = group_cell
        return self._via_group_cells[group_key]

    def _get_gds_layer(self, layer_name: str) -> int:
        """Get or create GDS layer index for a layer name."""
        if layer_name not in self._gds_layers:
            layer_num, datatype = self.layer_map.get_layer(layer_name)
            self._gds_layers[layer_name] = self.layout.layer(layer_num, datatype)
        return self._gds_layers[layer_name]

    def _to_um(self, value: float) -> float:
        """Convert value from HYP units to micrometers."""
        if self.units == "METRIC":
            return value * self.METER_TO_UM
        else:  # ENGLISH (inches)
            return value * self.INCH_TO_UM

    def _to_um_y(self, value: float) -> float:
        """Convert Y value from HYP units to micrometers with reflection fix.

        METRIC: Y already has correct sign (negative), no negation needed.
        ENGLISH: Y needs to be negated for X-axis reflection.
        """
        if self.units == "METRIC":
            return value * self.METER_TO_UM
        else:  # ENGLISH (inches)
            return -value * self.INCH_TO_UM

    def _arc_to_points(self, arc: TraceArc, num_points: int = 16) -> List[Tuple[float, float]]:
        """
        Discretize an arc into a list of points in micrometers.
        Returns points from (x1,y1) to (x2,y2) along the arc.

        The record has no direction flag, but it has a direction: an ARC runs
        counter-clockwise from (X1,Y1) to (X2,Y2) in file coordinates, the
        HyperLynx convention. Both writers keep it (export_hyperlynx.cpp and
        writers/hyperlynx_writer.py swap the ends of an arc KiCad calls
        counter-clockwise on its Y-down coordinates, then negate Y). So the
        sweep is the counter-clockwise angle in the file, and it may be more
        than 180 degrees. Where this reader negates Y on the way in (ENGLISH
        units) the mirror makes it clockwise in the GDS.

        num_points chords per half turn: an arc up to 180 degrees gets exactly
        num_points, as it always did, and a longer one proportionally more.
        The first and last points are the record's own ends.
        """
        x1 = self._to_um(arc.x1)
        y1 = self._to_um_y(arc.y1)
        x2 = self._to_um(arc.x2)
        y2 = self._to_um_y(arc.y2)
        xc = self._to_um(arc.xc)
        yc = self._to_um_y(arc.yc)
        radius = self._to_um(arc.radius)

        # Calculate start and end angles
        angle1 = math.atan2(y1 - yc, x1 - xc)
        angle2 = math.atan2(y2 - yc, x2 - xc)

        # Counter-clockwise in the file, so clockwise here when Y was negated.
        diff = angle2 - angle1
        if self._to_um_y(1.0) > 0:
            if diff < 0:
                diff += 2 * math.pi
        elif diff > 0:
            diff -= 2 * math.pi
        if abs(diff) > math.pi:
            num_points = int(math.ceil(num_points * abs(diff) / math.pi))

        # Generate points along the arc
        points = []
        for i in range(num_points + 1):
            t = i / num_points
            angle = angle1 + t * diff
            px = xc + radius * math.cos(angle)
            py = yc + radius * math.sin(angle)
            points.append((px, py))
        # The ends are the record's, not recomputed from the centre and the
        # radius: KiCad rounds those to the nanometre, so the computed ends can
        # miss by a rounding step, and _connect_traces_to_paths, which matches
        # ends to the nanometre, then splits the trace at the joint.
        points[0] = (x1, y1)
        points[-1] = (x2, y2)

        return points

    def _build_trace_elements(self, segments: List[TraceSegment], arcs: List[TraceArc]) -> Dict[Tuple[str, str, float], List]:
        """
        Group segments and arcs by (net, layer, width).
        Returns dict mapping (net, layer, width) -> list of (type, element) tuples.

        Net is part of the key so path-stitching (_connect_traces_to_paths)
        only chains elements of the same net. Without it, two distinct nets
        that happen to share an endpoint -- or merely touch at a T-junction --
        would be welded into one path, corrupting the drawn copper topology.
        """
        groups: Dict[Tuple[str, str, float], List] = {}

        for seg in segments:
            key = (seg.net_name, seg.layer, seg.width)
            groups.setdefault(key, []).append(('seg', seg))

        for arc in arcs:
            key = (arc.net_name, arc.layer, arc.width)
            groups.setdefault(key, []).append(('arc', arc))

        return groups

    def _connect_traces_to_paths(self, elements: List) -> List[List[Tuple[float, float]]]:
        """
        Connect segments and arcs that share endpoints into continuous paths.
        Elements is a list of ('seg', TraceSegment) or ('arc', TraceArc) tuples.
        Returns list of paths, where each path is a list of (x, y) points in micrometers.
        """
        if not elements:
            return []

        def point_key(x: float, y: float) -> Tuple[int, int]:
            return (round(x * 1000), round(y * 1000))  # nm precision

        # Convert elements to coordinates and store arc info
        # Each entry: ((x1,y1), (x2,y2), arc_points_or_None)
        elem_data = []
        for i, (etype, elem) in enumerate(elements):
            if etype == 'seg':
                x1 = self._to_um(elem.x1)
                y1 = self._to_um_y(elem.y1)
                x2 = self._to_um(elem.x2)
                y2 = self._to_um_y(elem.y2)
                elem_data.append(((x1, y1), (x2, y2), None))
            else:  # arc
                x1 = self._to_um(elem.x1)
                y1 = self._to_um_y(elem.y1)
                x2 = self._to_um(elem.x2)
                y2 = self._to_um_y(elem.y2)
                arc_points = self._arc_to_points(elem)
                elem_data.append(((x1, y1), (x2, y2), arc_points))

        # Build adjacency
        point_to_elems: Dict[Tuple[int, int], List[int]] = {}
        for i, ((x1, y1), (x2, y2), _) in enumerate(elem_data):
            k1 = point_key(x1, y1)
            k2 = point_key(x2, y2)
            point_to_elems.setdefault(k1, []).append(i)
            point_to_elems.setdefault(k2, []).append(i)

        # Build paths by traversing connected elements
        used = set()
        paths = []

        for start_idx in range(len(elem_data)):
            if start_idx in used:
                continue

            # Start a new path
            (x1, y1), (x2, y2), arc_pts = elem_data[start_idx]
            if arc_pts:
                path_points = list(arc_pts)
            else:
                path_points = [(x1, y1), (x2, y2)]
            used.add(start_idx)

            # Extend path in both directions
            for direction in [0, -1]:  # 0 = forward from last point, -1 = backward from first
                while True:
                    if direction == 0:
                        curr_point = path_points[-1]
                    else:
                        curr_point = path_points[0]

                    key = point_key(curr_point[0], curr_point[1])
                    neighbors = point_to_elems.get(key, [])

                    # Find unused neighbor
                    next_elem = None
                    for elem_idx in neighbors:
                        if elem_idx not in used:
                            next_elem = elem_idx
                            break

                    if next_elem is None:
                        break

                    # Get the neighbor's data
                    (nx1, ny1), (nx2, ny2), narc_pts = elem_data[next_elem]
                    nk1 = point_key(nx1, ny1)

                    # Determine which direction to add points
                    if nk1 == key:
                        # Start of neighbor matches current point
                        if narc_pts:
                            new_points = narc_pts[1:]  # Skip first (already have it)
                        else:
                            new_points = [(nx2, ny2)]
                    else:
                        # End of neighbor matches current point, reverse
                        if narc_pts:
                            new_points = list(reversed(narc_pts))[1:]
                        else:
                            new_points = [(nx1, ny1)]

                    if direction == 0:
                        path_points.extend(new_points)
                    else:
                        for pt in reversed(new_points):
                            path_points.insert(0, pt)

                    used.add(next_elem)

            paths.append(path_points)

        return paths

    # ------------------------------------------------------------------
    # Manufacturing grid
    # ------------------------------------------------------------------

    def _snap(self, v: int) -> int:
        """Snap one database-unit coordinate onto the manufacturing grid."""
        g = self.grid_dbu
        if g <= 1:
            return int(v)
        return int(round(v / float(g))) * g

    def _snap_um(self, v_um: float) -> float:
        """Snap a micrometer value, returned in micrometers.

        Used for the coordinates that also travel in a sidecar (bump centres,
        I/O pad positions), so the manifest and the drawn geometry agree
        exactly instead of by a couple of nanometers.
        """
        return self._snap(int(round(v_um / self.layout.dbu))) * self.layout.dbu

    def _snap_point(self, p) -> db.Point:
        return db.Point(self._snap(p.x), self._snap(p.y))

    @staticmethod
    def _sgn(v: int) -> int:
        return (v > 0) - (v < 0)

    def _right_normal(self, dx: int, dy: int,
                      half: int, diag: int) -> Optional[Tuple[int, int]]:
        """Offset from the centreline to the right-hand flank of a run.

        half on the perpendicular axis for an orthogonal run, (diag, diag) for
        an exactly diagonal one; those are the same offsets the segment quads
        use, so the flanks a corner patch lands on are the flanks that exist.
        None for a run that is neither, which the caller draws with a snapped
        approximate offset and counts as an odd angle.
        """
        if dx == 0 and dy == 0:
            return None
        if dx == 0 or dy == 0:
            return (self._sgn(dy) * half, -self._sgn(dx) * half)
        if abs(dx) == abs(dy):
            return (self._sgn(dy) * diag, -self._sgn(dx) * diag)
        return None

    def _quad_offset(self, dx: int, dy: int, half: int,
                     diag: int) -> Tuple[int, int]:
        """Offset from the centreline to one flank of a run, as its quad uses.

        The sign is whichever the quad happens to take; callers that need a
        side pick it. A run that is neither orthogonal nor exactly diagonal
        gets its perpendicular rounded onto the grid.
        """
        if dx == 0:
            return half, 0
        if dy == 0:
            return 0, half
        if abs(dx) == abs(dy):
            return (-diag if dy > 0 else diag), (diag if dx > 0 else -diag)
        length = math.hypot(dx, dy)
        return (self._snap(int(round(-dy * half / length))),
                self._snap(int(round(dx * half / length))))

    @staticmethod
    def _convex_hull(points: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """Convex hull of integer points (monotone chain), counter-clockwise."""
        pts = sorted(set(points))
        if len(pts) < 3:
            return pts

        def cross(o, a, b):
            return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

        lower: List[Tuple[int, int]] = []
        for q in pts:
            while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
                lower.pop()
            lower.append(q)
        upper: List[Tuple[int, int]] = []
        for q in reversed(pts):
            while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
                upper.pop()
            upper.append(q)
        return lower[:-1] + upper[:-1]

    def _any_angle_corner_patch(self, p: db.Point, d1: Tuple[int, int],
                                d2: Tuple[int, int], cross: int, half: int,
                                diag: int) -> Optional[db.Polygon]:
        """The corner patch at a bend with a run off the 45 degree grid.

        The hull of the four quad corners at p and the point where the two
        outer flanks meet. It closes the wedge the quads leave open, reaches no
        further than those flanks do (the mitre), and has no acute corner: the
        corners on the flanks are right angles and the one at the tip is 180
        degrees less the turn. None when the hull degenerates.
        """
        o1 = self._quad_offset(d1[0], d1[1], half, diag)
        o2 = self._quad_offset(d2[0], d2[1], half, diag)

        def outward(d, o):
            # Outside of the turn: right of a left turn, left of a right one.
            side = d[0] * o[1] - d[1] * o[0]
            return o if (side < 0) == (cross > 0) else (-o[0], -o[1])

        n1, n2 = outward(d1, o1), outward(d2, o2)
        fa = (p.x + n1[0], p.y + n1[1])
        fb = (p.x + n2[0], p.y + n2[1])
        # fa + t*d1 = fb + s*d2
        t = (((fb[0] - fa[0]) * d2[1] - (fb[1] - fa[1]) * d2[0])
             / float(d1[0] * d2[1] - d1[1] * d2[0]))
        tip = (self._snap(int(round(fa[0] + t * d1[0]))),
               self._snap(int(round(fa[1] + t * d1[1]))))
        hull = self._convex_hull([
            (p.x + o1[0], p.y + o1[1]), (p.x - o1[0], p.y - o1[1]),
            (p.x + o2[0], p.y + o2[1]), (p.x - o2[0], p.y - o2[1]), tip])
        if len(hull) < 3:
            return None
        return db.Polygon([db.Point(x, y) for x, y in hull])

    @staticmethod
    def _unit(dx: int, dy: int) -> Tuple[int, int]:
        """Direction of a 0/45/90 run, as components in {-1, 0, 1}."""
        return ((dx > 0) - (dx < 0), (dy > 0) - (dy < 0))

    def _extended(self, end: db.Point, toward: db.Point, half: int,
                  diag: int) -> db.Point:
        """``end`` moved half a width away from ``toward``, along their run.

        The move is the run's right-hand flank offset turned a quarter turn
        forward: half for an orthogonal run, diag on each axis for an exactly
        diagonal one, so the end stays on the grid and at 45 degrees. Any
        other run takes the direction scaled to half, then snapped per axis.
        """
        dx, dy = end.x - toward.x, end.y - toward.y
        n = self._right_normal(dx, dy, half, diag)
        if n is None:
            length = math.hypot(dx, dy)
            n = (self._snap(int(round(dy * half / length))),
                 self._snap(int(round(-dx * half / length))))
        return db.Point(end.x - n[1], end.y + n[0])

    def _inner_of(self, point: Tuple[int, int], direction: Tuple[int, int],
                  inside: db.Point, reach: int) -> db.Polygon:
        """A polygon covering everything on the `inside` side of a flank line.

        Big enough to swallow the corner patch whole, so intersecting with it
        is the same as clipping to the half plane.
        """
        ux, uy = direction
        px, py = -uy, ux
        if px * (inside.x - point[0]) + py * (inside.y - point[1]) < 0:
            px, py = -px, -py
        along = (ux * reach, uy * reach)
        off = (px * reach, py * reach)
        return db.Polygon([
            db.Point(point[0] - along[0], point[1] - along[1]),
            db.Point(point[0] + along[0], point[1] + along[1]),
            db.Point(point[0] + along[0] + off[0], point[1] + along[1] + off[1]),
            db.Point(point[0] - along[0] + off[0],
                     point[1] - along[1] + off[1])])

    def _corner_patch(self, a: db.Point, p: db.Point, b: db.Point,
                      half: int, diag: int) -> db.Polygon:
        """The copper that closes the corner at an interior vertex p.

        The two segment quads leave exactly one wedge open there, on the
        outside of the turn. The square of side 2*half centred on p closes it,
        which is why it was the first thing drawn, but at a 45 degree bend the
        square reaches the corner of its own bounding box while the two flanks
        meet earlier, so it leaves (2 - sqrt(2)) * width / 2 of copper sticking
        out past the flank: 1.17 um on a 4 um trace, a tip 2.83 um from the
        vertex where the flanks meet at 2.17 and KiCad's round join is at 2.00.
        That overshoot trips no rule, which is how it survived, but it is real
        copper pointing at whatever the trace passes, and it is not what the
        designer drew.

        So the patch is the square with the overshoot cut off along the flanks,
        united with the wedge in case the flanks meet outside the square (a 90
        degree turn between two diagonal runs). Two properties make this the
        shape to use rather than the wedge alone:

        - it has no acute corner. The wedge's own corner at p is the turn
          angle, 45 degrees at a 45 degree bend, and 3_2_angle.drc checks the
          RAW polygons, so a wedge drawn on its own is a violation even though
          the copper around it is a straight trace. Cutting the square keeps
          every corner at 90 or 135 degrees;
        - the cut lands ON the flanks instead of crossing them, so it adds no
          intersection vertex to the merged outline. That is the difference
          from the 45 degree chamfer tried earlier, which cut across the
          flanks and put merged-level vertices off the 5 nm grid.

        A run that is neither orthogonal nor exactly diagonal (any-angle
        routing, every vertex of a discretized arc) gets the hull patch of
        _any_angle_corner_patch instead. The square used to close those too,
        and turned with the trace a corner of it stands up to
        (sqrt(2) - 1) * half past the flank: 0.83 um on a 4 um trace, every
        few degrees along a curve.

        Falls back to the plain square, which is always safe, when the shape
        cannot be built exactly: a turn sharper than 90 degrees (where the
        flanks meet in a spike far outside the trace), or any result that is
        off-grid, not 0/45/90, or not a single piece.
        """
        square = db.Polygon(db.Box(p.x - half, p.y - half,
                                   p.x + half, p.y + half))
        d1 = (p.x - a.x, p.y - a.y)
        d2 = (b.x - p.x, b.y - p.y)
        cross = d1[0] * d2[1] - d1[1] * d2[0]
        if cross == 0 or d1[0] * d2[0] + d1[1] * d2[1] < 0:
            return square          # collinear, reversal, or sharper than 90
        n1 = self._right_normal(d1[0], d1[1], half, diag)
        n2 = self._right_normal(d2[0], d2[1], half, diag)
        if n1 is None or n2 is None:
            patch = self._any_angle_corner_patch(p, d1, d2, cross, half, diag)
            return square if patch is None else patch
        if cross < 0:
            # The open wedge is on the outside of the turn: right of a left
            # turn, left of a right one.
            n1, n2 = (-n1[0], -n1[1]), (-n2[0], -n2[1])
        flank_a = (p.x + n1[0], p.y + n1[1])
        flank_b = (p.x + n2[0], p.y + n2[1])

        # Where the two flanks meet: flank_a + t*d1 = flank_b + s*d2.
        num = ((flank_b[0] - flank_a[0]) * d2[1]
               - (flank_b[1] - flank_a[1]) * d2[0])
        if (num * d1[0]) % cross or (num * d1[1]) % cross:
            return square
        tip = (flank_a[0] + num * d1[0] // cross,
               flank_a[1] + num * d1[1] // cross)

        reach = 4 * (half + diag)
        region = (db.Region(square)
                  & db.Region(self._inner_of(flank_a, self._unit(*d1), p, reach))
                  & db.Region(self._inner_of(flank_b, self._unit(*d2), p, reach)))
        region += db.Region(db.Polygon([
            db.Point(p.x, p.y), db.Point(*flank_a), db.Point(*tip),
            db.Point(*flank_b)]))
        region.merge()
        if region.count() != 1:
            return square
        patch = next(region.each())
        if patch.holes():
            return square
        g = max(1, self.grid_dbu)
        pts = list(patch.each_point_hull())
        for q in pts:
            if q.x % g or q.y % g:
                return square
        for u, v in zip(pts, pts[1:] + pts[:1]):
            dx, dy = abs(v.x - u.x), abs(v.y - u.y)
            if not (dx == 0 or dy == 0 or dx == dy):
                return square
        return patch

    def _trace_polygons(self, points: List[Tuple[float, float]],
                        width_um: float, *,
                        free: Tuple[bool, bool] = (True, True)
                        ) -> List[db.Polygon]:
        """Fracture a trace centreline into on-grid polygons.

        One quad per segment plus one patch per interior corner, with each
        free end extended by w/2 (square ends; see free_ends for the ends
        that are not free). The union is the copper a mitered
        path with square ends would have drawn; the difference is that
        every drawn vertex is on the manufacturing grid and every drawn edge is
        0, 45 or 90 degrees. Snapping the vertices of a single path outline
        cannot give both: the perpendicular offset of a diagonal wire is
        width/(2*sqrt(2)), so either the vertex leaves the grid or the edge
        leaves 45 degrees. The offgrid and angle rules read the raw, as-drawn
        polygons, so fracturing satisfies them without changing the merged
        copper that the width, space and enclosure rules see.
        """
        dbu = self.layout.dbu
        raw = [(int(round(x / dbu)), int(round(y / dbu))) for x, y in points]

        # Snap the first vertex, then walk the centreline by snapped deltas.
        # Snapping each vertex on its own would be simpler but it breaks the
        # runs: two ends of an exactly diagonal segment can round in opposite
        # directions and leave |dx| and |dy| one grid step apart, which the
        # angle deck reports as a non-45 edge. Walking deltas keeps every run
        # orthogonal or exactly diagonal; the price is an accumulated drift of
        # at most half a grid step per segment, four orders of magnitude below
        # the via and pad overlaps that carry the connection.
        pts: List[db.Point] = []
        cx, cy = self._snap(raw[0][0]), self._snap(raw[0][1])
        pts.append(db.Point(cx, cy))
        for (px, py), (qx, qy) in zip(raw, raw[1:]):
            dx, dy = qx - px, qy - py
            sdx, sdy = self._snap(dx), self._snap(dy)
            if dx and dy and abs(dx) == abs(dy):
                mag = max(abs(sdx), abs(sdy))
                sdx = mag if dx > 0 else -mag
                sdy = mag if dy > 0 else -mag
            cx += sdx
            cy += sdy
            p = db.Point(cx, cy)
            if p != pts[-1]:
                pts.append(p)
        if len(pts) < 2:
            return []

        half = self._snap(int(round(width_um / 2.0 / dbu)))
        if half <= 0:
            return []
        # Offset components for a 45 degree run. Rounding the component, not
        # the resulting vertex, is what keeps the long edges exactly parallel
        # to the segment and therefore exactly at 45 degrees. Rounded UP, not
        # to nearest: half/sqrt(2) rounded down would draw the diagonal run
        # narrower than the nominal width and trip the minimum-width rule.
        g = max(1, self.grid_dbu)
        diag = int(math.ceil(half / math.sqrt(2.0) / g)) * g

        # Square ends: each free terminal vertex moves out by half along its
        # run, so each end carries w/2 of copper past its endpoint, as the
        # board (KiCad's round cap) and a reserved rectangle put it. Only the
        # first and last quad grow; the corner patches are drawn from the
        # interior vertices as before. Both moves read the original points,
        # so a two-point trace grows from its own ends.
        first, last = pts[0], pts[-1]
        if free[0]:
            first = self._extended(pts[0], pts[1], half, diag)
        if free[1]:
            last = self._extended(pts[-1], pts[-2], half, diag)
        pts[0], pts[-1] = first, last

        polys: List[db.Polygon] = []
        for a, b in zip(pts, pts[1:]):
            dx, dy = b.x - a.x, b.y - a.y
            ox, oy = self._quad_offset(dx, dy, half, diag)
            if dx and dy and abs(dx) != abs(dy):
                # Neither orthogonal nor 45: a discretized arc, or a board
                # drawn off the 45 grid. The angle rule will report it; draw
                # the nearest on-grid quad rather than silently reshaping the
                # copper to make a violation disappear.
                self._odd_angle_segments += 1
            polys.append(db.Polygon([
                db.Point(a.x + ox, a.y + oy),
                db.Point(b.x + ox, b.y + oy),
                db.Point(b.x - ox, b.y - oy),
                db.Point(a.x - ox, a.y - oy)]))

        # Corner patches: each closes the wedge the two neighbouring quads
        # leave open at an interior vertex, cut back to the flanks so the
        # copper stops where the trace does. See _corner_patch.
        for a, p, b in zip(pts, pts[1:], pts[2:]):
            polys.append(self._corner_patch(a, p, b, half, diag))
        return polys

    def add_path(self, points: List[Tuple[float, float]], width_um: float,
                 layer: str, *, free: Tuple[bool, bool] = (True, True)
                 ) -> List[db.Polygon]:
        """
        Add a trace. Points are in micrometers, width is in micrometers.
        Returns the copper drawn, as polygons in database units; empty when
        nothing was drawn.

        With the manufacturing grid active (the default) the trace is drawn as
        on-grid polygons; see _trace_polygons for why it cannot stay a single
        path. With snapping disabled it keeps a DPath with square ends, which is
        smaller and prettier in a viewer but fails the carrier's offgrid deck.
        ``free`` says which of the two ends get the w/2 square end.
        """
        if len(points) < 2:
            return []

        try:
            layer_idx = self._get_gds_layer(layer)
        except KeyError as e:
            print(f"Warning: {e} - skipping path on layer {layer}")
            return []

        if self.grid_dbu <= 1:
            dpoints = [db.DPoint(x, y) for x, y in points]
            half = width_um / 2.0
            path = db.DPath(dpoints, width_um, half if free[0] else 0.0,
                            half if free[1] else 0.0)
            self.routing_cell.shapes(layer_idx).insert(path)
            return [path.polygon().to_itype(self.layout.dbu)]

        polys = self._trace_polygons(points, width_um, free=free)
        for poly in polys:
            self.routing_cell.shapes(layer_idx).insert(poly)
        return polys

    def add_segments_as_paths(self, segments: List[TraceSegment], arcs: List[TraceArc] = None) -> int:
        """
        Convert segments and arcs to continuous paths and add to layout.
        Returns number of paths created. The copper drawn is kept per
        (layer, net) in ``trace_copper``, for refuse_touching_nets.
        """
        if arcs is None:
            arcs = []

        # Group by layer and width
        groups = self._build_trace_elements(segments, arcs)

        # Chain every group first: whether an end is free depends on the other
        # traces of its net on its layer, of every width. Drawn afterwards in
        # chaining order.
        traces: List[ChainedTrace] = []
        by_net: Dict[Tuple[str, str], List[ChainedTrace]] = {}
        for (net, layer, width), elements in groups.items():
            # Probe the layer once per group. Elements on layers the LYP
            # does not map are counted and skipped so the caller can fail
            # loudly when the board copper names do not match the PDK
            # metals (see the guard in convert_hyp_to_gds).
            try:
                self.layer_map.get_layer(layer)
            except KeyError as e:
                self._unmapped_layers[layer] = (
                    self._unmapped_layers.get(layer, 0) + len(elements))
                print(f"Warning: {e} - skipping {len(elements)} trace "
                      f"element(s)")
                continue
            self._mapped_trace_elements += len(elements)
            width_um = self._to_um(width)
            for points in self._connect_traces_to_paths(elements):
                trace = ChainedTrace(layer, net, width_um, points)
                traces.append(trace)
                by_net.setdefault((layer, net), []).append(trace)

        for members in by_net.values():
            for trace, free in zip(members,
                                   free_ends(members, self.layout.dbu)):
                trace.free = free

        total_paths = 0
        self.trace_copper = {}
        for t in traces:
            copper = self.trace_copper.setdefault((t.layer, t.net), [])
            polys = self.add_path(t.points, t.width_um, t.layer, free=t.free)
            if polys:
                copper.extend(polys)
                total_paths += 1

        return total_paths

    def _calculate_via_array(self, target_size_um: float, via_type: str) -> int:
        """
        Calculate number of vias (rows or columns) needed to approximate target size.

        Uses the formula: total_size = 2 * enc + n * size + (n-1) * sep
        Solving for n: n = ceil((target - 2*enc + sep) / (size + sep))

        Each length is rounded to the nearest dbu once (never truncated) and
        the quotient is ceil-divided in integers, so a target equal to the
        landing an n x n array draws gives exactly n.

        Args:
            target_size_um: Target pad size in micrometers
            via_type: Type of via ('Vn' for Via1-4, 'TV1' for TopVia1, 'TV2' for TopVia2)

        Returns:
            Number of vias needed (minimum 1)
        """
        params = self.PDK_VIA_PARAMS.get(via_type)
        if not params:
            return 1

        dbu = self.layout.dbu
        size, sep, enc, target = (int(round(v / dbu)) for v in (
            params['size'], params['sep'], params['enc'], target_size_um))

        # exact integer ceil
        n = -(-(target - 2 * enc + sep) // (size + sep))
        return max(1, n)  # Minimum 1 via

    def _get_via_layers_between(self, top_layer: str, bottom_layer: str) -> List[str]:
        """Get list of via layers needed between two metal layers."""
        try:
            top_idx = self.METAL_LAYERS.index(top_layer)
            bottom_idx = self.METAL_LAYERS.index(bottom_layer)
        except ValueError:
            return []

        if top_idx < bottom_idx:
            top_idx, bottom_idx = bottom_idx, top_idx

        via_layers = []
        for i in range(bottom_idx, top_idx):
            lower = self.METAL_LAYERS[i]
            upper = self.METAL_LAYERS[i + 1]
            key = (lower, upper)
            if key in self.VIA_LAYERS:
                via_layers.append(self.VIA_LAYERS[key])

        return via_layers

    def _get_or_create_via_pcell(self, padstack: Padstack) -> Optional[Tuple[db.Cell, db.Cell]]:
        """Get or create a via PCell for the given padstack."""
        if not self._pcells_available:
            return None

        # Filter padstack layers to only include valid metal layers
        # This handles cases like "MDEF" which are not real PDK layers
        valid_layers = [layer for layer in padstack.layers if layer in self.METAL_LAYERS]

        if len(valid_layers) < 2:
            # Need at least 2 metal layers to create a via stack
            if padstack.layers != valid_layers:
                print(f"  Skipping via: padstack has no valid metal layers (got: {padstack.layers})")
            return None

        # Calculate target size in micrometers from padstack pad dimensions
        # Use the larger of width/height (assuming square approximation)
        target_size_um = max(
            self._to_um(padstack.pad_width),
            self._to_um(padstack.pad_height)
        )

        # Calculate number of vias for each type
        vn_count = self._calculate_via_array(target_size_um, 'Vn')
        tv1_count = self._calculate_via_array(target_size_um, 'TV1')
        tv2_count = self._calculate_via_array(target_size_um, 'TV2')

        # Sort layers by stackup position (top to bottom) to handle unordered padstack layers
        # This ensures we correctly identify top and bottom layers regardless of HYP order
        if self.stackup_order:
            sorted_layers = sorted(valid_layers,
                key=lambda x: self.stackup_order.index(x) if x in self.stackup_order else 999)
        else:
            # Fallback to METAL_LAYERS order if no stackup defined
            sorted_layers = sorted(valid_layers,
                key=lambda x: self.METAL_LAYERS.index(x) if x in self.METAL_LAYERS else 999)

        # Create cache key from sorted layers AND via counts
        cache_key = f"{'_'.join(sorted_layers)}_{vn_count}_{tv1_count}_{tv2_count}"
        if cache_key in self._via_cells:
            group_cell = self._get_or_create_via_group(sorted_layers)
            return (self.layout.cell(self._via_cells[cache_key]), group_cell)

        # Determine top and bottom layers from sorted layers
        # sorted_layers is in stackup order (top to bottom)
        t_layer = sorted_layers[0]   # Top layer (first in stackup)
        b_layer = sorted_layers[-1]  # Bottom layer (last in stackup)

        try:
            via_cell = self.layout.create_cell("via_stack", "SG13_dev", {
                "b_layer": b_layer,
                "t_layer": t_layer,
                "vn_columns": vn_count,
                "vn_rows": vn_count,
                "vt1_columns": tv1_count,
                "vt1_rows": tv1_count,
                "vt2_columns": tv2_count,
                "vt2_rows": tv2_count,
            })
            via_cell.name = f"VIA_{'_'.join(sorted_layers)}_{vn_count}x{tv1_count}x{tv2_count}"
            self._via_cells[cache_key] = via_cell.cell_index()
            print(f"  Created via_stack: {b_layer}->{t_layer}, "
                  f"target={target_size_um:.1f}µm, "
                  f"vn={vn_count}x{vn_count}, tv1={tv1_count}x{tv1_count}, tv2={tv2_count}x{tv2_count}")
            group_cell = self._get_or_create_via_group(sorted_layers)
            return (via_cell, group_cell)
        except Exception as e:
            print(f"Warning: Could not create via PCell: {e}")
            return None

    @_intm4tm2_scope()
    def _bootstrap_intm4tm2_pcells(self) -> bool:
        """Register the IntM4TM2 PCell library for headless PCell creation.

        Binding the layout to the 'intm4tm2' technology is what makes the
        create_cell("cmim", "IntM4TM2", ...) lookup resolve, exactly as
        _bootstrap_sg13g2_pcells does for SG13_dev. The binding is layout-wide
        and mutually exclusive with the sg13g2 one: rebinding invalidates the
        library proxy cells created under the previous technology, which is why
        the via cache holds cell indices rather than db.Cell handles and why
        CMIM placement must stay after every SG13G2 PCell has been created.
        """
        root = _discover_path_var("INTERPOSER_PDK_ROOT")
        if not root:
            print("Warning: INTERPOSER_PDK_ROOT not found; cannot place CMIM PCells",
                  file=sys.stderr)
            return False

        klayout_root = Path(root) / "libs.tech" / "klayout"
        python_dir = klayout_root / "python"
        cni_dir = python_dir / "pycell4klayout-api" / "source" / "python"

        for entry in (str(python_dir), str(cni_dir)):
            if entry not in sys.path:
                sys.path.insert(0, entry)

        try:
            if "intm4tm2" not in db.Technology.technology_names():
                tech = db.Technology.create_technology("intm4tm2")
                lyt = klayout_root / "tech" / "intm4tm2.lyt"
                if lyt.is_file():
                    tech.load(str(lyt))

            if "IntM4TM2" not in db.Library.library_names():
                import intm4tm2_pycell_lib  # noqa: F401

            self.layout.technology_name = "intm4tm2"
            return True
        except (Exception, SystemExit) as exc:
            print(f"Warning: IntM4TM2 PCell bootstrap failed: {exc}",
                  file=sys.stderr)
            return False

    def _intm4tm2_tech(self) -> Dict[str, float]:
        """Interposer technology constants used to place and bound a cmim.

        Read from the PCell library's own tech parameters so the numbers stay
        the PDK's to define; _INTM4TM2_TECH_FALLBACK applies only when the
        checkout cannot be resolved. Keys: grid_um, min_lw_um, max_lw_um,
        max_c_fF, area_fF_per_um2, perim_fF_per_um. The two capacitance
        coefficients follow load_tech in cmim_footprint_gen.py.
        """
        if self._intm4tm2_tech_cache is not None:
            return self._intm4tm2_tech_cache
        tech = dict(_INTM4TM2_TECH_FALLBACK)
        root = _discover_path_var("INTERPOSER_PDK_ROOT")
        if root:
            tech_json = (Path(root) / "libs.tech" / "klayout" / "python" /
                         "intm4tm2_pycell_lib" / "intm4tm2_tech.json")
            try:
                with open(tech_json, "r") as f:
                    params = json.load(f)["techParams"]
                loaded = {
                    "grid_um": _si_num(params["grid"]),
                    "min_lw_um": _si_num(params["cmim_minLW"]) * 1e6,
                    "max_lw_um": _si_num(params["cmim_maxLW"]) * 1e6,
                    "max_c_fF": _si_num(params["cmim_maxC"]) * 1e15,
                    "area_fF_per_um2": _si_num(params["cmim_caspec"]) * 1e3,
                    "perim_fF_per_um": _si_num(params["cmim_cpspec"]) * 1e9,
                }
                if all(v > 0.0 for v in loaded.values()):
                    tech = loaded
            except (OSError, json.JSONDecodeError, KeyError, TypeError,
                    ValueError):
                pass
        self._intm4tm2_tech_cache = tech
        return tech

    def _cmim_out_of_range(self, tech: Dict[str, float], w_um: float,
                           l_um: float, m: int) -> str:
        """Why this cmim cannot be built, or "" when it can.

        The PCell must never be asked for a plate it rejects. It does not
        signal refusal by returning None: it hands back a nameless, empty cell
        that would be written into the GDS as an empty STRNAME record, which
        makes the whole file unreadable. Worse, a w/l large enough to pass its
        own coercion sends its via loop, which is O(w*l), into an unbounded
        allocation. Both are the same input class, so both are refused here.

        The capacitance is what actually bounds the device (the loop count
        scales with the product, not the side), so an in-spec rectangle such
        as 100 x 50 um stays legal while the metres/micrometres mix-up that
        asks for 8.11e6 um does not.
        """
        for name, value in (("w", w_um), ("l", l_um)):
            if not math.isfinite(value):
                return "%s=%r is not a finite number" % (name, value)
            if value < tech["min_lw_um"]:
                return ("%s=%g um is below the device minimum %g um"
                        % (name, value, tech["min_lw_um"]))
            if value > tech["max_lw_um"]:
                return ("%s=%g um is above the device maximum %g um"
                        % (name, value, tech["max_lw_um"]))
        cap_fF = m * (w_um * l_um * tech["area_fF_per_um2"]
                      + 2.0 * (w_um + l_um) * tech["perim_fF_per_um"])
        if cap_fF > tech["max_c_fF"]:
            return ("%g x %g um (m=%d) is %.4g fF, above the device maximum "
                    "%.4g fF" % (w_um, l_um, m, cap_fF, tech["max_c_fF"]))
        return ""

    @_intm4tm2_scope()
    def add_cmim_devices(self, devices: List[Dict]) -> Tuple[int, List[str]]:
        """Place cap_cmim devices from a sidecar entry list via the IntM4TM2 PCell.

        `devices` comes from load_cmim_devices(). Returns (placed count, refs
        that could not be placed); a non-empty second element is an incomplete
        interposer and the caller is expected to fail the run over it.
        """
        if not devices:
            return 0, []

        refs = [str(d.get("ref", "?")) if isinstance(d, dict) else "?"
                for d in devices]

        if not self._bootstrap_intm4tm2_pcells():
            return 0, refs
        try:
            _check_intm4tm2_cni("cmim")
        except IntM4TM2Refused as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 0, refs

        tech = self._intm4tm2_tech()
        grid = tech["grid_um"]
        group = None
        placed = 0
        skipped: List[str] = []

        for item in devices:
            ref = item.get("ref", "?") if isinstance(item, dict) else "?"
            if not isinstance(item, dict):
                print(f"  Warning: skipping non-object cmim_devices entry: "
                      f"{item!r}", file=sys.stderr)
                skipped.append(str(ref))
                continue

            try:
                x = float(item["x_um"])
                y = float(item["y_um"])
                w_um = _cmim_length_um(item, "w")
                l_um = _cmim_length_um(item, "l")
                m = int(float(item.get("m", 1)))
            except (KeyError, TypeError, ValueError) as exc:
                print(f"  Warning: skipping CMIM {ref}: invalid parameters ({exc})",
                      file=sys.stderr)
                skipped.append(str(ref))
                continue

            if w_um <= 0.0 or l_um <= 0.0 or m <= 0:
                bad = []
                if w_um <= 0.0:
                    bad.append(f"w={w_um:g}um")
                if l_um <= 0.0:
                    bad.append(f"l={l_um:g}um")
                if m <= 0:
                    bad.append(f"m={m}")
                print(f"  Warning: skipping CMIM {ref}: non-positive parameter(s) "
                      f"({', '.join(bad)})", file=sys.stderr)
                skipped.append(str(ref))
                continue

            out_of_range = self._cmim_out_of_range(tech, w_um, l_um, m)
            if out_of_range:
                print(f"  Warning: skipping CMIM {ref}: {out_of_range}",
                      file=sys.stderr)
                skipped.append(str(ref))
                continue

            # The PCell takes its dimensions in meters (Numeric(w) * 1e6 in
            # the library's setupParams); the sidecar carries micrometers.
            params = {
                "w": w_um * 1e-6,
                "l": l_um * 1e-6,
                "m": m,
                "Calculate": "C",
            }

            cell = self.layout.create_cell("cmim", "IntM4TM2", params)
            # Not just None: a PCell that refuses its parameters hands back a
            # nameless empty cell, which the GDS writer emits as an empty
            # STRNAME record and makes the whole file unreadable. The range
            # guard above should have caught every such input; this is the
            # backstop that keeps a broken cell out of the layout regardless.
            if cell is None or not cell.name or cell.bbox().empty():
                print(f"  Warning: failed to create CMIM PCell for {ref} "
                      f"({w_um:g} x {l_um:g} um, m={m})", file=sys.stderr)
                if cell is not None:
                    self.layout.delete_cell(cell.cell_index())
                skipped.append(str(ref))
                continue

            # The footprint position is the center of the MIM plate; the PCell
            # draws that plate from its own origin (Box(0, 0, w, l)), so the
            # instance origin is the plate's lower-left corner. Only that origin
            # is snapped: the PCell owns the device geometry.
            angle = 0.0
            try:
                angle = float(item.get("rotation_deg", 0.0))
            except (TypeError, ValueError):
                angle = 0.0
            # Rotation is about the plate center, so offset the lower-left
            # corner in the rotated frame (same convention as the die
            # placement path, which also uses DCplxTrans about the anchor).
            trans = db.DCplxTrans(1.0, angle, False, db.DVector(x, y)) * \
                db.DCplxTrans(1.0, 0.0, False,
                              db.DVector(-w_um / 2.0, -l_um / 2.0))
            if not angle:
                trans = db.DCplxTrans(
                    1.0, 0.0, False,
                    db.DVector(round((x - w_um / 2.0) / grid) * grid,
                               round((y - l_um / 2.0) / grid) * grid))

            if group is None:
                group = self.layout.create_cell("CMIM_DEVICES")
                self.routing_cell.insert(db.DCellInstArray(group, db.DTrans()))

            group.insert(db.DCellInstArray(cell, trans))

            placed += 1
            print(f"  Placed CMIM {ref} at ({x:g}, {y:g}) um "
                  f"({w_um:g} x {l_um:g} um" +
                  (f", {angle:g} deg)" if angle else ")"))

        return placed, skipped

    # Via layer name -> PDK_VIA_PARAMS key. 'Vn' covers the standard
    # vias (Via1..Via4); the two top vias have their own geometry class.
    _VIA_PARAM_KEY = {
        'Via4': 'Vn',
        'TopVia1': 'TV1',
        'TopVia2': 'TV2',
    }

    def _create_simple_via(self, via: Via, padstack: Padstack) -> bool:
        """Create a via stack from plain rectangles (PCell fallback).

        Geometry honors PDK_VIA_PARAMS (interposer_tech_default.json, or
        the sg13g2 defaults): each via level gets an n x n array of
        PDK-sized cuts -- same array formula as the PCell path, see
        _calculate_via_array -- instead of one oversized rectangle, and
        every metal level of the span gets a landing pad enclosing its
        adjacent arrays by the PDK enclosure (cuts without a landing pad
        would be floating enclosure violations).
        """
        # Filter padstack layers to only include valid metal layers
        valid_layers = [layer for layer in padstack.layers if layer in self.METAL_LAYERS]

        if len(valid_layers) < 2:
            # Need at least 2 metal layers to create a via stack
            return False

        # Sort layers by stackup position (top to bottom)
        if self.stackup_order:
            sorted_layers = sorted(valid_layers,
                key=lambda x: self.stackup_order.index(x) if x in self.stackup_order else 999)
        else:
            sorted_layers = sorted(valid_layers,
                key=lambda x: self.METAL_LAYERS.index(x) if x in self.METAL_LAYERS else 999)

        # Negate Y to mirror from HYP to GDS
        x_um = self._to_um(via.x)
        y_um = self._to_um_y(via.y)

        # Same target the PCell path aims for: the padstack pad size.
        target_size_um = max(
            self._to_um(padstack.pad_width),
            self._to_um(padstack.pad_height),
        )

        # Get via layers needed using sorted layers
        t_layer = sorted_layers[0]   # Top layer
        b_layer = sorted_layers[-1]  # Bottom layer
        via_layer_names = self._get_via_layers_between(t_layer, b_layer)
        if t_layer != b_layer and not via_layer_names:
            # A real metal transition that no VIA_LAYERS pair covers (a PDK
            # ladder gap). Drawing the landing pads anyway leaves them
            # electrically floating with no cuts, so refuse loudly instead.
            print(f"Warning: no via layers map the {t_layer} <-> {b_layer} "
                  f"transition (PDK ladder gap); skipping this via rather than "
                  f"drawing floating landing pads.", file=sys.stderr)
            return False

        # n x n arrays of PDK-sized cuts per via level.
        # arrays: via layer -> (array extent, metal enclosure) for pads.
        arrays: Dict[str, Tuple[float, float]] = {}
        for via_layer in via_layer_names:
            param_key = self._VIA_PARAM_KEY.get(via_layer, 'Vn')
            params = self.PDK_VIA_PARAMS.get(param_key)
            if not params:
                continue
            size = params['size']
            sep = params['sep']
            n = self._calculate_via_array(target_size_um, param_key)
            extent = n * size + (n - 1) * sep
            arrays[via_layer] = (extent, params['enc'])
            try:
                layer_idx = self._get_gds_layer(via_layer)
            except KeyError:
                continue  # Skip if layer not found
            origin = -extent / 2.0 + size / 2.0
            pitch = size + sep
            for row in range(n):
                cy = y_um + origin + row * pitch
                for col in range(n):
                    cx = x_um + origin + col * pitch
                    box = db.DBox(cx - size / 2.0, cy - size / 2.0,
                                  cx + size / 2.0, cy + size / 2.0)
                    self.routing_cell.shapes(layer_idx).insert(box)

        # Landing pads on every metal of the span (the PCell draws the
        # intermediate metals too, even when the padstack omits them).
        try:
            t_idx = self.METAL_LAYERS.index(t_layer)
            b_idx = self.METAL_LAYERS.index(b_layer)
        except ValueError:
            return False
        span_metals = self.METAL_LAYERS[min(b_idx, t_idx):max(b_idx, t_idx) + 1]
        for metal_layer in span_metals:
            side = 0.0
            for pair, via_name in self.VIA_LAYERS.items():
                if metal_layer in pair and via_name in arrays:
                    extent, enc = arrays[via_name]
                    side = max(side, extent + 2.0 * enc)
            if side <= 0.0:
                # No adjacent via geometry (e.g. pair outside VIA_LAYERS):
                # keep a pad at the padstack size so the landing exists.
                side = target_size_um
            if side <= 0.0:
                continue
            half = side / 2.0
            try:
                layer_idx = self._get_gds_layer(metal_layer)
            except KeyError:
                continue  # Skip if layer not found
            box = db.DBox(x_um - half, y_um - half, x_um + half, y_um + half)
            self.routing_cell.shapes(layer_idx).insert(box)

        return True

    def add_via(self, via: Via, padstack: Padstack) -> bool:
        """
        Add a via to the layout.

        Uses PDK PCells if available, otherwise falls back to simple rectangles.

        Returns True if successful, False otherwise.
        """
        # Negate Y to mirror from HYP to GDS
        x_um = self._to_um(via.x)
        y_um = self._to_um_y(via.y)

        # Try to use PCell first
        if self._pcells_available:
            result = self._get_or_create_via_pcell(padstack)
            if result:
                via_cell, group_cell = result
                trans = db.DTrans(db.DVector(x_um, y_um))
                group_cell.insert(db.DCellInstArray(via_cell, trans))
                return True

        # Fallback to simple rectangles
        return self._create_simple_via(via, padstack)

    def _place_die_flipped(self, template_cell: db.Cell, wrapper_name: str,
                           rotation: float = 0.0) -> db.Cell:
        """Wrap the template cell with a mirror-X (face-down) flattened copy.

        Instantiates the template with M180 (mirror around Y-axis: negate X)
        and flattens to a single cell so downstream EM/thermal tools see
        correct per-layer geometry without instance-level mirroring. Layer
        numbers are preserved; z-inversion of the BEOL stack is not captured
        in GDS and must be handled via the stackup YAML downstream.

        Args:
            template_cell: Imported GDS template cell (shared, read-only).
            wrapper_name: Name for the new wrapper cell.
            rotation: Extra rotation in degrees, baked into the same transform.
        """
        wrapper = self.layout.create_cell(wrapper_name)
        # M180 = rotate 180 deg + mirror=True => net: negate X only.
        flip = db.DCplxTrans(1.0, rotation + 180.0, True, db.DVector(0, 0))
        wrapper.insert(db.DCellInstArray(template_cell.cell_index(), flip))
        # prune=False: keep template cell so add_device() can reuse it for
        # other instances of the same chiplet via _imported_templates cache.
        wrapper.flatten(-1, False)
        return wrapper

    def add_device(self, device: Device, flip_chip: bool = False,
                   outline: Optional["_die_outline.DieOutline"] = None) -> bool:
        """
        Add a device/chiplet by loading its GDS file and instantiating it.

        Args:
            device: Device with GDS file path and position
            outline: The die's declared outline, read from this same GDS
                     (die_outline_gds). When given, it is the die's boundary
                     in the boundary manifest; when None (an exempt die, or a
                     device that is not a die of the .chiplet) the boundary is
                     the placed cell's bbox over all layers.

        Returns:
            True if successful, False otherwise.
        """
        if not device.gds_file:
            print(f"Warning: Device {device.ref} has no GDS_FILE")
            return False

        # GDS_FILE values flow verbatim from the board into the .hyp; they
        # may carry ${VAR} ecosystem-root references (expanded here, on read).
        gds_path = Path(_expand_path_vars(device.gds_file))
        if not gds_path.exists():
            print(f"Warning: GDS file not found: {gds_path}")
            return False

        try:
            # Convert device position to micrometers
            # HYP now exports Y consistently for both wires and devices
            # (KiCad exporter bug fixed: Y is negated for all elements)
            x_um = self._to_um(device.x)
            # _to_um_y (not _to_um) so the device Y reflection matches the
            # traces/vias; a no-op in METRIC (the only mode the writer emits),
            # correct in ENGLISH where the die would otherwise be mirrored.
            y_um = self._to_um_y(device.y)

            # Get expected cell name from GDS filename (without extension)
            expected_cell_name = gds_path.stem
            # Ready dies can each name their independent source layout.gds.
            # Keep the stem for cell/boundary labels, but identify a template
            # by its resolved file path so one die cannot reuse another's art.
            template_key = str(gds_path.resolve())

            # Import the GDS if we haven't seen this file before.
            # The first import creates a "template" cell; subsequent devices
            # using the same GDS reuse the template's subcells without
            # re-reading the file.
            if not hasattr(self, '_imported_templates'):
                self._imported_templates: Dict[str, db.Cell] = {}

            if template_key not in self._imported_templates:
                # Track existing cells before import
                existing_cells = set(cell.name for cell in self.layout.each_cell())

                # Read the external GDS directly into the current layout.
                #
                # The conflict policy has to be stated. KLayout's default is
                # AddToCell: when an incoming cell has the name of one already
                # here, its shapes are added INTO the existing cell. Two dies
                # from different vendors that happen to share a cell name (a
                # standard-cell library, a "SEAL", a "TOP") therefore end up
                # sharing ONE merged cell, so each die acquires the other's
                # geometry. Both are wrong, not just one: measured on a
                # two-cell case, each die's shared cell carries 3 shapes where
                # its own file has 2.
                #
                # It can still present as "one die is wrong", which is how it
                # was first reported, and that is a property of the comparison
                # rather than of the merge. Compare by area and a die whose
                # shared box contains the other's looks innocent, because the
                # union is unchanged. Compare by shape count and both fail.
                #
                # Nothing downstream notices, because the assembly DRC checks
                # the assembly and not whether each die still equals its file.
                #
                # RenameCell keeps every die's hierarchy to itself. The cost is
                # duplicated cells in the complete GDS, which is the honest cost
                # of two independent dies that happen to agree on a name.
                read_options = db.LoadLayoutOptions()
                read_options.cell_conflict_resolution = (
                    db.LoadLayoutOptions.CellConflictResolution.RenameCell)
                # Reading a file with another dbu into this layout switches
                # the layout's dbu and rescales nothing, so every shape
                # already here (the whole interposer) changes size. Nothing
                # written from this layout can be right after that; the die
                # outline read refuses such a die before anything is written,
                # and a device it does not read fails the run here.
                dbu_before = self.layout.dbu
                self.layout.read(str(gds_path), read_options)
                if self.layout.dbu != dbu_before:
                    raise DieGdsError(_die_outline.dbu_mismatch(
                        str(gds_path), Decimal(repr(self.layout.dbu)),
                        Decimal(repr(dbu_before))))

                # Find newly imported cells (cells that didn't exist before)
                new_cells = [cell for cell in self.layout.each_cell() if cell.name not in existing_cells]

                if not new_cells:
                    print(f"Warning: No new cells imported from {device.gds_file}")
                    return False

                # The top cell among the new cells: the first one no other new
                # cell instantiates. The die outline read resolves a die's top
                # cell with the same function (and requires exactly one), so
                # the two cannot name different cells.
                parentless = _die_outline.parentless_cells(new_cells)
                template_cell = parentless[0] if parentless else new_cells[0]

                self._imported_templates[template_key] = template_cell

            template_cell = self._imported_templates[template_key]
            # A third-party die is delivered as drawn. Its geometry and its
            # placement stay out of the manufacturing-grid pass: reshaping
            # someone else's layout is not the exporter's business, and the
            # placement is the design intent the boundary manifest records.
            self._foreign_cells.add(template_cell.cell_index())

            if flip_chip:
                # Flip-chip: extract geometry per-layer with mirror-X transform.
                # This flattens the template hierarchy so EM/thermal tools see
                # correct per-layer geometry without instance-level mirroring.
                wrapper_name = f"{device.ref}_{expected_cell_name}_flipped"
                imported_cell = self._place_die_flipped(
                    template_cell, wrapper_name, rotation=device.rotation)
                # The wrapper holds the die's own geometry, flattened; it is
                # as foreign as the template it came from.
                self._foreign_cells.add(imported_cell.cell_index())
                # Place with translation only -- mirror is baked into geometry
                trans = db.DTrans(db.DVector(x_um, y_um))
            else:
                # Non-flip: keep hierarchical instance (shared subcells)
                instance_name = f"{device.ref}_{expected_cell_name}"
                imported_cell = self.layout.create_cell(instance_name)
                imported_cell.insert(db.DCellInstArray(template_cell, db.DTrans()))
                if device.rotation != 0.0:
                    trans = db.DCplxTrans(1.0, device.rotation, False, db.DVector(x_um, y_um))
                else:
                    trans = db.DTrans(db.DVector(x_um, y_um))

            # Insert the device cell as an instance
            self.top_cell.insert(db.DCellInstArray(imported_cell, trans))

            # Record the chiplet mechanical boundary for the assembly DRC.
            # This is ADK assembly metadata, not fabrication geometry: it is
            # written to the <gds>.boundaries.json manifest (see write()) and
            # lives in NO PDK layer namespace, so it cannot alias a process
            # layer or the chiplet's own internal geometry.
            #
            # A declared die outline is the boundary. It is a box in the die
            # top cell's own frame; its four corners go through the flip-chip
            # wrapper's mirror and rotation (the transform _place_die_flipped
            # used) when there is one, then through the placement, so at any
            # rotation the boundary is the outline where it lands, not a
            # bbox of it. Without an outline the boundary is the cell bbox,
            # over every layer and the whole hierarchy.
            cell_bbox = imported_cell.dbbox()
            if outline is not None:
                box = outline.box_um()
                place = db.DCplxTrans(trans)
                if flip_chip:
                    place = place * db.DCplxTrans(
                        1.0, device.rotation + 180.0, True, db.DVector(0, 0))
                boundary_poly = db.DPolygon([place.trans(pt) for pt in (
                    db.DPoint(box.left, box.bottom),
                    db.DPoint(box.right, box.bottom),
                    db.DPoint(box.right, box.top),
                    db.DPoint(box.left, box.top))])
            elif not flip_chip and device.rotation != 0.0:
                # Non-flip with rotation: transform bbox corners through DCplxTrans
                corners = [
                    db.DPoint(cell_bbox.left, cell_bbox.bottom),
                    db.DPoint(cell_bbox.right, cell_bbox.bottom),
                    db.DPoint(cell_bbox.right, cell_bbox.top),
                    db.DPoint(cell_bbox.left, cell_bbox.top),
                ]
                boundary_poly = db.DPolygon([trans.trans(pt) for pt in corners])
            else:
                # Flip-chip (translation-only) or no rotation: transformed bbox works
                boundary_poly = db.DPolygon(cell_bbox.transformed(trans))
            self._record_boundary(device.ref, expected_cell_name, boundary_poly,
                                  x_um, y_um, device.rotation, flip_chip)

            mirror_str = " [mirror-X, flip-chip, per-layer]" if flip_chip else ""
            print(f"  Added device {device.ref}: {gds_path.name} ({imported_cell.name}) at anchor ({x_um:.2f}, {y_um:.2f}) um{mirror_str}")
            return True

        except DieGdsError:
            # The layout's dbu changed under the read: the caller has to stop
            # rather than write it, which a False here would not make it do.
            raise
        except Exception as e:
            print(f"Error loading device GDS {device.gds_file}: {e}")
            import traceback
            traceback.print_exc()
            return False

    def get_top_cell_bbox(self) -> Tuple[float, float, float, float]:
        """Get bounding box of top cell in micrometers.

        Returns:
            (x_min, y_min, width, height) in micrometers
        """
        bbox = self.top_cell.dbbox()  # DBox in micrometers (since we use dbu=0.001)
        return (bbox.left, bbox.bottom, bbox.width(), bbox.height())

    # Board outline layer: prBoundary.drawing in the interposer PDK layer
    # table (same convention as SG13G2). Drawn from the .hyp BOARD perimeter
    # (= KiCad Edge.Cuts); read back by update_chiplet_file to size the
    # interposer component from the fab outline instead of the drawn-copper
    # extent. A future assembly containment rule (chiplet inside interposer)
    # reads the same layer.
    # 235/0 since the 2026-07-16 IntM4TM2 layer-map parity migration
    # (was 189/0; GDS produced before then carry the outline on 189/0).
    PRBOUNDARY_LAYER = (235, 0)

    # prBoundary layers this plugin drew before the 189->235 migration. Only
    # used to enrich the loud "outline missing" warning in update_chiplet_file:
    # if the current layer is empty but geometry sits on one of these, the GDS
    # is pre-migration and just needs regenerating.
    LEGACY_PRBOUNDARY_LAYERS = ((189, 0),)

    # No-fill (metal density fill keep-out) targets. The user authors keep-outs
    # on dedicated KiCad layers (writers/chiplet_writer.NOFILL_LAYER_ROLES); the
    # role -> GDS mapping lives here, next to the rest of the GDS contract. The
    # PDK fill generators already subtract 160/0 (global) and <metal>/23 from
    # the fill region, so stamping these is all that is needed.
    NOFILL_GLOBAL_LAYER = (160, 0)          # NoMetFiller: blanket keep-out
    NOFILL_ROLE_TO_METAL = {"M4": "Metal4", "M5": "Metal5",
                            "TM1": "TopMetal1", "TM2": "TopMetal2"}
    NOFILL_DATATYPE = 23                     # <metal>/23 = per-metal nofill

    def add_board_outline(self, perimeter_segments: List[PerimeterSegment]) -> int:
        """Draw the board outline on prBoundary (235/0).

        Chains the BOARD-section PERIMETER_SEGMENTs into closed loops by
        matching endpoints and inserts one polygon per loop. All-or-nothing:
        if any segment fails to chain into a closed loop, nothing is drawn
        and a loud warning is printed -- a partial outline would understate
        the bbox, which is worse than the drawn-geometry fallback.

        Returns:
            Number of closed loops drawn (0 = nothing drawn).
        """
        if not perimeter_segments:
            return 0

        # Endpoint match tolerance in um. The writers emit %.9f in meters
        # (1e-3 um quantization); 5 nm is far below any real outline feature.
        tol = 0.005

        def _close(p, q):
            return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol

        remaining = []
        for seg in perimeter_segments:
            a = (self._to_um(seg.x1), self._to_um_y(seg.y1))
            b = (self._to_um(seg.x2), self._to_um_y(seg.y2))
            if not _close(a, b):  # drop degenerate zero-length segments
                remaining.append((a, b))

        loops = []
        while remaining:
            a, b = remaining.pop(0)
            pts = [a, b]
            while True:
                if _close(pts[-1], pts[0]):
                    pts.pop()  # drop the closing duplicate
                    break
                for i, (p, q) in enumerate(remaining):
                    if _close(p, pts[-1]):
                        pts.append(q)
                        remaining.pop(i)
                        break
                    if _close(q, pts[-1]):
                        pts.append(p)
                        remaining.pop(i)
                        break
                else:
                    print(f"Warning: board outline does not close "
                          f"({len(perimeter_segments)} perimeter segments, "
                          f"open end at ({pts[-1][0]:.2f}, {pts[-1][1]:.2f}) "
                          f"um); prBoundary not drawn", file=sys.stderr)
                    return 0
            if len(pts) < 3:
                print(f"Warning: degenerate board outline loop "
                      f"({len(pts)} points); prBoundary not drawn",
                      file=sys.stderr)
                return 0
            loops.append(pts)

        layer_idx = self.layout.layer(*self.PRBOUNDARY_LAYER)
        for pts in loops:
            poly = db.DSimplePolygon([db.DPoint(x, y) for (x, y) in pts])
            self.top_cell.shapes(layer_idx).insert(poly)
        return len(loops)

    def _nofill_target(self, role: str) -> Optional[Tuple[int, int]]:
        """(layer, datatype) for a no-fill role, or None if unmapped.

        'global' -> NoMetFiller 160/0. Per-metal roles resolve to <metal>/23:
        prefer the LYP's explicit ``<metal>.nofill`` purpose, and derive it from
        the drawn-metal layer number when the LYP does not declare it, so a
        metal rename in the PDK cannot silently drift the mapping.
        """
        if role == "global":
            return self.NOFILL_GLOBAL_LAYER
        metal = self.NOFILL_ROLE_TO_METAL.get(role)
        if metal is None:
            return None
        try:
            return self.layer_map.get_layer(metal, "nofill")
        except KeyError:
            pass
        try:
            num, _dt = self.layer_map.get_layer(metal)
        except KeyError:
            return None
        return (num, self.NOFILL_DATATYPE)

    def add_nofill_regions(self, records: List[Dict]) -> int:
        """Paint KiCad-authored no-fill keep-outs onto the GDS keep-out layers.

        role 'global' -> NoMetFiller 160/0 (all metals); per-metal roles ->
        <metal>/23. Must be called before write() so the manufacturing-grid
        snap normalizes the polygons alongside the drawn geometry.

        Returns the number of polygons inserted.
        """
        n = 0
        for rec in records or []:
            role = rec.get("role")
            ring = rec.get("polygon_um") or []
            if len(ring) < 3:
                continue
            target = self._nofill_target(role)
            if target is None:
                print("Warning: no-fill region with unknown role %r skipped"
                      % (role,), file=sys.stderr)
                continue
            layer_idx = self.layout.layer(*target)
            poly = db.DSimplePolygon(
                [db.DPoint(float(x), float(y)) for (x, y) in ring])
            self.top_cell.shapes(layer_idx).insert(poly)
            n += 1
        return n

    # Seal ring (opt-in, --seal-ring). The PDK's sealring PCell takes the
    # whole outline as l x w, draws it on EdgeSeal.boundary and puts the ring
    # inside it, origin at the lower-left corner. The deck flags every layer
    # outside 39/4, prBoundary included, so 39/4 has to be the board outline
    # itself, and what the ring must enclose is read from the ring's EdgeSeal.
    SEAL_RING_LIBRARY = "IntM4TM2"
    SEAL_RING_PCELL = "sealring"
    SEAL_BOUNDARY_LAYER = (39, 4)        # EdgeSeal.boundary
    SEAL_EDGESEAL_LAYER = (39, 0)        # EdgeSeal.drawing; its hole is the interior

    # The fabricated layers the sealring deck requires inside the ring
    # (SEAL.REQ.4), plus Passiv, which it allows outside only as a closed loop
    # around EdgeSeal: the ring's own loop is that loop, anything the export
    # draws is not. Each maps to the notch-heal space key its metal is held to
    # from the ring's metal. None means one grid step: the shape may not reach
    # the band, and the spacing proper is the full deck's to judge.
    SEAL_RING_CLEARANCE = {
        (9, 0): None,       # Passiv
        (9, 35): None,      # Passiv pillar
        (9, 36): None,      # Passiv sbump
        (9, 40): None,      # Passiv pdl
        (36, 0): None,      # MIM
        (50, 0): 'Mn_b',    # Metal4
        (50, 20): None,
        (50, 22): None,
        (50, 26): None,
        (66, 0): None,      # Via4
        (67, 0): 'Mn_b',    # Metal5
        (67, 20): None,
        (67, 22): None,
        (67, 26): None,
        (125, 0): None,     # TopVia1
        (126, 0): 'TM1_b',  # TopMetal1
        (126, 20): None,
        (126, 22): None,
        (129, 0): None,     # Vmim
        (133, 0): None,     # TopVia2
        (134, 0): 'TM2_b',  # TopMetal2
        (134, 20): None,
        (134, 22): None,
        (146, 0): None,     # ThinFilmRes
        (157, 0): None,     # LBE
    }

    def _seal_ring_outline(self) -> 'db.Box':
        """The board outline as the one on-grid rectangle the ring can take.

        Read from what add_board_outline drew, with its vertices put on the
        grid the way write() will put them, so the ring and the written
        prBoundary are the same rectangle. Refuses anything else: the PCell
        draws a rectangular ring only, and a ring that does not follow the
        outline leaves the outline outside 39/4.
        """
        li = self.layout.find_layer(*self.PRBOUNDARY_LAYER)
        polys = [] if li is None else list(
            db.Region(self.top_cell.shapes(li)).each())
        pl, pd = self.PRBOUNDARY_LAYER
        if not polys:
            raise SealRingRefused(
                "the board has no outline on prBoundary %d/%d (Edge.Cuts did "
                "not give one closed loop)" % (pl, pd))
        if len(polys) > 1:
            raise SealRingRefused(
                "the board outline is %d loops on prBoundary %d/%d; the ring "
                "needs exactly one" % (len(polys), pl, pd))
        poly = db.Polygon([self._snap_point(p)
                           for p in polys[0].each_point_hull()])
        if polys[0].holes() or not poly.is_box():
            raise SealRingRefused(
                "the board outline is not an axis-aligned rectangle (%d "
                "vertices); the sealring PCell draws a rectangular ring only"
                % poly.num_points_hull())
        return poly.bbox()

    @_intm4tm2_scope()
    def _seal_ring_minimum_um(self) -> float:
        """Smallest outline side the PDK sealring PCell accepts, in um.

        The PCell refuses a smaller outline by returning an empty cell, which
        says nothing about why, so the same bound is computed here from the
        PDK's own constants and rule values and refused with a reason.
        Needs the IntM4TM2 library bootstrapped (its python dir on sys.path).
        """
        from intm4tm2_pycell_lib.ihp import sealring_code as sc
        rules = sc.loadSealRules()
        span = rules['Seal_k']
        nm = (2 * (sc.BOUNDARY_TO_EDGESEAL_NM + span + rules['Seal_a'])
              + span // sc.CORNER_STEPS)
        return nm / 1000.0

    @_intm4tm2_scope()
    def add_seal_ring(self) -> 'db.DBox':
        """Draw the PDK seal ring on the board outline. Returns the outline.

        Places the IntM4TM2 sealring PCell with l x w equal to the outline at
        its lower-left corner, then clips the no-fill keep-outs to the ring's
        39/4 (the deck flags them outside it). Raises SealRingRefused, with
        nothing added, when the outline is missing, not one rectangle, or
        under the PCell's minimum, or when the PCell cannot be had.

        Binds the layout to the intm4tm2 technology like add_cmim_devices,
        so it must run after every SG13G2 PCell has been created.
        """
        box = self._seal_ring_outline()
        if not self._bootstrap_intm4tm2_pcells():
            raise SealRingRefused(
                "the %s PCell library could not be loaded (set "
                "INTERPOSER_PDK_ROOT to an interposer PDK checkout)"
                % self.SEAL_RING_LIBRARY)
        lib = db.Library.library_by_name(self.SEAL_RING_LIBRARY,
                                         self.layout.technology_name)
        if lib is None or lib.layout().pcell_declaration(
                self.SEAL_RING_PCELL) is None:
            raise SealRingRefused(
                "the %s library has no %s PCell (use an interposer PDK "
                "checkout that ships it)"
                % (self.SEAL_RING_LIBRARY, self.SEAL_RING_PCELL))
        try:
            _check_intm4tm2_cni(self.SEAL_RING_PCELL, self.SEAL_RING_LIBRARY)
        except IntM4TM2Refused as exc:
            raise SealRingRefused(str(exc))
        try:
            minimum = self._seal_ring_minimum_um()
        except Exception as exc:
            raise SealRingRefused(
                "could not read the seal ring rules from the PDK: %s" % exc)
        dbu = self.layout.dbu
        l_um, w_um = box.width() * dbu, box.height() * dbu
        if min(l_um, w_um) < minimum:
            raise SealRingRefused(
                "the board outline %.3f x %.3f um is below the sealring "
                "minimum of %.3f um per side" % (l_um, w_um, minimum))

        def outline_param(dbu_len):
            nm = int(round(dbu_len * dbu * 1000))
            return "%d.%03du" % (nm // 1000, nm % 1000)

        params = {"l": outline_param(box.width()),
                  "w": outline_param(box.height())}
        cell = self.layout.create_cell(self.SEAL_RING_PCELL,
                                       self.SEAL_RING_LIBRARY, params)
        # A refused PCell hands back a nameless empty cell, not None
        # (see add_cmim_devices); either way nothing of it may stay.
        if cell is None or not cell.name or cell.bbox().empty():
            if cell is not None:
                self.layout.delete_cell(cell.cell_index())
            raise SealRingRefused(
                "the %s PCell returned an empty cell for l=%s w=%s"
                % (self.SEAL_RING_PCELL, params["l"], params["w"]))
        trans = db.Trans(db.Vector(box.left, box.bottom))
        bl = self.layout.find_layer(*self.SEAL_BOUNDARY_LAYER)
        drawn = (db.Region() if bl is None else
                 db.Region(cell.begin_shapes_rec(bl)).transformed(trans))
        if drawn.bbox() != box:
            self.layout.delete_cell(cell.cell_index())
            raise SealRingRefused(
                "the %s PCell drew EdgeSeal.boundary %d/%d at %s, not on the "
                "board outline %s" % ((self.SEAL_RING_PCELL,)
                                      + self.SEAL_BOUNDARY_LAYER
                                      + (drawn.bbox(), box)))
        self.top_cell.insert(db.CellInstArray(cell.cell_index(), trans))
        self._seal_ring = (cell.cell_index(), trans)
        self._clip_keepouts(drawn)
        return box.to_dtype(dbu)

    def has_seal_ring(self) -> bool:
        """True while the ring drawn by add_seal_ring is placed in the top cell."""
        if self._seal_ring is None:
            return False
        ci = self._seal_ring[0]
        return (self.layout.is_valid_cell_index(ci)
                and any(inst.cell_index == ci
                        for inst in self.top_cell.each_inst()))

    def _clip_keepouts(self, boundary: 'db.Region') -> int:
        """Cut the no-fill keep-outs back to the ring's 39/4.

        A keep-out may be drawn to the board edge or past it, and the deck
        reports every layer outside 39/4, markers included. Only the part
        outside is removed; inside, the keep-out still stops the fill.
        Returns the number of keep-out shapes that changed.
        """
        targets = {self.NOFILL_GLOBAL_LAYER}
        for role in self.NOFILL_ROLE_TO_METAL:
            target = self._nofill_target(role)
            if target is not None:
                targets.add(target)
        clipped = 0
        for target in sorted(targets):
            li = self.layout.find_layer(*target)
            if li is None:
                continue
            shapes = self.top_cell.shapes(li)
            pending = []
            for sh in shapes.each():
                if (db.Region(sh.polygon) - boundary).is_empty():
                    continue
                pending.append((sh, db.Region(sh.polygon) & boundary))
            for sh, kept in pending:
                shapes.erase(sh)
                shapes.insert(kept)
            clipped += len(pending)
        if clipped:
            print(f"Seal ring: clipped {clipped} no-fill keep-out shape(s) to "
                  f"EdgeSeal.boundary {self.SEAL_BOUNDARY_LAYER[0]}/"
                  f"{self.SEAL_BOUNDARY_LAYER[1]}")
        return clipped

    def _check_seal_ring_clearance(self) -> None:
        """Stop when the export's own fabricated geometry crowds the ring.

        Everything on a SEAL_RING_CLEARANCE layer, outside the ring cell and
        the imported dies, must sit inside the EdgeSeal interior shrunk by
        that layer's space: the metal's own space rule from the ring's metal,
        which follows EdgeSeal's inner edge, or one grid step off the band.
        Raises SealRingRefused naming the layer, the rule, the count and the
        first place. Basic space only; the wide-metal space is the deck's.
        """
        ring_ci, trans = self._seal_ring
        ring = self.layout.cell(ring_ci)
        es = self.layout.find_layer(*self.SEAL_EDGESEAL_LAYER)
        interior = db.Region() if es is None else db.Region(
            ring.begin_shapes_rec(es)).transformed(trans).merged().holes()
        if interior.is_empty():
            raise SealRingRefused(
                "the %s cell has no EdgeSeal %d/%d interior; there is nothing "
                "for the ring to enclose" % ((self.SEAL_RING_PCELL,)
                                             + self.SEAL_EDGESEAL_LAYER))
        excluded = ({ring_ci} | set(ring.called_cells())
                    | self._foreign_cell_set())
        dbu = self.layout.dbu
        grid = max(self.grid_dbu, 1)
        offenders = []
        for (lnum, dt), rule_key in sorted(self.SEAL_RING_CLEARANCE.items()):
            li = self.layout.find_layer(lnum, dt)
            if li is None:
                continue
            shape_iter = self.top_cell.begin_shapes_rec(li)
            shape_iter.unselect_cells(sorted(excluded))
            own = db.Region(shape_iter)
            if own.is_empty():
                continue
            if rule_key is None:
                space = grid
                rule = "one grid step (%.3f um)" % (grid * dbu)
            else:
                space_um = self.PDK_NOTCH_SPACE[rule_key]
                space = max(int(round(space_um / dbu)), grid)
                rule = "%s = %g um" % (rule_key, space_um)
            outside = own - interior.sized(-space)
            if outside.is_empty():
                continue
            first = min((p.bbox() for p in outside.each()),
                        key=lambda b: (b.left, b.bottom))
            offenders.append(
                "%d/%d: %d shape(s) in the ring band or closer than %s to the "
                "ring, first at (%.3f, %.3f)-(%.3f, %.3f) um"
                % (lnum, dt, outside.count(), rule, first.left * dbu,
                   first.bottom * dbu, first.right * dbu, first.top * dbu))
        if offenders:
            raise SealRingRefused("\n".join(
                ["fabricated geometry crowds the seal ring"] + offenders
                + ["only the basic space is checked here; the wide-metal "
                   "space (TM2_bR) is left to the level-2 deck"]))

    def get_outline_bbox(self) -> Optional[Tuple[float, float, float, float]]:
        """Bbox of the drawn board outline (prBoundary 235/0), or None.

        Returns:
            (x_min, y_min, width, height) in micrometers, or None when the
            outline layer is absent or empty.
        """
        layer_idx = self.layout.find_layer(*self.PRBOUNDARY_LAYER)
        if layer_idx is None:
            return None
        bbox = self.top_cell.dbbox(layer_idx)
        if bbox.empty():
            return None
        return (bbox.left, bbox.bottom, bbox.width(), bbox.height())

    # Drawn metals that the notch heal runs on, mapped to the minimum-space
    # key each one is checked against in interposer_tech_default.json (the
    # same file the via parameters come from). Metal4 and Metal5 share Mn_b.
    NOTCH_HEAL_LAYERS = {
        (50, 0):  'Mn_b',    # Metal4
        (67, 0):  'Mn_b',    # Metal5
        (126, 0): 'TM1_b',   # TopMetal1
        (134, 0): 'TM2_b',   # TopMetal2
    }

    # Fallback space values, used only when the tech JSON is unavailable.
    # Same numbers, same units (um) as the keys above.
    _DEFAULT_NOTCH_SPACE = {'Mn_b': 0.21, 'TM1_b': 1.64, 'TM2_b': 2.0}

    # The heal keeps its hands off wire-bond pads. Pad.fR measures the exit
    # band from the edge where the trace crosses the pad marker, so widening
    # a trace at the pad mouth lengthens that edge and enlarges the band that
    # has to stay covered: patching a notch there trades it for a bigger
    # exit-length violation. That geometry belongs to the pad cell and its own
    # rules, not to a spacing heal. dfpad drawing (41/0) is the wire-bond pad;
    # the Cu-pillar pads carry the :pillar datatype and are not excluded.
    NOTCH_HEAL_KEEPOUT = (41, 0)

    # Landing pad layers (layer_num, datatype) per fab anchor: CuPillarPad
    # opens the :pillar datatype, SolderBumpPad the :sbump one, both on
    # TopMetal2, which the wire-bond I/O pad cell below shares. The pads
    # themselves are drawn by the interposer PDK's bump_mirror (3D bodies by
    # the interconnect PDK's bump3d_generator); this copy only keeps an
    # annotation layer from aliasing them.
    LANDING_FAB_LAYERS = {
        'ihp_cupillar_anchor': {
            'TopMetal2':      (134, 0),
            'Passiv:pillar':  (9, 35),
            'dfpad:pillar':   (41, 35),
            'Recog:pillar':   (99, 35),
        },
        'ihp_sbump_anchor': {
            'TopMetal2':      (134, 0),
            'Passiv:sbump':   (9, 36),
            'dfpad:sbump':    (41, 36),
            'Recog:sbump':    (99, 36),
        },
    }

    # Wire-bond pads use the drawing datatypes of the same families: a bond
    # pad is TopMetal2 with a passivation opening over it and a dfpad polygon
    # marking it as a pad. Both are load-bearing in the carrier's own deck,
    # not decoration: dfpad is what exempts the pad from the metal-slit
    # requirement (Slt.c, max unslitted metal width 30 um, which a 100 um pad
    # otherwise violates), and passiv AND dfpad AND TopMetal2 is how
    # layers_def.drc derives a pad at all.
    IO_PAD_FAB_LAYERS = {
        'TopMetal2': (134, 0),
        'Passiv':    (9, 0),
        'dfpad':     (41, 0),
    }

    # Metal enclosure of the passivation opening on a wire-bond pad, in um.
    # Pas.c (min TopMetal2 enclosure of Passiv) is 2.1 um in the interposer
    # tech JSON, and the bondpad PyCell reads the same techparam, so this is
    # not a second copy of the rule: it is the value the PyCell's output is
    # checked against after every build. The deck only checks Pas.c inside a
    # sealring, which a bare carrier has none of, so an enclosure that came
    # out wrong would otherwise ship unnoticed.
    IO_PAD_PASSIV_ENCLOSURE_UM = 2.1

    # Pad shape asked of the PyCell.
    #
    # 'square' rather than the tech default 'octagon', and the reason is the
    # grid, not taste. Measured on the reference board, the octagon is better
    # electrically: it meets a diagonal trace head-on instead of at 45
    # degrees, which took Pad.fR_TM2 from 27 markers to 9. But its facets run
    # at 45 degrees, and where a facet of one slope is crossed by a trace
    # flank of the other, the two lines meet at a half grid step: the merged
    # outline then picks up a vertex at 2.5 nm, which rounds to the database
    # unit and reports as both Offgrid and Angle45. Seven of each on this
    # board. Offgrid and angle are hard rules, so the octagon stays out until
    # the crossing is made exact (it needs the pad centre and
    # the trace flank constants to have the same parity in grid steps).
    #
    # Do NOT ask for 'circle': that branch leaks an intermediate polygon at
    # full pad size onto the passivation layer, which drops the metal
    # enclosure to 0.5 nm. _check_io_pad_geometry catches it, but naming it
    # here saves the debugging.
    IO_PAD_SHAPE = 'square'

    # Below this the PyCell stops honouring the aspect ratio: it resets
    # hwquota to 1.0 with only a print, so a rectangular pad would come out
    # square while the .chiplet still recorded the rectangle. Refuse instead.
    IO_PAD_MIN_DIMENSION_UM = 10.0

    # I/O pads (external interposer pads): wire-bond MVP; flipped_bump and
    # tsv_bump reserved for follow-up PRs.
    SUPPORTED_IO_CLASSES = {'wire_bond'}
    RESERVED_IO_CLASSES = {'flipped_bump', 'tsv_bump'}

    @_intm4tm2_scope()
    def _create_wire_bond_pad_cell(self, size_x_um: float,
                                    size_y_um: float) -> db.Cell:
        """Create a wire-bond I/O pad cell from the interposer PDK's PyCell.

        A bond pad is a fabricable structure the PDK defines, the same way a
        Cu-pillar pad is: pad metal on TopMetal2, a dfpad polygon marking it as
        a pad, and a passivation opening the metal encloses by Pas.c. This used
        to be three boxes drawn here, which was a workaround; the geometry now
        comes from IntM4TM2/bondpad, so a pad that ships is a pad the PDK
        drew. IO_PAD_FAB_LAYERS stops being a description and becomes the
        contract that output is checked against.

        Drawing dfpad and passiv is what makes this a pad to the carrier's own
        deck rather than a wide slab of metal. Without dfpad the pad is not
        exempt from the metal-slit rule (Slt.c: 30 um is the widest unslitted
        TopMetal2), so every 100 um bond pad was a violation; without passiv
        there is no opening for a bond wire to land in and layers_def.drc's
        pad derivation (passiv AND dfpad AND TopMetal2) never fires. Both are
        why padType stays 'bondpad': the PyCell's 'probepad' draws no dfpad.

        The PyCell is built in a scratch layout bound to the interposer
        technology and copied in, rather than instantiated here: this
        generator's layout is bound to 'sg13g2' so the via_stack PCell
        resolves, and its via variants are still unflattened at this point.

        Raises:
            RuntimeError: the PDK or its PyCell library is not reachable,
                the library runs on a foreign cni (IntM4TM2Refused), or
                the PyCell did not draw the fabrication layers. There is no
                hand-drawn fallback on purpose: an approximation of a
                fabricable pad is the thing this replaced.
            ValueError: the requested pad is too small for the PyCell to
                honour (caught by add_io_pads, which skips that pad).
        """
        if min(size_x_um, size_y_um) < self.IO_PAD_MIN_DIMENSION_UM:
            raise ValueError(
                f"I/O pad {size_x_um:g}x{size_y_um:g} um is below the "
                f"{self.IO_PAD_MIN_DIMENSION_UM:g} um the bondpad PyCell can "
                f"draw while honouring the requested aspect ratio")

        bm = _import_bump_mirror()
        ensure_lib = getattr(bm, '_ensure_pcell_lib', None) if bm else None
        if ensure_lib is None:
            raise RuntimeError(
                "cannot reach the interposer PDK's PyCell library, so the "
                "wire-bond pad geometry has no source. Set "
                "INTERPOSER_PDK_ROOT to an OpenIntM4TM2 checkout (the one "
                "whose libs.tech/klayout/python holds bump_mirror.py and "
                "intm4tm2_pycell_lib) and retry.")
        ensure_lib()
        _check_intm4tm2_cni('bondpad')

        scratch = db.Layout()
        scratch.dbu = self.layout.dbu
        scratch.technology_name = 'intm4tm2'
        variant = scratch.create_cell('bondpad', 'IntM4TM2', {
            'diameter': f"{max(size_x_um, size_y_um):g}u",
            'hwquota': f"{size_y_um / size_x_um:g}",
            'shape': self.IO_PAD_SHAPE,
            'padType': 'bondpad',
            'topMetal': 'TM2',
            # Both off: a filler exclusion ring or a via stack under every bond
            # pad would land on layers this cell does not own, and the ring
            # would grow the top cell's bbox, which is the frame every pillar
            # and .chiplet coordinate is rebased on.
            'stack': 'nil',
            'addFillerEx': 'nil',
        })
        if variant is None:
            raise RuntimeError(
                "IntM4TM2/bondpad PCell variant could not be created "
                "(create_cell returned None; the library or the 'intm4tm2' "
                "technology did not resolve)")
        variant.flatten(-1, True)

        cell = self.layout.create_cell(f"WB_PAD_{size_x_um:g}x{size_y_um:g}")
        cell.copy_tree(variant)
        self._check_io_pad_geometry(cell)
        return cell

    def _check_io_pad_geometry(self, cell: db.Cell) -> None:
        """Hold the PyCell's output to the pad contract.

        Two ways it can go wrong quietly, both seen in this PyCell: a foreign
        KLAYOUT_LYP_FILE honoured by whatever registered the library first
        retags or drops the fabrication layers, and the 'circle' shape leaks an
        intermediate polygon at full pad size onto the passivation layer, which
        leaves the metal enclosing its own opening by 0.5 nm instead of 2.1 um.
        Neither raises anything on its own, and the carrier deck only checks
        Pas.c inside a sealring, which a bare carrier has none of.
        """
        drawn = set()
        for idx in self.layout.layer_indexes():
            if not cell.bbox_per_layer(idx).empty():
                info = self.layout.get_info(idx)
                drawn.add((info.layer, info.datatype))
        expected = set(self.IO_PAD_FAB_LAYERS.values())
        if drawn != expected:
            raise RuntimeError(
                f"the bondpad PyCell drew layers {sorted(drawn)} instead of "
                f"the fabrication set {sorted(expected)}. Check for a foreign "
                f"KLAYOUT_LYP_FILE in the environment of whatever process "
                f"first registered the IntM4TM2 library.")

        metal = db.Region(cell.shapes(
            self.layout.layer(*self.IO_PAD_FAB_LAYERS['TopMetal2'])))
        passiv = db.Region(cell.shapes(
            self.layout.layer(*self.IO_PAD_FAB_LAYERS['Passiv'])))
        enc_dbu = int(round(self.IO_PAD_PASSIV_ENCLOSURE_UM
                            / self.layout.dbu))
        short = metal.enclosing_check(passiv, enc_dbu, False,
                                      db.Metrics.Euclidian)
        if not short.is_empty():
            raise RuntimeError(
                f"the bondpad PyCell drew {short.count()} place(s) where the "
                f"pad metal encloses its passivation opening by less than "
                f"Pas.c ({self.IO_PAD_PASSIV_ENCLOSURE_UM} um). Pad cell "
                f"{cell.name}, shape '{self.IO_PAD_SHAPE}'.")

    def _get_or_create_io_pad_cell(self, io_class: str,
                                    size_x_um: float,
                                    size_y_um: float) -> db.Cell:
        """Return a cached I/O pad cell for (io_class, size).

        Dispatches by io_class. Reserved classes raise NotImplementedError
        so the calling code can skip them with a warning rather than
        aborting the whole conversion.
        """
        if not hasattr(self, '_io_pad_cells'):
            self._io_pad_cells: Dict[str, db.Cell] = {}

        cache_key = f"{io_class}_{size_x_um:g}x{size_y_um:g}"
        if cache_key in self._io_pad_cells:
            return self._io_pad_cells[cache_key]

        if io_class == 'wire_bond':
            cell = self._create_wire_bond_pad_cell(size_x_um, size_y_um)
        elif io_class in self.RESERVED_IO_CLASSES:
            raise NotImplementedError(
                f"I/O class '{io_class}' is reserved but not yet implemented "
                "(only wire_bond is currently supported)")
        else:
            raise ValueError(f"Unknown I/O class: '{io_class}'")

        self._io_pad_cells[cache_key] = cell
        print(f"  Created I/O pad cell: {cache_key} "
              f"({size_x_um:g}x{size_y_um:g} um)")
        return cell

    def add_io_pads(self, io_pads_json: str) -> List[Dict]:
        """Place external I/O pads from a sidecar JSON.

        The JSON is produced by gds_to_kicad/io_pads/kicad_pcb_to_iopads.py.
        Each entry has io_class, x_um, y_um, size_x_um, size_y_um already
        in interposer-global GDS coordinates (Y-up, micrometers).

        Returns the list of pads actually placed (skipping reserved/unknown
        io_classes), suitable for injection into the .chiplet file.
        """
        pads_path = Path(io_pads_json)
        if not pads_path.exists():
            print(f"Warning: I/O pads file not found: {io_pads_json}",
                  file=sys.stderr)
            return []

        try:
            with open(pads_path, 'r') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Warning: could not read I/O pads file {io_pads_json}: "
                  f"{exc}", file=sys.stderr)
            return []
        if not isinstance(data, dict):
            print(f"Warning: I/O pads file {io_pads_json} is not a JSON "
                  f"object; ignoring.", file=sys.stderr)
            return []

        pads = data.get('io_pads', [])
        if not pads:
            print(f"  No io_pads found in {io_pads_json}")
            return []

        group_cells: Dict[str, db.Cell] = {}
        placed: List[Dict] = []
        counts: Dict[str, int] = {}

        for p in pads:
            if not isinstance(p, dict):
                print(f"  Warning: skipping non-object io_pad entry: {p!r}",
                      file=sys.stderr)
                continue
            io_class = p.get('io_class', 'wire_bond')
            try:
                sx = float(p.get('size_x_um', 0.0))
                sy = float(p.get('size_y_um', 0.0))
            except (TypeError, ValueError):
                print(f"  Warning: skipping pad {p.get('ref', '?')} with "
                      f"non-numeric size", file=sys.stderr)
                continue
            if sx <= 0 or sy <= 0:
                print(f"  Warning: skipping pad {p.get('ref', '?')} "
                      f"with invalid size: {sx}x{sy}", file=sys.stderr)
                continue
            # Parse the position before creating any cell, so a bad position
            # cannot leave an empty IO_PADS_<class> group cell behind.
            try:
                x = float(p.get('x_um', 0.0))
                y = float(p.get('y_um', 0.0))
            except (TypeError, ValueError):
                print(f"  Warning: skipping pad {p.get('ref', '?')} with "
                      f"non-numeric position", file=sys.stderr)
                continue
            try:
                pad_cell = self._get_or_create_io_pad_cell(io_class, sx, sy)
            except (NotImplementedError, ValueError) as e:
                print(f"  Warning: {e}; skipping pad "
                      f"{p.get('ref', '?')}", file=sys.stderr)
                continue

            if io_class not in group_cells:
                group_name = f"IO_PADS_{io_class.upper()}"
                group_cells[io_class] = self.layout.create_cell(group_name)
                self.top_cell.insert(
                    db.DCellInstArray(group_cells[io_class], db.DTrans()))
                counts[io_class] = 0

            # Place on the manufacturing grid and record what was placed: the
            # sidecar position travels into the .chiplet, so it has to be the
            # drawn one, not the one the board happened to carry.
            x = self._snap_um(x)
            y = self._snap_um(y)
            p['x_um'] = x
            p['y_um'] = y
            group_cells[io_class].insert(
                db.DCellInstArray(pad_cell, db.DTrans(db.DVector(x, y))))
            counts[io_class] += 1
            placed.append(p)

        for cls, n in counts.items():
            print(f"  Placed {n} {cls} pads")
        return placed

    def cleanup_orphan_top_cells(self) -> int:
        """Prune top-level cells other than self.top_cell.

        _place_die_flipped extracts geometry from an imported chiplet
        template into a wrapper cell, but leaves the original template
        (and its sub-cells from the source GDS) sitting in the layout
        with no parent. They show up as a second top-level cell in the
        resulting GDS, which confuses viewers that auto-pick a top
        (e.g. KLayout / chiplet-studio may render the chiplet alone).

        prune_cell with levels=-1 also removes descendants that become
        orphans, dropping the imported PDK sub-hierarchy along with the
        template.
        """
        main_top_idx = self.top_cell.cell_index()
        orphans = [c.cell_index() for c in self.layout.each_cell()
                   if c.cell_index() != main_top_idx and c.parent_cells() == 0]
        for idx in orphans:
            self.layout.prune_cell(idx, -1)
        return len(orphans)

    def _record_boundary(self, instance: str, source_die: str,
                         boundary_poly: "db.DPolygon",
                         origin_x_um: float, origin_y_um: float,
                         rotation_deg: float, flip_chip: bool) -> None:
        """Accumulate one chiplet mechanical-boundary record for the assembly
        boundary manifest emitted by write(). polygon_dbu is authoritative for
        the DRC; polygon_um and transform are identity/provenance."""
        dbu = self.layout.dbu
        poly_um, poly_dbu = [], []
        for p in boundary_poly.each_point_hull():
            poly_um.append([round(p.x, 6), round(p.y, 6)])
            poly_dbu.append([int(round(p.x / dbu)), int(round(p.y / dbu))])
        self._boundary_records.append({
            "instance": instance,
            "source_die": source_die,
            "class": "chiplet",
            "transform": {
                "origin_um": [round(origin_x_um, 6), round(origin_y_um, 6)],
                "rotation_deg": rotation_deg,
                "mirror_x": bool(flip_chip),
                "magnification": 1.0,
            },
            "polygon_dbu": poly_dbu,
            "polygon_um": poly_um,
        })

    def _write_boundary_manifest(self, output_path: str) -> Path:
        """Write the <stem>.boundaries.json sidecar the ADK assembly DRC reads.

        One polygon per placed chiplet, carried OUTSIDE any fabrication-layer
        namespace so the assembly contract is PDK-agnostic. Always written (even
        with zero boundaries) so a manifest is always present beside the GDS.
        """
        out = Path(output_path)
        manifest_path = out.with_name(out.stem + ".boundaries.json")
        # Schema + version policy: adk/docs/boundary_manifest.md (the adk
        # readers exact-match the version; bump producers and readers together).
        manifest = {
            "schema": "adk-boundary-manifest",
            "version": "1.0.0",
            "generator": "hyp_to_gds.py",
            "assembly_gds": out.name,
            "dbu_um": self.layout.dbu,
            "top_cell": self.top_cell.name,
            "boundaries": list(self._boundary_records),
        }
        try:
            manifest["assembly_gds_sha256"] = hashlib.sha256(
                out.read_bytes()).hexdigest()
        except OSError:
            pass
        with manifest_path.open("w") as fh:
            json.dump(manifest, fh, indent=2)
        print(f"  Boundary manifest written to: {manifest_path} "
              f"({len(self._boundary_records)} chiplet boundaries)")
        return manifest_path

    def record_pillars(self, records: List[dict]) -> None:
        """Accumulate as-drawn Cu-pillar records for the pillar manifest.

        The first call (even with an empty list) marks the bump-generation
        path as run, so write() emits a <stem>.pillars.json — possibly with
        an empty pillars array. Each record carries device_ref, pin_name,
        method, x_um/y_um (raw drawing frame, y-up, post collision
        auto-resolve; the manifest writer rebases them to the canonical
        GDS-bbox-corner frame), diameter_um, moved_by_auto_resolve, and --
        for moved bumps -- auto_resolve_shift_um (a frame-invariant
        magnitude, so the writer's rebase leaves it untouched).
        """
        if self._pillar_records is None:
            self._pillar_records = []
        # The pillar cells are instantiated at these centres and the write
        # pass puts those instances on the manufacturing grid, so the record
        # is snapped with them: "as drawn" has to mean as drawn.
        for rec in records:
            rec["x_um"] = round(self._snap_um(rec["x_um"]), 6)
            rec["y_um"] = round(self._snap_um(rec["y_um"]), 6)
        self._pillar_records.extend(records)

    def record_interconnect_rules(self, method: str, spacing_um: float,
                                  pitch_um: float, pad_size_um: float) -> None:
        """Record the attachment rules one method's pillars were checked against.

        These are the numbers auto-resolve enforces, so recording them is what
        makes moved_by_auto_resolve explainable after the fact. They travel in
        the pillar manifest's "methods" block; the interconnect PDK stays the
        authority, and a consumer that has both can tell a stale artifact from
        a current one.
        """
        if self._pillar_method_rules is None:
            self._pillar_method_rules = {}
        self._pillar_method_rules[method] = {
            "IXN_spacing": float(spacing_um),
            "IXN_pitch": float(pitch_um),
            "IXN_pad_size": float(pad_size_um),
        }

    def _write_pillar_manifest(self, output_path: str) -> Optional[Path]:
        """Write the <stem>.pillars.json sidecar with the as-drawn
        Cu-pillar/bump centers (canonical interposer GDS-bbox-corner frame,
        y-up, micrometers, post collision auto-resolve).

        The records accumulate in the raw drawing frame (HYP coordinates);
        here they are rebased by the interposer top-cell bbox lower-left so
        the manifest lives in the SAME frame as the .chiplet die positions
        and io_pads (the canonical GDS-bbox-corner frame of the coord frame
        contract, i.e. the exact re-anchor update_chiplet_file applies).
        The origin is captured at the first manifest write — the interposer
        GDS write, before chiplet instances are merged in — and reused for
        the complete-GDS manifest so both sidecars share one frame.

        Written only when the bump-generation path ran (record_pillars was
        called); a run that placed zero bumps still gets a manifest with an
        empty pillars array. x_um/y_um are authoritative for manifest-level
        checks; the GDS remains the fabrication ground truth. Version policy
        mirrors the boundary manifest: readers exact-match the version.
        """
        if self._pillar_records is None:
            return None
        if self._pillar_frame_origin is None:
            bbox = self.top_cell.dbbox()
            self._pillar_frame_origin = ((0.0, 0.0) if bbox.empty()
                                         else (bbox.left, bbox.bottom))
        origin_x, origin_y = self._pillar_frame_origin
        out = Path(output_path)
        manifest_path = out.with_name(out.stem + ".pillars.json")
        pillars = [
            dict(rec,
                 x_um=round(rec["x_um"] - origin_x, 6),
                 y_um=round(rec["y_um"] - origin_y, 6))
            for rec in sorted(self._pillar_records,
                              key=lambda r: (r["device_ref"], r["pin_name"]))
        ]
        manifest = {
            "schema": PILLAR_MANIFEST_SCHEMA,
            "version": PILLAR_MANIFEST_VERSION,
            "generator": "hyp_to_gds.py",
            "assembly_gds": out.name,
            "units": "um",
            "methods": dict(sorted((self._pillar_method_rules or {}).items())),
            "pillars": pillars,
        }
        with manifest_path.open("w") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
        print(f"  Pillar manifest written to: {manifest_path} "
              f"({len(pillars)} pillar(s))")
        return manifest_path

    def _paint_boundary_annotations(self) -> None:
        """Paint each chiplet boundary (and its instance label) onto a
        viewer-only annotation layer, for eyeball inspection of the assembly.

        Opt-in via --annotate-boundaries. The layer (default 1000/0, well
        outside IHP SG13G2's fab range) is read by NO DRC rule and is not part
        of the assembly contract -- that lives in the boundary manifest. So it
        can never alias a fabrication layer nor mask a missing check. Idempotent:
        clears the layer in the top cell first, so repeated write() calls do not
        duplicate shapes.
        """
        if not self._annotate_boundaries or not self._boundary_records:
            return
        viz_layer, viz_dt = self._boundary_viz_layer
        idx = self.layout.layer(viz_layer, viz_dt)
        self.top_cell.shapes(idx).clear()
        for rec in self._boundary_records:
            pts = [db.Point(x, y) for x, y in rec["polygon_dbu"]]
            if len(pts) < 3:
                continue
            poly = db.Polygon(pts)
            self.top_cell.shapes(idx).insert(poly)
            label = rec.get("instance") or rec.get("source_die") or ""
            if label:
                c = poly.bbox().center()
                self.top_cell.shapes(idx).insert(
                    db.Text(label, db.Trans(db.Vector(c.x, c.y))))
        print(f"  Boundary annotations painted on {viz_layer}/{viz_dt} "
              f"({len(self._boundary_records)} chiplets, viewer-only, no rule reads it)")

    def _foreign_cell_set(self) -> set:
        """Imported die cells plus everything they instantiate."""
        out = set()
        for ci in self._foreign_cells:
            if not self.layout.is_valid_cell_index(ci):
                continue
            out.add(ci)
            out.update(self.layout.cell(ci).called_cells())
        return out

    def _snap_shape(self, sh):
        """Return the on-grid geometry for a shape, or None if it is already
        on grid. Boxes, polygons, paths and text labels are covered; anything
        else is left alone rather than guessed at."""
        if sh.is_box():
            b = sh.box
            nb = db.Box(self._snap(b.left), self._snap(b.bottom),
                        self._snap(b.right), self._snap(b.top))
            return None if nb == b else nb
        if sh.is_path():
            p = sh.path
            pts = [self._snap_point(q) for q in p.each_point()]
            np_ = db.Path(pts, p.width, p.bgn_ext, p.end_ext, p.round)
            return None if np_ == p else np_
        if sh.is_polygon() or sh.is_simple_polygon():
            poly = sh.polygon
            np_ = db.Polygon([self._snap_point(q) for q in poly.each_point_hull()])
            for h in range(poly.holes()):
                np_.insert_hole([self._snap_point(q)
                                 for q in poly.each_point_hole(h)])
            return None if np_ == poly else np_
        if sh.is_text():
            t = sh.text
            nt = t.dup()
            nt.x = self._snap(t.x)
            nt.y = self._snap(t.y)
            return None if nt == t else nt
        return None

    def _snap_layout_to_grid(self) -> Tuple[int, int]:
        """Move every authored vertex and instance origin onto the grid.

        Idempotent, so running it before each write() is free on the second
        call. Imported chiplet dies are excluded on both counts: their
        geometry is not ours to reshape, and their placement is design intent
        that the boundary manifest records. Everything the exporter draws is
        covered, including via and pad cells, which are on grid inside their
        own cell but were instantiated at bump and pad coordinates that are
        not. Returns (shapes moved, instances moved).
        """
        if self.grid_dbu <= 1:
            return (0, 0)
        foreign = self._foreign_cell_set()
        shapes_moved = 0
        insts_moved = 0
        for cell in self.layout.each_cell():
            if cell.cell_index() in foreign:
                continue
            for li in self.layout.layer_indexes():
                shapes = cell.shapes(li)
                if shapes.is_empty():
                    continue
                pending = []
                for sh in shapes.each():
                    new_geom = self._snap_shape(sh)
                    if new_geom is not None:
                        pending.append((sh, new_geom))
                for sh, new_geom in pending:
                    shapes.replace(sh, new_geom)
                shapes_moved += len(pending)
            pending_i = []
            for inst in cell.each_inst():
                if inst.cell_index in foreign:
                    continue
                trans = inst.cplx_trans if inst.is_complex() else inst.trans
                disp = trans.disp
                snapped = db.Vector(self._snap(disp.x), self._snap(disp.y))
                if snapped != disp:
                    pending_i.append((inst, snapped))
            for inst, snapped in pending_i:
                if inst.is_complex():
                    trans = inst.cplx_trans.dup()
                    trans.disp = snapped
                    inst.cplx_trans = trans
                else:
                    trans = inst.trans.dup()
                    trans.disp = snapped
                    inst.trans = trans
            insts_moved += len(pending_i)
        return (shapes_moved, insts_moved)

    # Notch heal tuning. MARGIN fills a little past the rule so the result
    # clears it rather than sitting on it. MIN_SPAN keeps every patch wide
    # enough to escape the wedge it is filling: a wedge that closes at a
    # shallow angle reports a tiny edge-pair bounding box, and a patch that
    # small lands back inside the wedge and the heal never converges.
    # MAX_ITERS bounds the loop; it is not a convergence proof (see
    # _heal_notches), which is why exceeding it warns instead of passing.
    NOTCH_HEAL_MARGIN = 1.10
    NOTCH_HEAL_MIN_SPAN = 2.0     # multiples of the layer's space rule
    NOTCH_HEAL_MAX_ITERS = 12
    # Patch edges are axis-parallel, and that is a constraint, not a
    # default. A 45 degree chamfer on them is legal in isolation (on grid,
    # 0/45/90, no acute corner) and was measured to close the same notches
    # with less copper, but it makes the MERGED copper acquire vertices off
    # the 5 nm grid where a chamfer meets the diagonal traces, and the deck
    # reports those: 4 offgrid and 4 Angle45 markers on the reference board,
    # in flat, deep and tiling alike, none of them at a drawn vertex. An
    # axis-aligned edge crossing a 0/45/90 world does not do that. Do not
    # reintroduce the chamfer without re-running the offgrid and angle decks.

    def _heal_notches(self) -> int:
        """Fill sub-minimum-space notches in the drawn metals.

        A notch is a gap inside a single piece of copper; the gap between two
        separate pieces is a space violation and is deliberately left alone.
        That distinction is why this uses notch_check rather than the more
        obvious oversize/undersize closing, which would bridge any two nets
        sitting at exactly the minimum space, legal copper, into a short.

        Finding the gap inside one polygon does not make the fill safe,
        though. A patch over-fills its wedge (below), so it can reach copper
        the notch does not belong to: on a mixed-die board one box ran 4.19 um
        past a landing and stopped 0.8 um from another net's via cover, and a
        cover 2.04 um away would have been welded into a short no deck sees.
        So every patch is cut back to keep the layer's space from copper it
        does not own (_clear_other_copper), and a notch the cut leaves open
        stays open and is reported.

        The patches are rectilinear and on the manufacturing grid. They
        over-fill the wedge rather than tracing it, on purpose: the deck
        checks raw polygons, so a patch has to be 0/45/90 and on grid on its
        own, while the notch is usually bounded on one side by the arc of a
        round pad. A traced fill would inherit the arc's vertices and trip
        the offgrid and angle decks.

        Over-filling can expose a new, smaller notch at a patch corner, so
        the pass repeats until the layer is clean. This converges in practice
        but is not guaranteed to, hence the iteration cap and the warning.

        Foreign cells are excluded on the same grounds as the grid snap: an
        imported die's geometry is not ours to reshape.

        Returns the number of notch regions still open after the last pass,
        counted afresh at each layer's space rule.
        """
        foreign = list(self._foreign_cell_set())
        keepout = db.Region(
            self.top_cell.begin_shapes_rec(
                self.layout.layer(*self.NOTCH_HEAL_KEEPOUT))).merged()
        heal_cell = None
        residual = 0
        for (layer_num, datatype), rule_key in sorted(self.NOTCH_HEAL_LAYERS.items()):
            space_um = self.PDK_NOTCH_SPACE[rule_key]
            li = self.layout.layer(layer_num, datatype)
            space = int(round(space_um / self.layout.dbu))
            limit = space_um * self.NOTCH_HEAL_MARGIN / self.layout.dbu
            min_span = int(round(space_um * self.NOTCH_HEAL_MIN_SPAN
                                 / self.layout.dbu))

            patched = 0
            for _ in range(self.NOTCH_HEAL_MAX_ITERS):
                merged = self._own_metal(li, foreign)
                notches = merged.notch_check(limit, False, db.Metrics.Euclidian)
                boxes = self._notch_patches(notches, min_span)
                if not keepout.is_empty():
                    boxes = boxes.not_interacting(keepout)
                boxes = self._clear_other_copper(boxes, merged, notches, space)
                if (boxes - merged).is_empty():
                    # Either nothing is left to close, or what is left is
                    # inside the keepout, or no patch can close it without
                    # crowding other copper. All three mean: stop, do not spin.
                    break
                if heal_cell is None:
                    heal_cell = self.layout.create_cell(
                        f"{self.top_cell.name}_NOTCH_HEAL")
                    self.top_cell.insert(
                        db.CellInstArray(heal_cell.cell_index(), db.Trans()))
                for box in boxes.each():
                    heal_cell.shapes(li).insert(box)
                patched += boxes.count()

            # A fresh check at the rule itself, not "patches left to place": a
            # notch the clearance cut cannot close yields no patch, and counting
            # patches would call it closed. Notches under the keepout are out
            # of scope by design, not failures, and the carrier DRC reports
            # them against the pad rules where they belong.
            merged = self._own_metal(li, foreign)
            still = self._notch_patches(
                merged.notch_check(space, False, db.Metrics.Euclidian), min_span)
            if not keepout.is_empty():
                still = still.not_interacting(keepout)
            residual += still.count()
            if patched or not still.is_empty():
                print(f"  Notch heal ({layer_num}/{datatype}, {rule_key} = "
                      f"{space_um} um): {patched} patch(es)"
                      + (f", {still.count()} region(s) unclosed"
                         if not still.is_empty() else ""))
                for region in still.each():
                    c = region.bbox().center()
                    print(f"    unclosed notch near ({c.x * self.layout.dbu:.3f}, "
                          f"{c.y * self.layout.dbu:.3f}) um")
        if residual:
            print(f"Warning: {residual} notch region(s) below the minimum "
                  f"space are still open after the heal (each is listed under "
                  f"its heal line). "
                  f"The carrier DRC will report them; they need a layout fix, "
                  f"not a wider heal.", file=sys.stderr)
        return residual

    def _clear_other_copper(self, patches, copper, notches,
                            space: int) -> 'db.Region':
        """Cut each patch back so it keeps `space` from copper it does not own.

        A patch owns the merged polygons carrying the notch edges it was made
        from, which are the pairs whose bbox touches the patch itself; its
        bbox is not enough, since a patch merged from several boxes can span
        another net's notch without reaching it. Every other polygon is
        someone else's, even one the uncut box already touches. Each piece of
        that copper within `space` of the patch cuts it by the piece's bbox
        grown by `space`, snapped outward, so the patch stays rectilinear and
        on grid and only ever shrinks. Whole-polygon bboxes would cut far more
        than needed and leave notches open that a piece-wise cut closes.

        Owned copper is never cut against, so a patch with two owners is
        rejected before the cut: a cut against a third polygon could leave it
        touching one owner while nothing checks its space to the other. A
        patch still touching two polygons after the cut is dropped as well.
        Either way the notch stays open and the recount reports it.

        Patches are taken one at a time, and each one kept joins the copper
        the next is cut against, so patches placed in the same pass keep the
        space between each other as well.
        """
        g = max(1, self.grid_dbu)
        def snap_out(b):
            return db.Box((b.left // g) * g, (b.bottom // g) * g,
                          -((-b.right) // g) * g, -((-b.top) // g) * g)

        placed = db.Region()
        for patch in patches.each():
            reach = db.Region(patch).sized(space)
            edges = db.Edges()
            for pair in notches.each():
                if patch.touches(pair.bbox()):
                    edges.insert(pair.first)
                    edges.insert(pair.second)
            near = (copper + placed).interacting(reach).merged()
            if near.interacting(edges).count() > 1:
                continue
            others = near.not_interacting(edges) & reach
            cut = db.Region()
            for piece in others.each():
                cut.insert(snap_out(piece.bbox().enlarged(space, space)))
            # A cut can leave a sliver of the overhang standing on the pad,
            # narrower than the width rule; opening the patch by half the
            # space removes it, since every healed layer's width rule is at
            # or under its space rule. That reduces width violations, it
            # does not rule them out: the merged copper is not checked.
            kept = (db.Region(patch) - cut).sized(-(space // 2)).sized(space // 2)
            if near.interacting(kept).count() <= 1:
                placed += kept
        return placed.merged()

    def _own_metal(self, layer_index: int, foreign: List[int]) -> 'db.Region':
        """Merged drawn metal on one layer, excluding imported dies."""
        shape_iter = self.top_cell.begin_shapes_rec(layer_index)
        if foreign:
            shape_iter.unselect_cells(foreign)
        region = db.Region(shape_iter)
        region.merge()
        return region

    def _notch_patches(self, notches, min_span: int) -> 'db.Region':
        """Turn notch edge pairs into on-grid fill patches, one per junction.

        A single wedge is reported as a fan of edge pairs, one per step of the
        gap: at the junction below, five pairs measuring 0.00, 0.11, 0.69,
        1.27 and 1.86 um, all inside one 1.9 x 2.5 um spot. Patching each pair
        on its own puts five overlapping shapes there, and their corners seed
        the next round, so the heal walks up the trace leaving a staircase of
        patches behind it. Grouping the pairs first gives one patch per
        junction: on the reference board 25 patches and 377 um2 instead of 61
        and 556.
        """
        g = max(1, self.grid_dbu)
        def floor_g(v):
            return (v // g) * g
        def ceil_g(v):
            return -((-v) // g) * g

        # Group by proximity: grow each pair's footprint by half the patch
        # span, merge, and whatever fuses was one junction.
        seeds = db.Region()
        for pair in notches.each():
            seeds.insert(pair.bbox())
        seeds.size(max(g, ceil_g(min_span // 2)))
        seeds.merge()

        patches = db.Region()
        for cluster in seeds.each():
            b = cluster.bbox()
            patches.insert(db.Box(floor_g(b.left), floor_g(b.bottom),
                                  ceil_g(b.right), ceil_g(b.top)))
        patches.merge()
        return patches

    def write(self, output_path: str, sidecars: bool = True) -> None:
        """Write layout preserving via cell hierarchy.

        PCell variants are resolved to static geometry within their own cells.
        Via instances are grouped by metal pair for a clean hierarchy.
        Context info is stripped so the GDS opens cleanly without PDK dependencies.
        A <stem>.boundaries.json manifest with the chiplet boundaries is written
        alongside the GDS for the ADK assembly DRC, and — when the Cu-pillar
        generation path ran — a <stem>.pillars.json manifest with the as-drawn
        bump centers. sidecars=False leaves both to a later write_sidecars(),
        for a caller that still changes the GDS bytes after this write.

        With a seal ring drawn, the first write stops with SealRingRefused,
        before any byte is written, when the layout crowds the ring.
        """
        for via_index in self._via_cells.values():
            self.layout.cell(via_index).flatten(-1, True)

        # Onto the manufacturing grid before anything is written, so the GDS
        # and the sidecars describe the same geometry. The boundary
        # annotations are painted afterwards from the manifest records, which
        # keeps the viewer-only layer identical to the contract even where the
        # snap moved a die-adjacent vertex by a nanometer or two.
        shapes_moved, insts_moved = self._snap_layout_to_grid()
        if shapes_moved or insts_moved:
            print(f"  Manufacturing grid ({self.grid_dbu} dbu): snapped "
                  f"{shapes_moved} shape(s) and {insts_moved} instance "
                  f"origin(s)")

        # After the grid pass, so the heal reads the geometry that will
        # actually be written and its own patches are never moved again.
        self._heal_notches()
        if self._odd_angle_segments:
            print(f"Warning: {self._odd_angle_segments} trace segment(s) are "
                  f"neither orthogonal nor 45 degrees; the carrier's angle "
                  f"deck (3_2_angle.drc) allows only 0/45/90 on the metals, "
                  f"so these will be reported. Redraw them on the board.",
                  file=sys.stderr)

        # After the grid pass and the heal, so the check reads the geometry
        # that is about to be written. Once: the later complete-GDS write
        # adds only imported dies, which the check leaves out anyway.
        if self._seal_ring is not None and not self._seal_ring_cleared:
            self._check_seal_ring_clearance()
            self._seal_ring_cleared = True

        save_opts = db.SaveLayoutOptions()
        save_opts.write_context_info = False
        self._paint_boundary_annotations()
        self.layout.write(output_path, save_opts)
        if sidecars:
            self.write_sidecars(output_path)

    def write_sidecars(self, output_path: str) -> None:
        """Write the boundary and pillar manifests of the GDS at output_path.

        The boundary manifest binds the digest of the bytes on disk, so this
        runs once those bytes are final: straight after write(), or after the
        metal fill has rewritten the file in place.
        """
        self._write_boundary_manifest(output_path)
        self._write_pillar_manifest(output_path)


def get_default_connection_stacks() -> dict:
    """Return default connection stack definitions for .chiplet files.

    Sourced from the interconnect PDK manifest (single source of truth);
    byte-identical to the prior hardcoded PacTech table for the IHP methods.
    Raises if the interconnect PDK is not available (it is a required sibling
    dependency of the plugin).
    """
    im = _import_interconnect_manifest()
    if im is None:
        raise RuntimeError(
            "interconnect_pdk not found. Set INTERCONNECT_PDK_ROOT, or install "
            "interconnect_pdk as a sibling repo of the plugin."
        )
    try:
        lib = im.get_connection_library()
    except FileNotFoundError as exc:
        raise RuntimeError(
            "interconnect_pdk manifest data is missing (%s); the "
            "INTERCONNECT_PDK_ROOT checkout looks incomplete." % exc)
    return {
        mid: {
            "description": stack["description"],
            "layers": [dict(layer) for layer in stack["layers"]],
        }
        for mid, stack in lib.items()
    }


def _connection_stack_from_manifest(method_id: str):
    """{description, layers} for one manifest method, or None.

    Covers methods deliberately outside the default connection library
    (e.g. the vendorx demo): get_default_connection_stacks() keeps the
    emitted block byte-stable, but an explicitly selected per-die method
    only needs the manifest to define its connection stack.
    """
    im = _import_interconnect_manifest()
    if im is None:
        return None
    try:
        stack = im.get_connection_stack(method_id)
    except (KeyError, FileNotFoundError):
        return None  # unknown method, or a partial PDK install
    return {
        "description": stack["description"],
        "layers": [dict(layer) for layer in stack["layers"]],
    }


#: A refusal recorded while the argparse parser was being built, reported later
#: by _report_deferred_source_refusal. Not a cache: it exists because the read
#: happens before argv is parsed, so there is nowhere to report it yet.
_DEFERRED_SOURCE_REFUSAL = None


def _connection_type_cli_choices():
    """CLI choices for --connection-type from the manifest (all methods).

    Returns None (argparse accepts any value) when the manifest is unavailable,
    so --help still works without the interconnect PDK installed.

    This is the FIRST manifest read in the process: it runs while the parser is
    being built, before a single argument is parsed. That makes it the wrong
    place to report anything and the wrong place to die. Before this change an
    unusable manifest killed the worker right here with a raw traceback and
    zero bytes of help output, so a user with a version-rejecting PDK could not
    even run --help to find out about INTERCONNECT_PDK_ROOT, and no later
    handler could ever report the refusal. It also made the fix at the
    technology-block site unreachable in a CLI run, which would have been a
    green that proves nothing.

    So the refusal is RECORDED and re-raised at the boundary once argv exists.
    It is not simply swallowed and re-read there: the version WARNING dedups
    per process inside the reader, so a second read of the same manifest can
    come back clean and the refusal would evaporate. Whoever reads first owns
    the report.
    """
    global _DEFERRED_SOURCE_REFUSAL
    try:
        im = _import_interconnect_manifest()
        if im is None:
            return None
        return im.list_methods()
    except FileNotFoundError:
        return None  # PDK dir present, manifest not: same as not installed
    except SourceRefused as exc:
        _DEFERRED_SOURCE_REFUSAL = exc
        return None
    except Exception as exc:
        _DEFERRED_SOURCE_REFUSAL = SourceRefused(
            "the interconnect PDK manifest is present but unusable (%s: %s)"
            % (type(exc).__name__, exc),
            exit_code=_source_refusal_exit_code(exc),
        )
        return None


def _report_deferred_source_refusal():
    """Exit with the shared table's code if the manifest read at parser-build refused.

    Called immediately after parse_args, so --help has already had its chance
    to print and no artifact exists yet. Reporting here rather than at the read
    is what makes the refusal a refusal instead of a traceback.
    """
    exc = _DEFERRED_SOURCE_REFUSAL
    if exc is None:
        return
    print("Error: %s" % exc, file=sys.stderr)
    print("  Refusing to export: a cross-repo source is present and cannot be "
          "used. No artifact was written. Align the interconnect PDK with the "
          "plugin, or unset INTERCONNECT_PDK_ROOT to run without it.",
          file=sys.stderr)
    sys.exit(exc.exit_code)


def _probe_outline_layers(gds_path: str,
                          layers: Tuple[Tuple[int, int], ...]
                          ) -> Optional[Tuple[int, int]]:
    """First (layer, datatype) in `layers` carrying non-empty top-cell
    geometry in `gds_path`, or None.

    Best-effort: only used to enrich a warning, so any read error (missing
    file, no top cell) yields None rather than raising.
    """
    try:
        layout = db.Layout()
        layout.read(str(gds_path))
        top = layout.top_cell()
        if top is None:
            return None
        for (lnum, dt) in layers:
            idx = layout.find_layer(lnum, dt)
            if idx is not None and not top.dbbox(idx).empty():
                return (lnum, dt)
    except Exception:
        return None
    return None


def _layout_ref(target: str, chiplet_dir: str) -> str:
    """How a .chiplet names the layout `target`.

    Relative to `chiplet_dir`, where readers anchor a relative layout, '..'
    allowed, whenever the two share a root below the filesystem root; else
    absolute. The same workspace may be mounted at different roots (a host
    tree and its container mount), and only a relative reference opens from
    both. A target sharing nothing but the root (another mount, /opt) keeps
    its absolute path. Lexical on purpose (abspath, not resolve): a
    symlinked GDS inside the workspace must not become its target outside
    the mount.
    """
    target = os.path.abspath(target)
    chiplet_dir = os.path.abspath(chiplet_dir)
    try:
        common = os.path.commonpath([target, chiplet_dir])
    except ValueError:  # different drives
        return target
    if os.path.dirname(common) == common:
        return target
    return os.path.relpath(target, chiplet_dir)


def update_chiplet_file(chiplet_path: str, interposer_gds_path: str,
                        bbox: Tuple[float, float, float, float] = None,
                        connection_type: str = "",
                        attachment_surface_z: Optional[
                            'float | AttachFrame'] = None,
                        io_pads: Optional[List[Dict]] = None,
                        devices: Optional[List['Device']] = None,
                        die_connections: Optional[Dict[str, str]] = None,
                        die_thicknesses: Optional[Dict[str, float]] = None,
                        outline_bbox: Optional[Tuple[float, float, float,
                                                     float]] = None,
                        to_um=None, to_um_y=None,
                        die_outline_layers: Optional[Dict[str, Tuple[int,
                                                                     int]]]
                        = None,
                        die_outline_exempt: Optional[List[str]] = None,
                        outline_reader: Optional[
                            '_die_outline.DieGdsReader'] = None,
                        io_pad_refs: Optional[Iterable[str]] = None,
                        system_chiplet: Optional[Dict] = None,
                        die_layouts: Optional[Dict[
                            str, '_die_layouts.DieLayout']] = None) -> bool:
    """
    Update .chiplet file with interposer GDS path, dimensions, position,
    correct z-values for dies, and top_cell names from GDS files.

    Args:
        chiplet_path: Path to the .chiplet YAML file
        interposer_gds_path: Path to the interposer GDS file
        bbox: Optional tuple (x_min, y_min, width, height) in micrometers.
              If None, computed from interposer GDS bounding box.
        connection_type: Connection stack ID (e.g. "cupillar_opt1"). When set,
                         injects connection_stacks section and sets connection
                         field on each die component.
        attachment_surface_z: interposer die-attachment surface z (BEOL top) in
                              micrometers -- the plane dies mount on, so each
                              die's position.z = this + connection-stack height,
                              the exact decimal sum of the values as written
                              (AttachFrame.at). None reads it from the
                              interposer PDK (read_attach_frame), the worker
                              passes the AttachFrame its head read, and a
                              direct caller may pass a number.
                              Distinct from the interposer's physical body
                              thickness (dimensions.thickness), which is left as
                              the KiCad board-stackup value.
        io_pads: Optional list of placed I/O pad dicts (from add_io_pads).
                 When provided, injected under the interposer component as
                 an `io_pads:` block.
        devices: Optional list of Device objects from the HYP parser.
                 When provided, die x/y positions in the .chiplet are
                 overwritten using GDS-bbox-corner coordinates. Required
                 because KiCad's exporter computes positions against the
                 PCB bbox (which can be shifted from the GDS bbox by a
                 few hundred microns), so without this conversion the
                 dies appear offset from the interposer body in viewers
                 that anchor on the GDS bbox (e.g. Chiplet Studio).
        die_connections: Per-die connection overrides {component id: method
                 id}. A die not listed keeps connection_type. The stacks of
                 every method in use are injected, and each die's z comes
                 from its own stack.
        die_thicknesses: Per-die physical thickness {component id: um}.
                 Written to the die's dimensions.thickness (body z-extent,
                 spec default 0.0). Orthogonal to the connection stack
                 heights, which model the interconnect gap below the die,
                 and to position.z, which stays the z-mounting result.
        outline_bbox: Optional (x_min, y_min, width, height) in micrometers
                 of the board outline (prBoundary 235/0, drawn from KiCad's
                 Edge.Cuts). When available, interposer dimensions come
                 from it -- the fab outline -- instead of the drawn-geometry
                 bbox; position keeps the full-bbox center (the
                 anchor: bbox_center mesh contract). When None and bbox is
                 computed from the GDS here, it is derived from layer
                 235/0 if present in the file.
        die_outline_layers: {die id: (layer, datatype)} of the die outline in
                 each die's own GDS (--die-outline-layers). A declared die's
                 dimensions.width/height become that outline's size, in the
                 die GDS top cell's frame (rotation.z and orientation carry
                 the placement).
        die_outline_exempt: Die ids that keep the width/height already in
                 the .chiplet (--die-outline-exempt), each logged by name.
                 Every die must be in exactly one of the two;
                 resolve_die_outlines raises DieOutlineRefused, before
                 anything is written, when one is not or its GDS cannot give
                 what it needs.
        outline_reader: The run's DieGdsReader, so the die GDS reads done at
                 the worker head are reused rather than repeated. None reads
                 afresh.
        io_pad_refs: The references of every --io-pads entry, placed or not,
                 as the worker head used them; a .chiplet die with one of
                 these ids is a wire-bond pad, not sized
                 (resolve_die_outlines). None takes the refs of io_pads.
        system_chiplet: The loaded System .chiplet (--system-chiplet). When
                 given, the interposer's and each die's technology, and the
                 technologies block, come from it by component id
                 (technology_overlay). None leaves both as the writer made
                 them.
        die_layouts: {die id: DieLayout} from the --die-layouts sidecar
                 (die_layouts.load). Each such die's layout and top_cell
                 become its full layout's, and its metadata names the
                 abstract the assembly places (assembly_view,
                 assembly_view_layout). Both files are checked against the
                 contract first (resolve_die_layouts). None leaves every
                 die's layout as the board gives it.

    Returns:
        True if successful, False otherwise.

    Raises:
        DieOutlineRefused: a die cannot be sized or exempted (see
                 resolve_die_outlines). The .chiplet is left untouched.
        TechnologyRefused: the System .chiplet cannot supply a component's
                 technology (see technology_overlay.plan). The .chiplet is
                 left untouched.
        DieLayoutRefused: a --die-layouts entry names no die, or its files
                 do not agree with its contract (see resolve_die_layouts).
                 The .chiplet is left untouched.
        AttachFrameRefused: attachment_surface_z is None and the interposer
                 PDK's mount plane cannot be read. The .chiplet is left
                 untouched.
    """
    try:
        import yaml
    except ImportError:
        print("Error: PyYAML not installed. Install with: pip install pyyaml", file=sys.stderr)
        return False

    chiplet_file = Path(chiplet_path)
    if not chiplet_file.exists():
        print(f"Error: Chiplet file not found: {chiplet_path}", file=sys.stderr)
        return False

    # Compute bbox from interposer GDS if not provided
    if bbox is None:
        gds_path = Path(interposer_gds_path)
        if gds_path.exists():
            try:
                layout = db.Layout()
                layout.read(str(gds_path))
                gds_bbox = layout.top_cell().dbbox()
                bbox = (gds_bbox.left, gds_bbox.bottom,
                        gds_bbox.width(), gds_bbox.height())
                print(f"Computed interposer bbox from GDS: "
                      f"{bbox[2]:.2f} x {bbox[3]:.2f} um")
                if outline_bbox is None:
                    oidx = layout.find_layer(*GDSGenerator.PRBOUNDARY_LAYER)
                    if oidx is not None:
                        ob = layout.top_cell().dbbox(oidx)
                        if not ob.empty():
                            outline_bbox = (ob.left, ob.bottom,
                                            ob.width(), ob.height())
            except Exception as e:
                print(f"Warning: Could not read interposer GDS bbox: {e}",
                      file=sys.stderr)

    try:
        # Guarded read (H-B): delegate to the vendored reference loader rather
        # than a bare safe_load, so the tolerant format_version gate and the
        # intermediate guard run here too. allow_intermediate=True because this
        # IS the finalizer and its input still carries finalize_required. A
        # ChipletFormatError (unsupported major / malformed) is caught by the
        # broad `except Exception` below and converted to return False, which
        # the caller maps to a nonzero exit (convert_hyp_to_gds -> main), so a
        # refusal never reports success.
        data = _vendored_cfio().load(str(chiplet_file), allow_intermediate=True)

        # Every die sized or exempt before a byte is written (DieOutlineRefused
        # otherwise). In a worker run this repeats the head's decision on the
        # same file through the same reader, so it opens nothing again; a
        # direct caller gets the same refusals the worker does.
        if io_pad_refs is None:
            io_pad_refs = [str(p['ref']) for p in io_pads or []
                           if isinstance(p, dict) and p.get('ref')]
        die_plan = resolve_die_outlines(
            data, str(chiplet_file.resolve().parent), devices,
            die_outline_layers, die_outline_exempt, outline_reader,
            io_pad_refs)
        # Decided here for the same reason, and repeating the head's decision
        # in a worker run.
        technology_plan = (_technology_overlay.plan(data, system_chiplet,
                                                    io_pad_refs)
                           if system_chiplet is not None else None)
        # The same again, and the defence for a direct caller: the flow
        # checks the frame at admission, before the board exists.
        layout_plan = resolve_die_layouts(data, die_plan, die_layouts,
                                          outline_reader)
        # The mount plane, from the PDK unless given; before any write too.
        if isinstance(attachment_surface_z, AttachFrame):
            frame = attachment_surface_z
        elif attachment_surface_z is None:
            frame = read_attach_frame()
        else:
            frame = AttachFrame.given(attachment_surface_z)
        attachment_surface_z = frame.at()

        # Find and update the interposer component
        updated = False
        for component in data.get('components', []):
            if component.get('id') == 'interposer':
                abs_path = str(Path(interposer_gds_path).resolve())
                # Colocated artifacts get a plain relative reference, so the
                # exported set stays portable: move/copy/commit the output
                # directory and it still opens.
                layout_ref = _layout_ref(interposer_gds_path,
                                         str(chiplet_file.parent))
                component['layout'] = layout_ref

                # Read top_cell from interposer GDS. Not a refusal: the
                # interposer GDS is already on disk by now, and its top cell
                # is this run's own -c name. A failure is reported, and
                # top_cell stays what the .chiplet had.
                try:
                    component['top_cell'] = _read_gds_top_cell(abs_path)
                except DieGdsError as exc:
                    print(f"Warning: interposer top_cell not written: {exc}",
                          file=sys.stderr)

                if 'dimensions' not in component:
                    component['dimensions'] = {}

                # The die-attachment (BEOL-top) surface is a component-level
                # field, decoupled from the physical body. dimensions.thickness
                # is intentionally NOT overwritten here: it keeps the KiCad
                # board-stackup value the writer emitted (the physical interposer
                # body). attachment_surface_z is the mount plane.
                component['attachment_surface_z'] = attachment_surface_z

                # Update width/height from bbox
                if bbox:
                    x_min, y_min, width, height = bbox

                    # Dimensions: the fab outline (KiCad Edge.Cuts ->
                    # prBoundary 235/0) when drawn; the drawn-geometry
                    # bbox otherwise (legacy GDS without an outline).
                    if outline_bbox:
                        dim_w, dim_h = outline_bbox[2], outline_bbox[3]
                        dim_src = "board outline, prBoundary 235/0"
                    else:
                        dim_w, dim_h = width, height
                        dim_src = "drawn-geometry bbox"
                        # Loud degradation signal. Without the fab outline the
                        # interposer is sized from whatever copper happens to be
                        # drawn, and every die/pad downstream is anchored on that
                        # size and center -- so a missing outline silently shifts
                        # the whole assembly. Warn (stderr), and if the outline is
                        # merely on a pre-migration layer say so, since that is a
                        # one-command fix (regenerate).
                        pl, pd = GDSGenerator.PRBOUNDARY_LAYER
                        msg = (
                            f"Warning: prBoundary {pl}/{pd} outline not found in "
                            f"'{interposer_gds_path}'; interposer dimensions fall "
                            f"back to the drawn-geometry bbox "
                            f"({dim_w:.2f} x {dim_h:.2f} um), which is not the fab "
                            f"outline. Downstream placement anchors on this size "
                            f"and center, so dies/pads can shift in the assembly."
                        )
                        legacy = _probe_outline_layers(
                            interposer_gds_path,
                            GDSGenerator.LEGACY_PRBOUNDARY_LAYERS)
                        if legacy:
                            ll, ld = legacy
                            msg += (
                                f" An outline WAS found on the legacy prBoundary "
                                f"{ll}/{ld}: this GDS predates the {ll}/{ld}->"
                                f"{pl}/{pd} IntM4TM2 layer-map migration. "
                                f"Regenerate it with the current plugin so the "
                                f"outline lands on {pl}/{pd}."
                            )
                        else:
                            msg += (
                                " If this interposer should have a board outline, "
                                "add a closed Edge.Cuts in KiCad and re-export."
                            )
                        print(msg, file=sys.stderr)
                    component['dimensions']['width'] = dim_w
                    component['dimensions']['height'] = dim_h

                    # Per chiplet-system-spec/docs/coord_frame_contract.md
                    # section 1: position is the geometric center of
                    # the component in the canonical GDS-bbox-corner
                    # frame. The interposer's bbox center, expressed
                    # in its own bbox-corner frame, is (width/2,
                    # height/2).
                    #
                    # Position stays on the FULL bbox even when the
                    # dimensions come from the outline: with anchor:
                    # bbox_center the studio places the MESH bbox center
                    # (all GDS layers, outline included) at `position`.
                    # When the outline contains all drawn geometry -- the
                    # normal case -- both centers coincide; when copper
                    # leaks off-board they don't, and keeping the mesh
                    # contract preserves die/pillar registry (the loud
                    # off-board warning fires in convert_hyp_to_gds).
                    if 'position' not in component:
                        component['position'] = {}
                    component['position']['x'] = width / 2.0
                    component['position']['y'] = height / 2.0

                    # Per coord_frame_contract.md section 2: the
                    # interposer mesh is centered on its own GDS bbox.
                    component['anchor'] = 'bbox_center'

                    body_t = component['dimensions'].get('thickness')
                    print(f"Updated interposer: layout={layout_ref}")
                    print(f"  dimensions: {dim_w:.2f} x {dim_h:.2f} um "
                          f"({dim_src}); body thickness={body_t} um (kept), "
                          f"attachment_surface_z={attachment_surface_z} um")
                    print(f"  position: ({width/2.0:.2f}, {height/2.0:.2f}) um "
                          f"(bbox center, canonical GDS-bbox-corner frame)")
                    print(f"  anchor: bbox_center")
                else:
                    print(f"Updated interposer layout path to: {layout_ref}")
                    print(f"  attachment_surface_z={attachment_surface_z} um")

                if io_pads is not None:
                    # Per chiplet-system-spec/docs/coord_frame_contract.md
                    # sections 1 and 6.1: io_pads positions live in
                    # the same canonical GDS-bbox-corner frame as the
                    # interposer they nest under. The JSON producer
                    # (kicad_pcb_to_iopads.py) emits in HYP-absolute
                    # (1e5+ um values relative to the HYP file origin,
                    # which is the GDS-internal coord system since the
                    # interposer GDS is itself produced from HYP).
                    # Subtracting the GDS bbox lower-left lands every
                    # pad in the canonical frame. If bbox is None we
                    # cannot rebase; fall back to pass-through and
                    # warn so the leak is visible in the log.
                    if bbox is not None:
                        gds_left, gds_bottom = bbox[0], bbox[1]
                    else:
                        gds_left, gds_bottom = 0.0, 0.0
                        print("  Warning: no bbox available; io_pads "
                              "not re-anchored (will be in HYP-absolute, "
                              "out of canonical frame)", file=sys.stderr)
                    component['io_pads'] = [
                        {
                            'id': p.get('ref') or f"J{i+1}",
                            'io_class': p.get('io_class', 'wire_bond'),
                            'net': p.get('net', ''),
                            'position': {
                                'x': float(p.get('x_um', 0.0)) - gds_left,
                                'y': float(p.get('y_um', 0.0)) - gds_bottom,
                            },
                            'size': {
                                'x': float(p.get('size_x_um', 0.0)),
                                'y': float(p.get('size_y_um', 0.0)),
                            },
                            'layer': 'TopMetal2',
                        }
                        for i, p in enumerate(io_pads)
                    ]
                    print(f"  Injected {len(io_pads)} io_pads under interposer "
                          f"(re-anchored to GDS-bbox-corner: "
                          f"shift = ({-gds_left:+.2f}, {-gds_bottom:+.2f}) um)")

                updated = True
                break

        if not updated:
            print(f"Warning: No 'interposer' component found in {chiplet_path}")
            return False

        # Deduplicate top-level components that are now under interposer.io_pads.
        # Some flows (KiCad schematic with wire-bond footprints, exported to the
        # .chiplet by a manual/external tool) create one top-level 'die' component
        # per pad alongside the io_pads block. After injection those entries are
        # duplicates and show as scattered components in Chiplet Studio; drop them
        # so the pads appear only grouped under the interposer.
        if io_pads:
            pad_refs = {p.get('ref') for p in io_pads if p.get('ref')}
            if pad_refs:
                before = len(data['components'])
                data['components'] = [c for c in data['components']
                                      if c.get('id') not in pad_refs]
                removed = before - len(data['components'])
                if removed:
                    print(f"  Removed {removed} top-level components "
                          f"superseded by interposer.io_pads")

        # Inject connection_stacks and set connection on die components.
        # Per-die overrides (die_connections) win over the assembly-global
        # connection_type; the stacks of every method in use are injected.
        die_conns = die_connections or {}
        if connection_type or die_conns:
            default_stacks = get_default_connection_stacks()
            wanted = set(v for v in die_conns.values() if v)
            if connection_type:
                wanted.add(connection_type)
            # Resolve each wanted method: default library first, then any
            # manifest method that defines a connection stack (methods like
            # the vendorx demo sit outside the default library on purpose).
            resolved_stacks = {}
            for m in wanted:
                stack = default_stacks.get(m) or _connection_stack_from_manifest(m)
                if stack is not None:
                    resolved_stacks[m] = stack
            for m in sorted(wanted - set(resolved_stacks)):
                print(f"Warning: Unknown connection type '{m}', "
                      f"available: {list(default_stacks.keys())}", file=sys.stderr)
            known = set(resolved_stacks)
            if known:
                # Add connection_stacks section if not already present
                if 'connection_stacks' not in data:
                    data['connection_stacks'] = default_stacks
                    print(f"Injected connection_stacks ({len(default_stacks)} types)")
                for m in sorted(known):
                    if m not in data['connection_stacks']:
                        data['connection_stacks'][m] = resolved_stacks[m]
                        print(f"  Added connection stack '{m}'")

                # Set connection on each die component (per-die override
                # first, assembly default otherwise).
                # Respect per-die orientation: only assign connection to flip_chip dies
                for component in data.get('components', []):
                    comp_type = component.get('type', '')
                    if comp_type == 'die':
                        target = die_conns.get(component.get('id', ''),
                                               connection_type)
                        if not target or target not in known:
                            continue  # unknown methods warned above
                        orient = component.get('orientation', '')
                        if orient == 'flip_chip' or not orient:
                            component['connection'] = target
                            print(f"  Set connection={target} on {component.get('id')}")
                        else:
                            print(f"  Skipped {component.get('id')} (orientation={orient})")

        # Auto-declare the interconnect.adapter matching the dies' connection
        # method (manifest = single source of truth), whether the connection was
        # chosen via --connection-type or carried in from the board. Drives the
        # chiplet-studio 3D body render and the ADK interconnect DRC; an explicit
        # adapter already on the .chiplet is never overwritten.
        adapter = _maybe_set_interconnect_adapter(data)
        if adapter:
            print(f"  Set interconnect.adapter={adapter} (matches die connection)")

        # Compute die z-values from connection_stacks
        connection_stacks = data.get('connection_stacks', {})
        die_thicks = die_thicknesses or {}
        for component in data.get('components', []):
            if component.get('type') != 'die':
                continue

            # Per-die physical thickness (dimensions.thickness, um). The
            # intermediate writer emits the placeholder 0.0 (byte-exact
            # parity with the C++ exporter); the real value from the
            # board's DIE_THICKNESS_UM fields lands here, like the per-die
            # connection above. position.z is untouched: it remains the
            # z-mounting seating plane, not a function of the die body.
            thickness = die_thicks.get(component.get('id', ''))
            if thickness is not None:
                if 'dimensions' not in component:
                    component['dimensions'] = {}
                component['dimensions']['thickness'] = float(thickness)
                print(f"  {component.get('id')}: dimensions.thickness = "
                      f"{float(thickness)} um")

            # Width and height. A declared die gets its outline's size, from
            # the integer box and the die layout's dbu as exact Decimals
            # (exact_float refused at the read anything the field cannot
            # carry). The float written is the one whose repr is that exact
            # value, so the file holds 123.457, never the 123.45700000000001
            # float multiplication gives for 123457 DBU at dbu 0.001. An
            # exempt die keeps the width/height already in the file.
            die_ref = _component_ref(component)
            outline = die_plan.outlines.get(die_ref)
            if outline is not None:
                dims = component.setdefault('dimensions', {})
                dims['width'] = _die_outline.exact_float(outline.width_um)
                dims['height'] = _die_outline.exact_float(outline.height_um)
                print(f"  {die_ref}: dimensions {outline.width_um} x "
                      f"{outline.height_um} um (die outline "
                      f"{_die_outline.format_layer(outline.layer)} of "
                      f"{outline.gds.top_cell} in {outline.gds.path})")
            elif die_ref in die_plan.exempt:
                dims = component.get('dimensions') or {}
                print(f"  {die_ref}: exempt from the die outline read; keeps "
                      f"dimensions {dims.get('width')} x {dims.get('height')} "
                      f"um")
            else:
                # Not in the plan: resolve_die_outlines refuses every other
                # die, so this is a wire-bond pad written as a die whose
                # --io-pads entry add_io_pads skipped, and the dedup above
                # therefore kept it. It is left as it is.
                print(f"  {die_ref}: not sized: an --io-pads entry carries "
                      f"this id, so it is a wire-bond pad, not a die")

            # Per chiplet-system-spec/docs/coord_frame_contract.md section 2:
            # dies produced by the gds_to_kicad pipeline have
            # GDS (0,0) as the footprint anchor. The mesh is built
            # around that origin; position places it in the canonical
            # frame.
            component['anchor'] = 'gds_origin'

            conn_id = component.get('connection', '')
            if conn_id and conn_id in connection_stacks:
                stack = connection_stacks[conn_id]
                heights = [layer.get('height', 0.0)
                           for layer in stack.get('layers', [])]
                stack_height = float(sum((_as_written(h) for h in heights),
                                         Decimal(0)))
                die_z = frame.at(*heights)
                if 'position' not in component:
                    component['position'] = {}
                component['position']['z'] = die_z
                print(f"  {component.get('id')}: z = {attachment_surface_z} + "
                      f"{stack_height} = {die_z} um (connection: {conn_id})")
            elif 'position' in component and component['position'].get('z', 0) == 0:
                # No connection stack -- default z to the attachment surface.
                component['position']['z'] = attachment_surface_z

            # Re-anchor die x/y to the GDS bbox corner. KiCad's exporter
            # writes positions relative to the PCB bbox, which doesn't
            # always match the GDS routing bbox; the residual shift
            # (typically a few hundred microns) shows up in Chiplet
            # Studio as the die floating off the cu-pillars.
            if devices is not None and bbox is not None:
                gds_left, gds_bottom = bbox[0], bbox[1]
                device_map = {dev.ref: dev for dev in devices
                              if getattr(dev, 'gds_file', '')}
                ref = component.get('id', '')
                dev = device_map.get(ref)
                if dev is not None:
                    # Use the same converters add_device uses to place the die
                    # (to_um for X, to_um_y for Y) so the .chiplet position
                    # matches the GDS exactly. Fall back to the METRIC
                    # (HYP-metres) factor when a caller omits them.
                    if to_um is not None and to_um_y is not None:
                        abs_x_um = to_um(dev.x)
                        abs_y_um = to_um_y(dev.y)
                    else:
                        abs_x_um = dev.x * 1e6
                        abs_y_um = dev.y * 1e6
                    new_x = abs_x_um - gds_left
                    new_y = abs_y_um - gds_bottom
                    old = component.get('position', {})
                    print(f"  {ref}: re-anchored x/y "
                          f"({old.get('x', 0):.2f},{old.get('y', 0):.2f}) -> "
                          f"({new_x:.2f},{new_y:.2f}) [GDS bbox corner]")
                    component['position']['x'] = new_x
                    component['position']['y'] = new_y

            # top_cell of the die GDS, as resolve_die_outlines read it: the
            # device's GDS path, which the HYP parser already resolved against
            # the board directory (a board-relative die layout such as
            # ../chiplets/die.gds resolves here regardless of the output
            # directory, matching the geometry the assembly was built from),
            # else the .chiplet layout field, resolved against the .chiplet's
            # own directory (where readers anchor a relative layout) rather
            # than the process CWD. The plan holds it for declared and exempt
            # dies alike; a die whose GDS could not be read never gets here,
            # and a wire-bond pad written as a die keeps what it had. The same
            # choice of path, with ${VAR} left unexpanded, decides the
            # bundling below.
            die_read = die_plan.gds(die_ref)
            if die_read is not None:
                component['top_cell'] = die_read.top_cell
            die_gds = ''
            ref = component.get('id', '')
            dev = None
            if devices is not None:
                dev = next((d for d in devices
                            if d.ref == ref and getattr(d, 'gds_file', '')),
                           None)
                if dev is not None:
                    die_gds = dev.gds_file
            if not die_gds:
                die_gds = component.get('layout', '')
                if die_gds and "${" not in die_gds and not os.path.isabs(die_gds):
                    die_gds = os.path.normpath(os.path.join(
                        str(chiplet_file.resolve().parent), die_gds))

            # A die given a full layout (--die-layouts): the component names
            # that file, with the contract's top cell, never the file's one
            # parentless cell. The abstract stays what the assembly places,
            # and the metadata names it as it was composed, by the same choice
            # of path the plan read. Bundling below decides on the full file.
            die_layout = layout_plan.get(die_ref)
            if die_layout is not None:
                abstract = _die_gds_path(
                    die_ref, component,
                    {d.ref: d for d in devices or [] if d.gds_file},
                    str(chiplet_file.resolve().parent))
                metadata = component.get('metadata')
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata['assembly_view'] = _die_layouts.ASSEMBLY_VIEW
                metadata['assembly_view_layout'] = _layout_ref(
                    abstract, str(chiplet_file.parent))
                component['metadata'] = metadata
                component['top_cell'] = die_layout.top_cell
                component['layout'] = die_layout.layout
                die_gds = die_layout.layout

            # Self-contained die GDS. The die layout is a board-relative path
            # (e.g. ../chiplets/die.gds) copied verbatim from the board; it
            # resolves from the shipped example layout but dangles when the
            # output is regenerated into an unrelated directory, so the viewer
            # then renders an empty die box. If the recorded layout does not
            # resolve against the .chiplet's OWN directory (where readers anchor
            # a relative layout), bundle the die GDS next to the output and
            # point at it, so a moved/regenerated bundle still renders. When it
            # already resolves (the shipped demo, whose ../chiplets/ sibling
            # exists), leave it untouched: no 34 MB copy, no machine path, and
            # byte-stable for the reproducibility gate. An absolute layout that
            # resolves is rewritten relative to the .chiplet when the two share
            # a root (see _layout_ref), so it opens from every mount of that
            # root. Only real die devices (dev is not None) and dies given a
            # full layout are bundled or rewritten; the interposer GDS is
            # colocated.
            if ((dev is not None or die_layout is not None) and die_gds
                    and os.path.isfile(die_gds)):
                out_dir = chiplet_file.resolve().parent
                recorded = component.get('layout', '')
                resolves = False
                if recorded and "${" not in recorded:
                    cand = (recorded if os.path.isabs(recorded)
                            else os.path.normpath(
                                os.path.join(str(out_dir), recorded)))
                    resolves = os.path.isfile(cand)
                if not resolves:
                    if not ref:
                        raise ValueError("Cannot bundle a die layout without "
                                         "a nonempty component id")
                    # Each instance owns a directory: independent ready dies
                    # often both use layout.gds. A flat basename (or an
                    # ambiguous ref_basename concatenation) loses one source.
                    # Encode refs as one component, including '.'/'..' refs.
                    bundle_ref = quote(ref, safe="").replace(".", "%2E")
                    bundle_dir = out_dir / "chiplets" / bundle_ref
                    bundle_dir.mkdir(parents=True, exist_ok=True)
                    dest = bundle_dir / os.path.basename(die_gds)
                    if os.path.realpath(die_gds) != os.path.realpath(str(dest)):
                        shutil.copy2(die_gds, dest)
                    component['layout'] = os.path.join(
                        "chiplets", bundle_ref, os.path.basename(die_gds))
                    print(f"  {ref}: bundled die GDS -> {component['layout']} "
                          f"(self-contained; ../chiplets not reachable from "
                          f"the output dir)")
                elif os.path.isabs(recorded):
                    component['layout'] = _layout_ref(
                        recorded, str(chiplet_file.parent))
            if die_layout is not None:
                print(f"  {ref}: layout {component['layout']} (full layout, "
                      f"top cell {die_layout.top_cell}); the assembly view "
                      f"shows the abstract "
                      f"{component['metadata']['assembly_view_layout']}")

        # Strip the intermediate-frame marker emitted by KiCad's
        # exporter (see kicad/pcbnew/exporters/export_chiplet.cpp).
        # Per chiplet-system-spec/docs/coord_frame_contract.md section 4, item 1,
        # this finalize step converts to the canonical frame; the
        # canonical .chiplet has no _metadata block. Pop is no-op when
        # the input was already finalized (idempotent re-run).
        if data.pop('_metadata', None) is not None:
            print("  Stripped _metadata.finalize_required marker "
                  "(file is now canonical)")

        # Passthrough writer stamp (H-B crux): this finalizer re-emits the whole
        # loaded dict (unknown top-level keys included), so the stamped
        # format_version must describe the bytes written. check_format_version
        # returns the normalized version -- preserving a same-major higher minor,
        # normalizing an equal/lower/unquoted value to the supported string --
        # and re-warns on a higher minor. It never stamps a higher-minor input
        # DOWN to the baseline (which would forge a "1.0" label over 1.1 bytes).
        data['format_version'] = _vendored_cfio().check_format_version(
            data.get('format_version'))

        # An empty intermediate technologies block parses as None. Canonical
        # output uses an empty mapping, without inventing any technology IDs
        # or adding the optional block when the input omitted it.
        if 'technologies' in data and data['technologies'] is None:
            data['technologies'] = {}
        if technology_plan is not None:
            _technology_overlay.apply(data, *technology_plan)

        # Write back the updated file, atomically. A plain open(..., 'w')
        # truncates the existing document before a single byte of the new one
        # is written, so ANY failure in yaml.dump -- an unserializable value, a
        # full disk, the process being killed -- left a truncated .chiplet
        # where a valid one used to be. That is a data-loss window on the file
        # this whole module exists to protect, and it is not specific to any
        # one failure: it makes the class moot rather than handling members of
        # it. The temp file is a sibling so os.replace stays on one filesystem,
        # where it is atomic; a cross-device rename would not be.
        _atomic_write_text(
            chiplet_file,
            yaml.dump(data, default_flow_style=False, sort_keys=False),
        )

        print(f"Chiplet file updated: {chiplet_path}")
        return True

    except SourceRefused:
        # A refusal must reach the boundary as itself. This handler's job is to
        # turn ordinary failures into a False that the caller maps to exit 1,
        # which means bad CALLER input -- the wrong thing to tell someone whose
        # PDK is misaligned, because no argument they can change fixes it.
        raise
    except (DieOutlineRefused, TechnologyRefused, DieLayoutRefused,
            AttachFrameRefused):
        # Same reason: it names every refused die, and flattening it into
        # "Error updating chiplet file" would lose which ones and why.
        raise
    except Exception as e:
        if _is_manifest_version_error(e):
            # Raised raw by _connection_to_adapter and _connection_to_body_
            # diameter, which catch only (KeyError, FileNotFoundError). Without
            # this arm the same refusal would leave this one function as exit 6
            # through one helper and exit 1 through another.
            raise SourceRefused(str(e), exit_code=EXIT_SOURCE_VERSION) from e
        print(f"Error updating chiplet file: {e}", file=sys.stderr)
        return False


def _manifest_refusal(exc, what):
    """Wrap a reader exception as a typed refusal naming what could not be read.

    The manifest helpers used to let the reader's own exception escape raw.
    That was better than swallowing it, but it surfaced as a traceback out of
    main with exit 1, which means bad caller input, and in
    ``convert_hyp_to_gds`` it arrived AFTER the interposer GDS was already on
    disk. A refusal that lands after the artifact exists is a report, not a
    refusal. Typed, it is reported by the boundary with the shared table's code.
    """
    return SourceRefused(
        "the interconnect PDK manifest is present but unusable, so %s could "
        "not be read (%s: %s)" % (what, type(exc).__name__, exc),
        exit_code=_source_refusal_exit_code(exc),
    )


def _atomic_write_text(path, text):
    """Replace ``path`` with ``text`` atomically, or leave it untouched.

    Writes a sibling temp file, flushes it to the platter, then ``os.replace``,
    which is atomic within one filesystem. A reader either sees the whole old
    document or the whole new one, never a half-written one.
    Existing destination permissions are preserved; new files honor umask.

    The sibling directory matters: ``os.replace`` across filesystems is not
    atomic (and on some platforms not even permitted), so a temp file in /tmp
    would silently give back the guarantee this exists to provide.

    The fsync is what makes the guarantee survive a power loss rather than only
    a crashed process. Without it the rename can reach the disk before the
    bytes do, which is the one ordering that produces an empty file where a
    valid document used to be.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = None
    fd, tmp = create_output_temp(directory=directory, prefix=".chiplet-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            if mode is not None:
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), mode)
                else:  # Windows exposes chmod, but not fchmod.
                    os.chmod(tmp, mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Never leave the temp file behind on a failure; the original document
        # is still intact because os.replace has not run.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_manifest_version_error(exc):
    """True if ``exc`` is the interconnect reader's version refusal.

    Reads the module off ``sys.modules`` so a failure-reporting path never
    triggers an import, a diagnostic, or a refusal of its own.
    """
    im = sys.modules.get("interconnect_manifest")
    version_cls = _manifest_class(im, "ManifestVersionError") if im else ()
    return bool(version_cls) and isinstance(exc, version_cls)


# Compatibility entry points for worker consumers; the implementation is
# shared with carrier preflight without importing KLayout or the GUI.
_PATH_VAR_MARKERS = _layer_map.PATH_VAR_MARKERS


def _or_exit(read, *args, **kwargs):
    """``read(*args, **kwargs)``, ending the run on a LayerMapError.

    A refused carrier root is a ValueError, and callers sit inside except
    ValueError and except Exception blocks that would turn it into a skipped
    pad or a warning and draw on. An exit passes them.
    """
    try:
        return read(*args, **kwargs)
    except _layer_map.LayerMapError as exc:
        sys.exit(str(exc))


def _discover_path_var(name: str) -> Optional[str]:
    return _or_exit(_layer_map.discover_path_var, name, start=__file__)


def _expand_path_vars(path: Optional[str]) -> Optional[str]:
    return _or_exit(_layer_map.expand_path_vars, path,
                    discover=_discover_path_var)


def _find_default_lyp() -> str:
    return _or_exit(_layer_map.find_default_lyp, start=__file__)


def _find_interposer_template() -> str:
    """Best-effort path to the interposer KiCad template board.

    Discovery mirrors _find_default_lyp: resolve INTERPOSER_PDK_ROOT
    (environment -> sibling walk), then
    ``libs.tech/kicad/interposer_template.kicad_pcb`` under it. Returns the
    unexpanded ``${INTERPOSER_PDK_ROOT}`` form when no checkout resolves,
    so error text still points somewhere actionable.
    """
    sub = ("libs.tech", "kicad", "interposer_template.kicad_pcb")
    root = _discover_path_var("INTERPOSER_PDK_ROOT")
    if root:
        cand = Path(root).joinpath(*sub)
        if cand.is_file():
            return str(cand)
    return "${INTERPOSER_PDK_ROOT}/" + "/".join(sub)


def _expand_gds_path(gds_path: str) -> str:
    """``gds_path`` with ${VAR} references expanded; DieGdsError if one is not."""
    try:
        return _layer_map.expand_path_vars(gds_path,
                                           discover=_discover_path_var)
    except _layer_map.LayerMapError as exc:
        raise DieGdsError("cannot resolve GDS path %s: %s"
                          % (gds_path, exc)) from None


def _read_gds_top_cell(gds_path: str) -> str:
    """The one top cell of a GDS file, resolved the way a die's top cell is.

    Raises DieGdsError when the file is missing or unreadable, or holds no
    cell or more than one top cell. There is no None: a caller that writes
    top_cell from this either has the one cell or knows why not.
    """
    return _die_outline.read_top_cell(_expand_gds_path(gds_path))


def _component_ref(component: Dict) -> str:
    """A .chiplet component's id, as the reference HYP devices and flags use."""
    ident = component.get('id')
    return '' if ident is None else str(ident)


def _io_pad_refs(io_pads_json: Optional[str]) -> List[str]:
    """The references of every entry of an --io-pads file, placed or not.

    Read before anything is placed, for resolve_die_outlines at the worker
    head; the .chiplet update is handed the same list, so both decide on the
    same set whichever entries add_io_pads then skips. A file add_io_pads
    could not read gives none (add_io_pads reports it).
    """
    if not io_pads_json:
        return []
    try:
        with open(io_pads_json, 'r') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    pads = data.get('io_pads') if isinstance(data, dict) else None
    if not isinstance(pads, list):
        return []
    return [str(p['ref']) for p in pads if isinstance(p, dict) and p.get('ref')]


def _die_gds_path(ref: str, component: Optional[Dict],
                  device_by_ref: Dict[str, 'Device'],
                  chiplet_dir: str) -> Optional[str]:
    """The GDS a die's outline and top cell are read from, or None.

    The HYP device's GDS_FILE first: the parser already resolved it against
    the board directory, and it is the file add_device places. Otherwise the
    component's layout, resolved against the .chiplet's own directory, where
    readers anchor a relative layout. ${VAR} references are expanded
    (DieGdsError when one cannot be).
    """
    dev = device_by_ref.get(ref)
    path = dev.gds_file if dev is not None else ''
    if not path and component is not None:
        path = str(component.get('layout') or '')
        if path and "${" not in path and not os.path.isabs(path):
            path = os.path.normpath(os.path.join(chiplet_dir, path))
    if not path:
        return None
    return _expand_gds_path(path) if "${" in path else path


def resolve_die_outlines(chiplet_data: Optional[Dict], chiplet_dir: str,
                         devices: Optional[List['Device']],
                         outline_layers: Optional[Dict[str, Tuple[int, int]]]
                         = None,
                         outline_exempt: Optional[List[str]] = None,
                         reader: Optional['_die_outline.DieGdsReader'] = None,
                         io_pad_refs: Optional[Iterable[str]] = None
                         ) -> '_die_outline.DieOutlinePlan':
    """Decide, for every die, its outline or its exemption; refuse otherwise.

    With a .chiplet (``chiplet_data``), its components of type die are the
    dies, and each must be named by exactly one of ``outline_layers``
    ({ref: (layer, datatype)}, --die-outline-layers) and ``outline_exempt``
    (--die-outline-exempt). Without one, a named reference must be a HYP
    device, and a device nobody names keeps its cell bbox as the boundary.

    ``io_pad_refs`` are the references of the --io-pads entries. A .chiplet
    die component with one of these ids is a wire-bond pad written as a die
    (by a tool other than this plugin's writer); update_chiplet_file replaces
    it with the interposer's io_pads, so it is not a die here, needs no flag,
    and a flag naming it refuses.

    A declared die gets its outline read from its own GDS; an exempt die's GDS
    is still opened and its top cell resolved, so a missing or unreadable
    file refuses for either. A reference that two .chiplet dies share, or
    that two HYP devices share while it is a die or named by a flag, refuses,
    since a per-reference declaration cannot tell them apart. A shared HYP
    reference nothing else uses (two ``REF**`` footprints) is not a die and
    is left alone.

    Every offender is collected before raising DieOutlineRefused, so one run
    names all of them. Nothing is written here.
    """
    layers = dict(outline_layers or {})
    exempt = list(outline_exempt or [])
    declared_any = bool(layers or exempt)
    pads = set(io_pad_refs or ())
    reader = reader if reader is not None else _die_outline.DieGdsReader()
    devices = list(devices or [])
    device_by_ref = {dev.ref: dev for dev in devices if dev.gds_file}
    duplicated = set()
    chiplet_offenders: List[Tuple[str, str]] = []
    flag_offenders: List[Tuple[str, str]] = []

    if chiplet_data is not None:
        components = [c for c in chiplet_data.get('components') or []
                      if isinstance(c, dict)]
        all_dies = [(_component_ref(c), c) for c in components
                    if c.get('type') == 'die']
        dies = [(ref, c) for ref, c in all_dies if ref not in pads]
        for ref, count in Counter(ref for ref, _c in dies).items():
            if count > 1:
                duplicated.add(ref)
                chiplet_offenders.append((
                    ref, "%d .chiplet die components carry this id; a per-die "
                         "outline cannot tell them apart" % count))
        die_refs = {ref for ref, _c in dies}
        pad_dies = {ref for ref, _c in all_dies if ref in pads}
        other_types = {_component_ref(c): c.get('type') for c in components
                       if c.get('type') != 'die'}
        for flag, named in (("--die-outline-layers", layers),
                            ("--die-outline-exempt", exempt)):
            for ref in named:
                if ref in die_refs:
                    continue
                if ref in pad_dies:
                    why = ("an --io-pads entry carries this id, so the .chiplet "
                           "die component with it is a wire-bond pad that the "
                           "interposer's io_pads replace, not a die")
                elif ref in other_types:
                    why = ("the .chiplet component with this id is of type "
                           "%r, not a die" % other_types[ref])
                else:
                    why = "the .chiplet has no die component with this id"
                flag_offenders.append((ref, "%s names it, but %s"
                                       % (flag, why)))
        used = die_refs | set(layers) | set(exempt)
    else:
        for flag, named in (("--die-outline-layers", layers),
                            ("--die-outline-exempt", exempt)):
            for ref in named:
                if ref not in device_by_ref:
                    flag_offenders.append((ref, "%s names it, but no HYP "
                                                "device carries this "
                                                "reference" % flag))
        dies = [(dev.ref, None) for dev in devices
                if dev.ref in layers or dev.ref in exempt]
        used = set(layers) | set(exempt)

    offenders: List[Tuple[str, str]] = []
    for ref, count in Counter(dev.ref for dev in devices).items():
        if count > 1 and ref in used:
            duplicated.add(ref)
            offenders.append((ref, "%d HYP devices carry this reference; a "
                                   "per-die outline cannot tell them apart"
                              % count))
    offenders += chiplet_offenders + flag_offenders

    # (ref, path, layer or None for exempt), in die order, one per reference.
    reads: List[Tuple[str, str, Optional[Tuple[int, int]]]] = []
    seen = set()
    for ref, component in dies:
        if ref in seen or ref in duplicated:
            continue
        seen.add(ref)
        if ref in layers and ref in exempt:
            offenders.append((ref, "both --die-outline-layers and "
                                   "--die-outline-exempt name this die; it is "
                                   "either sized from its outline or exempt"))
            continue
        if ref not in layers and ref not in exempt:
            if declared_any:
                cause = ("neither --die-outline-layers nor "
                         "--die-outline-exempt names this die")
            else:
                cause = ("no die outline layer was given for any die (neither "
                         "--die-outline-layers nor --die-outline-exempt), so "
                         "this die cannot be sized from its GDS")
            offenders.append((ref, cause + ". Set the footprint field "
                              "DIE_OUTLINE_LAYER to the LAYER/DATATYPE its GDS "
                              "draws the die outline on (for example 39/4 for "
                              "SG13G2 dies) and export again"))
            continue
        try:
            path = _die_gds_path(ref, component, device_by_ref, chiplet_dir)
        except DieGdsError as exc:
            offenders.append((ref, str(exc)))
            continue
        if path is None:
            offenders.append((ref, "no die GDS to read: no HYP device with a "
                                   "GDS_FILE carries this reference and the "
                                   ".chiplet component has no layout"))
            continue
        reads.append((ref, path, layers.get(ref)))

    # Each file is opened once, with every layer any die declares on it,
    # however many spellings (a symlink, a ${VAR}) name it.
    wanted: Dict[str, set] = {}
    for _ref, path, layer in reads:
        layers_here = wanted.setdefault(os.path.realpath(path), set())
        if layer is not None:
            layers_here.add(layer)
    for real, path_layers in wanted.items():
        reader.load(real, sorted(path_layers))

    plan = _die_outline.DieOutlinePlan()
    for ref, path, layer in reads:
        try:
            if layer is None:
                plan.exempt[ref] = reader.gds(path)
            else:
                plan.outlines[ref] = reader.outline(path, layer)
        except DieGdsError as exc:
            offenders.append((ref, str(exc)))

    if offenders:
        raise DieOutlineRefused(offenders)
    return plan


def resolve_die_layouts(chiplet_data: Optional[Dict],
                        die_plan: '_die_outline.DieOutlinePlan',
                        die_layouts: Optional[Dict[str,
                                                   '_die_layouts.DieLayout']],
                        reader: Optional['_die_outline.DieGdsReader'] = None
                        ) -> Dict[str, '_die_layouts.DieLayout']:
    """Check each --die-layouts entry against its die; refuse otherwise.

    A named reference must be a die of the .chiplet that ``die_plan`` sized
    or exempted, since the GDS read for it there is the abstract the
    assembly places. Its full layout and that abstract must then agree with
    the contract (die_layouts.frame_cause). The full layout is read through
    ``reader``, so a worker run opens it once.

    Every offender is collected before raising DieLayoutRefused, and
    nothing is written here. Returns the entries by reference, for the
    .chiplet update.
    """
    if not die_layouts:
        return {}
    reader = reader if reader is not None else _die_outline.DieGdsReader()
    types: Dict[str, object] = {}
    for component in (chiplet_data or {}).get('components') or []:
        if isinstance(component, dict):
            types.setdefault(_component_ref(component), component.get('type'))
    offenders: List[Tuple[str, str]] = []
    for ref, entry in die_layouts.items():
        abstract = die_plan.gds(ref)
        if abstract is None:
            if types.get(ref) == 'die':
                why = ("an --io-pads entry carries this id, so the .chiplet "
                       "die component with it is a wire-bond pad, not a die")
            elif ref in types:
                why = ("the .chiplet component with this id is of type %r, "
                       "not a die" % types[ref])
            else:
                why = "the .chiplet has no die component with this id"
            offenders.append((ref, "--die-layouts names it, but " + why))
            continue
        try:
            full = reader.tops(entry.layout)
        except DieGdsError as exc:
            offenders.append((ref, "full layout: %s" % exc))
            continue
        cause = _die_layouts.frame_cause(entry, full, abstract)
        if cause:
            offenders.append((ref, cause))
    if offenders:
        raise DieLayoutRefused(offenders)
    return dict(die_layouts)


# Python module dir inside the interconnect PDK (IHP libs.tech layout).
_INTERCONNECT_PY = ("libs.tech", "klayout", "python")


#: Exit codes from the ecosystem's shared table (interposer-pnr's README and
#: cli.py; adk-tools maps its registry errors to the same two). Only the two
#: this module can produce are named here; the worker's total exit-code mapping
#: is a separate row and this constant is not it.
EXIT_SOURCE_UNREACHABLE = 5   # a cross-repo source is present but unusable
EXIT_SOURCE_VERSION = 6       # a source declares a major this code must not read


class SourceRefused(Exception):
    """A cross-repo source is present and cannot be used. Never a soft path.

    Deliberately subclasses plain ``Exception`` and NOT ``ValueError``,
    ``KeyError`` or ``OSError``. Every one of those is a lookup-shaped type
    that some handler between here and the process boundary already catches to
    mean "not found, carry on", which is exactly how the defect this class
    exists to fix stayed invisible. A refusal that any broad handler can absorb
    is not a refusal.

    ``exit_code`` carries the shared table's number so the boundary reports the
    refusal as itself rather than as exit 1, which means bad CALLER input and
    would send the user looking for an argument to change. No argument fixes an
    unusable manifest.
    """

    def __init__(self, message, exit_code=EXIT_SOURCE_UNREACHABLE):
        super().__init__(message)
        self.exit_code = exit_code


def _manifest_class(im, name):
    """A reader's exception class by name, or ``()`` if it has none.

    Read off the already-imported module with getattr and never imported: the
    plugin must run with no interconnect PDK installed, and ``except ()``
    catches nothing. The empty-tuple fallback is complete rather than merely
    defensive, because a reader too old to define the class is also a reader
    that can never raise it: the interconnect PDK's main and dev ship a reader
    with no version gate at all, and the gate lives only on an unmerged branch.

    Used ONLY to choose the exit code and word the message. The decision to
    refuse never depends on recognising a class, so a renamed or removed class
    costs a line of diagnostic and not the protection.
    """
    cls = getattr(im, name, ())
    return cls if isinstance(cls, type) else ()


def _accept_newer_minor_warnings(im):
    """Stop a warnings-as-errors host from refusing a manifest the policy accepts.

    The shared version policy says a same-major higher minor is ACCEPTED with a
    warning. Under ``-W error`` that warning becomes an exception, so two hosts
    correctly implementing the same policy disagree about the same file.

    Worse, it disagrees with itself: the reader adds its dedup key BEFORE
    calling ``warnings.warn``, so only the FIRST read of a given manifest in a
    process raises and every later read succeeds silently. Measured against the
    real reader under ``-W error``: read 1 raised, reads 2 and 3 returned all 5
    methods. Anything that tried to handle that exception would be handling a
    condition that moves depending on who read the manifest first.

    ``filterwarnings`` rather than ``catch_warnings``: the GUI runs the export
    on a worker thread (dialog_chiplet_export.py), and ``catch_warnings``
    mutates global filter state for the whole process while it is open, so on a
    thread it is not sound. This installs one filter once, and the warning
    still prints.

    interconnect_pdk is raising the policy question itself, since the same gap
    is in chiplet-spec's reference reader; this is the local mitigation only.
    """
    warning_cls = _manifest_class(im, "ManifestVersionWarning")
    if warning_cls:
        warnings.filterwarnings("always", category=warning_cls)


def _interconnect_python_candidates():
    """Candidate interconnect-PDK python dirs: env first, then sibling walk."""
    candidates = []
    env = os.environ.get("INTERCONNECT_PDK_ROOT")
    if env:
        candidates.append(Path(env).joinpath(*_INTERCONNECT_PY))
    here = Path(__file__).resolve()
    for base in here.parents:
        for dirname in _PATH_VAR_MARKERS["INTERCONNECT_PDK_ROOT"][0]:
            candidates.append((base / dirname).joinpath(*_INTERCONNECT_PY))
    return candidates


def _import_interconnect_manifest():
    """Import the interconnect PDK manifest reader (sibling repo), or None.

    Located via $INTERCONNECT_PDK_ROOT or a sibling-repo search, mirroring
    _import_bump_mirror. The interconnect PDK owns the bump-method registry.
    """
    for cand in _interconnect_python_candidates():
        if (cand / "interconnect_manifest.py").is_file():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            try:
                import interconnect_manifest
            except Exception as exc:
                # The file is HERE and does not import, so the source is
                # present and unusable, not absent. Returning None said
                # "absent" and let every caller take its tolerated path; the
                # stderr line did not stop anything. That is the same swallow
                # this module was fixed for, one frame up, so it refuses too.
                raise SourceRefused(
                    "found interconnect_manifest.py in %s but could not "
                    "import it (%s: %s)" % (cand, type(exc).__name__, exc)
                ) from exc
            _accept_newer_minor_warnings(interconnect_manifest)
            return interconnect_manifest
    return None


def _connection_to_body_diameter(connection_type):
    """Body diameter (um) for a connection-stack id, or None.

    Sourced from the interconnect PDK manifest (single source of truth): the
    pillar, or the ball of a solder bump. Unknown/empty ids return None: no
    body, so no landing and no flip-chip attachment.
    """
    if not connection_type:
        return None
    im = _import_interconnect_manifest()
    if im is None:
        return None
    try:
        method = im.get_method(connection_type)
    except (KeyError, FileNotFoundError):
        # KeyError IS the "unknown method" signal here, unlike at the
        # technology-block site: this looks a method up BY ID, so the reader
        # raising KeyError means that id is not in the manifest. Keep the arm.
        return None  # unknown method, or a partial PDK install
    except Exception as exc:
        raise _manifest_refusal(exc, "body geometry for '%s'" % connection_type)
    return method.get("body_diameter_um")


class LandingCell(NamedTuple):
    """The pad cell bump_mirror draws one fab anchor's landings with."""
    anchor: str
    pcell: str               # the IntM4TM2 PCell
    group_prefix: str        # of each die's group cell, as CUPILLARS_<ref>
    openings: frozenset      # the (layer, datatype) UBM openings it draws
    table_6_1: bool          # the body diameter picks the opening (Cu pillar)
    generator_kwargs: dict   # what CuPillarGenerator takes to draw it


def _bump_mirror_refusal(connection_type, bm, exc):
    """SourceRefused for a bump_mirror whose fab anchor API answers with
    something other than a pad cell or a ValueError."""
    return SourceRefused(
        "%s: bump_mirror at %s is present but its fab anchor API is unusable "
        "(%s: %s)" % (connection_type, getattr(bm, "__file__", None),
                      type(exc).__name__, exc))


def _landing_cell(connection_type, anchor, bm):
    """The LandingCell bump_mirror has for fab anchor ``anchor``, or
    ValueError when it has none. Any other failure of bump_mirror's answer
    is SourceRefused: the interposer checkout, not the method, is broken."""
    if hasattr(bm, "fab_anchor_openings"):
        try:
            pad = bm.fab_anchor(anchor)
            openings = bm.fab_anchor_openings(anchor)
        except ValueError as exc:
            raise ValueError("%s: %s" % (connection_type, exc)) from exc
        except Exception as exc:
            raise _bump_mirror_refusal(connection_type, bm, exc) from exc
        if not isinstance(openings, dict):
            raise ValueError("%s: bump_mirror gives no opening table for fab "
                             "anchor %s" % (connection_type, anchor))
        try:
            return LandingCell(anchor, pad.pcell, pad.cell_prefix + "S_",
                               frozenset(openings.values()), pad.table_6_1,
                               {"fab_anchor_id": anchor})
        except Exception as exc:
            raise _bump_mirror_refusal(connection_type, bm, exc) from exc
    # An interposer PDK from before its per-anchor pad cells draws CuPillarPad
    # for every anchor: its openings are CUPILLAR_FAB_LAYERS less the
    # TopMetal2 landing, so any other anchor is refused by them, and its
    # generator takes no fab_anchor_id. Drop this branch once no supported
    # interposer release predates fab_anchor_openings.
    fab_layers = getattr(bm, "CUPILLAR_FAB_LAYERS", None)
    if not isinstance(fab_layers, dict):
        raise ValueError("%s: bump_mirror declares no pad-cell layer table "
                         "(fab_anchor_openings or CUPILLAR_FAB_LAYERS)"
                         % connection_type)
    return LandingCell(anchor, "CuPillarPad", "CUPILLARS_",
                       frozenset(pair for name, pair in fab_layers.items()
                                 if name != "TopMetal2"), True, {})


def _check_landing_anchor(connection_type, bm, manifest=None):
    """Refuse a method whose landing bump_mirror's pad cells cannot draw.

    The manifest's fab_anchors table names the UBM openings the method's
    anchor needs; ``bm`` is bump_mirror, whose pad cell for that anchor
    draws the openings fab_anchor_openings gives. A method that cannot be
    drawn is refused, not skipped: a GDS without its landings would look
    fabricable. Returns the method's LandingCell, or None for an id the
    manifest does not know (left to the body-geometry check). Raises
    ValueError with the reason. ``manifest`` is read when not given.
    """
    im = _import_interconnect_manifest()
    if im is None:
        return None
    try:
        if manifest is None:
            manifest = im.load_manifest()
        method = im.get_method(connection_type, manifest)
    except (KeyError, FileNotFoundError):
        return None  # unknown method, or a partial PDK install
    except Exception as exc:
        raise _manifest_refusal(exc, "the fab anchor of '%s'" % connection_type)
    if bm is None:
        raise ValueError(
            "%s needs bump_mirror, which is unavailable. Set "
            "INTERPOSER_PDK_ROOT to the interposer PDK checkout "
            "(libs.tech/klayout/python/bump_mirror.py) and retry. Refusing to "
            "emit a complete GDS without its landings." % connection_type)
    if not isinstance(method, dict):
        raise ValueError("%s: the manifest entry is not an object" % connection_type)
    anchor = method.get("fab_anchor")
    if not isinstance(anchor, str) or not anchor:
        raise ValueError("%s: fab_anchor must be a nonempty string" % connection_type)
    cell = _landing_cell(connection_type, anchor, bm)
    check_anchor_drawable(connection_type, anchor, manifest.get("fab_anchors"),
                          cell.openings)
    return cell


def connection_method_availability():
    """Every manifest method id, mapped to None or the reason it is refused.

    Runs _check_landing_anchor, the check the pad loop refuses on, for each
    method, so a list built from this offers only what an export can draw.
    A method whose own entry cannot be read is refused alone, as the pad loop
    would refuse only a die that uses it. Empty when no interconnect PDK is
    found; an unusable manifest raises SourceRefused.
    """
    im = _import_interconnect_manifest()
    if im is None:
        return {}
    try:
        manifest = im.load_manifest()
        methods = im.list_methods(manifest)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise _manifest_refusal(exc, "the method list")
    bm = _import_bump_mirror() if methods else None
    availability = {}
    for method in methods:
        try:
            _check_landing_anchor(method, bm, manifest)
        except (ValueError, SourceRefused) as exc:
            availability[method] = str(exc)
        else:
            availability[method] = None
    return availability


def _connection_to_adapter(connection_type):
    """Interconnect adapter id for a connection-stack id, or None.

    The interconnect.adapter selects the ADK interconnect DRC (IXN pitch/spacing)
    and the 3D body stackup fragment that chiplet-studio merges at render time.
    Sourced from the manifest so the method and its adapter stay in lockstep.
    """
    if not connection_type:
        return None
    im = _import_interconnect_manifest()
    if im is None:
        return None
    try:
        method = im.get_method(connection_type)
    except (KeyError, FileNotFoundError):
        return None  # unknown method, or a partial PDK install
    except Exception as exc:
        raise _manifest_refusal(exc, "the adapter for '%s'" % connection_type)
    return method.get("adapter")


# Layer properties of the interconnect PDK's 3D body layers. Writer-verbatim
# ${VAR} form (readers expand via the ecosystem discovery convention); covers
# every method in the manifest, so it is adapter-independent.
_INTERCONNECT_LYP_REF = (
    "${INTERCONNECT_PDK_ROOT}/libs.tech/klayout/tech/interconnect.lyp")


#: Why no technology block was derived. Exactly two values, because exactly two
#: conditions are legitimately not errors. Every other manifest condition
#: refuses instead of being folded into a third reason.
_TECH_ABSENT = "manifest_absent"      # no interconnect PDK reachable from here
_TECH_NOT_DECLARED = "not_declared"   # manifest read fine, no method declares it


def _interconnect_technology_block(adapter):
    """``(technology block, None)``, or ``(None, reason)``. Refuses on a bad source.

    Mirrors the entries under ``technologies:`` (description /
    layer_properties / dbu) so viewers treat the interconnect method as a
    PDK-backed technology with its own provenance, instead of folding its
    identity into the interposer.

    Two conditions yield no block and are not errors: no interconnect PDK is
    reachable from here (``_TECH_ABSENT``), and the manifest was read fine but
    declares no method for this adapter (``_TECH_NOT_DECLARED``, the ordinary
    hand-set custom adapter). Everything else raises :class:`SourceRefused`.

    The bug this replaces was not the handler, it was the RETURN VALUE. The
    "no method declares this adapter" case is the ``for``/``else`` below, an
    exhausted loop that raises nothing, so no exception handler ever separated
    it from a broken manifest. A bare ``except Exception: return None`` simply
    turned every read failure into the same None the legitimate path returns,
    and the caller, seeing one None, could not tell a refused schema_version
    from a custom adapter. Narrowing the except alone would not have fixed
    that; the function has to say which of the two happened.

    There is deliberately NO ``except KeyError`` arm, unlike the sibling
    helpers above. They look a method up by id, so KeyError is their "unknown
    method" signal. Here the ids come from ``list_methods``, so a KeyError is
    ``KeyError('methods')`` from a manifest with no methods block: a structural
    break wearing the same type as an unknown id. Catching it would reopen this
    defect in a narrower form. Do not add it back.

    Refusing here is safe and does not widen much. ``update_chiplet_file``
    mutates an in-memory dict and opens the output exactly once, far below its
    call to this code, so nothing partial can reach disk. And this file already
    refuses on this manifest through ``_connection_to_adapter`` and
    ``_connection_to_body_diameter``, which catch only
    ``(KeyError, FileNotFoundError)``. The choice was never refuse-versus-warn;
    it was whether a field in the input document got to decide which one you
    got, since control only reached here when the ``.chiplet`` already declared
    an adapter.
    """
    im = _import_interconnect_manifest()
    if im is None:
        return None, _TECH_ABSENT
    vendor = None
    try:
        for mid in im.list_methods():
            method = im.get_method(mid)
            if method.get("adapter") == adapter:
                vendor = method.get("vendor")
                break
        else:
            return None, _TECH_NOT_DECLARED
    except FileNotFoundError:
        # Nothing to read: a partial or absent PDK install. Same condition as
        # im is None, and the only tolerated failure here.
        return None, _TECH_ABSENT
    except Exception as exc:
        raise SourceRefused(
            "the interconnect PDK manifest is present but unusable, so "
            "interconnect.technology for adapter '%s' cannot be derived "
            "(%s: %s)" % (adapter, type(exc).__name__, exc),
            exit_code=_source_refusal_exit_code(exc),
        ) from exc
    description = "Chiplet attachment"
    if vendor:
        description += f" ({vendor})"
    return {
        "description": description,
        "layer_properties": _INTERCONNECT_LYP_REF,
        "dbu": 0.001,
    }, None


def _source_refusal_exit_code(exc):
    """Shared-table exit code for a manifest failure: 6 for version, else 5.

    Reads the reader off ``sys.modules`` rather than calling
    ``_import_interconnect_manifest``, which prints its own diagnostic and can
    now itself refuse. A failure-reporting path must not be able to fail.
    """
    if _is_manifest_version_error(exc):
        return EXIT_SOURCE_VERSION
    return EXIT_SOURCE_UNREACHABLE


def _maybe_set_interconnect_adapter(data):
    """Declare ``interconnect`` (adapter + technology) on a .chiplet dict.

    Scans die components for a connection whose manifest method carries an
    adapter and declares it at the assembly root, so chiplet-studio merges the
    method's 3D body fragment and the ADK applies the method's IXN pitch/spacing
    DRC. Works whether the die connection was chosen via --connection-type or
    carried in from the board. An adapter already declared on the .chiplet is
    never overwritten (an explicit choice wins); the ``technology`` subblock is
    derived data and is refreshed for whatever adapter is effective, so files
    from older exports gain it on re-export. With mixed per-die methods the
    first die's adapter wins -- harmless, since the adapter is only the legacy
    fallback: per-method fragments and the per-method DRC sidecar carry the
    real per-die data. Returns the adapter that was newly set, otherwise None.
    """
    existing = data.get("interconnect")
    adapter = existing.get("adapter") if isinstance(existing, dict) else None
    newly_set = None
    if not adapter:
        for comp in data.get("components", []):
            if comp.get("type") != "die":
                continue
            adapter = _connection_to_adapter(comp.get("connection", ""))
            if adapter:
                newly_set = adapter
                break
    if adapter:
        if not isinstance(data.get("interconnect"), dict):
            data["interconnect"] = {}
        data["interconnect"]["adapter"] = adapter
        tech, reason = _interconnect_technology_block(adapter)
        if tech:
            data["interconnect"]["technology"] = tech
        elif reason == _TECH_NOT_DECLARED:
            # Not a defect and not a warning: the manifest was read and simply
            # carries no method for this adapter, which is what a hand-set
            # custom adapter looks like. Said out loud anyway, because the
            # emitted document is poorer than one with the block.
            print(
                "  Note: the interconnect PDK manifest declares no method for "
                "adapter '%s', so the .chiplet keeps the adapter and gains no "
                "interconnect.technology block. Expected for a custom adapter."
                % adapter
            )
        else:
            # Derived data we could not derive because the source is not here
            # at all. The merge layer treats interconnect: as exporter-owned,
            # so no older copy is carried forward and the document goes out
            # poorer. Degrading rather than refusing is deliberate: an absent
            # PDK is an environment condition and the plugin has to run
            # without it. The third cause this warning used to also cover, a
            # manifest present but unusable, no longer reaches here; it
            # refuses, which is what PLUG-1 was about.
            print(
                "  Warning: no interconnect PDK is reachable from here, so no "
                "interconnect.technology block is written for adapter '%s'. "
                "Install the interconnect PDK or set INTERCONNECT_PDK_ROOT "
                "and re-export to get one." % adapter
            )
    return newly_set


def _find_interposer_pdk_python():
    return _or_exit(_layer_map.find_interposer_pdk_python, start=__file__)


@_intm4tm2_scope()
def _import_bump_mirror():
    """Import bump_mirror (Cu-pillar geometry + DRC + auto-resolve).

    Resolved via _find_interposer_pdk_python(). Returns the module, or
    None when the interposer PDK is not reachable; callers that REQUIRE
    pillars must treat None as a hard error, never as a soft skip.
    Imported inside _intm4tm2_scope, so its directory, which holds the
    interposer's own pypreprocessor, does not stay on sys.path after it.
    """
    try:
        python_dir = _find_interposer_pdk_python()
        if python_dir is None:
            print("Warning: interposer PDK not found (set "
                  "INTERPOSER_PDK_ROOT or keep the sibling checkout).",
                  file=sys.stderr)
            return None
        if str(python_dir) not in sys.path:
            sys.path.insert(0, str(python_dir))
        import bump_mirror
        return bump_mirror
    except Exception as exc:
        print("Warning: could not import bump_mirror: %s" % exc,
              file=sys.stderr)
        return None


@_intm4tm2_scope()
def _add_device_pillars(bm, pillar_gen, ref: str, bumps,
                        body_diameter_um: float, *, pcell: str) -> int:
    """Draw one device's landings with bump_mirror, whose pad geometry is
    the IntM4TM2 PCell ``pcell`` the generator was built for. Returns the
    number of pads placed."""
    ensure_lib = getattr(bm, '_ensure_pcell_lib', None)
    if ensure_lib is not None:
        ensure_lib()
    _check_intm4tm2_cni(pcell)
    return pillar_gen.add_device_bumps(ref, bumps, body_diameter_um)


def _method_landing(bm, im, method, cell):
    """(generator, DrcParams, body diameter) that draw ``method``'s landings
    with pad cell ``cell``; ValueError when the manifest profile cannot be
    drawn. ``im`` is the interconnect manifest module, and the method's
    body diameter is known to resolve."""
    diameter = _connection_to_body_diameter(method)
    table = bm.CUPILLAR_TABLE_6_1 if cell.table_6_1 else {}
    # A method's declared pitch_rules are authoritative over the
    # body-diameter table lookup. from_body_diameter maps an IHP Table 6.1
    # body diameter (35/44/49/54) to that row's pitch/spacing and returns the
    # Option-2 defaults (pitch 80 / spacing 40) for any other diameter -- so
    # a vendor fine-pitch method (vendorx, body 40 um) was checked against
    # 80/40 instead of its real manifest spec. That let auto-resolve spread
    # vendorx's native 70 um bumps toward 80 (landing ~72 nm short -> a
    # phantom cu-pillar DRC error) while the assembly DRC, which reads the
    # manifest, passed the same geometry at the real 50 um pitch. Honor the
    # manifest's pitch_rules when the method declares them; keep the
    # body-diameter fab geometry (diameter, enclosure). In-table methods
    # declare the same numbers, so this is a no-op for them.
    #
    # For a Cu pillar outside the table take the Option-2 fallback fields
    # directly rather than via from_body_diameter(), so its "using Option 2
    # defaults" stderr warning does not fire -- that message is misleading
    # once the manifest pitch_rules below supersede those defaults. An
    # accurate note is emitted afterwards. A pad cell outside Table 6.1
    # (SolderBumpPad) starts from its own rule set (Padb) instead.
    in_table = table.get(diameter) is not None
    if in_table:
        params = bm.DrcParams.from_body_diameter(diameter)
    elif cell.table_6_1:
        params = bm.DrcParams()
    else:
        params = bm.DrcParams.load(fab_anchor_id=cell.anchor)
    try:
        opening, enclosure = resolve_carrier_profile(
            method, im.fab_params(method), diameter, table)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc
    try:
        pitch_rules = im.pitch_rules(method)
    except KeyError:
        pitch_rules = {}
    overrode = False
    if pitch_rules.get("IXN_pitch") is not None:
        params.min_pitch_um = pitch_rules["IXN_pitch"]
        overrode = True
    if pitch_rules.get("IXN_spacing") is not None:
        params.min_spacing_um = pitch_rules["IXN_spacing"]
        overrode = True
    # The pad size the pre-DRC reasons about has to be the pad size that gets
    # drawn, and the drawn one comes from fab_params (the generator below is
    # handed the same number). Without this, an out-of-table method kept the
    # Option-2 fallback of 40 um while drawing a 35 um opening, and
    # auto_resolve, which targets max(pitch, diameter + spacing), pushed for
    # 55 um where the method asks for 50. In-table methods declare the
    # table's own opening, so this is a no-op for them.
    params.diameter_um = opening
    params.min_enclosure_um = enclosure
    if cell.table_6_1 and not in_table:
        if overrode:
            print(f"  Note: {method} body diameter {diameter} um is outside "
                  f"IHP Table 6.1; using its manifest pitch/spacing "
                  f"({params.min_pitch_um}/{params.min_spacing_um} um).")
        else:
            print(f"  Warning: {method} body diameter {diameter} um is "
                  f"outside IHP Table 6.1 and declares no pitch_rules; using "
                  f"Option 2 defaults ({params.min_pitch_um}/"
                  f"{params.min_spacing_um} um).", file=sys.stderr)
    # The 3D body layers (e.g. a vendor's 510/511 vs IHP's 500/501) and the
    # fab pad geometry travel with the method, not with the assembly.
    bodies = im.layers_3d(method)
    generator = bm.CuPillarGenerator(
        enclosure_um=params.min_enclosure_um, bodies=bodies,
        passiv_opening_um=opening, **cell.generator_kwargs)
    print(f"\nGenerating {cell.pcell} landings (connection={method}, "
          f"body diameter={diameter} um, pitch {params.min_pitch_um} um / "
          f"spacing {params.min_spacing_um} um) with DRC validation...")
    print("  3D bodies: " + ", ".join(
        f"{name} ({lnum}/{ldt})" for name, lnum, ldt in bodies))
    return generator, params, diameter


FILL_ENGINE_SUBPATH = ("libs.tech", "klayout", "python", "fill_closure.py")


def _fill_engine_path() -> Optional[Path]:
    """The fill_closure.py this export must use, resolved, or None.

    $INTERPOSER_PDK_ROOT, when set, is the only place looked at: a root that
    lacks the engine is an error, not a cue to walk on to a sibling checkout
    that may be a different PDK. Unset, the sibling walk of the other
    interposer lookups applies.
    """
    selected = os.environ.get("INTERPOSER_PDK_ROOT")
    if selected:
        path = Path(selected).joinpath(*FILL_ENGINE_SUBPATH)
        if not path.is_file():
            print("Warning: INTERPOSER_PDK_ROOT=%s has no fill engine (%s); "
                  "point it at a checkout that includes the metal-fill work."
                  % (selected, path), file=sys.stderr)
            return None
        return path.resolve()
    root = _discover_path_var("INTERPOSER_PDK_ROOT")
    if root is None:
        print("Warning: interposer PDK not found (set INTERPOSER_PDK_ROOT "
              "or keep the sibling checkout).", file=sys.stderr)
        return None
    path = Path(root).joinpath(*FILL_ENGINE_SUBPATH)
    if not path.is_file():
        print("Warning: the interposer PDK checkout at %s has no fill engine "
              "(%s); use a checkout that includes the metal-fill work."
              % (root, path), file=sys.stderr)
        return None
    return path.resolve()


def _import_fill_closure():
    """Import the interposer PDK's metal-fill engine (fill_closure).

    Resolved by _fill_engine_path(). Returns the module, or None when the
    engine is not there, does not import, or imports from anywhere but the
    resolved path (an earlier sys.path entry or an already imported module of
    the same name). A caller that was asked to fill MUST treat None as a hard
    error, never a soft skip.
    """
    path = _fill_engine_path()
    if path is None:
        return None
    python_dir = str(path.parent)
    try:
        if python_dir not in sys.path:
            sys.path.insert(0, python_dir)
        import fill_closure as mod
    except Exception as exc:
        print("Warning: could not import the fill engine %s: %s"
              % (path, exc), file=sys.stderr)
        return None
    imported = getattr(mod, "__file__", None)
    if not imported or Path(imported).resolve() != path:
        print("Warning: fill_closure imported from %s, not from the resolved "
              "%s; refusing an engine from another checkout."
              % (imported, path), file=sys.stderr)
        return None
    return mod


def _filler_layers(layer_map) -> List[Tuple[int, int]]:
    """(layer, datatype) of the four BEOL filler purposes, from the LYP.

    Prefers the explicit ``<metal>.filler`` purpose, derives <metal>/22 from the
    drawn-metal number when the LYP does not declare it. Used only to measure
    coverage for the read-back map; the fill itself is the PDK engine's job.
    """
    out = []
    for metal in ("Metal4", "Metal5", "TopMetal1", "TopMetal2"):
        try:
            out.append(layer_map.get_layer(metal, "filler"))
            continue
        except KeyError:
            pass
        try:
            num, _dt = layer_map.get_layer(metal)
            out.append((num, 22))
        except KeyError:
            pass
    return out


def _compute_fill_coverage(gds_path, filler_layers, cell_um=200.0):
    """Coarse per-cell fill coverage over the prBoundary bbox, for the KiCad
    read-back layer.

    Returns {"cell_um": .., "grid": [{x_um,y_um,w_um,h_um,coverage}]} where
    coverage is the fraction of the cell covered by any filler shape (union
    across metals). Coarse by design -- a canvas glance, not the tiles.
    Coordinates stay in the GDS frame (Y up); the KiCad painter negates Y.
    """
    ly = db.Layout()
    ly.read(str(gds_path))
    top = ly.top_cell()
    dbu = ly.dbu

    def reg(layer, dt):
        li = ly.find_layer(layer, dt)
        if li is None:
            return db.Region()
        return db.Region(top.begin_shapes_rec(li))

    # Each cell reads only the fill shapes that touch it: one merge over the
    # whole closure fill costs more than the per-cell ones together, and the
    # AND below merges its input anyway, so the areas are the same.
    fill_lis = [li for li in (ly.find_layer(lnum, dt)
                              for (lnum, dt) in filler_layers)
                if li is not None]

    def fill_area(cell):
        fill = db.Region()
        for li in fill_lis:
            fill.insert(top.begin_shapes_rec_touching(li, cell))
        return (fill & db.Region(cell)).area()

    prb = reg(*GDSGenerator.PRBOUNDARY_LAYER)
    bb = prb.bbox() if not prb.is_empty() else top.bbox()
    grid = []
    if bb.empty() or not fill_lis:
        return {"cell_um": cell_um, "grid": grid}

    step = max(1, int(round(cell_um / dbu)))
    xi = bb.left
    while xi < bb.right:
        cx1 = min(xi + step, bb.right)
        yi = bb.bottom
        while yi < bb.top:
            cy1 = min(yi + step, bb.top)
            cell = db.Box(xi, yi, cx1, cy1)
            area = cell.area()
            if area > 0:
                cov = fill_area(cell)
                if cov > 0:
                    grid.append({
                        "x_um": round(xi * dbu, 4),
                        "y_um": round(yi * dbu, 4),
                        "w_um": round((cx1 - xi) * dbu, 4),
                        "h_um": round((cy1 - yi) * dbu, 4),
                        "coverage": round(cov / float(area), 4),
                    })
            yi += step
        xi += step
    return {"cell_um": cell_um, "grid": grid}


def _insert_metal_fill(gds_path, topcell, mode, filler_layers, log=print):
    """Stamp PDK metal density fill onto `gds_path` in place.

    Delegates the actual fill to the interposer PDK's engine
    (fill_closure.fill_stack) -- the single source of truth for the density
    rules -- and only locates it, calls it, and writes the read-back sidecars
    (``<stem>.fill_density.json`` and ``<stem>.fill_coverage.json``). Returns
    {"status": "ok"|"skipped"|"error", "report": .., "coverage": ..}.

    The ``klayout`` binary is required (the engine shells out to it, exactly
    like the assembly-DRC step); when it is absent we SOFT-SKIP with a clear
    message rather than failing the whole export.
    """
    if shutil.which("klayout") is None:
        log("Metal fill skipped: 'klayout' binary not on PATH (the fill engine "
            "needs it, same as assembly DRC). Run the PDK filler in KLayout on "
            "the exported GDS, or install the KLayout application.")
        return {"status": "skipped", "report": None, "coverage": None}

    fc = _import_fill_closure()
    if fc is None or not hasattr(fc, "fill_stack"):
        print("ERROR: --insert-metal-fill was requested but the interposer PDK "
              "fill engine (fill_closure.fill_stack) is unavailable; see the "
              "warning above.", file=sys.stderr)
        return {"status": "error", "report": None, "coverage": None}
    engine_file = getattr(fc, "__file__", None)
    if engine_file:
        log("Fill engine: %s" % Path(engine_file).resolve())

    with tempfile.TemporaryDirectory() as td:
        try:
            report = fc.fill_stack(gds_path, gds_path, topcell=topcell,
                                   mode=mode, workdir=td, log=log)
        except RuntimeError as exc:
            # The engine stops instead of filling when a fill macro does: with
            # a seal ring drawn, a ring with no interior, drawn metal outside
            # it, or a prBoundary that misses its interior. The first line is
            # the reason; the rest is KLayout's output. The GDS is written
            # only after a complete fill, so it is still the unfilled one.
            reason, _sep, detail = str(exc).partition("\n")
            print("ERROR: metal fill stopped: %s" % reason, file=sys.stderr)
            for line in detail.splitlines():
                print("  " + line, file=sys.stderr)
            return {"status": "error", "report": None, "coverage": None}

    stem = str(Path(gds_path).with_suffix(""))
    try:
        with open(stem + ".fill_density.json", "w") as f:
            json.dump(report if report is not None else {}, f, indent=2)
        log("Fill density report: %s.fill_density.json" % stem)
    except OSError as exc:
        print("Warning: could not write fill density report: %s" % exc,
              file=sys.stderr)

    coverage = None
    try:
        coverage = _compute_fill_coverage(gds_path, filler_layers)
        with open(stem + ".fill_coverage.json", "w") as f:
            json.dump(coverage, f, indent=2)
        log("Fill coverage map: %s.fill_coverage.json (%d cell(s))"
            % (stem, len(coverage.get("grid", []))))
    except Exception as exc:
        print("Warning: could not compute fill coverage map: %s" % exc,
              file=sys.stderr)

    return {"status": "ok", "report": report, "coverage": coverage}


def _parse_length_um(value) -> Optional[float]:
    """A sidecar w/l value -> micrometers, or None if unparseable.

    Numbers are already micrometers (schema v2, written by
    writers/chiplet_writer.py). Strings are accepted for hand-written and v1
    sidecars and follow the two board conventions: a 'um'/'u' suffix means
    micrometers, a bare number means meters (how cap_cmim.kicad_sym stores
    w/l). Keep in step with chiplet_writer.parse_length_um.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    token = str(value).strip().lower()
    if not token:
        return None
    if token.endswith("um"):
        token, scale = token[:-2], 1.0
    elif token.endswith("u"):
        token, scale = token[:-1], 1.0
    else:
        scale = 1e6  # bare number: meters -> micrometers
    try:
        return float(token) * scale
    except ValueError:
        return None


def _cmim_length_um(item: Dict, name: str) -> float:
    """Read `<name>_um` (schema v2), falling back to the v1 `<name>` key.

    Under the v1 key the board's own convention applies to numbers as well as
    to strings: a bare value is metres. Reading it as micrometres there would
    make a hand-written sidecar differ by 1e6 on nothing but JSON quoting.
    """
    for key in (name + "_um", name):
        if key not in item:
            continue
        raw = item[key]
        if (key == name and not isinstance(raw, bool)
                and isinstance(raw, (int, float))):
            value = float(raw) * 1e6
        else:
            value = _parse_length_um(raw)
        if value is None:
            raise ValueError('unparseable %s=%r' % (key, raw))
        return value
    raise KeyError(name + "_um")


def load_cmim_devices(cmim_devices_json: str) -> Optional[List[Dict]]:
    """Read the cap_cmim sidecar.

    Returns the device list, [] when the file is well formed but declares no
    devices, and None when it could not be read at all. The caller must treat
    None as fatal: a sidecar was requested, so "the file is garbage" is not
    the same answer as "this board has no capacitors".
    """
    path = Path(cmim_devices_json)
    if not path.exists():
        print(f"Warning: CMIM devices file not found: {cmim_devices_json}",
              file=sys.stderr)
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"Warning: could not read CMIM devices file "
              f"{cmim_devices_json}: {exc}", file=sys.stderr)
        return None

    if not isinstance(data, dict):
        print(f"Warning: CMIM devices file {cmim_devices_json} is not a JSON "
              f"object", file=sys.stderr)
        return None

    devices = data.get("cmim_devices", [])
    if not devices:
        print(f"  No cmim_devices found in {cmim_devices_json}")
        return []
    return devices


def load_nofill_regions(nofill_regions_json: str) -> Optional[List[Dict]]:
    """Read the no-fill regions sidecar (keep-outs authored in KiCad).

    Returns the region list, [] when the file is well formed but declares no
    regions, and None when it could not be read at all. The caller must treat
    None as fatal: a keep-out the designer drew must never be silently dropped
    (fill over probe pads or the seal ring is worse than a hard stop).
    """
    path = Path(nofill_regions_json)
    if not path.exists():
        print(f"Warning: no-fill regions file not found: {nofill_regions_json}",
              file=sys.stderr)
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"Warning: could not read no-fill regions file "
              f"{nofill_regions_json}: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"Warning: no-fill regions file {nofill_regions_json} is not a "
              f"JSON object", file=sys.stderr)
        return None
    regions = data.get("regions", [])
    if not regions:
        print(f"  No regions found in {nofill_regions_json}")
        return []
    return regions


def _report_refusal(what: str, exc: Exception) -> None:
    """First line of the refusal as the ERROR, any further lines under it."""
    reason, _sep, detail = str(exc).partition("\n")
    print("\nERROR: %s refused: %s" % (what, reason), file=sys.stderr)
    for line in detail.splitlines():
        print("  " + line, file=sys.stderr)


def refuse_touching_nets(copper: Dict[Tuple[str, str], List[db.Polygon]],
                         dbu: float) -> None:
    """Refuse two nets' traces that touch on one layer.

    ``copper`` is the trace copper per (layer, net), in database units of
    ``dbu`` um. Touching copper merges into one polygon, a short no space rule
    reports. On a board that passes its own clearance check the touch comes
    from a square end: KiCad checks clearance to its round cap, and the square
    reaches up to (sqrt(2) - 1) * w/2 further at its corners. Pads, vias and
    die copper are not part of this check; an LVS on the GDS sees those.

    Cost: one region test per pair of nets on a layer, so the work grows as
    nets squared per layer; nothing at tens of nets.
    """
    by_layer: Dict[str, Dict[str, db.Region]] = {}
    for (layer, net), polys in copper.items():
        if polys:
            by_layer.setdefault(layer, {})[net] = db.Region(polys).merged()
    found = []
    for layer, nets in sorted(by_layer.items()):
        names = sorted(nets)
        for k, a in enumerate(names):
            grown = nets[a].sized(1)
            for b in names[k + 1:]:
                touch = grown & nets[b]
                if touch.is_empty():
                    continue
                # Where they overlap, the overlap; where they only share an
                # edge or a corner, the 1 dbu sliver across it.
                overlap = nets[a] & nets[b]
                box = (touch if overlap.is_empty() else overlap).bbox()
                found.append("%s: %s meets %s at (%.3f, %.3f)-(%.3f, %.3f) um"
                             % (layer, a, b, box.left * dbu, box.bottom * dbu,
                                box.right * dbu, box.top * dbu))
    if found:
        raise TrackEndRefused(
            "%d place(s) where two nets' traces touch\n%s\n"
            "widen the clearance there; a square track end reaches up to "
            "(sqrt(2) - 1) * w/2 past the board's round cap"
            % (len(found), "\n".join(found)))


def convert_hyp_to_gds(
    hyp_path: str,
    output_path: str,
    lyp_path: str,
    cell_name: str = "INTERPOSER",
    with_chiplets: bool = False,
    complete_output_path: Optional[str] = None,
    chiplet_file_path: Optional[str] = None,
    tech_json_path: Optional[str] = None,
    pad_locations: Optional[Dict[str, str]] = None,
    connection_type: str = "",
    cupillar_gds_path: Optional[str] = None,
    io_pads_json: Optional[str] = None,
    cmim_devices_json: Optional[str] = None,
    annotate_boundaries: bool = False,
    boundary_viz_layer: Tuple[int, int] = (1000, 0),
    die_connections: Optional[Dict[str, str]] = None,
    die_thicknesses: Optional[Dict[str, float]] = None,
    grid_nm: int = MANUFACTURING_GRID_NM,
    nofill_regions_json: Optional[str] = None,
    insert_metal_fill: bool = False,
    fill_mode: str = "single-pass",
    die_outline_layers: Optional[Dict[str, Tuple[int, int]]] = None,
    die_outline_exempt: Optional[List[str]] = None,
    seal_ring: bool = False,
    system_chiplet_path: Optional[str] = None,
    die_layouts_json: Optional[str] = None,
) -> bool:
    """
    Main conversion function.

    Args:
        hyp_path: Path to input HYP file
        output_path: Path for output GDS file (interposer only)
        lyp_path: Path to KLayout LYP layer properties file
        cell_name: Name for the top-level GDS cell
        with_chiplets: If True, also generate complete GDS with chiplets
        complete_output_path: Path for complete GDS with chiplets
        chiplet_file_path: Path to .chiplet file to update with interposer GDS path
        tech_json_path: Path to interposer_tech_default.json for PDK via parameters
        pad_locations: Dict mapping device ref to pin_list JSON path; the
                       landing generator places DRC-validated pads of each
                       die's method at these pads (an alternative to a pre-generated
                       cupillar_gds_path)
        connection_type: Connection stack ID for chiplet file update (e.g. "cupillar_opt1")
        cupillar_gds_path: Path to pre-generated cu-pillar GDS (from bump_mirror.py)
        annotate_boundaries: If True, also paint each chiplet boundary onto a
                             viewer-only annotation layer (no DRC rule reads it)
        boundary_viz_layer: (layer, datatype) for the annotation (default 1000/0)
        die_connections: Per-die connection overrides {ref: method id}. A die
                         not listed uses connection_type. Drives both the 3D
                         bodies drawn under that die (its method's layers and
                         diameter) and its connection field in the .chiplet.
        die_thicknesses: Per-die physical thickness {ref: um}. Written to
                         each die's dimensions.thickness in the .chiplet;
                         dies not listed keep the format default of 0.0.
        die_outline_layers: {ref: (layer, datatype)} of each die's outline in
                         its own GDS. Sizes the die in the .chiplet and is
                         its boundary in the complete GDS's boundary manifest.
        die_outline_exempt: Refs that keep their .chiplet width/height and
                         the placed cell bbox as boundary. With
                         chiplet_file_path, every die of the .chiplet must be
                         in exactly one of the two (resolve_die_outlines);
                         a refusal is decided before anything is written.
        seal_ring: Draw the PDK seal ring on the board outline (one axis-
                         aligned rectangle on prBoundary). Refused, with
                         nothing written, when the outline or the PCell
                         cannot give a ring, or when the drawn geometry
                         crowds it (SealRingRefused).
        system_chiplet_path: The System .chiplet the assembly was built from
                         (--system-chiplet). With chiplet_file_path, the
                         finalizer takes the interposer's and each die's
                         technology, and the technologies block, from it by
                         component id. Refused, with nothing written, when it
                         cannot supply them (TechnologyRefused).
        die_layouts_json: The --die-layouts sidecar (die_layouts.py). It
                         needs chiplet_file_path, and is refused without
                         one. Each die it names gets its full layout as
                         its .chiplet layout and top_cell, while
                         the complete GDS keeps the abstract its GDS_FILE
                         names. Refused, with nothing written, when the
                         sidecar is malformed, names no die, or a file does
                         not agree with its contract (DieLayoutRefused).

    Returns:
        True if conversion was successful
    """
    print(f"Converting: {hyp_path} -> {output_path}")

    # Gate: do the text grammar and the YAML parser agree about this document's
    # top-level keys? The GUI tier decided block ownership from the raw text
    # with no parser available; this is the one place both exist, so it is the
    # one place that proposition can be checked. Here at the head, before any
    # durable artifact: a refusal after the interposer GDS is on disk is not a
    # refusal, and update_chiplet_file runs 500 lines later, after the write.
    if chiplet_file_path:
        parity = chiplet_top_level_key_parity_error(chiplet_file_path)
        if parity:
            print("\nERROR: " + parity, file=sys.stderr)
            return False

    # Load layer mapping
    layer_map = LayerMap(lyp_path)
    print(f"Loaded {layer_map}")

    # Parse HYP file
    parser = HYPParser(hyp_path)
    parser.parse()

    print(f"Parsed {len(parser.segments)} trace segments")
    print(f"Parsed {len(parser.arcs)} trace arcs")
    print(f"Parsed {len(parser.vias)} vias")
    print(f"Parsed {len(parser.padstacks)} padstack definitions")
    print(f"Parsed {len(parser.devices)} devices with GDS_FILE")
    print(f"Parsed {len(parser.pins)} pins")
    print(f"Units: {parser.units}")
    if parser.stackup_layers:
        print(f"Stackup (top→bottom): {' → '.join(parser.stackup_layers)}")

    # Print device info
    if parser.devices:
        print("Devices with GDS_FILE:")
        for dev in parser.devices:
            print(f"  {dev.ref}: {dev.gds_file}")
            print(f"    Position: ({dev.x:.6f}, {dev.y:.6f}) {parser.units}, Rotation: {dev.rotation}°")

    # Print padstack info
    if parser.padstacks:
        print("Padstacks:")
        for idx, ps in sorted(parser.padstacks.items()):
            print(f"  P={idx}: {' -> '.join(ps.layers)}")

    # Die outlines, read from each die's own GDS. Here, right after the parse
    # and before the interposer GDS, its sidecars or the .chiplet are
    # written, for the reason the parity gate gives: the .chiplet update runs
    # after the write, and a refusal there would leave a finished layout/
    # behind for a run that failed. One reader for the run; the .chiplet
    # update and add_device reuse what it read.
    outline_reader = _die_outline.DieGdsReader()
    die_plan = _die_outline.DieOutlinePlan()
    head_io_pad_refs = _io_pad_refs(io_pads_json) if chiplet_file_path else []
    if chiplet_file_path or with_chiplets:
        head_chiplet = None
        if chiplet_file_path:
            try:
                head_chiplet = _vendored_cfio().load(
                    chiplet_file_path, allow_intermediate=True)
            except Exception as exc:
                print(f"\nERROR: cannot read {chiplet_file_path} to size its "
                      f"dies: {exc}", file=sys.stderr)
                return False
        try:
            die_plan = resolve_die_outlines(
                head_chiplet,
                str(Path(chiplet_file_path).resolve().parent)
                if chiplet_file_path else "",
                parser.devices, die_outline_layers, die_outline_exempt,
                outline_reader, head_io_pad_refs)
        except DieOutlineRefused as exc:
            print("", file=sys.stderr)
            for line in exc.lines():
                print(line, file=sys.stderr)
            print("  Refusing to export: no layout, sidecar or .chiplet was "
                  "written.", file=sys.stderr)
            return False
        if die_plan.outlines or die_plan.exempt:
            print("Die outlines:")
        for ref, outline in die_plan.outlines.items():
            print(f"  {ref}: {outline.width_um} x {outline.height_um} um from "
                  f"{_die_outline.format_layer(outline.layer)} under "
                  f"{outline.gds.top_cell} in {outline.gds.path}")
        for ref in die_plan.exempt:
            print(f"  {ref}: exempt (--die-outline-exempt): keeps its .chiplet "
                  f"width/height and the cell bbox as boundary")
    elif die_outline_layers or die_outline_exempt:
        print("Warning: --die-outline-layers and --die-outline-exempt are "
              "ignored without --update-chiplet-file or --with-chiplets.",
              file=sys.stderr)

    # Full die layouts, checked against their contracts here for the die
    # outlines' reason, and against the dies the plan above decided. Without
    # a .chiplet the frame check would not run, so the run is refused rather
    # than carried on with the flag ignored.
    head_die_layouts = None
    if die_layouts_json and not chiplet_file_path:
        print("Error: --die-layouts needs --update-chiplet-file: the full "
              "layouts go into the .chiplet, and without one their frame "
              "check would not run; nothing was written.", file=sys.stderr)
        return False
    elif die_layouts_json:
        try:
            head_die_layouts = resolve_die_layouts(
                head_chiplet, die_plan, _die_layouts.load(die_layouts_json),
                outline_reader)
        except DieLayoutRefused as exc:
            print("", file=sys.stderr)
            for line in exc.lines():
                print(line, file=sys.stderr)
            print("  Refusing to export: no layout, sidecar or .chiplet was "
                  "written.", file=sys.stderr)
            return False
        print(f"Die layouts from {die_layouts_json}:")
        for ref, entry in head_die_layouts.items():
            print(f"  {ref}: {entry.layout} (top cell {entry.top_cell}); the "
                  f"assembly places {die_plan.gds(ref).path}")

    # The mount plane, read here for the die outlines' reason: a .chiplet
    # cannot be finalized without it, and nothing is written before.
    attach_frame = None
    if chiplet_file_path:
        try:
            attach_frame = read_attach_frame()
        except AttachFrameRefused as exc:
            print(f"\nAttach frame refused: {exc}", file=sys.stderr)
            print("  Refusing to export: no layout, sidecar or .chiplet was "
                  "written.", file=sys.stderr)
            return False
        print(f"Mount plane: {ATTACH_FRAME_KEY} = {attach_frame.at()} um "
              f"({attach_frame.source})")

    # Technology ids from the System .chiplet, decided here for the die
    # outlines' reason: the finalizer runs after the layout is written.
    system_chiplet = None
    if system_chiplet_path and not chiplet_file_path:
        print("Warning: --system-chiplet is ignored without "
              "--update-chiplet-file.", file=sys.stderr)
    elif system_chiplet_path:
        try:
            system_chiplet = _vendored_cfio().load(system_chiplet_path)
        except Exception as exc:
            print(f"\nERROR: cannot read the System .chiplet "
                  f"{system_chiplet_path}: {exc}", file=sys.stderr)
            print("  Refusing to export: no layout, sidecar or .chiplet was "
                  "written.", file=sys.stderr)
            return False
        try:
            bindings, _unused = _technology_overlay.plan(
                head_chiplet, system_chiplet, head_io_pad_refs)
        except TechnologyRefused as exc:
            print("", file=sys.stderr)
            for line in exc.lines():
                print(line, file=sys.stderr)
            print("  Refusing to export: no layout, sidecar or .chiplet was "
                  "written.", file=sys.stderr)
            return False
        print(f"Technologies from {system_chiplet_path}:")
        for ident, tech in bindings.items():
            print(f"  {ident}: {tech}")

    # Read the cap_cmim sidecar up front: whether the board has real devices to
    # place is what decides the no-geometry guard below, not the mere presence
    # of the flag (an unreadable or empty sidecar must not turn a failed export
    # into a successful one).
    cmim_devices: List[Dict] = []
    if cmim_devices_json:
        loaded = load_cmim_devices(cmim_devices_json)
        if loaded is None:
            print("\nERROR: --cmim-devices was given but the sidecar could "
                  "not be read (see the warning above). Refusing to write an "
                  "interposer without the capacitors it declares.",
                  file=sys.stderr)
            return False
        cmim_devices = loaded
        print(f"Parsed {len(cmim_devices)} cap_cmim device(s) from "
              f"{cmim_devices_json}")

    if not parser.segments and not parser.vias:
        if cmim_devices:
            print("No trace/via geometry found in HYP file; continuing for the "
                  f"{len(cmim_devices)} cap_cmim device(s).")
        else:
            print("Warning: No geometry found in HYP file")
            return False

    # Count segments by layer
    if parser.segments:
        layer_counts: Dict[str, int] = {}
        for seg in parser.segments:
            layer_counts[seg.layer] = layer_counts.get(seg.layer, 0) + 1

        print("Segments by layer:")
        for layer, count in sorted(layer_counts.items()):
            print(f"  {layer}: {count}")

    # Generate GDS
    generator = GDSGenerator(layer_map, cell_name, parser.units, parser.stackup_layers,
                             tech_json_path, annotate_boundaries=annotate_boundaries,
                             boundary_viz_layer=boundary_viz_layer,
                             grid_nm=grid_nm)

    if generator._pcells_available:
        print("PDK PCells available - using via_stack PCell for vias")
    else:
        print("PDK PCells not available - using simple rectangles for vias")
        print("  (Set PDK_ROOT to an IHP-Open-PDK checkout for SG13G2 "
              "via_stack PCells; the fallback honors "
              "interposer_tech_default.json via parameters)")

    # Process segments and arcs as connected paths (smooth corners)
    num_paths = generator.add_segments_as_paths(parser.segments, parser.arcs)
    try:
        refuse_touching_nets(generator.trace_copper, generator.layout.dbu)
    except TrackEndRefused as exc:
        _report_refusal("track ends", exc)
        return False
    print(f"Created {num_paths} continuous paths from {len(parser.segments)} segments and {len(parser.arcs)} arcs")

    # Loud guard: a board whose copper layers are not named after the PDK
    # metals loses (nearly) all routing to unmapped layers. Refuse to write
    # a GDS that would look fabricable while missing its traces.
    skipped_elems = sum(generator._unmapped_layers.values())
    if skipped_elems:
        total_elems = generator._mapped_trace_elements + skipped_elems
        skip_list = ", ".join(
            "%s (%d)" % (name, count)
            for name, count in sorted(generator._unmapped_layers.items()))
        pdk_names = ", ".join(
            sorted(k for k in layer_map.layers if ":" not in k))
        if skipped_elems / float(total_elems) > UNMAPPED_FAIL_FRACTION:
            print(
                "\nERROR: %d of %d trace element(s) sit on board layers the "
                "PDK does not map; the output GDS would be missing its "
                "routing.\n"
                "  Unmapped board layers: %s\n"
                "  PDK drawing layers:    %s\n"
                "  The interposer flow requires the board copper layers to "
                "be NAMED after the PDK metals\n"
                "  (KiCad: Board Setup > Physical Stackup, e.g. "
                "F.Cu -> TopMetal2, In1.Cu -> TopMetal1, In2.Cu -> Metal5, "
                "B.Cu -> Metal4),\n"
                "  or start from the interposer template: %s"
                % (skipped_elems, total_elems, skip_list, pdk_names,
                   _find_interposer_template()),
                file=sys.stderr)
            return False
        print("Warning: skipped %d of %d trace element(s) on unmapped "
              "layer(s): %s" % (skipped_elems, total_elems, skip_list))

    # Process vias
    via_success = 0
    for via in parser.vias:
        padstack = parser.padstacks.get(via.padstack_index)
        if padstack and generator.add_via(via, padstack):
            via_success += 1

    print(f"Successfully converted {via_success}/{len(parser.vias)} vias")

    # Board outline (KiCad Edge.Cuts -> .hyp BOARD perimeter) on prBoundary.
    # Downstream, update_chiplet_file sizes the interposer from this layer
    # so viewers show the fab outline rather than the drawn-copper extent.
    if parser.perimeter_segments:
        n_loops = generator.add_board_outline(parser.perimeter_segments)
        if n_loops:
            pl, pd = GDSGenerator.PRBOUNDARY_LAYER
            print(f"Board outline: {len(parser.perimeter_segments)} perimeter "
                  f"segment(s) -> {n_loops} closed loop(s) on prBoundary "
                  f"{pl}/{pd}")
    else:
        print("No board perimeter in HYP; prBoundary not drawn (interposer "
              "dimensions fall back to the drawn-geometry bbox)")

    # No-fill keep-outs authored in KiCad (writers/chiplet_writer): paint them
    # onto the GDS keep-out datatypes BEFORE write() so the grid snap normalizes
    # them and the PDK fill generators (which subtract 160/0 and <metal>/23)
    # honor them. Requested-but-unreadable is fatal: a dropped keep-out would
    # let fill land over probe pads or the seal ring.
    if nofill_regions_json:
        nofill_records = load_nofill_regions(nofill_regions_json)
        if nofill_records is None:
            print("\nERROR: --nofill-regions was given but the sidecar could "
                  "not be read (see the warning above). Refusing to fill "
                  "without the designer's keep-outs.", file=sys.stderr)
            return False
        n_nofill = generator.add_nofill_regions(nofill_records)
        if n_nofill:
            print("No-fill keep-outs: painted %d region(s) onto GDS keep-out "
                  "layers" % n_nofill)

    # Add cu-pillar pads: prefer pre-generated GDS, fall back to inline generation
    if cupillar_gds_path:
        cupillar_path = Path(cupillar_gds_path)
        if not cupillar_path.exists():
            print(f"Error: Cu-pillar GDS not found: {cupillar_gds_path}",
                  file=sys.stderr)
            return False
        print(f"\nMerging pre-generated cu-pillar GDS: {cupillar_gds_path}")
        cupillar_layout = db.Layout()
        cupillar_layout.read(str(cupillar_path))
        for src_cell in cupillar_layout.each_cell():
            if src_cell.parent_cells() == 0:  # top-level cell(s)
                new_cell = generator.layout.create_cell(src_cell.name)
                new_cell.copy_tree(src_cell)
                generator.top_cell.insert(
                    db.DCellInstArray(new_cell, db.DTrans()))
                print(f"  Merged cu-pillar cell: {src_cell.name}")
    elif pad_locations and parser.devices:
        # Resolve each device's connection method: per-die override first,
        # then the assembly-global --connection-type. A method whose landing
        # the pad cell cannot draw refuses the export; an id the manifest does
        # not know gets no pillars, loudly.
        requested = resolve_die_methods(pad_locations, die_connections,
                                        connection_type)
        connections_requested = bool(requested)
        bm = _import_bump_mirror() if requested else None
        device_methods = {}
        method_cells = {}
        for dev_ref, method in requested.items():
            try:
                cell = _check_landing_anchor(method, bm)
            except ValueError as exc:
                print(f"ERROR: Cannot draw the landing of {dev_ref}: {exc}",
                      file=sys.stderr)
                return False
            if cell is None or _connection_to_body_diameter(method) is None:
                print(f"  Warning: connection '{method}' on {dev_ref} has "
                      f"no body geometry in the interconnect manifest; "
                      f"skipping its landings", file=sys.stderr)
                continue
            device_methods[dev_ref] = method
            method_cells[method] = cell
        # Every method in use passed _check_landing_anchor, so bm is loaded.
        methods_in_use = sorted(set(device_methods.values()))
        if connections_requested:
            # The bump path was entered (connections requested alongside pad
            # locations): guarantee a pillar manifest even when no method
            # resolved body geometry, so consumers can tell "requested but
            # nothing drawn" (empty pillars array) from "bump path never ran"
            # (no manifest at all).
            generator.record_pillars([])
        if methods_in_use:
            # One generator + parameter set per method: the 3D body layers
            # (e.g. a vendor's 510/511 vs IHP's 500/501) and the fab
            # parameters travel with the method, not with the assembly.
            # _connection_to_body_diameter above already proved the manifest
            # resolves for every method in use.
            im = _import_interconnect_manifest()
            per_method = {}
            for m in methods_in_use:
                try:
                    per_method[m] = _method_landing(bm, im, m, method_cells[m])
                except ValueError as exc:
                    print(f"ERROR: Cannot draw carrier landing for {m}: "
                          f"{exc}", file=sys.stderr)
                    return False
                m_params = per_method[m][1]
                # The numbers this method's bumps are placed and auto-resolved
                # against travel with the drawn pads, in the pillar manifest.
                generator.record_interconnect_rules(
                    m, m_params.min_spacing_um, m_params.min_pitch_um,
                    m_params.diameter_um)
            device_map = {dev.ref: dev for dev in parser.devices}
            total_pillars = 0
            device_reports = {}
            for dev_ref, pin_json in pad_locations.items():
                method = device_methods.get(dev_ref)
                if not method:
                    continue  # warned above (or no connection at all)
                pillar_gen, params, body_diameter = per_method[method]
                dev = device_map.get(dev_ref)
                if not dev:
                    print(f"  Warning: Device {dev_ref} not in HYP, skipping")
                    continue
                try:
                    pin_lists = bm.load_pin_lists(["%s=%s" % (dev_ref, pin_json)])
                except SystemExit:
                    print(f"  Warning: could not load pin list for {dev_ref}")
                    continue
                positions = {dev_ref: {
                    "x": generator._to_um(dev.x),
                    "y": generator._to_um(dev.y),
                    "rotation": dev.rotation,
                }}
                bumps = bm.compute_bump_locations(pin_lists, positions)
                resolved, rep = bm.auto_resolve_collisions(
                    bumps, params, params.diameter_um)
                if rep.get("moved_count"):
                    print(f"  {dev_ref}: auto-resolved {rep['moved_count']} "
                          f"bump(s) (max shift {rep.get('max_delta_um', 0):.2f} "
                          f"um)")
                # max_detail=None -> complete, uncapped report (every
                # violation listed both in the panel and the JSON sidecar).
                report = bm.DrcValidator(params).validate(
                    resolved, params.diameter_um, params.min_enclosure_um,
                    max_detail=None)
                for r in report.results:
                    if r.severity in ("error", "warning"):
                        print(f"  DRC {r.rule} [{r.severity}]: {r.message}")
                s = report.summary
                print(f"  {dev_ref}: {s['error']} error(s), "
                      f"{s['warning']} warning(s) across {s['total']} checks")
                if not report.passed:
                    print(f"  Warning: {dev_ref} has residual landing DRC "
                          f"violations (continuing per policy).")
                dev_report = report.to_dict()
                dev_report["connection"] = method
                device_reports[dev_ref] = dev_report
                total_pillars += _add_device_pillars(
                    bm, pillar_gen, dev_ref, resolved, body_diameter,
                    pcell=method_cells[method].pcell)
                # Record the as-drawn centers for the pillar manifest: the
                # exact positions add_device_bumps just placed (resolved is
                # index-aligned with the pre-resolve bumps list; the 0.01 um
                # movement threshold matches auto_resolve_collisions').
                # Moved bumps also record the shift magnitude so consumers
                # can bound the expected pad-to-pillar deviation instead of
                # accepting any distance on the flag alone.
                pillar_records = []
                for orig, drawn in zip(bumps, resolved):
                    shift = math.hypot(
                        drawn.global_x_um - orig.global_x_um,
                        drawn.global_y_um - orig.global_y_um)
                    record = {
                        "device_ref": dev_ref,
                        "pin_name": drawn.pin_name or "",
                        "method": method,
                        "x_um": round(drawn.global_x_um, 6),
                        "y_um": round(drawn.global_y_um, 6),
                        "diameter_um": body_diameter,
                        "moved_by_auto_resolve": shift > 0.01,
                    }
                    if shift > 0.01:
                        record["auto_resolve_shift_um"] = round(shift, 6)
                    pillar_records.append(record)
                generator.record_pillars(pillar_records)
            # Merge the generated group cells (CUPILLARS_<ref>, SBUMPS_<ref>:
            # the pad cell's prefix) into the interposer top. Each device
            # lives in exactly one method's generator.
            merged = 0
            for m in methods_in_use:
                m_layout = per_method[m][0].layout
                group_prefix = method_cells[m].group_prefix
                # Iterate valid cells only: a generator that instantiates a
                # PCell and prunes the leftover proxy (as the IntM4TM2
                # CuPillarPad generator does) leaves freed slots in the cell
                # index space, so range(cells()) + cell(ci) would raise
                # "Not a valid cell index". each_cell() skips the gaps.
                for src in m_layout.each_cell():
                    if src.name.startswith(group_prefix):
                        new_cell = generator.layout.create_cell(src.name)
                        new_cell.copy_tree(src)
                        generator.top_cell.insert(
                            db.DCellInstArray(new_cell, db.DTrans()))
                        merged += 1
            print(f"Total landing pads placed: {total_pillars} "
                  f"({merged} device group(s))")

            # Persist a complete DRC report next to the interposer GDS so
            # the warn-and-continue violations survive past the GUI panel.
            if device_reports:
                agg = {"error": 0, "warning": 0, "info": 0, "total": 0}
                all_passed = True
                for d in device_reports.values():
                    for k in agg:
                        agg[k] += d["summary"].get(k, 0)
                    all_passed = all_passed and d.get("passed", True)
                out_p = Path(output_path)
                stem = out_p.stem
                if stem.endswith("_interposer"):
                    stem = stem[:-len("_interposer")]
                drc_path = out_p.with_name(stem + "_cupillar_drc.json")
                # version 2: per-method parameter sets ("methods") and a
                # per-device "connection" tag replace the v1 single
                # body_diameter_um/params pair (per-die method selection).
                doc = {
                    "version": 2,
                    "tool": "hyp_to_gds cu-pillar DRC",
                    "connection_type": connection_type,
                    "die_connections": device_methods,
                    "methods": {
                        m: {"body_diameter_um": per_method[m][2],
                            "params": per_method[m][1].to_dict()}
                        for m in methods_in_use
                    },
                    "summary": {**agg, "devices": len(device_reports),
                                "passed": all_passed},
                    "devices": device_reports,
                }
                try:
                    with open(str(drc_path), "w") as f:
                        json.dump(doc, f, indent=2)
                    print(f"Cu-pillar DRC report: {drc_path}")
                except OSError as e:
                    print(f"Warning: could not write DRC report: {e}",
                          file=sys.stderr)

    # Add external I/O pads (wire-bond, etc.) from sidecar JSON
    placed_io_pads: Optional[List[Dict]] = None
    if io_pads_json:
        print(f"\nAdding I/O pads from {io_pads_json}...")
        placed_io_pads = generator.add_io_pads(io_pads_json)
        print(f"Total I/O pads placed: {len(placed_io_pads)}")
    else:
        # Loud guard: standalone components -- PIN refs whose designator is
        # not one of the chiplet devices -- are external I/O pads (wire-bond,
        # etc.). The .hyp carries them as position-only anchors with no
        # geometry; their shapes reach the GDS only through the --io-pads
        # sidecar. Run by hand without it, those pads vanish silently -- the
        # classic "my J pads are missing from the GDS" report. The KiCad
        # plugin export extracts and passes the sidecar automatically; a
        # direct CLI run does not, so say so instead of writing a pad-less GDS.
        # cap_cmim parts also have pins and no chiplet GDS, but their geometry
        # comes from the PCell placed below, not from the io_pads sidecar.
        device_refs = {dev.ref for dev in parser.devices}
        device_refs |= {str(d.get("ref", "")) for d in cmim_devices
                        if isinstance(d, dict)}
        standalone = sorted({pin.ref.split('.', 1)[0] for pin in parser.pins
                             if pin.ref.split('.', 1)[0] not in device_refs})
        if standalone:
            shown = ", ".join(standalone[:12])
            if len(standalone) > 12:
                shown += ", ... (%d total)" % len(standalone)
            print(
                "\nWarning: %d standalone component(s) with pins but no chiplet "
                "GDS were found and --io-pads was not given; their pads will "
                "NOT be drawn in the GDS.\n"
                "  Components: %s\n"
                "  These look like external I/O pads (e.g. wire-bond). In the "
                ".hyp they are position-only PINs -- the pad geometry comes "
                "from the io_pads sidecar, not from the PINs.\n"
                "  To render them, extract the pads from the board and pass "
                "the sidecar (the KiCad plugin export does this for you):\n"
                "    python kicad_pcb_to_iopads.py <board>.kicad_pcb -o io_pads.json\n"
                "    python hyp_to_gds.py ... --io-pads io_pads.json"
                % (len(standalone), shown), file=sys.stderr)

    if cmim_devices:
        print(f"\nAdding CMIM PCells from {cmim_devices_json}...")
        placed_cmims, unplaced_cmims = generator.add_cmim_devices(cmim_devices)
        print(f"Placed {placed_cmims} of {len(cmim_devices)} "
              f"CMIM PCell device(s)")
        if unplaced_cmims:
            # Same policy as the Cu-pillar path: a requested structure that did
            # not make it into the layout fails the run. Shipping the GDS
            # without it would hand fabrication a silently incomplete
            # interposer, and exit 0 would tell the dialog it went fine.
            shown = ", ".join(unplaced_cmims[:12])
            if len(unplaced_cmims) > 12:
                shown += ", ... (%d total)" % len(unplaced_cmims)
            print(
                "\nERROR: %d of %d cap_cmim device(s) could not be placed and "
                "are MISSING from the interposer GDS.\n"
                "  Devices: %s\n"
                "  See the warnings above for the per-device cause. Common "
                "ones: the IntM4TM2 PCell library could not be loaded (set "
                "INTERPOSER_PDK_ROOT), or w/l on the footprint are outside "
                "what the cmim PCell accepts."
                % (len(unplaced_cmims), len(cmim_devices), shown),
                file=sys.stderr)
            return False

    # Seal ring on the board outline. After the CMIM PCells, because both bind
    # the layout to the intm4tm2 technology, and before write(), which stops
    # when the drawn geometry crowds the ring.
    if seal_ring:
        try:
            ring = generator.add_seal_ring()
        except SealRingRefused as exc:
            _report_refusal("seal ring", exc)
            return False
        print("Seal ring: drawn on the board outline (%.3f, %.3f)-(%.3f, "
              "%.3f) um, %.3f x %.3f um" % (ring.left, ring.bottom, ring.right,
                                            ring.top, ring.width(),
                                            ring.height()))

    # Write interposer GDS (routing + cu-pillars, without chiplet dies). With
    # the fill on, the sidecars wait for it: the boundary manifest binds the
    # digest of the final bytes, and the fill rewrites the file in place.
    try:
        generator.write(output_path, sidecars=not insert_metal_fill)
    except SealRingRefused as exc:
        _report_refusal("seal ring", exc)
        return False
    print(f"Interposer GDS file written to: {output_path}")

    # Metal density fill (PDK engine, on the interposer GDS in place). Runs
    # after write() so it operates on the final drawn metal + keep-outs. The
    # complete GDS below is intentionally left unfilled: it embeds chiplet dies
    # on the same metal layers, which are not interposer metal for density.
    if insert_metal_fill:
        fill_result = _insert_metal_fill(
            output_path, cell_name, fill_mode, _filler_layers(layer_map))
        if fill_result["status"] == "error":
            return False
        generator.write_sidecars(output_path)
        report = fill_result.get("report") or {}
        if isinstance(report, dict):
            concerns = []
            if report.get("converged") is False:
                concerns.append("did not converge")
            for key, val in report.items():
                if isinstance(val, dict):
                    if val.get("converged") is False:
                        concerns.append("%s did not converge" % key)
                    if val.get("state") in ("under", "over", "split"):
                        concerns.append("%s %s" % (key, val["state"]))
            if concerns:
                print("Warning: metal fill density concern (%s); see the fill "
                      "density report. The GDS may fail CMP density sign-off."
                      % ", ".join(concerns), file=sys.stderr)

    # Early design-error signal (precursor of the assembly containment
    # rule): drawn geometry sticking out of the board outline. KiCad's own
    # DRC flags the off-board footprint at design time; this catches it
    # again at the GDS, where it would otherwise silently widen the layout.
    outline_bbox = generator.get_outline_bbox()
    if outline_bbox:
        fx, fy, fw, fh = generator.get_top_cell_bbox()
        ox, oy, ow, oh = outline_bbox
        excess = [(name, v) for name, v in (
            ("left", ox - fx),
            ("bottom", oy - fy),
            ("right", (fx + fw) - (ox + ow)),
            ("top", (fy + fh) - (oy + oh)),
        ) if v > 0.005]
        if excess:
            detail = ", ".join(f"{name} {v:.2f} um" for name, v in excess)
            print(f"Warning: drawn geometry extends outside the board "
                  f"outline ({detail}). A die or trace sits off-board; "
                  f"the interposer keeps its outline size in viewers.",
                  file=sys.stderr)

    # Update chiplet file if requested
    if chiplet_file_path:
        bbox = generator.get_top_cell_bbox()
        # A finalize failure must surface: an un-finalized .chiplet still
        # carries _metadata.finalize_required and ChipletFormat::load refuses
        # it, so reporting exit 0 here would hand studio an unusable file.
        if not update_chiplet_file(chiplet_file_path, output_path, bbox,
                           connection_type=connection_type,
                           attachment_surface_z=attach_frame,
                           io_pads=placed_io_pads,
                           devices=parser.devices,
                           die_connections=die_connections,
                           die_thicknesses=die_thicknesses,
                           outline_bbox=outline_bbox,
                           to_um=generator._to_um,
                           to_um_y=generator._to_um_y,
                           die_outline_layers=die_outline_layers,
                           die_outline_exempt=die_outline_exempt,
                           outline_reader=outline_reader,
                           io_pad_refs=head_io_pad_refs,
                           system_chiplet=system_chiplet,
                           die_layouts=head_die_layouts):
            print(f"ERROR: failed to finalize chiplet file "
                  f"'{chiplet_file_path}'.", file=sys.stderr)
            return False

    # Generate complete GDS with chiplets if requested
    if with_chiplets and parser.devices:
        print(f"\nGenerating complete GDS with chiplets...")

        # Determine flip-chip dies. Primary signal: a die attached by a
        # body-bearing connection method (a pillar or a solder bump) --
        # resolved from the in-scope per-die map, or the assembly default for
        # dies without one -- so orientation does not depend on the .chiplet
        # sidecar (which is absent when --update-chiplet-file is not passed).
        flip_chip_refs = set()
        for device in parser.devices:
            method = (die_connections or {}).get(device.ref) or connection_type
            if method and _connection_to_body_diameter(method) is not None:
                flip_chip_refs.add(device.ref)

        # Union with the .chiplet sidecar's explicit declarations when present.
        if chiplet_file_path:
            import yaml
            try:
                # Guarded read (H-B): delegate to the vendored reference loader.
                # allow_intermediate=True since the finalize step above already
                # canonicalised the file. A ChipletFormatError (unsupported major
                # / malformed) is deliberately NOT in the (OSError, yaml.YAMLError)
                # handler below: this is an exporter, so bad input must reject
                # (propagates to a nonzero exit), never warn-and-continue.
                chiplet_data = _vendored_cfio().load(
                    chiplet_file_path, allow_intermediate=True)
                for comp in chiplet_data.get('components', []):
                    # The frame contract (coord_frame_contract.md 2.4) defines
                    # only face_up and flip_chip; an absent field means face_up.
                    # Reject a non-canonical token (a typo, or "face_down")
                    # rather than silently treating it as face_up (un-mirrored):
                    # this is an exporter, which 2.4 says MUST reject.
                    orient = comp.get('orientation') or ''
                    if orient not in ('', 'face_up', 'flip_chip'):
                        raise ValueError(
                            "component %r in %s has an unrecognized orientation "
                            "%r; expected face_up or flip_chip (use flip_chip, "
                            "not face_down)."
                            % (comp.get('id'), chiplet_file_path, orient))
                    if comp.get('connection') or orient == 'flip_chip':
                        flip_chip_refs.add(comp.get('id', ''))
            except (OSError, yaml.YAMLError) as exc:
                print(f"Warning: could not read flip-chip orientation from "
                      f"'{chiplet_file_path}': {exc}", file=sys.stderr)

        # Add devices to the layout
        device_success = 0
        unplaced_dies = []
        for device in parser.devices:
            is_flip = device.ref in flip_chip_refs
            outline = die_plan.outlines.get(device.ref)
            if outline is None:
                why = ("exempt" if device.ref in die_plan.exempt
                       else "no die outline declared")
                print(f"  {device.ref}: boundary is the placed cell bbox "
                      f"({why})")
            try:
                placed = generator.add_device(device, flip_chip=is_flip,
                                              outline=outline)
            except DieGdsError as exc:
                print(f"ERROR: {device.ref}: {exc}. Refusing to write the "
                      f"complete GDS.", file=sys.stderr)
                return False
            if placed:
                device_success += 1
                if is_flip:
                    print(f"  {device.ref}: placed with mirror-X (flip-chip)")
            elif die_plan.gds(device.ref) is not None:
                unplaced_dies.append(device.ref)

        print(f"Successfully added {device_success}/{len(parser.devices)} devices")

        # A die the head sized or exempted is one the .chiplet describes and
        # the boundary manifest must carry; a complete GDS without it is not
        # the assembly the .chiplet says.
        if unplaced_dies:
            print(f"ERROR: die(s) {', '.join(unplaced_dies)} could not be "
                  f"placed (see above); refusing to write the complete GDS.",
                  file=sys.stderr)
            return False

        if device_success == 0:
            print(f"ERROR: --with-chiplets requested but 0 of "
                  f"{len(parser.devices)} device GDS file(s) loaded; refusing "
                  f"to emit a die-less complete GDS.", file=sys.stderr)
            return False

        # Drop the imported chiplet templates (left orphan by
        # _place_die_flipped) so the GDS has a single top-level TOP
        # cell instead of TOP + Metal_Test (or whatever was imported).
        n_pruned = generator.cleanup_orphan_top_cells()
        if n_pruned:
            print(f"Pruned {n_pruned} orphan top-level cell(s) "
                  "(imported flip-chip templates)")

        # Write complete GDS
        if complete_output_path:
            generator.write(complete_output_path)
            print(f"Complete GDS file (with chiplets) written to: {complete_output_path}")
            if generator.has_seal_ring():
                print("Seal ring: carried into the complete GDS")
    elif with_chiplets and not parser.devices:
        print("Warning: --with-chiplets specified but no GDS_FILE devices found in HYP")

    return True


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Convert KiCad HYP files to GDS format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s interposer_template.hyp
  %(prog)s interposer_template.hyp -o interposer.gds
  %(prog)s interposer_template.hyp -l custom_layers.lyp -c MYCELL

  # Generate both interposer and complete GDS with chiplets:
  %(prog)s interposer_template.hyp -o interposer.gds --with-chiplets --complete-output complete.gds

  # Also update a .chiplet file with the interposer GDS path, sizing U1 and U2
  # from the 39/4 outline in their own GDS and leaving U3 as it is:
  %(prog)s interposer_template.hyp -o interposer.gds --update-chiplet-file design.chiplet \
      --die-outline-layers U1=39/4,U2=39/4 --die-outline-exempt U3

  # Generate cu-pillar pads at chiplet pad locations:
  %(prog)s interposer_template.hyp --with-chiplets --pad-locations U1=pins_interposer.json,U2=pins_diffamp.json

  # Merge pre-generated cu-pillar GDS (from bump_mirror.py):
  %(prog)s interposer_template.hyp --cupillar-gds cupillars.gds -o interposer.gds
        """
    )

    parser.add_argument(
        "hyp_file",
        help="Input HYP file path"
    )
    parser.add_argument(
        "-o", "--output",
        help="Output GDS file path (default: <input>.gds)"
    )
    parser.add_argument(
        "-l", "--lyp",
        default=None,
        help="KLayout LYP layer properties file (default: the interposer "
             "PDK's canonical intm4tm2.lyp via env/walk discovery; the .lyp "
             "belongs to the PDK, so set INTERPOSER_PDK_ROOT or pass this "
             "explicitly when no checkout resolves)"
    )
    parser.add_argument(
        "-c", "--cell",
        default="INTERPOSER",
        help="Top-level cell name (default: INTERPOSER)"
    )
    parser.add_argument(
        "--tech-json",
        type=str,
        metavar="JSON_FILE",
        help="Path to interposer_tech_default.json for PDK via parameters "
             "(default: use built-in values from sg13g2_tech.json)"
    )
    parser.add_argument(
        "--with-chiplets",
        action="store_true",
        help="Generate complete GDS with chiplet instances from GDS_FILE entries"
    )
    parser.add_argument(
        "--complete-output",
        type=str,
        metavar="PATH",
        help="Output path for complete GDS with chiplets (default: <input>_complete.gds)"
    )
    parser.add_argument(
        "--update-chiplet-file",
        type=str,
        metavar="CHIPLET_FILE",
        help="Update .chiplet file with interposer GDS path"
    )
    parser.add_argument(
        "--pad-locations",
        type=str,
        metavar="REF=FILE[,REF=FILE,...]",
        help="Pin list JSON files per device; the landing generator places "
             "DRC-validated pads of the die's connection method (Cu pillar or "
             "solder bump) at these pads (an alternative to a "
             "pre-generated --cupillar-gds). "
             "E.g. U1=pins_interposer.json,U2=pins_diffamp.json"
    )
    parser.add_argument(
        "--cupillar-gds",
        type=str,
        metavar="FILE",
        help="Pre-generated cu-pillar GDS file (from bump_mirror.py) to merge "
             "into the interposer layout"
    )
    parser.add_argument(
        "--connection-type",
        type=str,
        choices=_connection_type_cli_choices(),
        default="",
        metavar="TYPE",
        # Checked against the manifest's methods when one is found, but not
        # printed: docs/reference/ publishes this --help, so it must read the
        # same on any host.
        help="Connection stack type for chiplet file: a method id the "
             "interconnect PDK manifest lists (e.g., cupillar_opt1). Injects "
             "connection_stacks section and sets connection on die "
             "components. A die whose landing the pad cell cannot draw is "
             "refused; connection_methods.py lists which methods those are."
    )
    parser.add_argument(
        "--die-connections",
        type=str,
        metavar="REF=METHOD[,REF=METHOD,...]",
        help="Per-die connection stack overrides (e.g. U1=cupillar_opt1,"
             "U2=vendorx_microbump). Dies not listed use --connection-type. "
             "Each die's 3D bodies are drawn with its own method's layers "
             "and diameter, and its connection field in the .chiplet is set "
             "accordingly."
    )
    parser.add_argument(
        "--die-thicknesses",
        type=str,
        metavar="REF=UM[,REF=UM,...]",
        help="Per-die physical thickness in micrometers (e.g. U1=750,"
             "U2=750). Written to each die's dimensions.thickness in the "
             ".chiplet. Dies not listed keep the format default of 0.0. "
             "Interconnect stack heights are a separate axis "
             "(connection_stacks); do not fold them in here."
    )
    parser.add_argument(
        "--die-outline-layers",
        type=str,
        metavar="REF=LAYER/DATATYPE[,...]",
        help="The layer each die's own GDS draws its die outline on (e.g. "
             "U1=39/4,U2=39/4). With --update-chiplet-file, a declared die's "
             "width/height in the .chiplet become that outline's size, and "
             "with --with-chiplets it is the die's boundary. The layer must "
             "merge to exactly one rectangle under the GDS's one top cell; "
             "anything else refuses. Every die of the .chiplet must be named "
             "here or in --die-outline-exempt, and only one of them."
    )
    parser.add_argument(
        "--die-outline-exempt",
        type=str,
        metavar="REF[,REF,...]",
        help="Dies that keep the width/height already in the .chiplet and "
             "the placed cell bbox as boundary (e.g. U3). Each is logged "
             "by name. Their GDS must still exist and have one top cell."
    )
    parser.add_argument(
        "--io-pads",
        type=str,
        metavar="JSON_FILE",
        help="Sidecar JSON with external I/O pad locations in planner net names "
             "(never inverted here; produced by "
             "gds_to_kicad/io_pads/kicad_pcb_to_iopads.py). Each entry is "
             "rendered in the interposer GDS based on its io_class field "
             "(wire_bond now; flipped_bump and tsv_bump reserved). When "
             "combined with --update-chiplet-file, the placed pads are also "
             "injected under the interposer component."
    )
    parser.add_argument(
        "--cmim-devices",
        type=str,
        metavar="JSON_FILE",
        help="Sidecar JSON with cap_cmim footprint parameters for IntM4TM2 PCell placement."
    )
    parser.add_argument(
        "--system-chiplet",
        type=str,
        metavar="CHIPLET_FILE",
        help="The System .chiplet the assembly was built from. With "
             "--update-chiplet-file, the interposer's and each die's "
             "technology, and the technologies block, are taken from it by "
             "component id. Refused, with nothing written, when a component "
             "or a technology id has no match there."
    )
    parser.add_argument(
        "--die-layouts",
        type=str,
        metavar="JSON_FILE",
        help="Sidecar JSON naming, per die reference, its full layout (an "
             "absolute path), the contract's top_cell and bbox_um. Needs "
             "--update-chiplet-file, where that die's layout and top_cell "
             "become "
             "the full layout's, and its metadata names the abstract its "
             "GDS_FILE gives, which the complete GDS keeps. Refused, with "
             "nothing written, when an entry names no die or either file's "
             "top cell box is not bbox_um exactly."
    )
    parser.add_argument(
        "--nofill-regions",
        type=str,
        metavar="JSON_FILE",
        help="Sidecar JSON with no-fill (keep-out) polygons authored in KiCad "
             "(produced by writers/chiplet_writer.write_nofill_regions_json). "
             "Painted onto the GDS keep-out datatypes (160/0 global, <metal>/23 "
             "per metal) so the PDK metal-fill generators skip them."
    )
    parser.add_argument(
        "--insert-metal-fill",
        action="store_true",
        help="Stamp metal density fill onto the interposer GDS using the "
             "interposer PDK's fill engine (fill_closure.fill_stack). Needs "
             "INTERPOSER_PDK_ROOT with the fill work and the 'klayout' binary "
             "on PATH (same as assembly DRC). Off by default; the complete GDS "
             "is left unfilled."
    )
    parser.add_argument(
        "--fill-mode",
        choices=("single-pass", "closure"),
        default="single-pass",
        help="single-pass: stamp all four metals once (fast, default). "
             "closure: density-feedback loop for M4/M5 (slower, verifies the "
             "density band against the sign-off deck). Ignored without "
             "--insert-metal-fill."
    )
    parser.add_argument(
        "--seal-ring",
        action="store_true",
        help="Draw the interposer PDK's seal ring (IntM4TM2 sealring PCell) "
             "on the board outline, which must be one axis-aligned "
             "rectangle. Refused, with nothing written, when the outline is "
             "missing, not a rectangle or under the PCell minimum, or when "
             "fabricated geometry sits in the ring band or closer to the "
             "ring than its space rule. Off by default; the complete GDS "
             "carries the ring."
    )
    parser.add_argument(
        "--annotate-boundaries",
        action="store_true",
        help="Also paint each chiplet boundary (and instance label) onto a "
             "viewer-only annotation layer for eyeball inspection of the GDS. "
             "No DRC rule reads this layer; the assembly contract stays in the "
             "<gds>.boundaries.json manifest. Off by default."
    )
    parser.add_argument(
        "--grid-nm",
        type=int,
        default=MANUFACTURING_GRID_NM,
        help=f"Manufacturing grid in nanometers for the drawn geometry "
             f"(default {MANUFACTURING_GRID_NM}, the value the interposer "
             f"PDK's own offgrid deck enforces). 0 disables snapping and "
             f"keeps traces as paths; that output will not pass the carrier "
             f"DRC and is for debugging the raw conversion only."
    )
    parser.add_argument(
        "--boundary-viz-layer",
        type=str,
        default="1000/0",
        metavar="LAYER/DATATYPE",
        help="GDS layer for --annotate-boundaries (default: 1000/0, outside "
             "IHP SG13G2's fab range). Ignored unless --annotate-boundaries."
    )
    args = parser.parse_args()

    # Report a manifest refusal recorded while the parser was built. Here and
    # not there: --help has now had its chance to print, argv exists, and no
    # artifact has been written yet.
    _report_deferred_source_refusal()

    # Default lyp: the interposer PDK's canonical copy (env/walk); no
    # plugin-local fallback, the .lyp belongs to the PDK.
    args.lyp = _or_exit(_layer_map.resolve_lyp_path, args.lyp, start=__file__)

    # Expand ${VAR} ecosystem-root references in every path argument
    # (env -> sibling-checkout walk -> loud failure). Plain absolute or
    # relative paths pass through untouched.
    for _attr in ("hyp_file", "output", "tech_json",
                  "complete_output", "update_chiplet_file",
                  "cupillar_gds", "io_pads", "cmim_devices", "nofill_regions"):
        setattr(args, _attr, _expand_path_vars(getattr(args, _attr)))

    # Determine output path (interposer-only GDS)
    if args.output:
        output_path = args.output
    else:
        base = Path(args.hyp_file).stem
        output_path = str(Path(args.hyp_file).parent / f"{base}_interposer.gds")

    # Determine complete output path (with chiplets)
    if args.complete_output:
        complete_output_path = args.complete_output
    else:
        base = Path(args.hyp_file).stem
        complete_output_path = str(Path(args.hyp_file).parent / f"{base}_complete.gds")

    # Parse pad locations: "U1=file1.json,U2=file2.json" -> dict
    pad_locations = None
    if args.pad_locations:
        pad_locations = {}
        for item in args.pad_locations.split(','):
            if '=' not in item:
                print(f"Error: Invalid pad-locations format: '{item}'. Use REF=FILE.", file=sys.stderr)
                return 1
            ref, path = item.split('=', 1)
            pad_locations[ref.strip()] = _expand_path_vars(path.strip())

    # Parse per-die connections: "U1=cupillar_opt1,U2=vendorx_microbump"
    die_connections = None
    if args.die_connections:
        die_connections = {}
        for item in args.die_connections.split(','):
            if '=' not in item:
                print(f"Error: Invalid die-connections format: '{item}'. "
                      f"Use REF=METHOD.", file=sys.stderr)
                return 1
            ref, method = item.split('=', 1)
            die_connections[ref.strip()] = method.strip()

    # Parse per-die thickness: "U1=750,U2=750" (um, positive finite floats)
    die_thicknesses = None
    if args.die_thicknesses:
        die_thicknesses = {}
        for item in args.die_thicknesses.split(','):
            if '=' not in item:
                print(f"Error: Invalid die-thicknesses format: '{item}'. "
                      f"Use REF=UM.", file=sys.stderr)
                return 1
            ref, raw = item.split('=', 1)
            try:
                value = float(raw.strip())
            except ValueError:
                value = float("nan")
            if not math.isfinite(value) or value <= 0.0:
                print(f"Error: Invalid die thickness '{raw.strip()}' for "
                      f"'{ref.strip()}': expected a positive number of "
                      f"micrometers.", file=sys.stderr)
                return 1
            die_thicknesses[ref.strip()] = value

    # Parse the die outline declarations: "U1=39/4,U2=39/4" and "U3". Strict:
    # an empty or malformed token is an error naming it, never skipped. Given
    # but empty ("") is malformed too; absent is None.
    die_outline_layers = None
    die_outline_exempt = None
    try:
        if args.die_outline_layers is not None:
            die_outline_layers = _die_outline.parse_outline_layers(
                args.die_outline_layers)
        if args.die_outline_exempt is not None:
            die_outline_exempt = _die_outline.parse_outline_exempt(
                args.die_outline_exempt)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.die_layouts is not None and not args.die_layouts:
        print("Error: --die-layouts was given an empty path.", file=sys.stderr)
        return 1

    # Parse the annotation layer "LAYER/DATATYPE" (only used if --annotate-boundaries)
    try:
        _vl, _vd = args.boundary_viz_layer.split('/', 1)
        boundary_viz_layer = (int(_vl), int(_vd))
    except (ValueError, AttributeError):
        print(f"Error: Invalid --boundary-viz-layer '{args.boundary_viz_layer}'. "
              "Use LAYER/DATATYPE, e.g. 1000/0.", file=sys.stderr)
        return 1

    # Refuse an annotation layer that collides with a fabrication layer: the
    # painter clear()s the layer first, so aliasing prBoundary (235/0; 189/0
    # kept for pre-migration GDS), the exchange0 (190/0), or a landing-pad fab
    # layer would silently wipe real geometry (and break the "never aliases a
    # fab layer" contract).
    if args.annotate_boundaries:
        _fab_layers = {pair
                       for layers in GDSGenerator.LANDING_FAB_LAYERS.values()
                       for pair in layers.values()} | {(235, 0), (189, 0), (190, 0)}
        if boundary_viz_layer in _fab_layers:
            print(f"Error: --boundary-viz-layer {boundary_viz_layer[0]}/"
                  f"{boundary_viz_layer[1]} collides with a fabrication layer; "
                  f"choose one outside the fab range (default 1000/0).",
                  file=sys.stderr)
            return 1

    # Run conversion
    success = convert_hyp_to_gds(
        hyp_path=args.hyp_file,
        output_path=output_path,
        lyp_path=args.lyp,
        cell_name=args.cell,
        with_chiplets=args.with_chiplets,
        complete_output_path=complete_output_path,
        chiplet_file_path=args.update_chiplet_file,
        tech_json_path=args.tech_json,
        pad_locations=pad_locations,
        connection_type=args.connection_type,
        cupillar_gds_path=args.cupillar_gds,
        io_pads_json=args.io_pads,
        annotate_boundaries=args.annotate_boundaries,
        boundary_viz_layer=boundary_viz_layer,
        die_connections=die_connections,
        die_thicknesses=die_thicknesses,
        cmim_devices_json=args.cmim_devices,
        grid_nm=args.grid_nm,
        nofill_regions_json=args.nofill_regions,
        insert_metal_fill=args.insert_metal_fill,
        fill_mode=args.fill_mode,
        die_outline_layers=die_outline_layers,
        die_outline_exempt=die_outline_exempt,
        seal_ring=args.seal_ring,
        system_chiplet_path=args.system_chiplet,
        die_layouts_json=args.die_layouts,
    )

    return 0 if success else 1


def _main_reporting_refusals():
    """Run main(), reporting a SourceRefused with the shared table's exit code.

    A refusal raised deep in the run (the finalizer, a helper that lets the
    reader's own exception through) would otherwise leave as a traceback and
    exit 1. Exit 1 means bad caller input, so it would send the user looking
    for an argument to change when the fix is to align the PDK.
    """
    try:
        return main()
    except SourceRefused as exc:
        print("Error: %s" % exc, file=sys.stderr)
        print("  Refusing to export: a cross-repo source is present and "
              "cannot be used.", file=sys.stderr)
        return exc.exit_code
    except (DieOutlineRefused, TechnologyRefused, DieLayoutRefused) as exc:
        # convert_hyp_to_gds reports these itself at the head; this catches a
        # refusal from a path that raises it instead (update_chiplet_file).
        for line in exc.lines():
            print(line, file=sys.stderr)
        return 1
    except AttachFrameRefused as exc:
        print("Attach frame refused: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_main_reporting_refusals())
