"""`judge`, `show`, `route`, `close` and `findings`: every finding of a report given one owner or a closing.

A row is `<NAME>-F<n>`, finding n of `reports/<NAME>.md`, or `<NAME>.<r>-F<n>` for round r's report
from the second round. `judge` banks every finding of a report with its disposition in one append, so
none is left without an owner; a verifier's verdicts move rows already banked. A row has one owner at a
time, an agent with a record or a queue, and a row stays with an agent until that agent reports, so two
agents never build opposite fixes for one finding.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import re
import typing
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orchestra import DONE, REMAINS, Refused, cli, config, context, log, model, report, store
from orchestra.model import FindingEvent, FindingStatus

# The statuses of an agent that has not reported; `live` says which of them may still be working.
LIVE: frozenset[str] = frozenset({"briefed", "running"})
JUDGED: frozenset[str] = frozenset({"judged", "landed", "pruned"})
CLOSING: tuple[FindingStatus, ...] = tuple(status for status in model.FINDING_STATUSES if status in model.CLOSED)
KEY_RE = re.compile(r"F[1-9][0-9]*")
FORM = "`route:<owner>` or `close:<STATUS>:<evidence>`, STATUS one of " + ", ".join(CLOSING)


@dataclass(frozen=True)
class Route:
    owner: str


@dataclass(frozen=True)
class Close:
    status: FindingStatus
    evidence: str


Disposition = Route | Close


def disposition(value: str) -> Disposition:
    """`route:<owner>` or `close:<STATUS>:<evidence>` read; refused in any other shape."""
    head, _, rest = value.partition(":")
    if head == "route" and rest.strip():
        return Route(rest.strip())
    if head == "close":
        for status in CLOSING:
            if rest.startswith(f"{status}:") and (evidence := cli.one_line(rest[len(status) + 1 :])):
                return Close(status, evidence)
    raise Refused(f"`{value}` is no disposition: {FORM}")


def owners(state: Path, root: Path) -> Callable[[str], str]:
    """A check that a name may own a row: an agent with a record, or a queue the plugin or the project names."""
    queues = (*model.QUEUES, *config.load(root).queues)

    def owner(name: str) -> str:
        if name in queues or model.has_agent(state, name):
            return name
        raise Refused(f"{name} is no owner: no agent of that name has a record, and the queues are {', '.join(queues)}")

    return owner


def unjudged(agent: model.Agent) -> str | None:
    """Why the agent's latest report is judged before anything moves the agent on; None where none waits.

    `judge` reads the latest round alone, and refuses an agent landed or pruned, so a report passed over
    is never banked. A report stopped on a question waits on its answer instead, which resumes the agent.
    """
    if agent.status != "reported" or agent.outcome in report.STOPPED:
        return None
    return (
        f"{agent.name}'s round {max(agent.reports, default=1)} report is not judged:"
        f" `orchestra judge {agent.name}` banks its findings first"
    )


def worktrees(root: Path) -> dict[str, Path]:
    """Each agent's worktree git lists that still stands, by its branch; the primary checkout left out."""
    listed = config.worktrees(root)[1:]
    return {
        tree["branch"]: Path(tree["worktree"])
        for tree in listed
        if "branch" in tree and Path(tree["worktree"]).is_dir()
    }


def gone(agent: model.Agent, root: Path) -> str | None:
    """The branch of a writer or a driver whose worktree is gone, so that no resume reaches it; None otherwise.

    Claude Code removes a subagent's worktree when it finishes with no change (worktrees.md), so a writer
    that committed nothing, and any driver, has no worktree left once it stops, and none to resume in.
    """
    if agent.type not in report.COMMITTING:
        return None
    if agent.branch is None:
        return "no branch recorded"
    return None if agent.branch in worktrees(root) else agent.branch


