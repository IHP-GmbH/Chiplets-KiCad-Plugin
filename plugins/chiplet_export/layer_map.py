# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared HYP/LYP interpretation, usable before importing native modules.

Path selection and layer lookup are the worker's existing rules. Marked
carrier exports use strict lookup to refuse ambiguous drawing definitions;
legacy callers retain the historical last-definition behavior.
"""

import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Dict, NamedTuple, Optional, Tuple
import xml.etree.ElementTree as ET


class LayerMapError(ValueError):
    """The selected layer source cannot establish an unambiguous mapping."""


class CarrierPackageRefused(LayerMapError):
    """The carrier's technology package cannot give what the export reads.

    The message leads with the id chiplet-spec's package validator gives the
    same fault (PACKAGE_MANIFEST_MISSING, PACKAGE_MEMBER_PATH, ...), so a log
    reader keys on one vocabulary whichever tool refused.
    """

    def __init__(self, message_id, text):
        super().__init__("%s: %s" % (message_id, text))
        self.message_id = message_id


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


#: A carrier technology package's manifest, relative to its root: the marker
#: of a carrier root and the one file that names the package's members
#: (chiplet-spec technology_package.schema.json).
PACKAGE_MANIFEST = ("libs.tech", "adk", "technology.json")
PACKAGE_SCHEMA = "technology-package"
#: The package major this export reads; any minor of it is read.
PACKAGE_MAJOR = 1
#: The one carrier the export draws (read_carrier_package).
CARRIER_TECHNOLOGY = "intm4tm2"

PATH_VAR_MARKERS = {
    # The technology package manifest, not libs.tech/klayout/: every KLayout
    # PDK tree has that directory, the SG13G2 die PDK included, so it cannot
    # tell a carrier from another tree.
    "INTERPOSER_PDK_ROOT": (("interposer", "OpenIntM4TM2"), PACKAGE_MANIFEST),
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
    through to the walk. ``INTERPOSER_PDK_ROOT`` is the root of the carrier
    package :func:`find_carrier_package` resolves, so it is refused
    (:class:`CarrierPackageRefused`) where that refuses.
    Returns the root as a string, or None when unresolvable.
    """
    if name == "INTERPOSER_PDK_ROOT":
        package = find_carrier_package(env=env, start=start)
        return None if package is None else package.root
    return _discover_root(name, env=env, start=start)


def _discover_root(name, *, env, start) -> Optional[str]:
    marker = PATH_VAR_MARKERS.get(name)
    if marker is None:
        # Only the known ecosystem-root variables are expandable. An unknown
        # ${NAME} is a typo/misuse, not a licence to expand an arbitrary
        # environment variable; return None so expand_path_vars fails loudly.
        return None
    selected = (os.environ if env is None else env).get(name)
    if selected and Path(selected).joinpath(*marker[1]).exists():
        return selected
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


# The member path and version rules below are chiplet-spec's
# (reference/python/chiplet_format_io: technology_package._unportable and
# _parse_contract_version), copied so this reader refuses what the package
# validator refuses and nothing else; tests/test_carrier_package.py holds
# them equal when a chiplet-spec checkout is at hand.
_DRIVE = re.compile(r"^[A-Za-z]:")


def _unportable(raw: str) -> Optional[str]:
    """Why a member path would not travel with the package, as written, or None."""
    if "\0" in raw:
        return "carries a NUL byte, which no file system takes"
    if "\\" in raw:
        return "uses a backslash; member paths use forward slashes"
    if "$" in raw or "%" in raw:
        return "carries a variable; member paths are literal"
    if raw.startswith("~"):
        return "starts at a home folder; member paths are relative to the package root"
    if _DRIVE.match(raw):
        return "names a drive; member paths are relative to the package root"
    path = PurePosixPath(raw)
    if path.is_absolute():
        return "is absolute; member paths are relative to the package root"
    if ".." in path.parts:
        return "climbs out of the package root"
    return None


