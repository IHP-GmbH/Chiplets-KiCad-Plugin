# SPDX-License-Identifier: GPL-3.0-or-later
"""
Read supported device footprints already placed on a board.

Pure Python plus the `board`/`footprint` objects handed in -- no `wx`
import, and no module-level `import pcbnew`, so this stays importable
and testable with plain stand-in objects outside a running KiCad.

Device types are dispatched by the footprint's hidden "Model" field
through DEVICE_READERS. Today that registry has exactly one entry,
cap_cmim -- resistors/inductors have no generator to drive yet (see
apply_resize.DEVICE_GENERATORS), so there is nothing real to read for
them either. Adding a new device type once its PCell/generator exists:

    1. Write a `_read_<device>_fields(footprint, on_log) -> dict`
       function below, shaped like `_read_cap_cmim_fields`: always
       include "reference" and "model", plus whatever fields THAT
       device actually carries (parsed with its own unit rules -- do
       not assume "w"/"l"/capacitance-style parsing applies).
    2. Add one line to DEVICE_READERS mapping its "Model" string to that
       function.

find_supported_footprints() and everything that calls it need no
changes for this -- the returned dicts are allowed to have different
keys per device type (each reader defines its own shape beyond the
"reference"/"model" baseline), and apply_resize.generate_footprint_file()
dispatches on "model" the same way.
"""


def _field_text(footprint, name):
    """Field text for `name`, or None if absent/unreadable."""
    try:
        has_field = footprint.HasField(name)
    except Exception:
        has_field = True  # be permissive if HasField isn't available
    if not has_field:
        return None
    try:
        text = footprint.GetFieldText(name)
    except Exception:
        return None
    text = (text or "").strip()
    return text or None


def _parse_um(text):
    """'w'/'l' field text -> micrometres, or None if unparseable.

    Two formats genuinely occur on a real board, and they are only
    distinguishable by the unit suffix:

    - '8.11um' / '8.11u': written by cmim_footprint_gen.py's own hidden
      footprint properties (build_footprint's hidden_prop("w", ...)) --
      already in micrometres, suffix stripped and parsed directly.
    - '8.11e-6' (bare number, no suffix): how the cap_cmim symbol library
      stores "w"/"l" -- in METRES -- and what ends up on the footprint
      instance's fields after "Update PCB from Schematic" copies the
      symbol's properties verbatim. Confirmed by the PDK's own
      libs.tech/klayout/intm4tm2_tests/test_cmim_kicad_symbol.py, which
      reads the same fields via ``float(props["w"]) * 1e6``. Treating a
      bare value as already-micrometres (the earlier bug here) silently
      produces a ~0x0um footprint instead of the intended ~8.11x8.11um.

    cap_cmim-specific despite the generic-sounding name: a future
    device's own "w"/"l" (or whatever it calls its dimensions) might not
    share this exact two-format convention -- its reader should get its
    own parser rather than assuming this one applies.
    """
    if text is None:
        return None
    token = text.strip().lower()
    if token.endswith("um"):
        token = token[:-2]
        scale = 1.0
    elif token.endswith("u"):
        token = token[:-1]
        scale = 1.0
    else:
        scale = 1e6  # bare number: metres -> micrometres
    try:
        return float(token) * scale
    except ValueError:
        return None


_CAP_SUFFIX_SCALE = {"ff": 1.0, "f": 1.0, "pf": 1000.0, "p": 1000.0}


def _parse_capacitance_fF(text):
    """'100.00fF' / '1p' / '250f' / '250' -> femtofarads; unparseable -> None."""
    if text is None:
        return None
    token = text.strip().lower()
    # Longer suffixes ("pf"/"ff") must be tried before their single-letter
    # prefixes ("f"/"p") so "1pf" is not mis-read as trailing "f".
    for suffix in ("pf", "ff", "p", "f"):
        if token.endswith(suffix):
            try:
                return float(token[:-len(suffix)]) * _CAP_SUFFIX_SCALE[suffix]
            except ValueError:
                return None
    try:
        return float(token)
    except ValueError:
        return None


def _footprints_of(board):
    """board.Footprints() (the method the reference plugin actually uses),
    falling back to GetFootprints() for other pcbnew builds."""
    try:
        return list(board.Footprints())
    except AttributeError:
        return list(board.GetFootprints())


def _read_cap_cmim_fields(footprint, on_log):
    """params dict for one cap_cmim footprint instance.

    Returns:
        {
            "reference": str,
            "model": "cap_cmim",
            "w_um": float or None,
            "l_um": float or None,
            "capacitance_fF": float or None,
            "footprint_obj": the pcbnew FOOTPRINT instance,
        }
    """
    def log(message):
        if on_log is not None:
            on_log(message)

    try:
        reference = footprint.GetReference()
    except Exception:
        reference = "?"

    w_text = _field_text(footprint, "w")
    l_text = _field_text(footprint, "l")
    cap_text = _field_text(footprint, "Capacitance")

    w_um = _parse_um(w_text)
    l_um = _parse_um(l_text)
    capacitance_fF = _parse_capacitance_fF(cap_text)

    if w_text is None:
        log('{}: warning: missing "w" field'.format(reference))
    elif w_um is None:
        log('{}: warning: "w" field ("{}") could not be parsed as a '
            'number'.format(reference, w_text))

    if l_text is None:
        log('{}: warning: missing "l" field'.format(reference))
    elif l_um is None:
        log('{}: warning: "l" field ("{}") could not be parsed as a '
            'number'.format(reference, l_text))

    if cap_text is not None and capacitance_fF is None:
        log('{}: warning: "Capacitance" field ("{}") could not be '
            'parsed as a number'.format(reference, cap_text))

    return {
        "reference": reference,
        "model": "cap_cmim",
        "w_um": w_um,
        "l_um": l_um,
        "capacitance_fF": capacitance_fF,
        "footprint_obj": footprint,
    }


# Device types this plugin can read, keyed by the footprint's hidden
# "Model" field. See the module docstring for how to add a new entry.
DEVICE_READERS = {
    "cap_cmim": _read_cap_cmim_fields,
    # "res_xxx": _read_resistor_fields,   # once the resistor PCell/Model
    #                                      # name and its fields are known
    # "ind_xxx": _read_inductor_fields,   # same, for inductors
}


def find_supported_footprints(board, on_log=None):
    """Every footprint on `board` whose "Model" field matches a device
    type registered in DEVICE_READERS, each read by that type's own
    reader function.

    A footprint whose Model isn't registered at all is silently skipped
    (it isn't a device this plugin knows about yet) -- this is different
    from a registered device with a missing/bad field, which is always
    reported via `on_log` by that device's own reader, never dropped
    silently.
    """
    results = []
    for footprint in _footprints_of(board):
        model = _field_text(footprint, "Model")
        reader = DEVICE_READERS.get(model)
        if reader is None:
            continue
        results.append(reader(footprint, on_log))
    return results
