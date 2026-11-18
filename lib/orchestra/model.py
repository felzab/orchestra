"""What the state holds, and the one reader and writer of each kind every verb goes through.

Four kinds live in files: an agent's record (`agents/<NAME>.json`, rewritten whole), and finding
events, queue items and rulings (JSONL, one line per change, never rewritten). A JSONL line holds
its item's whole state, so an item's state is its last line; a write costs one line. The session's
fixed facts are `session.json`.
"""

from __future__ import annotations

import dataclasses
import functools
import re
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

from orchestra import Refused, store
from orchestra.store import Record, now

AgentType = Literal["writer", "driver", "auditor", "researcher", "verifier"]
# A hook writes the first three as the harness reports them; the verbs write the rest.
AgentStatus = Literal["briefed", "running", "reported", "judged", "landed", "pruned", "stopped"]
AGENT_STATUSES: tuple[AgentStatus, ...] = typing.get_args(AgentStatus)
# The statuses after which an agent's work is on the session branch, or was dropped.
SETTLED: frozenset[AgentStatus] = frozenset({"landed", "pruned"})
# The reason of a `pruned` agent whose work was thrown away, not landed.
DISCARDED = "discarded by `orchestra prune --discard`"

# OPEN: banked, owned by nobody. ROUTED: one owner, an agent or a queue, holds it. The rest close
# it on evidence: FIXED by a landed commit, RULED by the owner, HANDOFF to the next session, NOT A
# DEFECT on evidence that it is none, MOOT where later work removed what it was about.
FindingStatus = Literal["OPEN", "ROUTED", "FIXED", "RULED", "HANDOFF", "NOT A DEFECT", "MOOT"]
FINDING_STATUSES: tuple[FindingStatus, ...] = typing.get_args(FindingStatus)
CLOSED: frozenset[FindingStatus] = frozenset({"FIXED", "RULED", "HANDOFF", "NOT A DEFECT", "MOOT"})
FindingKind = Literal["bank", "route", "close", "reopen"]

# The queues every project has, which the plugin's pages route to; `queues` in the configuration adds more.
QUEUES = ("owner", "ending", "handoff")

Trigger = Literal["ci", "landed", "reported"]

# Upper case, so a row id `<NAME>-F<n>` reads apart from prose and a branch name. A resumed agent's
# round r of 2 or more numbers its rows `<NAME>.<r>-F<n>`: a name holds no `.`, so the two never meet.
NAME_RE = re.compile(r"[A-Z][A-Z0-9-]*")
# The names Windows keeps for devices whatever extension follows, which `NAME_RE` admits: an agent's
# record, brief and report named so would reach the device, not a file (Microsoft's "Naming Files,
# Paths, and Namespaces").
DEVICES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"{port}{n}" for port in ("COM", "LPT") for n in range(1, 10))})
# A task line of PLAN.md: a list item opening on its tick box.
TASK_RE = re.compile(r"\s*[-*+] \[(?P<tick>[ xX])\] (?P<text>.*)")
ROW_RE = re.compile(r"(?P<agent>[A-Z][A-Z0-9-]*)(?:\.(?P<round>[2-9]|[1-9][0-9]+))?-F(?P<n>[1-9][0-9]*)")

T = TypeVar("T")

# Read once per class: a read of the log or the findings file holds one record a line, each of a class read before.
_hints = functools.cache(typing.get_type_hints)


@dataclass(frozen=True)
class Session:
    programme: str
    session: str
    branch: str
    base: str
    base_commit: str
    created: str
    # Where the programme's first ruling number starts, carried over from an earlier record.
    rulings_from: int | None = None
    # The primary checkout the session was made in, so a clone sharing only its folder name never shares
    # its programme; None in a record an earlier build wrote.
    checkout: str | None = None


@dataclass(frozen=True)
class Agent:
    name: str
    type: AgentType
    status: AgentStatus = "briefed"
    model: str | None = None
    effort: str | None = None
    # Every id the harness gave it: a resumed agent can be given another.
    ids: tuple[str, ...] = ()
    # Each round's report, by round number.
    reports: dict[int, str] = field(default_factory=dict[int, str])
    branch: str | None = None
    verdict: str | None = None
    # The status word the last report opens with: DONE, DONE-WITH-CONCERNS, BLOCKED or NEEDS-ANSWER.
    outcome: str | None = None
    # Why the agent is `stopped`, such as a usage limit, so it can be resumed; DISCARDED once
    # `prune --discard` threw its work away.
    reason: str | None = None
    # When it entered each status, which `update_agent` stamps.
    since: dict[str, str] = field(default_factory=dict[str, str])
    # The SHA-256 of the brief's bytes when `brief --check` passed; a later edit makes it unchecked.
    checked: str | None = None


