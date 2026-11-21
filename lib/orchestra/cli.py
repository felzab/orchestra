"""The command line: every verb, mapped by name to the module that implements it.

A verb's module is imported only when that verb runs, and exposes the `Verbs` contract below. A verb
exits 0 when done, 1 when done with something remaining and 2 when refused, `land` adding codes of
its own that its `--help` lists, and 70 on a fault, whose trace it prints; it prints at most forty
lines unless given `--all`, ending on one summary line; `hook` alone answers the harness in its own
format. A verb that changes the session's record is refused outside the primary checkout (`READS`).
"""

from __future__ import annotations

import argparse
import importlib
import io
import logging
import sys
from collections.abc import Callable, Sequence
from typing import Protocol, cast

from orchestra import FAULT, REFUSED, Refused, config

# Each verb, the module under `orchestra` that implements it, and its one-line help.
VERBS: dict[str, tuple[str, str]] = {
    "init": ("init", "make a session's state on this branch, then run doctor"),
    "doctor": ("init", "check the configuration, each program it names and its preflight commands"),
    "status": ("status", "agents by status, what waits, unclosed findings, CI and drift"),
    "brief": ("brief", "write a brief's header, or check a brief before its dispatch"),
    "judge": ("findings", "give every finding of a report its disposition"),
    "show": ("findings", "print findings whole from their reports"),
    "route": ("findings", "give rows one owner, an agent or a queue"),
    "close": ("findings", "close rows on evidence"),
    "findings": ("findings", "every unclosed row by owner"),
    "land": ("land", "merge an agent's branch, run the configured steps and close the rows it fixed"),
    "prune": ("prune", "remove every landed or commit-less agent's worktree and branch"),
    "ci": ("ci", "read every run, check and status of the pushed commit, record expected reds, or run the gate"),
    "queue": ("queue", "something to do when CI concludes, an agent lands or an agent reports"),
    "ask": ("rulings", "queue a question for the owner with its options"),
    "rule": ("rulings", "record the owner's ruling in the owner's words"),
    "note": ("log", "append one stamped line to the session's log"),
    "mode": ("mode", "set the session attended or away"),
    "diff": ("diff", "capture the session's change into one file for a lens round"),
    "log": ("log", "print the session's log"),
    "override": ("override", "let the next refused call through once, logging the reason"),
    "hook": ("hooks", "answer a harness hook event"),
}
# The most lines a verb prints without `--all`, its summary line included.
LINES = 40
# Each verb that, as given, only reads the session's record, and so runs anywhere, an agent's worktree
# included; every other verb, a new one too, changes it and runs in the primary checkout alone. An
# agent's commands carry its coordinator's session id, which is all the lease checks, so where a command
# runs is what parts an agent from the coordinator: a guard against an accident, which a `cd` passes.
READS: dict[str, Callable[[argparse.Namespace], bool]] = {
    "doctor": lambda _: True,
    "status": lambda _: True,
    "show": lambda _: True,
    "findings": lambda _: True,
    "log": lambda _: True,
    "queue": lambda args: args.action is None,
    "ask": lambda args: args.question is None,
    # The capture alone is a file of the lens round's, never the record; `--tree` logs the tree it makes.
    "diff": lambda args: not args.tree,
    "land": lambda args: args.preview,
}


class Verbs(Protocol):
    """What a verb's module exposes: `args.verb` names which of its verbs runs."""

    def add(self, verb: str, parser: argparse.ArgumentParser) -> None: ...

    # A refusal raises `Refused`.
    def run(self, args: argparse.Namespace) -> int: ...


def module(verb: str) -> Verbs:
    return cast("Verbs", importlib.import_module(f"orchestra.{VERBS[verb][0]}"))


def emit(
    args: argparse.Namespace,
    lines: Sequence[str],
    summary: str,
    *,
    tail: bool = False,
    rest: str = "--all prints every one",
) -> None:
    """The lines and the summary line, forty in all unless `--all`; `tail` keeps the last lines, not the first.

    `rest` says where the lines cut are printed whole. The notice of a cut goes to standard error, so a
    pipe filtering the lines never drops it unseen, and in its place among them where the two streams merge.
    """
    room = LINES - 2
    if getattr(args, "all", False) or len(lines) <= room + 1:
        shown, cut = list(lines), 0
    else:
        shown, cut = (list(lines[-room:]) if tail else list(lines[:room])), len(lines) - room

    def notice() -> None:
        # A pipe buffers standard output, so the lines before the notice are flushed first to keep its place.
        sys.stdout.flush()
        print(f"... {cut} {'earlier' if tail else 'more'} line(s): {rest}", file=sys.stderr, flush=True)

    if cut and tail:
        notice()
    for line in shown:
        print(line)
    if cut and not tail:
        notice()
    print(summary)


def given(words: list[str]) -> str:
    """The words as typed, or stdin's text for `-`: a shell runs a backtick inside double quotes as a command."""
    return sys.stdin.read() if words == ["-"] else " ".join(words)


def one_line(text: str) -> str:
    """The text on one line, so a line break typed into it never splits a printed line."""
    return " ".join(text.split())


def parser(chosen: str | None = None) -> tuple[argparse.ArgumentParser, Verbs | None]:
    """The parser for every verb, and the module of `chosen`, whose own arguments only it holds."""
    top = argparse.ArgumentParser(
        prog="orchestra", description="Run a fleet of agents from state files this program owns."
    )
    verbs = top.add_subparsers(dest="verb", required=True, metavar="<verb>")
    found: Verbs | None = None
    for verb, (_, summary) in VERBS.items():
        sub = verbs.add_parser(verb, help=summary, description=summary)
        if verb == chosen:
            found = module(verb)
            if verb != "hook":
                sub.add_argument("--all", action="store_true", help=f"print every line rather than {LINES} at most")
            found.add(verb, sub)
    return top, found


def main(argv: Sequence[str] | None = None) -> int:
    # A Windows pipe takes the console's code page, which cannot carry every character a state file holds,
    # and its text mode ends each printed line in `\r\n`, which a skill injects into the model's context.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
            if stream is not sys.stdin:
                stream.reconfigure(newline="\n")
    words = list(sys.argv[1:] if argv is None else argv)
    chosen = words[0] if words and words[0] in VERBS else None
    try:
        top, found = parser(chosen)
        args = top.parse_args(words)
        # One checkout for the whole command: the guard's, which the verb's own call reads again.
        with config.once():
            if not READS.get(args.verb, lambda _: False)(args):
                config.primary_checkout(args.verb)
            # parse_args exits on any verb but `chosen`, which alone has a module.
            return cast("Verbs", found).run(args)
    except Refused as refused:
        print(f"orchestra {chosen}: {refused}" if chosen else f"orchestra: {refused}", file=sys.stderr)
        return REFUSED
    except Exception:
        # Python's own exit 1 would read as done with something remaining, and as no code `land` lists.
        logging.getLogger("orchestra").exception(
            "orchestra%s failed: a fault, not a verdict", f" {chosen}" if chosen else ""
        )
        return FAULT