def unreached(agent: model.Agent, root: Path) -> str | None:
    """Why the report of an agent stopped on a question is judged as it stands, no answer reaching it; else None."""
    if agent.outcome not in report.STOPPED or (branch := gone(agent, root)) is None:
        return None
    return (
        f"{agent.name} stopped {agent.outcome}, and its worktree is gone ({branch}), so no answer reaches it:"
        f" `orchestra judge {agent.name}` banks its report as it stands"
    )


def waiting(state: Path, agent: model.Agent) -> str | None:
    """Why an agent no longer working stays as it is: its report waits to be judged, or its question answered.

    A `stopped` agent's report, written before a limit, a takeover or a refused stop ended it, is judged
    too where it reads and asks nothing, as is a `running` one's whose stop hook never ran; one that does
    not read holds nothing back, so it can be settled. A record `is_live` holds live cannot be judged, so
    its report holds nothing back either: only a discard settles it. A report stopped on a question that
    no answer can reach, its worktree gone, is judged as it stands.
    """
    if agent.status == "stopped" or (agent.status == "running" and not is_live(agent)):
        round_ = max(agent.reports, default=1)
        held = _latest(state, agent)
        if held is None or held.status in report.STOPPED:
            return None
        why = f" ({agent.reason})" if agent.reason else ""
        return (
            f"{agent.name} is {agent.status}{why}, and its round {round_} report reads:"
            f" `orchestra judge {agent.name}` banks its findings first"
        )
    if agent.status != "reported":
        return None
    return (
        unjudged(agent)
        or unreached(agent, config.checkout().root)
        or f"{agent.name} stopped {agent.outcome}: answer it by message, and judge the report it writes"
    )


def _latest(state: Path, agent: model.Agent) -> report.Report | None:
    """The agent's latest round's report as `judge` reads it; None where it does not read."""
    try:
        return report.read(report.path(state, agent.name, max(agent.reports, default=1)), agent.type)
    except Refused:
        return None


def readable(state: Path, agent: model.Agent) -> bool:
    """Whether `judge` can read the agent's latest report, one stopped on a question included."""
    return _latest(state, agent) is not None


def is_live(agent: model.Agent, session: str | None = None) -> bool:
    """Whether the agent may still be writing its report or working on the rows it holds.

    A `running` record outlives an agent whose session died or whose stop hook never ran, and its
    transcript then shows the turn ended and falls silent, which `context.ended` judges; a transcript
    that cannot be read shows nothing, so the record is believed, as `prune` believes it, rather than
    hand a live agent's rows to a second owner. A hook passes its input's `session`, as `context.ended` takes it.
    """
    return agent.status == "briefed" or (agent.status == "running" and context.gone(agent, session) is None)


def live(state: Path, session: str | None = None) -> dict[str, str]:
    """Each agent `is_live` holds live, by name, with its status: one transcript read for each running agent."""
    return {agent.name: agent.status for agent in model.agents(state) if is_live(agent, session)}


def _route(
    row: str, held: FindingEvent | None, owner: str, live: dict[str, str], evidence: str | None = None
) -> FindingEvent | str | None:
    """The event moving the row to the owner; a refusal; None where the owner holds it already."""
    if held is None:
        return f"{row} is no row: `orchestra judge` banks a report's findings"
    if held.status in model.CLOSED:
        # Reopening keeps the close in the file, so the earlier evidence is never lost.
        return FindingEvent(row, "reopen", "ROUTED", owner, evidence)
    if held.status == "ROUTED" and held.owner == owner:
        # A verifier's correction is evidence the row must carry, even where its owner stays.
        return (
            None
            if evidence is None or evidence == held.evidence
            else FindingEvent(row, "route", "ROUTED", owner, evidence)
        )
    if held.status == "ROUTED" and held.owner in live:
        return f"{row} is routed to {held.owner}, which is {live[held.owner]}: its rows stay with it until it reports"
    return FindingEvent(row, "route", "ROUTED", owner, evidence)


