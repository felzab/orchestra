"""The report's grammar: a valid report of each type parses, and each way of breaking it is named by its line."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import MakeRepo, git
from orchestra import Refused, report

FINDING = "F1 · defect · silent · lib/store.py :: save"
SECOND = "F2 · optional · loud · lib/cli.py :: emit"


def text(
    kind: str = "writer", name: str = "FE", round_: int = 1, status: str = "DONE", fixed: str | None = None
) -> str:
    """A valid report of the type: two findings, or for a verifier two verdicts; `fixed` the rows its
    `Rows fixed:` line names, and no such line where None."""
    commits = {"writer": "Commits: 2, tip abc1234", "driver": "Commits: 0, tip abc1234"}.get(kind, "Commits: none")
    if kind == "verifier":
        items = ["REVIEW-F1 · REFUTED", "REVIEW-F2 · CORRECTED"]
        section = [
            "## Verdicts",
            "### REVIEW-F1 · REFUTED",
            "`git show base:lib/store.py` holds the guard at line 12.",
            "",
            "### REVIEW-F2 · CORRECTED",
            "F2 · optional · loud · lib/cli.py :: emit",
            "The anchor was `main`; the passage is at `emit`.",
        ]
    else:
        items = [FINDING, SECOND]
        section = [
            "## Findings",
            f"### {FINDING}",
            "Harm: a crash tears the file. Likelihood: rare, on a power cut.",
            "The write is not atomic.",
            "```markdown",
            "## Done",
            "### F9 · a quoted heading is no heading",
            "```",
            "",
            f"### {SECOND}",
            "Harm: a long line wraps. Likelihood: often, on any phone.",
        ]
    lines = [
        f"# {name} · {kind} · round {round_}",
        "",
        "## Summary",
        status,
        "Built the store's writes.",
        commits,
        *([] if fixed is None else [f"Rows fixed: {fixed}"]),
        *items,
        "Not verified: nothing",
        "Wrong in the brief: nothing",
        "",
        "## Done",
        "1. abc1234: `uv run pytest tests/test_store.py` exit 0.",
        "",
        *section,
        "",
        "## Not verified",
        "nothing",
        "",
        "## Wrong in the brief",
        "nothing",
        "",
        "## Outside the scope",
        "nothing",
        "",
    ]
    return "\n".join(lines)


def problems(written: str, kind: str = "writer", name: str = "FE", round_: int = 1) -> list[str]:
    _, said = report.parse(written, kind, name, round_)
    return [str(violation) for violation in said]


@pytest.mark.parametrize("kind", ["writer", "driver", "auditor", "researcher", "verifier"])
def test_a_valid_report_of_each_type_parses(kind: str) -> None:
    held, said = report.parse(text(kind), kind, "FE", 1)
    assert said == []
    assert held is not None
    assert (held.name, held.type, held.round, held.status) == ("FE", kind, 1, "DONE")
    if kind == "verifier":
        assert [(verdict.row, verdict.verdict) for verdict in held.verdicts] == [
            ("REVIEW-F1", "REFUTED"),
            ("REVIEW-F2", "CORRECTED"),
        ]
        assert held.verdicts[1].first == SECOND
    else:
        assert [finding.heading for finding in held.findings] == [FINDING, SECOND]
        first = held.findings[0]
        assert (first.kind, first.silent, first.anchor) == ("defect", True, "lib/store.py :: save")
        assert first.body.startswith("Harm: a crash tears the file.")
        # A heading inside a fenced block is the finding's text, not a heading.
        assert "### F9 · a quoted heading is no heading" in first.body
    assert held.commits == {"writer": 2, "driver": 0}.get(kind)
    assert held.tip == ("abc1234" if kind in {"writer", "driver"} else None)


def test_a_stopped_status_carries_its_reason_and_a_later_round_its_number() -> None:
    held, said = report.parse(text(status="NEEDS-ANSWER: which queue takes it?", round_=2), "writer", "FE", 2)
    assert said == []
    assert held is not None
    assert (held.status, held.reason, held.round) == ("NEEDS-ANSWER", "which queue takes it?", 2)


def test_a_writers_optional_rows_fixed_line_names_its_rows_each_faulted_where_it_is_none() -> None:
    """The line after the commit line, which a landing reads; left out, the report names no row."""
    held, said = report.parse(text(fixed="A-F2, A.2-F11."), "writer", "FE", 1)
    assert said == []
    assert held is not None
    assert (held.rows, len(held.findings)) == (("A-F2", "A.2-F11"), 2)
    held, said = report.parse(text(), "writer", "FE", 1)
    assert held is not None
    assert held.rows == ()
    no_row = "is no row id: <NAME>-F<n>, or <NAME>.<round>-F<n> from the second round"
    assert problems(text(fixed="A-F1, nonsense.")) == [f"line 7 `Rows fixed: A-F1, nonsense.`: `nonsense` {no_row}"]
    assert problems(text(fixed="")) == [
        "line 7 `Rows fixed: `: names no row: `Rows fixed: <id>, <id>.`, or leave the line out"
    ]
    # Only a writer's branch lands, and a driver's lands nothing, so another type's line is a stray item line.
    for kind in ("driver", "researcher"):
        found = problems(text(kind, fixed="A-F2."), kind)
        assert any(problem.startswith("line 7 `Rows fixed: A-F2.`: no item line") for problem in found), (kind, found)


def test_a_report_with_no_finding_holds_none() -> None:
    written = (
        text().split("## Findings")[0] + "## Findings\nnone\n\n## Not verified" + text().split("## Not verified")[1]
    )
    written = written.replace(f"{FINDING}\n{SECOND}\n", "")
    held, said = report.parse(written, "writer", "FE", 1)
    assert said == []
    assert held is not None
    assert held.findings == ()


@pytest.mark.parametrize(
    ("old", "new", "said"),
    [
        ("## Done\n", "", "`## Done` is missing"),
        (
            "## Not verified\nnothing\n\n## Wrong in the brief\nnothing\n",
            "## Wrong in the brief\nnothing\n\n## Not verified\nnothing\n",
            "line 30 `## Not verified`: stands after `## Wrong in the brief`",
        ),
        ("## Outside the scope\n", "## Notes\n", "line 33 `## Notes`: no heading of a writer's report"),
        ("\nDONE\n", "\nFINISHED\n", "line 4 `FINISHED`: the summary's first line is no status"),
        ("\nDONE\n", "\nBLOCKED\n", "line 4 `BLOCKED`: BLOCKED carries its reason after a colon"),
        (
            f"### {FINDING}",
            "### F1 · serious · lib/store.py",
            "line 16 `### F1 · serious · lib/store.py`: the finding heading",
        ),
        (
            f"### {SECOND}",
            "### F3 · optional · loud · lib/cli.py :: emit",
            "loud · lib/cli.py :: emit`: F3 where F2 is next",
        ),
        (
            "Harm: a long line wraps. Likelihood: often, on any phone.",
            "A long line wraps.",
            "its first line is no `Harm:",
        ),
        (
            f"\n{SECOND}\n",
            "\nF2 · defect · loud · lib/cli.py :: emit\n",
            "loud · lib/cli.py :: emit`: differs from its heading at line 24",
        ),
        (f"\n{SECOND}\nNot", "\nNot", "F2 has no line in the summary"),
        ("Commits: 2, tip abc1234", "Commits: two", "line 6 `Commits: two`: a writer's third summary line"),
        ("Built the store's writes.\n", "", "line 5 `Commits: 2, tip abc1234`: the sentence saying what was done"),
        (
            f"{FINDING}\n{SECOND}\nNot verified: nothing\nWrong in the brief: nothing\n",
            "",
            "the summary holds 3 line(s), and it needs",
        ),
        ("## Findings\n", "## Findings\nThe store has a flaw.\n", "line 16 `The store has a flaw.`: stands before"),
        ("## Not verified\nnothing", "## Not verified\n### F3 · defect · loud · x", "a finding outside `## Findings`"),
        ("## Outside the scope\nnothing", "## Outside the scope\n", "`## Outside the scope` holds nothing"),
        ("# FE · writer · round 1", "# FE · driver · round 1", "the title names a driver, and this agent is a writer"),
        ("# FE · writer · round 1", "# FE · writer · round 2", "the title names round 2"),
        ("# FE · writer · round 1", "FE's report", "line 1 `FE's report`: the first line is no title"),
        ("Not verified: nothing\n", "Not verified: nothing\n" + "x" * 1500 + "\n", "the summary's fixed lines hold 1"),
        ("## Not verified\n", "## Done\nagain\n\n## Not verified\n", "a second `## Done`"),
        (f"\n{SECOND}\nNot", f"\n{SECOND}\nF3 · optional · loud · x\nNot", "F3 is no heading of this report"),
        (f"\n{SECOND}\nNot", f"\n{SECOND}\n{SECOND}\nNot", "a second summary line for F2"),
    ],
    ids=[
        "a heading absent",
        "headings out of order",
        "a heading of no report",
        "a status of none of the four",
        "a stop with no reason",
        "a finding heading that does not parse",
        "numbers that skip",
        "a finding with no harm line",
        "a summary line differing from its heading",
        "a finding the summary leaves out",
        "a writer's commit line",
        "a summary missing its sentence",
        "a summary missing its closing lines",
        "text belonging to no finding",
        "a finding outside its section",
        "an empty section",
        "the title's type",
        "the title's round",
        "no title",
        "a summary over its bytes",
        "a heading twice",
        "a summary line naming no heading",
        "a summary line twice",
    ],
)
def test_each_way_of_breaking_the_grammar_is_named_by_its_line(old: str, new: str, said: str) -> None:
    written = text()
    assert old in written
    found = problems(written.replace(old, new, 1))
    assert any(said in problem for problem in found), found


def test_a_findings_section_of_prose_is_refused_rather_than_read_as_none() -> None:
    written = text().replace(f"{FINDING}\n{SECOND}\n", "")
    before, after = written.split("## Findings\n")[0], written.split("## Not verified\n")[1]
    found = problems(f"{before}## Findings\n- F1 the store tears\n\n## Not verified\n{after}")
    assert any("`## Findings` holds no `###` heading, so it holds the line `none`" in problem for problem in found), (
        found
    )


def test_a_review_of_many_findings_fits_the_summary_bound() -> None:
    """The bound holds the summary's fixed lines alone, so forty findings never force one to be dropped."""
    heads = [
        f"F{k} · optional · loud · lib/orchestra/module_{k}.py :: a_function_with_a_long_name" for k in range(1, 41)
    ]
    written = text().replace(f"{FINDING}\n{SECOND}\n", "".join(f"{head}\n" for head in heads), 1)
    section = "".join(f"### {head}\nHarm: a wrong row. Likelihood: rare.\n\n" for head in heads)
    before, after = written.split("## Findings\n")[0], written.split("## Not verified\n")[1]
    assert problems(f"{before}## Findings\n{section}## Not verified\n{after}") == []


