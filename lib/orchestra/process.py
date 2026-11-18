"""Every program orchestra starts, started here so that none opens a console window on Windows.

A hook Claude Code's desktop app starts may run with no console of its own, and Windows gives a
console program started from such a process, git among them, a new console and its window, which
flashes on the screen. `CREATE_NO_WINDOW` runs it with a console that has no window, and the
programs it starts in turn inherit that console (Win32's "Process Creation Flags"). A stream left
unredirected writes to that console and is lost, so every caller redirects each stream it reads.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO

# Elsewhere the flag does not exist, and a process starts no window.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
# A detached process has no console at all, which Windows' flags rule out beside `CREATE_NO_WINDOW`;
# its own process group keeps a Ctrl+C meant for the caller from reaching it.
DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0


def run(
    argv: Sequence[str], *, cwd: Path | None = None, timeout: float | None = None, into: IO[bytes] | None = None
) -> subprocess.CompletedProcess[bytes]:
    """The program run to its end, whatever its exit, its output captured.

    With `into`, its output and its errors go to that file instead, in the order written, and it reads
    no input: a long run such as the gate, whose log is read once it ends.
    """
    if into is None and timeout is None:
        return subprocess.run(argv, cwd=cwd, capture_output=True, check=False, creationflags=NO_WINDOW)
    if into is None:
        # Captured in files, not pipes: on Windows a timed-out run, once its program is killed, reads the
        # pipes to their end with no timeout, and a child a wrapper started, such as the `node` behind an
        # npm shim's `cmd.exe`, holds them open. A file's wait ends with the program itself
        # (python/cpython#87512, open: re-check this once it is fixed).
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            done = subprocess.run(
                argv, cwd=cwd, timeout=timeout, stdout=out, stderr=err, check=False, creationflags=NO_WINDOW
            )
            out.seek(0)
            err.seek(0)
            return subprocess.CompletedProcess(done.args, done.returncode, out.read(), err.read())
    return subprocess.run(
        argv,
        cwd=cwd,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        stdout=into,
        stderr=subprocess.STDOUT,
        check=False,
        creationflags=NO_WINDOW,
    )


def detach(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> None:
    """The program started in a process that outlives the caller and holds none of its streams."""
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=cwd,
        creationflags=DETACHED,
        start_new_session=sys.platform != "win32",
    )