def _close(row: str, held: FindingEvent | None, status: FindingStatus, evidence: str) -> FindingEvent | str:
    if held is None:
        return f"{row} is no row: `orchestra judge` banks a report's findings"
    if held.status in model.CLOSED:
        return f"{row} is {held.status} already ({held.evidence}): route it to reopen it"
    return FindingEvent(row, "close", status, held.owner, evidence)


def _apply(
    state: Path, plan: Callable[[dict[str, FindingEvent]], list[FindingEvent | str | None]]
) -> list[FindingEvent]:
    """The events `plan` makes from every row's state, appended in one write; refused, writing none, at a refusal."""

    def edit(rows: dict[str, FindingEvent]) -> list[FindingEvent]:
        made = plan(rows)
        if refused := [said for said in made if isinstance(said, str)]:
            raise Refused("; ".join(refused))
        return [event for event in made if isinstance(event, FindingEvent)]

    return model.write_findings(state, edit)


def route_rows(state: Path, owner: str, rows: list[str], alive: dict[str, str] | None = None) -> list[FindingEvent]:
    """Each row routed to the owner, all or none; the events written. The caller checks the owner with `owners`.

    `alive` is `live`'s answer where the caller routes several times in one go, so the transcripts are read once.
    """
    for row in rows:
        report.located(row)
    agents = live(state) if alive is None else alive

    def moved(row: str, held: FindingEvent | None) -> FindingEvent | str | None:
        # A row's state is its last line, so a route of a routed row carries its evidence, a correction included.
        kept = held.evidence if held is not None and held.status == "ROUTED" else None
        return _route(row, held, owner, agents, kept)

    return _apply(state, lambda now: [moved(row, now.get(row)) for row in dict.fromkeys(rows)])


def close_rows(state: Path, status: FindingStatus, evidence: str, rows: list[str]) -> list[FindingEvent]:
    """Each row closed with the status and its evidence, all or none; the events written."""
    if not (evidence := cli.one_line(evidence)):
        raise Refused("a row closes on its evidence, and the evidence is empty")
    for row in rows:
        report.located(row)
    return _apply(state, lambda now: [_close(row, now.get(row), status, evidence) for row in dict.fromkeys(rows)])


def _tail(event: FindingEvent) -> str:
    """What a printed row carries after its status: its owner where it is routed, and the evidence."""
    owner = f" to {event.owner}" if event.status == "ROUTED" else ""
    return owner + (f" ({event.evidence})" if event.evidence else "")


def said(event: FindingEvent) -> str:
    return f"{'REOPENED' if event.kind == 'reopen' else event.status} {event.row}{_tail(event)}"


# --- reading findings back from their reports ---------------------------------------------------------


class Reports:
    """Each report read once in a call, by the stem its rows open with."""

    def __init__(self, state: Path) -> None:
        self.state = state
        self.held: dict[str, report.Report | Refused] = {}

    def of(self, name: str, round_: int) -> report.Report:
        key = report.stem(name, round_)
        if key not in self.held:
            try:
                self.held[key] = report.read(report.path(self.state, name, round_))
            except Refused as refused:
                self.held[key] = refused
        found = self.held[key]
        if isinstance(found, Refused):
            raise found
        return found

    def finding(self, row: str) -> tuple[report.Finding, Path]:
        name, round_, number = report.located(row)
        found = self.of(name, round_)
        path = report.path(self.state, name, round_)
        if found.type == "verifier":
            raise Refused(f"{row}: {path} holds verdicts, and a verdict is shown with the row it judges")
        for finding in found.findings:
            if finding.number == number:
                return finding, path
        raise Refused(f"{row}: {path} holds no F{number}")


def _anchor(text: str) -> str:
    """An anchor as compared: no backticks, forward slashes and single spaces."""
    return " ".join(text.replace("`", "").replace("\\", "/").split())


