# SPDX-License-Identifier: GPL-3.0-or-later
"""
Generate .kicad_mod files via the cmim_footprint_gen.py, and
replace an already-placed footprint instance with the generated one.

Generated filenames/footprint names are capacitance-keyed
("CMIM_<label>", e.g. "CMIM_100fF"), matching the display style of the
official discrete-family footprints committed in OpenIntM4TM2
(CMIM_10fF ... CMIM_5pF) -- not footprint_name(w_um, l_um)'s own
dimension-keyed default ("CMIM_8p11x8p11um"), which is almost twice as
long and, at the same (tiny, plate-proportional) font size, visibly
overruns the device's outline.

Only apply_to_instance touches pcbnew, and it imports it lazily inside
the function -- the rest of this module has no pcbnew/wx dependency and
can run in plain Python.
"""

import importlib.util
import os
from pathlib import Path

from . import paths

_generator_module_cache = {}


def _load_generator_module(gen_script_path=None):
    """Import cmim_footprint_gen.py from `gen_script_path`, or -- when not
    given -- from wherever paths.discover_footprint_gen_path() finds it.

    Cached by resolved path so repeated calls across the four steps don't
    re-import on every capacitor.
    """
    gen_path = gen_script_path or paths.discover_footprint_gen_path()
    if not gen_path or not os.path.isfile(gen_path):
        raise FileNotFoundError(
            "cmim_footprint_gen.py not found (set the \"cmim_footprint_gen.py\" "
            'or "OpenIntM4TM2 root folder" field in the window, the {} '
            "environment variable, or use a sibling checkout of "
            "OpenIntM4TM2)".format(paths.REPO_ROOT_ENV_VAR)
        )
    gen_path = str(Path(gen_path).resolve())

    module = _generator_module_cache.get(gen_path)
    if module is not None:
        return module

    spec = importlib.util.spec_from_file_location("cmim_footprint_gen", gen_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _generator_module_cache[gen_path] = module
    return module


def load_tech(tech_json_path, gen_script_path=None):
    """Load the process tech dict via the generator's own load_tech()."""
    gen = _load_generator_module(gen_script_path)
    return gen.load_tech(tech_json_path)


def _format_cap_label(cap_fF):
    """Human capacitance label, e.g. 100 -> '100fF', 1500 -> '1p5pF'.

    Matches the display style of the official discrete-family footprints
    (CMIM_10fF ... CMIM_5pF) committed in OpenIntM4TM2 -- those are named
    by cmim_footprint_gen.py's own (underscore-prefixed, private)
    _cap_name()/_cap_nominal_label() helpers, which are off-limits here
    (only the spec's listed public functions are called on that module),
    so this is a small local formatter producing the same visual style.
    """
    if cap_fF >= 1000.0:
        value, unit = cap_fF / 1000.0, "pF"
    else:
        value, unit = float(cap_fF), "fF"
    text = "{:g}".format(round(value, 3))
    return (text + unit).replace(".", "p")


def _strip_visible_name_label(mod_path, name):
    with open(mod_path, "r") as handle:
        text = handle.read()

    marker = '(fp_text user "{}"'.format(name)
    start = text.find(marker)
    if start == -1:
        return

    depth = 0
    in_string = False
    i = start
    n = len(text)
    end = None
    while i < n:
        c = text[i]
        if in_string:
            if c == "\\":
                i += 1  # skip escaped character inside the string
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
        i += 1

    if end is None:
        return  # unexpected shape -- leave the file untouched rather than guess

    line_start = text.rfind("\n", 0, start) + 1
    after = end
    if after < n and text[after] == "\n":
        after += 1  # also drop the now-empty line's trailing newline

    with open(mod_path, "w") as handle:
        handle.write(text[:line_start] + text[after:])


def _generate_cap_cmim_footprint(params, tech, output_dir, on_log=None, gen_script_path=None):
    def log(message):
        if on_log is not None:
            on_log(message)

    reference = params.get("reference", "?")

    try:
        gen = _load_generator_module(gen_script_path)
    except Exception as exc:
        log("{}: ERROR: {}".format(reference, exc))
        return None

    w_um = params.get("w_um")
    l_um = params.get("l_um")

    if w_um is None or l_um is None:
        cap_fF = params.get("capacitance_fF")
        if cap_fF is None:
            log('{}: ERROR: missing both "w"/"l" and "Capacitance" '
                '(geometry cannot be computed)'.format(reference))
            return None
        try:
            cmin_fF, cmax_fF = gen.cap_bounds_fF(tech)
            if cap_fF < cmin_fF - 1e-6 or cap_fF > cmax_fF + 1e-6:
                log("{ref}: ERROR: Capacitance {c:.4f}fF out of range "
                    "[{lo:.2f}fF .. {hi:.1f}fF]: a square cap_cmim must stay "
                    "between Wmin={wmin:g}um (Cmin~{lo:.2f}fF) and "
                    "Cmax={cmaxp:g}pF.".format(
                        ref=reference, c=cap_fF, lo=cmin_fF, hi=cmax_fF,
                        wmin=tech["minLW_um"], cmaxp=cmax_fF / 1000.0))
                return None
            w_um = gen.cap_to_width(cap_fF, tech)
            l_um = w_um
        except Exception as exc:
            log("{}: ERROR: {}".format(reference, exc))
            return None

    min_lw_um = tech.get("minLW_um")
    if min_lw_um is not None and (w_um < min_lw_um - 1e-9 or l_um < min_lw_um - 1e-9):
        log("{ref}: ERROR: w={w:g}um l={l:g}um is below the device minimum "
            "(Wmin={wmin:g}um): the TopMetal1 via array (pad \"1\", PLUS) "
            "ends up empty and the pad would come out 0x0. Check the "
            "\"w\"/\"l\" field on the schematic symbol (remember it is "
            "stored in metres there, not micrometres).".format(
                ref=reference, w=w_um, l=l_um, wmin=min_lw_um))
        return None

    try:
        cap_label_source = params.get("capacitance_fF")
        if cap_label_source is None:
            cap_label_source = gen.cmim_capacitance_fF(w_um, l_um, tech)
        name = "CMIM_" + _format_cap_label(cap_label_source)
        out_path = os.path.join(output_dir, name + ".kicad_mod")
    except Exception as exc:
        log("{}: ERROR: {}".format(reference, exc))
        return None

    try:
        gen.write_footprint(w_um, l_um, tech, out_path, name=name)
    except Exception as exc:
        log("{}: ERROR: {}".format(reference, exc))
        return None

    _strip_visible_name_label(out_path, name)

    try:
        cap_fF_result = gen.cmim_capacitance_fF(w_um, l_um, tech)
    except Exception:
        cap_fF_result = None

    params["w_um"] = w_um
    params["l_um"] = l_um
    if cap_fF_result is not None:
        params["capacitance_fF"] = cap_fF_result

    if cap_fF_result is None:
        log("{}: generated {} (w={:g}um l={:g}um)".format(
            reference, out_path, w_um, l_um))
    else:
        log("{}: generated {} (w={:g}um l={:g}um C={:.2f}fF)".format(
            reference, out_path, w_um, l_um, cap_fF_result))
    return out_path


DEVICE_GENERATORS = {
    "cap_cmim": _generate_cap_cmim_footprint,
    # "res_xxx": _generate_resistor_footprint,   # once that PCell/generator exists
    # "ind_xxx": _generate_inductor_footprint,   # same, for inductors
}


def generate_footprint_file(params, tech, output_dir, on_log=None, gen_script_path=None):
    """Dispatch to the generator registered for params["model"] in
    DEVICE_GENERATORS (defaulting to "cap_cmim" when a caller doesn't set
    "model", for callers/tests written before device types existed).

    Returns None and logs an error if no generator is registered for
    that model -- e.g. a device board_reader.py already knows how to
    *read* (it's in DEVICE_READERS) but this module doesn't yet know how
    to *generate* a footprint for (not yet in DEVICE_GENERATORS).
    """
    def log(message):
        if on_log is not None:
            on_log(message)

    reference = params.get("reference", "?")
    model = params.get("model", "cap_cmim")
    handler = DEVICE_GENERATORS.get(model)
    if handler is None:
        log('{}: ERROR: no footprint generator registered for Model "{}" '
            "yet".format(reference, model))
        return None
    return handler(params, tech, output_dir, on_log, gen_script_path)

def _tokenize_sexp(text):
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "()":
            tokens.append(c)
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                elif c == '"':
                    i += 1
                    break
                else:
                    buf.append(c)
                    i += 1
            tokens.append(("".join(buf),))  # 1-tuple marks a leaf string
        elif c.isspace():
            i += 1
        else:
            buf = []
            while i < n and not text[i].isspace() and text[i] not in '()"':
                buf.append(text[i])
                i += 1
            tokens.append(("".join(buf),))
    return tokens


def _parse_sexp(text):
    tokens = _tokenize_sexp(text)
    pos = [0]

    def build():
        assert tokens[pos[0]] == "(", "expected '('"
        pos[0] += 1
        node = []
        while True:
            tok = tokens[pos[0]]
            if tok == "(":
                node.append(build())
            elif tok == ")":
                pos[0] += 1
                return node
            else:
                node.append(tok[0])
                pos[0] += 1

    while tokens[pos[0]] != "(":
        pos[0] += 1
    return build()


def _find_all(node, head):
    out = []
    if isinstance(node, list):
        if node and node[0] == head:
            out.append(node)
        for child in node:
            if isinstance(child, list):
                out.extend(_find_all(child, head))
    return out


def _child(node, head):
    for c in node:
        if isinstance(c, list) and c and c[0] == head:
            return c
    return None


def _read_pad_sizes_mm(mod_path):
    """{'1': (w_mm, h_mm), '2': (w_mm, h_mm), ...} from a .kicad_mod file."""
    with open(mod_path, "r") as handle:
        text = handle.read()
    tree = _parse_sexp(text)
    sizes = {}
    for pad in _find_all(tree, "pad"):
        number = pad[1]
        size = _child(pad, "size")
        if size is None:
            continue
        sizes[number] = (float(size[1]), float(size[2]))
    return sizes


def _style_provenance_field(footprint, field_name):
    """
    KiCad creates a brand-new footprint field visible on F.SilkS at
    1.27 mm by default -- harmless on a normal PCB, but on these
    um-scale devices it renders as a giant label sprawling across the
    whole view. Same fix as the reference Chiplets-KiCad-Plugin uses for
    its own machine-managed fields (writers/chiplet_writer.py,
    _style_managed_field): SWIG exposes GetFields() but not
    GetFieldByName(), so the field has to be looked up by name after
    SetField() creates/updates it. A no-op if the field can't be found or
    the API shape here doesn't match (never raises).
    """
    try:
        import pcbnew
        field = None
        for candidate in footprint.GetFields():
            if candidate.GetName() == field_name:
                field = candidate
                break
        if field is None:
            return
        field.SetLayer(pcbnew.F_Fab)
        field.SetVisible(False)
        small = pcbnew.FromMM(0.2)
        field.SetTextSize(pcbnew.VECTOR2I(small, small))
        field.SetTextThickness(pcbnew.FromMM(0.05))
    except Exception:
        pass


def _field_text(footprint, name):
    try:
        has_field = footprint.HasField(name)
    except Exception:
        has_field = True
    if not has_field:
        return None
    try:
        text = footprint.GetFieldText(name)
    except Exception:
        return None
    text = (text or "").strip()
    return text or None


def _format_meters(value_um):
    return "{:.12g}".format(float(value_um) * 1e-6)


def _format_capacitance_fF(value_fF):
    return "{:.12g}fF".format(float(value_fF))


def _apply_cap_cmim_fields(old_fp, new_fp, params):
    w_um = params.get("w_um")
    l_um = params.get("l_um")

    fields = {
        "Model": "cap_cmim",
        "Sim.Name": _field_text(old_fp, "Sim.Name") or "cap_cmim",
        "m": _field_text(old_fp, "m") or "1",
    }
    if w_um is not None:
        fields["w"] = _format_meters(w_um)
    if l_um is not None:
        fields["l"] = _format_meters(l_um)
    cap_fF = params.get("capacitance_fF")
    if cap_fF is not None:
        fields["Capacitance"] = _format_capacitance_fF(cap_fF)
    else:
        old_cap = _field_text(old_fp, "Capacitance")
        if old_cap is not None:
            fields["Capacitance"] = old_cap

    for name, value in fields.items():
        try:
            new_fp.SetField(name, value)
        except Exception:
            continue
        _style_provenance_field(new_fp, name)


def _apply_technology_fields(old_fp, new_fp, params):
    if not params:
        return
    if params.get("model") == "cap_cmim":
        _apply_cap_cmim_fields(old_fp, new_fp, params)


def apply_to_instance(board, footprint_obj, generated_mod_path, params=None, on_log=None):
    """Replace footprint_obj on `board` with a fresh load of the just-
    generated .kicad_mod, in place.

    The new footprint is added to the board BEFORE the old one is
    removed, so a failure partway through never leaves the board with
    neither copy of the part.

    Returns True on success. On any failure the board is left unchanged
    (old footprint stays exactly as it was) and False is returned.
    """
    def log(message):
        if on_log is not None:
            on_log(message)

    try:
        reference = footprint_obj.GetReference()
    except Exception:
        reference = "?"

    try:
        pad_sizes_mm = _read_pad_sizes_mm(generated_mod_path)
    except Exception as exc:
        log("{}: ERROR: could not read {}: {}".format(
            reference, generated_mod_path, exc))
        return False

    if "1" not in pad_sizes_mm or "2" not in pad_sizes_mm:
        log('{}: ERROR: {} does not have the expected pads "1" and "2"'.format(
            reference, generated_mod_path))
        return False

    old_pads = {}
    for candidate in footprint_obj.Pads():
        old_pads[candidate.GetNumber()] = candidate
    if "1" not in old_pads or "2" not in old_pads:
        log('{}: ERROR: the instance on the board does not have the '
            'expected pads "1" and "2" (nothing replaced)'.format(reference))
        return False

    import pcbnew  # local import: this is the only pcbnew-dependent path

    lib_dir = os.path.dirname(generated_mod_path)
    fp_name = os.path.splitext(os.path.basename(generated_mod_path))[0]

    try:
        new_fp = pcbnew.FootprintLoad(lib_dir, fp_name)
    except Exception as exc:
        log("{}: ERROR: could not load {} as a footprint: {}".format(
            reference, generated_mod_path, exc))
        return False
    if new_fp is None:
        log("{}: ERROR: pcbnew.FootprintLoad did not find \"{}\" in {}".format(
            reference, fp_name, lib_dir))
        return False

    new_pads = {}
    for candidate in new_fp.Pads():
        new_pads[candidate.GetNumber()] = candidate
    if "1" not in new_pads or "2" not in new_pads:
        log('{}: ERROR: the generated footprint does not have the expected '
            'pads "1" and "2" (nothing replaced)'.format(reference))
        return False

    old_sizes_mm = {
        number: (pcbnew.ToMM(pad.GetSize().x), pcbnew.ToMM(pad.GetSize().y))
        for number, pad in old_pads.items()
    }

    # Identity/placement carried over from the instance being replaced.
    new_fp.SetReference(reference)
    new_fp.SetPosition(footprint_obj.GetPosition())
    new_fp.SetOrientation(footprint_obj.GetOrientation())
    new_fp.SetLayer(footprint_obj.GetLayer())

    # Technology-field text refresh runs BEFORE the visibility-carryover
    # loop below, deliberately: _apply_technology_fields() (via
    # _style_provenance_field) unconditionally HIDES every field it
    # touches, including "Capacitance" -- which is also the one field
    # whose visibility is meant to be preserved from the old instance
    # when the schematic sync had made it visible (see the carryover
    # loop's docstring). Running the refresh first and letting the
    # carryover loop have the final word means Capacitance ends up
    # correctly re-shown when it should be, instead of the refresh's
    # blanket hide silently overriding a moment later.
    _apply_technology_fields(footprint_obj, new_fp, params)

    old_fields_by_name = {f.GetName(): f for f in footprint_obj.GetFields()}
    for new_field in new_fp.GetFields():
        name = new_field.GetName()
        if name in ("Reference", "Value"):
            continue
        old_field = old_fields_by_name.get(name)
        if old_field is not None and old_field.IsVisible():
            new_field.SetVisible(True)

    # Net connections, matched strictly by pad number -- never by index.
    for number, new_pad in new_pads.items():
        old_net = old_pads[number].GetNet()
        if old_net is not None:
            new_pad.SetNet(old_net)

    try:
        new_fp.SetField("CMIM_GENERATED_FILE", os.path.basename(generated_mod_path))
    except Exception:
        pass
    else:
        _style_provenance_field(new_fp, "CMIM_GENERATED_FILE")

    try:
        board.Add(new_fp)
    except Exception as exc:
        log("{}: ERROR: could not add the new footprint to the board: {} "
            "(the old instance was left untouched)".format(reference, exc))
        return False

    try:
        board.Remove(footprint_obj)
    except Exception as exc:
        log("{}: WARNING: the new footprint is on the board but the old one "
            "could not be removed ({}) -- both may now overlap, check by "
            "hand.".format(reference, exc))

    new_sizes_mm = {
        number: (pcbnew.ToMM(pad.GetSize().x), pcbnew.ToMM(pad.GetSize().y))
        for number, pad in new_pads.items()
    }

    log("{}: {} -> footprint replaced OK "
        "(pad1 {:.4f}x{:.4f}mm -> {:.4f}x{:.4f}mm, "
        "pad2 {:.4f}x{:.4f}mm -> {:.4f}x{:.4f}mm)".format(
            reference, fp_name,
            old_sizes_mm["1"][0], old_sizes_mm["1"][1],
            new_sizes_mm["1"][0], new_sizes_mm["1"][1],
            old_sizes_mm["2"][0], old_sizes_mm["2"][1],
            new_sizes_mm["2"][0], new_sizes_mm["2"][1]))
    return True
