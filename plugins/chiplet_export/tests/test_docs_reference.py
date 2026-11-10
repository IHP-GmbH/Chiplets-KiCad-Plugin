# SPDX-License-Identifier: GPL-3.0-or-later
"""docs/reference/ holds each command's --help as its parser prints it.

adk-docs includes those files as they are, so a flag added to a parser and
not to them would be missing from the published reference. They are written
by scripts/write_cli_help.py; when a case here fails, run it and commit the
result.

Python 3.13 changed how argparse lays out an option with a short and a long
name; the files are written under 3.11 and 3.12, which agree.
"""

import importlib.util
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "write_cli_help.py"

if sys.version_info >= (3, 13):
    pytest.skip("argparse lays out --help differently from Python 3.13; "
                "docs/reference/ is written under 3.11 and 3.12",
                allow_module_level=True)


def _writer():
    spec = importlib.util.spec_from_file_location("_write_cli_help_under_test",
                                                  SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WRITER = _writer()


@pytest.mark.parametrize("name,script", WRITER.COMMANDS,
                         ids=[name for name, _ in WRITER.COMMANDS])
def test_the_committed_help_is_the_parsers(name, script):
    committed = (WRITER.OUT / name).read_text(encoding="utf-8")
    assert committed == WRITER.help_text(script), (
        "docs/reference/%s is stale; run scripts/write_cli_help.py" % name)


@pytest.mark.parametrize("name", [name for name, _ in WRITER.COMMANDS])
def test_the_reference_carries_no_host_path(name):
    text = (WRITER.OUT / name).read_text(encoding="utf-8")
    assert str(REPO) not in text
    assert "/home/" not in text


def test_every_reference_file_has_a_command():
    written = {name for name, _ in WRITER.COMMANDS}
    assert {p.name for p in WRITER.OUT.iterdir()} == written

