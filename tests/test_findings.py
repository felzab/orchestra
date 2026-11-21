"""The findings verbs: every finding of a report gets one owner or a closing, and no refused call writes a line."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import inprocess, line, transcript, write_agent
from orchestra import context, log, model, report, store

STORE = "lib/store.py :: save"
CLI = "lib/cli.py :: emit"
# REVIEW-F2's evidence once VERIFY corrects it: the heading changed, so it leads the proof line.
CORRECTION = "corrected by VERIFY: F2 · optional · loud · lib/cli.py :: main; The flaw is at `main`."


@dataclass(frozen=True)
class Written:
    """A valid report's parts: one finding at each anchor, or for a verifier each verdict with its proof."""

    kind: model.AgentType
    anchors: tuple[str, ...] = ()
    verdicts: tuple[tuple[str, str, str], ...] = ()
    said: str = "DONE"
    round_: int = 1

    def text(self, name: str) -> str:
        if self.kind == "verifier":
            items = [f"{row} · {word}" for row, word, _ in self.verdicts]
            bodies = [f"### {row} · {word}\n{proof}\n" for row, word, proof in self.verdicts]
            section = ["## Verdicts", *(bodies or ["none"])]
        else:
            items = [f"F{k} · defect · silent · {anchor}" for k, anchor in enumerate(self.anchors, 1)]
            bodies = [
                f"### {heading}\nHarm: a torn file. Likelihood: rare.\nThe write at F{k}.\n"
                for k, heading in enumerate(items, 1)
            ]
            section = ["## Findings", *(bodies or ["none"])]
        commits = "Commits: 1, tip abc1234" if self.kind in {"writer", "driver"} else "Commits: none"
        head = [f"# {name} · {self.kind} · round {self.round_}", "## Summary", self.said, "Read the store.", commits]
        tail = ["## Not verified", "nothing", "## Wrong in the brief", "nothing", "## Outside the scope", "nothing", ""]
        closing = ["Not verified: nothing", "Wrong in the brief: nothing", "## Done", "read lib/store.py"]
        return "\n".join([*head, *items, *closing, *section, *tail])


@pytest.fixture
def state(project: Path) -> Path:
    done = inprocess("init", "s", cwd=project)
    assert done.returncode in {0, 1}, done.stderr
    return store.state_dir(project)


def agent(state: Path, name: str, kind: model.AgentType | Written, status: model.AgentStatus = "reported") -> None:
    """An agent's record, and where its report's parts are given, the report in its round's file."""
    written = kind if isinstance(kind, Written) else Written(kind)
    round_ = written.round_
    reports = {round_: str(report.path(state, name, round_))} if round_ > 1 else {}
    write_agent(state, model.Agent(name, written.kind, status, reports=reports))
    if isinstance(kind, Written):
        path = report.path(state, name, round_)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(written.text(name).encode("utf-8"))


def rows(state: Path) -> dict[str, model.FindingEvent]:
    return model.read_findings(state)


def lines(state: Path) -> bytes:
    path = state / store.FINDINGS
    return path.read_bytes() if path.exists() else b""


def test_judge_banks_every_finding_with_its_disposition_and_marks_the_agent_judged(project: Path, state: Path) -> None:
    agent(state, "FIXER", "writer", "briefed")
    agent(state, "REVIEW", Written("auditor", (STORE, CLI, "README :: install")))
    done = inprocess(
        "judge", "REVIEW", "F1=route:FIXER", "F2=close:NOT A DEFECT:the guard at line 12 holds", "F3=route:owner",
        "--verdict", "two real findings", cwd=project,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "ROUTED REVIEW-F1 to FIXER",
        "NOT A DEFECT REVIEW-F2 (the guard at line 12 holds)",
        "ROUTED REVIEW-F3 to owner",
        "judged REVIEW: 2 routed, 1 closed",
    ]
    held = rows(state)
    assert [(row, event.status, event.owner) for row, event in held.items()] == [
        ("REVIEW-F1", "ROUTED", "FIXER"),
        ("REVIEW-F2", "NOT A DEFECT", None),
        ("REVIEW-F3", "ROUTED", "owner"),
    ]
    # A bank line and a disposition line for each finding, all written by the one call.
    assert len(lines(state).splitlines()) == 6
    record = model.read_agent(state, "REVIEW")
    assert (record.status, record.verdict) == ("judged", "two real findings")
    assert log.entries(state)[-1].text == "REVIEW: 2 routed, 1 closed; two real findings"


