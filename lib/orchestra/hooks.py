"""`hook`: one Claude Code hook event answered, its input read from stdin, for the session's state.

Every registration in `hooks/hooks.json` runs `orchestra hook <event>`, in every session that has the
plugin enabled. Most of them hold no fleet on the branch a session is keyed by, so this module decides
that from git's own files, without running git or importing the state's modules, and answers nothing:
such a session pays the shell and one Python start. Where a fleet may exist, or the event concerns
one of the plugin's agents, `events` answers, except a subagent's `SendMessage`, which it never decides.

An answered event exits 0. A refusal is the JSON decision its event documents, never exit 2, which
on `UserPromptSubmit` would block the owner's prompt (Claude Code's hooks.md, "Exit code 2 behavior
per event"). A fault this module meets, such as stdin holding no JSON object, is raised, and the
launcher exits 1 on it: a hook error, which lets the call through.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from orchestra import DONE

if TYPE_CHECKING:
    # Named in annotations alone, so a hook's start never imports them.
    from collections.abc import Callable

# The events `hooks/hooks.json` registers, each by Claude Code's own name for it.
EVENTS = (
    "PreToolUse",
    "PermissionRequest",
    "PermissionDenied",
    "PostToolUse",
    "SubagentStop",
    "StopFailure",
    "Stop",
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
)
# The prefix Claude Code gives a plugin's agent types, `orchestra:writer` (hooks.md, "SubagentStart").
PLUGIN = "orchestra:"
# The folder in the git common directory holding each branch's pointer to its state: `store.POINTERS`,
# spelled here so this module imports no store, which a test holds equal.
POINTERS = "orchestra"
# The file marking a session away in its state: `store.AWAY`, spelled here as `POINTERS` is.
AWAY = "away.json"
# The tools that wait on a person's answer whatever the permissions allow, denied while the owner is away.
ASKING = ("AskUserQuestion", "ExitPlanMode")
# The events that decide nothing but while the session is away, besides a `PreToolUse` of an `ASKING` tool.
AWAY_ONLY = ("PermissionRequest", "PermissionDenied")
# The variables that move git's directory away from the `.git` beside the work tree (git(1), "ENVIRONMENT").
GIT_PLACES = ("GIT_DIR", "GIT_COMMON_DIR")
# The project root where the session started, which Claude Code exports to every hook's process and which, unlike
# the input's `cwd`, no `cd` moves (hooks.md, "Reference scripts by path").
PROJECT_DIR = "CLAUDE_PROJECT_DIR"
# What a HEAD file holding a branch opens with, the branch's name following.
BRANCH_HEAD = "ref: refs/heads/"
# The branch a reftable repository's HEAD file names, so that an older git still reads the folder as a
# repository; the real HEAD is in the reftable (git's reftable format, "Backward compatibility").
REFTABLE_STUB = ".invalid"


class Events(Protocol):
    """What `events` exposes: the stdout text answering one event, None for nothing.

    `place` is the folder `held` was read from: the input's `cwd`, or the project root where that held no fleet.
    """

    def answer(self, event: str, data: dict[str, object], *, held: Path | bool, place: Path | None) -> str | None: ...


def main(event: str) -> int:
    """The event answered on stdout, as UTF-8 bytes whatever the console's code page; 0, or a fault raised."""
    data = _input()
    if event not in EVENTS or _agents_message(event, data):
        return DONE
    cwd = data.get("cwd")
    place = Path(cwd) if isinstance(cwd, str) else None
    held = _pointers(place) if place is not None else False
    project = os.environ.get(PROJECT_DIR)
    if held is False and project and (place is None or Path(project) != place):
        # The main session's Bash `cd` moves the input's `cwd` (hooks.md, "Reference scripts by path"), and the state's
        # folder is a working directory a `cd` may stay in, so the session is found from the project root too,
        # which stays put. An agent's `cwd`, its worktree or the checkout, finds the session by itself.
        found = _pointers(Path(project))
        if found is not False:
            place, held = Path(project), found
    if held is False and not concerns_plugin(event, data):
        return DONE
    asks = event == "PreToolUse" and data.get("tool_name") in ASKING
    if (event in AWAY_ONLY or asks) and not _away(held=held):
        # Each prompt and question of a session at work would otherwise load `events` to learn only that.
        return DONE
    events = cast("Events", importlib.import_module("orchestra.events"))
    said = events.answer(event, data, held=held, place=place)
    if said:
        sys.stdout.buffer.write(said.encode("utf-8"))
    return DONE


