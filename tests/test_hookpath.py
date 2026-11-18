"""The cost a hook pays in a session with the plugin enabled: the Python starts the launcher makes for it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import LIB, git, inprocess
from orchestra import store
from test_launcher import LAUNCHED, LAUNCHER, plugin, shell_form

# A dispatch of one of the plugin's agents, which the hook refuses outside a fleet: an answer only a
# hook that reached `events` prints.
DISPATCH = {"tool_name": "Agent", "tool_input": {"prompt": "W", "subagent_type": "orchestra:writer"}}
# One `Stop` answered, then whether it took the fleet path, which imports `events`.
FLEET_PATH = "import sys\nfrom orchestra import hooks\nprint(hooks.main('Stop'), 'orchestra.events' in sys.modules)\n"


def names(folder: Path, **exits: int | None) -> Path:
    """Write a `sh` script for each name, logging its start: None runs this Python, a number exits with it."""
    folder.mkdir()
    real = shell_form(Path(sys.executable))
    for name, code in exits.items():
        then = f'exec "{real}" "$@"' if code is None else f"exit {code}"
        script = folder / name
        script.write_bytes(f"#!/bin/sh\necho {name} >> '{shell_form(folder / 'started')}'\n{then}\n".encode())
        script.chmod(0o755)
    return folder


def hook(
    path: Path, cwd: Path, *, event: str = "PreToolUse", launcher: Path = LAUNCHER, more: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    """`orchestra hook <event>` for a dispatch, through sh with only `path` on PATH, no `OS` and `more` set."""
    data = {"session_id": "s", "cwd": str(cwd), "hook_event_name": event, **DISPATCH}
    return launched(path, cwd, json.dumps(data).encode(), (launcher, "hook", event), more)


def launched(
    path: Path, cwd: Path, stdin: bytes, command: tuple[Path, str, str], more: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    """The launcher run with its arguments on `stdin`, through sh with only `path` on PATH, no `OS` and `more` set."""
    sh = shutil.which("sh")
    assert sh, "no sh on PATH"
    # A cache prefix of the developer's, or a plugin data folder, would move the plugin's bytecode.
    moved = {"PYTHONPYCACHEPREFIX", "CLAUDE_PLUGIN_DATA"}
    env = {k: v for k, v in os.environ.items() if k not in {*LAUNCHED, "OS", *moved}}
    env |= {"PATH": str(path), "MSYS_NO_PATHCONV": "1"} | (more or {})
    launcher, *args = command
    return subprocess.run(
        [sh, shell_form(launcher), *args],
        cwd=cwd,
        input=stdin,
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )


def answered(done: subprocess.CompletedProcess[bytes]) -> object:
    """The decision the hook printed, its exit code having been 0."""
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"]


@pytest.mark.parametrize(("platform", "first"), [("Windows_NT", "python"), (None, "python3")])
def test_a_hook_starts_one_python_by_the_platforms_first_name(tmp_path: Path, platform: str | None, first: str) -> None:
    """Windows' Store alias `python3` is never started there; elsewhere `python` may be a Python 2.

    The working directory's `json.py` would shadow the standard module the hook reads its input with,
    and a caller's switch turning Git Bash's path conversion off would hide `lib/` from the Python.
    """
    path = names(tmp_path / "names", python=None, python3=None)
    project = tmp_path / "project"
    project.mkdir()
    (project / "json.py").write_bytes(b"print('decoy')\n")

    assert answered(hook(path, project, more={"OS": platform} if platform else None)) == "deny"
    assert (path / "started").read_bytes().split() == [first.encode()]


def test_a_hook_passes_a_python_too_old_to_run_it(tmp_path: Path) -> None:
    """A `python3` below 3.11 exits 86 from the launcher's own check, and the next name answers the hook.

    Which codes count as a Python that never ran changes nothing here, since the next name answers either
    way, so each code is held below, where no next name answers.
    """
    path = names(tmp_path / "names", python3=86, python=None)

    assert answered(hook(path, tmp_path)) == "deny"
    assert (path / "started").read_bytes().split() == [b"python3", b"python"]


@pytest.mark.parametrize("code", [None, 86, 49, 126, 127], ids=["none found", "86", "49", "126", "127"])
def test_a_hook_no_python_ran_answers_nothing_and_blocks_no_call(tmp_path: Path, code: int | None) -> None:
    """No Python ran the hook, so no state was read and nothing is guarded: exit 0, never a block of every call."""
    path = names(tmp_path / "names") if code is None else names(tmp_path / "names", python3=code)

    done = hook(path, tmp_path)

    assert (done.returncode, done.stdout) == (0, b"")
    said = "none was found" if code is None else f"python3 exited {code}"
    assert f"orchestra: no Python 3.11 or newer answered the hook: {said}".encode() in done.stderr


def crashing(tmp_path: Path, fault: bytes) -> Path:
    """A copy of the plugin whose hook meets `fault` once it has read its input; the copied launcher."""
    launcher = plugin(tmp_path)
    copied = tmp_path / "lib" / "orchestra" / "hooks.py"
    held = copied.read_bytes()
    copied.write_bytes(held.replace(b"    data = _input()\n", b"    data = _input()\n    " + fault + b"\n"))
    assert copied.read_bytes() != held
    return launcher


def test_a_hook_that_raises_exits_1_with_its_trace(tmp_path: Path) -> None:
    """Claude Code reads a hook's stderr only on a non-zero exit."""
    launcher = crashing(tmp_path, b"raise RuntimeError('broken plugin')")
    path = names(tmp_path / "names", python3=None, python=None)

    done = hook(path, tmp_path, launcher=launcher)

    assert done.returncode == 1
    assert b"RuntimeError: broken plugin" in done.stderr
    assert (path / "started").read_bytes().split() == [b"python3"]