def test_judging_a_report_twice_refuses_and_a_judge_cut_short_completes(project: Path, state: Path) -> None:
    agent(state, "REVIEW", Written("auditor", (STORE,)))
    assert inprocess("judge", "REVIEW", "F1=route:owner", "--verdict", "v", cwd=project).returncode == 0
    before = lines(state)
    again = inprocess("judge", "REVIEW", "F1=route:owner", "--verdict", "v", cwd=project)
    assert again.returncode == 2
    assert "REVIEW's report is judged already (judged)" in again.stderr
    # A judge that banked the rows and stopped before it marked the agent: the retry marks it, banking nothing.
    model.update_agent(state, "REVIEW", lambda held: model.Agent(held.name, held.type, "reported"))
    retried = inprocess("judge", "REVIEW", "F1=route:owner", "--verdict", "v", cwd=project)
    assert retried.returncode == 0, retried.stderr
    assert "its 1 row(s) were banked by an earlier judge" in retried.stdout
    assert lines(state) == before
    assert model.read_agent(state, "REVIEW").status == "judged"


@pytest.mark.parametrize(
    ("record", "args", "said"),
    [
        (("reported", "DONE"), ("F1=route:owner",), "every finding takes a disposition, and F2 has none"),
        (
            ("reported", "DONE"),
            ("F1=route:owner", "F2=route:owner", "F3=route:owner"),
            "F3 names no finding of REVIEW's",
        ),
        (("reported", "DONE"), ("F1=route:owner", "F2=fix"), "`fix` is no disposition"),
        (("reported", "DONE"), ("F1=route:owner", "F2=close:DONE:it is"), "`close:DONE:it is` is no disposition"),
        (("reported", "DONE"), ("F1=route:owner", "F2=close:MOOT: "), "`close:MOOT: ` is no disposition"),
        (("reported", "DONE"), ("F1=route:owner", "F2=route:NOBODY"), "NOBODY is no owner"),
        (("reported", "DONE"), ("F1=route:owner", "F1=route:owner"), "F1 is given two dispositions"),
        (
            ("running", "DONE"),
            ("F1=route:owner", "F2=route:owner"),
            "REVIEW is running, so its report may be unfinished",
        ),
        (("reported", "BLOCKED: no access"), ("F1=route:owner", "F2=route:owner"), "stopped BLOCKED: no"),
    ],
    ids=[
        "a finding with no disposition",
        "a disposition for no finding",
        "a disposition of no shape",
        "a close of no closing status",
        "a close on no evidence",
        "an owner that is neither agent nor queue",
        "one finding given two",
        "an agent still running",
        "a report that stopped on what blocks it",
    ],
)
def test_a_judgement_it_cannot_make_writes_nothing(
    project: Path, state: Path, record: tuple[model.AgentStatus, str], args: tuple[str, ...], said: str
) -> None:
    status, opening = record
    agent(state, "REVIEW", Written("auditor", (STORE, CLI), said=opening), status)
    done = inprocess("judge", "REVIEW", *args, "--verdict", "v", cwd=project)
    assert done.returncode == 2
    assert said in done.stderr, done.stderr
    assert lines(state) == b""
    assert model.read_agent(state, "REVIEW").status == status


def test_a_later_round_is_read_from_its_own_file_and_banks_its_own_rows(project: Path, state: Path) -> None:
    agent(state, "FE", Written("auditor", (STORE,)))
    assert inprocess("judge", "FE", "F1=route:owner", "--verdict", "v", cwd=project).returncode == 0
    agent(state, "FE", Written("auditor", (CLI,), round_=2))
    done = inprocess("judge", "FE", "F1=route:owner", "--verdict", "round two", cwd=project)
    assert done.returncode == 0, done.stderr
    assert list(rows(state)) == ["FE-F1", "FE.2-F1"]
    shown = inprocess("show", "FE-F1", "FE.2-F1", cwd=project)
    assert shown.returncode == 0, shown.stderr
    assert f"### F1 · defect · silent · {STORE}" in shown.stdout
    assert f"### F1 · defect · silent · {CLI}" in shown.stdout