@pytest.mark.parametrize(
    ("kind", "old", "new", "said"),
    [
        ("auditor", "Commits: none", "Commits: 1, tip abc1234", "an auditor commits nothing"),
        ("verifier", "### REVIEW-F2 · CORRECTED\nF2", "### REVIEW-F2 · CORRECTED\nThe", "opens with the corrected"),
        ("verifier", "### REVIEW-F1 · REFUTED", "### REVIEW-F1 · WRONG", "the verdict heading does not parse"),
        ("verifier", "## Verdicts", "## Findings", "no heading of a verifier's report"),
        (
            "verifier",
            "### REVIEW-F1 · REFUTED\n`git show base:lib/store.py` holds the guard at line 12.\n",
            "### REVIEW-F1 · REFUTED\n",
            "the verdict names no proof",
        ),
        ("verifier", "### REVIEW-F2 · CORRECTED", "### REVIEW-F1 · CORRECTED", "a second verdict on REVIEW-F1"),
    ],
    ids=[
        "an auditor's commit line",
        "a correction with no heading",
        "a verdict of none of the four",
        "a verifier's findings",
        "a refutation with no proof",
        "a row given two verdicts",
    ],
)
def test_a_types_own_lines_are_held_to_its_grammar(kind: str, old: str, new: str, said: str) -> None:
    written = text(kind)
    assert old in written
    found = problems(written.replace(old, new, 1), kind)
    assert any(said in problem for problem in found), found


