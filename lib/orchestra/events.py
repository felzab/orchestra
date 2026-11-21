"""What each hook event does to the session's state, and the decision it returns to Claude Code.

`hooks` imports this module only where a fleet may exist, or where the event concerns one of the
plugin's agents. Every event first finds whose it is: an event inside a subagent belongs to the
agent record holding the input's `agent_id`; any other belongs to the coordinator only where the
input's `session_id` holds the state's lease. An event of nobody's gets no answer, so a second
session in the same checkout is never steered by this one's fleet; an event inside one of the
plugin's agents that no record holds is refused, since its guards would otherwise be off.

While the session is away, `PermissionRequest` refuses its prompts, `PreToolUse` its questions, and
each refusal, `PermissionDenied`'s in auto mode among them, is recorded for the owner's return.

Each function returns the event's stdout, None for nothing; the shapes are Claude Code's hooks.md.
A refusal is a `PreToolUse` deny or a `SubagentStop` block, which one `orchestra override` naming its
caller lets through, logged. Once this module runs, an event that cannot be judged, its state
unreadable, a lock held past the hook's budget or any fault, fails closed where it guards: a
`PreToolUse` of one of the plugin's agents, or dispatching one, is denied naming the fault, and while
the session is away any session's prompt or question in the checkout is refused. Every other such
event of the lease holder's session or of one of the plugin's agents logs a line and shows it in a
`systemMessage`; one of nobody's answers nothing, its trace on stderr. A hook that fails before,
its Python missing or crashed, or past its timeout, lets the call through (`"onFailure": "continue"`
in hooks/hooks.json), so a broken install never blocks a session.

The writes of an event inside one of the plugin's agents record that agent's own facts, its stop,
its branch and their log lines, so they pass whichever session holds the lease (`store.unleased`).
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import functools
import importlib
import json
import logging
import os
import re
import typing
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orchestra import Refused, brief, cli, config, context, findings, log, model, override, queue, report, store
from orchestra.hooks import ASKING, PLUGIN, concerns_plugin
from orchestra.store import Record

TYPES: tuple[str, ...] = typing.get_args(model.AgentType)
# The most a hook waits on locks and retries in all, inside the 30-s `timeout` each registration in
# hooks/hooks.json sets, so the hook decides before Claude Code cancels it; a test holds the order.
BUDGET_SECONDS = 20.0
# `SessionEnd` hooks share a 1.5-s budget, the process's start included, which no plugin hook's
# `timeout` raises (hooks.md, "SessionEnd"); `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` alone does.
SESSION_END_SECONDS = 1.0
# Every status reached after a report, so a resume starts the next round unless the agent stopped on a question.
REPORTED: frozenset[str] = frozenset({"reported", "judged", "landed", "pruned"})
# A stop refused this often lets the agent stop anyway, `stopped`: the next refusal would teach it nothing.
REFUSALS = 2
# What an agent's last message says when a usage limit or an API error ended it, so its report cannot be repaired.
LIMIT_RE = re.compile(r"You(?:\u2019|')ve hit your [^\n]*\blimit|^API Error\b", re.MULTILINE)
# A `Rows:` line in a message routes its rows to the agent it goes to.
ROWS_RE = re.compile(r"^[ \t]*Rows:[ \t]*(?P<rows>.+)$", re.MULTILINE)
# The tools that write a file, and the input key naming it (permissions.md, "Tool-specific permission rules").
WRITERS = {"Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}
# A peer's address may carry a ` [ref]` suffix naming the same agent.
REF_RE = re.compile(r"\s*\[[^\]]*\]$")
# Past this many minutes since the previous prompt, a usage figure read before it is stale.
STALE_MINUTES = 30
# Log entries newer than NOW.md past which it no longer says where the session stands.
NOW_EVENTS = 5
# The most an `additionalContext` carries before Claude Code moves it to a file.
CONTEXT_CHARS = 10_000
# The coordinator's own files in the state; every other file there is written by commands and hooks.
OWN = frozenset({store.PLAN, store.NOW})
# The project's own rules for each agent type, `<type>.md`, in the primary checkout.
ADDENDA = Path(".claude", "orchestra")
# The reports judged after the newest saved estimate at which the checkpoint is due again while agents stay at
# work, for a fleet that dispatches as work frees and so has no quiet wave end. At 4, one long session saved 29
# estimates in sixteen hours, 24 of them at up to 8% of its coordinator's tokens; 8 was estimated to halve that.
JUDGED = 8
# The `Lens:` line of a disclosure-only read, which frees no planned work, so its report leaves the
# checkpoint's count alone. The brief alone is read, so the read before a push and the review's own
# disclosure reader are both left out: nothing tells the two apart.
DISCLOSURE_ONLY = ("disclosure",)
# When the previous prompt arrived, for the clock line.
PROMPT = "prompt.json"
# The labels of the overview's rows naming judged and pruned agents, left out of the one given back after a
# compaction or a resume: one long session spent 17% of those overviews' characters on them.
FINISHED_ROWS = frozenset({"judged", "pruned"})
# The share of the compaction window from which each prompt tells the coordinator its context nears it, so that it
# compacts at its next seam before Claude Code compacts wherever the work stands. On one long coordinator day of the
# maintainer's, the owner's six compactions at seams fell at 0.75 to 0.91 of the 500,000-token window, and the seven
# automatic ones at 0.93.
NEAR_WINDOW = 0.75
# The tool `hooks/compact.js` registers, which compacts the coordinator once its turn ends.
COMPACT_TOOL = "mcp__orchestra__compact"
# The reason of a `SessionEnd` that `/clear` caused, whose session id never writes again, and the source
# of the `SessionStart` that follows it.
CLEARED = "clear"
# How recent a `/clear`'s stop of the lease is for the session starting after it to be told of it.
CLEARED_SECONDS = 60
# The log line `session_end` writes, which `session_start` reads back.
STOPPED_BY_CLEAR = "stopped by /clear in session "
# Each call refused while the session is away, one line each, which `orchestra mode attended` lists.
REFUSED = "refusals.jsonl"
# The most of a refused call's input a line keeps.
SUMMARY_CHARS = 160
# What the model reads in a refused call's result. Stated as facts: a model reading tool output was seen
# to ignore an instruction there, and `skills/run/SKILL.md` says what to do with a question meanwhile.
AWAY_PROMPT = (
    "Refused: the owner is away, so nobody can answer a permission prompt, and no permission rule or mode"
    " allows this call. The refusal is recorded for the owner's return."
)
AWAY_QUESTION = (
    "Refused: the owner is away, so nobody can answer a question or approve a plan. The refusal is recorded"
    " for the owner's return, and `orchestra ask` keeps a question until then."
)
LOGGER = logging.getLogger("orchestra")


@dataclass(frozen=True)
class Where:
    """Whose an event is, and the state it reads."""

    data: Record
    # The input's `cwd`: an agent's own folder, its worktree where it has one.
    cwd: Path
    # The folder the state was found from: `cwd`, or the project root where a `cd` left `cwd` in no fleet's clone.
    place: Path
    state: Path | None
    # The id of the subagent the event fires in; None in the main session.
    agent_id: str | None
    # The plugin type the event's agent carries, `writer` of `orchestra:writer`; None for any other.
    plugin: str | None
    # The record holding `agent_id`.
    agent: model.Agent | None
    # Whether the main session's id holds the state's lease.
    coordinator: bool

    @property
    def session(self) -> str | None:
        """The main session's id, whose folder holds the transcripts of the agents it dispatched."""
        held = self.data.get("session_id")
        return held if isinstance(held, str) else None

    @functools.cached_property
    def root(self) -> Path | None:
        """The primary checkout, from git, which only the events reading the project run; None without a state."""
        if self.state is None:
            return None
        try:
            return config.checkout(self.place).root
        except Refused:
            return None


