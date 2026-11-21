"""`status`: one read of the session's state, each figure taken from a state file, git or the harness's files.

It exits 0 whatever it finds, since a skill injects its output and an injected command that fails
aborts the skill; a figure it cannot read is printed as unknown, with the reason, where it would stand.
`--measure` prints the plan's usage limits and the figures an estimate starts from, and `--end` each
line of the ending a program can check.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import functools
import itertools
import json
import shutil
import statistics
import subprocess
import traceback
import typing
from collections.abc import Callable, Iterable
from pathlib import Path

from orchestra import (
    DONE,
    Refused,
    brief,
    ci,
    cli,
    config,
    context,
    diff,
    init,
    log,
    model,
    override,
    process,
    prune,
    queue,
    report,
    store,
)
from orchestra.model import plan_lines
from orchestra.store import Record

# The round, or the number of distinct agents a row was routed to, at which an agent or a row is
# re-planned with a fresh agent or a new slice rather than sent again: past it, rounds stop converging.
REPEATS = 3
# The size past which NOW.md has taken what belongs in PLAN.md or the log.
NOW_BYTES = 3000
# Its labelled lines; the state holds the rest of where the session stands, which `_standing` prints.
NOW_LINES = len(init.NOW_LABELS)
# A NOW.md line is cut here in the head; the file holds it whole.
NOW_WIDTH = 160
# Statuses an agent holds once it has reported at least once.
FINISHED = ("reported", "judged", "landed", "pruned")
# Statuses whose latest round no judge has banked: a report waiting, or a round stopped before its report.
UNJUDGED = frozenset({"reported", "stopped"})
CI_SHOWN = 3
# A CI reading that holds the final head green: a `none` one found a project running no CI, where nothing
# can fail. An `expected` one failed on rows a later landing clears, and at the end every landing is in.
GREEN = frozenset({"success", "none"})
# The column a line's label fills, so the figures line up.
LABEL = 10
# The most `waits` lines `land` and `judge` close on; `status --measure` prints every one.
WAITS_SHOWN = 5
# The most pairs of unticked slices sharing files that the drift names; a count says how many more.
PAIRS_SHOWN = 5
# The most chains tied for the longest that `--measure` names; a count says how many more.
CHAINS_SHOWN = 3
# The usage limits as `/usage` reports them run headless, with no customisation loaded and no transcript
# kept; its assistant message carries `usage_report` (the Agent SDK's TypeScript reference, `SDKUsageReport`).
USAGE_RUN = ("-p", "/usage", "--safe-mode", "--no-session-persistence", "--output-format", "stream-json", "--verbose")
# The state file keeping the last reading, and how long it is printed again rather than read anew.
USAGE = "usage.json"
USAGE_FRESH = datetime.timedelta(minutes=5)
# Past this the `/usage` run is given up on. It stays short of the Bash tool's default limit of 120 s
# (`BASH_DEFAULT_TIMEOUT_MS`, Claude Code's environment variables page), with room for the rest of the view,
# so a hung run prints its unknown row with the reason rather than timing out the call that ran `status`.
USAGE_SECONDS = 90
# The severity of a usage row far from its limit; any other, such as `warning`, is printed.
NORMAL = "normal"


class _Held(str):
    """An ending's line that holds, saying why."""


@dataclasses.dataclass(frozen=True)
class Reads:
    """What several sections of a view read, each read at its first use and kept for the rest of the view.

    No view writes the log, the plan or a brief, so a second read would find the same. A read that is
    refused is not kept: each section that needs it reads it again and names the fault, as it would alone.
    """

    state: Path
    agents: list[model.Agent]

    @functools.cached_property
    def entries(self) -> list[log.Entry]:
        return log.entries(self.state)

    @functools.cached_property
    def plan(self) -> list[tuple[str, bool, str]]:
        return plan_lines(self.state)

    @functools.cached_property
    def ticks(self) -> dict[str, bool]:
        return model.slices(self.plan)

    @functools.cached_property
    def planned(self) -> list[tuple[str, list[str], list[str]]]:
        return model.slice_lines(self.plan)

    @functools.cached_property
    def briefs(self) -> dict[str, brief.Header | Refused]:
        """Each agent's brief header, or why it cannot be read."""
        held: dict[str, brief.Header | Refused] = {}
        for agent in self.agents:
            try:
                held[agent.name] = brief.header(self.state, agent.name)
            except Refused as refused:
                held[agent.name] = refused
        return held

    @functools.cached_property
    def heads(self) -> dict[str, brief.Header]:
        """Each brief header that can be read: a brief that cannot is named by the overview's drift."""
        return {name: head for name, head in self.briefs.items() if isinstance(head, brief.Header)}

    @functools.cached_property
    def landed(self) -> set[str]:
        return queue.landed_slices(self.state, self.agents, self.heads)

    @functools.cached_property
    def finished(self) -> set[str]:
        """Each slice ticked or landed: what no `waits`, overlap or chain line reads as still to do."""
        return {name for name, ticked in self.ticks.items() if ticked} | self.landed

    @functools.cached_property
    def untaken(self) -> list[str]:
        """Each unticked slice no agent's brief names: work planned ahead while running, drift once the session ends.

        The overview, the ending and `waits` all read it here, so `status` and `land` never disagree on what
        waits. The brief of an agent settled with nothing landed takes nothing, since its work never landed.
        """
        taken = queue.on_slice(self.state, self.agents, self.heads)
        return [name for name, ticked in self.ticks.items() if not ticked and name not in taken]

    @functools.cached_property
    def due(self) -> list[model.QueueItem]:
        try:
            entries = self.entries
        except Refused:
            # Left to `due`, which reads the log only where an item waits, as it does called alone.
            entries = None
        return queue.due(self.state, self.agents, entries)


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """What every view reads, read once."""

    clock: datetime.datetime
    state: Path
    root: Path
    session: model.Session
    agents: list[model.Agent]

    @functools.cached_property
    def reads(self) -> Reads:
        return Reads(self.state, self.agents)

    @functools.cached_property
    def head(self) -> str:
        """The session branch's commit."""
        return config.git(self.root, "rev-parse", f"refs/heads/{self.session.branch}").strip()