def test_check_names_a_missing_file_and_a_file_name_of_no_report(tmp_path: Path) -> None:
    assert [str(v) for v in report.check(tmp_path / "FE.md", "auditor", None)] == [
        f"no report at {tmp_path / 'FE.md'}: create it with its six headings first"
    ]
    (tmp_path / "fe.md").write_bytes(text("auditor").encode())
    assert "fe.md is no report's name" in str(report.check(tmp_path / "fe.md", "auditor", None)[0])


def test_check_reads_a_later_rounds_file_name_against_its_title(tmp_path: Path) -> None:
    second = tmp_path / "FE.2.md"
    second.write_bytes(text("auditor", round_=2).encode())
    assert report.check(second, "auditor", None) == []
    second.write_bytes(text("auditor").encode())
    assert "the title names round 1, and this is round 2's report" in str(report.check(second, "auditor", None)[0])


def test_check_refuses_a_committing_agents_unclean_worktree(tmp_path: Path, make_repo: MakeRepo) -> None:
    tree = make_repo(tmp_path / "tree")
    file = tmp_path / "FE.md"
    file.write_bytes(text("writer").encode())
    assert report.check(file, "writer", tree) == []
    (tree / "left.txt").write_bytes(b"uncommitted\n")
    said = [str(v) for v in report.check(file, "writer", tree)]
    assert len(said) == 1
    assert "prints 1 line(s), `?? left.txt`: commit the work or restore it" in said[0]
    git(tree, "add", "left.txt")
    git(tree, "commit", "-m", "left")
    assert report.check(file, "writer", tree) == []


def test_read_refuses_naming_every_line_at_fault(tmp_path: Path) -> None:
    file = tmp_path / "FE.md"
    file.write_bytes(text().replace("\nDONE\n", "\nFINISHED\n").replace("## Done\n", "").encode())
    with pytest.raises(Refused) as refused:
        report.read(file)
    assert "line 4 `FINISHED`" in str(refused.value)
    assert "`## Done` is missing" in str(refused.value)


def test_a_refused_finding_heading_gets_no_second_fault_from_its_summary_line() -> None:
    """Its summary line matches a heading that exists; naming it "no heading" would mislead."""
    broken = text("auditor").replace(
        "Harm: a crash tears the file. Likelihood: rare, on a power cut.",
        "Harm: a crash tears the file.\nLikelihood: rare.",
    )
    _, said = report.parse(broken, "auditor")
    assert len(said) == 1, said
    assert "its first line is no `Harm:" in str(said[0])