@dataclass(frozen=True)
class FindingEvent:
    row: str
    kind: FindingKind
    status: FindingStatus
    owner: str | None = None
    evidence: str | None = None
    time: str = field(default_factory=now)

    def __post_init__(self) -> None:
        # Every writer goes through here, so a row can never be ROUTED to nobody or closed on nothing.
        if not ROW_RE.fullmatch(self.row):
            raise ValueError(f"`{self.row}` is no row id: <NAME>-F<n>")
        if self.status == "ROUTED" and not self.owner:
            raise ValueError(f"{self.row} is ROUTED to nobody")
        if self.status in CLOSED and not self.evidence:
            raise ValueError(f"{self.row} is closed {self.status} on no evidence")
        wanted: dict[FindingKind, frozenset[str]] = {
            "route": frozenset({"ROUTED"}),
            "close": CLOSED,
            "reopen": frozenset({"OPEN", "ROUTED"}),
        }
        if self.status not in wanted.get(self.kind, FINDING_STATUSES):
            raise ValueError(f"a {self.kind} event cannot leave {self.row} {self.status}")


@dataclass(frozen=True)
class QueueItem:
    number: int
    trigger: Trigger
    text: str
    # The agent whose landing or report fires it; None for any.
    agent: str | None = None
    done: bool = False
    time: str = field(default_factory=now)


@dataclass(frozen=True)
class Ruling:
    """A question put to the owner, and once ruled, the ruling: its number and the owner's own words."""

    ask: int
    question: str
    session: str
    options: tuple[str, ...] = ()
    # The recommended option, a 1-based index into `options`; None for no recommendation.
    recommend: int | None = None
    asked: str = field(default_factory=now)
    number: int | None = None
    words: str | None = None
    date: str | None = None
    # The question asked in its place, where it was asked again: it is then neither open nor ruled on.
    superseded_by: int | None = None

    @property
    def open(self) -> bool:
        return self.number is None and self.superseded_by is None


def to_record(item: object) -> Record:
    return dataclasses.asdict(typing.cast("Any", item))


def _typed(hint: object, value: object) -> object:
    """A JSON value as the field's type holds it: a list as a tuple, an object's keys as numbers."""
    origin = typing.get_origin(hint)
    if origin is tuple and isinstance(value, list):
        return tuple(typing.cast("list[object]", value))
    if origin is dict and isinstance(value, dict) and typing.get_args(hint)[0] is int:
        held = typing.cast("dict[str, object]", value)
        return {int(key): item for key, item in held.items()}
    # The checks above leave `value` a dict of unknown types on one path, which strict typing refuses.
    return typing.cast("object", value)


def from_record(cls: type[T], record: Record, source: Path) -> T:
    """The item a record holds; refused naming the file where the record is not one."""
    hints = _hints(cls)
    # An unknown key is refused, so a rewrite by an older build never drops a newer build's field unseen.
    try:
        return cls(**{name: _typed(hints.get(name), value) for name, value in record.items()})
    except (TypeError, ValueError) as error:
        raise Refused(
            f"{source} holds a record orchestra cannot read ({error}): only orchestra writes it, so restore it"
        ) from None


def _fold(records: list[Record], cls: type[T], key: str, source: Path) -> dict[Any, T]:
    """Each item's state as its last line, in the order items first appear."""
    return {record[key]: from_record(cls, record, source) for record in records if key in record}


def _read(path: Path, cls: type[T], key: str) -> dict[Any, T]:
    return _fold(store.read_jsonl(path), cls, key, path)


def _write(path: Path, cls: type[T], key: str, edit: Callable[[dict[Any, T]], list[T]]) -> list[T]:
    """The items `edit` makes from every item's state, appended under one lock; those items."""

    def append(records: list[Record]) -> list[Record]:
        return [to_record(item) for item in edit(_fold(records, cls, key, path))]

    return [from_record(cls, record, path) for record in store.edit_jsonl(path, append)]


# --- the plan -------------------------------------------------------------------------------------


