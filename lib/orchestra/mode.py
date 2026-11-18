"""`mode`: the session attended or away, and the hand-over of its state between Claude Code sessions.

`away` keeps a session nobody watches from waiting on a person. While `away.json` exists, the plugin's
hooks answer for the Claude Code session holding the state's lease and for its agents: a permission
prompt is refused, `AskUserQuestion` and `ExitPlanMode` are denied, and each refusal is recorded
(`events`). No settings file changes, so every other Claude Code session prompts as before, except
while a hook faults: one that cannot tell whose event it is refuses any session's prompt and question
in the checkout, since nobody may be there to answer. `attended` ends it and prints what happened
meanwhile: the log, the refusals and the open questions.

`stop` and `takeover` move the lease `store.py` describes; `takeover --wait` waits, up to `WAIT_SECONDS`,
for a holder still writing to stop or fall quiet, so a resume runs it once in the background.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import time
from pathlib import Path

from orchestra import DONE, REMAINS, Refused, cli, events, log, model, rulings, store
from orchestra.store import Record

MODES = ("attended", "away", "stop", "takeover")
# A note opening with this is a decision taken while nobody could be asked, which the return lists.
DECISION = "Decision:"
# The longest `takeover --wait` waits for a holder still writing, and the most it sleeps between tries:
# a holder's agents writing on put the quiet period off, and a resume should not wait on them for good.
WAIT_SECONDS = 15 * 60
TRY_SECONDS = 30


def add(verb: str, parser: argparse.ArgumentParser) -> None:
    del verb  # `mode` is the module's one verb
    parser.add_argument(
        "mode",
        choices=MODES,
        help="attended or away [until]; stop ends this session's writes; takeover <reason> continues a stopped one",
    )
    parser.add_argument("words", nargs="*", help="away: until when; takeover: why")
    parser.add_argument(
        "--wait",
        action="store_true",
        help=f"takeover: wait up to {WAIT_SECONDS // 60} min for a holder still writing to stop or fall quiet",
    )


def run(args: argparse.Namespace) -> int:
    words = cli.one_line(" ".join(args.words))
    if args.mode in {"attended", "stop"} and words:
        raise Refused(f"`orchestra mode {args.mode}` takes no words")
    if args.mode == "takeover" and not words:
        raise Refused('a takeover needs its reason: `orchestra mode takeover "<reason>"`')
    if args.wait and args.mode != "takeover":
        raise Refused(f"--wait belongs to a takeover, not to `mode {args.mode}`")
    state = store.state_dir()
    if args.mode == "away":
        return _away(args, state, words or None)
    if args.mode == "attended":
        return _attended(args, state)
    if args.mode == "stop":
        return _stop(args, state)
    return _takeover(args, state, words)


def _away(args: argparse.Namespace, state: Path, until: str | None) -> int:
    if (state / store.AWAY).exists():
        raise Refused("the session is away already: `orchestra mode attended` ends it")
    # The hooks know the away session by the lease, which only a command run with a session id takes.
    if store.caller() is None:
        raise Refused(
            f"{store.SESSION_ID} is unset, so no Claude Code session would be away: run this from the"
            " coordinator's session, as `/orchestra:away` does"
        )
    # The lengths mark where the away begins: a clock to the second cannot part two lines of one second.
    record = {
        "since": model.now(),
        "until": until,
        "log": len(log.entries(state)),
        "refusals": len(store.read_jsonl(state / events.REFUSED)),
    }
    # Written through the lease, which this session then holds, so the hooks answer for it alone.
    store.write_json(state / store.AWAY, record)
    said = f"away{f' until {until}' if until else ''}: this session's prompts are refused and its questions denied"
    log.record(state, "mode", said)
    cli.emit(args, [], f"{said}, each recorded; `orchestra mode attended` ends it")
    return DONE


def _refusals(records: list[Record]) -> list[str]:
    """Each refused call once, by its tool and input, with how often and by whom, the latest last."""
    grouped: dict[tuple[str, str], list[Record]] = {}
    for record in records:
        grouped.setdefault((str(record.get("tool")), str(record.get("input"))), []).append(record)
    lines: list[str] = []
    for (tool, given), held in sorted(grouped.items(), key=lambda item: str(item[1][-1].get("time"))):
        who = sorted({str(record.get("agent_type") or "the coordinator") for record in held})
        reasons = sorted({str(record["reason"]) for record in held if record.get("reason")})
        why = f"; auto mode: {', '.join(reasons)}" if reasons else ""
        last = str(held[-1].get("time"))[11:16]
        lines.append(f"refused   {len(held)}x {tool}: {given} ({', '.join(who)}; last at {last}{why})")
    return lines


def _attended(args: argparse.Namespace, state: Path) -> int:
    # Refused before anything is printed, rather than at the record's removal after it.
    store.check_lease(state)
    path = state / store.AWAY
    record = store.read_json(path) if path.exists() else None
    passed = log.entries(state)[record["log"] :] if record is not None else []
    refused = store.read_jsonl(state / events.REFUSED)[record.get("refusals", 0) :] if record is not None else []
    # The log since the away first, so a cut keeps its latest lines and every line below them.
    lines = [log.line(entry) for entry in passed] + _refusals(refused)
    if record is None:
        lines.append("nothing to end: the session was not away")
    else:
        store.remove(path)
    waiting = rulings.open_questions(state)
    lines += [line for question in waiting for line in rulings.shown(question)]
    decisions = sum(entry.kind == "note" and entry.text.startswith(DECISION) for entry in passed)
    log.record(state, "mode", "attended")
    summary = (
        f"attended; {len(passed)} log line(s) since away, {decisions} decision(s) taken alone,"
        f" {len(refused)} call(s) refused, {len(waiting)} question(s) open"
    )
    # A second run finds no away record, so the lines cut are the log's to print.
    cli.emit(args, lines, summary, tail=True, rest="`orchestra log --all` prints every one")
    return REMAINS if waiting else DONE


def _stop(args: argparse.Namespace, state: Path) -> int:
    if store.lease(state).get("stopped"):
        cli.emit(args, [], 'stopped already: `orchestra mode takeover "<reason>"` continues it from another session')
        return DONE
    # Logged first: once stopped, this session's own writes are refused.
    log.record(state, "mode", f"stopped by session {store.caller() or 'with no id'}")
    store.stop_lease(state)
    cli.emit(args, [], 'stopped: no session writes this state until `orchestra mode takeover "<reason>"`')
    return DONE


def _taken(state: Path, *, wait: bool) -> store.Record:
    """The lease taken, as `store.take_lease` takes it; with `wait`, tried until the holder is quiet or stopped.

    Each try sleeps until the quiet period the holder's last write set ends, `TRY_SECONDS` at most, and the
    last refusal stands once `WAIT_SECONDS` have passed. A refusal no wait can change is raised after one
    more try at once, which passes where the quiet period ended between the refusal and its re-read.
    """
    end = time.monotonic() + WAIT_SECONDS
    quiet = False
    while True:
        try:
            return store.take_lease(state)
        except Refused:
            at = store.takeover_at(state)
            left = end - time.monotonic()
            if not wait or left <= 0 or store.caller() is None or (at is None and quiet):
                raise
            quiet = at is None
            if at is None:
                continue
            ahead = (at - datetime.datetime.now().astimezone()).total_seconds()
            time.sleep(max(1.0, min(ahead + 1, TRY_SECONDS, left)))


def _takeover(args: argparse.Namespace, state: Path, reason: str) -> int:
    before = _taken(state, wait=args.wait)
    me = store.caller()
    if before.get("owner") == me and not before.get("stopped"):
        said = f"session {me} holds this state already"
    else:
        old = before.get("owner") or "none"
        how = "stopped" if before.get("stopped") else "quiet" if before else "unheld"
        said = f"session {me} took this state over from session {old} ({how}): {reason}"
        log.record(state, "mode", said)
    # No session can resume an agent another session started, so each left running from before this
    # session took the lease is stopped: then it can be judged, its rows routed again and its tree
    # pruned. Run again, a takeover cut short after the lease finishes this; the session's own agents,
    # running since it took the lease, stay.
    taken = datetime.datetime.fromisoformat(str(store.lease(state)["since"]))
    why = f"session {me} took this state over"

    def left_before(held: model.Agent) -> bool:
        started = held.since.get("running")
        return held.status == "running" and (started is None or datetime.datetime.fromisoformat(started) <= taken)

    left = [agent.name for agent in model.agents(state) if left_before(agent)]
    for name in left:
        model.update_agent(
            state,
            name,
            lambda held: dataclasses.replace(held, status="stopped", reason=why) if left_before(held) else held,
        )
    if left:
        log.record(state, "mode", f"stopped {', '.join(left)}: {why}")
    cli.emit(args, [f"stopped {name}: {why}" for name in left], said)
    return DONE
