"""`override`: the next call a hook would refuse its caller let through once, its reason in the log.

A hook that refuses wrongly would otherwise hold the session until someone edits the hook, so each
refusing hook asks `consume` first. One override waits at a time, a newer one replacing it, and serves one refusal
of the caller it names, the coordinator unless `--agent` names an agent, so no other caller's
refusal in a busy wave takes it first.

A hook's refusal that no override served is kept by its caller, the latest only, and an override is
armed only for a caller holding one: a verb's refusal, which no override reaches, would otherwise
leave an override waiting to let an unrelated refusal through. Serving a refusal clears its caller's.
"""

from __future__ import annotations

import argparse
import datetime
from pathlib import Path

from orchestra import DONE, Refused, cli, log, model, store
from orchestra.store import Record

# The keys of the override waiting; `refused` holds each caller's latest refusal no override served.
ARMED = ("reason", "for", "time")
REFUSED = "refused"


def add(verb: str, parser: argparse.ArgumentParser) -> None:
    del verb  # `override` is the module's one verb
    parser.add_argument("--agent", metavar="NAME", help="the agent whose next refusal it serves; the coordinator's")
    parser.add_argument(
        "--drop", action="store_true", help="remove the override waiting, which no refusal has used, instead"
    )
    parser.add_argument("reason", nargs="*", help="why the next refusal is wrong, kept in the log")


def _caller(name: str | None) -> str:
    return "the coordinator" if name is None else name


def _drop(args: argparse.Namespace, state: Path) -> int:
    dropped: list[Record] = []

    def drop(held: Record | None) -> Record:
        if held and held.get("reason"):
            dropped.append(held)
        return {key: value for key, value in (held or {}).items() if key not in ARMED}

    store.update_json(state / store.OVERRIDE, drop)
    if not dropped:
        cli.emit(args, [], "no override waits")
        return DONE
    said = f"dropped the override waiting for {_caller(dropped[0].get('for'))}: {dropped[0]['reason']}"
    log.record(state, "override", said)
    cli.emit(args, [], said)
    return DONE


def _refusals(held: Record | None) -> dict[str, Record]:
    found = (held or {}).get(REFUSED)
    return found if isinstance(found, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def waiting(state: Path) -> Record | None:
    """The override waiting, its `reason`, `for` and `time`; None where none waits."""
    path = state / store.OVERRIDE
    held = store.read_json(path) if path.exists() else {}
    return {key: held.get(key) for key in ARMED} if held.get("reason") else None


def run(args: argparse.Namespace) -> int:
    state = store.state_dir()
    reason = cli.one_line(" ".join(args.reason))
    if args.drop:
        if reason or args.agent is not None:
            raise Refused("`--drop` takes no reason and no `--agent`: one override waits, whoever it is for")
        return _drop(args, state)
    if not reason:
        raise Refused('an override needs its reason: `orchestra override "<reason>"`')
    name: str | None = args.agent
    if name is not None:
        model.read_agent(state, name)

    replaced: list[str] = []
    answered: list[Record] = []

    def arm(held: Record | None) -> Record:
        refused = _refusals(held).get(_caller(name))
        if refused is None:
            # Raised inside the lock, so the file is left as it was.
            raise Refused(
                f"no hook refusal of {_caller(name)} waits unserved: an override serves only a hook's refusal,"
                " and a verb's refusal names its own way forward"
            )
        answered.append(refused)
        # One waiting for a caller never refused again would wait for good, so the newer replaces it.
        if held and held.get("reason"):
            replaced.append(f"replaced the override waiting for {_caller(held.get('for'))}: {held['reason']}")
        return {**(held or {}), "reason": reason, "for": name, "time": model.now()}

    store.update_json(state / store.OVERRIDE, arm)
    for line in replaced:
        log.record(state, "override", line)
    log.record(state, "override", f"for {_caller(name)}: {reason}")
    last = answered[0]
    # So the coordinator sees which refusal it answers, and that it is the one it meant: the guard holds
    # the caller's latest refusal no override served, however long ago, not one just made.
    at = datetime.datetime.fromisoformat(str(last.get("time")))
    said = f"the latest hook refusal of {_caller(name)} no override served, at {at:%H:%M}, was {last.get('call')}"
    cli.emit(args, [*replaced, said], f"the next refused hook call of {_caller(name)} goes through once: {reason}")
    return DONE


def consume(state: Path, call: str, name: str | None = None) -> str | None:
    """The override waiting for agent `name`, or the coordinator for None, used up on `call`; None where none waits.

    Where none waits for that caller, the refusal is kept as its latest, which an override may then answer.
    """
    path = state / store.OVERRIDE
    taken: list[str] = []

    def take(held: Record | None) -> Record:
        refusals = _refusals(held)
        if held and held.get("reason") and held.get("for") == name:
            taken.append(str(held["reason"]))
            kept = {caller: refused for caller, refused in refusals.items() if caller != _caller(name)}
            return {REFUSED: kept} if kept else {}
        refusals = {**refusals, _caller(name): {"call": call, "time": model.now()}}
        return {**(held or {}), REFUSED: refusals}

    store.update_json(path, take)
    if not taken:
        return None
    log.record(state, "override", f"used on {call}: {taken[0]}")
    return taken[0]
