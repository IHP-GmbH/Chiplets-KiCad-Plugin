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


# Fraction of trace elements on unmapped layers above which the conversion
# fails instead of writing a near-empty GDS (board copper named with KiCad
# defaults instead of the PDK metals is the classic cause). Below the
# threshold stray layers are tolerated with an aggregate warning.
UNMAPPED_FAIL_FRACTION = 0.5


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

    # Board outline layer: prBoundary.drawing in the interposer PDK layer
    # table (same convention as SG13G2). Drawn from the .hyp BOARD perimeter
    # (= KiCad Edge.Cuts); read back by update_chiplet_file to size the
    # interposer component from the fab outline instead of the drawn-copper
    # extent. A future assembly containment rule (chiplet inside interposer)
    # reads the same layer.
    PRBOUNDARY_LAYER = (189, 0)

    def add_board_outline(self, perimeter_segments: List[PerimeterSegment]) -> int:
        """Draw the board outline on prBoundary (189/0).

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

    def get_outline_bbox(self) -> Optional[Tuple[float, float, float, float]]:
        """Bbox of the drawn board outline (prBoundary 189/0), or None.

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

    # I/O pads (external interposer pads): wire-bond MVP; flipped_bump and
    # tsv_bump reserved for follow-up PRs.
    SUPPORTED_IO_CLASSES = {'wire_bond'}
    RESERVED_IO_CLASSES = {'flipped_bump', 'tsv_bump'}

    def _create_wire_bond_pad_cell(self, size_x_um: float,
                                    size_y_um: float) -> db.Cell:
        """Create a wire-bond I/O pad cell: single rectangle on TopMetal2.

        Passiv opening and dfpad recognition are deferred to the follow-up
        PR that introduces the I/O pad DRC rule deck.

        The layer is the shared TopMetal2 *fab* entry from CUPILLAR_FAB_LAYERS
        (134/0), deliberately NOT routed through the trace LayerMap: the I/O pad
        must land on the same fab layer as the cu-pillars, which is not
        necessarily the TopMetal2 *routing* layer the LYP maps for traces.
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

            try:
                x = float(p.get('x_um', 0.0))
                y = float(p.get('y_um', 0.0))
            except (TypeError, ValueError):
                print(f"  Warning: skipping pad {p.get('ref', '?')} with "
                      f"non-numeric position", file=sys.stderr)
                continue
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


def update_chiplet_file(chiplet_path: str, interposer_gds_path: str,
                        bbox: Tuple[float, float, float, float] = None,
                        connection_type: str = "",
                        interposer_thickness: float = 13.83,
                        io_pads: Optional[List[Dict]] = None,
                        devices: Optional[List['Device']] = None,
                        die_connections: Optional[Dict[str, str]] = None,
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
        die_connections: Per-die connection overrides {component id: method
                 id}. A die not listed keeps connection_type. The stacks of
                 every method in use are injected, and each die's z comes
                 from its own stack.
        outline_bbox: Optional (x_min, y_min, width, height) in micrometers
                 of the board outline (prBoundary 189/0, drawn from KiCad's
                 Edge.Cuts). When available, interposer dimensions come
                 from it -- the fab outline -- instead of the drawn-geometry
                 bbox; position keeps the full-bbox center (the
                 anchor: bbox_center mesh contract). When None and bbox is
                 computed from the GDS here, it is derived from layer
                 189/0 if present in the file.

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
        with open(chiplet_file, 'r') as f:
            data = yaml.safe_load(f)

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

                # Set interposer thickness from BEOL stack height
                component['dimensions']['thickness'] = interposer_thickness

                # Update width/height from bbox
                if bbox:
                    x_min, y_min, width, height = bbox

                    # Dimensions: the fab outline (KiCad Edge.Cuts ->
                    # prBoundary 189/0) when drawn; the drawn-geometry
                    # bbox otherwise (legacy GDS without an outline).
                    if outline_bbox:
                        dim_w, dim_h = outline_bbox[2], outline_bbox[3]
                        dim_src = "board outline, prBoundary 189/0"
                    else:
                        dim_w, dim_h = width, height
                        dim_src = "drawn-geometry bbox"
                    component['dimensions']['width'] = dim_w
                    component['dimensions']['height'] = dim_h

                    # Per chiplet-studio/docs/coord_frame_contract.md
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

                    print(f"Updated interposer: layout={layout_ref}")
                    print(f"  dimensions: {dim_w:.2f} x {dim_h:.2f} um "
                          f"({dim_src}), thickness={interposer_thickness} um")
                    print(f"  position: ({width/2.0:.2f}, {height/2.0:.2f}) um "
                          f"(bbox center, canonical GDS-bbox-corner frame)")
                    print(f"  anchor: bbox_center")
                else:
                    print(f"Updated interposer layout path to: {layout_ref}")
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

    Prefers the interposer PDK's canonical
    ``libs.tech/klayout/tech/intm4tm2.lyp`` (env/walk discovery), falling
    back to the copy bundled with the plugin so a standalone install
    keeps working without the PDK checkout.
    """
    python_dir = _find_interposer_pdk_python()
    if python_dir is not None:
        cand = python_dir.parent / "tech" / "intm4tm2.lyp"
        if cand.is_file():
            return str(cand)
    return str(Path(__file__).parent / "intm4tm2.lyp")


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
    die_connections: Optional[Dict[str, str]] = None,
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
        die_connections: Per-die connection overrides {ref: method id}. A die
                         not listed uses connection_type. Drives both the 3D
                         bodies drawn under that die (its method's layers and
                         diameter) and its connection field in the .chiplet.

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
    elif pad_locations and parser.devices:
        # Resolve each device's connection method: per-die override first,
        # then the assembly-global --connection-type. A method without body
        # geometry in the interconnect manifest gets no pillars, loudly.
        _die_conns = die_connections or {}
        device_methods = {}
        for dev_ref in pad_locations:
            method = _die_conns.get(dev_ref, connection_type)
            if not method:
                continue
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
        elif methods_in_use:
            # One generator + parameter set per method: the 3D body layers
            # (e.g. a vendor's 510/511 vs IHP's 500/501) and the fab
            # parameters travel with the method, not with the assembly.
            # _connection_to_body_diameter above already proved the manifest
            # resolves for every method in use.
            im = _import_interconnect_manifest()
            per_method = {}
            for m in methods_in_use:
                m_diameter = _connection_to_body_diameter(m)
                m_params = bm.DrcParams.from_body_diameter(m_diameter)
                m_bodies = im.layers_3d(m)
                # Fab pad geometry travels with the method too: diameters
                # outside the IHP Table 6.1 (vendor methods) draw their
                # manifest-declared passivation opening.
                try:
                    m_fab = im.fab_params(m)
                except KeyError:
                    m_fab = {}
                print(f"\nGenerating Cu-pillars (connection={m}, "
                      f"body diameter={m_diameter} um) with DRC validation...")
                print("  3D bodies: " + ", ".join(
                    f"{name} ({lnum}/{ldt})" for name, lnum, ldt in m_bodies))
                per_method[m] = (
                    bm.CuPillarGenerator(
                        enclosure_um=m_params.min_enclosure_um,
                        bodies=m_bodies,
                        passiv_opening_um=m_fab.get("passiv_opening_um")),
                    m_params, m_diameter)
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
            # Merge generated CUPILLARS_<ref> cells into the interposer top.
            # Each device lives in exactly one method's generator.
            merged = 0
            for m in methods_in_use:
                m_layout = per_method[m][0].layout
                for ci in range(m_layout.cells()):
                    src = m_layout.cell(ci)
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

    # Write interposer GDS (routing + cu-pillars, without chiplet dies)
    generator.write(output_path)
    print(f"Interposer GDS file written to: {output_path}")

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
                with open(chiplet_file_path) as f:
                    chiplet_data = yaml.safe_load(f) or {}
                for comp in chiplet_data.get('components', []):
                    if comp.get('connection') or comp.get('orientation') == 'flip_chip':
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
             "PDK's canonical intm4tm2.lyp via env/walk discovery, falling "
             "back to the bundled copy)"
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

    # Default lyp: canonical interposer PDK copy, bundled fallback.
    if args.lyp is None:
        args.lyp = _find_default_lyp()

    # Expand ${VAR} ecosystem-root references in every path argument
    # (env -> sibling-checkout walk -> loud failure). Plain absolute or
    # relative paths pass through untouched.
    for _attr in ("hyp_file", "output", "lyp", "tech_json",
                  "complete_output", "update_chiplet_file",
                  "cupillar_gds", "io_pads"):
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

    # Parse the annotation layer "LAYER/DATATYPE" (only used if --annotate-boundaries)
    try:
        _vl, _vd = args.boundary_viz_layer.split('/', 1)
        boundary_viz_layer = (int(_vl), int(_vd))
    except (ValueError, AttributeError):
        print(f"Error: Invalid --boundary-viz-layer '{args.boundary_viz_layer}'. "
              "Use LAYER/DATATYPE, e.g. 1000/0.", file=sys.stderr)
        return 1

    # Refuse an annotation layer that collides with a fabrication layer: the
    # painter clear()s the layer first, so aliasing prBoundary (189/0), the
    # legacy exchange0 (190/0), or a cu-pillar fab layer would silently wipe
    # real geometry (and break the "never aliases a fab layer" contract).
    if args.annotate_boundaries:
        _fab_layers = set(GDSGenerator.CUPILLAR_FAB_LAYERS.values()) | {
            (189, 0), (190, 0)}
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
    )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
