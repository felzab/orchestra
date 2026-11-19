"""`status`, `status --measure` and `status --end`, called over a built state."""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import shutil
from pathlib import Path

import pytest

from conftest import claude_stub, git, inprocess, line, transcript, usage_answer, write_agent, write_config
from orchestra import ci, model, override, status, store

PLAN = """# Plan

## The request

Check the docs and the tests.

## Slices and their files

- [x] S1 `src/`
- [x] S0 `docs/own.md`, `notes/`
- [x] S4 review of the docs
- [ ] S2 docs/guide.md
- [ ] S3 tests/

## Landing order

- [ ] S2 before S3

## Ending

- [ ] the ending's checklist
"""


def _state(project: Path) -> Path:
    return project.parent / "state" / "project" / "s1"


def _ago(minutes: float) -> str:
    moment = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes)
    return moment.isoformat(timespec="seconds")


def _brief(state: Path, name: str, kind: str, slice_: str, owns: str) -> None:
    header = (
        f"Agent: {name}            Type: {kind}            Slice: {slice_}\n"
        "Base: main at 0000000\n"
        f"Report: {state / 'reports' / f'{name}.md'}\n"
        f"Owns: {owns}\nRows: none\nWrites: none\nLens: none\n"
    )
    (state / "briefs" / f"{name}.md").write_bytes(header.encode())


def _agent(state: Path, agent: model.Agent, slice_: str = "S2", owns: str = "docs/guide.md") -> None:
    write_agent(state, agent)
    _brief(state, agent.name, agent.type, slice_, owns)


def _ran(project: Path, ident: str, *, minutes: float, ended: bool = False, read: int = 0) -> None:
    """An agent's transcript, last changed `minutes` ago, mid-turn unless it `ended`."""
    folder = project.parent / "claude-config"
    transcript(folder, ident, line("assistant", "m1", read, calls=not ended), session="session-1", minutes=minutes)


@pytest.fixture
def built(project: Path) -> Path:
    """A session with an agent in every state status reads, its findings, queue, plan and a drifted file."""
    assert inprocess("init", "s1", cwd=project).returncode == 0
    state = _state(project)
    store.write_text(state / store.PLAN, PLAN)
    # Queued before the agents' records, so DONE's report, stamped as its record is written, comes after it.
    model.write_queue(
        state,
        lambda _: [
            model.QueueItem(1, "reported", "judge DONE's report", agent="DONE"),
            model.QueueItem(2, "landed", "push once FRESH lands", agent="FRESH"),
        ],
    )
    running = {"running": _ago(30)}
    _agent(state, model.Agent("FRESH", "writer", "running", ids=("a1",), reports={1: "r"}, since=running))
    _agent(state, model.Agent("QUIET", "writer", "running", ids=("a2",), reports={1: "r"}, since=running))
    _agent(state, model.Agent("ENDED", "auditor", "running", ids=("a3",), reports={1: "r"}, since=running))
    # Half a minute past each whole one, so a coarse clock's rounding never counts one short.
    _ran(project, "a1", minutes=1.5, read=83_838)
    _ran(project, "a2", minutes=20.5)
    # A `running` record whose turn ended reads as a stop hook that never ran once silent past `SILENT_MINUTES`.
    _ran(project, "a3", minutes=16, ended=True)
    rounds = {1: "r", 2: "r", 3: "r"}
    _agent(state, model.Agent("DONE", "writer", "reported", reports=rounds, outcome="DONE-WITH-CONCERNS"))
    _agent(state, model.Agent("BLOCK", "writer", "reported", reports={1: "r"}, outcome="BLOCKED"))
    _agent(state, model.Agent("ASKS", "researcher", "reported", reports={1: "r"}, outcome="NEEDS-ANSWER"))
    _agent(state, model.Agent("HALT", "writer", "stopped", reports={}, reason="usage limit, resets 18:00"))
    _agent(state, model.Agent("HELD", "driver", "judged", reports={1: "r"}, outcome="DONE"))
    _agent(state, model.Agent("LANDED", "writer", "landed", reports={1: "r"}), slice_="S1", owns="src/**")
    _agent(state, model.Agent("STRAY", "writer", "briefed"), slice_="S9")
    model.write_findings(
        state,
        lambda _: [
            model.FindingEvent("DONE-F1", "bank", "OPEN"),
            *[model.FindingEvent("DONE-F2", "route", "ROUTED", owner=owner) for owner in ("A", "B", "HELD")],
            model.FindingEvent("DONE-F3", "route", "ROUTED", owner="HELD"),
            model.FindingEvent("DONE-F4", "close", "FIXED", owner="HELD", evidence="abc1234"),
        ],
    )
    for path in ("src/app.py", "docs/stray.md", "docs/own.md", "notes/a.md"):
        (project / path).parent.mkdir(exist_ok=True)
        (project / path).write_bytes(b"x\n")
    git(project, "add", "src/app.py", "docs/stray.md", "docs/own.md", "notes/a.md")
    git(project, "commit", "-m", "work")
    return state


