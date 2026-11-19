"""Each agent's context size, and whether it is still working, read from its transcript.

The harness writes a sub-agent's transcript to
`<config dir>/projects/<project>/<session>/subagents/agent-<id>.jsonl`, one JSON object a line, and
an assistant message's `usage` gives the tokens its call read. The context at the agent's last call
is that call's input, cache-creation and cache-read tokens together.

Only the transcript's place and name are documented (sub-agents.md). The fields read here are not:
`message.usage`, `message.id`, shared by the lines one call's blocks reach the file as, the model name
`<synthetic>` on a line the harness wrote itself, and `isApiErrorMessage`. A release changing them
makes the context sizes and the reading of whether an agent still works wrong with no error.
"""

from __future__ import annotations

import datetime
import json
import re
import typing
from pathlib import Path

from orchestra import config, model, store

# The harness's agent ids are letters and digits; anything else would widen the transcript glob.
ID_RE = re.compile(r"[A-Za-z0-9]+")
# A session id as a folder name, held to letters, digits and hyphens for the same reason.
SESSION_RE = re.compile(r"[A-Za-z0-9-]+")
# The model name the harness gives a message it wrote itself, which no model call made.
SYNTHETIC = "<synthetic>"
INPUT = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
# The Bash tool's longest foreground call is ten minutes, so a working agent writes its transcript at
# least that often; five more minutes are the margin.
SILENT_MINUTES = 15
# The tails of a transcript read for its last message, each wider one only where the one before holds
# none of it whole, and the whole file last: a turn's end reads every agent's, so the first is small.
FIRST_BYTES = 1 << 16
TAIL_BYTES = 1 << 20


def projects() -> Path:
    return config.claude_dir() / "projects"


def transcript(agent: model.Agent, session: str | None = None) -> Path | None:
    """The transcript of the agent's latest id that has one; None where none of its ids has.

    The folder of `session`, the calling Claude Code session's unless given, holds the transcripts of
    the agents it dispatched and is searched first: the whole tree grows with every session stored,
    and a turn's end reads every agent's. An agent another session dispatched is found in the whole.
    """
    session = session or store.caller()
    folders = ["*/*"] if session is None or not SESSION_RE.fullmatch(session) else [f"*/{session}", "*/*"]
    for ident in reversed(agent.ids):
        if not ID_RE.fullmatch(ident):
            continue
        for folder in folders:
            found = sorted(
                projects().glob(f"{folder}/subagents/agent-{ident}.jsonl"), key=lambda path: path.stat().st_mtime
            )
            if found:
                return found[-1]
    return None


def as_object(value: object) -> dict[str, object]:
    return typing.cast("dict[str, object]", value) if isinstance(value, dict) else {}


def calls(path: Path) -> list[dict[str, object]]:
    """The usage of each call the agent's model made, in order: one per message, however many lines carry it."""
    return [as_object(message.get("usage")) for message in _made(path.read_bytes().splitlines())]


def last_call(path: Path) -> dict[str, object] | None:
    """The message of a transcript's last call, its `usage` and `model` among its keys; None where it made none.

    Read from the transcript's tail, and from the whole file only where the tail holds no call, so a main
    session's transcript of hundreds of megabytes costs a hook one read of a megabyte.
    """
    made: list[dict[str, object]] = []
    for offset in dict.fromkeys((max(0, path.stat().st_size - TAIL_BYTES), 0)):
        with path.open("rb") as held:
            held.seek(offset)
            data = held.read()
        # A read from the middle starts inside a line.
        made = _made(data.splitlines()[1:] if offset else data.splitlines())
        if made:
            break
    return made[-1] if made else None


def _made(lines: list[bytes]) -> list[dict[str, object]]:
    """The message of each call a model made, in order: one per call, however many lines carry it."""
    messages: dict[object, dict[str, object]] = {}
    for raw in lines:
        try:
            entry = as_object(json.loads(raw))
        except ValueError:
            # A line still being written.
            continue
        message = as_object(entry.get("message"))
        if message.get("role") != "assistant" or message.get("model") == SYNTHETIC or entry.get("isApiErrorMessage"):
            continue
        if as_object(message.get("usage")):
            # A call's message reaches the transcript as one line per content block, each with its usage.
            messages[message.get("id")] = message
    return list(messages.values())


def _messages(path: Path, offset: int) -> list[dict[str, object]]:
    """Each user or assistant message of a transcript from `offset`, the harness's own notices left out."""
    with path.open("rb") as held:
        held.seek(offset)
        data = held.read()
    # A read from the middle starts inside a line.
    raw = data.split(b"\n")[1:] if offset else data.split(b"\n")
    found: list[dict[str, object]] = []
    for line in raw:
        try:
            entry = as_object(json.loads(line))
        except ValueError:
            continue
        message = as_object(entry.get("message"))
        # An API error, or the line a usage limit writes, is no turn of the agent's.
        if message.get("role") not in ("user", "assistant") or entry.get("isApiErrorMessage"):
            continue
        if message.get("model") != SYNTHETIC:
            found.append(message)
    return found