def answer(event: str, data: Record, *, held: Path | bool, place: Path | None) -> str | None:
    """The stdout answering one event; None for nothing. An event that cannot be judged fails closed where it guards.

    `place` is the folder `hooks` read `held` from, the input's `cwd` unless that held no fleet.
    """
    place = place if place is not None else Path(str(data.get("cwd")))
    # One budget for the event and for its fault's answer, so the fault's log write ends inside it too.
    with store.budget(SESSION_END_SECONDS if event == "SessionEnd" else BUDGET_SECONDS):
        try:
            where = _where(data, held=held, place=place)
            facts = store.unleased() if where.agent is not None else contextlib.nullcontext()
            with facts:
                return HANDLERS[event](where)
        except Refused as refused:
            fault, traced = str(refused), False
        except Exception as error:
            LOGGER.exception("orchestra hook %s failed", event)
            fault, traced = f"{type(error).__name__}: {error}", True
        if not traced:
            # A refusal names its own fix, so it is one line on stderr, never a trace.
            LOGGER.error("orchestra hook %s: %s", event, fault)
        return _faulted(event, data, fault, held=held, place=place)


def _faulted(event: str, data: Record, fault: str, *, held: Path | bool, place: Path) -> str | None:
    """An event that cannot be judged: a guarded call denied naming the fault, any other the fault shown and logged.

    No exit 0 shows its stderr, so an event of the lease holder's session or of one of the plugin's
    agents logs a line and shows the user a `systemMessage`; one of nobody's writes and shows nothing,
    as it gets no answer when judged. The line is a fact, written past the lease, so it claims none.
    While the session is away, a prompt or a question is refused as ever, since nobody is there to
    answer it: `away.json` alone decides, the event's owner being among what may have failed to read.
    """
    state = None
    logged: list[bool] = []
    try:
        state = _state(held=held, place=place)
        if state is not None and _owned(data, state):
            known = state
            tool = data.get("tool_name")
            said = f"{event}{f' ({tool})' if isinstance(tool, str) else ''} failed: {fault}"
            with store.unleased():
                _quietly(lambda: logged.append(bool(log.record(known, "hook", said))))
    except Exception:
        LOGGER.exception("orchestra hook %s could not log its fault", event)
    if event == "PreToolUse" and concerns_plugin(event, data):
        return _deny(f"orchestra cannot judge this call, so it refuses it: {fault}")
    if state is not None and (state / store.AWAY).exists():
        if event == "PermissionRequest":
            return _refuse_prompt()
        if event == "PreToolUse" and data.get("tool_name") in ASKING:
            return _deny(AWAY_QUESTION)
    shown = f"orchestra's {event} hook failed: {fault}; `orchestra log` lists it"
    return _json({"systemMessage": shown}) if logged else None


def _owned(data: Record, state: Path) -> bool:
    """Whether an event is the lease holder's session's or inside one of the plugin's agents, from its input alone.

    A fault may come from reading the records, so they are not read; the lease is the one file read.
    """
    kind = data.get("agent_type")
    if isinstance(kind, str) and kind.startswith(PLUGIN):
        return True
    try:
        owner = store.lease(state).get("owner")
    except Refused:
        return False
    return owner is not None and owner == data.get("session_id")


def _id(value: object) -> str | None:
    # hooks.md's example spells a SubagentStart id `agent-abc123`; the Agent result's `agentId` has no prefix.
    return value.removeprefix("agent-") if isinstance(value, str) and value else None


def _state(*, held: Path | bool, place: Path) -> Path | None:
    """`held` is the branch's pointer where `hooks` read it from git's files in `place`, True where git must find it."""
    if held is False:
        return None
    try:
        return store.state_dir(place) if held is True else store.pointed(held)
    except Refused:
        return None


def _where(data: Record, *, held: Path | bool, place: Path) -> Where:
    cwd = Path(str(data.get("cwd")))
    state = _state(held=held, place=place)
    agent_id = _id(data.get("agent_id"))
    kind = data.get("agent_type")
    plugin = kind.removeprefix(PLUGIN) if isinstance(kind, str) and kind.startswith(PLUGIN) else None
    agent = None
    coordinator = False
    if state is not None:
        if agent_id is not None:
            agent = next((held for held in model.agents(state) if agent_id in {_id(i) for i in held.ids}), None)
        else:
            owner = store.lease(state).get("owner")
            coordinator = owner is not None and owner == data.get("session_id")
    return Where(data, cwd, place, state, agent_id, plugin, agent, coordinator)


def _quietly(act: Callable[[], object]) -> None:
    """A write the event's decision does not rest on, skipped where the store refuses it."""
    with contextlib.suppress(Refused):
        act()


def _json(held: Record) -> str:
    return json.dumps(held, ensure_ascii=False)


