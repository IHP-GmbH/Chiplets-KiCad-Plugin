# SPDX-License-Identifier: GPL-3.0-or-later
"""
Path discovery and persistence for the resizer passive elements plugin.

Resolution chain, following the same convention as ``discover_dependency_root``
in the sibling ``chiplet_export`` plugin: environment variable -> project text
variable -> sibling checkout on disk -> the hardcoded ``/work/OpenIntM4TM2``
Docker bind-mount path (last resort) -> "" (nothing found). The first three are
the "official" legs, tried first; the Docker path only kicks in when none of
them resolves. This is used to find the *shared* interposer PDK checkout (for
``intm4tm2_tech.json`` and ``cmim_footprint_gen.py``, both read-only, never
copied or modified here).

The local settings the dialog exposes (roots, tech.json override, output
.pretty directory) are persisted next to the board so the four buttons agree
without re-asking the user every time.
"""

import json
import os
from pathlib import Path

# Environment variables / project text variables naming the root of the shared
# interposer PDK checkout (the parent of "libs.tech"). INTERPOSER_PDK_ROOT is
# the ecosystem-wide name, used by chiplet_export and by hyp_to_gds, and is
# tried first so one variable configures every tool; INTM4TM2_ROOT stays
# accepted as an alias.
REPO_ROOT_ENV_VARS = ("INTERPOSER_PDK_ROOT", "INTM4TM2_ROOT")

# Back-compat alias for callers that referenced the single-variable name.
REPO_ROOT_ENV_VAR = REPO_ROOT_ENV_VARS[-1]

# Project text variables the dialog reads/writes for its editable fields
# (see dialog_log.py).
ROOT_DIR_TEXT_VAR = "CMIM_INTM4TM2_ROOT_DIR"
TECH_JSON_TEXT_VAR = "CMIM_TECH_JSON"
GEN_SCRIPT_TEXT_VAR = "CMIM_GEN_SCRIPT"
OUTPUT_DIR_TEXT_VAR = "CMIM_OUTPUT_DIR"

# Fixed layout of the shared repo, relative to its root. Never hard-coded
# elsewhere: both discover_tech_json_path and discover_footprint_gen_path
# route through _resolve_repo_relative with one of these tuples.
_TECH_JSON_RELATIVE = (
    "libs.tech", "klayout", "python",
    "intm4tm2_pycell_lib", "intm4tm2_tech.json",
)
_GEN_SCRIPT_RELATIVE = ("libs.tech", "kicad", "scripts", "cmim_footprint_gen.py")

# Candidate directory names for the sibling-checkout leg of discovery.
# "interposer" comes first: it is what the ecosystem checkout is actually
# called, and it heads the same list in chiplet_export's
# DEPENDENCY_ROOT_MARKERS.
_SIBLING_NAMES = ("interposer", "OpenIntM4TM2", "OpenIntM4TM2-main",
                  "openintm4tm2")

# Last-resort hardcoded root, tried only after every "official" leg above
# has failed: the conventional bind-mount path for OpenIntM4TM2 inside the
# project's Docker image. Kept as a fallback, not the primary path, so an
# up-to-date Docker image (or any other host) that already resolves the
# checkout through the official legs never depends on this convention.
_DOCKER_FALLBACK_ROOT = "/work/OpenIntM4TM2"

# Default local output folder name, created next to the open .kicad_pcb.
_DEFAULT_OUTPUT_DIRNAME = "local_footprints.pretty"


def _lookup_text_var(board, name):
    """Best-effort read of a KiCad project text variable.

    Mirrors the defensive std::map handling used throughout the sibling
    chiplet_export plugin (writers/chiplet_writer.py): the SWIG wrapper may
    expose the map as dict-like or std::map-like, and some boards have no
    usable PROJECT at all -- any of that yields "" rather than a raised
    exception. In KiCad 9 none of it fires: GetProject() returns an opaque
    object with no GetTextVars, so this is a read that always comes back
    empty. Kept as the forward-compatible leg; the working store is the
    JSON written by save_path_overrides.
    """
    if board is None:
        return ""
    try:
        project = board.GetProject()
    except Exception:
        return ""
    if project is None or not hasattr(project, "GetTextVars"):
        return ""
    try:
        text_vars = project.GetTextVars()
    except Exception:
        return ""
    if text_vars is None:
        return ""
    try:
        if name in text_vars:
            return str(text_vars[name])
    except (TypeError, KeyError):
        pass
    if hasattr(text_vars, "count") and hasattr(text_vars, "at"):
        try:
            if text_vars.count(name):
                return str(text_vars.at(name))
        except Exception:
            pass
    return ""


def _sibling_roots():
    """Candidate interposer PDK checkout roots near this plugin's install dir.

    Every ancestor is walked, not a fixed three levels: the plugin sits at
    ``<repo>/plugins/<name>/`` and the checkout it is looking for can be a
    sibling of the repo, of the ecosystem root above it, or of a Docker mount
    point further up still. Same shape as _discover_path_var in hyp_to_gds.
    """
    for base in Path(__file__).resolve().parents:
        for name in _SIBLING_NAMES:
            yield base / name


