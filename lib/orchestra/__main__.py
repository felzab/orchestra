"""`python -P -m orchestra`, with the plugin's `lib` folder on PYTHONPATH, as `bin/orchestra` runs a verb.

A hook's launcher program calls `main` itself.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import MutableMapping, Sequence
from typing import Protocol, cast

from orchestra import hooks

# The variables the launcher sets or clears for this process alone: Git Bash's two switches handing
# every argument over as typed, and the path to `lib`. Each `ORCHESTRA_CALLER_<name>` holds the
# caller's own value after an `=`, or nothing where the caller had none.
LAUNCHED = ("PYTHONPATH", "MSYS_NO_PATHCONV", "MSYS2_ARG_CONV_EXCL")
CALLER = "ORCHESTRA_CALLER_"


def caller_environment(environ: MutableMapping[str, str]) -> None:
    """The caller's own values of the launcher's variables put back, so every program this one starts gets them.

    A gate or a landing step run under the launcher's would differ from the same command typed by
    hand: a Git Bash script would hand its programs unconverted paths, and a project's Python would
    import the plugin's `lib` first. Nothing changes where the launcher did not run this process.
    """
    for name in LAUNCHED:
        held = environ.pop(CALLER + name, None)
        if held is None:
            continue
        if held:
            environ[name] = held.removeprefix("=")
        else:
            environ.pop(name, None)


class Cli(Protocol):
    def main(self, argv: Sequence[str] | None = None) -> int: ...


def main(argv: list[str]) -> int:
    caller_environment(os.environ)
    if len(argv) == len(("hook", "<event>")) and argv[0] == "hook":
        return hooks.main(argv[1])
    # Imported here: every hook would pay for building the whole command line.
    return cast("Cli", importlib.import_module("orchestra.cli")).main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
