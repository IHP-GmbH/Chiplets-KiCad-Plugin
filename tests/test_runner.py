# SPDX-License-Identifier: GPL-2.0-or-later
"""
Unit tests for pipeline/runner.py.

Uses ``sys.executable`` for the subprocess so the suite is portable
across CI, host, and the kicad-builder Docker image (no PATH lookups).
"""

import sys
import threading
import time
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from chiplet_kicad_plugin.pipeline import runner  # noqa: E402


PY = sys.executable


def test_echo_stdout():
    lines = []
    result = runner.run_async([PY, "-c", "print('hi')"], on_stdout=lines.append)
    assert result.exit_code == 0
    assert "hi" in result.stdout
    assert "hi" in lines


def test_stderr_capture():
    lines = []
    result = runner.run_async(
        [PY, "-c", "import sys; sys.stderr.write('err\\n')"],
        on_stderr=lines.append,
    )
    assert result.exit_code == 0
    assert "err" in result.stderr
    assert "err" in lines


def test_nonzero_exit():
    result = runner.run_async([PY, "-c", "raise SystemExit(7)"])
    assert result.exit_code == 7
    assert result.cancelled is False


def test_cancel_terminates():
    event = threading.Event()
    timer = threading.Timer(0.3, event.set)
    timer.start()
    try:
        result = runner.run_async(
            [PY, "-c", "import time\nwhile True:\n    time.sleep(0.1)"],
            cancel_event=event,
        )
    finally:
        timer.cancel()
    assert result.cancelled is True
    # The process was killed; on POSIX, terminated children report a
    # non-zero exit code (negative when signalled).
    assert result.exit_code != 0


def test_callbacks_invoked_per_line():
    captured = []
    code = (
        "import sys\n"
        "for i in range(3):\n"
        "    print('line', i)\n"
        "    sys.stdout.flush()\n"
    )
    result = runner.run_async([PY, "-c", code], on_stdout=captured.append)
    assert result.exit_code == 0
    assert captured == ["line 0", "line 1", "line 2"]


def test_callback_exception_does_not_abort():
    """A buggy callback must not silently lose output or hang the runner."""

    def boom(_):
        raise RuntimeError("boom")

    result = runner.run_async([PY, "-c", "print('ok')"], on_stdout=boom)
    assert result.exit_code == 0
    assert "ok" in result.stdout


def test_env_and_cwd_propagate(tmp_path):
    code = (
        "import os\n"
        "print(os.environ.get('CHIPLET_TEST_VAR', 'missing'))\n"
        "print(os.getcwd())\n"
    )
    result = runner.run_async(
        [PY, "-c", code],
        env={"CHIPLET_TEST_VAR": "present", "PATH": ""},
        cwd=str(tmp_path),
    )
    assert result.exit_code == 0
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "present"
    assert Path(lines[1]).resolve() == tmp_path.resolve()


def test_finishes_quickly_when_not_cancelled():
    start = time.monotonic()
    result = runner.run_async([PY, "-c", "print('quick')"])
    elapsed = time.monotonic() - start
    assert result.exit_code == 0
    # The poll interval is 50 ms; a no-op subprocess must finish well
    # under the cancel grace period (5 s).
    assert elapsed < 2.0