def _lines(done: object) -> list[str]:
    return str(getattr(done, "stdout", "")).splitlines()


def test_the_overview_says_what_runs_what_waits_and_what_drifted(project: Path, built: Path) -> None:
    git(project, "branch", "worktree-agent-h1")
    model.update_agent(built, "HELD", lambda held: dataclasses.replace(held, branch="worktree-agent-h1"))
    done = inprocess("status", cwd=project)
    assert done.returncode == 0, done.stderr
    lines = _lines(done)
    assert lines[0].endswith(" · a snapshot")
    datetime.datetime.fromisoformat(lines[0].split(" · ")[0])
    assert "agents    1 briefed, 3 running, 3 reported, 1 judged, 1 landed, 1 stopped" in lines
    assert "running   FRESH (writer, round 1): transcript written 1 min ago" in lines
    assert "BLOCKED   BLOCK (writer, round 1): its report waits on you" in lines
    assert "NEEDS-ANSWER ASKS (researcher, round 1): its report waits on you" in lines
    rounds = (
        "to judge  DONE (writer, round 3, DONE-WITH-CONCERNS); 3 rounds: re-plan it with a fresh agent or a new slice"
    )
    assert rounds in lines
    # HELD's branch holds nothing to land, so it is done.
    assert "judged    HELD" in lines
    assert not any(line.startswith("to land") for line in lines)
    assert "to resume HALT (writer, round 0): usage limit, resets 18:00" in lines
    assert "briefed   STRAY" in lines
    assert "due       #1 (when reported:DONE) judge DONE's report" in lines
    assert not any("#2" in line for line in lines)
    assert "unclosed  nobody (OPEN): DONE-F1" in lines
    assert "unclosed  HELD: DONE-F2 (routed to 3 agents: re-plan it), DONE-F3" in lines
    assert "drift     docs/stray.md: changed since the base, and no landed brief or ticked slice owns it" in lines
    # A driver whose branch nothing names may hold commits, so it is never read as done.
    model.update_agent(built, "HELD", lambda held: dataclasses.replace(held, branch=None))
    unknown = "branch unknown; `orchestra prune --discard HELD` settles it once you have saved what it holds"
    assert f"to land   HELD (driver, round 1, DONE): {unknown}" in _lines(inprocess("status", cwd=project))
    # The coordinator's own edits, named in backticks on its ticked slice's line, are no drift; prose owns nothing.
    assert [line for line in lines if line.startswith("drift     ") and "changed since" in line] == [
        "drift     docs/stray.md: changed since the base, and no landed brief or ticked slice owns it"
    ]
    assert "drift     agent STRAY: on no slice of PLAN.md (its slice is 'S9')" in lines
    # A slice planned ahead, such as the review, is no drift while the session runs; `--end` names it.
    assert "planned   S3" in lines
    assert not any(line.startswith("drift") and "S3" in line for line in lines)
    assert "CI        no reading yet" in lines
    assert not any("slice S1" in line or "slice S2" in line for line in lines)
    assert "now       Next action:" in lines
    assert lines[-1] == "status: 3 running (2 suspect), 3 to judge, 1 to resume, 1 due, 3 unclosed, 2 drift"


def _pairs(project: Path, built: Path, added: str) -> list[str]:
    """The drift lines naming unticked slices that share files, with `added` planned after S3."""
    store.write_text(built / store.PLAN, PLAN.replace("- [ ] S3 tests/\n", f"- [ ] S3 tests/\n{added}"))
    lines = _lines(inprocess("status", cwd=project))
    return [line for line in lines if line.startswith("drift     ") and "share" in line]