def _input() -> dict[str, object]:
    """The event's JSON object from stdin, read as bytes; a fault for anything else.

    Claude Code always sends the object, so stdin holding none was read by a Python that crashed before
    this one: raised, it is a hook error the session shows, where answering nothing would pass in silence.
    """
    raw = sys.stdin.buffer.read()
    try:
        held: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        held = None
    if isinstance(held, dict):
        return cast("dict[str, object]", held)
    raise ValueError(f"the hook's input is no JSON object: {len(raw)} bytes read from stdin")


def _away(*, held: Path | bool) -> bool:
    """Whether the state a pointer names may be away: False where it certainly is not, True where unknown."""
    if not isinstance(held, Path):
        return held
    try:
        state = Path(held.read_bytes().decode("utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return True
    return (state / AWAY).exists()


def _agents_message(event: str, data: dict[str, object]) -> bool:
    """Whether the event is a subagent's `SendMessage` call, which `events` decides nothing on.

    Only the coordinator's message resumes an agent or routes rows, and an event carrying `agent_id` is never
    the coordinator's. Loaded, `events` could only fault here, and a fault denies a plugin agent's call.
    """
    agent = data.get("agent_id")
    named = isinstance(agent, str) and bool(agent)
    return event in {"PreToolUse", "PostToolUse"} and data.get("tool_name") == "SendMessage" and named


def concerns_plugin(event: str, data: dict[str, object]) -> bool:
    """Whether the event fires inside one of the plugin's agents, or dispatches one.

    Either is refused outside a fleet, so it reaches `events` even where no state exists.
    """
    kind = data.get("agent_type")
    if isinstance(kind, str) and kind.startswith(PLUGIN):
        return True
    given = data.get("tool_input")
    if event != "PreToolUse" or data.get("tool_name") != "Agent" or not isinstance(given, dict):
        return False
    sub = cast("dict[str, object]", given).get("subagent_type")
    return isinstance(sub, str) and sub.startswith(PLUGIN)


def _pointers(cwd: Path) -> Path | bool:
    """The pointer naming the state of the clone holding `cwd`; False where it certainly has none, True where unknown.

    Read from git's repository layout (gitrepository-layout(5)): a `.git` folder is the common
    directory; a `.git` file names a linked worktree's or a submodule's git folder in its `gitdir:`
    line, whose `commondir` file, where present, names the common directory relative to it.
    """
    if any(os.environ.get(name) for name in GIT_PLACES):
        return True
    for folder in (cwd, *cwd.parents):
        dot = folder / ".git"
        try:
            if dot.is_dir():
                common = dot
            elif dot.is_file():
                line = dot.read_bytes().decode("utf-8").strip()
                if not line.startswith("gitdir:"):
                    return True
                own = folder / line.removeprefix("gitdir:").strip()
                shared = own / "commondir"
                common = own / shared.read_bytes().decode("utf-8").strip() if shared.is_file() else own
            else:
                continue
        except (OSError, UnicodeDecodeError):
            return True
        return _branch_held(common)
    return False


def _branch_held(common: Path) -> Path | bool:
    """The pointer of the branch the common directory's HEAD names; False where it has none, True where unknown.

    `store.state_dir` keys a session by the primary checkout's branch, whose HEAD is the common
    directory's own (gitrepository-layout(5)), so a pointer another branch left, from a session long
    ended, costs this one nothing, and a session's hook reads its state with no git run. Where HEAD
    names no branch of its own, detached, an alias or the stub a reftable repository keeps, any
    pointer counts, and `events` asks git.
    """
    folder = common / POINTERS
    if not folder.is_dir():
        return False
    try:
        head = (common / "HEAD").read_bytes().decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return True
    branch = head.removeprefix(BRANCH_HEAD)
    if branch in {head, REFTABLE_STUB}:
        return True
    # A branch that is a symbolic ref, such as `master` kept naming `main`, keys its session by the branch
    # it names, which git resolves; such a ref is always a loose file (git-symbolic-ref(1)).
    try:
        alias = (common / "refs" / "heads" / branch).read_bytes().startswith(b"ref:")
    except OSError:
        alias = False
    if alias:
        return True
    # `store.pointer`'s quoting, which a test holds equal, imported only where a pointer folder exists,
    # so the clones holding none never load it on Python 3.14, whose `pathlib` no longer imports it;
    # on 3.11 and 3.12 `pathlib` has loaded it already.
    quote: Callable[[str, str], str] = importlib.import_module("urllib.parse").quote
    pointer = folder / quote(branch, "")
    return pointer if pointer.is_file() else False
