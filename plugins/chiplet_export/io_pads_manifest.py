# SPDX-License-Identifier: GPL-3.0-or-later
"""The io pad manifest's envelope, as the writer stamps it and the worker
reads it.

chiplet-spec's ``io_pads.schema.json`` requires ``schema``, ``version`` and
``frame`` beside the ``io_pads`` list, and says a consumer hard-refuses a
frame it does not expect and never converts. The export writes the board
frame (``chiplet_writer.write_io_pads_json``) and ``hyp_to_gds.py --io-pads``
reads only that frame. A bare ``{"io_pads": [...]}`` states no frame, so it
is refused like a wrong one: reading it would mean assuming one.

Only the envelope is checked here; the entries are the worker's. No
KLayout, pcbnew or yaml at import: the writer imports the constants inside
KiCad. ``read`` imports the vendored reader, which needs PyYAML, when called.
"""

import json
from typing import Callable, Dict, List, Optional

if __package__:
    from . import vendored as _vendored
else:
    import vendored as _vendored

#: The manifest's discriminator, the version the writer stamps, and the
#: frame of a board-side emitter: the one frame the worker reads.
SCHEMA = "chiplet-io-pad-manifest"
VERSION = "1.0.0"
FRAME = "board_absolute_ynegated_um"

#: The version this reader accepts, for chiplet_format_io's version policy.
SUPPORTED_VERSION = "1.0"

#: The name the version policy gives the artifact in its messages.
ARTIFACT = "io_pads.json"


class IoPadsRefused(Exception):
    """The --io-pads manifest cannot be read as one; nothing is written.

    Not a ``ValueError``, for the reason ``DieOutlineRefused`` gives.
    """

    def lines(self) -> List[str]:
        return ["I/O pads refused: %s" % self]


def read(path: str, *,
         on_warn: Optional[Callable[[str], None]] = None) -> List[Dict]:
    """The pad entries of the manifest at ``path``; IoPadsRefused otherwise.

    The version goes through the vendored ``chiplet_format_io``'s shared
    version policy: a higher minor of major 1 is read with a note to
    ``on_warn``, under ``-W error`` too, as docs/VERSION_POLICY.md allows a
    consumer under an escalating host. The entries are returned as written.
    """
    cfio = _vendored.chiplet_format_io()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise IoPadsRefused("cannot read the --io-pads manifest %s: %s"
                            % (path, exc)) from None
    if not isinstance(data, dict):
        raise IoPadsRefused("%s is not a JSON object" % path)
    if not any(key in data for key in ("schema", "version", "frame")):
        raise IoPadsRefused(
            "%s has no schema, version or frame; io_pads.schema.json requires "
            "them, and a bare pad list states no frame. Write it as the "
            "KiCad export does (schema %r, version %r, frame %r)."
            % (path, SCHEMA, VERSION, FRAME))
    if data.get("schema") != SCHEMA:
        raise IoPadsRefused("%s has schema %r, not %r"
                            % (path, data.get("schema"), SCHEMA))
    try:
        cfio.check_contract_version(data.get("version"), SUPPORTED_VERSION,
                                    name=ARTIFACT, on_warn=on_warn)
    except cfio.ContractVersionError as exc:
        raise IoPadsRefused("%s: %s" % (path, exc)) from None
    except cfio.ContractVersionWarning:
        # A newer minor is accepted by the policy; under -W error its stdlib
        # warning is raised instead. The note already went to on_warn.
        pass
    if data.get("frame") != FRAME:
        raise IoPadsRefused(
            "%s is in frame %r; hyp_to_gds reads only %r and never converts"
            % (path, data.get("frame"), FRAME))
    pads = data.get("io_pads")
    if not isinstance(pads, list):
        raise IoPadsRefused("%s has no io_pads list" % path)
    return pads
