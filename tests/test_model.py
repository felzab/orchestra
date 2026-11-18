"""The data model: each kind read back as written, and a finding's state as its last event."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_agent
from orchestra import Refused, model


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / "programme" / "session"
    path.mkdir(parents=True)
    return path


@pytest.mark.parametrize("name", ["CON", "NUL", "COM1", "LPT9"])
def test_a_name_windows_keeps_for_a_device_is_no_agent_name(state: Path, name: str) -> None:
    """Its record, brief and report would reach the device, whatever extension follows the name."""
    with pytest.raises(Refused, match="a name Windows keeps for a device"):
        model.agent_path(state, name)
    assert not model.has_agent(state, name)
    assert model.agent_path(state, f"{name}-2").name == f"{name}-2.json"


def test_each_kind_reads_back_as_written(state: Path) -> None:
    session = model.Session("programme", "session", "work", "main", "abc", model.now(), rulings_from=101)
    model.write_session(state, session)
    assert model.read_session(state) == session
    agent = model.Agent(
        "API-AUTH",
        "writer",
        "reported",
        "opus",
        "high",
        ("a1",),
        {1: "r1.md", 2: "r2.md"},
        since={"reported": "earlier"},
    )
    write_agent(state, agent)
    assert model.read_agent(state, "API-AUTH") == agent
    assert (
        model.update_agent(state, "API-AUTH", lambda held: model.Agent(held.name, held.type, "judged")).status
        == "judged"
    )
    item = model.QueueItem(1, "landed", "push once it lands", agent="API-AUTH")
    model.write_queue(state, lambda _: [item])
    assert model.read_queue(state) == {1: item}
    assert model.next_ruling(state, {}) == 101
    ruling = model.Ruling(1, "Keep the cap?", "session", ("yes", "no"), number=101, words="no", date="2026-11-17")
    model.write_rulings(state, lambda _: [ruling])
    assert model.read_rulings(state) == {1: ruling}
    assert model.next_ruling(state, model.read_rulings(state)) == 102


def test_a_row_folds_to_its_last_event_through_route_close_and_reopen(state: Path) -> None:
    steps = [
        model.FindingEvent("API-AUTH-F1", "bank", "OPEN"),
        model.FindingEvent("API-AUTH-F1", "route", "ROUTED", owner="FIXER"),
        model.FindingEvent("API-AUTH-F1", "close", "FIXED", owner="FIXER", evidence="abc1234"),
        model.FindingEvent("API-AUTH-F1", "reopen", "ROUTED", owner="owner"),
    ]
    model.write_findings(state, lambda _: [model.FindingEvent("API-AUTH-F2", "bank", "OPEN")])
    for step in steps:
        model.write_findings(state, lambda _, step=step: [step])
        assert model.read_findings(state)["API-AUTH-F1"] == step
    assert list(model.read_findings(state)) == ["API-AUTH-F2", "API-AUTH-F1"]


def test_a_status_change_stamps_when_it_was_entered(state: Path) -> None:
    write_agent(state, model.Agent("API-AUTH", "writer", "running", since={"running": "earlier"}))
    same = model.update_agent(state, "API-AUTH", lambda held: held)
    assert same.since == {"running": "earlier"}
    moved = model.update_agent(
        state, "API-AUTH", lambda held: model.Agent(held.name, held.type, "reported", since=held.since)
    )
    assert moved.since["running"] == "earlier"
    assert moved.since["reported"] != "earlier"
    fresh = model.update_agent(state, "API-AUTH", lambda held: model.Agent(held.name, held.type, "judged"))
    assert set(fresh.since) == {"running", "reported", "judged"}


@pytest.mark.parametrize(
    ("row", "valid"),
    [
        ("API-AUTH-F1", True),
        ("API-AUTH.2-F3", True),
        ("API-AUTH.12-F1", True),
        ("API-AUTH.1-F1", False),
        ("FE.0-F1", False),
    ],
)
def test_a_row_id_carries_its_round_from_the_second_on(row: str, *, valid: bool) -> None:
    assert bool(model.ROW_RE.fullmatch(row)) is valid


@pytest.mark.parametrize(
    ("kind", "status", "owner", "evidence"),
    [
        ("route", "ROUTED", None, None),
        ("close", "FIXED", None, None),
        ("close", "ROUTED", "X", None),
        ("reopen", "FIXED", "X", "e"),
        ("bank", "OPEN", None, None),
    ],
    ids=[
        "routed to nobody",
        "closed on nothing",
        "a close that leaves it open",
        "a reopen that leaves it closed",
        "a malformed row id",
    ],
)
def test_an_event_no_row_may_hold_is_refused(
    kind: model.FindingKind, status: model.FindingStatus, owner: str | None, evidence: str | None
) -> None:
    row = "F1" if kind == "bank" else "API-AUTH-F1"
    with pytest.raises(ValueError, match=r"API-AUTH-F1|`F1`"):
        model.FindingEvent(row, kind, status, owner, evidence)
