#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
HYP to GDS Converter

Converts KiCad HYP (Hyperlynx) files to GDSII format.
Extracts trace segments from HYP and generates polygons on appropriate PDK layers.
"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import klayout.db as db
except ImportError:
    print("Error: KLayout Python module not found.", file=sys.stderr)
    print("Please install with: pip install klayout", file=sys.stderr)
    sys.exit(1)


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


class LayerMap:
    """Manages PDK layer definitions from KLayout LYP file."""

    def __init__(self, lyp_path: str):
        self.layers: Dict[str, Tuple[int, int]] = {}
        self._load_lyp(lyp_path)

    def _load_lyp(self, lyp_path: str) -> None:
        """Load layer definitions from KLayout LYP (XML) file.

        LYP format per layer:
            <properties>
                <name>Metal4.drawing</name>
                <source>50/0</source>   (or "50/0@1" with cell view suffix)
            </properties>
        """
        try:
            tree = ET.parse(lyp_path)
            root = tree.getroot()

            for props in root.findall('.//properties'):
                name_elem = props.find('name')
                source_elem = props.find('source')
                if name_elem is None or source_elem is None:
                    continue
                if name_elem.text is None or source_elem.text is None:
                    continue

                full_name = name_elem.text.strip()
                source = source_elem.text.strip()

                # Strip @N cell view suffix (e.g. "50/0@1" -> "50/0")
                if '@' in source:
                    source = source.split('@')[0]

                # Parse layer/datatype from source
                try:
                    parts = source.split('/')
                    layer_num = int(parts[0])
                    datatype = int(parts[1]) if len(parts) > 1 else 0
                except (ValueError, IndexError):
                    continue

                # Split "Name.purpose" into name and purpose
                if '.' in full_name:
                    name, purpose = full_name.split('.', 1)
                else:
                    name = full_name
                    purpose = "drawing"

                # Store with "name:purpose" key
                key = f"{name}:{purpose}"
                self.layers[key] = (layer_num, datatype)

                # Also store drawing layers with just name for convenience
                if purpose == "drawing":
                    self.layers[name] = (layer_num, datatype)

        except FileNotFoundError:
            print(f"Error: Could not find {lyp_path}", file=sys.stderr)
            sys.exit(1)
        except ET.ParseError as e:
            print(f"Error parsing LYP file: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error reading layer table: {e}", file=sys.stderr)
            sys.exit(1)

    def get_layer(self, name: str, purpose: str = "drawing") -> Tuple[int, int]:
        """Get GDS layer and datatype for a given layer name."""
        key = f"{name}:{purpose}"
        if key in self.layers:
            return self.layers[key]
        if name in self.layers:
            return self.layers[name]
        raise KeyError(f"Layer '{name}' with purpose '{purpose}' not found in PDK")

    def __repr__(self) -> str:
        return f"LayerMap({len(self.layers)} layers from LYP)"


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

