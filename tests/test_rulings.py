"""`ask` and `rule`: a question queued with its options, and the owner's ruling under the next number."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import inprocess
from orchestra import log, model, store


@pytest.fixture
def state(project: Path) -> Path:
    """A session whose programme's rulings start at 101."""
    assert inprocess("init", "s1", "--rulings-from", "101", cwd=project).returncode == 0
    return project.parent / "state" / "project" / "s1"


def test_a_question_waits_with_its_options_until_the_owner_rules_on_it(project: Path, state: Path) -> None:
    asked = inprocess(
        "ask",
        "Keep the cap at 20?",
        "--option",
        "keep: every dispatch past 20 waits",
        "--option",
        "raise: two more agents run at once",
        "--recommend",
        "2",
        cwd=project,
    )
    assert asked.returncode == 0, asked.stderr
    listed = inprocess("ask", cwd=project)
    assert listed.returncode == 1
    assert "  2. raise: two more agents run at once (recommended)" in listed.stdout
    ruled = inprocess("rule", "1", "Raise it, but only this week.", cwd=project)
    assert ruled.returncode == 0, ruled.stderr
    held = model.read_rulings(state)[1]
    assert (held.number, held.words, held.recommend) == (101, "Raise it, but only this week.", 2)
    assert inprocess("ask", cwd=project).returncode == 0
    again = inprocess("rule", "1", "Keep it.", cwd=project)
    assert again.returncode == 2
    assert "question 1 has ruling 101 already: Raise it, but only this week. A changed" in again.stderr
    assert "A changed ruling is a new question, asked with `--supersedes 1`, and its own ruling" in again.stderr


def test_a_question_asked_again_closes_the_first_which_takes_no_ruling(project: Path, state: Path) -> None:
    """One answer recorded on both would make the stored rulings contradict each other."""
    assert inprocess("ask", "Match whole words?", cwd=project).returncode == 0
    again = inprocess("ask", "Match whole words, case folded?", "--supersedes", "1", cwd=project)
    assert again.returncode == 0, again.stderr
    assert again.stdout.splitlines()[-1] == "queued question 2, superseding question 1: Match whole words, case folded?"
    assert "question 1," not in inprocess("ask", cwd=project).stdout
    measured = inprocess("status", "--measure", cwd=project).stdout.splitlines()
    assert [line for line in measured if line.startswith("asked")] == ["asked     #2 Match whole words, case folded?"]
    assert measured[-1].endswith(", 1 asked and not ruled")
    refused = inprocess("rule", "1", "Yes.", cwd=project)
    assert refused.returncode == 2
    assert "question 1 was asked again as question 2: rule on that one" in refused.stderr
    assert model.read_rulings(state)[1].number is None
    twice = inprocess("ask", "A third", "--supersedes", "1", cwd=project)
    assert twice.returncode == 2
    assert "question 1 was asked again as question 2 already: supersede that one" in twice.stderr
    assert inprocess("rule", "2", "Yes.", cwd=project).returncode == 0


def test_a_ruled_question_asked_again_is_marked_replaced_and_takes_no_second_ruling(project: Path, state: Path) -> None:
    """A ruling the owner later replaces must not read as standing to whoever reads the rulings."""
    assert inprocess("ask", "Keep the cap at 20?", cwd=project).returncode == 0
    assert inprocess("rule", "1", "Keep it.", cwd=project).returncode == 0
    again = inprocess("ask", "Keep the cap at 20 this week too?", "--supersedes", "1", cwd=project)
    assert again.returncode == 0, again.stderr
    said = "queued question 2, superseding question 1 and its ruling 101: Keep the cap at 20 this week too?"
    assert again.stdout.splitlines()[-1] == said
    held = model.read_rulings(state)[1]
    assert (held.number, held.words, held.superseded_by) == (101, "Keep it.", 2)
    refused = inprocess("rule", "1", "Raise it.", cwd=project)
    assert refused.returncode == 2
    assert "question 1 was asked again as question 2: rule on that one" in refused.stderr
    missing = inprocess("ask", "A fourth", "--supersedes", "9", cwd=project)
    assert missing.returncode == 2
    assert "--supersedes 9 names no question in this programme" in missing.stderr


def test_a_ruling_of_another_session_asked_again_names_that_session(project: Path, state: Path) -> None:
    """The programme's questions interleave across its sessions, so a mistyped id can reach another session's ruling.

    A successor session may rightly ask its predecessor's ruling again, so it is not refused, but the summary and
    the log line name whose ruling it marks replaced.
    """
    earlier = model.Ruling(1, "Keep the cap at 20?", "s0", number=101, words="Keep it.")
    model.write_rulings(state, lambda _: [earlier])
    again = inprocess("ask", "Keep the cap at 20 this week too?", "--supersedes", "1", cwd=project)
    assert again.returncode == 0, again.stderr
    said = "superseding session s0's question 1 and its ruling 101: Keep the cap at 20 this week too?"
    assert again.stdout.splitlines()[-1] == f"queued question 2, {said}"
    assert [entry.text for entry in log.entries(state) if entry.kind == "ask"] == [f"question 2, {said}"]
    assert model.read_rulings(state)[1].superseded_by == 2
    # The asking session's own ruling names no session.
    assert inprocess("rule", "2", "Keep it this week too.", cwd=project).returncode == 0
    own = inprocess("ask", "Keep the cap at 20 next week?", "--supersedes", "2", cwd=project)
    assert own.stdout.splitlines()[-1].startswith("queued question 3, superseding question 2 and its ruling 102:")


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (("--option", "keep"), "needs a label and its consequence"),
        (("--option", "keep: nothing changes", "--recommend", "2"), "--recommend 2 names no option"),
    ],
)
def test_an_option_without_its_consequence_or_a_recommendation_past_them_is_refused(
    project: Path, state: Path, args: tuple[str, ...], said: str
) -> None:
    done = inprocess("ask", "Keep the cap?", *args, cwd=project)
    assert done.returncode == 2
    assert said in done.stderr
    assert model.read_rulings(state) == {}


def test_another_sessions_question_and_ruling_are_refused_and_write_nothing(project: Path) -> None:
    """The rulings file sits beside the sessions, past the store's guard, so each verb reads the lease itself."""
    owner, other = {store.SESSION_ID: "session-owner"}, {store.SESSION_ID: "session-other"}
    assert inprocess("init", "s1", cwd=project, env=owner).returncode == 0
    state = store.state_dir(project)
    assert inprocess("ask", "Keep the cap?", cwd=project, env=owner).returncode == 0
    asked = inprocess("ask", "Raise the cap?", cwd=project, env=other)
    ruled = inprocess("rule", "1", "Keep it.", cwd=project, env=other)
    assert (asked.returncode, ruled.returncode) == (2, 2)
    held = model.read_rulings(state)
    assert list(held) == [1]
    assert held[1].number is None