def _deny(reason: str) -> str:
    return _json(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def _allow(updated: Record) -> str:
    # In auto mode a changed input with no decision can be denied (hooks.md, "PreToolUse `updatedInput` in auto mode").
    return _json(
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": updated}}
    )


def _context(event: str, text: str) -> str:
    return _json({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text[:CONTEXT_CHARS]}})


def _waived(where: Where, call: str) -> bool:
    """Whether an override waits for this refusal's caller, the event's agent or the coordinator, used up on it."""
    if where.state is None or (where.agent is None and not where.coordinator):
        return False
    try:
        return override.consume(where.state, call, where.agent.name if where.agent else None) is not None
    except Refused:
        return False


def _round(agent: model.Agent) -> int:
    return max(agent.reports, default=1)


def _resumed_round(agent: model.Agent) -> int:
    """The round a resume runs: the next after a report, the same where the agent stopped on a question or a fault.

    A `BLOCKED` or `NEEDS-ANSWER` report is completed once answered, so its findings are judged with it.
    """
    done = agent.status in REPORTED and agent.outcome not in report.STOPPED
    return _round(agent) + 1 if done else _round(agent)


def _relative(state: Path, path: Path) -> str:
    return path.relative_to(state).as_posix()


def _record_branch(where: Where, agent: model.Agent) -> None:
    """A writer's or a driver's branch, read from the worktree its event fires in, where its record has none."""
    if agent.branch is not None or agent.type not in report.COMMITTING or where.state is None:
        return
    state = where.state
    try:
        if config.checkout(where.cwd).primary:
            # Running in the coordinator's checkout, its branch is the session's, never its own.
            return
        branch = config.git(where.cwd, "symbolic-ref", "--quiet", "--short", "HEAD").strip()
    except Refused:
        return
    _quietly(lambda: model.update_agent(state, agent.name, lambda held: dataclasses.replace(held, branch=branch)))


# --- PreToolUse -----------------------------------------------------------------------------------


def pre_tool_use(where: Where) -> str | None:
    tool = where.data.get("tool_name")
    given = context.as_object(where.data.get("tool_input"))
    if tool in ASKING:
        return _deny(AWAY_QUESTION) if _refused_away(where) else None
    try:
        if tool == "Agent":
            return _dispatch(where, given)
        if isinstance(tool, str) and tool in WRITERS:
            return _fence(where, given.get(WRITERS[tool]))
        if tool == "SendMessage" and where.coordinator:
            return _resume(where, given)
    except Refused as refused:
        # A state the decision rests on cannot be read: refused loudly, so a guard never passes unread.
        call = f"{tool} ({refused})"
        return None if _waived(where, call) else _deny(f"orchestra cannot decide this call: {refused}")
    return None


def _dispatch(where: Where, given: Record) -> str | None:
    """A checked brief's agent started with its profile, its name, its worktree and its three paths."""
    sub = given.get("subagent_type")
    if not isinstance(sub, str) or not sub.startswith(PLUGIN) or sub.removeprefix(PLUGIN) not in TYPES:
        return None
    kind = sub.removeprefix(PLUGIN)
    state, root = where.state, where.root
    name = str(given.get("prompt") or "").strip()
    call = f"the dispatch of {name or 'an agent'}"
    if state is None or root is None:
        why = "no orchestra session holds this branch: `orchestra init <session>` makes one"
    elif not where.coordinator:
        # The verbs' own refusal, so the dispatch names the takeover as every verb does.
        held, me = store.lease(state), str(where.data.get("session_id"))
        why = store.refusal(held, me, store.takeover_at(state)) or "this session holds no lease"
    elif not model.NAME_RE.fullmatch(name):
        why = "an orchestra dispatch's prompt is the agent's name alone, as `orchestra brief NAME` wrote its brief"
    elif not model.has_agent(state, name):
        sliced = " --slice S" if kind == brief.SLICED else ""
        why = (
            f"no agent {name} in this session: `orchestra brief {name} --type {kind}{sliced} --body FILE` writes its"
            " brief from a file holding its lists and its four sections, and checks it"
        )
    else:
        return _start(where, given, model.read_agent(state, name), root)
    return None if _waived(where, call) else _deny(why)


def _start(where: Where, given: Record, agent: model.Agent, root: Path) -> str | None:
    state = typing.cast("Path", where.state)
    kind = str(given["subagent_type"]).removeprefix(PLUGIN)
    name = agent.name
    head = brief.header(state, name)
    try:
        profile = brief.profile(head, config.load(root))
        unknown = None
    except Refused as refused:
        profile, unknown = None, str(refused)
    if agent.type != kind:
        why = f"{name} is {agent.type}, and this dispatch names orchestra:{kind}"
    elif agent.status != "briefed":
        why = f"{name} is {agent.status}: a dispatch starts a briefed agent, and SendMessage resumes this one"
    elif not brief.stamped(state, agent):
        why = f"{name}'s brief is not checked, or changed since its check: run `orchestra brief {name} --check`"
    else:
        why = unknown
    if why is not None and not _waived(where, f"the dispatch of {name} ({why})"):
        return _deny(why)
    updated: Record = {**given, "name": name, "prompt": _prompt(state, root, agent)}
    if profile is not None:
        updated["model"] = profile.model
        if profile.effort is not None:
            updated["effort"] = profile.effort
    if kind in report.COMMITTING:
        # A named call starts a teammate in the coordinator's checkout where agent teams are on,
        # unless the call itself carries `isolation` (sub-agents.md); the definition's own does not count.
        updated["isolation"] = "worktree"
    return _allow(updated)


def _prompt(state: Path, root: Path, agent: model.Agent) -> str:
    """The agent's paths, the project's addendum for its type among them or `none`, so no agent probes for one.

    The report's line names its title too, as a resume names its round: the dispatch alone knows the round is 1,
    and an agent left to guess one from its name was seen titling its first report round 2.
    """
    name = agent.name
    held = brief.brief_path(state, name)
    addendum = root / ADDENDA / f"{agent.type}.md"
    return "\n".join(
        [
            f"Brief: {held}",
            f"Messages: {held.with_name(f'{name}.messages.md')}",
            f"Report: {report.path(state, name)}, titled `# {name} · {agent.type} · round 1`",
            f"Addendum: {addendum if addendum.is_file() else 'none'}",
        ]
    )