def _contract_version(value) -> Optional[Tuple[int, int]]:
    """``(major, minor)`` of a ``"MAJOR.MINOR"`` or ``"MAJOR.MINOR.PATCH"``
    string of ASCII digits, or None."""
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) not in (2, 3):
        return None
    if not all(part.isascii() and part.isdigit() for part in parts):
        return None
    return (int(parts[0]), int(parts[1]))


class CarrierPackage(NamedTuple):
    """A carrier technology package the export draws: its root and its
    manifest, read once by :func:`read_carrier_package`."""
    root: str
    manifest: dict

    @property
    def id(self) -> str:
        return self.manifest["id"]

    def member(self, *keys) -> str:
        """The file the manifest names at ``keys`` (``"stack", "path"``);
        CarrierPackageRefused if the member is absent, its path would not
        travel with the package, or it names no file.
        """
        value = self.manifest
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        where = "%s in %s under %s" % (".".join(keys),
                                       "/".join(PACKAGE_MANIFEST), self.root)
        if not isinstance(value, str) or not value:
            raise CarrierPackageRefused("PACKAGE_MEMBER_ABSENT",
                                        "no %s" % where)
        reason = _unportable(value)
        if reason:
            raise CarrierPackageRefused(
                "PACKAGE_MEMBER_PATH", "%s is %r, which %s"
                % (where, value, reason))
        path = os.path.join(self.root, *PurePosixPath(value).parts)
        if not _inside(self.root, path):
            raise CarrierPackageRefused(
                "PACKAGE_MEMBER_PATH", "%s is %r, which leads out of the "
                "package root through a link" % (where, value))
        if not os.path.isfile(path):
            raise CarrierPackageRefused(
                "PACKAGE_MEMBER_MISSING", "%s is %r, which names no file"
                % (where, value))
        return path


def read_carrier_package(root) -> CarrierPackage:
    """The technology package at ``root``; CarrierPackageRefused if it is not
    one the export draws.

    Checks what the export relies on, not the whole schema (chiplet-spec's
    validator does that): a JSON object inside the root, of schema
    ``technology-package`` major 1, with the carrier role and an id, and
    that id IntM4TM2's: the sheet and stack are the package's, but pads and
    connections still land on IntM4TM2's TopMetal2, so another carrier is
    refused with ``CARRIER_UNSUPPORTED_TECHNOLOGY``, the id packaging-flow
    and chiplet-spec refuse it with. That refusal goes when the layer a
    connection lands on is read from the package.
    """
    root = str(root)
    where = "/".join(PACKAGE_MANIFEST)
    path = Path(root).joinpath(*PACKAGE_MANIFEST)
    if not path.is_file():
        raise CarrierPackageRefused(
            "PACKAGE_MANIFEST_MISSING", "%s has no %s" % (root, where))
    if not _inside(root, path):
        raise CarrierPackageRefused(
            "PACKAGE_MANIFEST_MISSING",
            "%s under %s leads out of the root through a link" % (where, root))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CarrierPackageRefused(
            "PACKAGE_MANIFEST_UNREADABLE", "%s: %s" % (path, exc)) from None
    if not isinstance(data, dict):
        raise CarrierPackageRefused(
            "PACKAGE_MANIFEST_UNREADABLE", "%s is not a JSON object" % path)
    version = _contract_version(data.get("schema_version"))
    if version is None or version[0] != PACKAGE_MAJOR:
        raise CarrierPackageRefused(
            "PACKAGE_SCHEMA_VERSION",
            "%s is version %r; this export reads %s %d.x"
            % (path, data.get("schema_version"), PACKAGE_SCHEMA,
               PACKAGE_MAJOR))
    if data.get("schema") != PACKAGE_SCHEMA:
        raise CarrierPackageRefused(
            "PACKAGE_SCHEMA_INVALID", "%s is schema %r, not %r"
            % (path, data.get("schema"), PACKAGE_SCHEMA))
    roles = data.get("roles")
    if not isinstance(roles, list) or not roles \
            or not all(isinstance(role, str) for role in roles):
        raise CarrierPackageRefused(
            "PACKAGE_SCHEMA_INVALID", "%s has roles %r" % (path, roles))
    if set(roles) != {"carrier"}:
        raise CarrierPackageRefused(
            "PACKAGE_ROLE_NOT_MODELLED",
            "%s has roles %r; carrier is the only role modelled"
            % (path, roles))
    if not isinstance(data.get("id"), str) or not data["id"]:
        raise CarrierPackageRefused(
            "PACKAGE_SCHEMA_INVALID", "%s has no id" % path)
    if data["id"] != CARRIER_TECHNOLOGY:
        raise CarrierPackageRefused(
            "CARRIER_UNSUPPORTED_TECHNOLOGY",
            "%s is the package %r; IntM4TM2 (%r) is the only carrier the "
            "export draws until the layer a connection lands on is read from "
            "the technology package" % (path, data["id"], CARRIER_TECHNOLOGY))
    return CarrierPackage(root, data)


