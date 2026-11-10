# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI.md is chiplet-export's reference: its flags table and the parser agree.

Every flag the parser takes has a row, every row names a flag the parser
takes, and the Default and Value cells say what the parser does. --help and a
usage error work with no KiCad (pcbnew is imported once the arguments are
parsed), and the help names the reference.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CLI_SOURCE = PLUGIN_ROOT / "chiplet_export_cli.py"
REFERENCE = PLUGIN_ROOT / "CLI.md"

sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_export.pipeline.export_exit import (  # noqa: E402
    EXIT_BOARD, EXIT_DRC_NOT_PASSED, EXIT_NOT_WRITTEN, EXIT_OK,
)

# Runs the CLI as a script with pcbnew unimportable.
_NO_KICAD = ("import runpy, sys; sys.modules['pcbnew'] = None; "
             "sys.argv = sys.argv[1:]; "
             "runpy.run_path(sys.argv[0], run_name='__main__')")


@pytest.fixture
def parser(monkeypatch):
    """The CLI's parser, built with pcbnew unimportable."""
    monkeypatch.setitem(sys.modules, "pcbnew", None)
    spec = importlib.util.spec_from_file_location("_cli_reference_under_test",
                                                  CLI_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._build_parser()


def _section(title):
    text = REFERENCE.read_text(encoding="utf-8")
    match = re.search(r"^## %s\n(.*?)(?=^## |\Z)" % re.escape(title), text,
                      re.M | re.S)
    assert match, "CLI.md has no '## %s' section" % title
    return match.group(1)


def _rows(title):
    """The table rows of a section, as lists of stripped cells."""
    rows = []
    for line in _section(title).splitlines():
        if line.startswith("|") and not re.match(r"^\|[\s:-]+\|", line):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows[1:]  # the header


def _documented():
    """{flag: (value cell, default cell)} from the Flags table."""
    flags = {}
    for row in _rows("Flags"):
        for flag in re.findall(r"`(--?[a-z][a-z-]*)`", row[0]):
            assert flag not in flags, "%s has two rows" % flag
            flags[flag] = (row[1], row[2])
    return flags


def _actions(parser):
    return {flag: action for action in parser._actions
            for flag in action.option_strings}


def test_every_parser_flag_has_a_row(parser):
    missing = sorted(set(_actions(parser)) - set(_documented()))
    assert not missing, "flags missing from CLI.md: %s" % missing


def test_every_row_names_a_parser_flag(parser):
    unknown = sorted(set(_documented()) - set(_actions(parser)))
    assert not unknown, "CLI.md documents flags the parser lacks: %s" % unknown


def test_the_cells_say_what_the_parser_does(parser):
    documented = _documented()
    for flag, action in _actions(parser).items():
        if flag in ("-h", "--help"):
            continue
        value, default = documented[flag]
        if action.required:
            assert default == "required", flag
            continue
        assert default != "required", flag
        if action.const is True:  # store_true
            assert (value, default) == ("flag", "off"), flag
        for choice in action.choices or ():
            assert "`%s`" % choice in value, (flag, choice)
        if isinstance(action.default, str) and action.default:
            assert "`%s`" % action.default in default, flag


def test_the_exit_table_is_the_tools(parser):
    codes = {int(row[0]) for row in _rows("Exit codes")}
    assert codes == {EXIT_OK, EXIT_NOT_WRITTEN, EXIT_BOARD,
                     EXIT_DRC_NOT_PASSED}


def _run_without_kicad(*argv):
    return subprocess.run(
        [sys.executable, "-c", _NO_KICAD, str(CLI_SOURCE), *argv],
        capture_output=True, text=True, timeout=60, check=False)


def test_help_needs_no_kicad_and_names_the_reference():
    result = _run_without_kicad("--help")
    assert result.returncode == 0, result.stderr
    assert "--board" in result.stdout
    assert ("reference: %s" % REFERENCE) in result.stdout
    assert REFERENCE.is_file()


def test_a_usage_error_needs_no_kicad():
    result = _run_without_kicad("--no-such-flag")
    assert result.returncode == EXIT_BOARD
    assert "usage: chiplet-export" in result.stderr
    assert "pcbnew" not in result.stderr


def test_the_readme_leads_to_the_reference():
    head = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    head = head.split("\n## ", 1)[0]
    assert "(CLI.md)" in head