def _fence(where: Where, target: object) -> str | None:
    """An agent kept to its own paths, and the coordinator out of the files commands and hooks write."""
    if not isinstance(target, str) or not target:
        return None
    if where.agent_id is not None:
        return _agent_fence(where, target)
    if where.coordinator and where.state is not None:
        return _coordinator_fence(where, target, where.state)
    return None


def _agent_fence(where: Where, target: str) -> str | None:
    agent, state = where.agent, where.state
    if agent is None or state is None:
        if where.plugin is None:
            return None
        why = (
            f"no orchestra record holds agent {where.agent_id}: an orchestra agent writes only inside the session"
            " that dispatched it, once its dispatch is recorded"
        )
        return None if _waived(where, f"a write to {target} ({why})") else _deny(why)
    own = report.path(state, agent.name, _round(agent))
    scratch = state / store.SCRATCH / agent.name
    if agent.type in report.COMMITTING:
        _record_branch(where, agent)
    if config.identical(Path(target), own) or config.within(Path(target), scratch):
        return None
    if agent.type in report.COMMITTING:
        # Its worktree keeps it from other agents' files, and the landing refuses a path outside `Owns:`;
        # the programme's folder, every session's state and the plan and handoffs later sessions read as
        # instructions, lies outside every worktree, so it is fenced here.
        if not config.within(Path(target), state.parent):
            return None
        why = (
            f"{target} is in the programme's folder, which orchestra's commands, hooks and coordinator write:"
            f" {agent.name} writes there only its report, {own}, and its scratch folder, {scratch}"
        )
    else:
        writes = brief.header(state, agent.name).writes
        pad = where.data.get("scratchpad_dir")
        if any(_written(where.root, held, Path(target)) for held in writes) or (
            isinstance(pad, str) and pad and config.within(Path(target), Path(pad))
        ):
            return None
        why = (
            f"{agent.name} is {report.article(agent.type)} and writes only its report, {own}, its scratch folder,"
            f" {scratch}, its brief's `Writes:` paths ({', '.join(writes) or 'none'}) and the session's scratchpad:"
            " message the coordinator for another"
        )
    return None if _waived(where, f"{agent.name}'s write to {target}") else _deny(why)


def _written(root: Path | None, pattern: str, target: Path) -> bool:
    """Whether a `Writes:` pattern, read from the primary checkout, names the target, both by their resolved spelling.

    A link's spelling of an allowed folder is allowed, since `resolve` follows it; the long-path form,
    which `resolve` keeps as written, is refused naming the path. The deny side, which must never pass
    a file, judges by identity.
    """
    return brief.owned([_spelled(root / pattern if root else Path(pattern))], _spelled(target))


def _spelled(path: Path) -> str:
    """A path as a pattern is matched: absolute, `/`-separated, and case-folded where the file system folds case."""
    return os.path.normcase(path.resolve()).replace("\\", "/")


def _state_file(state: Path, target: str) -> bool:
    """Whether a path is the session's state, or the programme's rulings beside it."""
    return config.within(Path(target), state) or config.within(Path(target), model.rulings_path(state))


def _coordinator_fence(where: Where, target: str, state: Path) -> str | None:
    if not _state_file(state, target) or (
        any(config.identical(Path(target), state / name) for name in OWN)
        or config.within(Path(target), state / store.ESTIMATES)
        or config.within(Path(target), state / store.SCRATCH)
        or _brief_file(state, Path(target))
    ):
        return None
    why = (
        f"{target} is written by orchestra's commands and hooks alone; the coordinator writes PLAN.md, NOW.md,"
        " a brief below its header, the estimates and the scratch folder"
    )
    return None if _waived(where, f"the coordinator's write to {target}") else _deny(why)


def _brief_file(state: Path, target: Path) -> bool:
    """Whether a path is a brief, which the coordinator writes, rather than a messages file, which a hook writes."""
    named = target.name.endswith(".md") and model.NAME_RE.fullmatch(target.name.removesuffix(".md")) is not None
    return named and config.identical(target.parent, state / brief.BRIEFS)


def _addressee(state: Path, to: object) -> model.Agent | None:
    """The agent a message goes to, by its name or by an id the harness gave it."""
    if not isinstance(to, str):
        return None
    to = REF_RE.sub("", to).strip()
    if model.has_agent(state, to):
        return model.read_agent(state, to)
    wanted = _id(to)
    return next((agent for agent in model.agents(state) if wanted in {_id(i) for i in agent.ids}), None)


def _resume(where: Where, given: Record) -> str | None:
    """A resume refused where its worktree is gone, its report unjudged or its context too large; else its next report.

    The context's ceiling spares the answer to a report stopped on a question, which closes that round.

    A message to an agent mid-turn resumes nothing: it reads it at its next turn. A `running` record whose
    turn ended is resumed, since the send that wrote it may never have landed.
    """
    state, root = where.state, where.root
    if state is None or root is None:
        return None
    agent = _addressee(state, given.get("to"))
    if agent is None or agent.status == "briefed" or context.mid_turn(agent, where.session):
        return None
    name = agent.name
    why = None
    if (held := findings.gone(agent, root)) is not None:
        why = (
            f"{name}'s worktree is gone ({held}), so a resume has no worktree to run in:"
            f" {_settle(state, agent)}dispatch a successor briefed with what is left"
        )
    elif (unjudged := findings.unjudged(agent)) is not None:
        # `judge` reads an agent's latest round alone, so a round begun before it would leave this one's unread.
        why = unjudged
    elif agent.outcome not in report.STOPPED or agent.status not in ("reported", "running"):
        # A report stopped on a question is no ceiling's: its answer is the one message that closes the round,
        # and while its worktree stands `judge`, `land` and `prune --discard` wait for that answer, so a
        # successor could take nothing.
        # `running` lets a resend through whose first send failed; a usage stop keeps the old outcome but waits
        # on no answer, so the ceiling applies again.
        size, ceiling = context.size(agent, where.session), config.load(root).context_ceiling
        if size is not None and size > ceiling:
            why = (
                f"{name}'s context is {size:,} tokens, over the configuration's contextCeiling of {ceiling:,}:"
                " a resume sends it all again, so dispatch a successor briefed with what is left"
            )
    if why is not None and not _waived(where, f"the resume of {name}"):
        return _deny(why)
    resumed = _running(state, agent)
    if resumed is None:
        return None
    following, same = resumed
    message = given.get("message")
    if not isinstance(message, str):
        return None
    path = report.path(state, name, following)
    if same:
        told = f"This continues round {following}: complete your report at {path}."
    else:
        told = f"This is round {following}: write its report to {path}, titled `round {following}`."
    return _allow({**given, "message": f"{message.rstrip()}\n\n{told}"})


