"""Stand-in programs the operating system will actually execute.

A test that exercises the harness end to end has to hand it something runnable,
because the harness launches a subprocess. POSIX takes a shebang. Windows does
not, so the interpreter has to be named by a .cmd shim placed beside the script.

Both platforms get the same Python source, which keeps the stand-ins' behaviour
identical rather than maintaining a shell version and a batch version of each.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

WINDOWS = os.name == "nt"


def executable_python(path: Path, source: str) -> Path:
    """Write ``source`` as a runnable program and return the path to invoke.

    On Windows that path is a .cmd shim next to the script, not ``path`` itself.
    """
    if not WINDOWS:
        path.write_text(f"#!/usr/bin/env python3\n{source}", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return path

    script = path.with_name(f"{path.name}.py")
    script.write_text(source, encoding="utf-8")
    shim = path.with_name(f"{path.name}.cmd")
    # %* forwards the argument tail with its quoting intact, and cmd.exe exits
    # with the interpreter's status, which the callers assert on.
    shim.write_text(
        f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
    )
    return shim


def printing_stand_in(path: Path, output: str) -> Path:
    """A program that prints ``output`` and exits 0, whatever it is passed."""
    return executable_python(path, f"print({output!r}, end='')\n")
