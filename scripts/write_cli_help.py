# SPDX-License-Identifier: GPL-3.0-or-later
"""Write each command's --help into docs/reference/, the text adk-docs includes.

The parsers are the single source; nothing in docs/reference/ is edited by
hand. tests/test_docs_reference.py fails when a committed file and its parser
disagree, and names this script.

Each --help runs in a child with COLUMNS pinned and colour off, so the text
does not depend on the terminal, and with pcbnew unimportable, as on a host
without KiCad. chiplet-export prints the absolute path of its reference; the
plugin directory in it is written relative to the repository.

usage: python3 scripts/write_cli_help.py [--check]
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "chiplet_export"
OUT = REPO / "docs" / "reference"

#: (file under docs/reference, script under plugins/chiplet_export)
COMMANDS = [
    ("chiplet-export.txt", "chiplet_export_cli.py"),
    ("board-snapshot.txt", "board_snapshot_cli.py"),
    ("hyp-to-gds.txt", "hyp_to_gds.py"),
    ("connection-methods.txt", "connection_methods.py"),
]

# Runs the script as `python3 script --help` does (its directory first on
# sys.path), with pcbnew unimportable.
_NO_KICAD = ("import os, runpy, sys; sys.modules['pcbnew'] = None; "
             "sys.argv = sys.argv[1:]; "
             "sys.path.insert(0, os.path.dirname(sys.argv[0])); "
             "runpy.run_path(sys.argv[0], run_name='__main__')")


def help_text(script):
    env = dict(os.environ, COLUMNS="80", LINES="25", NO_COLOR="1",
               PYTHON_COLORS="0", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", _NO_KICAD, str(PLUGIN / script),
                           "--help"], env=env, capture_output=True, text=True,
                          timeout=60)
    if proc.returncode != 0:
        raise SystemExit("%s --help exited %d:\n%s" % (script, proc.returncode,
                                                       proc.stderr))
    return proc.stdout.replace(str(PLUGIN), "plugins/chiplet_export")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="write nothing; exit 1 if a file is stale")
    args = parser.parse_args(argv)
    stale = []
    for name, script in COMMANDS:
        text = help_text(script)
        path = OUT / name
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
            continue
        OUT.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if stale:
        print("stale: %s (run scripts/write_cli_help.py)" % ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