def _settle(state: Path, agent: model.Agent) -> str:
    """The verb that settles an agent no resume reaches, ahead of its successor's dispatch; empty where it is judged.

    No answer reaches it, so `judge` takes its report as it stands, a question included, and a judged record
    holds nothing a successor's brief needs. A round stopped before its report reads, which `judge` refuses,
    has nothing to bank, and `prune` settles it.
    """
    if agent.status in findings.JUDGED:
        return ""
    if agent.status == "reported" or findings.readable(state, agent):
        return f"`orchestra judge {agent.name}` banks its report, then "
    return "`orchestra prune` settles its record, then "


def _running(state: Path, agent: model.Agent) -> tuple[int, bool] | None:
    """The resumed agent `running` in the round it resumes, written before the message reaches it.

    Written here rather than after the send, since the agent's turn may end before a later hook runs,
    and a stop checked against the round before would be overwritten. A send this hook let through can
    still fail to land, leaving an idle agent `running`, so the retry is told its round again: the
    round it already holds, continued where its report exists. The round and whether it continues;
    None where the agent is no longer to be resumed.
    """
    held: list[tuple[int, bool]] = []

    def resume(before: model.Agent) -> model.Agent:
        if before.status == "briefed":
            return before
        if before.status == "running":
            round_ = _round(before)
            held.append((round_, report.path(state, before.name, round_).exists()))
            return before
        following = _resumed_round(before)
        held.append((following, following == _round(before)))
        added = _relative(state, report.path(state, before.name, following))
        return dataclasses.replace(before, status="running", reports={**before.reports, following: added})

    model.update_agent(state, agent.name, resume)
    if not held:
        return None
    log.record(state, "resume", f"{agent.name} round {held[0][0]}")
    return held[0]


# --- the away mode --------------------------------------------------------------------------------


def _away(where: Where) -> bool:
    """Whether the session is away and the event is its own: the lease holder's, a subagent's of it, or an agent's.

    A subagent's event carries its main session's `session_id`, so the lease holder's background agents
    are refused too, and no other Claude Code session in the checkout is.
    """
    state = where.state
    if state is None or not (state / store.AWAY).exists():
        return False
    if where.agent is not None:
        return True
    owner = store.lease(state).get("owner")
    return owner is not None and owner == where.data.get("session_id")


def _refused_away(where: Where, reason: object = None) -> bool:
    """The event's call recorded as refused while the session is away; whether it is away.

    Recorded past the lease, as an agent's own facts are: a stopped lease still refuses its calls.
    """
    if not _away(where):
        return False
    data = where.data
    record: Record = {
        "time": model.now(),
        "event": data.get("hook_event_name"),
        "tool": data.get("tool_name"),
        "input": _summary(data.get("tool_input")),
        "agent_id": data.get("agent_id"),
        "agent_type": data.get("agent_type"),
    }
    if isinstance(reason, str) and reason:
        record["reason"] = reason
    state = typing.cast("Path", where.state)
    with store.unleased():
        _quietly(lambda: store.append_jsonl(state / REFUSED, record))
    return True


def _summary(given: object) -> str:
    """A call's input on one short line: its first text value, such as a command or a path, else its JSON."""
    held = context.as_object(given)
    text = next((value for value in held.values() if isinstance(value, str) and value.strip()), None)
    return cli.one_line(text if text is not None else _json(held))[:SUMMARY_CHARS]


def permission_request(where: Where) -> str | None:
    """A prompt refused while the session is away, for the main session and each of its agents alike."""
    return _refuse_prompt() if _refused_away(where) else None


def _refuse_prompt() -> str:
    decision = {"behavior": "deny", "message": AWAY_PROMPT}
    return _json({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}})


def permission_denied(where: Where) -> str | None:
    """A refusal by auto mode, recorded while the session is away; the denial stands."""
    _refused_away(where, where.data.get("reason"))
    return None


# --- PostToolUse ----------------------------------------------------------------------------------


def post_tool_use(where: Where) -> str | None:
    if not where.coordinator or where.state is None or where.root is None:
        return None
    given = context.as_object(where.data.get("tool_input"))
    tool = where.data.get("tool_name")
    if tool == "Agent":
        response = context.as_object(where.data.get("tool_response"))
        said = _dispatched(where.state, where.root, given, response, where.session)
    elif tool == "SendMessage":
        said = _sent(where.state, where.root, given, where.session)
    else:
        said = None
    return _context("PostToolUse", said) if said else None


def _route_to(state: Path, root: Path, name: str, rows: list[str], session: str | None) -> str | None:
    """Each row routed to the agent on its own, so a row another agent holds keeps no other from it; what was not."""
    if not rows:
        return None
    owner = findings.owners(state, root)(name)
    # Read once for every row: each running agent's transcript is read to judge it, and the hook's caller waits.
    held = findings.live(state, session)
    refused: list[str] = []
    for row in rows:
        try:
            findings.route_rows(state, owner, [row], held)
        except Refused as refusal:
            refused.append(str(refusal))
    return f"Not routed to {name}: {'; '.join(refused)}" if refused else None


def _dispatched(state: Path, root: Path, given: Record, response: Record, session: str | None) -> str | None:
    """The id the harness gave a dispatched agent and `running` in its record, and its brief's `Rows:` routed to it.

    Routed here, so a row a brief names is the agent's as a row a message names is, and its landing closes it.
    """
    sub, name, ident = given.get("subagent_type"), given.get("name"), _id(response.get("agentId"))
    if not isinstance(sub, str) or not sub.startswith(PLUGIN) or not isinstance(name, str) or ident is None:
        return None
    if not model.has_agent(state, name):
        return None
    first = _relative(state, report.path(state, name))

    def start(held: model.Agent) -> model.Agent:
        return dataclasses.replace(
            held,
            ids=held.ids if ident in held.ids else (*held.ids, ident),
            status="running" if held.status == "briefed" else held.status,
            model=str(given["model"]) if isinstance(given.get("model"), str) else held.model,
            effort=str(given["effort"]) if isinstance(given.get("effort"), str) else held.effort,
            reports=held.reports or {1: first},
        )

    agent = model.update_agent(state, name, start)
    log.record(state, log.DISPATCH, f"{name} as {ident} on {agent.model or 'the inherited model'}")
    return _route_to(state, root, name, list(brief.header(state, name).rows), session)