def _review_given_to_a_verifier(project: Path, state: Path) -> None:
    agent(state, "VERIFY", "verifier", "briefed")
    agent(state, "FIXER", "writer", "briefed")
    agent(state, "REVIEW", Written("auditor", (STORE, CLI, "README :: install")))
    # A row a fixer would take waits on a queue while it is verified, as the review page has it.
    for_verify = ("F1=route:VERIFY", "F2=route:ending", "F3=route:VERIFY")
    assert inprocess("judge", "REVIEW", *for_verify, "--verdict", "v", cwd=project).returncode == 0
    verdicts = (
        ("REVIEW-F1", "REFUTED", "`git show base:lib/store.py` holds the guard at line 12."),
        ("REVIEW-F2", "CORRECTED", "F2 · optional · loud · lib/cli.py :: main\nThe flaw is at `main`."),
        ("REVIEW-F3", "CONFIRMED", "`uv run pytest` exit 1 with the guard removed."),
    )
    agent(state, "VERIFY", Written("verifier", verdicts=verdicts))


def test_a_verifiers_report_closes_a_refuted_row_and_routes_a_corrected_one_as_corrected(
    project: Path, state: Path
) -> None:
    _review_given_to_a_verifier(project, state)
    done = inprocess(
        "judge", "VERIFY", "REVIEW-F2=route:ending", "REVIEW-F3=route:FIXER", "--verdict", "one refuted", cwd=project
    )
    assert done.returncode == 0, done.stderr
    held = rows(state)
    assert (held["REVIEW-F1"].status, held["REVIEW-F1"].evidence) == (
        "NOT A DEFECT",
        "refuted by VERIFY: `git show base:lib/store.py` holds the guard at line 12.",
    )
    # Routed to the queue it waits on already, the correction still reaches the row.
    assert (held["REVIEW-F2"].owner, held["REVIEW-F2"].evidence) == (
        "ending",
        CORRECTION,
    )
    assert (held["REVIEW-F3"].owner, held["REVIEW-F3"].evidence) == ("FIXER", None)
    # A judge cut short before it marked the verifier: the retry moves nothing a second time.
    before = lines(state)
    model.update_agent(state, "VERIFY", lambda held: model.Agent(held.name, held.type, "reported"))
    given = ("REVIEW-F2=route:ending", "REVIEW-F3=route:FIXER")
    retried = inprocess("judge", "VERIFY", *given, "--verdict", "one refuted", cwd=project)
    assert retried.returncode == 0, retried.stderr
    assert lines(state) == before


def test_a_corrected_row_keeps_its_correction_when_routed_on_to_its_fixer(project: Path, state: Path) -> None:
    """The fixer's dispatch routes the row again, and a row's state is its last line."""
    _review_given_to_a_verifier(project, state)
    given = ("REVIEW-F2=route:ending", "REVIEW-F3=route:owner")
    assert inprocess("judge", "VERIFY", *given, "--verdict", "v", cwd=project).returncode == 0
    done = inprocess("route", "FIXER", "REVIEW-F2", cwd=project)
    assert done.returncode == 0, done.stderr
    held = rows(state)["REVIEW-F2"]
    assert (held.owner, held.evidence) == ("FIXER", CORRECTION)


def test_a_corrected_row_keeps_its_correction_when_a_later_verifier_confirms_and_routes_it(
    project: Path, state: Path
) -> None:
    """A second verifier round on one row confirms the corrected finding, which its fixer still needs."""
    _review_given_to_a_verifier(project, state)
    given = ("REVIEW-F2=route:ending", "REVIEW-F3=route:owner")
    assert inprocess("judge", "VERIFY", *given, "--verdict", "v", cwd=project).returncode == 0
    agent(state, "AGAIN", "verifier", "briefed")
    agent(state, "AGAIN", Written("verifier", verdicts=(("REVIEW-F2", "CONFIRMED", "`pytest` exit 1."),)))
    done = inprocess("judge", "AGAIN", "REVIEW-F2=route:FIXER", "--verdict", "v", cwd=project)
    assert done.returncode == 0, done.stderr
    held = rows(state)["REVIEW-F2"]
    assert (held.owner, held.evidence) == ("FIXER", CORRECTION)


def test_a_verdict_records_its_proof_line_and_a_corrected_heading_only_where_it_changed(
    project: Path, state: Path
) -> None:
    """A CORRECTED body opens with the corrected heading, often the banked one unchanged, which proves nothing."""
    agent(state, "REVIEW", Written("auditor", (STORE, CLI)))
    waits = ("F1=route:ending", "F2=route:ending")
    assert inprocess("judge", "REVIEW", *waits, "--verdict", "v", cwd=project).returncode == 0
    verdicts = (
        ("REVIEW-F1", "CORRECTED", f"F1 · defect · silent · {STORE}\n\nThe guard is missing at `save`, line 12."),
        ("REVIEW-F2", "REFUTED", f"F2 · defect · silent · {CLI}\n`emit` flushes at line 30."),
    )
    agent(state, "VERIFY", Written("verifier", verdicts=verdicts))
    done = inprocess("judge", "VERIFY", "REVIEW-F1=route:ending", "--verdict", "v", cwd=project)
    assert done.returncode == 0, done.stderr
    held = rows(state)
    assert held["REVIEW-F1"].evidence == "corrected by VERIFY: The guard is missing at `save`, line 12."
    assert held["REVIEW-F2"].evidence == "refuted by VERIFY: `emit` flushes at line 30."