# Interposer die-attachment surface z (a.k.a. BEOL top), in micrometers: the
# plane dies mount on, in the interposer's local frame. For SG13G2 it is the top
# of TopMetal2 above the silicon surface -- 10.83 (TM2 bottom) + 3.00 (TM2
# thickness) = 13.83, the sum of the SG13G2 BEOL band thicknesses (process spec
# Rev 1.2, Fig 1.1.1 / Sec 2.16). It is a *process* constant, not a per-design
# tunable, and is a distinct quantity from the interposer's physical body
# thickness (dimensions.thickness, hundreds of um), which comes from the KiCad
# board stackup. The same value is declared across the ecosystem and MUST stay
# in sync with it:
#   - chiplet-studio/configs/stackups/intm4tm2.yaml  (attachment_surface_z: 13.83)
#   - the interconnect PDK stackup fragments, which rebase onto it
#   - chiplet-spec coord_frame_contract.md sections 3.2 / 3.4
# If the interposer PDK ever ships a machine-readable BEOL-top descriptor under
# INTERPOSER_PDK_ROOT, that is the seam to source this from (falling back here).
SG13G2_ATTACHMENT_SURFACE_Z_UM = 13.83


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

        LIMITATION: the .hyp ARC record carries no sweep direction, so this
        always draws the minor (<=180 degree) arc between the endpoints. A
        KiCad arc with a sweep greater than 180 degrees is rendered as its
        complement. Interposer routing is overwhelmingly orthogonal, so this
        is rare; a warning is emitted at the ambiguous semicircle.
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

        # Determine arc direction (shortest path -> minor arc).
        diff = angle2 - angle1
        if diff > math.pi:
            diff -= 2 * math.pi
        elif diff < -math.pi:
            diff += 2 * math.pi
        # At ~180 degrees the minor/major choice is ambiguous from endpoints
        # alone; flag it so a mis-rendered curved trace is not silent.
        if abs(abs(diff) - math.pi) < math.radians(2.0):
            print(f"Warning: arc on {arc.layer} spans ~180 degrees; the .hyp "
                  f"carries no sweep direction, so the minor arc is drawn and "
                  f"may not match the source.", file=sys.stderr)

        # Generate points along the arc
        points = []
        for i in range(num_points + 1):
            t = i / num_points
            angle = angle1 + t * diff
            px = xc + radius * math.cos(angle)
            py = yc + radius * math.sin(angle)
            points.append((px, py))

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

    @staticmethod
    def _unit(dx: int, dy: int) -> Tuple[int, int]:
        """Direction of a 0/45/90 run, as components in {-1, 0, 1}."""
        return ((dx > 0) - (dx < 0), (dy > 0) - (dy < 0))

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

        Falls back to the plain square, which is always safe, when the shape
        cannot be built exactly: a turn sharper than 90 degrees (where the
        flanks meet in a spike far outside the trace), a run that is neither
        orthogonal nor exactly diagonal so its flank is only approximated, or
        any result that is off-grid, not 0/45/90, or not a single piece.
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
            return square
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
                        width_um: float) -> List[db.Polygon]:
        """Fracture a trace centreline into on-grid polygons.

        One quad per segment plus one patch per interior corner. The union is
        the copper a mitered path would have drawn; the difference is that
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

        polys: List[db.Polygon] = []
        for a, b in zip(pts, pts[1:]):
            dx, dy = b.x - a.x, b.y - a.y
            if dx == 0:
                ox, oy = half, 0
            elif dy == 0:
                ox, oy = 0, half
            elif abs(dx) == abs(dy):
                ox = -diag if dy > 0 else diag
                oy = diag if dx > 0 else -diag
            else:
                # Neither orthogonal nor 45: a discretized arc, or a board
                # drawn off the 45 grid. The angle rule will report it; draw
                # the nearest on-grid quad rather than silently reshaping the
                # copper to make a violation disappear.
                length = math.hypot(dx, dy)
                ox = self._snap(int(round(-dy * half / length)))
                oy = self._snap(int(round(dx * half / length)))
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

    def add_path(self, points: List[Tuple[float, float]], width_um: float, layer: str) -> bool:
        """
        Add a trace. Points are in micrometers, width is in micrometers.

        With the manufacturing grid active (the default) the trace is drawn as
        on-grid polygons; see _trace_polygons for why it cannot stay a single
        path. With snapping disabled it keeps the historical DPath, which is
        smaller and prettier in a viewer but fails the carrier's offgrid deck.
        """
        if len(points) < 2:
            return False

        try:
            layer_idx = self._get_gds_layer(layer)
        except KeyError as e:
            print(f"Warning: {e} - skipping path on layer {layer}")
            return False

        if self.grid_dbu <= 1:
            dpoints = [db.DPoint(x, y) for x, y in points]
            self.routing_cell.shapes(layer_idx).insert(db.DPath(dpoints, width_um))
            return True

        polys = self._trace_polygons(points, width_um)
        if not polys:
            return False
        for poly in polys:
            self.routing_cell.shapes(layer_idx).insert(poly)
        return True

    def add_segments_as_paths(self, segments: List[TraceSegment], arcs: List[TraceArc] = None) -> int:
        """
        Convert segments and arcs to continuous paths and add to layout.
        Returns number of paths created.
        """
        if arcs is None:
            arcs = []

        # Group by layer and width
        groups = self._build_trace_elements(segments, arcs)

        total_paths = 0
        for (_net, layer, width), elements in groups.items():
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
            paths = self._connect_traces_to_paths(elements)

            for path_points in paths:
                if self.add_path(path_points, width_um, layer):
                    total_paths += 1

        return total_paths

    def _calculate_via_array(self, target_size_um: float, via_type: str) -> int:
        """
        Calculate number of vias (rows or columns) needed to approximate target size.

        Uses the formula: total_size = 2 * enc + n * size + (n-1) * sep
        Solving for n: n = ceil((target - 2*enc + sep) / (size + sep))

        Args:
            target_size_um: Target pad size in micrometers
            via_type: Type of via ('Vn' for Via1-4, 'TV1' for TopVia1, 'TV2' for TopVia2)

        Returns:
            Number of vias needed (minimum 1)
        """
        params = self.PDK_VIA_PARAMS.get(via_type)
        if not params:
            return 1

        size = params['size']
        sep = params['sep']
        enc = params['enc']

        # n = ceil((target - 2*enc + sep) / (size + sep))
        n = math.ceil((target_size_um - 2 * enc + sep) / (size + sep))
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

    def add_device(self, device: Device, flip_chip: bool = False) -> bool:
        """
        Add a device/chiplet by loading its GDS file and instantiating it.

        Args:
            device: Device with GDS file path and position

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

            # Import the GDS if we haven't seen this file before.
            # The first import creates a "template" cell; subsequent devices
            # using the same GDS reuse the template's subcells without
            # re-reading the file.
            if not hasattr(self, '_imported_templates'):
                self._imported_templates: Dict[str, db.Cell] = {}

            if expected_cell_name not in self._imported_templates:
                # Track existing cells before import
                existing_cells = set(cell.name for cell in self.layout.each_cell())

                # Read the external GDS directly into the current layout
                self.layout.read(str(gds_path))

                # Find newly imported cells (cells that didn't exist before)
                new_cells = [cell for cell in self.layout.each_cell() if cell.name not in existing_cells]

                if not new_cells:
                    print(f"Warning: No new cells imported from {device.gds_file}")
                    return False

                # Find the top cell among new cells (one that has no parent among new cells)
                template_cell = None
                for cell in new_cells:
                    has_parent_in_new = False
                    for other in new_cells:
                        if other != cell:
                            for inst in other.each_inst():
                                if inst.cell.name == cell.name:
                                    has_parent_in_new = True
                                    break
                        if has_parent_in_new:
                            break
                    if not has_parent_in_new:
                        template_cell = cell
                        break

                if not template_cell:
                    template_cell = new_cells[0]  # Fallback to first new cell

                self._imported_templates[expected_cell_name] = template_cell

            template_cell = self._imported_templates[expected_cell_name]
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
            cell_bbox = imported_cell.dbbox()
            if not flip_chip and device.rotation != 0.0:
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

    # Cu-pillar pad layer definitions (layer_num, datatype). The TopMetal2
    # entry is shared with the wire-bond I/O pad cell below. Cu-pillar cell
    # generation itself lives in the interposer PDK's bump_mirror (fab pads)
    # delegating 3D bodies to the interconnect PDK's bump3d_generator.
    CUPILLAR_FAB_LAYERS = {
        'TopMetal2':      (134, 0),
        'Passiv:pillar':  (9, 35),
        'dfpad:pillar':   (41, 35),
        'Recog:pillar':   (99, 35),
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
    # the crossing is made exact (see SESSION_LOG: it needs the pad centre and
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
            RuntimeError: the PDK or its PyCell library is not reachable, or
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
    # Patches are axis-aligned rectangles, and that is a constraint, not a
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

        A notch is a gap inside a single piece of copper, so filling one can
        never connect two nets; the gap between two separate pieces is a
        space violation and is deliberately left alone. That distinction is
        the whole reason this uses notch_check rather than the more obvious
        oversize/undersize closing: the closing would bridge any two nets
        sitting at exactly the minimum space, which is legal copper, and
        turn it into a short.

        The patches are axis-aligned boxes on the manufacturing grid. They
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

        Returns the number of notches still reported after the last pass.
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
            limit = space_um * self.NOTCH_HEAL_MARGIN / self.layout.dbu
            min_span = int(round(space_um * self.NOTCH_HEAL_MIN_SPAN
                                 / self.layout.dbu))
            def in_scope_patches():
                """Patches this pass would place, keepout already removed."""
                merged = self._own_metal(li, foreign)
                boxes = self._notch_patches(
                    merged.notch_check(limit, False, db.Metrics.Euclidian),
                    min_span)
                if not keepout.is_empty():
                    boxes = boxes.not_interacting(keepout)
                return merged, boxes

            patched = 0
            for _ in range(self.NOTCH_HEAL_MAX_ITERS):
                merged, boxes = in_scope_patches()
                if (boxes - merged).is_empty():
                    # Either nothing is left to close, or what is left is
                    # inside the keepout, or the patch shape cannot close it.
                    # All three mean: stop, do not spin.
                    break
                if heal_cell is None:
                    heal_cell = self.layout.create_cell(
                        f"{self.top_cell.name}_NOTCH_HEAL")
                    self.top_cell.insert(
                        db.CellInstArray(heal_cell.cell_index(), db.Trans()))
                for box in boxes.each():
                    heal_cell.shapes(li).insert(box)
                patched += boxes.count()

            # Residual counts only what the heal owns. Notches under the
            # keepout are out of scope by design, not failures, and the
            # carrier DRC reports them against the pad rules where they
            # belong.
            merged, boxes = in_scope_patches()
            still = (boxes - merged).count()
            residual += still
            if patched or still:
                print(f"  Notch heal ({layer_num}/{datatype}, {rule_key} = "
                      f"{space_um} um): {patched} patch(es)"
                      + (f", {still} region(s) unclosed" if still else ""))
        if residual:
            print(f"Warning: {residual} notch region(s) below the minimum "
                  f"space are still open after {self.NOTCH_HEAL_MAX_ITERS} "
                  f"heal passes. "
                  f"The carrier DRC will report them; they need a layout fix, "
                  f"not a wider heal.", file=sys.stderr)
        return residual

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

    def write(self, output_path: str) -> None:
        """Write layout preserving via cell hierarchy.

        PCell variants are resolved to static geometry within their own cells.
        Via instances are grouped by metal pair for a clean hierarchy.
        Context info is stripped so the GDS opens cleanly without PDK dependencies.
        A <stem>.boundaries.json manifest with the chiplet boundaries is written
        alongside the GDS for the ADK assembly DRC, and — when the Cu-pillar
        generation path ran — a <stem>.pillars.json manifest with the as-drawn
        bump centers.
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

        save_opts = db.SaveLayoutOptions()
        save_opts.write_context_info = False
        self._paint_boundary_annotations()
        self.layout.write(output_path, save_opts)
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


def _connection_type_cli_choices():
    """CLI choices for --connection-type from the manifest (all methods).

    Returns None (argparse accepts any value) when the manifest is unavailable,
    so --help still works without the interconnect PDK installed.
    """
    im = _import_interconnect_manifest()
    if im is None:
        return None
    return im.list_methods()


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


def update_chiplet_file(chiplet_path: str, interposer_gds_path: str,
                        bbox: Tuple[float, float, float, float] = None,
                        connection_type: str = "",
                        attachment_surface_z: float = SG13G2_ATTACHMENT_SURFACE_Z_UM,
                        io_pads: Optional[List[Dict]] = None,
                        devices: Optional[List['Device']] = None,
                        die_connections: Optional[Dict[str, str]] = None,
                        die_thicknesses: Optional[Dict[str, float]] = None,
                        outline_bbox: Optional[Tuple[float, float, float,
                                                     float]] = None,
                        to_um=None, to_um_y=None) -> bool:
    """
    Update .chiplet file with interposer GDS absolute path, dimensions, position,
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
                              die's position.z = this + connection-stack height.
                              Defaults to the SG13G2 process constant 13.83.
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

    Returns:
        True if successful, False otherwise.
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

        # Find and update the interposer component
        updated = False
        for component in data.get('components', []):
            if component.get('id') == 'interposer':
                abs_path = str(Path(interposer_gds_path).resolve())
                # Colocated artifacts get a relative reference (readers
                # anchor relative paths on the .chiplet's directory), so
                # the exported set stays portable: move/copy/commit the
                # output directory and it still opens. Anything outside
                # the .chiplet's tree keeps the absolute path (.chiplet
                # is machine-local by default).
                try:
                    layout_ref = str(Path(abs_path).relative_to(
                        chiplet_file.resolve().parent))
                except ValueError:
                    layout_ref = abs_path
                component['layout'] = layout_ref

                # Read top_cell from interposer GDS
                interposer_top_cell = _read_gds_top_cell(abs_path)
                if interposer_top_cell:
                    component['top_cell'] = interposer_top_cell

                if 'dimensions' not in component:
                    component['dimensions'] = {}

                # The die-attachment (BEOL-top) surface is a component-level
                # field, decoupled from the physical body. dimensions.thickness
                # is intentionally NOT overwritten here: it keeps the KiCad
                # board-stackup value the writer emitted (the physical interposer
                # body). Consumers read attachment_surface_z as the mount
                # reference and fall back to dimensions.thickness only for legacy
                # files that predate this split.
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

                    # Per chiplet-spec/docs/coord_frame_contract.md
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
                    # Per chiplet-spec/docs/coord_frame_contract.md
                    # sections 4.2 and 6.1: io_pads positions live in
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

            # Per chiplet-spec/docs/coord_frame_contract.md sections 2
            # and 4.4: dies produced by the gds_to_kicad pipeline have
            # GDS (0,0) as the footprint anchor. The mesh is built
            # around that origin; position places it in the canonical
            # frame.
            component['anchor'] = 'gds_origin'

            conn_id = component.get('connection', '')
            if conn_id and conn_id in connection_stacks:
                stack = connection_stacks[conn_id]
                stack_height = sum(
                    layer.get('height', 0.0) for layer in stack.get('layers', [])
                )
                die_z = attachment_surface_z + stack_height
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

            # Read top_cell from the die GDS. Prefer the device's GDS path,
            # which the HYP parser already resolved against the board directory:
            # a board-relative die layout (e.g. ../chiplets/die.gds) resolves
            # correctly here regardless of the output directory, matching the
            # geometry the assembly was built from. Fall back to the .chiplet
            # layout field, resolved against the .chiplet's own directory (where
            # readers anchor a relative layout) rather than the process CWD.
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
            if die_gds:
                die_top_cell = _read_gds_top_cell(die_gds)
                if die_top_cell:
                    component['top_cell'] = die_top_cell

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
            # byte-stable for the reproducibility gate. Only real die devices
            # (dev is not None) are bundled; the interposer GDS is colocated.
            if dev is not None and die_gds and os.path.isfile(die_gds):
                out_dir = chiplet_file.resolve().parent
                recorded = component.get('layout', '')
                resolves = False
                if recorded and "${" not in recorded:
                    cand = (recorded if os.path.isabs(recorded)
                            else os.path.normpath(
                                os.path.join(str(out_dir), recorded)))
                    resolves = os.path.isfile(cand)
                if not resolves:
                    bundle_dir = out_dir / "chiplets"
                    bundle_dir.mkdir(parents=True, exist_ok=True)
                    dest = bundle_dir / os.path.basename(die_gds)
                    if os.path.realpath(die_gds) != os.path.realpath(str(dest)):
                        shutil.copy2(die_gds, dest)
                    component['layout'] = os.path.join(
                        "chiplets", os.path.basename(die_gds))
                    print(f"  {ref}: bundled die GDS -> {component['layout']} "
                          f"(self-contained; ../chiplets not reachable from "
                          f"the output dir)")

        # Strip the intermediate-frame marker emitted by KiCad's
        # exporter (see kicad/pcbnew/exporters/export_chiplet.cpp).
        # Per chiplet-spec/docs/coord_frame_contract.md section 4.1,
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

        # Write back the updated file
        with open(chiplet_file, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        print(f"Chiplet file updated: {chiplet_path}")
        return True

    except Exception as e:
        print(f"Error updating chiplet file: {e}", file=sys.stderr)
        return False


# Ecosystem-root variables accepted inside path inputs (board text vars,
# footprint fields, CLI arguments, .chiplet entries). Each maps to the
# candidate directory names walked for next to this checkout (canonical
# ecosystem name first, then the upstream repository name so default
# GitHub clones resolve too) plus the marker subpath that must exist
# under the root. Same discovery convention as
# _find_interposer_pdk_python (see adk/docs/integration.md).
_PATH_VAR_MARKERS = {
    "INTERPOSER_PDK_ROOT": (("interposer", "OpenIntM4TM2"),
                            ("libs.tech", "klayout")),
    "GDS_TO_KICAD_ROOT": (("gds_to_kicad", "gds-to-kicad"), ("pdks",)),
    "ADK_ROOT": (("adk", "ADK"), ("klayout", "drc")),
    "INTERCONNECT_PDK_ROOT": (("interconnect_pdk",
                               "IHP-Interconnect-IntM4TM2"), ("manifest",)),
    # Base SG13G2 PDK (via_stack PCell library). Standard IHP convention:
    # $PDK_ROOT/ihp-sg13g2/...
    "PDK_ROOT": (("IHP-Open-PDK",), ("ihp-sg13g2", "libs.tech", "klayout")),
}

_PATH_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _discover_path_var(name: str) -> Optional[str]:
    """Resolve an ecosystem-root variable: env first, then sibling walk.

    A set-but-invalid environment value (marker subpath missing) falls
    through to the walk, mirroring _find_interposer_pdk_python.
    Returns the root as a string, or None when unresolvable.
    """
    marker = _PATH_VAR_MARKERS.get(name)
    if marker is None:
        # Only the known ecosystem-root variables are expandable. An unknown
        # ${NAME} is a typo/misuse, not a licence to expand an arbitrary
        # environment variable; return None so _expand_path_vars fails loudly.
        return None
    env = os.environ.get(name)
    if env and Path(env).joinpath(*marker[1]).is_dir():
        return env
    dirnames, sub = marker
    here = Path(__file__).resolve()
    for base in here.parents:
        for dirname in dirnames:
            cand = base / dirname
            if cand.joinpath(*sub).is_dir():
                return str(cand)
    return None


def _expand_path_vars(path: Optional[str]) -> Optional[str]:
    """Expand ${VAR} ecosystem-root references in a path input.

    Resolution per variable: environment -> sibling-checkout walk ->
    LOUD failure (a path that silently keeps a literal ``${VAR}``
    component would just "not exist" downstream and mask the real
    problem). Paths without ``${`` pass through untouched, so absolute
    and relative inputs keep their normal semantics.
    """
    if not path or "${" not in path:
        return path

    def _repl(match):
        name = match.group(1)
        value = _discover_path_var(name)
        if value is None:
            sys.exit(
                "ERROR: cannot resolve ${%s} in path '%s'. Set the %s "
                "environment variable or keep the checkout next to this "
                "tool (ecosystem discovery convention, see "
                "adk/docs/integration.md)." % (name, path, name))
        return value

    result = _PATH_VAR_RE.sub(_repl, path)
    if "${" in result:
        # A malformed reference (e.g. unterminated ${, or an invalid name) the
        # regex could not match survived: fail loud rather than letting a
        # literal ${...} component silently "not exist" downstream.
        sys.exit(
            "ERROR: malformed variable reference in path '%s'. Use ${NAME} "
            "with an ecosystem-root name (see adk/docs/integration.md)." % path)
    return result


def _find_default_lyp() -> str:
    """Default layer-properties file for the interposer GDS.

    Resolves the interposer PDK's canonical
    ``libs.tech/klayout/tech/intm4tm2.lyp`` via env/walk discovery. The
    file belongs to the interposer PDK, not the plugin, so there is no
    vendored copy: when no checkout resolves, return the unexpanded
    ``${INTERPOSER_PDK_ROOT}`` form (mirrors _find_interposer_template) so
    the error text points somewhere actionable instead of silently using a
    stale bundled .lyp.
    """
    python_dir = _find_interposer_pdk_python()
    if python_dir is not None:
        cand = python_dir.parent / "tech" / "intm4tm2.lyp"
        if cand.is_file():
            return str(cand)
    return "${INTERPOSER_PDK_ROOT}/libs.tech/klayout/tech/intm4tm2.lyp"


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


def _read_gds_top_cell(gds_path: str) -> Optional[str]:
    """Read the top cell name from a GDS file.

    Returns the name of the top cell, or None if the file cannot be read.
    """
    try:
        gds_file = Path(_expand_path_vars(gds_path))
        if not gds_file.exists():
            return None
        layout = db.Layout()
        layout.read(str(gds_file))
        top = layout.top_cell()
        if top:
            return top.name
    except Exception:
        pass
    return None


# Python module dir inside the interconnect PDK (IHP libs.tech layout).
_INTERCONNECT_PY = ("libs.tech", "klayout", "python")


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
                return interconnect_manifest
            except Exception as exc:
                # The file exists but does not import: surface the real error
                # instead of letting callers report a misleading "not found".
                print(f"Warning: found interconnect_manifest.py in {cand} but "
                      f"could not import it: {exc}", file=sys.stderr)
                return None
    return None


def _connection_to_body_diameter(connection_type):
    """Cu-pillar body diameter (um) for a connection-stack id, or None.

    Sourced from the interconnect PDK manifest (single source of truth). Solder
    bumps (a 'Ball' body) and unknown/empty ids return None -> skip pillar gen.
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
    layers = method.get("connection_stack", {}).get("layers", [])
    if any("Ball" in layer.get("name", "") for layer in layers):
        return None
    return method.get("body_diameter_um")


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
    return method.get("adapter")


# Layer properties of the interconnect PDK's 3D body layers. Writer-verbatim
# ${VAR} form (readers expand via the ecosystem discovery convention); covers
# every method in the manifest, so it is adapter-independent.
_INTERCONNECT_LYP_REF = (
    "${INTERCONNECT_PDK_ROOT}/libs.tech/klayout/tech/interconnect.lyp")


def _interconnect_technology_block(adapter):
    """Technology metadata for an interconnect adapter, or None.

    Mirrors the entries under ``technologies:`` (description /
    layer_properties / dbu) so viewers treat the interconnect method as a
    PDK-backed technology with its own provenance, instead of folding its
    identity into the interposer. None when the manifest is unavailable or
    no method declares the adapter (e.g. a hand-set custom adapter).
    """
    im = _import_interconnect_manifest()
    if im is None:
        return None
    vendor = None
    try:
        for mid in im.list_methods():
            method = im.get_method(mid)
            if method.get("adapter") == adapter:
                vendor = method.get("vendor")
                break
        else:
            return None
    except Exception:
        return None
    description = "Chiplet attachment"
    if vendor:
        description += f" ({vendor})"
    return {
        "description": description,
        "layer_properties": _INTERCONNECT_LYP_REF,
        "dbu": 0.001,
    }


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
        tech = _interconnect_technology_block(adapter)
        if tech:
            data["interconnect"]["technology"] = tech
        else:
            # Derived data we could not derive. The merge layer treats
            # interconnect: as exporter-owned, so no older copy is carried
            # forward any more and the document would otherwise go out quietly
            # poorer: a consumer needing layer_properties to render the
            # interconnect layers finds nothing to look at. Whatever this
            # pipeline already put there is left alone; the point is that the
            # gap is never silent. The two causes read very differently to a
            # user, so name both rather than guessing which one happened.
            print(
                "  Warning: no technology metadata for interconnect adapter "
                "'%s'. Either the interconnect PDK manifest is not readable "
                "from here, or no method in it declares that adapter. The "
                ".chiplet keeps the adapter but gains no "
                "interconnect.technology block." % adapter
            )
    return newly_set


def _find_interposer_pdk_python():
    """Locate the interposer PDK's python tooling dir (bump_mirror.py).

    Ecosystem discovery convention (same shape as ADK_ROOT /
    INTERCONNECT_PDK_ROOT): explicit root via $INTERPOSER_PDK_ROOT first,
    then an upward walk from this file for a sibling interposer/ checkout.
    Returns the directory as a Path, or None.
    """
    env = os.environ.get("INTERPOSER_PDK_ROOT")
    if env:
        cand = Path(env) / "libs.tech" / "klayout" / "python"
        if (cand / "bump_mirror.py").is_file():
            return cand
    here = Path(__file__).resolve()
    for base in here.parents:
        for dirname in _PATH_VAR_MARKERS["INTERPOSER_PDK_ROOT"][0]:
            cand = base / dirname / "libs.tech" / "klayout" / "python"
            if (cand / "bump_mirror.py").is_file():
                return cand
    return None


def _import_bump_mirror():
    """Import bump_mirror (Cu-pillar geometry + DRC + auto-resolve).

    Resolved via _find_interposer_pdk_python(). Returns the module, or
    None when the interposer PDK is not reachable; callers that REQUIRE
    pillars must treat None as a hard error, never as a soft skip.
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


def _import_fill_closure():
    """Import the interposer PDK's metal-fill engine (fill_closure).

    Resolved via _find_interposer_pdk_python() (same INTERPOSER_PDK_ROOT
    discovery as bump_mirror). Returns the module, or None when the PDK is not
    reachable OR the checkout predates the fill engine. A caller that was asked
    to fill MUST treat None as a hard error, never a soft skip.
    """
    try:
        python_dir = _find_interposer_pdk_python()
        if python_dir is None:
            print("Warning: interposer PDK not found (set INTERPOSER_PDK_ROOT "
                  "or keep the sibling checkout).", file=sys.stderr)
            return None
        has_fc = (python_dir / "fill_closure.py").is_file()
        has_fs = (python_dir / "fill_stack.py").is_file()
        if not (has_fc or has_fs):
            print("Warning: interposer PDK checkout has no fill engine "
                  "(libs.tech/klayout/python/fill_closure.py); use a checkout "
                  "that includes the metal-fill work.", file=sys.stderr)
            return None
        if str(python_dir) not in sys.path:
            sys.path.insert(0, str(python_dir))
        # fill_stack may ship as its own module or as an entry point added to
        # fill_closure; accept either and return whichever exposes fill_stack.
        mod = None
        if has_fs:
            try:
                import fill_stack as mod
            except Exception:
                mod = None
        if mod is None or not hasattr(mod, "fill_stack"):
            import fill_closure as mod
        return mod
    except Exception as exc:
        print("Warning: could not import the fill engine: %s" % exc,
              file=sys.stderr)
        return None


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

    fill = db.Region()
    for (lnum, dt) in filler_layers:
        fill += reg(lnum, dt)
    fill.merge()

    prb = reg(*GDSGenerator.PRBOUNDARY_LAYER)
    bb = prb.bbox() if not prb.is_empty() else top.bbox()
    grid = []
    if bb.empty() or fill.is_empty():
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
                cov = (fill & db.Region(cell)).area()
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

    with tempfile.TemporaryDirectory() as td:
        report = fc.fill_stack(gds_path, gds_path, topcell=topcell, mode=mode,
                               workdir=td, log=log)

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
                       cu-pillar generator places DRC-validated pillars at
                       these pads (an alternative to a pre-generated
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

    Returns:
        True if conversion was successful
    """
    print(f"Converting: {hyp_path} -> {output_path}")

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
        # then the assembly-global --connection-type. A method without body
        # geometry in the interconnect manifest gets no pillars, loudly.
        _die_conns = die_connections or {}
        device_methods = {}
        connections_requested = False
        for dev_ref in pad_locations:
            method = _die_conns.get(dev_ref, connection_type)
            if not method:
                continue
            connections_requested = True
            if _connection_to_body_diameter(method) is None:
                print(f"  Warning: connection '{method}' on {dev_ref} has "
                      f"no body geometry in the interconnect manifest; "
                      f"skipping its pillars", file=sys.stderr)
                continue
            device_methods[dev_ref] = method
        methods_in_use = sorted(set(device_methods.values()))
        bm = _import_bump_mirror() if methods_in_use else None
        if methods_in_use and bm is None:
            # A connection stack was requested: a GDS without its pillars
            # would look fabricable while missing the attachment structures,
            # and no downstream DRC can flag absent geometry. Fail loud.
            print(
                "ERROR: Cu-pillar generation requested (connection=%s) but "
                "bump_mirror is unavailable. Set INTERPOSER_PDK_ROOT to the "
                "interposer PDK checkout (libs.tech/klayout/python/"
                "bump_mirror.py) and retry. Refusing to emit a complete GDS "
                "without its pillars." % ", ".join(methods_in_use),
                file=sys.stderr)
            return False
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
                m_diameter = _connection_to_body_diameter(m)
                # A method's declared pitch_rules are authoritative over the
                # body-diameter table lookup. from_body_diameter maps an IHP
                # Table 6.1 body diameter (35/44/49/54) to that row's
                # pitch/spacing and returns the Option-2 defaults (pitch 80 /
                # spacing 40) for any other diameter -- so a vendor fine-pitch
                # method (vendorx, body 40 um) or a solder bump (body 80 um) was
                # checked against 80/40 instead of its real manifest spec. That
                # let auto-resolve spread vendorx's native 70 um bumps toward 80
                # (landing ~72 nm short -> a phantom cu-pillar DRC error) while
                # the assembly DRC, which reads the manifest, passed the same
                # geometry at the real 50 um pitch. Honor the manifest's
                # pitch_rules when the method declares them; keep the
                # body-diameter fab geometry (diameter, enclosure). In-table
                # methods declare the same numbers, so this is a no-op for them.
                #
                # For an out-of-table diameter take the Option-2 fallback fields
                # directly rather than via from_body_diameter(), so its "using
                # Option 2 defaults" stderr warning does not fire -- that message
                # is misleading once the manifest pitch_rules below supersede
                # those defaults. An accurate note is emitted afterwards.
                m_in_table = bm.CUPILLAR_TABLE_6_1.get(m_diameter) is not None
                m_params = (bm.DrcParams.from_body_diameter(m_diameter)
                            if m_in_table else bm.DrcParams())
                try:
                    m_fab = im.fab_params(m)
                except KeyError:
                    m_fab = {}
                try:
                    m_pr = im.pitch_rules(m)
                except KeyError:
                    m_pr = {}
                m_overrode = False
                if m_pr.get("IXN_pitch") is not None:
                    m_params.min_pitch_um = m_pr["IXN_pitch"]
                    m_overrode = True
                if m_pr.get("IXN_spacing") is not None:
                    m_params.min_spacing_um = m_pr["IXN_spacing"]
                    m_overrode = True
                # The pad size the pre-DRC reasons about has to be the pad size
                # that gets drawn, and the drawn one comes from fab_params
                # (CuPillarGenerator below is handed the same number). Without
                # this, an out-of-table method kept the Option-2 fallback of 40
                # um while drawing a 35 um opening, and auto_resolve, which
                # targets max(pitch, diameter + spacing), pushed for 55 um where
                # the method asks for 50. In-table methods declare the table's
                # own opening, so this is a no-op for them.
                if m_fab.get("passiv_opening_um") is not None:
                    m_params.diameter_um = m_fab["passiv_opening_um"]
                if not m_in_table:
                    if m_overrode:
                        print(f"  Note: {m} body diameter {m_diameter} um is "
                              f"outside IHP Table 6.1; using its manifest "
                              f"pitch/spacing ({m_params.min_pitch_um}/"
                              f"{m_params.min_spacing_um} um).")
                    else:
                        print(f"  Warning: {m} body diameter {m_diameter} um is "
                              f"outside IHP Table 6.1 and declares no "
                              f"pitch_rules; using Option 2 defaults "
                              f"({m_params.min_pitch_um}/"
                              f"{m_params.min_spacing_um} um).",
                              file=sys.stderr)
                m_bodies = im.layers_3d(m)
                # Fab pad geometry travels with the method too (m_fab above):
                # diameters outside the IHP Table 6.1 (vendor methods) draw
                # their manifest-declared passivation opening.
                print(f"\nGenerating Cu-pillars (connection={m}, "
                      f"body diameter={m_diameter} um, "
                      f"pitch {m_params.min_pitch_um} um / spacing "
                      f"{m_params.min_spacing_um} um) with DRC validation...")
                print("  3D bodies: " + ", ".join(
                    f"{name} ({lnum}/{ldt})" for name, lnum, ldt in m_bodies))
                per_method[m] = (
                    bm.CuPillarGenerator(
                        enclosure_um=m_params.min_enclosure_um,
                        bodies=m_bodies,
                        passiv_opening_um=m_fab.get("passiv_opening_um")),
                    m_params, m_diameter)
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
                    print(f"  Warning: {dev_ref} has residual Cu-pillar DRC "
                          f"violations (continuing per policy).")
                dev_report = report.to_dict()
                dev_report["connection"] = method
                device_reports[dev_ref] = dev_report
                total_pillars += pillar_gen.add_device_bumps(
                    dev_ref, resolved, body_diameter)
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
            # Merge generated CUPILLARS_<ref> cells into the interposer top.
            # Each device lives in exactly one method's generator.
            merged = 0
            for m in methods_in_use:
                m_layout = per_method[m][0].layout
                # Iterate valid cells only: a generator that instantiates a
                # PCell and prunes the leftover proxy (as the IntM4TM2
                # CuPillarPad generator does) leaves freed slots in the cell
                # index space, so range(cells()) + cell(ci) would raise
                # "Not a valid cell index". each_cell() skips the gaps.
                for src in m_layout.each_cell():
                    if src.name.startswith("CUPILLARS_"):
                        new_cell = generator.layout.create_cell(src.name)
                        new_cell.copy_tree(src)
                        generator.top_cell.insert(
                            db.DCellInstArray(new_cell, db.DTrans()))
                        merged += 1
            print(f"Total Cu-pillar pads placed: {total_pillars} "
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

    # Write interposer GDS (routing + cu-pillars, without chiplet dies)
    generator.write(output_path)
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
                           io_pads=placed_io_pads,
                           devices=parser.devices,
                           die_connections=die_connections,
                           die_thicknesses=die_thicknesses,
                           outline_bbox=outline_bbox,
                           to_um=generator._to_um,
                           to_um_y=generator._to_um_y):
            print(f"ERROR: failed to finalize chiplet file "
                  f"'{chiplet_file_path}'.", file=sys.stderr)
            return False

    # Generate complete GDS with chiplets if requested
    if with_chiplets and parser.devices:
        print(f"\nGenerating complete GDS with chiplets...")

        # Determine flip-chip dies. Primary signal: a die attached by a
        # body-bearing (cu-pillar) connection method -- resolved from the
        # in-scope per-die map, or the assembly default for dies without one --
        # so orientation does not depend on the .chiplet sidecar (which is
        # absent when --update-chiplet-file is not passed). Solder-bump-only
        # dies still need the sidecar; the normal emit_chiplet path carries it.
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
        for device in parser.devices:
            is_flip = device.ref in flip_chip_refs
            if generator.add_device(device, flip_chip=is_flip):
                device_success += 1
                if is_flip:
                    print(f"  {device.ref}: placed with mirror-X (flip-chip)")

        print(f"Successfully added {device_success}/{len(parser.devices)} devices")

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

  # Also update a .chiplet file with the interposer GDS path:
  %(prog)s interposer_template.hyp -o interposer.gds --update-chiplet-file design.chiplet

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
        help="Update .chiplet file with interposer GDS absolute path"
    )
    parser.add_argument(
        "--pad-locations",
        type=str,
        metavar="REF=FILE[,REF=FILE,...]",
        help="Pin list JSON files per device; the cu-pillar generator places "
             "DRC-validated pillars at these pads (an alternative to a "
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
        help="Connection stack type for chiplet file (e.g., cupillar_opt1, sbump_sac305). "
             "Injects connection_stacks section and sets connection on die components. "
             "Choices: %(choices)s"
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
        "--io-pads",
        type=str,
        metavar="JSON_FILE",
        help="Sidecar JSON with external I/O pad locations (produced by "
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

    # Default lyp: the interposer PDK's canonical copy (env/walk); no
    # plugin-local fallback, the .lyp belongs to the PDK.
    if args.lyp is None:
        args.lyp = _find_default_lyp()

    # Expand ${VAR} ecosystem-root references in every path argument
    # (env -> sibling-checkout walk -> loud failure). Plain absolute or
    # relative paths pass through untouched.
    for _attr in ("hyp_file", "output", "lyp", "tech_json",
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
    # kept for pre-migration GDS), the exchange0 (190/0), or a cu-pillar fab
    # layer would silently wipe real geometry (and break the "never aliases a
    # fab layer" contract).
    if args.annotate_boundaries:
        _fab_layers = set(GDSGenerator.CUPILLAR_FAB_LAYERS.values()) | {
            (235, 0), (189, 0), (190, 0)}
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
    )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
