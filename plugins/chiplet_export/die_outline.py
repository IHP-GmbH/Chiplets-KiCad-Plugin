# SPDX-License-Identifier: GPL-3.0-or-later
"""DIE_OUTLINE_LAYER: the footprint field naming the layer that sizes a die.

A die the .chiplet export sizes from its GDS carries the footprint field
DIE_OUTLINE_LAYER, whose value is the LAYER/DATATYPE that GDS draws the die
outline on (39/4, the SG13G2 EdgeSeal boundary, for SG13G2 dies). The worker
(hyp_to_gds.py --update-chiplet-file or --with-chiplets, die_outline_gds.py)
reads that layer and takes the die width and height from its one rectangle.
There is no default: a die without the field refuses by name.

This module is what KiCad's process and the worker share: the value grammar
(parse_layer, format_layer), the footprint classification the .chiplet writer
and the export preflight both use (die_role), and the preflight itself, which
checks the declarations before any output exists. The GDS itself is read only
by the worker. Nothing here imports pcbnew, klayout or yaml at module level:
KiCad's Python has no klayout, and the worker has no pcbnew.

Scope, from die_role: footprints the writer does not emit as dies
(BOM-excluded, IO_CLASS, cap_cmim) are out of scope, and their field is never
read. Dies marked with CHIPLET_PAD_DOMAINS keep their archived PinList size, so
they are exempt (--die-outline-exempt) and their field is never read either.
Every other footprint is emitted as a die, with or without a GDS_FILE, so it
needs a GDS_FILE and the field; a refusal for one without GDS_FILE says how to
exclude it.
"""

import datetime
import re
from collections import Counter
from typing import Dict, List, Tuple

if __package__:
    from .board_snapshot import SnapshotError, _properties
    from .pad_domains import PROPERTY as PAD_DOMAINS_PROPERTY, PadDomainError
else:
    from board_snapshot import SnapshotError, _properties
    from pad_domains import PROPERTY as PAD_DOMAINS_PROPERTY, PadDomainError


#: The footprint field that declares a die's outline layer.
FIELD = "DIE_OUTLINE_LAYER"

#: GDS layer and datatype numbers are 16-bit.
MAX_GDS_NUMBER = 65535

_LAYER_TOKEN = re.compile(r"([0-9]+)/([0-9]+)\Z")

# What write_chiplet does with a footprint (die_role).
NOT_EMITTED = "not_emitted"    # FP_EXCLUDE_FROM_BOM
IO_PAD = "io_pad"              # IO_CLASS, nested under interposer.io_pads
CMIM = "cmim"                  # interposer-integrated capacitor, not a die
ARCHIVED_DIE = "archived_die"  # CHIPLET_PAD_DOMAINS: archived PinList size
OUTLINE_DIE = "outline_die"    # sized from the outline layer of its GDS

CMIM_MODEL = "cap_cmim"

# The fix every missing or malformed declaration names.
_SET_FIELD = ("Set %s to the LAYER/DATATYPE its GDS draws the die outline on "
              "(39/4 for SG13G2 dies)" % FIELD)
_NOT_A_LAYER = "not LAYER/DATATYPE, two integers from 0 to %d" % MAX_GDS_NUMBER

# Why write_chiplet does not export a footprint as a die, by role.
_NOT_A_DIE = {
    NOT_EMITTED: "it is excluded from the bill of materials",
    IO_PAD: "it is an IO pad (IO_CLASS)",
    CMIM: "it is a %s capacitor" % CMIM_MODEL,
}

# What writers.hyperlynx_writer._sanitize_hyp_token rewrites in a reference,
# and the name it gives a footprint without one: the .hyp device then carries
# another reference than the one the flags name.
_HYP_REWRITTEN = {'"': "'", "\n": " ", "\r": " ", "\t": " "}
_HYP_NO_REFERENCE = "EMPTY"

# The YAML 1.1 resolvers (PyYAML's, which the worker reads the .chiplet with)
# that can take a .chiplet id for something other than a string. The writer
# quotes an id that is not a plain token starting with a letter, a digit or
# '_' (writers._yaml.yaml_scalar), so only these resolvers, whose tokens start
# with a digit, ever see one, and every token they match is written bare.
_YAML_INT = re.compile(r"(?:0b[0-1_]+|0[0-7_]+|0|[1-9][0-9_]*"
                       r"|0x[0-9a-fA-F_]+)\Z")
_YAML_FLOAT = re.compile(r"[0-9][0-9_]*\.[0-9_]*(?:[eE][-+][0-9]+)?\Z")
_YAML_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})\Z")


def format_layer(layer: Tuple[int, int]) -> str:
    return "%d/%d" % tuple(layer)