def test_a_verifiers_judge_reads_only_its_own_report_and_those_its_verdicts_name(
    project: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verifier banks no finding, so no other banked row's report is read for a repeat it cannot make."""
    _review_given_to_a_verifier(project, state)
    agent(state, "OTHER", Written("auditor", (STORE,)))
    model.write_findings(
        state,
        lambda _: [
            model.FindingEvent("OTHER-F1", "bank", "OPEN"),
            model.FindingEvent("OTHER-F1", "route", "ROUTED", "owner"),
        ],
    )
    read: list[Path] = []
    real = report.read

    def counted(path: Path, agent_type: str | None = None) -> report.Report:
        read.append(path)
        return real(path, agent_type)

    monkeypatch.setattr(report, "read", counted)
    given = ("REVIEW-F2=route:ending", "REVIEW-F3=route:FIXER")
    done = inprocess("judge", "VERIFY", *given, "--verdict", "v", cwd=project)
    assert done.returncode == 0, done.stderr
    assert sorted({path.stem for path in read}) == ["REVIEW", "VERIFY"]


def test_a_verifiers_route_leaves_a_row_with_the_live_agent_holding_it(project: Path, state: Path) -> None:
    """A row stays with its agent until that agent reports, so a verdict cannot hand it to a second owner."""
    agent(state, "FIXER", "writer", "briefed")
    agent(state, "REVIEW", Written("auditor", (STORE,)))
    assert inprocess("judge", "REVIEW", "F1=route:FIXER", "--verdict", "v", cwd=project).returncode == 0
    proof = f"F1 · defect · silent · {STORE}\nThe guard is missing at `load` too."
    agent(state, "VERIFY", Written("verifier", verdicts=(("REVIEW-F1", "CORRECTED", proof),)))
    before = lines(state)
    done = inprocess("judge", "VERIFY", "REVIEW-F1=route:owner", "--verdict", "v", cwd=project)
    assert done.returncode == 2
    assert "REVIEW-F1 is routed to FIXER, which is briefed: its rows stay with it until it reports" in done.stderr
    assert lines(state) == before


def test_a_verifiers_confirmed_row_takes_a_disposition(project: Path, state: Path) -> None:
    _review_given_to_a_verifier(project, state)
    before = lines(state)
    done = inprocess("judge", "VERIFY", "REVIEW-F2=route:FIXER", "--verdict", "v", cwd=project)
    assert done.returncode == 2
    assert "every verdict but REFUTED takes a disposition, and REVIEW-F3 has none" in done.stderr
    assert lines(state) == before


def test_a_confirmed_verdict_on_a_fixed_row_needs_no_disposition_and_one_on_a_refuted_row_does(
    project: Path, state: Path
) -> None:
    """Verifying fixed rows confirms what closed them; a confirmation of a row closed as no defect contradicts it."""
    agent(state, "FIXER", "writer", "briefed")
    agent(state, "REVIEW", Written("auditor", (STORE, CLI)))
    closes = ("F1=close:FIXED:abc1234", "F2=close:NOT A DEFECT:the guard holds")
    assert inprocess("judge", "REVIEW", *closes, "--verdict", "v", cwd=project).returncode == 0
    verdicts = (
        ("REVIEW-F1", "CONFIRMED", "`uv run pytest` exit 1 with abc1234 reverted."),
        ("REVIEW-F2", "CONFIRMED", "`uv run pytest` exit 1 with the guard removed."),
    )
    agent(state, "VERIFY", Written("verifier", verdicts=verdicts))
    before = lines(state)
    refused = inprocess("judge", "VERIFY", "--verdict", "v", cwd=project)
    assert refused.returncode == 2
    assert "every verdict but REFUTED takes a disposition, and REVIEW-F2 has none" in refused.stderr
    done = inprocess("judge", "VERIFY", "REVIEW-F2=route:FIXER", "--verdict", "v", cwd=project)
    assert done.returncode == 0, done.stderr
    assert "REVIEW-F1 is FIXED already, which its CONFIRMED verdict agrees with: it moves nothing" in done.stdout
    held = rows(state)
    assert (held["REVIEW-F1"].status, held["REVIEW-F1"].evidence) == ("FIXED", "abc1234")
    assert (held["REVIEW-F2"].kind, held["REVIEW-F2"].owner) == ("reopen", "FIXER")
    # The one line written is REVIEW-F2's reopening.
    assert len(lines(state).splitlines()) == len(before.splitlines()) + 1
    # Only a confirmation agrees with a fix: a correction of the fixed row takes a disposition, or it would be lost.
    correction = f"F1 · defect · silent · {STORE}\nThe fix guards `save` but not `load`."
    agent(state, "AGAIN", Written("verifier", verdicts=(("REVIEW-F1", "CORRECTED", correction),)))
    after = lines(state)
    corrected = inprocess("judge", "AGAIN", "--verdict", "v", cwd=project)
    assert corrected.returncode == 2
    assert "every verdict but REFUTED takes a disposition, and REVIEW-F1 has none" in corrected.stderr
    assert lines(state) == after


def test_a_verifiers_verdict_on_a_row_never_banked_moves_nothing_and_takes_no_disposition(
    project: Path, state: Path
) -> None:
    """A verifier sent a report's findings before that report was judged would otherwise stay unjudged for good."""
    agent(state, "AUDIT", Written("auditor", (STORE, CLI)))
    verdicts = (
        ("AUDIT-F1", "CONFIRMED", "`uv run pytest` exit 1 with the guard removed."),
        ("AUDIT-F2", "REFUTED", "`git show base:lib/cli.py` holds the guard."),
    )
    agent(state, "VERIFY", Written("verifier", verdicts=verdicts))
    given = inprocess("judge", "VERIFY", "AUDIT-F1=route:owner", "--verdict", "v", cwd=project)
    assert given.returncode == 2
    assert "AUDIT-F1 is no banked row, so its verdict moves nothing: give it no disposition" in given.stderr
    done = inprocess("judge", "VERIFY", "--verdict", "the audit was not judged first", cwd=project)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[:2] == [
        "AUDIT-F1 is no banked row, so its verdict moves nothing",
        "AUDIT-F2 is no banked row, so its verdict moves nothing",
    ]
    assert lines(state) == b""
    assert model.read_agent(state, "VERIFY").status == "judged"


def test_judge_names_a_finding_at_the_anchor_of_another_reports_row_and_how_it_was_closed(
    project: Path, state: Path
) -> None:
    agent(state, "REVIEW", Written("auditor", (STORE, CLI, "README :: install")))
    given = ("F1=route:owner", "F2=close:MOOT:the module is gone", "F3=route:owner")
    assert inprocess("judge", "REVIEW", *given, "--verdict", "v", cwd=project).returncode == 0
    agent(state, "LENS", Written("auditor", (f"`{STORE}`", CLI, "docs :: intro")))
    routed = ("F1=route:owner", "F2=route:owner", "F3=route:owner")
    done = inprocess("judge", "LENS", *routed, "--verdict", "v", cwd=project)
    assert done.returncode == 0, done.stderr
    assert f"LENS-F1 may repeat REVIEW-F1 (ROUTED to owner): both at `{STORE}`" in done.stdout
    # A closed row is named with its close, so the second judgement sees the first.
    assert f"LENS-F2 may repeat REVIEW-F2 (MOOT: the module is gone): both at `{CLI}`" in done.stdout
    assert "LENS-F3 may repeat" not in done.stdout


def test_a_repeat_is_named_from_the_bank_line_without_the_banked_rows_report(project: Path, state: Path) -> None:
    """The bank line carries the anchor, so a session's earlier reports are not read again at every judge."""
    agent(state, "REVIEW", Written("auditor", (STORE,)))
    assert inprocess("judge", "REVIEW", "F1=close:FIXED:abc1234", "--verdict", "v", cwd=project).returncode == 0
    report.path(state, "REVIEW").unlink()
    agent(state, "LENS", Written("auditor", (STORE,)))
    done = inprocess("judge", "LENS", "F1=route:owner", "--verdict", "v", cwd=project)
    assert done.returncode == 0, done.stderr
    assert f"LENS-F1 may repeat REVIEW-F1 (FIXED: abc1234): both at `{STORE}`" in done.stdout


def test_a_bank_line_written_before_it_carried_an_anchor_reads_and_its_report_gives_the_anchor(
    project: Path, state: Path
) -> None:
    """findings.jsonl outlives an upgrade, so a line with no `anchor` key still reads and still meets a repeat."""
    agent(state, "REVIEW", Written("auditor", (STORE,)))
    written = [
        {"row": "REVIEW-F1", "kind": "bank", "status": "OPEN", "owner": None, "evidence": None, "time": "t"},
        {"row": "REVIEW-F1", "kind": "close", "status": "MOOT", "owner": None, "evidence": "gone", "time": "t"},
    ]
    (state / store.FINDINGS).write_bytes(b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in written))
    model.update_agent(state, "REVIEW", lambda held: model.Agent(held.name, held.type, "judged"))
    assert rows(state)["REVIEW-F1"] == model.FindingEvent("REVIEW-F1", "close", "MOOT", evidence="gone", time="t")
    agent(state, "LENS", Written("auditor", (STORE,)))
    done = inprocess("judge", "LENS", "F1=route:owner", "--verdict", "v", cwd=project)
    assert done.returncode == 0, done.stderr
    assert f"LENS-F1 may repeat REVIEW-F1 (MOOT: gone): both at `{STORE}`" in done.stdout


