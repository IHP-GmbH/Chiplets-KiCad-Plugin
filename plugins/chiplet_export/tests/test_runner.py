# SPDX-License-Identifier: GPL-3.0-or-later
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

from chiplet_export.pipeline import runner  # noqa: E402


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


def test_timeout_terminates():
    start = time.monotonic()
    result = runner.run_async([PY, "-c", "import time; time.sleep(30)"],
                              timeout=0.3)
    assert result.timed_out is True
    assert result.cancelled is False
    assert result.exit_code != 0
    assert time.monotonic() - start < runner.GRACE_SECONDS + 3


def test_a_run_inside_its_timeout_is_not_timed_out():
    result = runner.run_async([PY, "-c", "print('ok')"], timeout=30)
    assert (result.exit_code, result.stdout, result.timed_out) == (
        0, "ok\n", False)


def test_the_child_reads_no_stdin_of_the_caller():
    """The caller's stdin is a pipe with data in it; the child sees EOF."""
    import subprocess
    caller = (
        "import sys; sys.path.insert(0, %r)\n"
        "from chiplet_export.pipeline import runner\n"
        "run = runner.run_async([sys.executable, '-c',"
        " 'import sys; print(repr(sys.stdin.read()))'], timeout=30)\n"
        "print(run.stdout, end='')\n" % str(PLUGIN_ROOT.parent))
    proc = subprocess.run([PY, "-c", caller], input="leaked\n",
                          capture_output=True, text=True, timeout=60)
    assert proc.stdout == "''\n", proc.stderr


def test_an_interrupt_stops_the_child_before_it_propagates(monkeypatch):
    """A KeyboardInterrupt can land anywhere in the wait loop; this one lands
    at the cancel check. It must reach the caller, but not with the child
    still running, or the caller's clean-up races a process that is still
    writing its outputs."""
    started = []
    real_popen = runner.subprocess.Popen

    def popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        started.append(proc)
        return proc

    class InterruptedWait:
        def is_set(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    try:
        with pytest.raises(KeyboardInterrupt):
            runner.run_async(
                [PY, "-c", "import time\nwhile True:\n    time.sleep(0.1)"],
                cancel_event=InterruptedWait(),
            )
        assert started[0].poll() is not None, (
            "the interrupt propagated with the child still running")
    finally:
        for proc in started:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


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