def parse_layer(text: str) -> Tuple[int, int]:
    """``"39/4"`` -> ``(39, 4)``; ValueError for anything else.

    Two decimal integers from 0 to 65535 and nothing more: no sign, no space,
    no third field. A field value is stripped before it gets here.
    """
    match = _LAYER_TOKEN.match(text)
    if not match:
        raise ValueError(text)
    layer, datatype = int(match.group(1)), int(match.group(2))
    if layer > MAX_GDS_NUMBER or datatype > MAX_GDS_NUMBER:
        raise ValueError(text)
    return layer, datatype


class DieOutlineLayerRefused(Exception):
    """One or more dies do not declare a usable outline layer.

    Raised by the export preflight, before any output exists. Deliberately not
    a ``ValueError``, for the reason ``pipeline.orchestrator
    .InterconnectSourceRefused`` gives: a handler written for a malformed
    value must not be able to absorb it. ``offenders`` lists every
    (reference, cause) pair, in board order. The message names the references
    first, so they survive a status line cut to its first few words, then
    each cause once, with the references it applies to (by_cause).
    """

    def __init__(self, offenders):
        self.offenders = list(offenders)
        refs = []
        for ref, _cause in self.offenders:
            if _shown(ref) not in refs:
                refs.append(_shown(ref))
        super().__init__("Die outline layer refused: %s: %s" % (
            ", ".join(refs),
            "; ".join("%s: %s" % (", ".join(group), cause)
                      for group, cause in self.by_cause())))

    def by_cause(self):
        """[(references, cause)], one entry per distinct cause, in the order
        the causes first appear, the references as the message shows them.

        A board full of ordinary library parts refuses every one of them for
        the same reason, and one sentence per part would bury the rest.
        """
        groups: Dict[str, List[str]] = {}
        for ref, cause in self.offenders:
            groups.setdefault(cause, []).append(_shown(ref))
        return [(group, cause) for cause, group in groups.items()]


def _shown(ref):
    # A reference the refusal is about can be empty or hold the very comma
    # that joins the list, so it is quoted then.
    if isinstance(ref, str) and ref and _reference_problem(ref) is None:
        return ref
    return repr(ref)


def _hyp_reference(ref):
    """The REF the .hyp gives the footprint with reference ``ref``."""
    ref = ref or _HYP_NO_REFERENCE
    for char, replacement in _HYP_REWRITTEN.items():
        ref = ref.replace(char, replacement)
    return ref


def _yaml_read_back(ref):
    """What the worker reads back from ``ref`` written as a bare .chiplet id,
    as text, or None when that id cannot be read at all.

    Mirrors PyYAML's int, float and timestamp constructors: "007" is 7,
    "1_0" is 10, "0x1F" is 31, "1.50" is 1.5, and "2001-02-30" does not load.
    """
    plain = ref.replace("_", "")
    try:
        if _YAML_FLOAT.match(ref):
            return str(float(plain))
        if _YAML_INT.match(ref):
            if plain[:2] in ("0b", "0x"):
                return str(int(plain[2:], 2 if plain[1] == "b" else 16))
            if plain != "0" and plain.startswith("0"):
                return str(int(plain, 8))
            return str(int(plain))
        date = _YAML_DATE.match(ref)
        if date:
            return datetime.date(*map(int, date.groups())).isoformat()
    except ValueError:
        return None
    return ref


def _reference_problem(ref):
    """Why ``ref`` cannot travel in the worker's REF=L/D,... flags, or name
    the same die in the .hyp and the .chiplet the worker reads, or None."""
    if not ref:
        return ("the footprint has an empty reference, which the worker's "
                "per-die flags cannot name. Give it a reference")
    if "," in ref or "=" in ref:
        return ("the reference contains ',' or '=', which the worker's "
                "REF=LAYER/DATATYPE,... flags cannot carry. Rename the "
                "footprint")
    if ref != ref.strip():
        return ("the reference starts or ends with whitespace, which the "
                "worker strips, so it would name another die. Rename the "
                "footprint")
    if _hyp_reference(ref) != ref:
        return ("the reference contains a double quote, a tab or a line "
                "break, which the .hyp rewrites, so the worker would find no "
                "die by it. Rename the footprint")
    if _yaml_read_back(ref) != ref:
        return ("the .chiplet writes this reference unquoted and YAML takes "
                "it for a number or a date, so the worker would find no "
                "die by it. Rename the footprint")
    return None


def _field_text(footprint, name):
    # Same lookup as writers.chiplet_writer._field_text. Kept here so the
    # export preflight never imports the pcbnew-bound writer module.
    if footprint.HasField(name):
        return footprint.GetFieldText(name)
    return ""