def test_a_row_naming_the_owners_word_warns_where_it_goes_to_an_agent_and_not_to_a_queue(
    project: Path, state: Path
) -> None:
    """A queue builds nothing, so only a route to an agent can build what the owner has not ruled on."""
    agent(state, "FIXER", "writer", "briefed")
    agent(state, "REVIEW", Written("auditor", (STORE, CLI, "README :: install")))
    path = report.path(state, "REVIEW")
    text = path.read_bytes().decode("utf-8")
    # The row going to an agent spells it as owners and pasted text do: a typographic apostrophe, a capital.
    text = text.replace("The write at F1.", "This is the Owner\N{RIGHT SINGLE QUOTATION MARK}s call.")
    path.write_bytes(text.replace("The write at F2.", "Lowering it is the owner's word.").encode("utf-8"))
    given = ("F1=route:FIXER", "F2=route:ending", "F3=route:FIXER")
    done = inprocess("judge", "REVIEW", *given, "--verdict", "v", cwd=project)
    assert done.returncode == 0, done.stderr
    warned = [line for line in done.stdout.splitlines() if "the owner's word or call" in line]
    assert warned == [
        "REVIEW-F1 says its change is the owner's word or call, and goes to the agent FIXER:"
        " ask the owner before it is built, unless a ruling covers it"
    ]
    # A verifier's verdict is read too, as the text the row is judged on, with the body of the finding it judges:
    # REVIEW-F3's proof says it, and REVIEW-F2's banked finding does where its confirmation does not.
    proof = "F3 · optional · silent · README :: install\nRemoving the check is the owner's call."
    verdicts = (("REVIEW-F3", "CORRECTED", proof), ("REVIEW-F2", "CONFIRMED", "`pytest` exit 1."))
    agent(state, "VERIFY", Written("verifier", verdicts=verdicts))
    given = ("REVIEW-F3=route:FIXER", "REVIEW-F2=route:FIXER")
    verified = inprocess("judge", "VERIFY", *given, "--verdict", "v", cwd=project)
    assert verified.returncode == 0, verified.stderr
    warned = [line.split(":")[0] for line in verified.stdout.splitlines() if "the owner's word or call" in line]
    assert warned == [
        "REVIEW-F3 says its change is the owner's word or call, and goes to the agent FIXER",
        "REVIEW-F2 says its change is the owner's word or call, and goes to the agent FIXER",
    ]


