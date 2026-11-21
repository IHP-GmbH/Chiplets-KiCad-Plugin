# SPDX-License-Identifier: GPL-3.0-or-later
"""The board model the worker draws from: what HYPParser reads from a .hyp
and RoutedGeometryParser from a routed-geometry document. Standard library
only, so both parsers and the worker import it as a sibling."""

from dataclasses import dataclass
from typing import List


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
