"""`queue`: items added and ended through the command line, and which are due after which event."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from conftest import inprocess, orchestra, write_agent
from orchestra import FAULT, model, store
from orchestra import queue as items
from orchestra.status import paced

EARLY = "2026-11-17T10:00:00+01:00"
LATER = "2026-11-17T11:00:00+01:00"


@pytest.fixture
def state(project: Path) -> Path:
    assert inprocess("init", "s1", cwd=project).returncode == 0
    held = store.state_dir(project)
    write_agent(held, model.Agent("A", "writer", "running"))
    return held


def queued(state: Path, *made: model.QueueItem) -> None:
    model.write_queue(state, lambda _: list(made))


def status(state: Path, name: str, to: model.AgentStatus) -> None:
    model.update_agent(state, name, lambda held: dataclasses.replace(held, status=to))


def numbers(state: Path) -> list[int]:
    return [item.number for item in items.due(state)]


def test_a_ci_item_is_due_once_a_concluded_run_is_read_after_it(state: Path) -> None:
    store.append_jsonl(state / store.LOG, {"time": EARLY, "kind": items.CI_READ, "text": "run 1 concluded"})
    queued(state, model.QueueItem(1, "ci", "push the fix", time=LATER))
    store.append_jsonl(state / store.LOG, {"time": LATER, "kind": "note", "text": "a note is no run"})
    assert numbers(state) == []
    store.append_jsonl(state / store.LOG, {"time": "2026-11-17T12:00:00+01:00", "kind": "ci", "text": "run 2"})
    assert numbers(state) == [1]


def test_a_landed_item_is_due_once_its_agent_lands(state: Path) -> None:
    queued(state, model.QueueItem(1, "landed", "dispatch B", "A", time=EARLY))
    status(state, "A", "reported")
    assert numbers(state) == []
    status(state, "A", "landed")
    assert numbers(state) == [1]


def test_a_landed_item_never_falls_due_when_its_agent_is_discarded(project: Path, state: Path) -> None:
    """A discarded agent is settled as a landed one is, and nothing of it landed."""
    write_agent(state, model.Agent("B", "writer", "stopped"))
    queued(state, model.QueueItem(1, "landed", "tell X the contract landed", "B", time=EARLY))
    assert inprocess("prune", "--discard", "B", cwd=project).returncode == 0
    assert model.read_agent(state, "B").status == "pruned"
    assert numbers(state) == []
    said = "which was discarded and never lands: `orchestra queue done 1` ends it"
    assert any(said in line for line in paced(state))


def reported(state: Path, at: str) -> None:
    """A's record as a report at `at` leaves it."""
    write_agent(state, model.Agent("A", "writer", "reported", since={"reported": at}))


def test_a_reported_item_is_due_from_the_first_report_after_it_was_queued(state: Path) -> None:
    """An item queued after a report, for the round a resume starts, waits for that round's report."""
    queued(state, model.QueueItem(1, "reported", "judge A", "A", time=EARLY))
    assert numbers(state) == []
    reported(state, LATER)
    assert numbers(state) == [1]
    queued(state, model.QueueItem(2, "reported", "judge round two", "A", time="2026-11-17T12:00:00+01:00"))
    assert numbers(state) == [1]
    status(state, "A", "running")
    assert numbers(state) == [1]
    reported(state, "2026-11-17T13:00:00+01:00")
    assert numbers(state) == [1, 2]


def test_add_and_done_number_the_items_and_log_the_end(project: Path, state: Path) -> None:
    added = inprocess("queue", "add", "--when", "landed:A", "dispatch", "B", cwd=project)
    assert added.returncode == 0, added.stderr
    assert added.stdout.splitlines() == ["  1  waits  landed:A                  dispatch B"]
    piped = inprocess("queue", "add", "--when", "ci", "-", cwd=project, stdin=b"push `abc`\n")
    assert piped.stdout.split() == ["2", "waits", "ci", "push", "`abc`"]
    status(state, "A", "landed")
    listed = inprocess("queue", cwd=project)
    assert listed.stdout.splitlines()[0] == "  1  DUE    landed:A                  dispatch B"
    assert listed.stdout.splitlines()[-1].startswith("2 item(s) waiting, 1 due")
    done = inprocess("queue", "done", "1", cwd=project)
    assert done.returncode == 0, done.stderr
    assert done.stdout.rstrip().endswith("queue     done 1: dispatch B")
    assert numbers(state) == []
    assert model.read_queue(state)[1].done
    again = inprocess("queue", "done", "1", cwd=project)
    assert (again.returncode, again.stderr.strip()) == (2, "orchestra queue: queue item 1 is done already")


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (["add", "--when", "landed:NOBODY", "x"], "no agent NOBODY in this session"),
        (["add", "--when", "merged:A", "x"], "`merged:A` is none of ci, landed:NAME and reported:NAME"),
    ],
)
def test_an_event_naming_no_agent_or_no_kind_is_refused(project: Path, state: Path, args: list[str], said: str) -> None:
    done = inprocess("queue", *args, cwd=project, stdin=b"")
    assert done.returncode == 2
    assert said in done.stderr
    assert model.read_queue(state) == {}


def test_a_fault_exits_with_a_code_of_its_own_and_prints_its_trace(project: Path, state: Path) -> None:
    """Python's own exit 1 would read as done with something remaining; a fault is no verdict at all."""
    status(state, "A", "reported")
    queued(state, model.QueueItem(1, "reported", "judge A", agent="A", time="not a time"))
    done = orchestra("queue", cwd=project)
    assert done.returncode == FAULT == 70, done.stderr
    assert done.stderr.startswith("orchestra queue failed: a fault, not a verdict\nTraceback")
    assert "ValueError" in done.stderr
