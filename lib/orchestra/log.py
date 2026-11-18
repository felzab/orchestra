"""`note` and `log`: the session's event log, one stamped line per event.

Each verb that dispatches, judges, lands, pushes or reads CI writes its own line through `record`;
`note` adds the coordinator's. The clock is read here, never typed, so no line carries a guessed time.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from orchestra import DONE, Refused, cli, model, store

# The kind of a note naming what the coordinator waits on, which the turn's-end check reads.
WAITING = "waiting"
# The kinds of a judge and a dispatch, which the turn's-end check counts to find the checkpoint due.
JUDGE, DISPATCH = "judge", "dispatch"


@dataclass(frozen=True)
class Entry:
    time: str
    kind: str
    text: str


def record(state: Path, kind: str, text: str) -> Entry:
    entry = Entry(model.now(), kind, text)
    store.append_jsonl(state / store.LOG, model.to_record(entry))
    return entry


def entries(state: Path) -> list[Entry]:
    path = state / store.LOG
    return [model.from_record(Entry, held, path) for held in store.read_jsonl(path)]


def line(entry: Entry) -> str:
    """An entry as one printed line, its time to the minute."""
    return f"{entry.time[:16].replace('T', ' ')}  {entry.kind:<8}  {entry.text}"


def add(verb: str, parser: argparse.ArgumentParser) -> None:
    if verb == "note":
        parser.add_argument("--waiting", action="store_true", help="the note names what the session waits on")
        parser.add_argument("text", nargs="+", help="the note's words, or - to read them from stdin")


def run(args: argparse.Namespace) -> int:
    state = store.state_dir()
    if args.verb == "log":
        held = entries(state)
        cli.emit(args, [line(entry) for entry in held], f"{len(held)} entries in {state / store.LOG}", tail=True)
        return DONE
    text = cli.one_line(cli.given(args.text))
    if not text:
        raise Refused("a note needs words: give them as arguments, or `-` and the text on stdin")
    entry = record(state, WAITING if args.waiting else "note", text)
    cli.emit(args, [], line(entry))
    return DONE
