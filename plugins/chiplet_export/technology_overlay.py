# SPDX-License-Identifier: GPL-3.0-or-later
"""Technology ids for the exported .chiplet, taken from the System .chiplet.

The board does not carry technology ids: the writers name a technology only
from a die's LYP path, and a board synthesized from a System document has
none. The System .chiplet the assembly was built from does name them, on
every component, so the finalizer takes them from there by component id:

- the interposer and every die of the exported document get the technology
  of the System component with the same id;
- ``technologies`` becomes the System's entries for exactly the ids bound,
  copied as written.

Nothing is inferred. No LYP is looked up, and an id the System does not
declare is refused rather than invented. A System component the exported
document does not have (a build that stopped before placing it) is ignored,
and so is a component that is a wire-bond pad (an --io-pads reference), which
the finalizer folds under the interposer and does not treat as a die.

The carrier must be IntM4TM2: a System whose interposer component names
another technology is refused, the cause led by
``CARRIER_UNSUPPORTED_TECHNOLOGY``, the id packaging-flow and chiplet-spec
refuse the same carrier with. The export draws every carrier with IntM4TM2's
sheet, layers and via cells, so it would otherwise write a valid-looking
interposer for a carrier it does not model. The refusal is temporary: it goes
when a carrier is read from its technology package.

Every offender is collected before raising, so one run names them all.
"""

import copy
import os

#: Fields of a technology entry that hold a resource path (the chiplet-spec
#: technology definition is a closed set; the other two are description and
#: dbu).
PATH_FIELDS = ("layer_properties", "stackup")

#: The one carrier technology the export models (the module docstring).
CARRIER_TECHNOLOGY = "intm4tm2"


class TechnologyRefused(Exception):
    """The System .chiplet cannot supply the exported document's technologies.

    ``offenders`` is a list of (id, cause); the id is the component's, or the
    technology's for a problem in its catalog entry.
    """

    def __init__(self, offenders):
        self.offenders = list(offenders)
        super().__init__("; ".join("%s: %s" % item for item in self.offenders))

    def lines(self):
        return ["Technology refused: %s: %s" % item for item in self.offenders]


def _anchored(path):
    """True for a path whose meaning does not depend on the .chiplet's
    directory: absolute, or rooted at an environment variable."""
    return os.path.isabs(path) or path.startswith("${")


def plan(document, system, pads=()):
    """Return (bindings, technologies) for ``document`` from ``system``.

    ``bindings`` maps each component id of ``document``, other than the
    wire-bond pad references in ``pads``, to its technology id;
    ``technologies`` maps each bound id to a copy of the System's entry,
    sorted by id like the writers emit them. Raises TechnologyRefused.
    """
    by_id = {}
    duplicated = set()
    for component in system.get("components") or []:
        ident = component.get("id")
        if ident in by_id:
            duplicated.add(ident)
        by_id[ident] = component
    catalog = system.get("technologies") or {}

    pads = set(pads)
    offenders = []
    bindings = {}
    for component in document.get("components") or []:
        ident = component.get("id")
        if ident in pads:
            continue
        source = by_id.get(ident)
        if source is None:
            offenders.append((ident, "no component of that id in the System "
                                     ".chiplet"))
            continue
        if ident in duplicated:
            offenders.append((ident, "more than one component of that id in "
                                     "the System .chiplet"))
            continue
        tech = source.get("technology")
        if not isinstance(tech, str) or not tech:
            offenders.append((ident, "its System component has no "
                                     "technology"))
            continue
        if tech not in catalog:
            offenders.append((ident, "technology %r is not declared in the "
                                     "System's technologies" % tech))
            continue
        if source.get("type") == "interposer" and tech != CARRIER_TECHNOLOGY:
            offenders.append((ident, "CARRIER_UNSUPPORTED_TECHNOLOGY: the "
                                     "carrier's technology is %r; IntM4TM2 (%r) "
                                     "is the only carrier modelled until carriers "
                                     "are read from their technology package"
                                     % (tech, CARRIER_TECHNOLOGY)))
            continue
        bindings[ident] = tech

    technologies = {}
    for tech in sorted(set(bindings.values())):
        entry = catalog[tech] if catalog[tech] is not None else {}
        if not isinstance(entry, dict):
            offenders.append((tech, "its System entry is not a mapping"))
            continue
        for field in PATH_FIELDS:
            value = entry.get(field)
            if value is None or value == "":
                continue
            if not isinstance(value, str) or not _anchored(value):
                offenders.append((tech, "%s %r is a relative path, which "
                                        "cannot be anchored to the exported "
                                        ".chiplet" % (field, value)))
        technologies[tech] = copy.deepcopy(entry)

    if offenders:
        raise TechnologyRefused(offenders)
    return bindings, technologies


def _put(mapping, key, value, after):
    """Set ``mapping[key]`` in place when present; otherwise insert it after
    the first key of ``after`` that the mapping has, or at the end."""
    if key in mapping:
        mapping[key] = value
        return
    anchor = next((k for k in after if k in mapping), None)
    if anchor is None:
        mapping[key] = value
        return
    items = list(mapping.items())
    mapping.clear()
    for k, v in items:
        mapping[k] = v
        if k == anchor:
            mapping[key] = value


def apply(document, bindings, technologies):
    """Write a plan from ``plan`` into ``document``, in place.

    Keys go where the writer puts them: ``technologies`` after
    ``interposer``, a component's ``technology`` after its ``anchor``.
    """
    _put(document, "technologies", technologies,
         ("interposer", "assembly", "format_version"))
    for component in document.get("components") or []:
        if component.get("id") in bindings:
            _put(component, "technology", bindings[component.get("id")],
                 ("anchor", "type", "id"))
