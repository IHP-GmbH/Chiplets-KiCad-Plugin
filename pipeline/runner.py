# SPDX-License-Identifier: GPL-2.0-or-later
"""
Asynchronous subprocess runner for the chiplet export pipeline.

Wraps ``subprocess.Popen`` with two reader threads so stdout and
stderr stream line-by-line into caller-provided callbacks. The runner
honours a ``threading.Event`` for cancel: when set, the process gets
SIGTERM, and if it has not exited after a brief grace period it gets
SIGKILL.

Pure stdlib; importable on any Python with no KiCad or wx dependency,
so the test suite runs with stock pytest.
"""

import subprocess
import threading
import time
from dataclasses import dataclass


GRACE_SECONDS = 5.0
_POLL_INTERVAL = 0.05


@dataclass
class RunResult:
    """Outcome of a single ``run_async`` invocation."""

    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    cancelled: bool = False


def _pump(stream, sink, on_line):
    """Read `stream` line-by-line, append to sink, fire callback.

    A buggy callback must never abort the pump, otherwise the child
    process can block on a full pipe.
    """
    try:
        for raw in iter(stream.readline, ""):
            if not raw:
                break
            sink.append(raw)
            if on_line is not None:
                try:
                    on_line(raw.rstrip("\n"))
                except Exception:
                    pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _terminate(proc):
    """Best-effort SIGTERM -> wait -> SIGKILL."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    deadline = time.monotonic() + GRACE_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(_POLL_INTERVAL)
    try:
        proc.kill()
    except Exception:
        pass


def run_async(cmd, on_stdout=None, on_stderr=None, cancel_event=None,
              env=None, cwd=None):
    """Spawn `cmd`, streaming stdout/stderr until completion or cancel.

    Args:
        cmd: List of arguments (no shell). First element is the executable.
        on_stdout: Optional callable(str). Invoked once per stdout line
            with the trailing newline stripped.
        on_stderr: Optional callable(str). Invoked once per stderr line.
        cancel_event: Optional ``threading.Event``. If set during the
            run, the process is terminated.
        env: Optional environment dict passed to ``Popen``.
        cwd: Optional working directory for the subprocess.

    Returns:
        ``RunResult`` with exit code, captured streams, and whether
        cancellation triggered termination.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    stdout_lines = []
    stderr_lines = []
    out_thread = threading.Thread(
        target=_pump, args=(proc.stdout, stdout_lines, on_stdout),
        daemon=True,
    )
    err_thread = threading.Thread(
        target=_pump, args=(proc.stderr, stderr_lines, on_stderr),
        daemon=True,
    )
    out_thread.start()
    err_thread.start()

    cancelled = False
    while True:
        if (cancel_event is not None and cancel_event.is_set()
                and proc.poll() is None):
            cancelled = True
            _terminate(proc)
            break
        if proc.poll() is not None:
            break
        time.sleep(_POLL_INTERVAL)

    out_thread.join(timeout=GRACE_SECONDS)
    err_thread.join(timeout=GRACE_SECONDS)

    exit_code = proc.poll()
    if exit_code is None:
        exit_code = -1

    return RunResult(
        exit_code=exit_code,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
        cancelled=cancelled,
    )