def _sent(state: Path, root: Path, given: Record, session: str | None) -> str | None:
    """A message kept in its agent's messages file, and its `Rows:` routed; the resume is `_resume`'s."""
    agent = _addressee(state, given.get("to"))
    if agent is None:
        return None
    message = given.get("message")
    text = message if isinstance(message, str) else ""
    if text.strip():
        _keep_message(state, agent.name, text)
    rows = [row[0] for line in ROWS_RE.finditer(text) for row in model.ROW_RE.finditer(line["rows"])]
    return _route_to(state, root, agent.name, rows, session)


def _keep_message(state: Path, name: str, text: str) -> None:
    path = brief.brief_path(state, name).with_name(f"{name}.messages.md")
    entry = f"\n## {model.now()} coordinator\n\n{text.replace(chr(13) + chr(10), chr(10)).strip(chr(10))}\n"
    store.check_lease(state)
    with store.locked(path), path.open("ab") as out:
        out.write(entry.encode("utf-8"))


# --- SubagentStop and StopFailure -----------------------------------------------------------------


def subagent_stop(where: Where) -> str | None:
    """A limit stop marked `stopped`; any other stop refused while the report breaks its grammar."""
    agent, state = where.agent, where.state
    if agent is None or state is None:
        return None
    name, round_ = agent.name, _round(agent)
    last = str(where.data.get("last_assistant_message") or "")
    if LIMIT_RE.search(last):
        _stopped(state, agent, last)
        return None
    _record_branch(where, agent)
    path = report.path(state, name, round_)
    said = report.check(path, agent.type, where.cwd if agent.type in report.COMMITTING else None)
    outcome = None
    if said:
        if _refusals(state, agent) >= REFUSALS:
            _stopped(state, agent, f"its report was refused {REFUSALS} times: {said[0]}")
            return None
        if not _waived(where, f"the stop of {name} ({said[0]})"):
            log.record(state, "stop", f"{name} round {round_}: {said[0]}")
            faults = "\n".join(f"- {violation}" for violation in said)
            reason = f"Your report at {path} cannot be read yet. Repair each line, then stop again:\n{faults}"
            return _json({"decision": "block", "reason": reason})
    else:
        outcome = report.read(path, agent.type).status

    kept: list[str] = []

    def reported(held: model.Agent) -> model.Agent:
        # Stamped on every report, a second with no `running` between included: an item queued for it falls due by it.
        since = {**held.since, "reported": model.now()}
        if held.status in findings.JUDGED:
            kept.append(_kept(held))
            return dataclasses.replace(held, since=since)
        return dataclasses.replace(held, status="reported", outcome=outcome, reason=None, since=since)

    model.update_agent(state, name, reported)
    log.record(state, "report", f"{name} round {round_}: {outcome or 'let through unread'}{''.join(kept)}")
    return None


def _kept(held: model.Agent) -> str:
    """The log line's tail for a stop after the record was judged, landed or pruned, which keeps that status.

    A second judge of the round is refused, so `reported` or `stopped` written over it would hold the agent
    back for good.
    """
    was = "discarded" if held.reason == model.DISCARDED else held.status
    return f", after its record was {was}, which it stays: read the report for findings written since"


def _refusals(state: Path, agent: model.Agent) -> int:
    """How often this run of the agent has had its stop refused."""
    since = agent.since.get("running")
    start = datetime.datetime.fromisoformat(since) if since else None
    prefix = f"{agent.name} round {_round(agent)}:"
    return sum(
        entry.kind == "stop"
        and entry.text.startswith(prefix)
        and (start is None or datetime.datetime.fromisoformat(entry.time) >= start)
        for entry in log.entries(state)
    )


def _stopped(state: Path, agent: model.Agent, why: str) -> None:
    """The agent marked `stopped` for `why`, a record judged, landed or pruned keeping its status as at a report."""
    reason = cli.one_line(why)[:300]
    kept: list[str] = []

    def stopped(held: model.Agent) -> model.Agent:
        if held.status in findings.JUDGED:
            kept.append(_kept(held))
            return held
        return dataclasses.replace(held, status="stopped", reason=reason)

    model.update_agent(state, agent.name, stopped)
    log.record(state, "stopped", f"{agent.name}: {reason}{''.join(kept)}")


def stop_failure(where: Where) -> str | None:
    """An agent whose turn an API error ended, such as a usage limit, marked `stopped` with the error."""
    if where.agent is not None and where.state is not None:
        error = where.data.get("error")
        details = where.data.get("error_details") or where.data.get("last_assistant_message") or ""
        _stopped(where.state, where.agent, f"{error}: {details}")
    return None


# --- the coordinator's turn -----------------------------------------------------------------------