def repeats(new: dict[str, report.Finding], rows: dict[str, FindingEvent], reports: Reports) -> list[str]:
    """Each new finding at the anchor of an unclosed row of another report: one defect several agents reported."""
    stems = {row.rsplit("-F", 1)[0] for row in new}
    said: list[str] = []
    for row, event in rows.items():
        if event.status in model.CLOSED or row in new or row.rsplit("-F", 1)[0] in stems:
            continue
        try:
            earlier, _ = reports.finding(row)
        except Refused:
            continue
        for named, finding in new.items():
            if (anchor := _anchor(finding.anchor)) == _anchor(earlier.anchor):
                owner = f" to {event.owner}" if event.owner else ""
                said.append(f"{named} may repeat {row} ({event.status}{owner}): both at `{anchor}`")
    return said


# --- the verbs ----------------------------------------------------------------------------------------


def add(verb: str, parser: argparse.ArgumentParser) -> None:
    if verb == "judge":
        parser.add_argument("name", help="the agent whose report is judged")
        parser.add_argument(
            "dispositions",
            nargs="*",
            metavar="F<n>=DISPOSITION",
            help=f"one for each finding, {FORM}; a verifier's report names each row it judged, <NAME>-F<n>=...",
        )
        parser.add_argument("--verdict", required=True, help="one line on the report, kept in the agent's record")
    elif verb == "show":
        parser.add_argument("rows", nargs="+", metavar="ROW", help="<NAME>-F<n>, or <NAME>.<round>-F<n>")
    elif verb == "route":
        parser.add_argument("owner", help="an agent with a record, or a queue")
        parser.add_argument("rows", nargs="+", metavar="ROW")
    elif verb == "close":
        parser.add_argument("status", choices=CLOSING, help="quoted where it is several words")
        parser.add_argument("evidence", help="what shows the row is closed: a commit, a ruling, a command and its exit")
        parser.add_argument("rows", nargs="+", metavar="ROW")


def run(args: argparse.Namespace) -> int:
    here = config.checkout()
    state = store.state_dir(held=here)
    if args.verb in {"judge", "route"}:
        return (judge if args.verb == "judge" else route)(args, state, owners(state, here.root))
    verbs: dict[str, Callable[[argparse.Namespace, Path], int]] = {"show": show, "close": close, "findings": unclosed}
    return verbs[args.verb](args, state)


def _given(values: list[str], *, verifier: bool) -> dict[str, Disposition]:
    """Each `KEY=DISPOSITION` by its key; refused where a key is malformed or named twice."""
    given: dict[str, Disposition] = {}
    for value in values:
        key, sep, rest = value.partition("=")
        valid = model.ROW_RE.fullmatch(key) if verifier else KEY_RE.fullmatch(key)
        if not sep or not valid:
            shape = "<NAME>-F<n>=" if verifier else "F<n>="
            raise Refused(f"`{value}` is no {shape}<disposition>: {FORM}")
        if key in given:
            raise Refused(f"{key} is given two dispositions: one finding, one disposition")
        given[key] = disposition(rest)
    return given


