"""`queue`: something to do once CI concludes, an agent lands or an agent reports, and when it is due.

An item is a line of `queue.jsonl` and is never rewritten: `done` appends the item's state with
`done` set, and logs it. `due` is what the status view and the turn's-end check read, so an item
waiting on an event is named the moment the event has happened.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import importlib
import typing
from collections.abc import Callable
from pathlib import Path

from orchestra import DONE, Refused, cli, log, model, report, store

# The log kind the `ci` verb writes each time it reads a concluded run, and only then.
CI_READ = "ci"
# The log kind of a queue item's end.
KIND = "queue"
# The report statuses of an agent whose work is done, which `land` may find already on the session branch.
DELIVERED: frozenset[str] = frozenset(report.STATUSES) - report.STOPPED


def landed_nothing(agent: model.Agent) -> bool:
    """Whether the agent is settled with none of its work landed, so that its slice is not done by it.

    `prune --discard` threw its work away. Otherwise a `pruned` record landed where `land` stamped it
    `landed`, a stamp later statuses keep, or where its report says its work is done: `land` prunes such
    a writer with no stamp when the session branch already holds its branch. A writer pruned before its
    first commit, or after a report stopped on a question, has neither.
    """
    return agent.status == "pruned" and (
        agent.reason == model.DISCARDED or ("landed" not in agent.since and agent.outcome not in DELIVERED)
    )


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
        help="the event it waits on; NAME is an agent's, or a slice's that PLAN.md lists and no agent took yet",
    )
    adding.add_argument("text", nargs="+", help="what to do, or - to read it from stdin")
    ending = actions.add_parser("done", help="mark an item done", description="mark an item done")
    ending.add_argument("number", type=int, help="the item's number, as `orchestra queue` lists it")


def _seconds(stamp: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(stamp)


def due(state: Path) -> list[model.QueueItem]:
    """Every item not done whose event has happened, by number."""
    waiting = [item for _, item in sorted(model.read_queue(state).items()) if not item.done]
    if not waiting:
        return []
    agents = {agent.name: agent for agent in model.agents(state)}
    reads = [_seconds(entry.time) for entry in log.entries(state) if entry.kind == CI_READ]

    def happened(item: model.QueueItem) -> bool:
        if item.trigger == "ci":
            return any(read > _seconds(item.time) for read in reads)
        agent = agents.get(item.agent or "")
        if agent is None:
            return False
        if item.trigger == "landed":
            # One settled with nothing landed never lands, so what waits on its landing never falls due.
            return agent.status in model.SETTLED and not landed_nothing(agent)
        # Due from the first report at or after it was queued: each report stamps the record anew, so an
        # item queued after one, for the round a resume starts, waits for that round's report.
        since = agent.since.get("reported")
        return since is not None and _seconds(since) >= _seconds(item.time)

    return [item for item in waiting if happened(item)]


def _planned(state: Path) -> set[str]:
    """Each slice PLAN.md lists, as `status` reads the plan; none where there is no plan to read."""
    # Imported here, as `brief` does: the status view imports this module.
    slices = typing.cast("Callable[[Path], dict[str, bool]]", importlib.import_module("orchestra.status").slices)
    try:
        return set(slices(state))
    except Refused:
        return set()


def _shown(item: model.QueueItem, held: set[int]) -> str:
    when = item.trigger if item.agent is None else f"{item.trigger}:{item.agent}"
    return f"{item.number:>3}  {'DUE' if item.number in held else 'waits':<5}  {when:<24}  {item.text}"


def run(args: argparse.Namespace) -> int:
    state = store.state_dir()
    if args.action == "add":
        trigger, agent = args.when
        # A slice PLAN.md lists may be briefed later, under its own name; `due` waits for a name with no record.
        if agent is not None and not model.agent_path(state, agent).exists() and agent not in _planned(state):
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