@pytest.mark.parametrize(
    ("found", "said"),
    [
        # The next name reads an empty stdin, which the program refuses.
        (("python3", "python"), "orchestra: python3 exited 5; then python failed in the hook: its trace is above"),
        (
            ("python3",),
            "orchestra: no Python 3.11 or newer answered the hook: python3 exited 5 (tried python3, python and py -3)",
        ),
    ],
)
def test_a_hook_that_crashes_after_reading_its_input_exits_1(tmp_path: Path, found: tuple[str, ...], said: str) -> None:
    """The crash may have read stdin, so the call is never passed off as one no Python saw."""
    launcher = crashing(tmp_path, b"os._exit(5)")
    path = names(tmp_path / "names", **dict.fromkeys(found))

    done = hook(path, tmp_path, launcher=launcher)

    assert (done.returncode, done.stdout) == (1, b"")
    assert done.stderr.decode().endswith(said + "\n"), done.stderr
    assert (path / "started").read_bytes().decode().split() == list(found)


def test_a_crash_after_a_python_that_never_ran_names_both(tmp_path: Path) -> None:
    launcher = crashing(tmp_path, b"os._exit(5)")
    path = names(tmp_path / "names", python3=86, python=None)

    done = hook(path, tmp_path, launcher=launcher)

    assert done.returncode == 1
    assert done.stderr.decode() == (
        "orchestra: no Python 3.11 or newer answered the hook: python3 exited 86; python exited 5"
        " (tried python3, python and py -3)\n"
    )


def test_a_crash_after_the_read_exiting_127_alone_passes_as_a_python_that_never_ran(tmp_path: Path) -> None:
    """The case the exit codes leave open: a Windows abort exits 127 in Git Bash, as a name not found does.

    With no next name to read the emptied stdin, the call goes through unjudged, once; no run has shown it.
    """
    launcher = crashing(tmp_path, b"os._exit(127)")
    path = names(tmp_path / "names", python3=None)

    done = hook(path, tmp_path, launcher=launcher)

    assert (done.returncode, done.stdout) == (0, b"")
    assert done.stderr.endswith(b": python3 exited 127 (tried python3, python and py -3)\n")


def test_a_hook_given_no_input_exits_1_rather_than_answer_nothing(tmp_path: Path) -> None:
    """Claude Code always sends the event, so an empty stdin is a fault, never an event to let through."""
    path = names(tmp_path / "names", python=None)

    done = launched(path, tmp_path, b"", (LAUNCHER, "hook", "PreToolUse"))

    assert (done.returncode, done.stdout) == (1, b"")
    assert b"the hook's input is no JSON object" in done.stderr


def test_a_hook_caches_the_plugins_bytecode_where_the_session_turns_writing_off(tmp_path: Path) -> None:
    """Claude Code's desktop app sets PYTHONDONTWRITEBYTECODE, under which each hook would compile anew."""
    launcher = plugin(tmp_path)
    path = names(tmp_path / "names", python=None)

    written = ["__init__", "__main__", "hooks"]
    off = {"PYTHONDONTWRITEBYTECODE": "1"}
    beside = tmp_path / "lib" / "orchestra" / "__pycache__"

    # Claude Code gives a hook the plugin's data folder, where caches go and an update keeps them.
    data = tmp_path / "data"
    done = hook(path, tmp_path, event="Stop", launcher=launcher, more={**off, "CLAUDE_PLUGIN_DATA": str(data)})
    assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")
    (folder,) = {p.parent for p in data.glob("pycache/**/orchestra/*.pyc")}
    assert sorted(p.name.partition(".")[0] for p in folder.glob("*.pyc")) == written
    assert not beside.exists()

    # Without one, as in a terminal, beside the modules.
    done = hook(path, tmp_path, event="Stop", launcher=launcher, more=off)
    assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")
    assert sorted(p.name.partition(".")[0] for p in beside.glob("*.pyc")) == written