def test_show_prints_whole_findings_judged_or_not_and_names_a_row_it_cannot(project: Path, state: Path) -> None:
    agent(state, "REVIEW", Written("auditor", (STORE, CLI)))
    assert (
        inprocess("judge", "REVIEW", "F1=route:owner", "F2=route:owner", "--verdict", "v", cwd=project).returncode == 0
    )
    agent(state, "LENS", Written("auditor", (STORE,)))
    done = inprocess("show", "REVIEW-F2", "LENS-F1", "REVIEW-F9", "NOBODY-F1", cwd=project)
    assert done.returncode == 2
    out = done.stdout.splitlines()
    assert out[:4] == [
        f"REVIEW-F2 · ROUTED to owner · {report.path(state, 'REVIEW')}",
        f"### F2 · defect · silent · {CLI}",
        "Harm: a torn file. Likelihood: rare.",
        "The write at F2.",
    ]
    assert f"LENS-F1 · not judged · {report.path(state, 'LENS')}" in out
    assert out[-1] == "2 of 4 finding(s) shown"
    assert f"REVIEW-F9: {report.path(state, 'REVIEW')} holds no F9" in done.stderr
    assert "no report at" in done.stderr


def test_route_moves_a_row_from_a_finished_owner_and_refuses_one_a_live_agent_holds(project: Path, state: Path) -> None:
    agent(state, "FIXER", "writer", "running")
    agent(state, "OTHER", "writer", "briefed")
    agent(state, "REVIEW", Written("auditor", (STORE, CLI)))
    assert (
        inprocess("judge", "REVIEW", "F1=route:FIXER", "F2=route:owner", "--verdict", "v", cwd=project).returncode == 0
    )
    before = lines(state)
    # All or none: REVIEW-F2 could move, and REVIEW-F1's refusal holds it back.
    held = inprocess("route", "OTHER", "REVIEW-F2", "REVIEW-F1", cwd=project)
    assert held.returncode == 2
    assert "REVIEW-F1 is routed to FIXER, which is running: its rows stay with it until it reports" in held.stderr
    assert lines(state) == before
    moved = inprocess("route", "OTHER", "REVIEW-F2", cwd=project)
    assert moved.returncode == 0, moved.stderr
    assert moved.stdout.splitlines() == ["ROUTED REVIEW-F2 to OTHER", "1 row(s) moved to OTHER"]
    again = inprocess("route", "OTHER", "REVIEW-F2", cwd=project)
    assert again.stdout.splitlines() == ["0 row(s) moved to OTHER"]
    model.update_agent(state, "FIXER", lambda agent: model.Agent(agent.name, agent.type, "reported"))
    assert inprocess("route", "OTHER", "REVIEW-F1", cwd=project).returncode == 0
    assert rows(state)["REVIEW-F1"].owner == "OTHER"


