"""`queue`: something to do once CI concludes, an agent lands or an agent reports, and when it is due.

An item is a line of `queue.jsonl` and is never rewritten: `done` appends the item's state with
`done` set, and logs it. `due` is what the status view and the turn's-end check read, so an item
waiting on an event is named the moment the event has happened.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import functools
import typing
from pathlib import Path

from orchestra import DONE, Refused, brief, cli, log, model, report, store

# The log kind the `ci` verb writes each time it reads a concluded run, and only then.
CI_READ = "ci"
# The log kind of a queue item's end.
KIND = "queue"
# The report statuses of an agent whose work is done, which `land` may find already on the session branch.
DELIVERED: frozenset[str] = frozenset(report.STATUSES) - report.STOPPED


def landed_nothing(agent: model.Agent) -> bool:
    """Whether the agent is settled with none of its work landed, so that its slice is not done by it.

    `prune --discard` threw its work away. Otherwise a `pruned` record landed where `land` stamped it
    `landed`, a stamp later statuses keep, as `land` does too where the session branch already holds a
    branch whose report says its work is done. A writer pruned before its first commit, after a report
    stopped on a question, or with its branch lost before it landed, has no stamp.
    """
    return agent.status == "pruned" and (agent.reason == model.DISCARDED or "landed" not in agent.since)


def _when(value: str) -> tuple[model.Trigger, str | None]:
    """`ci`, `landed:NAME` or `reported:NAME` as the item's trigger and agent."""
    trigger, _, name = value.partition(":")
    if trigger == "ci" and not name:
        return "ci", None
    if trigger in {"landed", "reported"} and name:
        return typing.cast("model.Trigger", trigger), name
    raise argparse.ArgumentTypeError(f"`{value}` is none of ci, landed:NAME and reported:NAME")


def add(_verb: str, parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", metavar="<action>")
    adding = actions.add_parser("add", help="queue an item for an event", description="queue an item for an event")
    adding.add_argument(
        "--when",
        required=True,
        type=_when,
        metavar="ci|landed:NAME|reported:NAME",
        help="the event it waits on; NAME is an agent's, or for landed:NAME a slice's that PLAN.md lists",
    )
    adding.add_argument("text", nargs="+", help="what to do, or - to read it from stdin")
    ending = actions.add_parser("done", help="mark an item done", description="mark an item done")
    ending.add_argument("number", type=int, help="the item's number, as `orchestra queue` lists it")


def _seconds(stamp: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(stamp)


def due(
    state: Path, agents: list[model.Agent] | None = None, entries: list[log.Entry] | None = None
) -> list[model.QueueItem]:
    """Every item not done whose event has happened, by number.

    `agents` and `entries` are the records and the log where the caller already holds them, as the turn-end hook
    does, so they are not read twice; each is read here where not given.
    """
    waiting = [item for _, item in sorted(model.read_queue(state).items()) if not item.done]
    if not waiting:
        return []
    held = model.agents(state) if agents is None else agents
    by_name = {agent.name: agent for agent in held}
    reads = [
        _seconds(entry.time) for entry in (log.entries(state) if entries is None else entries) if entry.kind == CI_READ
    ]
    # Read once a call, and only for an item naming a slice, since it reads every agent's brief.
    landed = functools.cache(lambda: landed_slices(state, held))

    def happened(item: model.QueueItem) -> bool:
        if item.trigger == "ci":
            return any(read > _seconds(item.time) for read in reads)
        agent = by_name.get(item.agent or "")
        if item.trigger == "landed":
            if agent is None:
                # A slice `run` accepted, whose agents may carry any name: due once the status view reads it landed.
                return item.agent in landed()
            # One settled with nothing landed never lands, so what waits on its landing never falls due.
            return agent.status in model.SETTLED and not landed_nothing(agent)
        if agent is None:
            return False
        # Due from the first report at or after it was queued: each report stamps the record anew, so an
        # item queued after one, for the round a resume starts, waits for that round's report.
        since = agent.since.get("reported")
        return since is not None and _seconds(since) >= _seconds(item.time)

    return [item for item in waiting if happened(item)]


def on_slice(state: Path, agents: list[model.Agent]) -> dict[str, list[model.Agent]]:
    """The agents on each slice their briefs name, one settled with nothing landed left out: it took nothing.

    A brief that cannot be read takes nothing; the status view's drift names it.
    """
    held: dict[str, list[model.Agent]] = {}
    for agent in agents:
        if landed_nothing(agent):
            continue
        with contextlib.suppress(Refused):
            held.setdefault(brief.header(state, agent.name).slice, []).append(agent)
    return held


def landed_slices(state: Path, agents: list[model.Agent]) -> set[str]:
    """Each slice an agent on it has landed, with no agent on it still unsettled: done, whether ticked or not.

    `land` writes no tick, so a slice reads as done the moment its last agent lands, not once the
    coordinator ticks its line.
    """
    return {name for name, held in on_slice(state, agents).items() if all(a.status in model.SETTLED for a in held)}


def _shown(item: model.QueueItem, held: set[int]) -> str:
    when = item.trigger if item.agent is None else f"{item.trigger}:{item.agent}"
    return f"{item.number:>3}  {'DUE' if item.number in held else 'waits':<5}  {when:<24}  {item.text}"


def run(args: argparse.Namespace) -> int:
    state = store.state_dir()
    if args.action == "add":
        trigger, agent = args.when
        # A slice PLAN.md lists may be briefed later, under any name: `due` reads a landing of the slice itself.
        # A report is an agent's alone, so `reported:` takes an agent's name.
        if agent is not None and not model.agent_path(state, agent).exists():
            if trigger != "landed":
                raise Refused(
                    f"no agent {agent} in this session: `reported:` names an agent, as `orchestra status` lists"
                )
            if agent not in model.slices(model.plan_lines(state)):
                raise Refused(
                    f"no agent {agent} in this session and no slice of {store.PLAN}: `orchestra status` lists both"
                )
        text = cli.one_line(cli.given(args.text))
        if not text:
            raise Refused("an item needs words: give them as arguments, or `-` and the text on stdin")

        def append(items: dict[int, model.QueueItem]) -> list[model.QueueItem]:
            return [model.QueueItem(max(items, default=0) + 1, trigger, text, agent)]

        (made,) = model.write_queue(state, append)
        cli.emit(args, [], _shown(made, {item.number for item in due(state)}))
        return DONE
    if args.action == "done":
        return _done(args, state)
    waiting = [item for _, item in sorted(model.read_queue(state).items()) if not item.done]
    held = {item.number for item in due(state)}
    lines = [_shown(item, held) for item in waiting]
    cli.emit(args, lines, f"{len(waiting)} item(s) waiting, {len(held)} due: `orchestra queue done N` ends one")
    return DONE


def _done(args: argparse.Namespace, state: Path) -> int:
    number: int = args.number

    def end(items: dict[int, model.QueueItem]) -> list[model.QueueItem]:
        item = items.get(number)
        if item is None:
            raise Refused(f"no queue item {number}: `orchestra queue` lists them")
        if item.done:
            raise Refused(f"queue item {number} is done already")
        return [dataclasses.replace(item, done=True)]

    (ended,) = model.write_queue(state, end)
    entry = log.record(state, KIND, f"done {number}: {ended.text}")
    cli.emit(args, [], log.line(entry))
    return DONE