def _resolve_repo_relative(relative_parts, board=None):
    """First existing file among the "official" roots, then the Docker
    fallback (env var -> project text var -> sibling checkout -> the
    hardcoded /work/OpenIntM4TM2 bind-mount path, tried last)."""
    candidate_roots = []

    for var in REPO_ROOT_ENV_VARS:
        env_root = os.environ.get(var)
        if env_root:
            candidate_roots.append(Path(env_root))

    saved_root = load_path_overrides(board)[0]
    if saved_root:
        candidate_roots.append(Path(saved_root))

    for var in REPO_ROOT_ENV_VARS:
        proj_root = _lookup_text_var(board, var)
        if proj_root:
            candidate_roots.append(Path(proj_root))

    candidate_roots.extend(_sibling_roots())
    candidate_roots.append(Path(_DOCKER_FALLBACK_ROOT))

    for root in candidate_roots:
        try:
            candidate = root.joinpath(*relative_parts)
        except Exception:
            continue
        if candidate.is_file():
            return str(candidate)
    return ""


def discover_tech_json_path(board=None):
    """Resolve intm4tm2_tech.json from the shared OpenIntM4TM2 checkout."""
    return _resolve_repo_relative(_TECH_JSON_RELATIVE, board=board)


def discover_footprint_gen_path(board=None):
    """Resolve cmim_footprint_gen.py from the shared OpenIntM4TM2 checkout.

    Used internally by apply_resize.py to import the generator without
    ever copying or modifying it (restriction: OpenIntM4TM2 is read-only).
    """
    return _resolve_repo_relative(_GEN_SCRIPT_RELATIVE, board=board)


def resolve_from_root(root_dir):
    """(tech_json_path, gen_script_path) found directly under an explicit
    OpenIntM4TM2 checkout root, each "" if not present there.

    Used to auto-fill the dialog's individual fields the moment the user
    points the "OpenIntM4TM2 root folder" field at a checkout -- handy
    when neither the environment variables nor a sibling checkout resolve
    (e.g. an older Docker image without the PDK baked in, so
    the checkout only exists at some path the user mounted by hand).
    """
    if not root_dir:
        return "", ""
    root = Path(root_dir)
    try:
        tech = root.joinpath(*_TECH_JSON_RELATIVE)
        gen = root.joinpath(*_GEN_SCRIPT_RELATIVE)
    except Exception:
        return "", ""
    return (
        str(tech) if tech.is_file() else "",
        str(gen) if gen.is_file() else "",
    )


def discover_output_pretty_dir(board):
    """Local default output folder: next to the open .kicad_pcb, never
    inside the shared OpenIntM4TM2 checkout."""
    if board is not None:
        try:
            board_file = board.GetFileName()
        except Exception:
            board_file = ""
        if board_file:
            return str(Path(board_file).resolve().parent / _DEFAULT_OUTPUT_DIRNAME)
    return str(Path.home() / _DEFAULT_OUTPUT_DIRNAME)


_SETTINGS_FILENAME = ".resizer_passive_elements.json"
_SETTINGS_KEYS = (ROOT_DIR_TEXT_VAR, TECH_JSON_TEXT_VAR,
                  GEN_SCRIPT_TEXT_VAR, OUTPUT_DIR_TEXT_VAR)


def _settings_path(board):
    """Where the dialog's path fields are persisted, next to the .kicad_pcb.

    Project text variables would be the natural home, but KiCad's SWIG
    bindings do not expose them: ``BOARD.GetProject()`` returns an opaque
    SwigPyObject with no ``GetTextVars``, so both writing and reading them is
    a no-op. A small JSON beside the board is the honest substitute; it is
    per-project the same way text variables would have been.
    """
    if board is None:
        return None
    try:
        board_file = board.GetFileName()
    except Exception:
        return None
    if not board_file:
        return None
    return Path(board_file).resolve().parent / _SETTINGS_FILENAME


def save_path_overrides(board, root_dir, tech_json_path, gen_script_path,
                        output_dir, on_log=None):
    """Persist the dialog's four path fields. True when they were written."""
    path = _settings_path(board)
    if path is None:
        if on_log is not None:
            on_log("note: paths not saved (the board has no file on disk yet)")
        return False
    values = dict(zip(_SETTINGS_KEYS,
                      (root_dir or "", tech_json_path or "",
                       gen_script_path or "", output_dir or "")))
    try:
        with open(path, "w") as handle:
            json.dump(values, handle, indent=2, sort_keys=True)
    except OSError as exc:
        if on_log is not None:
            on_log("note: paths not saved to {}: {}".format(path, exc))
        return False
    return True


def load_path_overrides(board):
    """Previously saved (root_dir, tech_json_path, gen_script_path,
    output_dir), each "" if unset."""
    path = _settings_path(board)
    values = {}
    if path is not None:
        try:
            with open(path, "r") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                values = loaded
        except (OSError, json.JSONDecodeError):
            values = {}
    return tuple(
        str(values.get(key, "") or "") or _lookup_text_var(board, key)
        for key in _SETTINGS_KEYS
    )