def stop(where: Where) -> str | None:
    """The turn continued once with what the coordinator can act on now."""
    state = where.state
    if not where.coordinator or state is None or where.data.get("stop_hook_active"):
        return None
    agents = model.agents(state)
    # Listed once for every report stopped on a question, and only once one is read.
    listed = functools.cache(lambda: {} if where.root is None else findings.worktrees(where.root))

    def reported(agent: model.Agent) -> str:
        if agent.outcome not in report.STOPPED:
            return f"{agent.name} reported ({agent.outcome}) and is not judged"
        # A report stopped on a question waits on an answer by message, which `judge` waits for too, unless
        # no answer can reach the agent: its worktree is gone, and its report is judged as it stands. The
        # root's git runs only here, so a turn's end with no such report pays nothing for it.
        root = where.root
        unreached = None if root is None else findings.unreached(agent, root, listed)
        return unreached or f"{agent.name} stopped {agent.outcome} and waits on your answer by message"

    undone = [reported(agent) for agent in agents if agent.status == "reported"]
    # `status` lists it as to resume: resumed, or replaced by a successor once `prune` has settled it.
    undone += [
        f"{agent.name} stopped ({agent.reason or 'no reason recorded'}) and is not resumed"
        for agent in agents
        if agent.status == "stopped"
    ]
    undone += [f"queue item {item.number} is due: {item.text}" for item in queue.due(state)]
    entries = log.entries(state)
    tasks = where.data.get("background_tasks")
    busy = any(context.working(agent, where.session) for agent in agents) or (
        isinstance(tasks, list) and len(typing.cast("list[object]", tasks)) > 0
    )
    # A session nobody watches must never stall unseen; an attended one ends its turn in front of its owner.
    if not busy and not _waits(agents, entries) and (state / store.AWAY).exists():
        undone.append('nothing runs, and no `orchestra note --waiting "<what>"` names what the session waits on')
    now = state / store.NOW
    if now.is_file():
        # Compared at the log's own precision, the second: an entry of NOW.md's second may follow it.
        written = datetime.datetime.fromtimestamp(now.stat().st_mtime).astimezone().replace(microsecond=0)
        # A note logs a fault or an observation, which moves no next action, so it is left out; a decision
        # taken while nobody could be asked does move one.
        later = sum(
            (entry.kind != log.NOTE or entry.text.startswith(log.DECISION))
            and datetime.datetime.fromisoformat(entry.time) >= written
            for entry in entries
        )
        if later >= NOW_EVENTS:
            undone.append(f"NOW.md predates the last {later} log entries")
    if due := _checkpoint(state, entries, agents, where.session):
        undone.append(due)
    if not undone:
        return None
    return _context("Stop", "Undone in this orchestra session:\n" + "\n".join(f"- {line}" for line in undone))


def _writer(agents: list[model.Agent], dispatched: str) -> bool:
    """Whether a dispatch entry, `<NAME> as <id> on <model>`, names a writer."""
    name = dispatched.partition(" as ")[0]
    return any(agent.name == name and agent.type == "writer" for agent in agents)


def _checkpoint(state: Path, entries: list[log.Entry], agents: list[model.Agent], session: str | None) -> str | None:
    """A line where the checkpoint is due and a plan line is unticked; None where it is not.

    It is due once a writer is dispatched while no estimate is saved, since start.md lets the researchers
    that settle the plan's premises go out before the first estimate. After the newest saved one it is due
    at a wave's end, where checkpoint.md runs it: no agent is live and a writer has landed since. A fleet
    that dispatches as work frees has no such end, so it is due too once `JUDGED` reports are judged since,
    each able to free work the plan holds back; a report whose brief's `Lens:` line is `DISCLOSURE_ONLY`, a
    disclosure-only read, frees none, so it is not counted.
    """
    saved = [path.stat().st_mtime for path in (state / store.ESTIMATES).glob("*") if path.is_file()]
    if saved:
        # Compared at the log's own precision, the second, as NOW.md is.
        newest = datetime.datetime.fromtimestamp(max(saved)).astimezone().replace(microsecond=0)
        landed = sum(
            agent.type == "writer"
            and (stamp := agent.since.get("landed")) is not None
            and datetime.datetime.fromisoformat(stamp) >= newest
            for agent in agents
        )
        # The transcripts are read last, and only where a landing could end a wave. A brief drafted and not yet
        # dispatched holds no wave open: review.md drafts the review's briefs while the last writers run.
        if landed and not any(agent.status == "running" and findings.is_live(agent, session) for agent in agents):
            why = f"no agent is live, and {landed} writer(s) landed since the last saved estimate ({newest:%H:%M})"
        else:
            # The brief is read last, so only an entry judged since the estimate costs a read.
            judged = sum(
                entry.kind == log.JUDGE
                and datetime.datetime.fromisoformat(entry.time) >= newest
                and not _disclosure_only(state, entry.text)
                for entry in entries
            )
            if judged < JUDGED:
                return None
            why = f"{judged} reports were judged since the last saved estimate ({newest:%H:%M})"
    elif any(entry.kind == log.DISPATCH and _writer(agents, entry.text) for entry in entries):
        why = "a writer has been dispatched, and no estimate is saved"
    else:
        return None
    try:
        unticked = sum(not ticked for _, ticked, _ in model.plan_lines(state))
    except Refused:
        return None
    if not unticked:
        return None
    lines = "1 plan line is" if unticked == 1 else f"{unticked} plan lines are"
    return f"{why}, and {lines} unticked: the checkpoint in checkpoint.md is due"


def _disclosure_only(state: Path, judged: str) -> bool:
    """Whether a judge entry's agent made a disclosure-only read, by its brief's `Lens:` line.

    The entry opens with the report's stem, `<NAME>` or `<NAME>.<round>` (`report.stem`), then `:`. A
    brief that cannot be read is counted, as the report of any other agent is.
    """
    name = judged.partition(":")[0].partition(".")[0]
    try:
        return brief.header(state, name).lens == DISCLOSURE_ONLY
    except Refused:
        return False


def _waits(agents: list[model.Agent], entries: list[log.Entry]) -> bool:
    """Whether a waiting note stands: one written after every agent's latest change of status."""
    notes = [entry.time for entry in entries if entry.kind == log.WAITING]
    if not notes:
        return False
    changed = [datetime.datetime.fromisoformat(stamp) for agent in agents for stamp in agent.since.values()]
    return not changed or datetime.datetime.fromisoformat(notes[-1]) >= max(changed)


def session_start(where: Where) -> str | None:
    """The state's path, the head of `orchestra status` and NOW.md, given back after a compaction or a resume.

    The overview leaves out its rows naming judged and pruned agents, `FINISHED_ROWS`; `orchestra status` keeps them.

    After a `/clear` the new session holds no lease, and no input names the session it replaced, so it
    is told only that a `/clear` stopped this state a moment ago, never that the stopped session was its own.
    """
    state, root = where.state, where.root
    if where.data.get("source") == CLEARED and state is not None:
        return _cleared(state)
    if not where.coordinator or state is None or root is None:
        return None
    clock = datetime.datetime.now().astimezone()
    lines = [f"This session coordinates the orchestra session whose state is {state}."]
    # Imported here, since no other event reads it and every hook would pay for its imports.
    overview = typing.cast(
        "Callable[[datetime.datetime, Path, Path, str | None], tuple[list[str], str]]",
        importlib.import_module("orchestra.status").coordinator,
    )
    try:
        shown, summary = overview(clock, state, root, where.session)
        # The rows naming finished agents, which nothing after a compaction acts on; the `agents` row counts them.
        shown = [line for line in shown if line.split(" ", 1)[0] not in FINISHED_ROWS]
        lines += [f"`orchestra status` at {clock.isoformat(timespec='seconds')}:", *shown[: cli.LINES - 2], summary]
    except Refused as refused:
        lines.append(f"`orchestra status` cannot be read: {refused}")
    now = state / store.NOW
    lines += ["", "NOW.md:", store.read_bytes(now).decode("utf-8", "replace") if now.is_file() else "(none)"]
    return _context("SessionStart", "\n".join(lines))