def judge(args: argparse.Namespace, state: Path, owner: Callable[[str], str]) -> int:
    agent = model.read_agent(state, args.name)
    if is_live(agent):
        raise Refused(f"{agent.name} is {agent.status}, so its report may be unfinished: judge it once it reports")
    if agent.status in JUDGED:
        raise Refused(f"{agent.name}'s report is judged already ({agent.status}): `orchestra findings` lists its rows")
    # A takeover leaves a live agent `stopped`, and it fills its report as it goes.
    if why := context.working(agent):
        raise Refused(f"{agent.name} may still be working ({why}): judge it once its turn ends")
    verdict = cli.one_line(args.verdict)
    if not verdict:
        raise Refused("--verdict takes one line on the report")
    round_ = max(agent.reports, default=1)
    held = report.read(report.path(state, agent.name, round_), agent.type)
    # A question no answer can reach, its agent's worktree gone, leaves the report as it stands to be judged.
    if held.status in report.STOPPED and gone(agent, config.checkout().root) is None:
        raise Refused(
            f"{agent.name} stopped {held.status}: {held.reason}. Answer it by message, and judge the report it writes"
        )
    verifier = agent.type == "verifier"
    given = _given(args.dispositions, verifier=verifier)
    for chosen in given.values():
        if isinstance(chosen, Route):
            owner(chosen.owner)
    alive = live(state)
    reports = Reports(state)
    new = {report.row(agent.name, round_, finding.number): finding for finding in held.findings}
    if verifier:
        events = _apply(state, lambda now: _verdicts(agent.name, held.verdicts, given, now, alive))
    else:
        events = _apply(state, lambda now: _bank(agent.name, held.findings, round_, given, now))
    model.update_agent(state, agent.name, lambda before: dataclasses.replace(before, status="judged", verdict=verdict))
    routed = sum(event.status == "ROUTED" for event in events)
    closed = sum(event.status in model.CLOSED for event in events)
    log.record(state, log.JUDGE, f"{report.stem(agent.name, round_)}: {routed} routed, {closed} closed; {verdict}")
    lines = [said(event) for event in events if event.kind != "bank"]
    rows = model.read_findings(state)
    # No write removes a row, so a verdict's row missing now was missing when the verdicts were applied.
    lines += [f"{v.row} is no banked row, so its verdict moves nothing" for v in held.verdicts if v.row not in rows]
    if new and not events:
        lines.append(f"its {len(new)} row(s) were banked by an earlier judge, and stand as it left them")
    lines += repeats(new, rows, reports)
    # Imported here, since the hooks import this module and none of them reads the status view.
    paced = typing.cast("Callable[[Path], list[str]]", importlib.import_module("orchestra.status").paced)
    lines += paced(state)
    cli.emit(args, lines, f"judged {report.stem(agent.name, round_)}: {routed} routed, {closed} closed")
    return DONE


def _bank(
    name: str,
    found: tuple[report.Finding, ...],
    round_: int,
    given: dict[str, Disposition],
    now: dict[str, FindingEvent],
) -> list[FindingEvent | str | None]:
    """A finding report's rows banked, each with its disposition; a refusal where one has none or is banked."""
    keys = [f"F{finding.number}" for finding in found]
    if unknown := [key for key in given if key not in keys]:
        return [
            f"{', '.join(unknown)} names no finding of {name}'s report, whose findings are {', '.join(keys) or 'none'}"
        ]
    if missing := [key for key in keys if key not in given]:
        return [f"every finding takes a disposition, and {', '.join(missing)} has none: F<n>={FORM}"]
    rows = [report.row(name, round_, finding.number) for finding in found]
    banked = [row for row in rows if row in now]
    if rows and banked == rows:
        # One append banks a report's rows, so all of them banked means a judge that stopped before
        # it marked the agent judged: this one completes it, adding nothing.
        return []
    if banked:
        return [f"{', '.join(banked)} is banked already: a report is judged once"]
    made: list[FindingEvent | str | None] = []
    for key, row in zip(keys, rows, strict=True):
        made.append(FindingEvent(row, "bank", "OPEN"))
        match given[key]:
            case Route(owner):
                made.append(FindingEvent(row, "route", "ROUTED", owner))
            case Close(status, evidence):
                made.append(FindingEvent(row, "close", status, evidence=evidence))
    return made


