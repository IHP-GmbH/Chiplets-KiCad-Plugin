# SPDX-License-Identifier: GPL-3.0-or-later
"""Temporary files that will become ordinary user-facing outputs."""

import os
import secrets


def create_output_temp(*, directory, prefix, suffix):
    """Create an exclusive sibling file with normal open-for-writing permissions.

    The kernel applies the current umask to 0666. Unlike mkstemp's private
    0600 files, this mode remains appropriate after link/replace publication.
    Never inspect or temporarily change the process-wide umask in the GUI.
    The caller owns the descriptor and removes the temporary path on failure.
    """
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(100):
        path = os.path.join(directory, prefix + secrets.token_hex(8) + suffix)
        try:
            fd = os.open(path, flags, 0o666)
        except FileExistsError:
            continue
        return fd, path
    raise FileExistsError("Could not allocate an exclusive output temporary file")
