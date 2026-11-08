# SPDX-License-Identifier: GPL-3.0-or-later
"""run_export itself, against a .chiplet it cannot read.

Why this file exists separately from test_previous_export_content_guard.py:
those tests call ``evaluate_content_guard`` directly, which proves the helper
computes the right verdict and proves nothing about whether run_export asks it,
believes it, or stops. The defect this suite is about lived precisely in the
wiring: every component behaved as documented and the composition still copied
over the user's file. A helper test cannot see that.

run_export needs pcbnew, so the dependency is doubled EXPLICITLY: a module whose
``__getattr__`` raises, with each attribute declared one at a time as the import
graph proves it necessary. An undeclared access fails loudly rather than
returning a mock that satisfies any call. This is a strict dependency double and
it is not a claim that the native KiCad path was exercised; the native writers
are replaced here, and what is under test is the orchestration between the guard
and the copy.

Each case runs in a SUBPROCESS. The double has to occupy ``sys.modules["pcbnew"]``
before the writer modules import, and doing that in-process would leave a fake
pcbnew visible to every later test in the session, silently turning the suite's
honest "skipped: no pcbnew" into something else. Isolation is cheaper than that
ordering hazard.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = PLUGIN_ROOT.parent

UNREADABLE = b"\xff\xfe\x00 the user's only copy of this file \x83\x28"

CANONICAL = (
    'format_version: "1.0"\n'
    'assembly:\n'
    '  name: demo\n'
    'components:\n'
    '  - id: interposer\n'
    '    position:\n'
    '      x: 100.0\n'
    '      y: 50.0\n'
    'flow:\n'
    '  steps: []\n'
)

#: What a re-export stages over CANONICAL: the board moved the interposer, the
#: flow: block is not the writer's to emit, and the finalizer has yet to drop
#: the intermediate marker.
STAGED_REEXPORT = (
    'format_version: "1.0"\n'
    '_metadata:\n'
    '  finalize_required: true\n'
    'assembly:\n'
    '  name: demo\n'
    'components:\n'
    '  - id: interposer\n'
    '    position:\n'
    '      x: 1.0\n'
    '      y: 2.0\n'
)

#: The strict double plus the drivers, run as one script in a clean interpreter.
#: `emit_chiplet` is on and `emit_complete_gds` is off. A run that passes the
#: guard stages the .chiplet and starts the worker, which is the fake below
#: (discovery's hyp_to_gds lookup is pointed at it, and the interpreter running
#: this driver starts it), so whether the canonical file is replaced never
#: depends on this host's worker.
_DRIVER = r'''
import inspect, json, os, sys, types

sys.path.insert(0, {plugins_dir!r})

class _StrictPcbnew(types.ModuleType):
    """Anything not declared below is a bug in the double, not a default."""
    def __getattr__(self, name):
        raise AssertionError(
            "the run_export path touched pcbnew.%s, which this double does not "
            "declare. Declare it deliberately or stop calling it." % name)

_pcb = _StrictPcbnew("pcbnew")
# Declared one at a time, each because an import or a call needs it. The
# integer values are sentinels: nothing here depends on KiCad's real enum
# numbering, and if something starts to, it will compare against a sentinel and
# fail rather than accidentally agree.
_pcb.ActionPlugin = type("ActionPlugin", (object,), {{"register": lambda self: None}})
for _i, _name in enumerate([
        "BOARD", "LSET", "SHAPE_POLY_SET", "PCB_SHAPE", "VECTOR2I",
        "BS_ITEM_TYPE_COPPER", "BS_ITEM_TYPE_DIELECTRIC",
        "F_Cu", "B_Cu", "F_Fab", "F_CrtYd", "B_CrtYd",
        "PAD_ATTRIB_NPTH", "PAD_ATTRIB_PTH",
        "PAD_SHAPE_CIRCLE", "PAD_SHAPE_OVAL", "PAD_SHAPE_RECT",
        "PAD_SHAPE_ROUNDRECT",
        "PCB_ARC_T", "PCB_PAD_T", "PCB_TRACE_T", "PCB_VIA_T", "PCB_ZONE_T",
        "SHAPE_T_POLY", "SHAPE_T_RECT", "FP_EXCLUDE_FROM_BOM"]):
    setattr(_pcb, _name, 9000 + _i)
_pcb.PCB_IU_PER_MM = 1000000.0
_pcb.FromMM = lambda mm: int(mm * 1000000)
_pcb.LoadBoard = lambda path: (_ for _ in ()).throw(
    AssertionError("run_export must not load a board from disk"))
_pcb.ExpandTextVars = lambda token, project=None: token
sys.modules["pcbnew"] = _pcb

from chiplet_export.pipeline import orchestrator as orc
from chiplet_export.writers import chiplet_writer, hyperlynx_writer

OUT = {out_dir!r}
FINAL = os.path.join(OUT, "board.chiplet")

# Captured BEFORE the doubles displace them; this is the last moment the real
# functions are reachable in this interpreter.
REAL_SIGNATURES = {{
    "write_chiplet": str(inspect.signature(chiplet_writer.write_chiplet)),
    "write_hyperlynx": str(inspect.signature(hyperlynx_writer.write_hyperlynx)),
}}

def _fake_write_chiplet(board, output_path, interconnect_manifest=None, export_report=None):
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write({staged!r})
    return True

def _fake_write_hyperlynx(board, output_path):
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write("{{VERSION=2.0}}\n")
    return True

DOUBLE_SIGNATURES = {{
    "write_chiplet": str(inspect.signature(_fake_write_chiplet)),
    "write_hyperlynx": str(inspect.signature(_fake_write_hyperlynx)),
}}

chiplet_writer.write_chiplet = _fake_write_chiplet
hyperlynx_writer.write_hyperlynx = _fake_write_hyperlynx

from chiplet_export.pipeline import discovery
discovery.find_hyp_to_gds = lambda *_a, **_kw: {worker!r}

{extra_setup}

lines = []
options = orc.ExportOptions(output_dir=OUT, emit_chiplet=True,
                            emit_complete_gds=False, force={force!r},
                            worker_python_override=sys.executable)
board = types.SimpleNamespace(Footprints=lambda: [])
result = orc.run_export(board, options, {plugin_root!r}, on_log=lines.append)

manifest = ""
mpath = os.path.join(OUT, "MANIFEST.md")
if os.path.exists(mpath):
    with open(mpath, "r", encoding="utf-8") as fh:
        manifest = fh.read()

listing = sorted(os.listdir(OUT))
with open(os.path.join(OUT, "_probe.json"), "w", encoding="utf-8") as fh:
    json.dump({{"error": result.error or "", "exit_code": result.exit_code,
               "listing": listing,
               "log": lines, "manifest": manifest,
               "assessments": globals().get("ASSESSMENT_CALLS", []),
               "real_signatures": REAL_SIGNATURES,
               "double_signatures": DOUBLE_SIGNATURES}}, fh)
'''


#: Stands in for hyp_to_gds.py. run_export starts it as
#: ``<worker python> <hyp_to_gds.py> <args>``, and the driver makes it that
#: script. It finalizes the file it is handed the way the real finalizer does,
#: by dropping the intermediate marker, then exits with the code the test asks
#: for.
_FAKE_WORKER = r'''
import sys

args = sys.argv[1:]
if "--update-chiplet-file" in args:
    path = args[args.index("--update-chiplet-file") + 1]
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace("_metadata:\n  finalize_required: true\n", ""))
sys.exit({exit_code})
'''

def _fake_worker(tmp_path, exit_code):
    """Write the fake worker script; return its path."""
    script = tmp_path / "fake_worker.py"
    script.write_text(_FAKE_WORKER.format(exit_code=exit_code),
                      encoding="utf-8")
    return str(script)


def _run(tmp_path, canonical_bytes=None, sidecar=None, force=False,
         staged=CANONICAL, extra_setup="", worker_exit=0):
    """Drive run_export in a clean interpreter; return (probe dict, final bytes).

    ``canonical_bytes`` None means no existing .chiplet. ``sidecar`` None means
    no digest sidecar; otherwise the exact bytes to write as one.
    ``worker_exit`` is the fake worker's exit code, for a run that reaches it.
    """
    out = tmp_path / "out"
    out.mkdir()
    # "board" is the board name run_export derives from the doubled
    # board, so this is the path the pipeline actually reads and writes.
    final = out / "board.chiplet"
    if canonical_bytes is not None:
        final.write_bytes(canonical_bytes)
    if sidecar is not None:
        (out / ("board.chiplet" + ".exportcontent.sha256")).write_bytes(sidecar)

    script = _DRIVER.format(
        plugins_dir=str(PLUGINS_DIR), out_dir=str(out),
        plugin_root=str(PLUGIN_ROOT), staged=staged, force=force,
        worker=_fake_worker(tmp_path, worker_exit),
        extra_setup=textwrap.dedent(extra_setup))
    path = tmp_path / "driver.py"
    path.write_text(script, encoding="utf-8")

    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(path)], capture_output=True,
                          text=True, env=env, timeout=120)
    probe = out / "_probe.json"
    assert probe.exists(), (
        "the driver did not finish; run_export raised or the double refused.\n"
        "stdout: %s\nstderr: %s" % (proc.stdout, proc.stderr))
    with open(probe, "r", encoding="utf-8") as fh:
        result = json.load(fh)
    return result, (final.read_bytes() if final.exists() else None)


# --- the doubles still model what they replace -------------------------------

def test_the_writer_doubles_still_match_the_real_signatures(tmp_path):
    """The doubles are checked against the real writers, not against memory.

    Every other test in this file replaces both writers, so from here on the
    suite is only ever as truthful as those two stand-ins. Nothing else notices
    when the real signature moves underneath them, and the failure is quiet in
    both directions.

    Quiet one way: the orchestrator wraps each writer call in
    ``except Exception`` and turns it into an ExportResult error. So if the real
    ``write_chiplet`` grows a parameter the orchestrator starts passing, the
    double raises TypeError, the run returns an error, no file is written, and
    every preservation assertion in this file still passes -- because the bytes
    on disk survived a write that never happened. That is the exact vacuous
    green this suite exists to prevent, reappearing one level down in the
    scaffolding.

    Quiet the other way: if the real writer LOSES or renames a parameter, the
    double keeps accepting the old call happily and the suite goes on testing a
    function shape that no longer exists.

    Comparing the whole ``str(inspect.signature(...))`` rather than just the
    parameter count is deliberate: parameter NAMES are part of the contract the
    moment any call site uses a keyword, and the orchestrator already passes
    ``interconnect_manifest`` by keyword. A rename would be invisible to an
    arity check and fatal at the call site.

    The real signatures are read inside the subprocess, at the last moment they
    are reachable, and only there: importing the writers needs pcbnew, which is
    why they are doubled in the first place.
    """
    probe, _ = _run(tmp_path)
    real, double = probe["real_signatures"], probe["double_signatures"]
    assert real, "the driver recorded no real signatures; the capture moved"
    assert set(real) == set(double)
    for name in sorted(real):
        assert double[name] == real[name], (
            "the %s double no longer matches the writer it replaces.\n"
            "  real:   %s%s\n  double: %s%s\n"
            "Update the double in _DRIVER. Until it matches, every test in this "
            "file is exercising a function shape the product does not have, and "
            "a mismatch that raises TypeError is swallowed by the orchestrator's "
            "except Exception and reads as an ordinary export failure."
            % (name, name, real[name], name, double[name]))


# --- preservation: an unreadable canonical, every baseline shape -------------

@pytest.mark.parametrize("sidecar_label,sidecar", [
    ("absent", None),
    ("empty", b"   \n"),
    ("unreadable", b"\xff\xfe not a valid utf-8 digest \x83\x28"),
])
@pytest.mark.parametrize("force", [False, True])
def test_run_export_never_overwrites_an_unreadable_chiplet(
        tmp_path, sidecar_label, sidecar, force):
    """The defect, through the real orchestration, across all six combinations.

    Each baseline shape reaches the guard by a different route: absent and empty
    and unreadable all return before the digest is ever computed, so the failure
    that used to fail closed never fired. With force on top, nothing objected at
    all. The property is the same in every case and it is about bytes, not
    messages: the file the user cannot read is the file they still have.
    """
    probe, after = _run(tmp_path, canonical_bytes=UNREADABLE,
                        sidecar=sidecar, force=force)
    assert after == UNREADABLE, (
        "run_export overwrote an unreadable .chiplet (sidecar %s, force=%s)"
        % (sidecar_label, force))
    assert probe["error"], (
        "run_export destroyed nothing but reported success, so the next run "
        "would be told to proceed (sidecar %s, force=%s)"
        % (sidecar_label, force))
    assert "board.chiplet" in probe["error"]
    assert "unchanged" in probe["error"], (
        "the refusal must tell the user their file is intact: %r"
        % probe["error"])
    # Refused before staging: nothing was put next to the file either.
    assert not [name for name in probe["listing"]
                if name == ".board.chiplet.staging"
                or name.endswith(".unfinalized")], probe["listing"]


def test_the_refusal_gives_a_recoverable_next_step(tmp_path):
    """A refusal the user cannot act on sends them to delete the file.

    Naming the path and the decode error is not enough on its own: the obvious
    way out of "the exporter will not read this" is to remove it, which is the
    data loss the refusal just prevented. The message has to offer keeping the
    bytes first.
    """
    probe, _after = _run(tmp_path, canonical_bytes=UNREADABLE, sidecar=None)
    err = probe["error"]
    assert "move the file aside" in err
    assert "force=True does not bypass" in err


# --- the negative bypass: the fix removed, the preservation test must fail ---

def test_the_preservation_property_fails_without_the_readability_check(
        tmp_path):
    """Prove the tests above can fail. Without this they might assert nothing.

    Restores the pre-fix behaviour at the only place it lived (unwritable_reason
    returning None on a decode error) and asserts that run_export then destroys
    the file. If this ever starts passing while the tests above also pass, one
    of them has stopped measuring: the property cannot both need the check and
    survive its removal.

    The worker succeeds here. A failed worker leaves the canonical file alone
    whatever the guard said, so only a run whose finalized .chiplet replaces the
    canonical one can show what the readability check is protecting against.
    """
    bypass = '''
        from chiplet_export.pipeline import chiplet_merge as _cm
        _real = _cm.unwritable_reason

        def _pre_fix_unwritable_reason(path):
            """The old behaviour: a file we cannot decode is not our question."""
            import os
            if not path or not os.path.exists(path):
                return None
            try:
                _cm.read_document(path)
            except (OSError, UnicodeDecodeError):
                return None
            return _real(path)

        _cm.unwritable_reason = _pre_fix_unwritable_reason
    '''
    probe, after = _run(tmp_path, canonical_bytes=UNREADABLE, sidecar=None,
                        extra_setup=bypass, worker_exit=0)
    assert after != UNREADABLE, (
        "the readability check was removed and the file SURVIVED, so the "
        "preservation tests above are not what is keeping it alive")
    assert after == CANONICAL.encode("utf-8"), (
        "the file was replaced, but not by what this export staged")
    assert not probe["error"], (
        "the bypass was expected to reproduce a silent overwrite; it errored "
        "instead, so this is not the defect those tests guard: %r"
        % probe["error"])


# --- propagation: one assessment, reported despite the file being rewritten --

def test_the_assessment_is_made_once_and_survives_the_rewrite(tmp_path):
    """The property helper tests cannot reach: the note describes the file as it
    was BEFORE the export, and still says so after the export replaced it.

    run_export replaces the canonical file with the finalized one. A report
    generated from a second look would describe the file the export just wrote,
    which always matches, so a broken implementation would print a reassuring
    line for every run. Counting the assessments and comparing the reported
    state against the PRE-export file is what distinguishes them.
    """
    counter = '''
        from chiplet_export.pipeline import chiplet_merge as _cm
        ASSESSMENT_CALLS = []
        _real_assess = _cm.assess_previous_export_content

        def _counting_assess(path):
            out = _real_assess(path)
            ASSESSMENT_CALLS.append(out.state)
            return out

        _cm.assess_previous_export_content = _counting_assess
    '''
    # An existing, readable, UNBASELINED file: the export proceeds and the
    # worker succeeds, so the file is replaced and a re-read at report time
    # would see the new content.
    probe, after = _run(tmp_path, canonical_bytes=CANONICAL.encode("utf-8"),
                        sidecar=None, extra_setup=counter,
                        staged=STAGED_REEXPORT, worker_exit=0)

    assert probe["error"] == "", "expected a successful export: %r" % probe
    assert probe["assessments"] == ["no_baseline"], (
        "expected exactly one assessment of the pre-export file, got %r"
        % (probe["assessments"],))
    # What matters is that the PRE-export bytes are gone from that path: the
    # finalized .chiplet replaced the file the guard assessed.
    assert after != CANONICAL.encode("utf-8"), (
        "the canonical file was never rewritten, so this test cannot tell a "
        "single pre-export assessment from a re-read at report time")
    assert b"x: 1.0" in after and b"finalize_required" not in after

    guard_lines = [l for l in probe["log"]
                   if "previous-export content guard" in l]
    assert len(guard_lines) == 1, (
        "the guard reported %d times for one evaluation: %r"
        % (len(guard_lines), guard_lines))
    assert "no baseline" in guard_lines[0]
    assert "WITHOUT content-guard protection" in guard_lines[0]
    assert "previous-export content guard" in probe["manifest"], (
        "the note reached the log but not MANIFEST.md, so the durable record "
        "of what was checked is missing")
    assert "no baseline" in probe["manifest"]


# --- a failed worker: the canonical file is only ever replaced on success ----

def test_a_failed_worker_leaves_the_canonical_file_as_it_was(tmp_path):
    """Through the real runner and a real subprocess exit, not a doubled result.

    The worker finalizes a staging file next to the canonical one, so when it
    exits non-zero the user's .chiplet is still the one they had, and what the
    run staged sits beside it under a name nothing loads as a product.
    """
    probe, after = _run(tmp_path, canonical_bytes=CANONICAL.encode("utf-8"),
                        sidecar=None, staged=STAGED_REEXPORT, worker_exit=1)
    assert probe["error"] == ""
    assert probe["exit_code"] == 1
    assert after == CANONICAL.encode("utf-8"), (
        "a failed worker cost the user the .chiplet they already had")
    # The digest is recorded only for a .chiplet this run put in place. One
    # recorded here would baseline the file as it is, so the next export would
    # regenerate a hand edit to it without a word.
    assert "board.chiplet.exportcontent.sha256" not in probe["listing"]
    assert "board.chiplet.unfinalized" in probe["listing"]
    assert not [name for name in probe["listing"]
                if name == ".board.chiplet.staging"]
    unfinalized = (tmp_path / "out" / "board.chiplet.unfinalized").read_text(
        encoding="utf-8")
    # Finalized by the fake before it failed, and carrying the flow: block the
    # guard moved over from the canonical file.
    assert "x: 1.0" in unfinalized and "finalize_required" not in unfinalized
    assert "flow:" in unfinalized
