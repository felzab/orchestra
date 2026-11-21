"""`ask` and `rule`: a question queued for the owner with its options, and the owner's ruling on it.

A question is queued rather than asked in the session while nobody may be there to answer it, so it
never parks the fleet; the owner rules on it later, and the ruling keeps the owner's own words under
the programme's next ruling number. `ask` with no question lists the open ones, as the owner's
return reads them. A question asked again with `--supersedes N` closes question N unruled, so one
answer is never recorded on both; where N is ruled already, its ruling is marked replaced by the new
question's, so no reader of the rulings takes it as standing.
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

from orchestra import DONE, REMAINS, Refused, cli, log, model, store


def add(verb: str, parser: argparse.ArgumentParser) -> None:
    if verb == "ask":
        parser.add_argument("question", nargs="?", help="the question, readable alone; none lists the open ones")
        parser.add_argument(
            "--option",
            action="append",
            default=[],
            metavar='"<label>: <consequence>"',
            help="one answer the owner can give and what follows from it; repeat for each",
        )
        parser.add_argument("--recommend", type=int, metavar="N", help="the option recommended, counted from 1")
        parser.add_argument(
            "--supersedes",
            type=int,
            metavar="ID",
            help="the question this one asks again: an open one is closed and takes no ruling, a ruled one's ruling is"
            " marked replaced by this one's",
        )
    else:
        parser.add_argument("ask", type=int, metavar="ID", help="the question's number, as `orchestra ask` lists it")
        parser.add_argument("words", nargs="+", help="the owner's ruling in the owner's own words, or - to read stdin")


def open_questions(state: Path) -> list[model.Ruling]:
    """Every question of the programme with no ruling yet, oldest first."""
    return [held for held in model.read_rulings(state).values() if held.open]


def shown(question: model.Ruling) -> list[str]:
    """A question as printed lines: its number and words, then each option, the recommended one marked."""
    lines = [f"question {question.ask}, asked {question.asked[:16].replace('T', ' ')}: {question.question}"]
    for k, option in enumerate(question.options, 1):
        lines.append(f"  {k}. {option}" + (" (recommended)" if k == question.recommend else ""))
    return lines


def run(args: argparse.Namespace) -> int:
    state = store.state_dir()
    if args.verb == "rule":
        return _rule(args, state)
    if args.question is None:
        if args.option or args.recommend is not None or args.supersedes is not None:
            raise Refused("--option, --recommend and --supersedes belong to a question: give the question first")
        waiting = open_questions(state)
        lines = [line for question in waiting for line in shown(question)]
        summary = (
            f'{len(waiting)} question(s) open: `orchestra rule ID "<the owner\'s words>"` records each ruling'
            if waiting
            else "no question is open"
        )
        cli.emit(args, lines, summary)
        return REMAINS if waiting else DONE
    return _ask(args, state)


def _ask(args: argparse.Namespace, state: Path) -> int:
    question = cli.one_line(str(args.question))
    if not question:
        raise Refused("a question needs words")
    options: list[str] = []
    for given in args.option:
        label, colon, consequence = str(given).partition(":")
        # Each option carries what follows from it, so the owner chooses between outcomes, not labels.
        if not (colon and label.strip() and consequence.strip()):
            raise Refused(f'`--option {given}` needs a label and its consequence: "<label>: <consequence>"')
        options.append(f"{label.strip()}: {consequence.strip()}")
    recommend: int | None = args.recommend
    if recommend is not None and not 1 <= recommend <= len(options):
        raise Refused(f"--recommend {recommend} names no option: give 1 to {len(options)}, counted in the order given")
    session = model.read_session(state)
    # The rulings file belongs to the programme, beside the sessions, so no state write checks the lease for it.
    store.check_lease(state)

    def append(held: dict[int, model.Ruling]) -> list[model.Ruling]:
        number = max(held, default=0) + 1
        made = model.Ruling(number, question, session.session, tuple(options), recommend)
        if (old := args.supersedes) is None:
            return [made]
        if old not in held:
            raise Refused(
                f"--supersedes {old} names no question in this programme: `orchestra ask` lists the open ones"
            )
        # A ruled question may be asked again too, so the ruling it replaces is marked as replaced.
        if (later := held[old].superseded_by) is not None:
            raise Refused(f"question {old} was asked again as question {later} already: supersede that one")
        return [dataclasses.replace(held[old], superseded_by=number), made]

    *replaced, asked = model.write_rulings(state, append)
    again = "".join(
        f", superseding question {old.ask}" + (f" and its ruling {old.number}" if old.number is not None else "")
        for old in replaced
    )
    log.record(state, "ask", f"question {asked.ask}{again}: {question}")
    cli.emit(args, shown(asked)[1:], f"queued question {asked.ask}{again}: {question}")
    return DONE


def _rule(args: argparse.Namespace, state: Path) -> int:
    text = cli.given(args.words).strip()
    if not text:
        raise Refused("a ruling needs the owner's words: give them as arguments, or `-` and the words on stdin")
    store.check_lease(state)

    def record(held: dict[int, model.Ruling]) -> list[model.Ruling]:
        if args.ask not in held:
            raise Refused(f"no question {args.ask} in this programme: `orchestra ask` lists the open ones")
        asked = held[args.ask]
        if asked.superseded_by is not None:
            raise Refused(f"question {args.ask} was asked again as question {asked.superseded_by}: rule on that one")
        if asked.number is not None:
            raise Refused(
                f"question {args.ask} has ruling {asked.number} already: {asked.words}."
                " A changed ruling is a new question and its own ruling"
            )
        number = model.next_ruling(state, held)
        return [dataclasses.replace(asked, number=number, words=text, date=model.now())]

    (ruled,) = model.write_rulings(state, record)
    said = f"ruling {ruled.number} on question {ruled.ask}: {cli.one_line(text)}"
    log.record(state, "rule", said)
    cli.emit(args, [], said)
    return DONE