def find_carrier_package(*, env=None, start=None) -> Optional[CarrierPackage]:
    """The carrier package at $INTERPOSER_PDK_ROOT, else at the sibling
    checkout; None when neither is there.

    A set variable that holds no package is refused, not passed over for
    the sibling: the export would draw with a carrier other than the one
    named. A package found either way is refused when the export does not
    draw it (:func:`read_carrier_package`).
    """
    selected = (os.environ if env is None else env).get("INTERPOSER_PDK_ROOT")
    if selected and not Path(selected).joinpath(*PACKAGE_MANIFEST).is_file():
        raise CarrierPackageRefused(
            "PACKAGE_MANIFEST_MISSING",
            "$INTERPOSER_PDK_ROOT is %s, which has no %s, the manifest of a "
            "carrier technology package (OpenIntM4TM2 has one from v2026.11). "
            "Point the variable at a package root, or unset it to use the "
            "sibling checkout." % (selected, "/".join(PACKAGE_MANIFEST)))
    root = _discover_root("INTERPOSER_PDK_ROOT", env=env, start=start)
    return None if root is None else read_carrier_package(root)


def _inside(root, path) -> bool:
    real_root = os.path.realpath(root)
    try:
        return os.path.commonpath([real_root,
                                   os.path.realpath(path)]) == real_root
    except ValueError:  # Windows: the two are on different drives.
        return False


def find_default_lyp(*, env=None, start=None) -> str:
    """Default layer-properties file for the interposer GDS.

    The ``layer_properties.lyp`` member of the carrier package. The file
    belongs to the interposer PDK, not the plugin, so there is no vendored
    copy: when no package resolves, return the unexpanded
    ``${INTERPOSER_PDK_ROOT}`` form of the manifest that names it, which
    :func:`expand_path_vars` then refuses with the variable to set.
    """
    package = find_carrier_package(env=env, start=start)
    if package is None:
        return "${INTERPOSER_PDK_ROOT}/" + "/".join(PACKAGE_MANIFEST)
    return package.member("layer_properties", "lyp")


def find_interposer_template(*, env=None, start=None) -> Optional[str]:
    """The interposer's KiCad template board, the package's
    ``kicad_template.board``; None when no package resolves or it names no
    usable board. Only an error text reads it.
    """
    package = find_carrier_package(env=env, start=start)
    if package is None:
        return None
    try:
        return package.member("kicad_template", "board")
    except CarrierPackageRefused:
        return None


def find_interposer_pdk_python(*, env=None, start=None):
    """Locate the interposer PDK's KLayout python dir (its PCell libraries
    and landing tooling).

    The root is :func:`find_carrier_package`'s. Whether the directory holds
    a given module is the importer's question. Returns the directory as a
    Path, or None when no package resolves.
    """
    package = find_carrier_package(env=env, start=start)
    if package is None:
        return None
    return Path(package.root) / "libs.tech" / "klayout" / "python"


def resolve_lyp_path(lyp_path=None, *, env=None, start=None):
    """Select/expand the worker's LYP once and return its absolute path.

    `start` is the actual worker script so the existing sibling walk also
    works with installed/symlinked plugins. GUI picker discovery is separate.
    """
    selected = (find_default_lyp(env=env, start=start)
                if lyp_path is None else lyp_path)
    expanded = expand_path_vars(selected, env=env, start=start)
    return str(Path(expanded).absolute())
