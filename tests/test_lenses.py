"""The lenses a change makes due, from each lens file's two lines and the project's triggers, and held at the end."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import git, inprocess, write_agent, write_config
from orchestra import Refused, diff, model

# A lens file of the project's own for each form, each replacing the plugin's of its name.
LENSES = {
    "security": "**Applies.** always.\n**Run by.** auditor.\n",
    "tests": "**Applies.** paths `**/tests/**`, `**/test_*`.\n**Run by.** driver.\n",
    "necessity": "**Run by.** driver.\n**Applies.** paths `**/tests/**`, `**/test_*`.\n",
    "prompt": "**Applies.** paths `skills/**`, `agents/**`.\n**Run by.** auditor.\n",
    "document": "**Applies.** paths `**/*.md` except `skills/**`, `agents/**`.\n**Run by.** auditor.\n",
    "data": "**Applies.** declared.\n**Run by.** auditor.\n",
    "cost": "**Applies.** declared.\n**Run by.** driver.\n",
    "accessibility": "**Applies.** declared.\n**Run by.** auditor.\n",
    "disclosure": "**Applies.** declared.\n**Run by.** researcher.\n",
    "estimate": "**Applies.** on request.\n**Run by.** auditor.\n",
}
TRIGGERS: list[dict[str, object]] = [
    {"name": "data", "when": ["app/db/"]},
    {"name": "accessibility", "when": []},
    {"name": "disclosure", "when": ["**"]},
]
CHANGED = ("app/db/store.py", "skills/run/page.md", "tests/test_store.py")


def _project(project: Path, triggers: list[dict[str, object]]) -> Path:
    folder = project / diff.PROJECT_LENSES
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in LENSES.items():
        (folder / f"{name}.md").write_bytes(text.encode())
    write_config(project, {"stateRoot": (project.parent / "state").as_posix(), "lenses": {"triggers": triggers}})
    return folder


def _lens_lines(stdout: str) -> dict[str, str]:
    return {line.split()[1].rstrip(","): line for line in stdout.splitlines() if line.startswith("lens ")}


@pytest.fixture
def changed(project: Path) -> Path:
    """A session whose change touches a test file, a skill page and a stored-state path."""
    assert inprocess("init", "s1", cwd=project).returncode == 0
    for path in CHANGED:
        (project / path).parent.mkdir(parents=True, exist_ok=True)
        (project / path).write_bytes(b"x\n")
    git(project, "add", *CHANGED)
    git(project, "commit", "-m", "work")
    return project.parent / "state" / "project" / "s1"


@pytest.mark.usefixtures("changed")
def test_the_changed_paths_make_each_lens_due_by_its_applies_line_and_the_projects_triggers(project: Path) -> None:
    _project(project, TRIGGERS)
    done = inprocess("diff", cwd=project)
    assert done.returncode == 0, done.stderr
    lines = _lens_lines(done.stdout)
    assert lines["security"] == "lens     security, by an auditor: every change"
    assert lines["tests"] == "lens     tests, by a driver: tests/test_store.py"
    assert lines["necessity"] == "lens     necessity, by a driver: tests/test_store.py"
    assert lines["prompt"] == "lens     prompt, by an auditor: skills/run/page.md"
    assert lines["data"] == "lens     data, by an auditor: app/db/store.py"
    assert lines["disclosure"] == f"lens     disclosure, by a researcher: {', '.join(CHANGED)}"
    config = ".claude/orchestra.json"
    assert (
        lines["cost"]
        == f"lens     cost, by a driver: undeclared; give its paths, or none, under `lenses.triggers` in {config}"
    )
    # A Markdown page its `except` leaves out, a trigger declaring no paths and a lens run on request.
    assert not {"document", "accessibility", "estimate"} & set(lines)
    assert " undeclared" in done.stdout.splitlines()[-1]

    _project(project, [trigger for trigger in TRIGGERS if trigger["name"] != "data"])
    lines = _lens_lines(inprocess("diff", cwd=project).stdout)
    undeclared = f"undeclared; give its paths, or none, under `lenses.triggers` in {config}"
    assert lines["data"] == f"lens     data, by an auditor: {undeclared}"


def _judged(state: Path, name: str, kind: model.AgentType, lens: str) -> None:
    """An agent whose brief names `lens`, judged with a verdict as `judge` leaves it."""
    write_agent(state, model.Agent(name, kind, "judged", reports={1: "r"}, outcome="DONE", verdict="ok"))
    header = (
        f"Agent: {name}            Type: {kind}            Slice: S1\nBase: main at 0000000\n"
        f"Report: {state / 'reports' / f'{name}.md'}\nOwns: none\nRows: none\nWrites: none\nLens: {lens}\n"
    )
    (state / "briefs" / f"{name}.md").write_bytes(header.encode())


def _lens_end(project: Path) -> str:
    done = inprocess("status", "--end", cwd=project)
    assert done.returncode == 0, done.stderr
    (line,) = [line for line in done.stdout.splitlines() if "each due lens judged or declined" in line]
    return line


def test_the_end_holds_each_due_lens_by_a_judged_report_of_its_type_or_a_declining_note(
    project: Path, changed: Path
) -> None:
    _project(project, TRIGGERS)
    readers: tuple[tuple[str, model.AgentType, str], ...] = (
        ("SEC", "auditor", "security"),
        ("PROMPT", "auditor", "prompt"),
        ("DATA", "auditor", "data"),
        ("TESTS", "driver", "tests"),
        ("NEED", "driver", "necessity"),
    )
    for name, kind, lens in readers:
        _judged(changed, name, kind, lens)
    # A reader given several lenses holds none of them: each due lens has an agent of its own.
    _judged(changed, "GROUPED", "auditor", "security, contract")
    # An auditor cannot answer the question of a lens a researcher runs.
    _judged(changed, "WRONG", "auditor", "disclosure")
    # Pruned or landed before its report was judged, an agent's brief holds no lens: its review was never banked.
    _judged(changed, "UNJUDGED", "auditor", "accessibility, contract")
    write_agent(changed, model.Agent("UNJUDGED", "auditor", "pruned", reports={1: "r"}, outcome="DONE"))
    line = _lens_end(project)
    assert line.startswith("NOT held  each due lens judged or declined: no judged report and no")
    missing = line.partition(" note for ")[2].split(", ")
    assert "disclosure (read by an auditor; its Run by line names a researcher)" in missing
    assert "contract (read beside other lenses by GROUPED)" in missing
    # Undeclared, it is read or declined as a due lens is, since nothing says whether the change touches it.
    assert "cost (undeclared)" in missing
    named = {said.split()[0] for said in missing}
    # Held, declined or never due: a trigger with no paths declares a lens due on nothing.
    assert not {"security", "prompt", "data", "tests", "necessity", "document", "accessibility"} & named

    _judged(changed, "RES", "researcher", "disclosure")
    # The plugin's own lenses this project does not replace, each declined with its reason.
    for name in named - {"disclosure"}:
        assert inprocess("note", f"Lens declined: {name}, not this change's", cwd=project).returncode == 0
    line = _lens_end(project)
    assert line.startswith("held      each due lens judged or declined: declined ")
    assert "contract (not this change's)" in line


def test_a_lens_line_is_read_in_every_form_and_any_other_value_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.md"
    lens = diff.parse("x", path, "**Applies.** paths `a/`, `b/**` except `b/c`\n**Run by.** researcher\n")
    assert (lens.applies, lens.paths, lens.excepted, lens.run_by) == ("paths", ("a/", "b/**"), ("b/c",), "researcher")
    # A project's lens checked out with CRLF line ends reads as its LF form.
    crlf = diff.parse("x", path, "**Applies.** paths `a/`.\r\n**Run by.** driver.\r\n")
    assert (crlf.applies, crlf.paths, crlf.run_by) == ("paths", ("a/",), "driver")
    by = "**Run by.** auditor.\n"
    for text, said in (
        ("**Applies.** sometimes.\n" + by, "`**Applies.** sometimes` is none of"),
        ("**Applies.** paths a/, b/.\n" + by, "`**Applies.** paths a/, b/` is none of"),
        (
            "**Applies.** always.\n**Run by.** writer.\n",
            "`**Run by.** writer` names none of auditor, driver, researcher",
        ),
        ("**Applies.** always.\n**Applies.** declared.\n" + by, "holds 2 `**Applies.**` lines"),
        # A line missing is refused as a wrong one is: no default stands in for the lens's author.
        ("**Applies.** declared.\n", "holds 0 `**Run by.**` lines"),
        (by, "holds 0 `**Applies.**` lines"),
    ):
        with pytest.raises(Refused, match=said.replace("*", r"\*").replace(".", r"\.")):
            diff.parse("x", path, text)


def test_doctor_fails_a_trigger_naming_no_lens_and_a_lens_line_it_cannot_read(project: Path) -> None:
    write_config(
        project,
        {"stateRoot": (project.parent / "state").as_posix(), "lenses": {"triggers": [{"name": "nope", "when": []}]}},
    )
    done = inprocess("doctor", cwd=project)
    assert done.returncode == 1
    assert "FAILED   .claude/orchestra.json: `lenses.triggers[0]` names `nope`, which is no lens file" in done.stdout
    folder = _project(project, [])
    (folder / "bad.md").write_bytes(b"**Applies.** always.\n**Run by.** everyone.\n")
    done = inprocess("doctor", cwd=project)
    assert done.returncode == 1
    assert f"FAILED   {folder / 'bad.md'}: `**Run by.** everyone` names none of" in done.stdout
    # A file lacking a line is named as one holding a wrong value is: no default reads it for its author.
    (folder / "bad.md").write_bytes(b"# Lens: bad\n\n**Question.** Is it so?\n")
    done = inprocess("doctor", cwd=project)
    assert f"FAILED   {folder / 'bad.md'} holds 0 `**Applies.**` lines, and a lens file holds one" in done.stdout


def test_every_lens_the_plugin_ships_states_when_it_applies_and_who_runs_it() -> None:
    found = [
        diff.parse(path.stem, path, path.read_bytes().decode()) for path in sorted(diff.PLUGIN_LENSES.glob("*.md"))
    ]
    assert found