def test_two_unticked_slices_sharing_files_with_no_after_edge_drift(project: Path, built: Path) -> None:
    """The plan and the estimate read them as parallel, while `brief --check` refuses the second as the first runs."""
    pair = "- [ ] S5 `docs/a.md`, `src/b.py`\n- [ ] S6 `notes/x.md`, `docs/`{}\n"
    said = (
        "drift     slices S5 and S6 share 1 file(s), `docs/` first, and neither is after the other: the second's"
        " brief is refused while the first runs, so name S5 in S6's `after:`"
    )
    assert _pairs(project, built, pair.format("")) == [said]
    assert _pairs(project, built, pair.format(" after: S5")) == []
    # An edge through a third slice orders them as well, a ticked one included.
    assert _pairs(project, built, pair.format(" after: S1") + "- [x] S1b `x/` after: S5\n") == [said]
    assert _pairs(project, built, pair.format(" after: S1b") + "- [x] S1b `x/` after: S5\n") == []
    # A ticked slice is done, so it shares nothing with what is still planned.
    assert _pairs(project, built, pair.format("").replace("- [ ] S5", "- [x] S5")) == []


def test_the_drift_names_five_pairs_of_slices_and_counts_the_rest(project: Path, built: Path) -> None:
    """A plan ordered by its sections alone, with no `after:` edges, would push every later line out."""
    wide = "".join(f"- [ ] W{k} `shared.md`\n" for k in range(4))
    lines = _pairs(project, built, wide)
    assert len(lines) == 6
    more = "1 more pair(s) of slices share files likewise: each is named here once those above are ordered"
    assert lines[-1] == f"drift     {more}"


def test_a_row_is_marked_for_a_re_plan_once_routed_to_three_agents_and_never_for_its_queues(
    project: Path, built: Path
) -> None:
    """The review routes a row to `ending`, to `ending` again with a correction, then to its fixer: no bounce."""
    write_config(project, {"stateRoot": (project.parent / "state").as_posix(), "queues": ["review"]})
    routes = {
        "REVIEW-F1": ("ending", "ending", "HELD"),
        # A configured queue is a queue as the built-in ones are.
        "REVIEW-F2": ("review", "owner", "handoff", "A", "HELD"),
        "REVIEW-F3": ("ending", "A", "B", "ending", "HELD"),
        # Three routes to one agent are no bounce: the agents are counted, not the events.
        "REVIEW-F4": ("HELD", "HELD", "HELD"),
    }
    # A fix that lands closes its row FIXED, so the next fixer receives it by a `reopen` event.
    bounced = [
        model.FindingEvent("REVIEW-F5", "route", "ROUTED", owner="A"),
        model.FindingEvent("REVIEW-F5", "close", "FIXED", owner="A", evidence="abc1234"),
        model.FindingEvent("REVIEW-F5", "reopen", "ROUTED", owner="B"),
        model.FindingEvent("REVIEW-F5", "close", "FIXED", owner="B", evidence="def5678"),
        model.FindingEvent("REVIEW-F5", "reopen", "ROUTED", owner="HELD"),
    ]
    model.write_findings(
        built,
        lambda _: [
            *[
                model.FindingEvent(row, "route", "ROUTED", owner=owner)
                for row, owners in routes.items()
                for owner in owners
            ],
            *bounced,
        ],
    )
    lines = _lines(inprocess("status", cwd=project))
    marked = "(routed to 3 agents: re-plan it)"
    held = f"DONE-F2 {marked}, DONE-F3, REVIEW-F1, REVIEW-F2, REVIEW-F3 {marked}, REVIEW-F4, REVIEW-F5 {marked}"
    assert f"unclosed  HELD: {held}" in lines


def test_a_running_record_the_harness_contradicts_is_suspect_with_its_reason(project: Path, built: Path) -> None:
    lines = _lines(inprocess("status", cwd=project))
    assert "suspect   QUIET (writer, round 1): running, but its transcript is silent 20 min mid-turn, past 15" in lines
    assert "suspect   ENDED (auditor, round 1): running, but its transcript's last message ends its turn" in lines
    write_agent(built, model.Agent("LOST", "writer", "running", ids=("a9",)))
    lines = _lines(inprocess("status", cwd=project))
    assert "suspect   LOST (writer, round 0): running, but no transcript found for a9" in lines


def test_now_md_past_its_bound_is_said(project: Path, built: Path) -> None:
    text = "# Now\n\nNext action: " + "x" * 3000 + "\n"
    store.write_text(built / store.NOW, text)
    lines = _lines(inprocess("status", cwd=project))
    assert f"NOW.md    {len(text):,} bytes, past 3,000: move the rest to PLAN.md or `orchestra note`" in lines
    assert any(line.startswith("now       Next action: xxx") and line.endswith("…") for line in lines)