def add(verb: str, parser: argparse.ArgumentParser) -> None:
    del verb  # the module's one verb
    view = parser.add_mutually_exclusive_group()
    view.add_argument("--measure", action="store_true", help="the usage limits and the figures an estimate starts from")
    view.add_argument("--end", action="store_true", help="each machine-checkable line of the ending, held or not")


def run(args: argparse.Namespace) -> int:
    clock = datetime.datetime.now().astimezone()
    head = f"{clock.isoformat(timespec='seconds')} · a snapshot"
    try:
        lines, summary = _view(args, clock)
    except Refused as refused:
        lines, summary = [], f"status: {refused}"
    except Exception as error:  # noqa: BLE001 -- the contract is exit 0: the fault is printed, not swallowed.
        traceback.print_exc()
        lines, summary = [], f"status: failed with {type(error).__name__}: {error}; the trace is on stderr"
    cli.emit(args, [head, *lines] if lines else [head], summary)
    return DONE


def _view(args: argparse.Namespace, clock: datetime.datetime) -> tuple[list[str], str]:
    here = config.checkout()
    state = store.state_dir(held=here)
    session = model.read_session(state)
    snap = Snapshot(clock, state, here.root, session, model.agents(state))
    title = _title(snap)
    if args.measure:
        lines, summary = measure(snap)
    elif args.end:
        lines, summary = end(snap)
    else:
        lines, summary = overview(snap)
    return [title, *lines], summary


def _title(snap: Snapshot) -> str:
    session = snap.session
    return f"session {session.programme}/{session.session} on {session.branch}, state {snap.state}"


def _row(label: str, text: str) -> str:
    return f"{label:<{LABEL - 1}} {text}"


def _guarded(label: str, read: Callable[[], list[str]]) -> tuple[list[str], int | None]:
    """A section's lines and their count, or one line saying it is unknown and why and no count.

    One unreadable file then hides no other section.
    """
    try:
        lines = read()
    except Refused as refused:
        return [_row(label, f"unknown: {refused}")], None
    return lines, len(lines)


def _count(count: int | None, what: str) -> str:
    return f"{what} unknown" if count is None else f"{count} {what}"


