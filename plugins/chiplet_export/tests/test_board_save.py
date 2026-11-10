# SPDX-License-Identifier: GPL-3.0-or-later
"""What a KiCad GUI save keeps of a board and a sheet the flow wrote.

A board-root ``(property ...)`` is a cache of the project's text variables:
every GUI save rewrites the root from the ``.kicad_pro`` (see
kicad_save_emulation.py). A root key survives only when the project's
``text_variables`` hold it with the same value. Footprints, their fields, the
nets and the outline are written back as they were read, and those are what a
board revision compares.

A sheet's title block has no such cache: eeschema writes it back as it read
it. Comment 1 carries the digest of the .chiplet the sheet was drawn from, and
the schematic reader takes it from the netlist kicad-cli exports. The GUI save
(SCH_EDIT_FRAME::saveSchematicFile) and ``kicad-cli sch upgrade --force`` both
load and save with the same SCH_IO_KICAD_SEXPR, so the second stands for the
first.

The board save runs in a child process with an empty HOME, under a Python that
imports pcbnew: this interpreter when it does and kicad-cli is on PATH (the
KiCad image), else the AppDir named by KICAD_APPDIR (its
usr/lib/python3/dist-packages, usr/lib and usr/bin/kicad-cli), run with
KICAD_APPDIR_PYTHON (default python3, the version the AppDir was built for).
Without either, the module skips.

CHIPLET_BOARD_SAVE_INPUT names a board the flow wrote (with its .kicad_pro
beside it, if any); the opt-in case saves a copy and asks every root key it
carries to come back.
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "flow_draft_board" / "cf_padring_die_cmos5l.draft.kicad_pcb"
SHEET = HERE / "fixtures" / "flow_draft_board" / "cf_padring_die_cmos5l.draft.kicad_sch"
EMULATION = HERE / "kicad_save_emulation.py"
BASE_DIGEST = ("chiplet sha256 "
               "9e43a3d003ddd2e77f694aaadac7365e5225e40b85f3b2083b4b4e33112dc17d")

# The one key the plugin reads from the board root (chiplet_writer
# _lookup_property: board properties first, project text variables second).
# Every other setting it reads is a project text variable or the environment.
ROOT_KEY = "INTERPOSER_LYP"
ROOT_VALUE = "${INTERPOSER_PDK_ROOT}/libs.tech/klayout/tech/intm4tm2.lyp"


def _kicad_tools():
    """(python, its extra env, kicad-cli), or Nones."""
    cli = shutil.which("kicad-cli")
    if importlib.util.find_spec("pcbnew") is not None and cli:
        return sys.executable, {}, cli
    appdir = os.environ.get("KICAD_APPDIR")
    if appdir:
        usr = Path(appdir) / "usr"
        dist = usr / "lib" / "python3" / "dist-packages"
        cli = usr / "bin" / "kicad-cli"
        if (dist / "pcbnew.py").is_file() and cli.is_file():
            return (os.environ.get("KICAD_APPDIR_PYTHON", "python3"),
                    {"PYTHONPATH": str(dist), "LD_LIBRARY_PATH": str(usr / "lib")},
                    str(cli))
    return None, None, None


PYTHON, PYTHON_ENV, KICAD_CLI = _kicad_tools()
if PYTHON is None:
    pytest.skip("needs pcbnew (KiCad Python); on a host, KICAD_APPDIR "
                "names a KiCad AppDir (pcbnew and kicad-cli)", allow_module_level=True)


def _child_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    for key, value in PYTHON_ENV.items():
        env[key] = os.pathsep.join(filter(None, (value, os.environ.get(key))))
    env.update(HOME=str(home), KICAD_CONFIG_HOME=str(home / "kicad"),
               PYTHONDONTWRITEBYTECODE="1")
    return env


def _save(tmp_path, board, project_text_vars=None):
    """Ctrl+S on ``board`` (already in tmp_path), in a child with an empty HOME."""
    if project_text_vars is not None:
        board.with_suffix(".kicad_pro").write_text(json.dumps(
            {"meta": {"filename": board.with_suffix(".kicad_pro").name, "version": 1},
             "text_variables": project_text_vars}, indent=2), encoding="utf-8")
    report = tmp_path / "report.json"
    proc = subprocess.run([PYTHON, "-B", str(EMULATION), str(board), str(report)],
                          env=_child_env(tmp_path), capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(report.read_text(encoding="utf-8"))


def _with_root_property(text, key, value):
    """The board text with one root property, where KiCad writes them."""
    at = text.index("\n  (net ")
    return text[:at + 1] + '  (property "%s" "%s")\n' % (key, value) + text[at + 1:]


def _copy(tmp_path, text=None):
    board = tmp_path / "board" / "cf_padring_die_cmos5l.draft.kicad_pcb"
    board.parent.mkdir()
    board.write_text(FIXTURE.read_text(encoding="utf-8") if text is None else text,
                     encoding="utf-8")
    return board


def _layout(observation):
    return {k: observation[k] for k in ("nets", "outline", "footprints")}


def test_a_gui_save_keeps_every_footprint_net_and_outline(tmp_path):
    report = _save(tmp_path, _copy(tmp_path))
    before, after = report["before"]["observation"], report["after"]["observation"]
    assert len(before["footprints"]) == 35
    assert _layout(after) == _layout(before)


def test_the_flow_board_has_no_root_property_to_lose(tmp_path):
    report = _save(tmp_path, _copy(tmp_path))
    assert report["before"]["root_properties"] == {}
    assert report["after"]["root_properties"] == {}


def test_a_root_property_mirrored_in_the_project_survives(tmp_path):
    board = _copy(tmp_path, _with_root_property(FIXTURE.read_text(encoding="utf-8"),
                                                ROOT_KEY, ROOT_VALUE))
    report = _save(tmp_path, board, {ROOT_KEY: ROOT_VALUE})
    assert report["before"]["root_properties"] == {ROOT_KEY: ROOT_VALUE}
    assert report["after"]["root_properties"] == {ROOT_KEY: ROOT_VALUE}


def test_a_root_property_the_project_lacks_is_lost(tmp_path):
    # The negative control: a save that never synchronizes keeps the key, and
    # the case above would pass for the wrong reason.
    board = _copy(tmp_path, _with_root_property(FIXTURE.read_text(encoding="utf-8"),
                                                ROOT_KEY, ROOT_VALUE))
    report = _save(tmp_path, board)
    assert report["before"]["root_properties"] == {ROOT_KEY: ROOT_VALUE}
    assert report["after"]["root_properties"] == {}
    assert report["project_text_variables"] == {}


def test_the_project_value_replaces_a_different_root_value(tmp_path):
    board = _copy(tmp_path, _with_root_property(FIXTURE.read_text(encoding="utf-8"),
                                                ROOT_KEY, ROOT_VALUE))
    report = _save(tmp_path, board, {ROOT_KEY: "/elsewhere.lyp"})
    assert report["after"]["root_properties"] == {ROOT_KEY: "/elsewhere.lyp"}


def test_a_board_the_flow_wrote_keeps_its_root_keys(tmp_path):
    source = os.environ.get("CHIPLET_BOARD_SAVE_INPUT")
    if not source:
        pytest.skip("CHIPLET_BOARD_SAVE_INPUT unset")
    source = Path(source)
    board = _copy(tmp_path, source.read_text(encoding="utf-8"))
    project = source.with_suffix(".kicad_pro")
    if project.is_file():
        shutil.copyfile(project, board.with_suffix(".kicad_pro"))
    report = _save(tmp_path, board)
    lost = {k: v for k, v in report["before"]["root_properties"].items()
            if report["after"]["root_properties"].get(k) != v}
    assert not lost, ("a GUI save drops or rewrites these root keys; mirror them "
                      "into the .kicad_pro text_variables: %s" % sorted(lost))
    assert (_layout(report["after"]["observation"])
            == _layout(report["before"]["observation"]))


def _cli(tmp_path, *args):
    proc = subprocess.run([KICAD_CLI, *args], env=_child_env(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _saved_sheet_comment_1(tmp_path, text):
    """Save a copy of ``text`` as eeschema does; comment 1 as the netlist has it.

    Returns (generator after the save, comment 1 of the root sheet or None).
    """
    sheet = tmp_path / "sheet" / "cf_padring_die_cmos5l.draft.kicad_sch"
    sheet.parent.mkdir()
    sheet.write_text(text, encoding="utf-8")
    _cli(tmp_path, "sch", "upgrade", "--force", str(sheet))
    generator = re.search(r'^\s*\(generator "([^"]*)"\)', sheet.read_text(encoding="utf-8"),
                          re.M).group(1)
    netlist = tmp_path / "sheet" / "cf_padring_die_cmos5l.net"
    _cli(tmp_path, "sch", "export", "netlist", "--format", "kicadsexpr",
         "-o", str(netlist), str(sheet))
    root = re.search(r'\(sheet\s+\(number "1"\)\s+\(name "/"\)(.*?)\n\t\t\)\n',
                     netlist.read_text(encoding="utf-8"), re.S)
    assert root, "the netlist has no root sheet"
    comment = re.search(r'\(comment\s+\(number "1"\)\s+\(value "((?:[^"\\]|\\.)*)"\)',
                        root.group(1))
    return generator, comment.group(1) if comment else None


def test_a_sheet_save_keeps_the_base_digest_in_comment_1(tmp_path):
    text = SHEET.read_text(encoding="utf-8")
    assert '(generator "chiplet_to_kicad_sch")' in text
    generator, comment = _saved_sheet_comment_1(tmp_path, text)
    # The file was written by eeschema, not left as the generator wrote it.
    assert generator == "eeschema"
    assert comment == BASE_DIGEST


def test_a_sheet_without_the_digest_reads_without_it(tmp_path):
    # The control: no save path drops comment 1, so show that the read above
    # would see it gone.
    text = SHEET.read_text(encoding="utf-8")
    line = '\t\t(comment 1 "%s")\n' % BASE_DIGEST
    assert text.count(line) == 1
    generator, comment = _saved_sheet_comment_1(tmp_path, text.replace(line, ""))
    assert generator == "eeschema"
    # The netlist lists every comment number, an empty one with value "".
    assert comment == ""
