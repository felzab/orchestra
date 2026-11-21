"""`brief`: the header written from the state, and each fault `--check` refuses, over a fixture session."""

from __future__ import annotations

import dataclasses
import hashlib
import sys
from pathlib import Path

import pytest

from conftest import git, inprocess, line, transcript, write_config
from orchestra import brief, config, context, model, store

BODY = (
    "GOAL      The module exists.\n"
    "WORK      1. Write it. At: lib/a.py. Accept: its test passes.\n"
    "PREMISES  none\n"
    "TRAPS     none\n"
)


@pytest.fixture
def state(project: Path) -> Path:
    """A session on `main`, whose branch holds `lib/a.py` and `docs/[id]/page.md`."""
    for path in ("lib/a.py", "docs/[id]/page.md"):
        (project / path).parent.mkdir(parents=True, exist_ok=True)
        (project / path).write_bytes(b"x\n")
    git(project, "add", ".")
    git(project, "commit", "-m", "files")
    assert inprocess("init", "s1", cwd=project).returncode == 0
    return store.state_dir(project)


def briefed(project: Path, name: str, *, kind: str = "writer", **values: str) -> Path:
    """A brief written by the verb, its lists filled from `values` (`none` by default) and BODY below them."""
    done = inprocess("brief", name, "--type", kind, "--slice", "S1", cwd=project)
    assert done.returncode == 0, done.stderr
    path = store.state_dir(project) / "briefs" / f"{name}.md"
    text = path.read_bytes().decode()
    for label in brief.LISTS:
        text = text.replace(f"\n{label}:\n", f"\n{label}: {values.get(label.lower(), 'none')}\n")
    path.write_bytes(text.replace("GOAL\nWORK\nPREMISES\nTRAPS\n", BODY).encode())
    return path


def check(project: Path, name: str) -> tuple[int, list[str]]:
    done = inprocess("brief", name, "--check", cwd=project)
    return done.returncode, done.stdout.splitlines()


def test_the_header_is_filled_from_the_state_and_git(project: Path, state: Path) -> None:
    done = inprocess("brief", "FE-A", "--type", "auditor", "--slice", "the  pages", cwd=project)
    assert done.returncode == 0, done.stderr
    path = state / "briefs" / "FE-A.md"
    assert done.stdout.splitlines()[0] == str(path)
    short = git(project, "rev-parse", "--short", "main")
    assert path.read_bytes().decode().splitlines() == [
        "Agent: FE-A    Type: auditor    Slice: the pages",
        f"Base: main at {short}",
        f"Report: {state / 'reports' / 'FE-A.md'}",
        "Owns:",
        "Rows:",
        "Writes:",
        "Lens:",
        "",
        "GOAL",
        "WORK",
        "PREMISES",
        "TRAPS",
    ]
    assert b"\r" not in path.read_bytes()
    assert (state / "briefs" / "FE-A.messages.md").read_bytes() == b""
    record = model.read_agent(state, "FE-A")
    assert (record.type, record.status, record.checked) == ("auditor", "briefed", None)
    assert done.stdout.splitlines()[1].startswith("FE-A briefed as an auditor on the pages:")
    again = inprocess("brief", "FE-A", "--type", "writer", "--slice", "S2", cwd=project)
    assert again.returncode == 2
    assert "exists, and what is written in it would be lost" in again.stderr


def test_a_writers_owns_is_filled_from_its_slices_line_in_the_plan(project: Path, state: Path) -> None:
    """The plan's line names the slice's files in backticks, as `status` reads it, so nobody copies them by hand."""
    plan = (
        "# Plan\n\n## Slices and their files\n\n"
        "- [ ] S1 the module `lib/a.py` and `docs/[id]/page.md` after: `S0`, S2\n"
        "- [ ] S2 a slice whose line names no file\n\n"
        "## Landing order\n\n- [ ] S3 `lib/a.py`\n"
    )
    store.write_text(state / store.PLAN, plan)
    done = inprocess("brief", "A", "--type", "writer", "--slice", "S1", cwd=project)
    assert done.returncode == 0, done.stderr
    assert "Owns: lib/a.py, docs/[id]/page.md" in (state / "briefs" / "A.md").read_bytes().decode().splitlines()
    assert brief.header(state, "A").owns == ("lib/a.py", "docs/[id]/page.md")
    assert done.stdout.splitlines()[1] == (
        "A briefed as a writer on S1: `Owns:` holds the 2 path(s) its line in PLAN.md names;"
        " fill Rows, Writes and Lens and the four sections, then --check"
    )
    # A line naming no file, a line under another heading and a reader's brief leave the label to be filled.
    for name, kind, slice_ in (("B", "writer", "S2"), ("C", "writer", "S3"), ("D", "auditor", "S1")):
        assert inprocess("brief", name, "--type", kind, "--slice", slice_, cwd=project).returncode == 0
        assert brief.header(state, name).labels["Owns"] == ""
    # So does a missing plan: the brief is written all the same.
    (state / store.PLAN).unlink()
    done = inprocess("brief", "E", "--type", "writer", "--slice", "S1", cwd=project)
    assert done.returncode == 0, done.stderr
    assert brief.header(state, "E").labels["Owns"] == ""