def _verdicts(
    name: str,
    verdicts: tuple[report.Verdict, ...],
    given: dict[str, Disposition],
    now: dict[str, FindingEvent],
    live: dict[str, str],
) -> list[FindingEvent | str | None]:
    """A verifier's rows moved: a REFUTED one closed by itself, a CORRECTED one routed with its correction.

    A verdict on a row never banked moves nothing and takes no disposition.
    """
    rows = [verdict.row for verdict in verdicts]
    if unknown := [row for row in given if row not in rows]:
        return [
            f"{', '.join(unknown)} has no verdict in {name}'s report, whose verdicts are {', '.join(rows) or 'none'}"
        ]
    if moot := [row for row in given if row not in now]:
        return [f"{', '.join(moot)} is no banked row, so its verdict moves nothing: give it no disposition"]
    banked = [verdict for verdict in verdicts if verdict.row in now]
    if missing := [verdict.row for verdict in banked if verdict.verdict != "REFUTED" and verdict.row not in given]:
        return [f"every verdict but REFUTED takes a disposition, and {', '.join(missing)} has none: <ROW>={FORM}"]
    made: list[FindingEvent | str | None] = []
    for verdict in banked:
        held = now.get(verdict.row)
        # A refutation names its proof, so the row closes on the verifier's evidence unless the judge says otherwise.
        refuted = Close("NOT A DEFECT", f"refuted by {name}: {cli.one_line(verdict.first)}")
        match given.get(verdict.row, refuted):
            case Route(owner):
                # A row's state is its last line, so a route of a routed row carries its evidence, an earlier
                # verifier's correction included, unless this verifier corrects it again.
                kept = held.evidence if held is not None and held.status == "ROUTED" else None
                corrected = f"corrected by {name}: {verdict.first}" if verdict.verdict == "CORRECTED" else kept
                made.append(_route(verdict.row, held, owner, live, corrected))
            case Close(status, evidence):
                # A row closed so already was closed by a judge of this report that stopped before it marked
                # the agent judged, so this one completes it; a route to the same owner is likewise nothing.
                same = held is not None and (held.status, held.evidence) == (status, evidence)
                made.append(None if same else _close(verdict.row, held, status, evidence))
    return made


def show(args: argparse.Namespace, state: Path) -> int:
    rows = model.read_findings(state)
    reports = Reports(state)
    lines: list[str] = []
    refused: list[str] = []
    for row in args.rows:
        try:
            finding, path = reports.finding(row)
        except Refused as error:
            refused.append(str(error))
            continue
        event = rows.get(row)
        where = "not judged" if event is None else f"{event.status}{_tail(event)}"
        lines += [f"{row} · {where} · {path}", f"### {finding.heading}", *finding.body.split("\n"), ""]
    if refused:
        cli.emit(args, lines, f"{len(args.rows) - len(refused)} of {len(args.rows)} finding(s) shown")
        raise Refused("; ".join(refused))
    cli.emit(args, lines, f"{len(args.rows)} finding(s) shown")
    return DONE


def route(args: argparse.Namespace, state: Path, owner: Callable[[str], str]) -> int:
    events = route_rows(state, owner(args.owner), args.rows)
    cli.emit(args, [said(event) for event in events], f"{len(events)} row(s) moved to {args.owner}")
    return DONE


def close(args: argparse.Namespace, state: Path) -> int:
    events = close_rows(state, args.status, args.evidence, args.rows)
    cli.emit(args, [said(event) for event in events], f"{len(events)} row(s) closed {args.status}")
    return DONE


def unclosed(args: argparse.Namespace, state: Path) -> int:
    rows = {row: event for row, event in model.read_findings(state).items() if event.status not in model.CLOSED}
    if not rows:
        cli.emit(args, [], "every row is closed")
        return DONE
    reports = Reports(state)
    by: dict[str, list[str]] = {}
    for row, event in rows.items():
        try:
            heading = reports.finding(row)[0].heading.split(report.SEP, 1)[1]
        except Refused:
            heading = "(its report cannot be read: `orchestra show` names why)"
        note = f" ({event.evidence})" if event.evidence else ""
        by.setdefault(event.owner or "nobody (OPEN)", []).append(f"  {row} · {heading}{note}")
    lines = [line for owner, held in by.items() for line in (f"{owner}: {len(held)}", *held)]
    opened = sum(event.status == "OPEN" for event in rows.values())
    cli.emit(args, lines, f"{len(rows)} unclosed row(s): {opened} OPEN, {len(rows) - opened} ROUTED")
    return REMAINS