def is_cmim(footprint):
    """True when `footprint` is an interposer-integrated MIM capacitor.

    Both spellings occur: the KiCad footprint library writes `Model`, the
    schematic symbol carries `Sim.Name`, and a footprint that came through
    "Update PCB from Schematic" has both.
    """
    model = (_field_text(footprint, "Model") or
             _field_text(footprint, "Sim.Name")).strip()
    return model == CMIM_MODEL


def die_role(footprint, pcbnew):
    """Classify `footprint` exactly as write_chiplet treats it.

    One function for the writer loop and the export preflight, so the two
    cannot disagree about which footprints need an outline layer. The order is
    the writer's: BOM exclusion, IO_CLASS, cap_cmim, then the carrier
    pad-domain marker. A duplicated property refuses as PadDomainError, as
    pad_domains.archived_die_dimensions always did.
    """
    if footprint.GetAttributes() & pcbnew.FP_EXCLUDE_FROM_BOM:
        return NOT_EMITTED
    if _field_text(footprint, "IO_CLASS"):
        return IO_PAD
    if is_cmim(footprint):
        return CMIM
    try:
        marked = PAD_DOMAINS_PROPERTY in _properties(footprint)
    except SnapshotError as exc:
        raise PadDomainError(str(exc)) from exc
    return ARCHIVED_DIE if marked else OUTLINE_DIE


def _classified(board, pcbnew):
    """[(footprint, reference, role)] in board order. The pcbnew module is
    imported only when the board has footprints, as in
    pad_domains.validate_board_pad_domains."""
    footprints = list(board.Footprints())
    if footprints and pcbnew is None:
        import pcbnew
    return [(fp, fp.GetReference(), die_role(fp, pcbnew)) for fp in footprints]


def read_die_outline_layers(board, pcbnew=None) -> Dict[str, str]:
    """{reference: DIE_OUTLINE_LAYER text} for every die sized from its GDS.

    The raw field text, malformed values included, and "" for a die without
    the field: whoever shows it must show what the board says, and the
    preflight quotes it. Marked dies and footprints the writer does not
    export as dies are not listed.
    """
    return {ref: _field_text(fp, FIELD)
            for fp, ref, role in _classified(board, pcbnew)
            if role == OUTLINE_DIE}


def die_roles(board, pcbnew=None) -> Dict[str, str]:
    """{reference: die_role} for a view keyed by reference, such as the
    export dialog's per-die rows.

    Where footprints share a reference, a die sized from its GDS wins over a
    marked die, and a marked die over a footprint the writer does not export
    as a die, so a row never hides a die behind another footprint. Whether
    the shared reference is acceptable is the preflight's call.
    """
    rank = {OUTLINE_DIE: 0, ARCHIVED_DIE: 1}
    roles: Dict[str, str] = {}
    for _fp, ref, role in _classified(board, pcbnew):
        held = roles.get(ref)
        if held is None or rank.get(role, 2) < rank.get(held, 2):
            roles[ref] = role
    return roles