def _minutes(since: datetime.timedelta) -> int:
    return int(since.total_seconds() // 60)


def _stamp(agent: model.Agent, status: str) -> datetime.datetime | None:
    held = agent.since.get(status)
    try:
        return datetime.datetime.fromisoformat(held) if held else None
    except ValueError:
        return None


def _who(agent: model.Agent) -> str:
    rounds = len(agent.reports)
    said = f"{agent.name} ({agent.type}, round {rounds}"
    said += f", {agent.outcome})" if agent.outcome and agent.outcome not in report.STOPPED else ")"
    if rounds >= REPEATS and agent.status not in model.SETTLED:
        said += f"; {rounds} rounds: re-plan it with a fresh agent or a new slice"
    return said


# --- the overview -----------------------------------------------------------------------------------


def coordinator(clock: datetime.datetime, state: Path, root: Path, caller: str | None = None) -> tuple[list[str], str]:
    """The overview a coordinator's session is given when it starts, resumes or compacts, under `status`'s title.

    The title names the state and the session's branch, and the overview each running agent, which is
    all a compaction's summary must keep. NOW.md follows it there, whole where it fits the hook's room,
    so its `now` lines are left out rather than given twice. `caller` is the Claude Code session the
    hook's input names, whose folder holds the transcripts of the agents it dispatched.
    """
    snap = Snapshot(clock, state, root, model.read_session(state), model.agents(state))
    lines, summary = overview(snap, now=False, caller=caller)
    return [_title(snap), *lines], summary


def overview(snap: Snapshot, *, now: bool = True, caller: str | None = None) -> tuple[list[str], str]:
    lines, _ = _guarded("away", lambda: _away(snap.state))
    by: dict[str, list[model.Agent]] = {status: [] for status in model.AGENT_STATUSES}
    for agent in snap.agents:
        by[agent.status].append(agent)
    counts = ", ".join(f"{len(held)} {status}" for status, held in by.items() if held)
    lines.append(_row("agents", counts or "none"))
    suspect = 0
    for agent in by["running"]:
        quiet, why = context.ended(agent, snap.clock, caller)
        if why:
            suspect += 1
            lines.append(_row("suspect", f"{_who(agent)}: running, but {why}"))
        else:
            lines.append(_row("running", f"{_who(agent)}: transcript written {quiet} min ago"))
    reported = by["reported"]
    waiting = [agent for agent in reported if agent.outcome in report.STOPPED]
    lines += [_row(str(agent.outcome), f"{_who(agent)}: its report waits on you") for agent in waiting]
    lines += [_row("to judge", _who(agent)) for agent in reported if agent not in waiting]
    lines += [_row("to resume", f"{_who(agent)}: {agent.reason or 'no reason recorded'}") for agent in by["stopped"]]
    # A judged agent is done unless its branch holds commits the session branch lacks: one line names the done.
    try:
        ahead = _ahead(snap, _branches(snap, caller))
    except Refused as refused:
        ahead = {}
        lines.append(_row("to land", f"unknown: {refused}"))
    lines += [
        _row("to land", _who(agent) + ("" if ahead[agent.name] else f": branch unknown; {_discard(agent.name)}"))
        for agent in by["judged"]
        if agent.name in ahead
    ]
    if done := [agent.name for agent in by["judged"] if agent.name not in ahead]:
        lines.append(_row("judged", ", ".join(done)))
    for status in ("briefed", "landed", "pruned"):
        if by[status]:
            lines.append(_row(status, ", ".join(agent.name for agent in by[status])))
    due, due_count = _guarded("due", lambda: [_row("due", _item(item)) for item in snap.reads.due])
    try:
        unclosed, findings = _findings(snap.state, snap.root)
    except Refused as refused:
        unclosed, findings = None, [_row("unclosed", f"unknown: {refused}")]
    ci, _ = _guarded("CI", lambda: _ci(snap))
    drift, drifted = _guarded("drift", lambda: _drift(snap))
    # Planned ahead, such as the review, it is no drift until the session ends; `--end` names it then.
    planned, _ = _guarded("planned", lambda: [_row("planned", said) for said in [_named(snap.reads.untaken)] if said])
    armed, _ = _guarded("override", lambda: [_row("override", said) for said in [_armed(snap.state)] if said])
    standing, _ = _guarded("waiting", lambda: _standing(snap))
    lines += [*due, *findings, *ci, *drift, *planned, *armed, *standing, *_now(snap.state, shown=now)]
    parts = [
        f"{len(by['running'])} running" + (f" ({suspect} suspect)" if suspect else ""),
        f"{len(reported)} to judge",
        f"{len(by['stopped'])} to resume",
        _count(due_count, "due"),
        _count(unclosed, "unclosed"),
        _count(drifted, "drift"),
    ]
    return lines, "status: " + ", ".join(parts)


def _away(state: Path) -> list[str]:
    """A line while the session is away: a resumed or compacted coordinator reads no other sign of it."""
    path = state / store.AWAY
    if not path.exists():
        return []
    held = store.read_json(path)
    since, until = str(held.get("since", ""))[:16].replace("T", " "), held.get("until")
    said = f"since {since}" + (f", until {until}" if until else "") + ": `orchestra mode attended` ends it"
    return [_row("away", said)]


def _item(item: model.QueueItem) -> str:
    when = f"{item.trigger}:{item.agent}" if item.agent else item.trigger
    return f"#{item.number} (when {when}) {item.text}"


def _findings(state: Path, root: Path) -> tuple[int, list[str]]:
    """The count of unclosed rows, and one line per owner naming each, a row routed to three agents marked.

    Only the distinct agents count: the review's own flow routes a row to a queue, to it again with a
    verifier's correction, then to its fixer, and none of that bounces the row between agents.
    """
    queues = {*model.QUEUES, *config.load(root).queues}
    routes: dict[str, set[str]] = {}
    records = store.read_jsonl(state / store.FINDINGS)
    for record in records:
        owner = record.get("owner")
        # The status, not the kind: a row closed FIXED goes to its next fixer by a `reopen` event.
        if record.get("status") == "ROUTED" and owner not in queues:
            routes.setdefault(str(record.get("row")), set()).add(str(owner))
    owners: dict[str, list[str]] = {}
    unclosed = 0
    for row, event in model.read_findings(state, records).items():
        if event.status in model.CLOSED:
            continue
        unclosed += 1
        routed = len(routes.get(row, ()))
        said = f"{row} (routed to {routed} agents: re-plan it)" if routed >= REPEATS else row
        owners.setdefault(event.owner or "nobody (OPEN)", []).append(said)
    return unclosed, [_row("unclosed", f"{owner}: {', '.join(rows)}") for owner, rows in owners.items()]


def _reading(reading: ci.Reading) -> str:
    took = "" if reading.minutes is None else f", {reading.minutes:.0f} min"
    return f"{reading.commit[:9]} {reading.conclusion}{took}, read {reading.time}"


def _ci(snap: Snapshot) -> list[str]:
    readings = ci.readings(snap.state)
    if not readings:
        return [_row("CI", "no reading yet")]
    head = snap.head
    said = _reading(readings[0])
    if readings[0].commit != head:
        said += f"; the head {head[:9]} has no reading"
    return [_row("CI", said)]


def _drift(snap: Snapshot) -> list[str]:
    """What drifted from the plan: files no landed brief or ticked slice owns, agents on no slice, and slices
    planned as parallel that share files.

    A ticked slice's line names the files it wrote in backticks, `` - [x] <SLICE> `<file>`... ``, which
    owns them as a brief would: the coordinator makes the edits it can spell out itself, with no brief.
    Only a backticked path owns, since any other word, such as a reader's slice described in prose,
    would own the folder of its name.
    """
    reads = snap.reads
    lines: list[str] = []
    planned = reads.ticks
    owned = [
        name
        for section, ticked, text in reads.plan
        if section == model.SLICES and ticked
        for name in model.CODE_RE.findall(text.partition(" ")[2])
    ]
    for agent in snap.agents:
        header = reads.briefs[agent.name]
        if isinstance(header, Refused):
            lines.append(_row("drift", f"agent {agent.name}: its brief cannot be read ({header})"))
            continue
        # A reader or a driver briefed with no slice works outside the plan, which is no drift: neither lands work.
        if header.slice not in planned and not (header.slice == brief.NONE and agent.type != brief.SLICED):
            lines.append(_row("drift", f"agent {agent.name}: on no slice of PLAN.md (its slice is {header.slice!r})"))
        # An agent settled with nothing landed, discarded or pruned before its work, excuses no change.
        if agent.status in model.SETTLED and not queue.landed_nothing(agent):
            owned += header.owns
    branch = f"refs/heads/{snap.session.branch}"
    # `-z` gives each change as its status letter and its path, in fields of their own; under `--no-renames`
    # a move is a deletion and an addition, so `D` names every path the session moved or deleted.
    fields = config.git(snap.root, "diff", "--name-status", "--no-renames", "-z", snap.session.base_commit, branch)
    parts = fields.split("\0")
    changed = list(zip(parts[::2], parts[1::2], strict=False))
    for _, path in changed:
        if not brief.owned(owned, path):
            lines.append(_row("drift", f"{path}: changed since the base, and no landed brief or ticked slice owns it"))
    # A path a landing moved stays on the lines still to do, where `waits` and the overlaps read it as written.
    gone = {path for letter, path in changed if letter == "D"}
    for name, files, _ in reads.planned:
        if name not in reads.finished:
            lines += [
                _row("drift", f"slice {name} names `{path}`, which the session deleted or moved since its base")
                for path in files
                if path in gone
            ]
    return lines + _parallel(reads)


def _parallel(reads: Reads) -> list[str]:
    """Each two unfinished slices whose files overlap while neither is after the other, however many steps back.

    The plan and the estimate read such a pair as parallel, while `brief --check` refuses the second's
    brief as long as the first's agent runs. A landed slice shares nothing, ticked or not.
    """
    planned = reads.planned
    after = {name: named for name, _, named in planned}
    finished = reads.finished
    unticked = [(name, files) for name, files, _ in planned if name not in finished and files]
    reach = {name: _before(name, after) for name, _ in unticked}
    lines: list[str] = []
    for at, (one, mine) in enumerate(unticked):
        for other, theirs in unticked[at + 1 :]:
            if one in reach[other] or other in reach[one]:
                continue
            if shared := [path for path in theirs if any(brief.overlap(path, held) for held in mine)]:
                said = f"share {len(shared)} file(s), `{shared[0]}` first, and neither is after the other"
                lines.append(
                    _row(
                        "drift",
                        f"slices {one} and {other} {said}: the second's brief is refused while the first runs,"
                        f" so name {one} in {other}'s `after:`",
                    )
                )
    if len(lines) > PAIRS_SHOWN:
        # A plan ordered by its sections alone held 258 such pairs, which would push every later line out.
        more = f"{len(lines) - PAIRS_SHOWN} more pair(s) of slices share files likewise"
        lines = [*lines[:PAIRS_SHOWN], _row("drift", f"{more}: each is named here once those above are ordered")]
    return lines


def _before(name: str, after: dict[str, list[str]]) -> set[str]:
    """Every slice the named one's `after:` reaches, through the slices it names in turn."""
    seen: set[str] = set()
    todo = list(after.get(name, ()))
    while todo:
        if (held := todo.pop()) not in seen:
            seen.add(held)
            todo += after.get(held, ())
    return seen


def _clock(stamp: str) -> str:
    """A stamp the program wrote, as its hour and minute."""
    return stamp[11:16]


def _standing(snap: Snapshot) -> list[str]:
    """What the state holds that NOW.md once copied: the waiting note, the newest estimate and the last gate run."""
    entries = snap.reads.entries
    lines: list[str] = []
    notes = [entry for entry in entries if entry.kind == log.WAITING]
    if notes:
        note = notes[-1]
        noted = datetime.datetime.fromisoformat(note.time)
        moved = any(stamp and stamp > noted for agent in snap.agents for stamp in map(_parsed, agent.since.values()))
        stale = "; an agent has changed status since: note what the session waits on now" if moved else ""
        lines.append(_row("waiting", f"{note.text}, noted {_clock(note.time)}{stale}"))
    saved = [path for path in (snap.state / store.ESTIMATES).glob("*") if path.is_file()]
    if saved:
        newest = max(saved, key=lambda path: path.stat().st_mtime)
        at = datetime.datetime.fromtimestamp(newest.stat().st_mtime).astimezone()
        lines.append(_row("estimate", f"{store.ESTIMATES}/{newest.name}, saved {at:%H:%M}"))
    if runs := [entry for entry in entries if entry.kind == ci.GATE]:
        lines.append(_row("gate", f"{runs[-1].text}, at {_clock(runs[-1].time)}"))
    return lines


def _parsed(stamp: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return None


def _now(state: Path, *, shown: bool = True) -> list[str]:
    """NOW.md's first lines after its title unless not `shown`, and a line when it has grown past its bound."""
    try:
        data = store.read_bytes(state / store.NOW)
    except FileNotFoundError:
        return [_row("NOW.md", "missing: write its two labelled lines")]
    lines: list[str] = []
    if len(data) > NOW_BYTES:
        lines.append(
            _row("NOW.md", f"{len(data):,} bytes, past {NOW_BYTES:,}: move the rest to PLAN.md or `orchestra note`")
        )
    text = [line for line in data.decode("utf-8", "replace").splitlines() if line.strip() and not line.startswith("# ")]
    for line in text[:NOW_LINES] if shown else []:
        lines.append(_row("now", line if len(line) <= NOW_WIDTH else line[: NOW_WIDTH - 1] + "…"))
    return lines


# --- --measure --------------------------------------------------------------------------------------


def measure(snap: Snapshot) -> tuple[list[str], str]:
    # First, so a cut at forty lines never drops the figure a wide wave is weighed against.
    lines, _ = _guarded("usage", lambda: usage(snap.state, snap.root, snap.clock))
    try:
        ran, started = _rounds(snap.reads.entries)
    except Refused as refused:
        ran, started = {}, {}
        lines.append(_row("spans", f"unknown: {refused}"))
    spans: dict[str, list[float]] = {}
    for agent in snap.agents:
        # Every round: a resume stamps `running` again, so the record alone keeps only the last one.
        if agent.status in FINISHED and agent.name in ran:
            spans.setdefault(agent.type, []).append(ran[agent.name])
    for kind, held in sorted(spans.items()):
        lines.append(
            _row(
                kind,
                f"{len(held)} finished, median {statistics.median(held):.0f} min,"
                f" longest {max(held):.0f} min, each agent's rounds summed",
            )
        )
    running = [agent for agent in snap.agents if agent.status == "running"]
    for agent in running:
        since = started.get(agent.name)
        took = f"{_minutes(snap.clock - since)} min since its dispatch or resume" if since else "minutes unknown"
        size = context.size(agent)
        lines.append(_row("running", f"{agent.name} ({agent.type}): {took}, context {_tokens(size)}"))
    holds, _ = _guarded("holds", lambda: _holders(snap.state, snap.agents))
    waiting, _ = _guarded("waits", lambda: waits(snap.reads, snap.clock, paths=True))
    ci, _ = _guarded("CI", lambda: _ci_minutes(snap.state))
    plan, unticked = _guarded("unticked", lambda: _unticked_lines(snap.reads))
    chain, _ = _guarded("chain", lambda: _chain(snap.reads))
    hop, _ = _guarded("hop", lambda: _hops(snap.reads.entries))
    due, due_count = _guarded("due", lambda: [_row("due", _item(item)) for item in snap.reads.due])
    asked, asked_count = _guarded(
        "asked",
        lambda: [
            _row("asked", f"#{ruling.ask} {ruling.question}")
            for ruling in model.read_rulings(snap.state).values()
            if ruling.open
        ],
    )
    lines += [*holds, *ci, *plan, *chain, *hop, *due, *waiting, *asked]
    finished = sum(len(held) for held in spans.values())
    parts = [
        f"{finished} finished",
        f"{len(running)} running",
        _count(unticked, "unticked"),
        _count(due_count, "due"),
        _count(asked_count, "asked and not ruled"),
    ]
    return lines, "measure: " + ", ".join(parts)


def _rounds(entries: list[log.Entry]) -> tuple[dict[str, float], dict[str, datetime.datetime]]:
    """Each agent's minutes run, its rounds summed, and the start of the round it has open, from the log.

    A round runs from a `dispatch` or `resume` line to the next `report` or `stopped` line naming the agent,
    so the waits between rounds count nothing; a limit's stop ends a round no report ends. A second start
    before that end is a send retried after one that never landed, so the round starts at the later one.
    """
    ran: dict[str, float] = {}
    started: dict[str, datetime.datetime] = {}
    for entry in entries:
        if (at := _parsed(entry.time)) is None:
            continue
        # The lines read `<NAME> as …`, `<NAME> round <n>…` and `<NAME>: …`, and a name holds no space.
        name = entry.text.partition(" ")[0].removesuffix(":")
        if entry.kind in (log.DISPATCH, "resume"):
            started[name] = at
        elif entry.kind in ("report", "stopped") and (since := started.pop(name, None)) is not None:
            ran[name] = ran.get(name, 0.0) + (at - since).total_seconds() / 60
    return ran, started


def _unticked_lines(reads: Reads) -> list[str]:
    """Each unticked line of PLAN.md, a slice's as its id, its count of files and the slices it is after.

    A wide slice's paths ran a line to kilobytes at every reading, of a file the coordinator wrote and
    holds whole; the overlaps its files make are on the `waits` lines.
    """
    landed = reads.landed
    lines: list[str] = []
    for section, ticked, text in reads.plan:
        if ticked:
            continue
        if section != model.SLICES or not text:
            lines.append(_row("unticked", text))
            continue
        name, files, after = model.slice_line(text)
        said = f"{name}, {len(files)} file(s)" + (f", after {', '.join(after)}" if after else "")
        lines.append(_row("unticked", said + (": landed, so done; tick its line" if name in landed else "")))
    return lines


def _chain(reads: Reads) -> list[str]:
    """The longest chain of unfinished slices by their `after:` edges: its depth, and each chain tied for it.

    It is where an estimate's critical path starts. An edge to a slice finished or not listed holds
    nothing back, so it ends the chain; an edge closing a cycle is not followed.
    """
    planned = reads.planned
    finished = reads.finished
    open_ = [name for name, _, _ in planned if name not in finished]
    after = {name: [other for other in named if other in open_] for name, _, named in planned if name in open_}
    if not after:
        return [_row("chain", "no unfinished slice")]
    depth: dict[str, int] = {}

    def deep(name: str, seen: frozenset[str]) -> int:
        if name not in depth:
            behind = [deep(other, seen | {name}) for other in after[name] if other not in seen | {name}]
            depth[name] = 1 + max(behind, default=0)
        return depth[name]

    longest = max(deep(name, frozenset()) for name in open_)

    def chains(name: str) -> Iterable[list[str]]:
        """Each chain of the slice's depth that ends at it, first to last."""
        if depth[name] == 1:
            yield [name]
            return
        for other in after[name]:
            if depth.get(other) == depth[name] - 1:
                for held in chains(other):
                    yield [*held, name]

    # Counted rather than listed, since waves of slices each after the whole wave before tie in a number
    # of chains that grows as a power of their count.
    count: dict[str, int] = {}
    for name in sorted(open_, key=depth.__getitem__):
        behind = [count[other] for other in after[name] if depth.get(other) == depth[name] - 1]
        count[name] = sum(behind) if depth[name] > 1 else 1
    ends = [name for name in open_ if depth[name] == longest]
    found = sum(count[name] for name in ends)
    first = itertools.islice((chain for end in ends for chain in chains(end)), CHAINS_SHOWN)
    shown = "; ".join(", ".join(chain) for chain in first)
    more = f"; and {found - CHAINS_SHOWN} more" if found > CHAINS_SHOWN else ""
    tied = f", {found} chains tied, each" if found > 1 else ","
    return [_row("chain", f"{longest} slice(s) deep{tied} first to last: {shown}{more}")]


def _hops(entries: list[log.Entry]) -> list[str]:
    """The minutes from each `land` line of the log to the next `dispatch` line: what a landing leaves idle."""
    hops: list[float] = []
    landed: list[datetime.datetime] = []
    for entry in entries:
        if (at := _parsed(entry.time)) is None:
            continue
        if entry.kind == "land":
            landed.append(at)
        elif entry.kind == log.DISPATCH:
            hops += [(at - since).total_seconds() / 60 for since in landed]
            landed = []
    if not hops:
        return [_row("hop", "no landing has been followed by a dispatch yet")]
    said = f"median {statistics.median(hops):.0f} min, longest {max(hops):.0f} min"
    return [_row("hop", f"{len(hops)} hop(s) from a landing to the next dispatch, {said}")]


def usage(state: Path, root: Path, clock: datetime.datetime) -> list[str]:
    """The plan's usage limits, one line a row, read by a Claude Code run of its own or from the cached reading.

    A command alone runs it, never a hook: it starts a second Claude Code process.
    """
    path = state / USAGE
    # A cache that cannot be read is read again from Claude Code.
    with contextlib.suppress(Refused, KeyError, ValueError):
        held = store.read_json(path) if path.is_file() else None
        if held is not None and clock - datetime.datetime.fromisoformat(str(held["time"])) < USAGE_FRESH:
            return _usage_lines(held, "cached")
    program, how = _claude(root)
    held = {"time": clock.isoformat(timespec="seconds"), "program": program, "how": how, **_read_usage(program, root)}
    # Another session's status may read but not write this state.
    with contextlib.suppress(Refused):
        store.write_json(path, held)
    return _usage_lines(held, "read")


def _claude(root: Path) -> tuple[str, str]:
    """The Claude Code program, found on PATH as its installers put it, else the configured one; and which."""
    found = shutil.which("claude")
    if found is not None:
        return found, "found on PATH"
    named = config.CONFIG_FILE.as_posix()
    configured = config.load(root).claude_path(root)
    if configured is None:
        raise Refused(f"`claude` is not on PATH, and {named} names no `claude` program: set one, or put it on PATH")
    if not configured.is_file():
        raise Refused(
            f"NOT READ: the `claude` {named} names, {configured}, is missing. A desktop app update moves its"
            f" bundled program to a folder named for the new version: give `claude` the program's new path"
        )
    return str(configured), f"configured in {named}"


def _read_usage(program: str, root: Path) -> Record:
    """`/usage` run headless, which uses no model tokens: its rows, or why it gave none.

    Only `kind`, `percent`, `resets_at`, `severity` and a scoped row's label are kept: the report's
    other fields and the stream around it describe the account.
    """
    try:
        done = process.run((program, *USAGE_RUN), cwd=root, timeout=USAGE_SECONDS)
    except subprocess.TimeoutExpired:
        return {"refused": f"`/usage` gave no answer within {USAGE_SECONDS} s"}
    except OSError as error:
        return {"refused": f"{program} cannot be run: {error.strerror or error}"}
    if done.returncode != 0:
        # Its error text may name the account, so only the code is kept.
        return {"refused": f"claude exited {done.returncode}: run `claude -p /usage` to read why"}
    reports: list[object] = []
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            message: object = json.loads(line)
            if isinstance(message, dict) and "usage_report" in message:
                reports.append(typing.cast("Record", message)["usage_report"])
    if not reports:
        return {"refused": "no usage report: it needs a claude.ai login with a plan, not usage-based billing"}
    limits = context.as_object(context.as_object(reports[-1]).get("rate_limits")).get("limits")
    if not isinstance(limits, list):
        return {"refused": "Claude Code could not get the plan's usage: the request was refused or rate limited"}
    return {"rows": [_usage_row(context.as_object(row)) for row in typing.cast("list[object]", limits)]}


def _usage_row(row: Record) -> Record:
    scope = context.as_object(row.get("scope"))
    labels = [context.as_object(scope.get(part)).get("display_name") for part in ("model", "surface")]
    return {
        "kind": row.get("kind"),
        "label": next((label for label in labels if isinstance(label, str)), None),
        "percent": row.get("percent"),
        "resets_at": row.get("resets_at"),
        "severity": row.get("severity"),
    }


def _usage_lines(held: Record, how: str) -> list[str]:
    stamp = str(held.get("time"))
    said = f"{how} {_clock(stamp)} by {held.get('program')} ({held.get('how')})"
    if "refused" in held:
        return [_row("usage", f"none: {held['refused']}; {said}")]
    rows = typing.cast("list[Record]", held.get("rows") or [])
    lines = [_row("usage", f"{len(rows)} row(s) as the server sent them, {said}")]
    for row in rows:
        label = f" ({row['label']})" if row.get("label") else ""
        try:
            resets = f"resets {datetime.datetime.fromisoformat(str(row['resets_at'])).astimezone():%Y-%m-%d %H:%M}"
        except ValueError:
            resets = "no reset time"
        # The server's own reading of how near the limit is, named where it is not `normal`.
        severity = row.get("severity")
        marked = f", {severity}" if isinstance(severity, str) and severity not in {"", NORMAL} else ""
        lines.append(_row("usage", f"{row.get('kind')}{label}: {row.get('percent')}% used, {resets}{marked}"))
    return lines


def _tokens(size: int | None) -> str:
    return "unknown" if size is None else f"{size:,} tokens"


def _holders(state: Path, agents: list[model.Agent]) -> list[str]:
    """The context of each agent not running that holds an unclosed row: its next rows may cost less in a fresh one."""
    held: dict[str, int] = {}
    for event in model.read_findings(state).values():
        if event.status not in model.CLOSED and event.owner:
            held[event.owner] = held.get(event.owner, 0) + 1
    return [
        _row(
            "holds",
            f"{agent.name} ({agent.type}, {agent.status}): {held[agent.name]} unclosed row(s),"
            f" context {_tokens(context.size(agent))}",
        )
        for agent in agents
        if agent.name in held and agent.status != "running"
    ]


def _for(agent: model.Agent, clock: datetime.datetime) -> str:
    """How long the agent has held its status."""
    since = _stamp(agent, agent.status)
    return f"{agent.status} for {_minutes(clock - since)} min" if since else f"{agent.status}, since unknown"


def waits(reads: Reads, clock: datetime.datetime, *, paths: bool = False) -> list[str]:
    """Each unticked slice no agent took and each queue item whose event has not happened: work that may start now.

    A slice's line names each running agent whose brief owns one of its files, once with how many, and
    each unticked slice its `after:` names, or says its files are free, so a slice planned after another
    without needing its files or its contract reads as free. Only `paths` lists the files themselves:
    `--measure` lists every line, and `land` and `judge` close on the count alone, since a wide slice's
    paths ran a line to kilobytes at every landing.
    """
    agents, heads = reads.agents, reads.heads
    planned = reads.planned
    # A slice landed is done at once, so what is after it reads free before anyone ticks its line.
    done = reads.finished
    listed = {name for name, _, _ in planned}
    named = set(reads.untaken)
    untaken = {name: (files, after) for name, files, after in planned if name in named}
    lines: list[str] = []
    if untaken:
        writing = [
            agent
            for agent in agents
            if agent.status == "running" and agent.type in report.COMMITTING and agent.name in heads
        ]
        for name, (files, after) in untaken.items():
            held = [
                f"it is after {other}, " + ("which is unticked" if other in listed else "which PLAN.md does not list")
                for other in after
                if other not in done
            ]
            for agent in writing:
                hit = [path for path in files if any(brief.overlap(path, owns) for owns in heads[agent.name].owns)]
                if not hit:
                    continue
                count = "1 of its files is" if len(hit) == 1 else f"{len(hit)} of its files are"
                said = f"{count} in the `Owns:` of {agent.name}, {report.article(agent.type)} {_for(agent, clock)}"
                held.append(said + (": " + ", ".join(f"`{path}`" for path in hit) if paths else ""))
            said = "; ".join(held) if held else "its files are free"
            lines.append(_row("waits", f"slice {name}, which no agent took: {said}"))
    due = {item.number for item in reads.due}
    named = {agent.name: agent for agent in agents}
    for number, item in sorted(model.read_queue(reads.state).items()):
        if item.done or number in due:
            continue
        if item.agent is None:
            on = "the next CI reading"
        elif (agent := named.get(item.agent)) is None:
            # `queue add` takes a name with no record only for a slice of the plan.
            on = f"slice {item.agent}, which has not landed"
        elif item.trigger == "landed" and queue.landed_nothing(agent):
            ended = f"`orchestra queue done {number}` ends it"
            on = f"{agent.name}, which settled with nothing landed and never lands: {ended}"
        else:
            on = f"{agent.name}, {_for(agent, clock)}"
        lines.append(_row("waits", f"{_item(item)}: waits on {on}"))
    return lines


def paced(state: Path) -> list[str]:
    """The `waits` lines a verb that moves the fleet closes on: the first `WAITS_SHOWN`, then how many more."""
    clock = datetime.datetime.now().astimezone()
    lines, count = _guarded("waits", lambda: waits(Reads(state, model.agents(state)), clock))
    if count is not None and count > WAITS_SHOWN:
        more = f"{count - WAITS_SHOWN} more: `orchestra status --measure` lists every one"
        lines = [*lines[:WAITS_SHOWN], _row("waits", more)]
    return lines


def _ci_minutes(state: Path) -> list[str]:
    readings = ci.readings(state)
    if not readings:
        return [_row("CI", "no reading yet")]
    return [_row("CI", _reading(reading)) for reading in readings[:CI_SHOWN]]


# --- --end ------------------------------------------------------------------------------------------


def end(snap: Snapshot) -> tuple[list[str], str]:
    checks: list[tuple[str, Callable[[], str | None]]] = [
        ("no unclosed finding", lambda: _unclosed(snap.state)),
        ("nothing running", lambda: _named(agent.name for agent in snap.agents if context.working(agent))),
        # A report waiting, or a round stopped unfinished, leaves no row, no branch and no queue item behind.
        ("every report judged", lambda: _named(f"{a.name} ({a.status})" for a in snap.agents if a.status in UNJUDGED)),
        ("no unlanded branch", lambda: _unlanded(snap)),
        ("CI green at the head", lambda: _green(snap)),
        ("no due queue item", lambda: _named(f"#{item.number}" for item in snap.reads.due)),
        ("each due lens judged or declined", lambda: _lenses(snap)),
        ("no override waiting", lambda: _armed(snap.state)),
        ("no planned slice untaken", lambda: _drifted(snap.reads.untaken)),
        ("no landed slice unticked", lambda: _unticked(snap.reads)),
        ("no frozen tree left", lambda: _frozen(snap.state)),
    ]
    lines: list[str] = []
    held = 0
    for line, check in checks:
        try:
            why = check()
        except Refused as refused:
            why = f"unknown: {refused}"
        ok = why is None or isinstance(why, _Held)
        held += ok
        lines.append(_row("held" if ok else "NOT held", line + ("" if why is None else f": {why}")))
    return lines, f"end: {held} of {len(checks)} lines held"


def _armed(state: Path) -> str | None:
    """The override waiting, which lets its caller's next refused hook call through unseen; None where none waits."""
    held = override.waiting(state)
    if held is None:
        return None
    since = str(held.get("time") or "")[:16].replace("T", " ")
    caller = held.get("for") or "the coordinator"
    return f"for {caller} since {since}: {held.get('reason')}; `orchestra override --drop` removes it"


def _frozen(state: Path) -> str | None:
    """The review's frozen trees the state still holds, each a registered git worktree; None where none is left."""
    folder = state / store.TREES
    # The trash a removal renames a tree into is being deleted already.
    entries = folder.iterdir() if folder.is_dir() else ()
    held = [entry for entry in entries if entry.is_dir() and entry.name != prune.TRASH]
    return f"{len(held)} in {folder}: `orchestra prune --ending` removes each" if held else None


def _drifted(untaken: list[str]) -> str | None:
    """At the end a slice no agent took is drift: its work was planned and never done, or done and never ticked."""
    said = _named(untaken)
    return None if said is None else f"{said}: no agent took it; dispatch it, tick it if you did it, or remove its line"


def _unticked(reads: Reads) -> str | None:
    """Each slice landed whose line is unticked, which the ending asks to be ticked against its landed commit."""
    landed = reads.landed
    said = _named(name for name, ticked in reads.ticks.items() if not ticked and name in landed)
    return None if said is None else f"{said}: its agents landed; tick its line in PLAN.md"


def _named(names: Iterable[str]) -> str | None:
    listed = list(names)
    return ", ".join(listed) if listed else None


def _unclosed(state: Path) -> str | None:
    return _named(row for row, event in model.read_findings(state).items() if event.status not in model.CLOSED)


def _branches(snap: Snapshot, caller: str | None = None) -> dict[str, str | None]:
    """Each agent not settled by its branch, the one `land` and `prune` read, read once a view.

    An agent with no branch recorded is looked for in the harness's files, a search of its own each,
    first in the folder of `caller`, the session a hook's input names.
    """
    held = snap.agents
    return {agent.name: prune.branch_of(agent, caller) for agent in held if agent.status not in model.SETTLED}


def _ahead(snap: Snapshot, branches: dict[str, str | None]) -> dict[str, int | None]:
    """Each agent not landed whose branch holds commits the session branch lacks, and how many.

    A writer or a driver past its brief whose branch neither the record nor the harness names counts as
    None, since its commits cannot be ruled out.
    """
    heads = set(config.git(snap.root, "for-each-ref", "--format=%(refname)", "refs/heads/").splitlines())
    found: dict[str, int | None] = {}
    for agent in snap.agents:
        if agent.name not in branches:
            continue
        branch = branches[agent.name]
        if branch is None:
            if agent.type in report.COMMITTING and agent.status != "briefed":
                found[agent.name] = None
        elif f"refs/heads/{branch}" in heads:
            span = f"refs/heads/{snap.session.branch}..refs/heads/{branch}"
            if count := int(config.git(snap.root, "rev-list", "--count", span).strip() or 0):
                found[agent.name] = count
    return found


def _unlanded(snap: Snapshot) -> str | None:
    branches = _branches(snap)
    ahead = _ahead(snap, branches)
    said = _named(
        f"{name} (branch unknown)" if count is None else f"{name} ({branches[name]}, {count} commits)"
        for name, count in ahead.items()
    )
    unknown = [name for name, count in ahead.items() if count is None]
    return f"{said}; {_discard('NAME')} for each branch unknown" if said and unknown else said


def _discard(name: str) -> str:
    """The verb settling an agent whose branch nothing names, which no landing then can."""
    return f"`orchestra prune --discard {name}` settles it once you have saved what it holds"


def _green(snap: Snapshot) -> str | None:
    if not ci.remote(snap.root):
        return _Held("no remote, so no CI")
    readings = ci.readings(snap.state)
    head = snap.head
    if not readings or readings[0].commit != head:
        return f"the head {head[:9]} has no reading"
    if readings[0].conclusion == "expected":
        return f"{_reading(readings[0])}: an expected red did not clear, though every landing meant to clear it is in"
    if readings[0].conclusion not in GREEN:
        return _reading(readings[0])
    return None


def _lenses(snap: Snapshot) -> str | None:
    """Each lens the session's change makes due or leaves undeclared, held by a judged report or a note declining it.

    A report counts where the agent's brief names the lens on its `Lens:` line, alone or beside others,
    and the agent is of the type the lens's `**Run by.**` line names, since another type cannot answer
    its question. How many lenses one reader takes is the coordinator's judgement of the review's width.
    An undeclared lens is held as a due one is, since nothing else says whether the change touches what
    it reads.
    """
    changed = diff.changed_paths(snap.root, f"{snap.session.base_commit}..{snap.head}")
    read: dict[str, set[str]] = {}
    heads = snap.reads.heads
    for agent in snap.agents:
        # `judge` sets the verdict, which landing and pruning keep: a pruned agent never judged held no lens.
        # A brief that cannot be read is named by the overview's drift.
        if agent.verdict is not None and agent.name in heads:
            for name in heads[agent.name].lens:
                read.setdefault(name, set()).add(agent.type)
    declined = diff.declined(snap.state)
    missing: list[str] = []
    excused: list[str] = []
    for held in diff.due(snap.root, config.load(snap.root), changed):
        lens = held.lens
        if not (held.due or held.undeclared) or lens.run_by in read.get(lens.name, ()):
            continue
        if lens.name in declined:
            excused.append(f"{lens.name} ({declined[lens.name]})")
        elif lens.name in read:
            types = " and ".join(report.article(kind) for kind in sorted(read[lens.name]))
            missing.append(f"{lens.name} (read by {types}; its Run by line names {report.article(lens.run_by)})")
        else:
            missing.append(f"{lens.name} (undeclared)" if held.undeclared and not held.due else lens.name)
    if missing:
        return f"no judged report and no `Lens declined: <name>, <reason>` note for {', '.join(missing)}"
    return _Held(f"declined {'; '.join(excused)}") if excused else None