def test_a_body_file_is_written_below_the_header_and_the_brief_checked_in_the_same_call(
    project: Path, state: Path, tmp_path: Path
) -> None:
    """Otherwise a brief costs the header verb, the body's write and the check: three calls where two serve."""
    store.write_text(state / store.PLAN, "# Plan\n\n## Slices and their files\n\n- [ ] S1 `lib/a.py`\n")
    body = tmp_path / "body.md"
    body.write_bytes(f"Rows: none\nWrites: none\nLens: none\n\n{BODY}".encode())
    done = inprocess("brief", "A", "--type", "writer", "--slice", "S1", "--body", str(body), cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    path = state / "briefs" / "A.md"
    assert done.stdout.splitlines() == [str(path), f"A's brief is checked: {path}"]
    short = git(project, "rev-parse", "--short", "main")
    head = f"Agent: A    Type: writer    Slice: S1\nBase: main at {short}\nReport: {state / 'reports' / 'A.md'}\n"
    assert path.read_bytes().decode() == f"{head}Owns: lib/a.py\nRows: none\nWrites: none\nLens: none\n\n{BODY}"
    assert brief.stamped(state, model.read_agent(state, "A"))
    # A check given a body would check the brief written and drop the body unread, so it is refused.
    done = inprocess("brief", "A", "--check", "--body", str(body), cwd=project)
    assert (done.returncode, done.stderr) == (
        2,
        "orchestra brief: --check takes the name alone: the header already names the type and the slice\n",
    )
    # A body naming its own `Owns:` keeps it, and one with a fault is written and refused as a check refuses it.
    body.write_bytes(f"Owns: lib/b.py (new)\nRows: none\nWrites: none\nLens:\n\n{BODY}".encode())
    done = inprocess("brief", "B", "--type", "writer", "--slice", "S1", "--body", str(body), cwd=project)
    assert done.returncode == 2
    assert done.stdout.splitlines()[1:-1] == ["`Lens:` has no value: name what it holds, or write `none`"]
    assert brief.header(state, "B").owns == ("lib/b.py",)
    # A body typing a line of the header the verb writes is refused before anything is written.
    body.write_bytes(f"Base: main at 1234567\nRows: none\n\n{BODY}".encode())
    done = inprocess("brief", "C", "--type", "writer", "--slice", "S1", "--body", str(body), cwd=project)
    assert done.returncode == 2
    assert "holds `Base: main at 1234567`: leave the header's first three lines to the verb" in done.stderr
    assert not (state / "briefs" / "C.md").exists()
    assert not model.agent_path(state, "C").exists()


def test_a_reader_or_a_driver_is_briefed_outside_the_plan_with_no_slice_and_a_writer_on_none_is_drift(
    project: Path, state: Path
) -> None:
    """A reader changes no file a slice owns, and a driver plants in its own worktree and lands nothing, so a
    review, a question or a probe needs no slice made up for it; a writer's commits land, so it needs one."""
    for name, kind in (("READER", "auditor"), ("PROBE", "driver")):
        done = inprocess("brief", name, "--type", kind, cwd=project)
        assert done.returncode == 0, done.stderr
        assert brief.header(state, name).slice == "none"
    refused = inprocess("brief", "WRITER", "--type", "writer", cwd=project)
    assert refused.returncode == 2
    assert "a new brief needs --type, and --slice for a writer; `--check` checks one written" in refused.stderr
    loose = inprocess("brief", "LOOSE", "--type", "writer", "--slice", "none", cwd=project)
    assert loose.returncode == 0, loose.stderr
    # LOOSE's line shows the drift check ran over READER and PROBE too.
    lines = inprocess("status", cwd=project).stdout.splitlines()
    assert [line for line in lines if line.startswith("drift     agent ")] == [
        "drift     agent LOOSE: on no slice of PLAN.md (its slice is 'none')"
    ]


def test_a_whole_brief_is_stamped_and_an_edit_unstamps_it(project: Path, state: Path) -> None:
    path = briefed(project, "A", owns="lib/a.py, lib/b.py (new)", rows="none")
    head = brief.header(state, "A")
    assert (head.owns, head.new, head.rows, head.slice) == (("lib/a.py", "lib/b.py"), ("lib/b.py",), (), "S1")
    code, lines = check(project, "A")
    assert code == 0, lines
    assert lines == [f"A's brief is checked: {path}"]
    record = model.read_agent(state, "A")
    assert record.checked == hashlib.sha256(path.read_bytes()).hexdigest()
    assert brief.stamped(state, record)
    path.write_bytes(path.read_bytes() + b"one more line\n")
    assert not brief.stamped(state, model.read_agent(state, "A"))


def test_each_fault_of_the_text_is_named_and_an_old_stamp_cleared(project: Path, state: Path) -> None:
    path = briefed(project, "A", owns="lib/a.py", rows="X-F1, bad", lens="")
    assert check(project, "A")[0] == 2
    model.update_agent(state, "A", lambda held: dataclasses.replace(held, checked="stale"))
    text = path.read_bytes().decode().replace("Type: writer", "Type: driver").replace(" at ", " on ")
    text = text.replace("Report: ", "Report: x").replace(BODY, "WORK      1. Write it.\n\n2. Test it.\nGOAL\nTRAPS\n")
    path.write_bytes(text.encode())
    code, lines = check(project, "A")
    assert code == 2
    assert lines[:-1] == [
        "`Type:` says driver, and the record says writer",
        "`Base:` is not `<branch> at <commit>`",
        f"`Report:` is not {state / 'reports' / 'A.md'}",
        "`Lens:` has no value: name what it holds, or write `none`",
        "`Rows:` entry `bad` is no row id: <NAME>-F<n>",
        "the sections are WORK, GOAL, TRAPS: write GOAL, WORK, PREMISES, TRAPS, once each, in order",
        "GOAL is empty",
        "TRAPS is empty",
        "WORK item 1 has no `Accept:`: name the check and what it asserts",
        "WORK item 1 has no `At:`, and the brief carries rows: name the symbol, path or rule",
        "WORK item 2 has no `Accept:`: name the check and what it asserts",
        "WORK item 2 has no `At:`, and the brief carries rows: name the symbol, path or rule",
        "`X-F1` is no banked row: `orchestra findings` lists them",
    ]
    assert lines[-1] == f"A's brief is not checked: 13 fault(s), {path}"
    assert model.read_agent(state, "A").checked is None


@pytest.mark.usefixtures("state")
def test_a_work_section_with_no_item_and_a_first_line_rewritten(project: Path) -> None:
    path = briefed(project, "A", owns="lib/a.py")
    text = path.read_bytes().decode().replace("Agent: A ", "Agent: B ")
    path.write_bytes(text.replace("WORK      1. Write it.", "WORK      Write it.").encode())
    assert check(project, "A")[1][:-1] == [
        "`Agent:` names B, not A",
        "WORK holds no numbered item: `1. <the change> Accept: <the check>`",
    ]
    path.write_bytes(b"Agent A, a writer\n" + path.read_bytes().split(b"\n", 1)[1])
    assert check(project, "A")[1][0] == "the first line is not `Agent: <NAME>    Type: <type>    Slice: <slice>`"


@pytest.mark.usefixtures("state")
def test_an_owns_entry_naming_no_file_is_refused_unless_marked_new(project: Path) -> None:
    briefed(project, "A", owns="lib/a.py, docs/[id], lib/*.py, lib/c.py, lib/d/** (new)")
    assert check(project, "A")[1][:-1] == [
        "`Owns:` entry `lib/c.py` names no file on main: correct it, or mark a file still to be made `lib/c.py (new)`"
    ]


@pytest.mark.usefixtures("state")
def test_a_model_line_picks_a_profile_and_an_unknown_one_is_refused(project: Path) -> None:
    profiles = {"default": {"model": "opus"}, "quick": {"model": "sonnet", "effort": "low"}}
    write_config(project, {"stateRoot": (project.parent / "state").as_posix(), "models": profiles})
    settings = config.load(project)
    path = briefed(project, "A", owns="lib/a.py")
    assert brief.profile(brief.parse(path.read_bytes().decode()), settings) == config.Model("opus")
    path.write_bytes(path.read_bytes().replace(b"\nOwns:", b"\nModel: quick\nOwns:"))
    assert check(project, "A")[0] == 0
    assert brief.profile(brief.parse(path.read_bytes().decode()), settings) == config.Model("sonnet", "low")
    path.write_bytes(path.read_bytes().replace(b"Model: quick", b"Model: slow"))
    code, lines = check(project, "A")
    assert code == 2
    assert lines[:-1] == [
        "`Model:` names the profile slow, which `models` in .claude/orchestra.json lacks; its profiles: default, quick"
    ]


def test_a_writes_entry_reaching_the_checkout_the_state_or_the_home_folder_is_refused(
    project: Path, state: Path
) -> None:
    """A reader runs in the coordinator's checkout itself, so a `Writes:` entry there would let it write anything."""
    outside = f"{(project.parent / 'out').as_posix()}/**"
    wrong = {
        ".": "reaches the checkout",
        "lib/out/**": "reaches the checkout",
        f"{state.parent.as_posix()}/**": "reaches the session's state",
        Path.home().as_posix(): "covers the home folder",
    }
    if sys.platform == "win32":
        # Another drive's root, which holds no home folder; POSIX has one root, which holds it.
        wrong["Z:/**"] = "covers a file system's root"
        # The long-path form names the checkout's own folder, which Windows' file calls take as written.
        wrong[f"\\\\?\\{project / 'lib'}\\**"] = "reaches the checkout"
    briefed(project, "A", kind="auditor", writes=", ".join([*wrong, outside]))
    code, lines = check(project, "A")
    assert code == 2
    for entry, said in wrong.items():
        assert any(line.startswith(f"`Writes:` entry `{entry}` {said}") for line in lines), (entry, lines)
    assert not any(outside in line for line in lines)


def test_an_overlap_with_a_running_or_a_checked_brief_is_refused(project: Path, state: Path) -> None:
    briefed(project, "RUN", owns="lib/**")
    model.update_agent(state, "RUN", lambda held: dataclasses.replace(held, status="running"))
    briefed(project, "NEXT", owns="docs/[id]/page.md")
    assert check(project, "NEXT")[0] == 0
    # A brief written and never checked, and one whose agent landed, collide with nothing.
    briefed(project, "DRAFT", owns="lib/new/x.py (new)")
    briefed(project, "DONE", owns="lib/a.py")
    model.update_agent(state, "DONE", lambda held: dataclasses.replace(held, status="landed"))
    briefed(project, "A", owns="lib/new/x.py (new), docs/*/page.md")
    code, lines = check(project, "A")
    assert code == 2
    assert lines[:-1] == [
        "`docs/*/page.md` overlaps NEXT's `docs/[id]/page.md` (briefed)",
        "`lib/new/x.py` overlaps RUN's `lib/**` (running)",
    ]


def test_a_path_an_unlanded_branch_changes_by_commit_or_by_edit_is_refused(
    project: Path, state: Path, tmp_path: Path
) -> None:
    tree = tmp_path / "tree"
    git(project, "worktree", "add", "-q", "-b", "worktree-agent-a1", str(tree))
    (tree / "lib" / "a.py").write_bytes(b"changed\n")
    git(tree, "commit", "-qam", "change")
    (tree / "lib" / "edit.py").write_bytes(b"unsaved\n")
    briefed(project, "OTHER", owns="lib/a.py")
    model.update_agent(state, "OTHER", lambda held: dataclasses.replace(held, branch="worktree-agent-a1"))
    briefed(project, "A", owns="lib/a.py, lib/edit.py (new), docs/[id]/page.md")
    assert check(project, "A")[1][:-1] == [
        "lib/a.py is changed by OTHER (worktree-agent-a1), unlanded",
        "lib/edit.py is changed by OTHER (worktree-agent-a1), unlanded",
    ]
    git(project, "merge", "-q", "worktree-agent-a1")
    (tree / "lib" / "edit.py").unlink()
    assert check(project, "A")[0] == 0


def test_a_drivers_worktree_holds_no_path_of_a_writers_brief(project: Path, state: Path, tmp_path: Path) -> None:
    """A driver commits nothing and reverts each plant, so its worktree's edit is no work a landing would bring."""
    tree = tmp_path / "tree"
    git(project, "worktree", "add", "-q", "-b", "worktree-agent-d1", str(tree))
    (tree / "lib" / "a.py").write_bytes(b"planted\n")
    briefed(project, "PROBE", kind="driver")
    model.update_agent(
        state, "PROBE", lambda held: dataclasses.replace(held, status="running", branch="worktree-agent-d1")
    )
    briefed(project, "A", owns="lib/a.py")
    assert check(project, "A") == (0, [f"A's brief is checked: {state / 'briefs' / 'A.md'}"])
    # The same worktree a writer's is work that lands.
    model.update_agent(state, "PROBE", lambda held: dataclasses.replace(held, type="writer"))
    assert check(project, "A")[1][:-1] == ["lib/a.py is changed by PROBE (worktree-agent-d1), unlanded"]


def test_a_frozen_review_tree_holds_no_path_and_a_detached_tree_elsewhere_does(
    project: Path, state: Path, tmp_path: Path
) -> None:
    """`diff --tree` checks a commit out for a lens reader, and nothing of it lands; an ending that skipped
    `prune --ending` leaves it registered, so the next session's briefs meet a previous session's tree."""
    earlier = tmp_path / "earlier-state"
    (earlier / store.TREES).mkdir(parents=True)
    (earlier / store.SESSION).write_bytes(b"{}\n")
    frozen = earlier / store.TREES / git(project, "rev-parse", "main")
    git(project, "worktree", "add", "-q", "--detach", str(frozen), "main")
    (frozen / "lib" / "a.py").write_bytes(b"read and left changed\n")
    # A `trees/` folder that holds no session file beside it is no session state's.
    loose = tmp_path / "elsewhere" / store.TREES / "loose"
    git(project, "worktree", "add", "-q", "--detach", str(loose), "main")
    (loose / "docs" / "[id]" / "page.md").write_bytes(b"changed\n")
    briefed(project, "A", owns="lib/a.py")
    assert check(project, "A") == (0, [f"A's brief is checked: {state / 'briefs' / 'A.md'}"])
    briefed(project, "B", owns="docs/[id]/page.md")
    lines = check(project, "B")[1][:-1]
    assert len(lines) == 1, lines
    assert lines[0].startswith("docs/[id]/page.md is changed by ")
    assert lines[0].endswith("/loose, unlanded")


@pytest.mark.skipif(sys.platform != "win32", reason="the 260-character path limit is Windows' alone")
@pytest.mark.usefixtures("state")
def test_a_worktree_whose_paths_pass_windows_limit_changes_none_of_them(project: Path, tmp_path: Path) -> None:
    """`git status` reads such a path as deleted unless `core.longpaths` is set, and exits 0 all the same; an
    agent's worktree sits deeper than the checkout, under `.claude/worktrees/`."""
    # Git for Windows' installer sets `core.fscache` in the system configuration, which the suite leaves out; with
    # it, a path too long to read is reported missing rather than skipped with a warning.
    git(project, "config", "core.fscache", "true")
    # As deep as the checkout takes without long paths, as test_diff's long-path case builds it.
    room = 240 - len(str(project)) - 1
    segments = (room - 12) // 10
    deep = "/".join(["deep12345"] * segments) + "/" + "f" * (room - segments * 10 - 4) + ".txt"
    (project / deep).parent.mkdir(parents=True)
    (project / deep).write_bytes(b"deep\n")
    git(project, "add", deep)
    git(project, "commit", "-m", "deep")
    tree = tmp_path / ("worktree" * 5)
    git(project, "-c", "core.longpaths=true", "worktree", "add", "-q", "--detach", str(tree), "main")
    assert len(str(tree / deep)) > 260, len(str(tree / deep))
    briefed(project, "A", owns=deep)
    code, lines = check(project, "A")
    assert code == 0, lines


def test_a_row_routed_to_a_live_agent_or_in_a_checked_brief_is_refused(project: Path, state: Path) -> None:
    def route(row: str, owner: str) -> None:
        model.write_findings(
            state,
            lambda _: [model.FindingEvent(row, "bank", "OPEN"), model.FindingEvent(row, "route", "ROUTED", owner)],
        )

    briefed(project, "RUN", owns="none")
    model.update_agent(state, "RUN", lambda held: dataclasses.replace(held, status="running"))
    briefed(project, "GONE", owns="none")
    model.update_agent(state, "GONE", lambda held: dataclasses.replace(held, status="landed"))
    route("X-F1", "RUN")
    route("X-F2", "GONE")
    route("X-F3", "owner")
    route("X-F4", "NEXT")
    briefed(project, "NEXT", owns="none", rows="X-F4")
    assert check(project, "NEXT")[0] == 0
    briefed(project, "A", owns="none", rows="X-F1, X-F2, X-F3, X-F4")
    assert check(project, "A")[1][:-1] == [
        "`X-F4` is in NEXT's `Rows:` too (briefed)",
        "`X-F1` is routed to RUN, which is running",
        "`X-F4` is routed to NEXT, which is briefed",
    ]


def test_a_running_record_whose_transcript_ended_holds_no_path_and_no_row(project: Path, state: Path) -> None:
    """Its successor takes its `Owns:` and its rows, as `resume.md` sends a lost agent's work to one."""
    briefed(project, "LOST", owns="lib/**")
    model.update_agent(state, "LOST", lambda held: dataclasses.replace(held, status="running", ids=("l1",)))
    model.write_findings(
        state,
        lambda _: [model.FindingEvent("X-F1", "bank", "OPEN"), model.FindingEvent("X-F1", "route", "ROUTED", "LOST")],
    )
    briefed(project, "NEXT", owns="lib/a.py", rows="X-F1")
    assert check(project, "NEXT")[1][:-1] == [
        "`lib/a.py` overlaps LOST's `lib/**` (running)",
        "`X-F1` is routed to LOST, which is running",
    ]
    # A turn that has just ended may still be refused its stop by the hook, so it holds both until silent.
    transcript(project.parent / "claude-config", "l1", line("assistant", "m1"))
    assert check(project, "NEXT")[0] == 2
    transcript(project.parent / "claude-config", "l1", line("assistant", "m1"), minutes=context.SILENT_MINUTES + 1)
    assert check(project, "NEXT")[0] == 0


@pytest.mark.parametrize(
    ("path", "pattern", "owned"),
    [
        ("lib/a.py", "lib/a.py", True),
        ("lib/a/b.py", "lib/a", True),
        ("lib/a/b.py", "lib/a/", True),
        ("lib/a.py", "lib/*.py", True),
        ("lib/x/y/a.py", "lib/**/a.py", True),
        ("lib/a.py", "lib/**/a.py", True),
        ("lib/x/y/a.py", "lib/**", True),
        ("docs/[id]/page.md", "docs/[id]/*.md", True),
        ("lib\\a.py", "./lib/a.py", True),
        ("lib/a.py", ".\\lib\\a.py", True),
        ("lib/ab.py", "lib/a", False),
        ("lib/x/a.py", "lib/*.py", False),
        ("docs/i/page.md", "docs/[id]/*.md", False),
    ],
)
def test_a_star_matches_within_a_folder_and_two_across_folders(path: str, pattern: str, *, owned: bool) -> None:
    assert brief.owned([pattern], path) is owned


@pytest.mark.parametrize(
    ("one", "other", "overlap"),
    [
        ("tests/fixtures/ci/**", "tests/**", True),
        ("lib/*.py", "lib/x*", True),
        ("lib/**/z.py", "lib/a/*/z.py", True),
        ("docs", "docs/a.md", True),
        ("a/*/c", "a/b/*", True),
        # The automaton reads a leading `./` and a backslash as the plain spelling, so the prefilter must too.
        ("./lib/a.py", "lib/**", True),
        ("lib\\a.py", "lib/a.py", True),
        ("lib/*.py", "lib/a/b.py", False),
        ("docs", "docsx", False),
        ("a/*.md", "a/*.py", False),
    ],
)
def test_two_patterns_overlap_where_one_path_answers_both(one: str, other: str, *, overlap: bool) -> None:
    assert brief.overlap(one, other) is overlap
    assert brief.overlap(other, one) is overlap


def test_a_verb_run_outside_any_checkout_says_where_to_run_it(project: Path) -> None:
    """The state's folders lie outside the checkout, and a coordinator may run a verb from one."""
    assert inprocess("init", "s1", cwd=project).returncode == 0
    briefs = store.state_dir(project) / "briefs"
    done = inprocess("brief", "A", "--check", cwd=briefs)
    assert done.returncode == 2
    assert f"{briefs} is in no git checkout (" in done.stderr
    assert done.stderr.rstrip().endswith("run orchestra from the project's checkout")