def fleet_path(cwd: Path, *, with_git: bool) -> bool:
    """Whether a `Stop` in `cwd` takes the fleet path; without git on PATH, any git it ran would fail it."""
    data = json.dumps({"session_id": "s", "cwd": str(cwd), "hook_event_name": "Stop"}).encode()
    env = {**os.environ, "PYTHONPATH": str(LIB)}
    if not with_git:
        env["PATH"] = str(Path(sys.executable).parent)
    done = subprocess.run(
        (sys.executable, "-P", "-c", FLEET_PATH), input=data, capture_output=True, env=env, check=False
    )
    assert (done.returncode, done.stderr) == (0, b"")
    code, fleet = done.stdout.decode().split()
    assert code == "0"
    return fleet == "True"


def test_a_hook_takes_the_fleet_path_only_where_the_primary_branch_has_a_pointer(project: Path) -> None:
    """A pointer another branch left, from a session long ended, costs a later session nothing.

    The session is keyed by the primary checkout's branch, which a linked worktree's hooks read too;
    the branch's name is quoted as `store.pointer` quotes it.
    """
    git(project, "checkout", "-q", "-b", "feat/ü")
    common = project / ".git"
    store.pointer(common, "feat/ü-old").parent.mkdir()
    store.pointer(common, "feat/ü-old").write_bytes(b"gone\n")
    assert not fleet_path(project, with_git=False)

    store.pointer(common, "feat/ü").write_bytes(b"gone\n")
    tree = project.parent / "agent-tree"
    git(project, "worktree", "add", "-q", "-b", "agent", str(tree))
    assert fleet_path(project, with_git=True)
    assert fleet_path(tree, with_git=True)


def test_a_coordinators_edit_is_fenced_with_no_git_run(project: Path) -> None:
    """The branch's pointer names the state, so the hook on every coordinator's edit pays no git."""
    session = "c0ffee00-0000-4000-8000-000000000001"
    assert inprocess("init", "s1", cwd=project, env={store.SESSION_ID: session}).returncode == 0
    target = store.state_dir(project) / store.LOG
    data = {"session_id": session, "cwd": str(project), "tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    env = {**os.environ, "PYTHONPATH": str(LIB), "PATH": str(Path(sys.executable).parent)}
    done = subprocess.run(
        (sys.executable, "-P", "-m", "orchestra", "hook", "PreToolUse"),
        input=json.dumps(data).encode(),
        capture_output=True,
        env=env,
        check=False,
    )
    assert answered(done) == "deny"


def test_a_hook_asks_git_where_head_names_a_branch_alias(project: Path) -> None:
    """`master` kept as a symbolic ref naming `main`: git keys the session by `main`, which `init` wrote."""
    common = project / ".git"
    store.pointer(common, "main").parent.mkdir()
    store.pointer(common, "main").write_bytes(b"gone\n")
    git(project, "symbolic-ref", "refs/heads/master", "refs/heads/main")
    git(project, "checkout", "-q", "master")
    assert fleet_path(project, with_git=True)


def test_a_hook_asks_git_where_head_is_detached_or_a_reftable_stub(project: Path, tmp_path: Path) -> None:
    """Neither names the primary checkout's branch, so any pointer counts."""
    store.pointer(project / ".git", "main").parent.mkdir()
    store.pointer(project / ".git", "main").write_bytes(b"gone\n")
    git(project, "checkout", "-q", "--detach")
    assert fleet_path(project, with_git=True)

    reftable = tmp_path / "reftable"
    git(tmp_path, "init", "-q", "--ref-format=reftable", "-b", "main", str(reftable))
    store.pointer(reftable / ".git", "main").parent.mkdir()
    store.pointer(reftable / ".git", "main").write_bytes(b"gone\n")
    assert (reftable / ".git" / "HEAD").read_bytes().strip() == b"ref: refs/heads/.invalid"
    assert fleet_path(reftable, with_git=True)