def _cleared(state: Path) -> str | None:
    """A line naming the state a `/clear` stopped in the last `CLEARED_SECONDS`; None where none did."""
    entries = log.entries(state)
    last = entries[-1] if entries else None
    if last is None or last.kind != "mode" or not last.text.startswith(STOPPED_BY_CLEAR):
        return None
    age = (datetime.datetime.now().astimezone() - datetime.datetime.fromisoformat(last.time)).total_seconds()
    if age > CLEARED_SECONDS:
        return None
    old = last.text.removeprefix(STOPPED_BY_CLEAR).split(":", 1)[0]
    said = (
        f"A /clear ended session {old} {age:.0f} s ago, which coordinated the orchestra session whose state is"
        f' {state}. If this conversation continues that work, `orchestra mode takeover "<reason>"` takes it over.'
    )
    return _context("SessionStart", said)


def session_end(where: Where) -> str | None:
    """The lease marked stopped, as `orchestra mode stop` marks it, where `/clear` ended the session holding it.

    `/clear` gives the conversation a new session id (Claude Code's env-vars.md), and no input names
    the old one to the new session, so the lease cannot pass to it unasked; once stopped, the new
    session takes it with `orchestra mode takeover` at once rather than after ten quiet minutes.
    Another reason, such as a closed window, keeps the lease: that session may be resumed by its id.
    """
    state, session = where.state, where.data.get("session_id")
    if not where.coordinator or state is None or where.data.get("reason") != CLEARED or not isinstance(session, str):
        return None
    # Unleased: the hook's process may carry the new id already, which holds no lease.
    with store.unleased():
        if store.release_lease(state, session):
            log.record(state, "mode", f'{STOPPED_BY_CLEAR}{session}: `orchestra mode takeover "<reason>"`')
    return None


def user_prompt_submit(where: Where) -> str | None:
    """The clock read now, the minutes since the previous prompt, and a context nearing its compaction window.

    From `NEAR_WINDOW` of the window the coordinator reads that the compact tool compacts at its next seam. Past
    the window, which an applied window never lets the context reach, the owner reads it too, in a `systemMessage`:
    the window has not applied, and where mods do not load only the owner can type `/compact` (skills.md: the model
    cannot run it). A `Stop` hook would carry it at a model call each time it fired.
    """
    state = where.state
    if not where.coordinator or state is None:
        return None
    clock = datetime.datetime.now().astimezone()
    path = state / PROMPT
    previous: datetime.datetime | None = None
    if path.is_file():
        try:
            previous = datetime.datetime.fromisoformat(str(store.read_json(path).get("time")))
        except (Refused, ValueError):
            previous = None
    _quietly(lambda: store.write_json(path, {"time": clock.isoformat(timespec="seconds")}))
    said = f"The clock reads {clock:%Y-%m-%d %H:%M %z}."
    if previous is not None:
        gap = int((clock - previous).total_seconds() // 60)
        passed = "Less than a minute has" if gap < 1 else "1 minute has" if gap == 1 else f"{gap} minutes have"
        said += f" {passed} passed since the previous prompt."
        if gap > STALE_MINUTES:
            said += " A usage figure read before that is stale: `orchestra status --measure` reads it again."
    near = _near_window(where)
    if near is None:
        return _context("UserPromptSubmit", said)
    size, window = f"{near[0]:,}", f"{near[1]:,}"
    # Facts, not an order: an imperative in added context can read as an injection (hooks.md, "Add context for Claude").
    tool = f"`{COMPACT_TOOL}`, where the session has it, or else the owner's `/compact`, compacts at the next seam"
    if near[0] <= near[1]:
        said += f" Your context is {size} tokens, at least {NEAR_WINDOW:.0%} of the {window}-token compaction"
        said += f" window; {tool}."
        return _context("UserPromptSubmit", said)
    said += f" Your context is {size} tokens, past the {window}-token compaction window, which has not applied; {tool}."
    shown = (
        f"orchestra: this session's context is {size} tokens, past its {window}-token compaction window, which"
        " has not applied: the coordinator compacts at its next seam where its compact tool loads, or type"
        " `/compact` then."
    )
    return _json(
        {
            "systemMessage": shown,
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": said[:CONTEXT_CHARS]},
        }
    )


def _near_window(where: Where) -> tuple[int, int] | None:
    """The main session's context and its compaction window, from `NEAR_WINDOW` of it on; None below that.

    No hook input carries the main session's context, so it is read from its transcript, which may lag the
    conversation (hooks.md, "Common input fields"). A desktop session started from a pre-warmed process was
    seen running past the window its project's settings set (anthropics/claude-code issue 105316), which no
    other sign shows.
    """
    given, root = where.data.get("transcript_path"), where.root
    if not isinstance(given, str) or not given or root is None:
        return None
    try:
        last = context.last_call(Path(given))
    except OSError:
        return None
    if last is None:
        return None
    size = context.tokens(context.as_object(last.get("usage")))
    model = last.get("model")
    # Imported here, since no other event reads the settings files and every hook would pay for its imports.
    read = typing.cast("Callable[[Path, str | None], int | None]", importlib.import_module("orchestra.init").window)
    window = read(root, model if isinstance(model, str) else None)
    return (size, window) if window is not None and size >= NEAR_WINDOW * window else None


HANDLERS: dict[str, Callable[[Where], str | None]] = {
    "PreToolUse": pre_tool_use,
    "PermissionRequest": permission_request,
    "PermissionDenied": permission_denied,
    "PostToolUse": post_tool_use,
    "SubagentStop": subagent_stop,
    "StopFailure": stop_failure,
    "Stop": stop,
    "SessionStart": session_start,
    "SessionEnd": session_end,
    "UserPromptSubmit": user_prompt_submit,
}