def plan_lines(state: Path) -> list[tuple[str, bool, str]]:
    """Every task line of PLAN.md as its section's heading, whether it is ticked, and its text.

    Here rather than in `status`, so the turn's end reads the plan without loading the status view.
    """
    try:
        text = store.read_bytes(state / store.PLAN).decode("utf-8", "replace")
    except FileNotFoundError:
        raise Refused(
            f"{state / store.PLAN} is missing: write it again under the headings `orchestra init` gives it"
        ) from None
    section = ""
    found: list[tuple[str, bool, str]] = []
    for line in text.splitlines():
        if line.startswith("## "):
            section = line.strip()
        elif task := TASK_RE.fullmatch(line):
            found.append((section, task["tick"] != " ", task["text"].strip()))
    return found


# --- the session ----------------------------------------------------------------------------------


def read_session(state: Path) -> Session:
    path = state / store.SESSION
    return from_record(Session, store.read_json(path), path)


def write_session(state: Path, session: Session) -> None:
    store.write_json(state / store.SESSION, to_record(session))


# --- agents ---------------------------------------------------------------------------------------


def agent_path(state: Path, name: str) -> Path:
    if not NAME_RE.fullmatch(name):
        raise Refused(f"`{name}` is no agent name: upper-case letters, digits and hyphens, a letter first")
    if name in DEVICES:
        raise Refused(
            f"`{name}` is a name Windows keeps for a device, so no file of it could be written: choose another"
        )
    return state / store.AGENTS / f"{name}.json"


def has_agent(state: Path, name: str) -> bool:
    return NAME_RE.fullmatch(name) is not None and name not in DEVICES and agent_path(state, name).exists()


def read_agent(state: Path, name: str) -> Agent:
    path = agent_path(state, name)
    if not path.exists():
        raise Refused(f"no agent {name} in this session: `orchestra status` lists them")
    return from_record(Agent, store.read_json(path), path)


def agents(state: Path) -> list[Agent]:
    """Every agent of the session, by name."""
    paths = sorted((state / store.AGENTS).glob("*.json"))
    return [from_record(Agent, store.read_json(path), path) for path in paths]


def update_agent(state: Path, name: str, edit: Callable[[Agent], Agent]) -> Agent:
    """The agent's record changed by `edit` under its lock, so a hook and a verb never overwrite each other."""
    path = agent_path(state, name)

    def change(held: Record | None) -> Record:
        if held is None:
            raise Refused(f"no agent {name} in this session: `orchestra status` lists them")
        before = from_record(Agent, held, path)
        after = edit(before)
        # An edit may build a fresh record, so the earlier stamps are carried from the one held.
        since = {**before.since, **after.since}
        if after.status != before.status:
            since[after.status] = now()
        return to_record(dataclasses.replace(after, since=since))

    return from_record(Agent, store.update_json(path, change), path)


# --- findings -------------------------------------------------------------------------------------


def read_findings(state: Path) -> dict[str, FindingEvent]:
    return _read(state / store.FINDINGS, FindingEvent, "row")


def write_findings(state: Path, edit: Callable[[dict[str, FindingEvent]], list[FindingEvent]]) -> list[FindingEvent]:
    return _write(state / store.FINDINGS, FindingEvent, "row", edit)


# --- the queue ------------------------------------------------------------------------------------


def read_queue(state: Path) -> dict[int, QueueItem]:
    return _read(state / store.QUEUE, QueueItem, "number")


def write_queue(state: Path, edit: Callable[[dict[int, QueueItem]], list[QueueItem]]) -> list[QueueItem]:
    return _write(state / store.QUEUE, QueueItem, "number", edit)


# --- rulings --------------------------------------------------------------------------------------


def rulings_path(state: Path) -> Path:
    return state.parent / store.RULINGS


def read_rulings(state: Path) -> dict[int, Ruling]:
    """The programme's questions and rulings, by the number each question was asked under."""
    return _read(rulings_path(state), Ruling, "ask")


def write_rulings(state: Path, edit: Callable[[dict[int, Ruling]], list[Ruling]]) -> list[Ruling]:
    return _write(rulings_path(state), Ruling, "ask", edit)


def next_ruling(state: Path, rulings: dict[int, Ruling]) -> int:
    """The number the next ruling takes: one past the programme's highest, or where any of its sessions starts them."""
    numbers = [ruling.number + 1 for ruling in rulings.values() if ruling.number is not None]
    for path in state.parent.glob(f"*/{store.SESSION}"):
        if (start := from_record(Session, store.read_json(path), path).rulings_from) is not None:
            numbers.append(start)
    return max(numbers, default=1)
