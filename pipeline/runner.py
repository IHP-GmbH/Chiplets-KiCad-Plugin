# SPDX-License-Identifier: GPL-3.0-or-later
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

import queue
import subprocess
import threading
import time
from dataclasses import dataclass


GRACE_SECONDS = 5.0
_POLL_INTERVAL = 0.05

# Enqueued by a pump when its stream closes, telling the matching dispatcher
# thread to stop.
_SENTINEL = object()


@dataclass
class RunResult:
    """Outcome of a single ``run_async`` invocation."""

    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    cancelled: bool = False


def _pump(stream, sink, line_queue):
    """Drain `stream` line-by-line into `sink` and `line_queue`.

    The pump never calls user code: it only appends to the capture list and
    enqueues each line for a separate dispatcher thread. A slow or blocking
    ``on_line`` callback therefore cannot back-pressure the child's pipe and
    deadlock the run.
    """
    try:
        for raw in iter(stream.readline, ""):
            if not raw:
                break
            sink.append(raw)
            line_queue.put(raw.rstrip("\n"))
    finally:
        try:
            stream.close()
        except Exception:
            pass
        line_queue.put(_SENTINEL)


def _dispatch(line_queue, on_line):
    """Deliver queued lines to `on_line` until the sentinel arrives.

    Runs on its own thread so callback latency is decoupled from the pipe
    drain. A buggy callback never aborts delivery or loses captured output.
    """
    while True:
        line = line_queue.get()
        if line is _SENTINEL:
            return
        if on_line is not None:
            try:
                on_line(line)
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
    out_queue = queue.Queue()
    err_queue = queue.Queue()
    threads = [
        threading.Thread(target=_pump,
                         args=(proc.stdout, stdout_lines, out_queue),
                         daemon=True),
        threading.Thread(target=_pump,
                         args=(proc.stderr, stderr_lines, err_queue),
                         daemon=True),
        threading.Thread(target=_dispatch, args=(out_queue, on_stdout),
                         daemon=True),
        threading.Thread(target=_dispatch, args=(err_queue, on_stderr),
                         daemon=True),
    ]
    for t in threads:
        t.start()

    # Block in the kernel on proc.wait (cancel-responsive via the short
    # timeout) instead of spinning on a sleep poll.
    cancelled = False
    while True:
        if (cancel_event is not None and cancel_event.is_set()
                and proc.poll() is None):
            cancelled = True
            _terminate(proc)
            break
        try:
            proc.wait(timeout=_POLL_INTERVAL)
            break
        except subprocess.TimeoutExpired:
            continue

    # Pumps end when the pipes close (process exit / terminate) and enqueue a
    # sentinel that ends each dispatcher. Bound the total wait by one shared
    # GRACE_SECONDS deadline rather than GRACE_SECONDS per thread.
    join_deadline = time.monotonic() + GRACE_SECONDS
    for t in threads:
        t.join(timeout=max(0.0, join_deadline - time.monotonic()))

    exit_code = proc.poll()
    if exit_code is None:
        exit_code = -1

    return RunResult(
        exit_code=exit_code,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
        cancelled=cancelled,
    )