def _turn_ended(path: Path) -> bool:
    """Whether the transcript's last message is the agent's own and calls no tool, which ends its turn."""
    size = path.stat().st_size
    messages: list[dict[str, object]] = []
    for span in (FIRST_BYTES, TAIL_BYTES, size):
        messages = _messages(path, max(0, size - span))
        if span >= size:
            break
        # A tail opening on the last message's own lines may have cut off an earlier block of it.
        first, last = (messages[0], messages[-1]) if messages else ({}, {})
        cut = first.get("role") == last.get("role") == "assistant" and first.get("id") == last.get("id")
        if messages and not cut:
            break
    if not messages or messages[-1].get("role") != "assistant":
        return False
    last = messages[-1]
    # One assistant message reaches the transcript as several lines sharing its id.
    blocks: list[object] = []
    for message in reversed(messages):
        if message.get("role") != "assistant" or message.get("id") != last.get("id"):
            break
        content = message.get("content")
        blocks += typing.cast("list[object]", content) if isinstance(content, list) else []
    return not any(as_object(block).get("type") == "tool_use" for block in blocks)


def ended(agent: model.Agent, clock: datetime.datetime, session: str | None = None) -> tuple[int | None, str | None]:
    """Minutes since the agent's transcript changed, and why it is no longer working; None for why while it may be.

    The one answer to "is this agent still working", whatever its record reads: a crashed stop hook
    leaves it `running`, a takeover leaves a live agent `stopped`, and a resumed agent's reads as its
    last report did until its hook runs. A turn that ended has ended where the record says the agent
    stopped. Under a `running` record it has ended only once silent past `SILENT_MINUTES` too: the stop
    hook runs after the turn's last line, and refuses a stop whose report does not yet read, and a
    message whose blocks stream may show a finished block first. One silent past `SILENT_MINUTES`
    mid-turn is a process that died mid-call.
    """
    quiet, why, _, _ = _judged(agent, clock, session)
    return quiet, why


def _judged(
    agent: model.Agent, clock: datetime.datetime, session: str | None = None
) -> tuple[int | None, str | None, datetime.datetime | None, bool]:
    """`ended`'s answer, when the transcript was last written, and whether its turn ended.

    None for the time where the transcript cannot be read.
    """
    path = transcript(agent, session)
    if path is None:
        return None, f"no transcript found for {', '.join(agent.ids) or 'an agent with no id recorded'}", None, False
    try:
        written = datetime.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        done = _turn_ended(path)
    except OSError as error:
        return None, f"its transcript cannot be read: {error}", None, False
    quiet = int((clock - written).total_seconds() // 60)
    silent = quiet > SILENT_MINUTES
    if done and (silent or agent.status != "running"):
        return quiet, "its transcript's last message ends its turn", written, done
    if silent:
        return quiet, f"its transcript is silent {quiet} min mid-turn, past {SILENT_MINUTES}", written, done
    return quiet, None, written, done


def gone(agent: model.Agent, session: str | None = None) -> str | None:
    """Why the agent no longer works, from a transcript that shows it; None where it may, or no transcript is read.

    No transcript shows nothing either way, which a harness that keeps them elsewhere leaves for a live agent.
    """
    quiet, why = ended(agent, datetime.datetime.now().astimezone(), session)
    return why if quiet is not None else None


def working(agent: model.Agent, session: str | None = None) -> str | None:
    """Why the agent may still be working, as `ended` judges it, and when it counts as ended; None where it is not.

    A crashed session's agent reads as working until then, so a caller waits for the time named
    rather than removing what the agent may still be writing.
    """
    quiet, why, written, done = _judged(agent, datetime.datetime.now().astimezone(), session)
    if why is not None or written is None:
        return None
    # `ended` counts whole minutes, so the first one past `SILENT_MINUTES` is when it reads silent.
    at = store.clock_time(written + datetime.timedelta(minutes=SILENT_MINUTES + 1))
    shown = (
        f"its record is running, and its transcript's turn ended {quiet} min ago, which its stop hook may still refuse"
        if done
        else f"its transcript is mid-turn, written {quiet} min ago"
    )
    return f"{shown}; it counts as ended once silent past {SILENT_MINUTES} min, at {at}"


def mid_turn(agent: model.Agent, session: str | None = None) -> bool:
    """Whether the agent's transcript shows a turn under way, its last message calling a tool and not yet silent."""
    _, why, written, done = _judged(agent, datetime.datetime.now().astimezone(), session)
    return why is None and written is not None and not done


def tokens(usage: dict[str, object]) -> int:
    return sum(value for key in INPUT if isinstance(value := usage.get(key), int))


def size(agent: model.Agent, session: str | None = None) -> int | None:
    """The agent's context at its last call, in tokens; None where it has no transcript or made no call."""
    path = transcript(agent, session)
    made = calls(path) if path is not None else []
    return tokens(made[-1]) if made else None
