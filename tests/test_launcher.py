"""bin/orchestra and `orchestra.__main__`, run through `sh` beside a stand-in command line."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "orchestra"
# The launcher's variables, which every program the tool starts must see as the caller left them.
LAUNCHED = ("PYTHONPATH", "MSYS_NO_PATHCONV", "MSYS2_ARG_CONV_EXCL")
# Imports a standard module the working directory shadows and a module only the caller's PYTHONPATH
# holds, then prints the arguments and the environment a program it started would get.
STAND_IN = (
    "import json, os, sys, kept\n"
    "def main(argv=None):\n"
    "    print('stand-in', list(argv), kept.NAME)\n"
    f"    print(json.dumps({{name: os.environ.get(name) for name in {LAUNCHED!r}}}))\n"
    "    return 0\n"
)


def plugin(tmp_path: Path) -> Path:
    """Copy the launcher and the real entry point beside a stand-in `cli`; the copied launcher."""
    (tmp_path / "bin").mkdir()
    launcher = tmp_path / "bin" / "orchestra"
    shutil.copyfile(LAUNCHER, launcher)
    package = tmp_path / "lib" / "orchestra"
    package.mkdir(parents=True)
    for name in ("__init__.py", "__main__.py", "hooks.py"):
        shutil.copyfile(ROOT / "lib" / "orchestra" / name, package / name)
    (package / "cli.py").write_bytes(STAND_IN.encode())
    return launcher


def stub(directory: Path, marker: Path) -> None:
    """Write a `python3` that fails as Windows' Store alias does, its 9009 read as 49, leaving a marker."""
    directory.mkdir()
    script = directory / "python3"
    script.write_bytes(f"#!/bin/sh\n: > '{marker.as_posix()}'\nexit 49\n".encode())
    script.chmod(0o755)


def shell_form(path: Path) -> str:
    """The path as Git Bash's PATH names it, `/c/...`, where cygpath exists; else its POSIX form."""
    cygpath = shutil.which("cygpath")
    if cygpath is None:
        return path.as_posix()
    return subprocess.run([cygpath, "-u", str(path)], capture_output=True, check=True).stdout.decode().strip()


def run(
    launcher: Path,
    path: list[Path],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    args: tuple[str, ...] = ("status", "two words"),
) -> subprocess.CompletedProcess[str]:
    """Run the launcher by its shell form through sh, with only `path` on PATH and `env` added."""
    sh = shutil.which("sh")
    assert sh, "no sh on PATH"
    held = {k: v for k, v in os.environ.items() if k not in LAUNCHED}
    held["PATH"] = os.pathsep.join(str(p) for p in path)
    return subprocess.run(
        [sh, shell_form(launcher), *args],
        cwd=cwd,
        env={**held, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def kept_module(tmp_path: Path) -> dict[str, str]:
    """A folder holding `kept.py`, as the caller's PYTHONPATH names it."""
    kept = tmp_path / "kept"
    kept.mkdir()
    (kept / "kept.py").write_bytes(b"NAME = 'kept'\n")
    return {"PYTHONPATH": str(kept)}


def test_skips_a_failing_python3_and_hands_the_callers_environment_on(tmp_path: Path) -> None:
    launcher = plugin(tmp_path)
    marker = tmp_path / "stub-ran"
    stub(tmp_path / "stub", marker)
    project = tmp_path / "project"
    project.mkdir()
    (project / "json.py").write_bytes(b"print('decoy')\n")
    caller = kept_module(tmp_path)

    interpreter = Path(sys.executable).parent
    # The platform's own form, as a Windows user's environment holds it; with `OS` empty the
    # launcher tries `python3` first, as it does everywhere but Windows.
    result = run(launcher, [tmp_path / "stub", interpreter], project, env={**caller, "OS": ""})

    assert result.returncode == 0, result.stderr
    assert marker.exists()
    said, environment = result.stdout.splitlines()
    assert said == "stand-in ['status', 'two words'] kept"
    # A gate or a landing step the tool starts sees the caller's values, never the launcher's.
    assert json.loads(environment) == {
        "PYTHONPATH": caller["PYTHONPATH"],
        "MSYS_NO_PATHCONV": None,
        "MSYS2_ARG_CONV_EXCL": None,
    }


def test_names_the_missing_interpreter_and_blocks_no_hook(tmp_path: Path) -> None:
    launcher = plugin(tmp_path)
    stub(tmp_path / "stub", tmp_path / "stub-ran")

    result = run(launcher, [tmp_path / "stub"], tmp_path)
    assert result.returncode == 2
    assert "no Python 3.11 or newer found" in result.stderr
    # A hook's exit 2 blocks the prompt or the tool call in every session of the project.
    hooked = run(launcher, [tmp_path / "stub"], tmp_path, args=("hook", "UserPromptSubmit"))
    assert (hooked.returncode, hooked.stdout) == (0, "")
    assert "no Python 3.11 or newer answered the hook: python3 exited 49 " in hooked.stderr


def test_hands_an_argument_opening_with_a_slash_over_as_typed(tmp_path: Path) -> None:
    """Git Bash rewrites `/x/y` into `X:/y` for a native Python unless told not to."""
    launcher = plugin(tmp_path)
    caller = kept_module(tmp_path)
    interpreter = [Path(sys.executable).parent]

    result = run(launcher, interpreter, tmp_path, env=caller, args=("ci", "/x/y", "--match=/a/b"))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "stand-in ['ci', '/x/y', '--match=/a/b'] kept"
    # A shell already exporting the switch: Git Bash must still convert lib/'s `/c/...` path, and its
    # own value reaches what the tool starts.
    preset = run(launcher, interpreter, tmp_path, env={**caller, "MSYS_NO_PATHCONV": "1"})
    assert preset.returncode == 0, preset.stderr
    assert json.loads(preset.stdout.splitlines()[1])["MSYS_NO_PATHCONV"] == "1"
