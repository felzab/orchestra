"""`process`: every program the tool starts goes through it, and none opens a console window on Windows."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import LIB
from orchestra import process

# Whether the program it runs holds a console, and whether that console has a window.
REPORT = (
    "import ctypes\n"
    "kernel = ctypes.windll.kernel32\n"
    "print(kernel.GetConsoleCP() != 0, kernel.GetConsoleWindow() != 0)\n"
)
# A parent started with no console, as a hook from the desktop app may be, running REPORT through `process.run`,
# its output captured, then written to a file as the gate's is.
PARENT = (
    "import sys, tempfile\n"
    "from orchestra import process\n"
    "print(process.run((sys.executable, '-c', sys.argv[1])).stdout.decode().strip())\n"
    "print(process.run((sys.executable, '-c', sys.argv[1]), timeout=60).stdout.decode().strip())\n"
    "with tempfile.TemporaryFile() as out:\n"
    "    process.run((sys.executable, '-c', sys.argv[1]), into=out)\n"
    "    out.seek(0)\n"
    "    print(out.read().decode().strip())\n"
)


def test_a_program_started_from_a_process_with_no_console_opens_no_window() -> None:
    """Windows gives a console program a new console when its parent has none; the runner's has no window."""
    # A skip inside the case, so the type checker reads the Windows-only flag below as Windows code.
    if sys.platform != "win32":
        pytest.skip("only Windows gives a console program a window of its own")
    done = subprocess.run(
        (sys.executable, "-c", PARENT, REPORT),
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(LIB)},
        check=True,
        creationflags=subprocess.DETACHED_PROCESS,
    )
    # A console, so its own children inherit it rather than each open one, and no window on it.
    # Each arm of `process.run`: captured, timed and into a file.
    assert done.stdout.decode().split() == ["True", "False"] * 3, done.stderr


# A wrapper whose own child keeps the wrapper's output open after it ends, as `cmd.exe` leaves the
# `node` behind an npm shim: it starts the child on its own streams, notes the child's id and waits.
WRAPPER = (
    "import subprocess, sys, time\n"
    "from pathlib import Path\n"
    "sleep = (sys.executable, '-c', 'import time; time.sleep(60)')\n"
    "child = subprocess.Popen(sleep, stdout=sys.stdout, stderr=sys.stderr)\n"
    "Path(sys.argv[1]).write_bytes(str(child.pid).encode())\n"
    "time.sleep(60)\n"
)


def test_a_timed_out_program_ends_the_wait_though_its_child_holds_the_output(tmp_path: Path) -> None:
    """A preflight or a usage reading is cut off at its timeout, not when the wrapper's child lets go."""
    noted = tmp_path / "child"
    start = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            process.run((sys.executable, "-c", WRAPPER, str(noted)), timeout=2)
        took = time.monotonic() - start
        assert took < 20, took
    finally:
        os.kill(int(noted.read_bytes()), signal.SIGTERM)