def _explicit_text(value):
    """An explicit map value as the field text it stands for.

    "L/D" text, or a (layer, datatype) pair spelled the same way, so the pair
    goes through the one grammar too: (39.0, 4) is "39.0/4" and refuses.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return "/".join(str(v) for v in value)
    return str(value)


def _declaration_problem(footprint, explicit, from_dialog, ref):
    """(layer, None) for a usable declaration, else (None, cause)."""
    if from_dialog:
        raw = _explicit_text(explicit.get(ref))
        missing = "the export dialog gives it no outline layer"
        malformed = ("the export dialog gives it the outline layer %r, which "
                     "is %s" % (raw, _NOT_A_LAYER))
    else:
        raw = _field_text(footprint, FIELD)
        missing = ("%s is empty" % FIELD if footprint.HasField(FIELD)
                   else "no %s field" % FIELD)
        malformed = "%s is %r, which is %s" % (FIELD, raw, _NOT_A_LAYER)
    text = raw.strip()
    if not text:
        return None, "%s. %s" % (missing, _SET_FIELD)
    try:
        return parse_layer(text), None
    except ValueError:
        return None, "%s. %s" % (malformed, _SET_FIELD)


def preflight_die_outline_layers(board, explicit=None, pcbnew=None
                                 ) -> Tuple[Dict[str, Tuple[int, int]],
                                            List[str]]:
    """What the worker's --die-outline-layers and --die-outline-exempt carry.

    Returns ``(layers, exempt)``: {reference: (layer, datatype)} for every die
    sized from its GDS, and the sorted references of the marked dies, which
    keep their archived PinList size. Every die the writer exports is in
    exactly one of them.

    ``explicit`` ({reference: "L/D" text or (layer, datatype)}), when not
    empty, replaces the board's DIE_OUTLINE_LAYER fields wholesale, as an
    explicit die_thicknesses map does, and is held to the same grammar. The
    field of a marked die, and of any footprint the writer does not export as
    a die, is never read.

    Raises DieOutlineLayerRefused naming every offender at once: a die with no
    or a malformed declaration, a die without GDS_FILE, a reference two dies
    share or a die shares with an IO pad or a .hyp device, a reference the
    flags, the .hyp or the .chiplet cannot carry, and an explicit entry for
    anything but a die sized from its GDS. PadDomainError from die_role
    propagates.
    """
    explicit = dict(explicit or {})
    from_dialog = bool(explicit)
    classified = _classified(board, pcbnew)
    dies = [(fp, ref, role) for fp, ref, role in classified
            if role in (OUTLINE_DIE, ARCHIVED_DIE)]
    counts = Counter(ref for _fp, ref, _role in dies)
    outline_refs = {ref for _fp, ref, role in dies if role == OUTLINE_DIE}
    # The worker also finds two kinds of footprint that are not dies by
    # reference: every IO_CLASS footprint is an --io-pads entry, excluded
    # from the bill of materials or not (write_io_pads_json), and every
    # footprint with a GDS_FILE a .hyp device. A die sharing its reference
    # with one of them refuses there, so it refuses here, before any output.
    # Only a footprint whose reference a die also has is looked at.
    shared = set()
    for fp, ref, role in classified:
        if role in (OUTLINE_DIE, ARCHIVED_DIE):
            continue
        if ref in counts and _field_text(fp, "IO_CLASS"):
            shared.add(ref)
        if _hyp_reference(ref) in counts and _field_text(fp, "GDS_FILE"):
            shared.add(_hyp_reference(ref))

    layers: Dict[str, Tuple[int, int]] = {}
    exempt: List[str] = []
    offenders: List[Tuple[str, str]] = []
    named = set()
    for fp, ref, role in classified:
        if role not in (OUTLINE_DIE, ARCHIVED_DIE):
            if (ref in explicit and ref not in outline_refs
                    and ref not in named):
                named.add(ref)
                offenders.append((ref, "the export dialog gives it an outline "
                                       "layer, but %s, so the .chiplet does "
                                       "not export it as a die"
                                  % _NOT_A_DIE[role]))
            continue
        if ref in named:
            continue
        problem = _reference_problem(ref)
        if problem is None and counts[ref] > 1:
            problem = ("%d footprints the .chiplet exports as dies carry this "
                       "reference, and a per-die outline layer cannot tell "
                       "them apart. Give each die its own reference"
                       % counts[ref])
        elif problem is None and ref in shared:
            problem = ("an IO pad (IO_CLASS) or a part with a GDS_FILE carries "
                       "this reference too, and the worker, which finds both "
                       "by reference, cannot tell it from the die. Give each "
                       "die its own reference")
        if problem is not None:
            named.add(ref)
            offenders.append((ref, problem))
            continue
        if role == ARCHIVED_DIE:
            if ref in explicit:
                offenders.append((ref, "the export dialog gives it an outline "
                                       "layer, but it is marked with %s and "
                                       "keeps its archived PinList size"
                                  % PAD_DOMAINS_PROPERTY))
            else:
                exempt.append(ref)
            continue
        if not _field_text(fp, "GDS_FILE").strip():
            # Every footprint the writer does not skip is exported as a die,
            # so an ordinary library part (a resistor, a connector, a logo)
            # lands here. Without a GDS_FILE the likelier fix is to take it
            # out of the .chiplet, so say how. Update PCB from Schematic
            # copies the attribute from the symbol, so a part with one is
            # excluded there, or the next update undoes it.
            offenders.append((ref, "no GDS_FILE field, so there is no GDS to "
                                   "read its die outline from. If it is not "
                                   "a die, set 'Exclude from bill of "
                                   "materials' on its symbol in the "
                                   "schematic (on the footprint, for a part "
                                   "that is only on the board) and the "
                                   ".chiplet export stops treating it as "
                                   "one. If it is, set GDS_FILE and %s"
                                   % FIELD))
            continue
        layer, cause = _declaration_problem(fp, explicit, from_dialog, ref)
        if cause is not None:
            offenders.append((ref, cause))
        else:
            layers[ref] = layer

    on_board = {ref for _fp, ref, _role in classified}
    for ref in sorted((r for r in explicit if r not in on_board), key=str):
        offenders.append((ref, "the export dialog gives it an outline layer, "
                               "but no footprint on the board carries this "
                               "reference"))
    if offenders:
        raise DieOutlineLayerRefused(offenders)
    return layers, sorted(exempt)