def test_what_the_state_holds_of_where_the_session_stands_is_printed_from_it(project: Path, built: Path) -> None:
    """NOW.md keeps only the next action and why; a second copy of the rest would go stale."""
    assert not any(
        line.split()[0] in {"waiting", "estimate", "gate"} for line in _lines(inprocess("status", cwd=project))
    )
    later = (datetime.datetime.now().astimezone() + datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
    gate = "abc123def passed: exit 0, 4.2 min, args none"
    store.append_jsonl(
        built / store.LOG,
        {"time": later, "kind": "waiting", "text": "FRESH's report"},
        {"time": later, "kind": "gate", "text": gate},
    )
    saved = built / store.ESTIMATES / "first.md"
    saved.write_bytes(b"# Estimate\n")
    at = datetime.datetime.fromtimestamp(saved.stat().st_mtime).astimezone()
    lines = _lines(inprocess("status", cwd=project))
    assert f"waiting   FRESH's report, noted {later[11:16]}" in lines
    assert f"estimate  estimates/first.md, saved {at:%H:%M}" in lines
    assert f"gate      {gate}, at {later[11:16]}" in lines
    # A note older than an agent's latest change of status no longer says what the session waits on.
    soon = (datetime.datetime.now().astimezone() + datetime.timedelta(minutes=9)).isoformat(timespec="seconds")
    model.update_agent(built, "FRESH", lambda held: dataclasses.replace(held, since={**held.since, "reported": soon}))
    stale = "; an agent has changed status since: note what the session waits on now"
    assert f"waiting   FRESH's report, noted {later[11:16]}{stale}" in _lines(inprocess("status", cwd=project))


def test_status_exits_0_where_there_is_no_session_or_a_file_is_torn(project: Path) -> None:
    """A skill injects it, and an injected command that exits non-zero aborts the skill."""
    done = inprocess("status", "--measure", cwd=project)
    assert done.returncode == 0, done.stderr
    assert _lines(done)[0].endswith(" · a snapshot")
    assert _lines(done)[-1] == "status: no orchestra session on main: run `orchestra init <session>` on its branch"
    assert inprocess("init", "s1", cwd=project).returncode == 0
    (_state(project) / store.FINDINGS).write_bytes(b"torn\n")
    done = inprocess("status", cwd=project)
    assert done.returncode == 0, done.stderr
    assert any(line.startswith("unclosed  unknown: ") and "line 1 is no JSON object" in line for line in _lines(done))
    assert _lines(done)[-1].endswith(", unclosed unknown, 0 drift")
    # A torn log hides the spans an estimate starts from, and no other section.
    (_state(project) / store.LOG).write_bytes(b"torn\n")
    measured = _lines(inprocess("status", "--measure", cwd=project))
    assert any(line.startswith("spans     unknown: ") and "line 1 is no JSON object" in line for line in measured)
    assert measured[-1].startswith("measure: 0 finished, 0 running, ")


def _logged(state: Path, kind: str, text: str, minutes: float) -> None:
    """The log's line of `kind`, `minutes` ago, as the hook that writes that kind words it."""
    store.append_jsonl(state / store.LOG, {"time": _ago(minutes), "kind": kind, "text": text})


def _dispatch(state: Path, name: str, minutes: float) -> None:
    _logged(state, "dispatch", f"{name} as x1 on opus", minutes)


def test_measure_gives_the_figures_an_estimate_starts_from(project: Path, built: Path) -> None:
    for name, minutes in (("W1", 10), ("W2", 30), ("W3", 50)):
        since = {"running": _ago(minutes + 60), "reported": _ago(60)}
        _agent(built, model.Agent(name, "writer", "landed", reports={1: "r"}, since=since))
        _dispatch(built, name, minutes + 60)
        _logged(built, "report", f"{name} round 1: DONE", 60)
    _dispatch(built, "FRESH", 30)
    model.write_rulings(
        built,
        lambda _: [
            model.Ruling(1, "Keep the cap?", "s1"),
            model.Ruling(2, "Split the slice?", "s1", number=7, words="yes", date="2026-11-17"),
        ],
    )
    done = inprocess("status", "--measure", cwd=project)
    assert done.returncode == 0, done.stderr
    lines = _lines(done)
    assert lines[0].endswith(" · a snapshot")
    assert "writer    3 finished, median 30 min, longest 50 min, each agent's rounds summed" in lines
    assert "running   FRESH (writer): 30 min since its dispatch or resume, context 84,312 tokens" in lines
    # QUIET's dispatch has no line in the log, so its minutes are not guessed from its record.
    assert any(line.startswith("running   QUIET (writer): minutes unknown, context ") for line in lines)
    assert [line for line in lines if line.startswith("unticked")] == [
        "unticked  S2 docs/guide.md",
        "unticked  S3 tests/",
        "unticked  S2 before S3",
        "unticked  the ending's checklist",
    ]
    assert "due       #1 (when reported:DONE) judge DONE's report" in lines
    assert "asked     #1 Keep the cap?" in lines
    assert not any("Split the slice" in line for line in lines)
    assert lines[-1] == "measure: 3 finished, 3 running, 4 unticked, 1 due, 1 asked and not ruled"


def test_measure_sums_a_resumed_agents_rounds_and_counts_no_wait_between_them(project: Path, built: Path) -> None:
    """A resume stamps `running` again, so the record alone keeps the last round; a span from the first
    dispatch to the last report would count the 38 minutes the agent sat waiting on an answer."""
    since = {"running": _ago(1), "reported": _ago(0)}
    _agent(built, model.Agent("RESUMED", "auditor", "judged", reports={1: "r", 2: "r"}, since=since))
    _dispatch(built, "RESUMED", 40)
    _logged(built, "report", "RESUMED round 1: NEEDS-ANSWER", 39)
    _logged(built, "resume", "RESUMED round 2", 1)
    _logged(built, "report", "RESUMED round 2: DONE", 0)
    # A limit's stop ends a round no report ends, so the round before it counts and the wait after it does not.
    _agent(built, model.Agent("LIMITED", "driver", "reported", reports={1: "r"}))
    _dispatch(built, "LIMITED", 30)
    _logged(built, "stopped", "LIMITED: You've hit your limit", 26)
    _logged(built, "resume", "LIMITED round 1", 10)
    _logged(built, "report", "LIMITED round 1: DONE", 4)
    # The running agent's minutes count from its latest start: a resend after one that never landed restarts it.
    _logged(built, "resume", "FRESH round 2", 12)
    _logged(built, "resume", "FRESH round 2", 7)
    lines = _lines(inprocess("status", "--measure", cwd=project))
    assert "auditor   1 finished, median 2 min, longest 2 min, each agent's rounds summed" in lines
    assert "driver    1 finished, median 10 min, longest 10 min, each agent's rounds summed" in lines
    assert any(line.startswith("running   FRESH (writer): 7 min since its dispatch or resume,") for line in lines)


def test_measure_lists_what_waits_and_the_context_of_each_agent_holding_rows(project: Path, built: Path) -> None:
    """A slice no agent took reads free unless a running writer's brief owns one of its files."""
    plan = PLAN.replace("- [ ] S3 tests/\n", "- [ ] S3 tests/\n- [ ] S7 `docs/`\n")
    store.write_text(built / store.PLAN, plan)
    lines = _lines(inprocess("status", "--measure", cwd=project))
    held = "1 of its files is in the `Owns:` of {}, a writer running for 30 min: `docs/`"
    assert [line for line in lines if line.startswith("waits")] == [
        "waits     slice S3, which no agent took: its files are free",
        f"waits     slice S7, which no agent took: {held.format('FRESH')}; {held.format('QUIET')}",
        "waits     #2 (when landed:FRESH) push once FRESH lands: waits on FRESH, running for 30 min",
    ]
    assert [line for line in lines if line.startswith("holds")] == [
        "holds     HELD (driver, judged): 2 unclosed row(s), context unknown"
    ]


def test_a_slice_after_another_waits_until_that_one_is_ticked(project: Path, built: Path) -> None:
    """A slice needing another's contract, not its files, would otherwise read as free."""
    added = "- [ ] S7 `notes/b.md` after: S2\n- [ ] S8 `notes/c.md` after: S1\n- [ ] S10 `notes/d.md` after: S99\n"
    store.write_text(built / store.PLAN, PLAN.replace("- [ ] S3 tests/\n", f"- [ ] S3 tests/\n{added}"))
    lines = [line for line in _lines(inprocess("status", "--measure", cwd=project)) if line.startswith("waits")]
    assert lines[:4] == [
        "waits     slice S3, which no agent took: its files are free",
        "waits     slice S7, which no agent took: it is after S2, which is unticked",
        "waits     slice S8, which no agent took: its files are free",
        "waits     slice S10, which no agent took: it is after S99, which PLAN.md does not list",
    ]


def test_a_wide_slice_names_each_holding_writer_once_with_a_count(project: Path, built: Path) -> None:
    """A clause a path ran a judge and a land to kilobytes a line; `--measure` alone lists the paths."""
    _brief(built, "FRESH", "writer", "S2", "docs/**")
    files = [f"docs/part{k}.md" for k in range(30)]
    wide = "- [ ] S7 " + ", ".join(f"`{path}`" for path in files) + "\n"
    store.write_text(built / store.PLAN, PLAN.replace("- [ ] S3 tests/\n", f"- [ ] S3 tests/\n{wide}"))
    said = "waits     slice S7, which no agent took: 30 of its files are in the `Owns:` of FRESH, a writer running for "
    said += "30 min"
    assert said in status.paced(built)
    measured = _lines(inprocess("status", "--measure", cwd=project))
    assert said + ": " + ", ".join(f"`{path}`" for path in files) in measured


def test_a_verb_closes_on_five_waits_lines_and_a_count_of_the_rest(built: Path) -> None:
    model.write_queue(built, lambda _: [model.QueueItem(k, "ci", f"item {k}") for k in range(3, 9)])
    lines = status.paced(built)
    assert len(lines) == 6
    assert lines[-1] == "waits     3 more: `orchestra status --measure` lists every one"
    store.write_text(built / store.PLAN, "# Plan\n")
    model.write_queue(built, lambda items: [dataclasses.replace(item, done=True) for item in items.values()])
    assert status.paced(built) == []


def test_end_holds_each_machine_checkable_line_or_names_what_breaks_it(project: Path, built: Path) -> None:
    git(project, "switch", "-c", "worktree-agent-h1")
    (project / "held.txt").write_bytes(b"x\n")
    git(project, "add", "held.txt")
    git(project, "commit", "-m", "held")
    git(project, "switch", "main")
    # The record holds no branch, and the harness's file beside the transcript names it, as `land` reads it.
    model.update_agent(built, "HELD", lambda held: dataclasses.replace(held, ids=("h1",)))
    transcript(
        project.parent / "claude-config", "h1", line("assistant", "m1"), meta={"worktreeBranch": "worktree-agent-h1"}
    )
    git(project, "remote", "add", "origin", "https://example.invalid/project.git")
    done = inprocess("status", "--end", cwd=project)
    assert done.returncode == 0, done.stderr
    lines = _lines(done)
    assert "NOT held  no unclosed finding: DONE-F1, DONE-F2, DONE-F3" in lines
    assert "NOT held  nothing running: FRESH" in lines
    # A report waiting, or a round stopped unfinished, leaves nothing else behind for the end to see.
    waiting = "ASKS (reported), BLOCK (reported), DONE (reported), HALT (stopped)"
    assert f"NOT held  every report judged: {waiting}" in lines
    # The writers past their brief whose branch nothing names may hold commits too.
    unknown = ", ".join(f"{name} (branch unknown)" for name in ("BLOCK", "DONE", "FRESH", "HALT"))
    named = f"{unknown}, HELD (worktree-agent-h1, 1 commits), QUIET (branch unknown)"
    discard = "`orchestra prune --discard NAME` settles it once you have saved what it holds for each branch unknown"
    assert f"NOT held  no unlanded branch: {named}; {discard}" in lines
    assert "to land   HELD (driver, round 1, DONE)" in _lines(inprocess("status", cwd=project))
    assert "NOT held  no due queue item: #1" in lines
    # At the end a slice no agent took is drift: planned and never done, or done and never ticked.
    untaken = "S3: no agent took it; dispatch it, tick it if you did it, or remove its line"
    assert f"NOT held  no planned slice untaken: {untaken}" in lines
    assert "held      no frozen tree left" in lines
    assert lines[-1] == "end: 2 of 10 lines held"


def test_an_override_waiting_is_listed_and_holds_the_end_open_until_dropped(project: Path, built: Path) -> None:
    """One left armed lets its caller's next refused hook call through unseen, with an unrelated reason."""
    assert "held      no override waiting" in _lines(inprocess("status", "--end", cwd=project))
    override.consume(built, "the dispatch of B (its brief is not checked)")
    assert inprocess("override", "the stamp is stale", cwd=project).returncode == 0
    held = override.waiting(built)
    assert held is not None
    since = str(held["time"])[:16].replace("T", " ")
    said = f"for the coordinator since {since}: the stamp is stale; `orchestra override --drop` removes it"
    assert f"override  {said}" in _lines(inprocess("status", cwd=project))
    assert f"NOT held  no override waiting: {said}" in _lines(inprocess("status", "--end", cwd=project))
    dropped = inprocess("override", "--drop", cwd=project)
    assert dropped.stdout.splitlines() == ["dropped the override waiting for the coordinator: the stamp is stale"]
    assert not any(line.startswith("override") for line in _lines(inprocess("status", cwd=project)))
    assert "held      no override waiting" in _lines(inprocess("status", "--end", cwd=project))
    # The refusal it would have served still stands, so it may be armed again.
    assert inprocess("override", "the stamp is stale", cwd=project).returncode == 0


def _readings(state: Path, *held: ci.Reading) -> None:
    """CI's state holding these readings, oldest first, as `orchestra ci` keeps them."""
    store.write_json(state / store.CI, {"expected": [], "readings": [model.to_record(one) for one in held]})


def test_ci_is_read_against_the_session_branchs_head(project: Path) -> None:
    """An `expected` reading failed only on rows a later landing clears, and at the end every landing is in."""
    assert inprocess("init", "s1", cwd=project).returncode == 0
    state = _state(project)
    head = git(project, "rev-parse", "HEAD")
    # With no remote nothing is pushed, so no CI can read the head.
    assert "held      CI green at the head: no remote, so no CI" in _lines(inprocess("status", "--end", cwd=project))
    git(project, "remote", "add", "origin", "https://example.invalid/project.git")
    older = [ci.Reading(f"{k}" * 40, "failure", 9.0, f"2026-11-17T1{k}:00:00+01:00") for k in range(3)]
    _readings(state, *older, ci.Reading(head, "expected", 4.4, "2026-11-17T14:00:00+01:00"))
    assert f"CI        {head[:9]} expected, 4 min, read 2026-11-17T14:00:00+01:00" in _lines(
        inprocess("status", cwd=project)
    )
    measured = [line for line in _lines(inprocess("status", "--measure", cwd=project)) if line.startswith("CI")]
    assert measured == [
        f"CI        {head[:9]} expected, 4 min, read 2026-11-17T14:00:00+01:00",
        "CI        222222222 failure, 9 min, read 2026-11-17T12:00:00+01:00",
        "CI        111111111 failure, 9 min, read 2026-11-17T11:00:00+01:00",
    ]
    unclear = "an expected red did not clear, though every landing meant to clear it is in"
    expected = f"NOT held  CI green at the head: {head[:9]} expected, 4 min, read 2026-11-17T14:00:00+01:00: {unclear}"
    assert expected in _lines(inprocess("status", "--end", cwd=project))
    _readings(state, ci.Reading(head, "success", 4.0, "2026-11-17T14:30:00+01:00"))
    assert "held      CI green at the head" in _lines(inprocess("status", "--end", cwd=project))
    _readings(state, ci.Reading(head, "failure", None, "2026-11-17T15:00:00+01:00"))
    ended = _lines(inprocess("status", "--end", cwd=project))
    assert f"NOT held  CI green at the head: {head[:9]} failure, read 2026-11-17T15:00:00+01:00" in ended
    _readings(state, *older)
    assert f"NOT held  CI green at the head: the head {head[:9]} has no reading" in _lines(
        inprocess("status", "--end", cwd=project)
    )
    assert (
        f"CI        222222222 failure, 9 min, read 2026-11-17T12:00:00+01:00; the head {head[:9]} has no reading"
        in (_lines(inprocess("status", cwd=project)))
    )


# --- the usage limits -----------------------------------------------------------------------------


def _usage(project: Path) -> list[str]:
    done = inprocess("status", "--measure", cwd=project)
    assert done.returncode == 0, done.stderr
    assert "someone@example.invalid" not in done.stdout + done.stderr
    return [line for line in _lines(done) if line.startswith("usage")]


def _local(stamp: str) -> str:
    return f"{datetime.datetime.fromisoformat(stamp).astimezone():%Y-%m-%d %H:%M}"


def test_measure_reads_each_usage_row_from_a_headless_usage_run_and_keeps_it_five_minutes(
    project: Path, claude_on_path: Path
) -> None:
    assert inprocess("init", "s1", cwd=project).returncode == 0
    lines = _usage(project)
    program = shutil.which("claude")
    assert lines[0].startswith("usage     3 row(s) as the server sent them, read ")
    assert lines[0].endswith(f" by {program} (found on PATH)")
    assert lines[1:] == [
        f"usage     session: 12% used, resets {_local('2026-11-18T03:00:00Z')}",
        f"usage     weekly_all: 61% used, resets {_local('2026-11-20T08:00:00Z')}",
        f"usage     weekly_scoped (Opus): 87% used, resets {_local('2026-11-20T08:00:00Z')}, warning",
    ]
    calls = (claude_on_path / "calls.jsonl").read_bytes().splitlines()
    # The flags as the CLI reference names them, never the code's own list, which a flag the CLI refuses passes.
    run = ["-p", "/usage", "--safe-mode", "--no-session-persistence", "--output-format", "stream-json", "--verbose"]
    assert [json.loads(call) for call in calls] == [run]
    # First after the title, so a cut at forty lines never drops the figure a wide wave is weighed against.
    measured = _lines(inprocess("status", "--measure", cwd=project))
    assert measured[2].startswith("usage     ")
    # A second reading within five minutes is the first, said to be cached, with no second run.
    again = _usage(project)
    assert again[0].startswith("usage     3 row(s) as the server sent them, cached ")
    assert again[1:] == lines[1:]
    assert len((claude_on_path / "calls.jsonl").read_bytes().splitlines()) == 1
    # The reading expires at five minutes: short of it, by a margin a loaded run keeps, it is cached; at it, read anew.
    cache = store.state_dir(project) / status.USAGE
    for age, how, runs in (
        (datetime.timedelta(minutes=4, seconds=30), "cached", 1),
        (datetime.timedelta(minutes=5), "read", 2),
    ):
        held = store.read_json(cache)
        held["time"] = (datetime.datetime.now().astimezone() - age).isoformat(timespec="seconds")
        store.replace_bytes(cache, json.dumps(held).encode())
        assert _usage(project)[0].startswith(f"usage     3 row(s) as the server sent them, {how} ")
        assert len((claude_on_path / "calls.jsonl").read_bytes().splitlines()) == runs


@pytest.mark.parametrize(
    ("answer", "code", "said"),
    [
        (
            usage_answer(None),
            None,
            "Claude Code could not get the plan's usage: the request was refused or rate limited",
        ),
        (b'{"type": "result", "is_error": false}\n', None, "no usage report: it needs a claude.ai login with a plan"),
        (usage_answer(), b"1", "claude exited 1: run `claude -p /usage` to read why"),
    ],
    ids=["rate limited", "no report", "failed"],
)
def test_measure_says_why_a_usage_run_gave_no_rows(
    project: Path, claude_on_path: Path, answer: bytes, code: bytes | None, said: str
) -> None:
    assert inprocess("init", "s1", cwd=project).returncode == 0
    (claude_on_path / "answer.jsonl").write_bytes(answer)
    if code is not None:
        (claude_on_path / "code").write_bytes(code)
    (line,) = _usage(project)
    assert line.startswith(f"usage     none: {said}")


def test_measure_runs_the_configured_claude_only_where_none_is_on_path(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_on_path: Path
) -> None:
    assert inprocess("init", "s1", cwd=project).returncode == 0
    # git alone stays on PATH; the program is found nowhere else.
    monkeypatch.setenv("PATH", str(Path(str(shutil.which("git"))).parent))
    named = "usage     unknown: `claude` is not on PATH, and .claude/orchestra.json names no `claude` program"
    assert _usage(project)[0].startswith(named)
    program = claude_stub(tmp_path / "bundled")
    settings: dict[str, object] = {"stateRoot": (tmp_path / "state").as_posix(), "claude": str(program)}
    write_config(project, settings)
    lines = _usage(project)
    assert lines[0].endswith(f" by {program} (configured in .claude/orchestra.json)")
    assert len(lines) == 4
    # Missing, as after an update of the desktop app moved it, it is named loudly with the repair.
    (project.parent / "state" / "project" / "s1" / status.USAGE).unlink()
    gone = tmp_path / "gone" / program.name
    write_config(project, {**settings, "claude": str(gone)})
    (line,) = _usage(project)
    assert line.startswith(
        f"usage     unknown: NOT READ: the `claude` .claude/orchestra.json names, {gone}, is missing."
    )
    assert line.endswith("give `claude` the program's new path")
    # Once one is on PATH, as an installer puts it, the configured program is not read at all.
    monkeypatch.setenv("PATH", f"{claude_on_path}{os.pathsep}{os.environ['PATH']}")
    assert _usage(project)[0].endswith(f" by {shutil.which('claude')} (found on PATH)")


def test_measure_keeps_no_word_of_a_failed_usage_runs_errors(project: Path, claude_on_path: Path) -> None:
    """Its error text may name the account, so only the exit code is kept, printed or saved."""
    assert inprocess("init", "s1", cwd=project).returncode == 0
    (claude_on_path / "stderr").write_bytes(b"Not signed in as someone@example.invalid\n")
    (claude_on_path / "code").write_bytes(b"1")
    (line,) = _usage(project)
    assert line.startswith("usage     none: claude exited 1: run `claude -p /usage` to read why")
    assert b"someone@example.invalid" not in (store.state_dir(project) / status.USAGE).read_bytes()
