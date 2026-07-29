# SPDX-License-Identifier: GPL-3.0-or-later
"""
Path discovery and persistence for the resizer passive elements plugin.

Resolution chain (mirrors ``discover_dependency_root`` in the reference
Chiplets-KiCad-Plugin, section 1.3 of the spec): environment variable ->
project text variable -> sibling checkout on disk -> the hardcoded
``/work/OpenIntM4TM2`` Docker bind-mount path (last resort) -> ""
(nothing found). The first three are the "official" legs, tried first;
the Docker path only kicks in when none of them resolves. This is used
to find the *shared* ``OpenIntM4TM2`` checkout (for
``intm4tm2_tech.json`` and ``cmim_footprint_gen.py``, both read-only,
never copied or modified here).

The two *local* settings the dialog exposes (tech.json override, output
.pretty directory) are persisted separately as project text variables so
the four buttons agree without re-asking the user every time.
"""

import os
from pathlib import Path

# Environment variable / project text variable naming the root of the
# shared OpenIntM4TM2 checkout (the parent of "libs.tech").
REPO_ROOT_ENV_VAR = "INTM4TM2_ROOT"

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
_SIBLING_NAMES = ("OpenIntM4TM2", "OpenIntM4TM2-main", "openintm4tm2")

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

    Mirrors the defensive std::map handling used throughout the reference
    plugin (writers/chiplet_writer.py): the SWIG wrapper may expose the
    map as dict-like or std::map-like, and some boards have no usable
    PROJECT at all -- any of that yields "" rather than a raised
    exception.
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
    """Candidate OpenIntM4TM2 checkout roots near this plugin's install dir."""
    plugin_dir = Path(__file__).resolve().parent
    bases = (plugin_dir.parent, plugin_dir.parent.parent, plugin_dir.parent.parent.parent)
    for base in bases:
        for name in _SIBLING_NAMES:
            yield base / name


def _resolve_repo_relative(relative_parts, board=None):
    """First existing file among the "official" roots, then the Docker
    fallback (env var -> project text var -> sibling checkout -> the
    hardcoded /work/OpenIntM4TM2 bind-mount path, tried last)."""
    candidate_roots = []

    env_root = os.environ.get(REPO_ROOT_ENV_VAR)
    if env_root:
        candidate_roots.append(Path(env_root))

    proj_root = _lookup_text_var(board, REPO_ROOT_ENV_VAR)
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
    when neither INTM4TM2_ROOT nor a sibling checkout resolves (e.g. an
    older Docker image that doesn't have OpenIntM4TM2 baked in yet, so
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


def save_path_overrides(board, root_dir, tech_json_path, gen_script_path, output_dir):
    """Persist the dialog's four path fields as project text variables."""
    if board is None:
        return
    try:
        project = board.GetProject()
    except Exception:
        return
    if project is None or not hasattr(project, "GetTextVars"):
        return
    try:
        text_vars = project.GetTextVars()
        text_vars[ROOT_DIR_TEXT_VAR] = root_dir or ""
        text_vars[TECH_JSON_TEXT_VAR] = tech_json_path or ""
        text_vars[GEN_SCRIPT_TEXT_VAR] = gen_script_path or ""
        text_vars[OUTPUT_DIR_TEXT_VAR] = output_dir or ""
        if hasattr(project, "SetTextVars"):
            project.SetTextVars(text_vars)
    except Exception:
        pass


def load_path_overrides(board):
    """Previously saved (root_dir, tech_json_path, gen_script_path,
    output_dir), each "" if unset."""
    return (
        _lookup_text_var(board, ROOT_DIR_TEXT_VAR),
        _lookup_text_var(board, TECH_JSON_TEXT_VAR),
        _lookup_text_var(board, GEN_SCRIPT_TEXT_VAR),
        _lookup_text_var(board, OUTPUT_DIR_TEXT_VAR),
    )