def test_a_running_record_whose_transcript_ended_holds_no_row_and_its_report_can_be_judged(
    project: Path, state: Path
) -> None:
    """A session that died, or a stop hook that never ran, leaves the record `running` with no agent behind it.

    A transcript that cannot be read shows nothing, so that record still holds its rows, as `prune` keeps its tree.
    A turn that has just ended is still held: its stop hook may yet refuse the stop and the agent write on.
    """
    write_agent(state, model.Agent("FIXER", "writer", "running", ids=("f1",)))
    agent(state, "REVIEW", Written("auditor", (STORE,)))
    assert inprocess("judge", "REVIEW", "F1=route:FIXER", "--verdict", "v", cwd=project).returncode == 0
    held = inprocess("route", "owner", "REVIEW-F1", cwd=project)
    assert "REVIEW-F1 is routed to FIXER, which is running" in held.stderr
    config = project.parent / "claude-config"
    transcript(config, "f1", line("assistant", "m1", calls=True))
    assert inprocess("route", "owner", "REVIEW-F1", cwd=project).returncode == 2
    assert (
        "FIXER is running, so its report may be unfinished"
        in inprocess("judge", "FIXER", "--verdict", "v", cwd=project).stderr
    )
    transcript(config, "f1", line("assistant", "m1"))
    assert (
        "REVIEW-F1 is routed to FIXER, which is running" in inprocess("route", "owner", "REVIEW-F1", cwd=project).stderr
    )
    transcript(config, "f1", line("assistant", "m1"), minutes=context.SILENT_MINUTES + 1)
    moved = inprocess("route", "owner", "REVIEW-F1", cwd=project)
    assert moved.returncode == 0, moved.stderr
    assert rows(state)["REVIEW-F1"].owner == "owner"
    Path(report.path(state, "FIXER")).write_bytes(Written("writer").text("FIXER").encode("utf-8"))
    judged = inprocess("judge", "FIXER", "--verdict", "its stop hook never ran", cwd=project)
    assert judged.returncode == 0, judged.stderr
    assert model.read_agent(state, "FIXER").status == "judged"


