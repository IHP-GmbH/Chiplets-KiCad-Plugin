# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared HYP/LYP interpretation, usable before importing native modules.

Path selection and layer lookup are the worker's existing rules. Marked
carrier exports use strict lookup to refuse ambiguous drawing definitions;
legacy callers retain the historical last-definition behavior.
"""

import os
from pathlib import Path
import re
from typing import Dict, Optional, Tuple
import xml.etree.ElementTree as ET


class LayerMapError(ValueError):
    """The selected layer source cannot establish an unambiguous mapping."""


class CarrierRootRefused(LayerMapError):
    """``$INTERPOSER_PDK_ROOT`` is set and is not an IntM4TM2 PDK tree.

    The upward walk used to stand in for such a root, so a run drew with the
    sibling IntM4TM2 checkout whatever the variable named. The message leads
    with ``CARRIER_UNSUPPORTED_ROOT``, of the id family packaging-flow and
    chiplet-spec refuse another carrier with, and names the marker looked for
    and the path given. Temporary: it goes when a carrier root is validated by
    its technology package.
    """

    def __init__(self, selected, marker):
        super().__init__(
            "CARRIER_UNSUPPORTED_ROOT: $INTERPOSER_PDK_ROOT is %s, which has no "
            "%s, the marker of an IntM4TM2 PDK tree; IntM4TM2 is the only "
            "carrier modelled until carriers are read from their technology "
            "package. Point the variable at an IntM4TM2 checkout, or "
            "unset it to use the sibling checkout." % (selected, marker))


class LayerMap:
    """Manages PDK layer definitions from KLayout LYP file."""

    def __init__(self, lyp_path: str, *, strict: bool = False):
        self.layers: Dict[str, Tuple[int, int]] = {}
        self._strict = strict
        self._conflicts = set()
        self._invalid = set()
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
                if name_elem is None or name_elem.text is None:
                    continue

                full_name = name_elem.text.strip()

                # Split "Name.purpose" exactly as the HYP consumer does.
                if '.' in full_name:
                    name, purpose = full_name.split('.', 1)
                else:
                    name, purpose = full_name, "drawing"
                key = f"{name}:{purpose}"

                if source_elem is None or source_elem.text is None:
                    self._invalid.add(key)
                    continue
                source = source_elem.text.strip()
                if self._strict and re.fullmatch(
                        r"[0-9]+(?:/[0-9]+)?(?:@[0-9]+)?", source) is None:
                    # Check the entire concrete source before the historical
                    # parser strips its cell-view suffix or splits on '/'.
                    # Invalid named duplicates stay invalid in either order.
                    self._invalid.add(key)
                    continue

                # Strip @N cell view suffix (e.g. "50/0@1" -> "50/0")
                if '@' in source:
                    source = source.split('@')[0]

                # Parse layer/datatype from source
                try:
                    parts = source.split('/')
                    layer_num = int(parts[0])
                    datatype = int(parts[1]) if len(parts) > 1 else 0
                except (ValueError, IndexError):
                    self._invalid.add(key)
                    continue

                # Store with "name:purpose" key
                value = (layer_num, datatype)
                if key in self.layers and self.layers[key] != value:
                    self._conflicts.add(key)
                self.layers[key] = value

                # Also store drawing layers with just name for convenience
                if purpose == "drawing":
                    self.layers[name] = (layer_num, datatype)

        except (OSError, ET.ParseError) as exc:
            raise LayerMapError(f"Cannot read LYP '{lyp_path}': {exc}") from exc

    def get_layer(self, name: str, purpose: str = "drawing") -> Tuple[int, int]:
        """Get GDS layer and datatype for a given layer name."""
        key = f"{name}:{purpose}"
        if self._strict and key in self._invalid:
            raise LayerMapError(f"Invalid LYP source for '{key}'")
        if self._strict and key in self._conflicts:
            raise LayerMapError(f"Conflicting LYP definitions for '{key}'")
        if key in self.layers:
            return self.layers[key]
        if name in self.layers:
            return self.layers[name]
        raise KeyError(f"Layer '{name}' with purpose '{purpose}' not found in PDK")

    def __repr__(self) -> str:
        return f"LayerMap({len(self.layers)} layers from LYP)"


PATH_VAR_MARKERS = {
    # The stack descriptor, not libs.tech/klayout/: every KLayout PDK tree has
    # that directory, the SG13G2 die PDK included, so it cannot tell an
    # IntM4TM2 tree from another.
    "INTERPOSER_PDK_ROOT": (("interposer", "OpenIntM4TM2"),
                            ("libs.tech", "intm4tm2_stack.json")),
    "GDS_TO_KICAD_ROOT": (("gds_to_kicad", "gds-to-kicad"), ("pdks",)),
    "ADK_ROOT": (("adk", "ADK"), ("klayout", "drc")),
    "INTERCONNECT_PDK_ROOT": (("interconnect_pdk",
                               "IHP-Interconnect-IntM4TM2"), ("manifest",)),
    # Base SG13G2 PDK (via_stack PCell library). Standard IHP convention:
    # $PDK_ROOT/ihp-sg13g2/...
    "PDK_ROOT": (("IHP-Open-PDK",), ("ihp-sg13g2", "libs.tech", "klayout")),
}

_PATH_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def discover_path_var(name: str, *, env=None, start=None) -> Optional[str]:
    """Resolve an ecosystem-root variable: env first, then sibling walk.

    A set-but-invalid environment value (marker subpath missing) falls
    through to the walk, except ``INTERPOSER_PDK_ROOT``, which is refused
    (:class:`CarrierRootRefused`).
    Returns the root as a string, or None when unresolvable.
    """
    marker = PATH_VAR_MARKERS.get(name)
    if marker is None:
        # Only the known ecosystem-root variables are expandable. An unknown
        # ${NAME} is a typo/misuse, not a licence to expand an arbitrary
        # environment variable; return None so expand_path_vars fails loudly.
        return None
    selected = (os.environ if env is None else env).get(name)
    if selected and Path(selected).joinpath(*marker[1]).exists():
        return selected
    if selected and name == "INTERPOSER_PDK_ROOT":
        raise CarrierRootRefused(selected, Path(*marker[1]).as_posix())
    dirnames, sub = marker
    here = Path(start or __file__).resolve()
    for base in here.parents:
        for dirname in dirnames:
            cand = base / dirname
            if cand.joinpath(*sub).exists():
                return str(cand)
    return None


def expand_path_vars(path: Optional[str], *, env=None, start=None,
                     discover=None) -> Optional[str]:
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
        value = (discover(name) if discover is not None else
                 discover_path_var(name, env=env, start=start))
        if value is None:
            raise LayerMapError(
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
        raise LayerMapError(
            "ERROR: malformed variable reference in path '%s'. Use ${NAME} "
            "with an ecosystem-root name (see adk/docs/integration.md)." % path)
    return result


def find_default_lyp(*, env=None, start=None) -> str:
    """Default layer-properties file for the interposer GDS.

    Resolves the interposer PDK's canonical
    ``libs.tech/klayout/tech/intm4tm2.lyp`` via env/walk discovery. The
    file belongs to the interposer PDK, not the plugin, so there is no
    vendored copy: when no checkout resolves, return the unexpanded
    ``${INTERPOSER_PDK_ROOT}`` form (mirrors _find_interposer_template) so
    the error text points somewhere actionable instead of silently using a
    stale bundled .lyp.
    """
    python_dir = find_interposer_pdk_python(env=env, start=start)
    if python_dir is not None:
        cand = python_dir.parent / "tech" / "intm4tm2.lyp"
        if cand.is_file():
            return str(cand)
    return "${INTERPOSER_PDK_ROOT}/libs.tech/klayout/tech/intm4tm2.lyp"


def find_interposer_pdk_python(*, env=None, start=None):
    """Locate the interposer PDK's python tooling dir (bump_mirror.py).

    The root is discover_path_var's: $INTERPOSER_PDK_ROOT first, refused
    when it is not an IntM4TM2 tree (:class:`CarrierRootRefused`), then the
    sibling walk. A root without bump_mirror.py is refused too.
    Returns the directory as a Path, or None when no root resolves.
    """
    root = discover_path_var("INTERPOSER_PDK_ROOT", env=env, start=start)
    if root is None:
        return None
    python_dir = Path(root) / "libs.tech" / "klayout" / "python"
    if not (python_dir / "bump_mirror.py").is_file():
        raise LayerMapError(
            "ERROR: the interposer PDK at %s is an IntM4TM2 tree without "
            "libs.tech/klayout/python/bump_mirror.py, the tooling this plugin "
            "draws pillars and pads with." % root)
    return python_dir

def resolve_lyp_path(lyp_path=None, *, env=None, start=None):
    """Select/expand the worker's LYP once and return its absolute path.

    `start` is the actual worker script so the existing sibling walk also
    works with installed/symlinked plugins. GUI picker discovery is separate.
    """
    selected = (find_default_lyp(env=env, start=start)
                if lyp_path is None else lyp_path)
    expanded = expand_path_vars(selected, env=env, start=start)
    return str(Path(expanded).absolute())
