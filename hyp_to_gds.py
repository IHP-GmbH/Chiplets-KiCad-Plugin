#!/usr/bin/env python3
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
import sys
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

    def parse(self) -> None:
        """Parse the HYP file and extract all geometry."""
        try:
            with open(self.hyp_path, 'r') as f:
                content = f.read()
        except FileNotFoundError:
            print(f"Error: Could not find {self.hyp_path}", file=sys.stderr)
            sys.exit(1)

        self._parse_units(content)
        self._parse_stackup(content)
        self._parse_padstacks(content)
        self._parse_devices(content)
        self._parse_segments_and_vias(content)
        # Device positions now come directly from HYP file (no centroid calculation needed)

    def _parse_units(self, content: str) -> None:
        """Parse units declaration."""
        if "UNITS=ENGLISH" in content:
            self.units = "ENGLISH"  # inches
        elif "UNITS=METRIC" in content:
            self.units = "METRIC"  # mm

    def _parse_stackup(self, content: str) -> None:
        """Parse STACKUP section to get layer order (top to bottom)."""
        stackup_match = re.search(r'\{STACKUP(.*?)\}', content, re.DOTALL)
        if stackup_match:
            for line in stackup_match.group(1).split('\n'):
                if 'SIGNAL' in line:
                    layer_match = re.search(r'L="([^"]+)"', line)
                    if layer_match:
                        self.stackup_layers.append(layer_match.group(1))

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

    def _parse_devices(self, content: str) -> None:
        """Parse DEVICES section for GDS_FILE entries with position."""
        for match in self.DEVICE_PATTERN.finditer(content):
            device = Device(
                ref=match.group(1),
                layer=match.group(2),
                x=float(match.group(3)),       # Position X from HYP
                y=float(match.group(4)),       # Position Y from HYP
                rotation=float(match.group(5)),  # Rotation in degrees
                gds_file=match.group(6)
            )
            self.devices.append(device)


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

    def __init__(self, layer_map: LayerMap, cell_name: str = "INTERPOSER", units: str = "ENGLISH",
                 stackup_order: List[str] = None, tech_json_path: Optional[str] = None,
                 annotate_boundaries: bool = False,
                 boundary_viz_layer: Tuple[int, int] = (1000, 0)):
        self.layer_map = layer_map
        self.units = units
        self.stackup_order = stackup_order or []  # Layer order from HYP STACKUP (top to bottom)
        self.PDK_VIA_PARAMS = self._load_via_params(tech_json_path)
        self.layout = db.Layout()
        self.layout.dbu = 0.001  # 1 DBU = 1 nm (0.001 um) - Changed from 0.0001 to match standard GDS
        self.top_cell = self.layout.create_cell(cell_name)
        self.routing_cell = self.layout.create_cell(f"{cell_name}_ROUTING")
        self.top_cell.insert(db.DCellInstArray(self.routing_cell, db.DTrans()))
        self._gds_layers: Dict[str, int] = {}  # Cache for layer indices
        self._via_cells: Dict[str, db.Cell] = {}  # Cache for via PCell instances
        self._via_group_cells: Dict[str, db.Cell] = {}  # metal_pair -> group cell
        self._boundary_records: List[dict] = []  # chiplet boundaries -> manifest
        # Opt-in, viewer-only annotation of the boundaries. No DRC rule reads
        # boundary_viz_layer; the contract lives in the manifest, not the GDS.
        self._annotate_boundaries = bool(annotate_boundaries)
        self._boundary_viz_layer = boundary_viz_layer
        self._pcells_available = self._check_pcells_available()

    def _check_pcells_available(self) -> bool:
        """Check if PDK PCells are available (running with klayout -zz -r)."""
        try:
            # Try to create a via_stack PCell - this will only work if
            # KLAYOUT_PATH is set to include the PDK
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

        # Determine arc direction (shortest path)
        diff = angle2 - angle1
        if diff > math.pi:
            diff -= 2 * math.pi
        elif diff < -math.pi:
            diff += 2 * math.pi

        # Generate points along the arc
        points = []
        for i in range(num_points + 1):
            t = i / num_points
            angle = angle1 + t * diff
            px = xc + radius * math.cos(angle)
            py = yc + radius * math.sin(angle)
            points.append((px, py))

        return points

    def _build_trace_elements(self, segments: List[TraceSegment], arcs: List[TraceArc]) -> Dict[Tuple[str, float], List]:
        """
        Group segments and arcs by (layer, width).
        Returns dict mapping (layer, width) -> list of (type, element) tuples.
        """
        groups: Dict[Tuple[str, float], List] = {}

        for seg in segments:
            key = (seg.layer, seg.width)
            groups.setdefault(key, []).append(('seg', seg))

        for arc in arcs:
            key = (arc.layer, arc.width)
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

    def add_path(self, points: List[Tuple[float, float]], width_um: float, layer: str) -> bool:
        """
        Add a path using DPath for smooth corners.
        Points are in micrometers, width is in micrometers.
        """
        if len(points) < 2:
            return False

        try:
            dpoints = [db.DPoint(x, y) for x, y in points]
            path = db.DPath(dpoints, width_um)
            layer_idx = self._get_gds_layer(layer)
            self.routing_cell.shapes(layer_idx).insert(path)
            return True
        except KeyError as e:
            print(f"Warning: {e} - skipping path on layer {layer}")
            return False

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
        for (layer, width), elements in groups.items():
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

    def add_segment(self, segment: TraceSegment) -> bool:
        """
        Convert a trace segment to a polygon and add to layout.
        ...
        """
        # Debug: Print first segment coordinates
        if not hasattr(self, "_debug_first_seg"):
            print(f"DEBUG: First Segment Processing")
            print(f"  Raw HYP: ({segment.x1}, {segment.y1}) to ({segment.x2}, {segment.y2})")
            print(f"  Conv UM: ({self._to_um(segment.x1):.2f}, {self._to_um_y(segment.y1):.2f})")
            self._debug_first_seg = True

        # Convert to micrometers (negate Y to mirror from HYP to GDS)
        x1 = self._to_um(segment.x1)
        y1 = self._to_um_y(segment.y1)
        x2 = self._to_um(segment.x2)
        y2 = self._to_um_y(segment.y2)
        half_width = self._to_um(segment.width) / 2

        # Calculate direction vector
        dx = x2 - x1
        dy = y2 - y1
        length = math.sqrt(dx * dx + dy * dy)

        if length < 1e-9:  # Degenerate segment (zero length)
            print(f"Warning: Skipping zero-length segment in {segment.net_name}")
            return False

        # Perpendicular unit vector
        px = -dy / length
        py = dx / length

        # Create 4 corners of the polygon
        points = [
            db.DPoint(x1 + px * half_width, y1 + py * half_width),
            db.DPoint(x1 - px * half_width, y1 - py * half_width),
            db.DPoint(x2 - px * half_width, y2 - py * half_width),
            db.DPoint(x2 + px * half_width, y2 + py * half_width),
        ]

        try:
            polygon = db.DPolygon(points)
            layer_idx = self._get_gds_layer(segment.layer)
            self.routing_cell.shapes(layer_idx).insert(polygon)
            return True
        except KeyError as e:
            print(f"Warning: {e} - skipping segment on layer {segment.layer}")
            return False

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
            return (self._via_cells[cache_key], group_cell)

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
            self._via_cells[cache_key] = via_cell
            print(f"  Created via_stack: {b_layer}->{t_layer}, "
                  f"target={target_size_um:.1f}µm, "
                  f"vn={vn_count}x{vn_count}, tv1={tv1_count}x{tv1_count}, tv2={tv2_count}x{tv2_count}")
            group_cell = self._get_or_create_via_group(sorted_layers)
            return (via_cell, group_cell)
        except Exception as e:
            print(f"Warning: Could not create via PCell: {e}")
            return None

    def _create_simple_via(self, via: Via, padstack: Padstack) -> bool:
        """Create a simple via using rectangles (fallback when PCells not available)."""
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

        # Standard via size (from PDK: typically 0.45um for Via4, larger for TopVias)
        via_sizes = {
            'Via4': 0.45,
            'TopVia1': 1.2,
            'TopVia2': 2.0,
        }

        # Metal enclosure around via
        metal_enc = 0.5  # um

        # Get via layers needed using sorted layers
        t_layer = sorted_layers[0]   # Top layer
        b_layer = sorted_layers[-1]  # Bottom layer
        via_layer_names = self._get_via_layers_between(t_layer, b_layer)

        # Create metal pads on all valid metal layers in the stack
        for metal_layer in valid_layers:
            try:
                # Determine via size based on adjacent via
                size = 2.0  # Default size in um
                for vl in via_layer_names:
                    if vl in via_sizes:
                        size = max(size, via_sizes[vl] + 2 * metal_enc)

                half_size = size / 2
                layer_idx = self._get_gds_layer(metal_layer)
                box = db.DBox(x_um - half_size, y_um - half_size,
                              x_um + half_size, y_um + half_size)
                self.routing_cell.shapes(layer_idx).insert(box)
            except KeyError:
                pass  # Skip if layer not found

        # Create via rectangles
        for via_layer in via_layer_names:
            try:
                size = via_sizes.get(via_layer, 0.45)
                half_size = size / 2
                layer_idx = self._get_gds_layer(via_layer)
                box = db.DBox(x_um - half_size, y_um - half_size,
                              x_um + half_size, y_um + half_size)
                self.routing_cell.shapes(layer_idx).insert(box)
            except KeyError:
                pass  # Skip if layer not found

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

        gds_path = Path(device.gds_file)
        if not gds_path.exists():
            print(f"Warning: GDS file not found: {device.gds_file}")
            return False

        try:
            # Convert device position to micrometers
            # HYP now exports Y consistently for both wires and devices
            # (KiCad exporter bug fixed: Y is negated for all elements)
            x_um = self._to_um(device.x)
            y_um = self._to_um(device.y)

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

            if flip_chip:
                # Flip-chip: extract geometry per-layer with mirror-X transform.
                # This flattens the template hierarchy so EM/thermal tools see
                # correct per-layer geometry without instance-level mirroring.
                wrapper_name = f"{device.ref}_{expected_cell_name}_flipped"
                imported_cell = self._place_die_flipped(
                    template_cell, wrapper_name, rotation=device.rotation)
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

    # Cu-pillar pad layer definitions (layer_num, datatype)
    CUPILLAR_FAB_LAYERS = {
        'TopMetal2':      (134, 0),
        'Passiv:pillar':  (9, 35),
        'dfpad:pillar':   (41, 35),
        'Recog:pillar':   (99, 35),
    }

    # 3D visualization auxiliary layers (not fabrication)
    # Cu pillar body diameter is larger than passiv opening (Table 6.1)
    CUPILLAR_3D_LAYERS = {
        'CuPillar:pillar':  (500, 35),
        'SnAgCap:pillar':   (501, 35),
    }
    CUPILLAR_BODY_DIAMETER = 44.0  # um (Table 6.1 Option 1: 44 +/- 3)

    def _create_cupillar_cell(self, diameter_um: float = 35.0,
                               encl_um: float = 7.5,
                               num_points: int = 256) -> db.Cell:
        """Create a static cu-pillar pad cell with circle geometry.

        Generates fabrication layers (same as CuPillarPad pcell) plus
        3D auxiliary layers for visualization and simulation.

        Args:
            diameter_um: Passivation opening diameter in micrometers
            encl_um: TopMetal2 enclosure around opening in micrometers
            num_points: Number of polygon points for circle approximation

        Returns:
            KLayout Cell containing the cu-pillar pad geometry
        """
        radius = diameter_um / 2.0
        tm2_radius = radius + encl_um

        cell_name = f"CUPILLAR_{diameter_um:.0f}um"
        cell = self.layout.create_cell(cell_name)

        # Fabrication layers
        for layer_name, (layer_num, datatype) in self.CUPILLAR_FAB_LAYERS.items():
            layer_idx = self.layout.layer(layer_num, datatype)
            r = radius if layer_name == 'Passiv:pillar' else tm2_radius

            points = []
            for i in range(num_points):
                angle = 2 * math.pi * i / num_points
                x = r * math.cos(angle)
                y = r * math.sin(angle)
                points.append(db.DPoint(x, y))
            poly = db.DPolygon(points)
            cell.shapes(layer_idx).insert(poly)

        # 3D auxiliary layers (Cu pillar body + SnAg cap, same XY footprint).
        # Owned by the interconnect PDK; delegate to its generator when present,
        # otherwise fall back to the built-in IHP layers (0-regression).
        body_radius = self.CUPILLAR_BODY_DIAMETER / 2.0
        bump3d = _import_bump3d()
        if bump3d is not None:
            bump3d.add_3d_bodies(self.layout, cell, body_radius,
                                 num_points=num_points)
        else:
            for layer_name, (layer_num, datatype) in self.CUPILLAR_3D_LAYERS.items():
                layer_idx = self.layout.layer(layer_num, datatype)
                points = []
                for i in range(num_points):
                    angle = 2 * math.pi * i / num_points
                    x = body_radius * math.cos(angle)
                    y = body_radius * math.sin(angle)
                    points.append(db.DPoint(x, y))
                poly = db.DPolygon(points)
                cell.shapes(layer_idx).insert(poly)

        return cell

    def _get_or_create_cupillar_cell(self, diameter_um: float = 35.0,
                                      encl_um: float = 7.5) -> db.Cell:
        """Get or create a cached cu-pillar pad cell."""
        if not hasattr(self, '_cupillar_cells'):
            self._cupillar_cells: Dict[str, db.Cell] = {}

        cache_key = f"{diameter_um}_{encl_um}"
        if cache_key not in self._cupillar_cells:
            cell = self._create_cupillar_cell(diameter_um, encl_um)
            self._cupillar_cells[cache_key] = cell
            print(f"  Created cu-pillar cell: {diameter_um:.0f}um diameter, "
                  f"{encl_um:.1f}um TM2 enclosure")
        return self._cupillar_cells[cache_key]

    def add_cupillar_pads(self, device_ref: str, pad_locations_json: str,
                          device_x_um: float, device_y_um: float,
                          device_rotation: float = 0.0,
                          diameter_um: float = 35.0,
                          encl_um: float = 7.5) -> int:
        """Add cu-pillar pads at chiplet pad locations.

        Reads pad coordinates from a pin_list JSON file and instantiates
        cu-pillar pad cells at each location, transformed to interposer
        global coordinates.

        Args:
            device_ref: Device reference (e.g., "U1")
            pad_locations_json: Path to pin_list JSON file
            device_x_um: Device placement X in micrometers (interposer coords)
            device_y_um: Device placement Y in micrometers (interposer coords)
            device_rotation: Device rotation in degrees
            diameter_um: Cu-pillar opening diameter
            encl_um: TopMetal2 enclosure

        Returns:
            Number of cu-pillar pads placed
        """
        pad_path = Path(pad_locations_json)
        if not pad_path.exists():
            print(f"Warning: Pad locations file not found: {pad_locations_json}")
            return 0

        with open(pad_path, 'r') as f:
            data = json.load(f)

        pins = data.get('pins', [])
        if not pins:
            print(f"Warning: No pins found in {pad_locations_json}")
            return 0

        # Get or create the cu-pillar cell template
        cupillar_cell = self._get_or_create_cupillar_cell(diameter_um, encl_um)

        # Create a group cell for this device's cu-pillars
        group_cell = self.layout.create_cell(f"CUPILLARS_{device_ref}")
        self.top_cell.insert(db.DCellInstArray(group_cell, db.DTrans()))

        # DBU to um conversion: pin_list coordinates are in database units
        # For IHP SG13G2: 1 DBU = 1 nm, so dbu_to_um = 0.001
        dbu_to_um = 0.001

        count = 0
        for pin in pins:
            # Convert pad center from chiplet-local DBU to micrometers
            pad_x_um = pin.get('center_x_dbu', 0.0) * dbu_to_um
            pad_y_um = pin.get('center_y_dbu', 0.0) * dbu_to_um

            # Transform from chiplet-local to interposer-global coordinates
            if device_rotation != 0.0:
                angle_rad = math.radians(device_rotation)
                cos_a = math.cos(angle_rad)
                sin_a = math.sin(angle_rad)
                global_x = device_x_um + pad_x_um * cos_a - pad_y_um * sin_a
                global_y = device_y_um + pad_x_um * sin_a + pad_y_um * cos_a
            else:
                global_x = device_x_um + pad_x_um
                global_y = device_y_um + pad_y_um

            trans = db.DTrans(db.DVector(global_x, global_y))
            group_cell.insert(db.DCellInstArray(cupillar_cell, trans))
            count += 1

        print(f"  Placed {count} cu-pillar pads for {device_ref}")
        return count

    # I/O pads (external interposer pads): wire-bond MVP; flipped_bump and
    # tsv_bump reserved for follow-up PRs.
    SUPPORTED_IO_CLASSES = {'wire_bond'}
    RESERVED_IO_CLASSES = {'flipped_bump', 'tsv_bump'}

    def _create_wire_bond_pad_cell(self, size_x_um: float,
                                    size_y_um: float) -> db.Cell:
        """Create a wire-bond I/O pad cell: single rectangle on TopMetal2.

        Passiv opening and dfpad recognition are deferred to the follow-up
        PR that introduces the I/O pad DRC rule deck.
        """
        cell_name = f"WB_PAD_{size_x_um:g}x{size_y_um:g}"
        cell = self.layout.create_cell(cell_name)
        layer_num, layer_dt = self.CUPILLAR_FAB_LAYERS['TopMetal2']
        layer_idx = self.layout.layer(layer_num, layer_dt)
        half_x = size_x_um / 2.0
        half_y = size_y_um / 2.0
        cell.shapes(layer_idx).insert(
            db.DBox(-half_x, -half_y, half_x, half_y))
        return cell

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

        with open(pads_path, 'r') as f:
            data = json.load(f)

        pads = data.get('io_pads', [])
        if not pads:
            print(f"  No io_pads found in {io_pads_json}")
            return []

        group_cells: Dict[str, db.Cell] = {}
        placed: List[Dict] = []
        counts: Dict[str, int] = {}

        for p in pads:
            io_class = p.get('io_class', 'wire_bond')
            sx = float(p.get('size_x_um', 0.0))
            sy = float(p.get('size_y_um', 0.0))
            if sx <= 0 or sy <= 0:
                print(f"  Warning: skipping pad {p.get('ref', '?')} "
                      f"with invalid size: {sx}x{sy}", file=sys.stderr)
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

            x = float(p.get('x_um', 0.0))
            y = float(p.get('y_um', 0.0))
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

    def write(self, output_path: str) -> None:
        """Write layout preserving via cell hierarchy.

        PCell variants are resolved to static geometry within their own cells.
        Via instances are grouped by metal pair for a clean hierarchy.
        Context info is stripped so the GDS opens cleanly without PDK dependencies.
        A <stem>.boundaries.json manifest with the chiplet boundaries is written
        alongside the GDS for the ADK assembly DRC.
        """
        for via_cell in self._via_cells.values():
            via_cell.flatten(-1, True)

        save_opts = db.SaveLayoutOptions()
        save_opts.write_context_info = False
        self._paint_boundary_annotations()
        self.layout.write(output_path, save_opts)
        self._write_boundary_manifest(output_path)


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
    lib = im.get_connection_library()
    return {
        mid: {
            "description": stack["description"],
            "layers": [dict(layer) for layer in stack["layers"]],
        }
        for mid, stack in lib.items()
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


def update_chiplet_file(chiplet_path: str, interposer_gds_path: str,
                        bbox: Tuple[float, float, float, float] = None,
                        connection_type: str = "",
                        interposer_thickness: float = 13.83,
                        io_pads: Optional[List[Dict]] = None,
                        devices: Optional[List['Device']] = None) -> bool:
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
        interposer_thickness: BEOL stack top height in micrometers.
                              For SG13G2: 13.83 (TopMetal2 top at 10.83 + 3.00).
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
            except Exception as e:
                print(f"Warning: Could not read interposer GDS bbox: {e}",
                      file=sys.stderr)

    try:
        with open(chiplet_file, 'r') as f:
            data = yaml.safe_load(f)

        # Find and update the interposer component
        updated = False
        for component in data.get('components', []):
            if component.get('id') == 'interposer':
                abs_path = str(Path(interposer_gds_path).resolve())
                component['layout'] = abs_path

                # Read top_cell from interposer GDS
                interposer_top_cell = _read_gds_top_cell(abs_path)
                if interposer_top_cell:
                    component['top_cell'] = interposer_top_cell

                if 'dimensions' not in component:
                    component['dimensions'] = {}

                # Set interposer thickness from BEOL stack height
                component['dimensions']['thickness'] = interposer_thickness

                # Update width/height from bbox
                if bbox:
                    x_min, y_min, width, height = bbox
                    component['dimensions']['width'] = width
                    component['dimensions']['height'] = height

                    # Per chiplet-studio/docs/coord_frame_contract.md
                    # section 1: position is the geometric center of
                    # the component in the canonical GDS-bbox-corner
                    # frame. The interposer's bbox center, expressed
                    # in its own bbox-corner frame, is (width/2,
                    # height/2).
                    if 'position' not in component:
                        component['position'] = {}
                    component['position']['x'] = width / 2.0
                    component['position']['y'] = height / 2.0

                    # Per coord_frame_contract.md section 2: the
                    # interposer mesh is centered on its own GDS bbox.
                    component['anchor'] = 'bbox_center'

                    print(f"Updated interposer: layout={abs_path}")
                    print(f"  dimensions: {width:.2f} x {height:.2f} um, "
                          f"thickness={interposer_thickness} um")
                    print(f"  position: ({width/2.0:.2f}, {height/2.0:.2f}) um "
                          f"(bbox center, canonical GDS-bbox-corner frame)")
                    print(f"  anchor: bbox_center")
                else:
                    print(f"Updated interposer layout path to: {abs_path}")
                    print(f"  thickness={interposer_thickness} um")

                if io_pads is not None:
                    # Per chiplet-studio/docs/coord_frame_contract.md
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

        # Inject connection_stacks and set connection on die components
        if connection_type:
            default_stacks = get_default_connection_stacks()
            if connection_type not in default_stacks:
                print(f"Warning: Unknown connection type '{connection_type}', "
                      f"available: {list(default_stacks.keys())}", file=sys.stderr)
            else:
                # Add connection_stacks section if not already present
                if 'connection_stacks' not in data:
                    data['connection_stacks'] = default_stacks
                    print(f"Injected connection_stacks ({len(default_stacks)} types)")
                elif connection_type not in data.get('connection_stacks', {}):
                    data['connection_stacks'][connection_type] = default_stacks[connection_type]

                # Set connection on each die component
                # Respect per-die orientation: only assign connection to flip_chip dies
                for component in data.get('components', []):
                    comp_type = component.get('type', '')
                    if comp_type == 'die':
                        orient = component.get('orientation', '')
                        if orient == 'flip_chip' or not orient:
                            component['connection'] = connection_type
                            print(f"  Set connection={connection_type} on {component.get('id')}")
                        else:
                            print(f"  Skipped {component.get('id')} (orientation={orient})")

        # Compute die z-values from connection_stacks
        connection_stacks = data.get('connection_stacks', {})
        for component in data.get('components', []):
            if component.get('type') != 'die':
                continue

            # Per chiplet-studio/docs/coord_frame_contract.md sections 2
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
                die_z = interposer_thickness + stack_height
                if 'position' not in component:
                    component['position'] = {}
                component['position']['z'] = die_z
                print(f"  {component.get('id')}: z = {interposer_thickness} + "
                      f"{stack_height} = {die_z} um (connection: {conn_id})")
            elif 'position' in component and component['position'].get('z', 0) == 0:
                # No connection stack -- default z to interposer_thickness
                component['position']['z'] = interposer_thickness

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
                    abs_x_um = dev.x * 1e6  # HYP is meters
                    abs_y_um = dev.y * 1e6
                    new_x = abs_x_um - gds_left
                    new_y = abs_y_um - gds_bottom
                    old = component.get('position', {})
                    print(f"  {ref}: re-anchored x/y "
                          f"({old.get('x', 0):.2f},{old.get('y', 0):.2f}) -> "
                          f"({new_x:.2f},{new_y:.2f}) [GDS bbox corner]")
                    component['position']['x'] = new_x
                    component['position']['y'] = new_y

            # Read top_cell from die GDS
            die_gds = component.get('layout', '')
            if die_gds:
                die_top_cell = _read_gds_top_cell(die_gds)
                if die_top_cell:
                    component['top_cell'] = die_top_cell

        # Strip the intermediate-frame marker emitted by KiCad's
        # exporter (see kicad/pcbnew/exporters/export_chiplet.cpp).
        # Per chiplet-studio/docs/coord_frame_contract.md section 4.1,
        # this finalize step converts to the canonical frame; the
        # canonical .chiplet has no _metadata block. Pop is no-op when
        # the input was already finalized (idempotent re-run).
        if data.pop('_metadata', None) is not None:
            print("  Stripped _metadata.finalize_required marker "
                  "(file is now canonical)")

        # Write back the updated file
        with open(chiplet_file, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        print(f"Chiplet file updated: {chiplet_path}")
        return True

    except Exception as e:
        print(f"Error updating chiplet file: {e}", file=sys.stderr)
        return False


def _read_gds_top_cell(gds_path: str) -> Optional[str]:
    """Read the top cell name from a GDS file.

    Returns the name of the top cell, or None if the file cannot be read.
    """
    try:
        gds_file = Path(gds_path)
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


def _import_interconnect_manifest():
    """Import the interconnect PDK manifest reader (sibling repo), or None.

    Located via $INTERCONNECT_PDK_ROOT or a sibling-repo search, mirroring
    _import_bump_mirror. The interconnect PDK owns the bump-method registry.
    """
    try:
        candidates = []
        env = os.environ.get("INTERCONNECT_PDK_ROOT")
        if env:
            candidates.append(Path(env) / "python")
        here = Path(__file__).resolve()
        for base in here.parents:
            candidates.append(base / "interconnect_pdk" / "python")
        for cand in candidates:
            if (cand / "interconnect_manifest.py").is_file():
                if str(cand) not in sys.path:
                    sys.path.insert(0, str(cand))
                import interconnect_manifest
                return interconnect_manifest
    except Exception:
        pass
    return None


def _import_bump3d():
    """Import the interconnect PDK 3D body generator (sibling repo), or None."""
    try:
        here = Path(__file__).resolve()
        for base in here.parents:
            cand = base / "interconnect_pdk" / "scripts"
            if (cand / "bump3d_generator.py").is_file():
                if str(cand) not in sys.path:
                    sys.path.insert(0, str(cand))
                import bump3d_generator
                return bump3d_generator
    except Exception:
        pass
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
    except KeyError:
        return None
    layers = method.get("connection_stack", {}).get("layers", [])
    if any("Ball" in layer.get("name", "") for layer in layers):
        return None
    return method.get("body_diameter_um")


def _import_bump_mirror():
    """Import bump_mirror (Cu-pillar geometry + DRC + auto-resolve).

    Located at <project_root>/interposer/scripts/bump_mirror.py relative to
    this file. Returns the module, or None if it cannot be imported so the
    caller degrades gracefully (warn + no pillars).
    """
    try:
        scripts_dir = (Path(__file__).resolve().parent.parent
                       / "interposer" / "scripts")
        if scripts_dir.is_dir() and str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import bump_mirror
        return bump_mirror
    except Exception as exc:
        print("Warning: could not import bump_mirror: %s" % exc,
              file=sys.stderr)
        return None


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
    annotate_boundaries: bool = False,
    boundary_viz_layer: Tuple[int, int] = (1000, 0),
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
        pad_locations: Dict mapping device ref to pin_list JSON path
                       (deprecated -- use cupillar_gds_path instead)
        connection_type: Connection stack ID for chiplet file update (e.g. "cupillar_opt1")
        cupillar_gds_path: Path to pre-generated cu-pillar GDS (from bump_mirror.py)
        annotate_boundaries: If True, also paint each chiplet boundary onto a
                             viewer-only annotation layer (no DRC rule reads it)
        boundary_viz_layer: (layer, datatype) for the annotation (default 1000/0)

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

    if not parser.segments and not parser.vias:
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
                             boundary_viz_layer=boundary_viz_layer)

    if generator._pcells_available:
        print("PDK PCells available - using via_stack PCell for vias")
    else:
        print("PDK PCells not available - using simple rectangles for vias")
        print("  (Run with 'klayout -zz -r' and KLAYOUT_PATH set for PCell support)")

    # Process segments and arcs as connected paths (smooth corners)
    num_paths = generator.add_segments_as_paths(parser.segments, parser.arcs)
    print(f"Created {num_paths} continuous paths from {len(parser.segments)} segments and {len(parser.arcs)} arcs")

    # Process vias
    via_success = 0
    for via in parser.vias:
        padstack = parser.padstacks.get(via.padstack_index)
        if padstack and generator.add_via(via, padstack):
            via_success += 1

    print(f"Successfully converted {via_success}/{len(parser.vias)} vias")

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
        for ci in range(cupillar_layout.cells()):
            src_cell = cupillar_layout.cell(ci)
            if src_cell.parent_cells() == 0:  # top-level cell(s)
                new_cell = generator.layout.create_cell(src_cell.name)
                new_cell.copy_tree(src_cell)
                generator.top_cell.insert(
                    db.DCellInstArray(new_cell, db.DTrans()))
                print(f"  Merged cu-pillar cell: {src_cell.name}")
    elif (pad_locations and parser.devices
          and _connection_to_body_diameter(connection_type) is not None):
        body_diameter = _connection_to_body_diameter(connection_type)
        bm = _import_bump_mirror()
        if bm is None:
            print("Warning: bump_mirror unavailable; skipping Cu-pillar "
                  "generation (pillars absent from GDS).", file=sys.stderr)
        else:
            print(f"\nGenerating Cu-pillars (connection={connection_type}, "
                  f"body diameter={body_diameter} um) with DRC validation...")
            device_map = {dev.ref: dev for dev in parser.devices}
            params = bm.DrcParams.from_body_diameter(body_diameter)
            pillar_gen = bm.CuPillarGenerator(
                enclosure_um=params.min_enclosure_um)
            total_pillars = 0
            device_reports = {}
            for dev_ref, pin_json in pad_locations.items():
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
                device_reports[dev_ref] = report.to_dict()
                total_pillars += pillar_gen.add_device_bumps(
                    dev_ref, resolved, body_diameter)
            # Merge generated CUPILLARS_<ref> cells into the interposer top.
            merged = 0
            for ci in range(pillar_gen.layout.cells()):
                src = pillar_gen.layout.cell(ci)
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
                doc = {
                    "version": 1,
                    "tool": "hyp_to_gds cu-pillar DRC",
                    "connection_type": connection_type,
                    "body_diameter_um": body_diameter,
                    "params": params.to_dict(),
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

    # Write interposer GDS (routing + cu-pillars, without chiplet dies)
    generator.write(output_path)
    print(f"Interposer GDS file written to: {output_path}")

    # Update chiplet file if requested
    if chiplet_file_path:
        bbox = generator.get_top_cell_bbox()
        update_chiplet_file(chiplet_file_path, output_path, bbox,
                           connection_type=connection_type,
                           io_pads=placed_io_pads,
                           devices=parser.devices)

    # Generate complete GDS with chiplets if requested
    if with_chiplets and parser.devices:
        print(f"\nGenerating complete GDS with chiplets...")

        # Determine flip-chip dies from chiplet file
        flip_chip_refs = set()
        if chiplet_file_path:
            try:
                import yaml
                with open(chiplet_file_path) as f:
                    chiplet_data = yaml.safe_load(f)
                for comp in chiplet_data.get('components', []):
                    if comp.get('connection') or comp.get('orientation') == 'flip_chip':
                        flip_chip_refs.add(comp.get('id', ''))
            except Exception:
                pass

        # Add devices to the layout
        device_success = 0
        for device in parser.devices:
            is_flip = device.ref in flip_chip_refs
            if generator.add_device(device, flip_chip=is_flip):
                device_success += 1
                if is_flip:
                    print(f"  {device.ref}: placed with mirror-X (flip-chip)")

        print(f"Successfully added {device_success}/{len(parser.devices)} devices")

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
        default=str(Path(__file__).parent / "interposer_ihp.lyp"),
        help="KLayout LYP layer properties file (default: interposer_ihp.lyp)"
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
        help="(Deprecated: use bump_mirror.py + --cupillar-gds instead) "
             "Pin list JSON files per device for cu-pillar pad generation "
             "(e.g., U1=pins_interposer.json,U2=pins_diffamp.json)"
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
        "--annotate-boundaries",
        action="store_true",
        help="Also paint each chiplet boundary (and instance label) onto a "
             "viewer-only annotation layer for eyeball inspection of the GDS. "
             "No DRC rule reads this layer; the assembly contract stays in the "
             "<gds>.boundaries.json manifest. Off by default."
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
            pad_locations[ref.strip()] = path.strip()

    # Parse the annotation layer "LAYER/DATATYPE" (only used if --annotate-boundaries)
    try:
        _vl, _vd = args.boundary_viz_layer.split('/', 1)
        boundary_viz_layer = (int(_vl), int(_vd))
    except (ValueError, AttributeError):
        print(f"Error: Invalid --boundary-viz-layer '{args.boundary_viz_layer}'. "
              "Use LAYER/DATATYPE, e.g. 1000/0.", file=sys.stderr)
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
    )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