def test_a_stopped_agent_whose_transcript_is_mid_turn_is_judged_only_once_it_falls_silent(
    project: Path, state: Path
) -> None:
    """A takeover leaves a live agent `stopped`, and the report it fills as it goes already reads."""
    agent(state, "REVIEW", Written("auditor", (STORE,)), "stopped")
    write_agent(state, model.Agent("REVIEW", "auditor", "stopped", ids=("r1",)))
    config = project.parent / "claude-config"
    transcript(config, "r1", line("assistant", "m1", calls=True))
    refused = inprocess("judge", "REVIEW", "F1=route:owner", "--verdict", "v", cwd=project)
    assert refused.returncode == 2
    assert "REVIEW may still be working (its transcript is mid-turn" in refused.stderr
    assert lines(state) == b""
    transcript(config, "r1", line("assistant", "m1", calls=True), minutes=context.SILENT_MINUTES + 1)
    judged = inprocess("judge", "REVIEW", "F1=route:owner", "--verdict", "v", cwd=project)
    assert judged.returncode == 0, judged.stderr


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (("owner", "REVIEW-F9"), "REVIEW-F9 is no row"),
        (("owner", "review-f1"), "`review-f1` is no row id"),
    ],
    ids=["a row never banked", "no row id"],
)
def test_a_route_it_cannot_make_writes_nothing(project: Path, state: Path, args: tuple[str, ...], said: str) -> None:
    agent(state, "REVIEW", Written("auditor", (STORE,)))
    assert inprocess("judge", "REVIEW", "F1=route:handoff", "--verdict", "v", cwd=project).returncode == 0
    before = lines(state)
    done = inprocess("route", *args, cwd=project)
    assert done.returncode == 2
    assert said in done.stderr, done.stderr
    assert lines(state) == before


def test_close_closes_rows_on_evidence_and_route_reopens_a_closed_one(project: Path, state: Path) -> None:
    agent(state, "REVIEW", Written("auditor", (STORE, CLI)))
    assert (
        inprocess("judge", "REVIEW", "F1=route:owner", "F2=route:owner", "--verdict", "v", cwd=project).returncode == 0
    )
    done = inprocess("close", "FIXED", "abc1234 adds\nthe guard", "REVIEW-F1", "REVIEW-F2", cwd=project)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == "FIXED REVIEW-F1 (abc1234 adds the guard)"
    before = lines(state)
    twice = inprocess("close", "MOOT", "gone", "REVIEW-F1", cwd=project)
    assert twice.returncode == 2
    assert "REVIEW-F1 is FIXED already (abc1234 adds the guard): route it to reopen it" in twice.stderr
    assert lines(state) == before
    reopened = inprocess("route", "owner", "REVIEW-F1", cwd=project)
    assert reopened.stdout.splitlines()[0] == "REOPENED REVIEW-F1 to owner"
    assert rows(state)["REVIEW-F1"].kind == "reopen"


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (("MOOT", " \n ", "REVIEW-F1"), "the evidence is empty"),
        (("MOOT", "gone", "REVIEW-F1", "REVIEW-F9"), "REVIEW-F9 is no row"),
    ],
    ids=["no evidence", "a row never banked beside one that is"],
)
def test_a_close_it_cannot_make_writes_nothing(project: Path, state: Path, args: tuple[str, ...], said: str) -> None:
    agent(state, "REVIEW", Written("auditor", (STORE,)))
    assert inprocess("judge", "REVIEW", "F1=route:owner", "--verdict", "v", cwd=project).returncode == 0
    before = lines(state)
    done = inprocess("close", *args, cwd=project)
    assert done.returncode == 2
    assert said in done.stderr, done.stderr
    assert lines(state) == before


def test_findings_lists_every_unclosed_row_by_owner_and_exits_one_while_one_remains(project: Path, state: Path) -> None:
    agent(state, "FIXER", "writer", "briefed")
    agent(state, "REVIEW", Written("auditor", (STORE, CLI, "README :: install")))
    given = ("F1=route:FIXER", "F2=route:owner", "F3=route:FIXER")
    assert inprocess("judge", "REVIEW", *given, "--verdict", "v", cwd=project).returncode == 0
    done = inprocess("findings", cwd=project)
    assert done.returncode == 1
    assert done.stdout.splitlines() == [
        "FIXER: 2",
        f"  REVIEW-F1 · defect · silent · {STORE}",
        "  REVIEW-F3 · defect · silent · README :: install",
        "owner: 1",
        f"  REVIEW-F2 · defect · silent · {CLI}",
        "3 unclosed row(s): 0 OPEN, 3 ROUTED",
    ]
    rows_ = ("REVIEW-F1", "REVIEW-F2", "REVIEW-F3")
    assert inprocess("close", "HANDOFF", "the next session takes it", *rows_, cwd=project).returncode == 0
    closed = inprocess("findings", cwd=project)
    assert (closed.returncode, closed.stdout.strip()) == (0, "every row is closed")
