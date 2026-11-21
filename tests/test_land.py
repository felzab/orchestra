"""`land`: an agent's branch merged whole through the configured steps, every refusal before the merge."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import MakeRepo, git
from orchestra import land, log, model, prune
from orchestra.config import Step
from test_prune import TRASH, Fleet, add_agent, commit, make_fleet, ran, route, wait_gone, write
from test_report import text

# Writes `doc.json` from the two sources, so two branches each moving one move different parts of it.
GENERATOR = (
    "from pathlib import Path\n"
    "parts = [Path(f'src/{name}.txt').read_bytes().strip() for name in ('a', 'b')]\n"
    "Path('doc.json').write_bytes(b'{' + b', '.join(parts) + b'}\\n')\n"
)
# Fails while any source names `bad`, as a type checker fails on a merged tree.
CHECKER = (
    "import sys\nfrom pathlib import Path\n"
    "bad = [p.name for p in Path('src').glob('*.txt') if b'bad' in p.read_bytes()]\n"
    "print('fails:', *bad)\nsys.exit(1 if bad else 0)\n"
)
# Records every subject it is handed and refuses one asking to be refused.
COMMIT_MSG_HOOK = (
    "#!/bin/sh\n"
    'subject="$(head -n 1 "$1")"\n'
    'printf "%s\\n" "$subject" >> "$(git rev-parse --git-dir)/hooklog"\n'
    'case "$subject" in *REFUSE*) echo "refused by the fixture hook" >&2; exit 1 ;; esac\n'
)
# Records the message source git hands it, `merge` for a merge's own message (githooks(5)).
PREPARE_HOOK = '#!/bin/sh\nprintf "%s\\n" "$2" >> "$(git rev-parse --git-dir)/sourcelog"\n'
OWNED = "notes.txt, other.txt, docs/, src/, doc.json, lock.txt"


def _step(kind: str, when: list[str], script: str, **more: object) -> dict[str, object]:
    return {"kind": kind, "when": when, "run": [sys.executable, script], **more}


STEPS = [
    _step("install", ["lock.txt"], "tools/install.py"),
    _step("regenerate", ["src/"], "tools/generate.py", writes=["doc.json"]),
    _step("check", ["src/"], "tools/check.py"),
]


@pytest.fixture
def fleet(tmp_path: Path, make_repo: MakeRepo) -> Fleet:
    made = make_fleet(make_repo(tmp_path / "project"), {"land": {"steps": STEPS}})
    hooks = tmp_path / "hooks"
    # Git runs a hook only where its executable bit is set, which a Windows checkout does not need.
    write(hooks, "commit-msg", COMMIT_MSG_HOOK).chmod(0o755)
    write(hooks, "prepare-commit-msg", PREPARE_HOOK).chmod(0o755)
    git(made.root, "config", "core.hooksPath", hooks.as_posix())
    commit(
        made.root,
        "Repo: The project",
        {
            "src/a.txt": '"a": 1',
            "src/b.txt": '"b": 1',
            "doc.json": '{"a": 1, "b": 1}\n',
            "notes.txt": "first\n",
            "lock.txt": "1\n",
            "tools/generate.py": GENERATOR,
            "tools/check.py": CHECKER,
            "tools/install.py": "from pathlib import Path\nPath('installed.txt').write_bytes(b'yes')\n",
        },
    )
    # The install's output is ignored, as an install folder is.
    with (made.root / ".git" / "info" / "exclude").open("ab") as excluded:
        excluded.write(b"installed.txt\n")
    return made


def agent(fleet: Fleet, name: str = "WORKER", ident: str = "a1") -> Path:
    tree = add_agent(fleet, name, ident, "judged")
    brief = fleet.state / "briefs" / f"{name}.md"
    brief.write_bytes(brief.read_bytes().replace(b"Owns: notes.txt, other.txt, docs/", f"Owns: {OWNED}".encode()))
    return tree


def head(fleet: Fleet) -> str:
    return git(fleet.root, "rev-parse", "HEAD")


def stopped(fleet: Fleet, code: int, name: str = "WORKER") -> str:
    """A landing that stopped with `code`: no merge in progress, no change, the session branch where it was."""
    before = head(fleet)
    done, out, err = fleet.run("land", name)
    assert done == code, (done, out, err)
    assert "Traceback" not in err
    assert git(fleet.root, "status", "--porcelain") == ""
    assert not (fleet.root / ".git" / "MERGE_HEAD").exists()
    assert head(fleet) == before, "a stopped landing committed something"
    return err


def test_a_branch_lands_whole_as_one_merge_through_the_hooks_and_its_tree_goes(fleet: Fleet) -> None:
    tree = agent(fleet)
    start = head(fleet)
    commit(tree, "Docs: A note", {"notes.txt": "second\n"})
    tip = commit(tree, "Docs: Another file", {"other.txt": "new\n"})
    code, out, err = fleet.run("land", "WORKER")
    assert code == 0, err
    merge = git(fleet.root, "rev-parse", "--short", "HEAD")
    assert out.splitlines()[0] == f"landed WORKER (worktree-agent-a1) as {merge}"
    assert git(fleet.root, "log", "-1", "--format=%P").split() == [start, tip]
    hooklog = (fleet.root / ".git" / "hooklog").read_bytes().decode().splitlines()
    assert hooklog == ["Merge WORKER into main"]
    # The agent's own commits went through the hook first, each with its `-m` message.
    assert (fleet.root / ".git" / "sourcelog").read_bytes().decode().splitlines()[-1] == "merge"
    # Nothing the agent changed is a step's `when`, so no step ran.
    assert not (fleet.root / "installed.txt").exists()
    assert f"removed {tree} and worktree-agent-a1 (WORKER)" in out
    assert model.read_agent(fleet.state, "WORKER").status == "pruned"
    assert git(fleet.root, "status", "--porcelain") == ""
    wait_gone(fleet.root / ".claude" / "worktrees" / TRASH)


def test_a_landing_closes_only_the_rows_routed_to_its_agent_that_its_report_names_fixed(fleet: Fleet) -> None:
    tree = agent(fleet)
    for row, owner in (("A-F1", "WORKER"), ("A-F2", "OTHER"), ("A-F4", "WORKER"), ("A.2-F11", "WORKER")):
        route(fleet, row, owner)
    model.write_findings(fleet.state, lambda _: [model.FindingEvent("A-F3", "bank", "OPEN")])
    write(fleet.state, "reports/WORKER.md", text("writer", "WORKER", fixed="A.2-F11, A-F2, A-F3."))
    commit(tree, "Docs: A note", {"notes.txt": "second\n"})
    code, out, err = fleet.run("land", "WORKER")
    assert code == 0, err
    merge = git(fleet.root, "rev-parse", "--short", "HEAD")
    rows = model.read_findings(fleet.state)
    assert f"FIXED A.2-F11 by {merge}" in out
    assert (rows["A.2-F11"].status, rows["A.2-F11"].evidence) == ("FIXED", merge)
    assert f"A-F3 is named fixed while OPEN: `orchestra close FIXED {merge} A-F3` if it is" in out
    assert "A-F2 is named fixed and is ROUTED OTHER, so it stands as it is" in out
    assert "still routed to WORKER that no report's `Rows fixed:` line names: A-F1, A-F4" in out
    assert [rows[row].status for row in ("A-F1", "A-F2", "A-F3", "A-F4")] == ["ROUTED", "ROUTED", "OPEN", "ROUTED"]
    # The rows still routed to it keep its tree for a later round.
    assert f"kept {tree} (WORKER): rows are routed to it: A-F1, A-F4" in out
    assert model.read_agent(fleet.state, "WORKER").status == "landed"
    # The register's row ids stay out of the history a push publishes.
    assert not model.ROW_RE.search(git(fleet.root, "log", "--format=%B", "main"))


def test_a_row_routed_back_after_a_landing_is_closed_by_the_latest_report_alone(fleet: Fleet) -> None:
    """A row an earlier round named fixed, routed back to its agent since, stays routed unless the latest round
    names it; a row the earlier landing closed is named again without a line."""
    tree = agent(fleet)
    for row in ("A-F1", "A-F2", "A-F3"):
        route(fleet, row, "WORKER")
    write(fleet.state, "reports/WORKER.md", text("writer", "WORKER", fixed="A-F1, A-F2."))
    commit(tree, "Docs: A note", {"notes.txt": "second\n"})
    # A-F3, still routed to it, keeps its tree for the second round.
    assert fleet.run("land", "WORKER")[0] == 0
    # The coordinator finds A-F1's claim false and routes it back; the agent runs a second round.
    route(fleet, "A-F1", "WORKER")
    model.update_agent(
        fleet.state,
        "WORKER",
        lambda held: replace(held, status="judged", reports={1: "reports/WORKER.md", 2: "reports/WORKER.2.md"}),
    )
    write(fleet.state, "reports/WORKER.2.md", text("writer", "WORKER", round_=2, fixed="A-F2, A-F3."))
    commit(tree, "Docs: Another file", {"other.txt": "new\n"})
    code, out, err = fleet.run("land", "WORKER")
    assert code == 0, err
    rows = model.read_findings(fleet.state)
    assert [rows[row].status for row in ("A-F1", "A-F2", "A-F3")] == ["ROUTED", "FIXED", "FIXED"]
    assert f"FIXED A-F3 by {git(fleet.root, 'rev-parse', '--short', 'HEAD')}" in out
    assert "still routed to WORKER that no report's `Rows fixed:` line names: A-F1" in out
    assert "A-F2" not in out


@pytest.mark.parametrize(
    ("status", "turn", "after"),
    [
        ("judged", "ended", "pruned"),
        ("running", "unseen", "running"),
        ("judged", "mid-turn", "judged"),
        ("reported", "ended", "reported"),
        ("stopped", "ended", "stopped"),
    ],
)
def test_a_landing_credits_every_agent_whose_branch_it_brought_in(
    fleet: Fleet, status: model.AgentStatus, turn: str, after: model.AgentStatus
) -> None:
    """A merge agent's branch carries a fixer's commits: the fixer lands and its row closes unless it still runs or
    its report waits to be judged, a stopped one's that reads included, since that report may still change or be
    refused; its row closes when it lands itself.
    """
    fixer = agent(fleet, "FIXER", "b1")
    model.update_agent(fleet.state, "FIXER", lambda held: replace(held, status=status))
    # A `running` record is kept by its status alone, whatever its transcript shows; one with none found is too.
    # A judged one whose transcript is mid-turn may still be working, though no report of it waits.
    if turn != "unseen":
        ran(fleet, "b1", ended=turn != "mid-turn")
    write(fleet.state, "reports/FIXER.md", text("writer", "FIXER", fixed="A-F1."))
    commit(fixer, "Docs: A note", {"notes.txt": "second\n"})
    merger = agent(fleet, "MERGER", "b2")
    git(merger, "merge", "-q", "--no-ff", "worktree-agent-b1", "-m", "Merge the fixer")
    write(fleet.state, "reports/MERGER.md", text("writer", "MERGER", fixed="A-F2."))
    commit(merger, "Docs: Another file", {"other.txt": "new\n"})
    route(fleet, "A-F1", "FIXER")
    route(fleet, "A-F2", "MERGER")
    code, out, err = fleet.run("land", "MERGER")
    assert code == 0, err
    assert {row: event.status for row, event in model.read_findings(fleet.state).items()} == {
        "A-F1": "FIXED" if after == "pruned" else "ROUTED",
        "A-F2": "FIXED",
    }
    assert [model.read_agent(fleet.state, name).status for name in ("FIXER", "MERGER")] == [after, "pruned"]
    assert out.splitlines()[-1].startswith("landed MERGER, FIXER as ")
    if after == "pruned":
        return
    # Its turn long over and its report judged, the fixer lands by the merge that brought it in, closing its row.
    ran(fleet, "b1", minutes=20)
    model.update_agent(fleet.state, "FIXER", lambda held: replace(held, status="judged"))
    code, out, err = fleet.run("land", "FIXER")
    assert code == 0, out + err
    assert out.splitlines()[-1].startswith("landed FIXER as "), out
    assert model.read_agent(fleet.state, "FIXER").status == "pruned"
    assert model.read_findings(fleet.state)["A-F1"].status == "FIXED", out


def test_a_landing_credits_no_agent_whose_branch_had_landed_before_it(fleet: Fleet) -> None:
    """An agent landed earlier keeps a row for a later round, which another branch's claim must not close."""
    first = agent(fleet, "FIRST", "c1")
    commit(first, "Docs: A note", {"notes.txt": "second\n"})
    route(fleet, "A-F2", "FIRST")
    assert fleet.run("land", "FIRST")[0] == 0
    second = agent(fleet, "SECOND", "c2")
    write(fleet.state, "reports/SECOND.md", text("writer", "SECOND", fixed="A-F2."))
    commit(second, "Docs: Another file", {"other.txt": "new\n"})
    code, out, err = fleet.run("land", "SECOND")
    assert code == 0, err
    assert model.read_findings(fleet.state)["A-F2"].status == "ROUTED"
    assert "A-F2 is named fixed and is ROUTED FIRST, so it stands as it is" in out


def test_a_landing_carries_no_settled_agent_and_looks_up_none_of_their_branches(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pruned agent's branch outlives its record where its prune could not delete it, as a merge agent may carry it.
    Its work was credited when it settled, so a later landing neither credits it again nor asks git for its branch.
    """
    fixer = agent(fleet, "FIXER", "d1")
    write(fleet.state, "reports/FIXER.md", text("writer", "FIXER", fixed="A-F1."))
    commit(fixer, "Docs: A note", {"notes.txt": "second\n"})
    merger = agent(fleet, "MERGER", "d2")
    git(merger, "merge", "-q", "--no-ff", "worktree-agent-d1", "-m", "Merge the fixer")
    commit(merger, "Docs: Another file", {"other.txt": "new\n"})
    model.update_agent(fleet.state, "FIXER", lambda held: replace(held, status="pruned"))
    route(fleet, "A-F1", "FIXER")
    asked: list[str] = []
    real = prune.branch_exists

    def counted(root: Path, branch: str) -> bool:
        asked.append(branch)
        return real(root, branch)

    monkeypatch.setattr(prune, "branch_exists", counted)
    code, out, err = fleet.run("land", "MERGER")
    assert code == 0, err
    assert out.splitlines()[-1].startswith("landed MERGER as "), out
    assert "worktree-agent-d1" not in asked
    assert model.read_findings(fleet.state)["A-F1"].status == "ROUTED"


def test_a_branch_merged_before_its_bookkeeping_is_settled_by_landing_it_again(fleet: Fleet) -> None:
    """A landing stopped after its merge commit, or a merge by hand, leaves rows routed and its agent unlanded;
    landing it ends both. A branch with no commit of its own, behind them, is credited nothing."""
    agent(fleet, "EMPTY", "e1")
    route(fleet, "A-F3", "EMPTY")
    tree = agent(fleet)
    write(fleet.state, "reports/WORKER.md", text("writer", "WORKER", fixed="A-F1, A-F3."))
    commit(tree, "Docs: A note", {"notes.txt": "second\n"})
    route(fleet, "A-F1", "WORKER")
    git(fleet.root, "merge", "-q", "--no-ff", "worktree-agent-a1", "-m", "Merge by hand")
    git(fleet.root, "commit", "-q", "--allow-empty", "-m", "Repo: A later landing")
    merge = git(fleet.root, "rev-parse", "--short", "HEAD^")
    before = head(fleet)
    code, out, err = fleet.run("land", "WORKER")
    assert code == 0, err
    assert out.splitlines()[0] == f"worktree-agent-a1 was merged already, as {merge}: its bookkeeping is done now"
    assert model.read_findings(fleet.state)["A-F1"].evidence == merge
    assert model.read_agent(fleet.state, "WORKER").status == "pruned"
    assert head(fleet) == before
    code, out, err = fleet.run("land", "EMPTY")
    assert code == 0, err
    assert out.splitlines()[-1] == "nothing to land: the session branch already holds worktree-agent-e1"
    assert model.read_findings(fleet.state)["A-F3"].status == "ROUTED"


def test_a_branch_fast_forwarded_onto_another_agents_landing_has_nothing_to_land(fleet: Fleet) -> None:
    """A branch with no commit of its own that took in the session branch, as an agent merges it into its own,
    fast-forwards onto another agent's merge: that merge is no landing of its, so its claim closes nothing."""
    late = agent(fleet, "LATE", "g1")
    tree = agent(fleet)
    commit(tree, "Docs: A note", {"notes.txt": "second\n"})
    assert fleet.run("land", "WORKER")[0] == 0
    git(late, "merge", "-q", "main")
    assert git(late, "rev-parse", "HEAD") == head(fleet)
    write(fleet.state, "reports/LATE.md", text("writer", "LATE", fixed="A-F1."))
    route(fleet, "A-F1", "LATE")
    code, out, err = fleet.run("land", "LATE")
    assert code == 0, err
    assert out.splitlines()[-1] == "nothing to land: the session branch already holds worktree-agent-g1", out
    assert model.read_findings(fleet.state)["A-F1"].status == "ROUTED"


def test_a_round_stopped_before_its_report_with_nothing_to_land_is_not_stamped_by_the_round_before(
    fleet: Fleet,
) -> None:
    """A resume and a limit's stop each keep the outcome the round before reported, so only a judged record's
    outcome is its latest round's word, and a stopped round that delivered nothing frees no slice."""
    agent(fleet)
    write(fleet.state, "reports/WORKER.md", text("writer", "WORKER"))
    resumed = {1: "2026-11-21T08:00:00+00:00", 2: "2026-11-21T09:00:00+00:00"}
    model.update_agent(
        fleet.state,
        "WORKER",
        lambda held: replace(held, status="stopped", outcome="DONE", reports=resumed, reason="You've hit your limit"),
    )
    code, out, err = fleet.run("land", "WORKER")
    assert code == 0, err
    assert out.splitlines()[-1] == "nothing to land: the session branch already holds worktree-agent-a1", out
    assert "landed" not in model.read_agent(fleet.state, "WORKER").since
    assert not [entry for entry in log.entries(fleet.state) if entry.kind == "land"]


def test_a_record_a_hook_changes_while_the_landing_runs_its_git_keeps_what_the_hook_wrote(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp is decided on the record read before the preflight's git commands, as a resume may land meanwhile."""
    agent(fleet)
    write(fleet.state, "reports/WORKER.md", text("writer", "WORKER"))
    model.update_agent(fleet.state, "WORKER", lambda held: replace(held, outcome="DONE"))
    checked = land.preflight

    def resumed(root: Path, branch: str) -> None:
        model.update_agent(fleet.state, "WORKER", lambda held: replace(held, status="running"))
        checked(root, branch)

    monkeypatch.setattr(land, "preflight", resumed)
    code, out, err = fleet.run("land", "WORKER")
    assert code == 0, err
    assert out.splitlines()[-1] == "nothing to land: the session branch already holds worktree-agent-a1", out
    held = model.read_agent(fleet.state, "WORKER")
    assert (held.status, "landed" in held.since) == ("running", False)


def test_a_merge_touching_a_steps_paths_installs_regenerates_and_checks(fleet: Fleet) -> None:
    tree = agent(fleet)
    commit(tree, "Docs: The source grows", {"src/a.txt": '"a": 2', "lock.txt": "2\n"})
    code, out, err = fleet.run("land", "WORKER")
    assert code == 0, err
    assert (fleet.root / "installed.txt").exists()
    assert git(fleet.root, "show", "HEAD:doc.json") == '{"a": 2, "b": 1}'
    said = out.splitlines()[0]
    assert said.index("install") < said.index("regenerate") < said.index("check"), said


def test_a_conflict_in_a_regenerated_file_is_answered_by_regenerating_it(fleet: Fleet) -> None:
    """Each side regenerated its own document, so the documents conflict while the sources merge clean."""
    tree = agent(fleet)
    commit(tree, "Docs: b grows", {"src/b.txt": '"b": 2', "doc.json": '{"a": 1, "b": 2}\n'})
    commit(fleet.root, "Docs: a grows", {"src/a.txt": '"a": 2', "doc.json": '{"a": 2, "b": 1}\n'})
    code, _, err = fleet.run("land", "WORKER")
    assert code == 0, err
    assert git(fleet.root, "show", "HEAD:doc.json") == '{"a": 2, "b": 2}'
    # The conflict's lines git adds to the merge's message are left out of the commit.
    assert git(fleet.root, "log", "-1", "--format=%B") == "Merge WORKER into main"


def test_a_conflict_no_regeneration_answers_aborts_naming_who_else_changes_the_path(fleet: Fleet) -> None:
    tree = agent(fleet)
    commit(tree, "Docs: A note", {"notes.txt": "agent\n"})
    other = agent(fleet, "OTHER", "d2")
    commit(other, "Docs: Another note", {"notes.txt": "other\n"})
    commit(fleet.root, "Docs: The session's note", {"notes.txt": "session\n"})
    said = stopped(fleet, 3)
    assert "CONFLICT in notes.txt" in said
    assert "the merge is aborted" in said
    assert "notes.txt also changed by OTHER (worktree-agent-d2)" in said
    assert "merges the session branch into its own branch" in said


def test_a_failing_regeneration_aborts_the_merge(fleet: Fleet) -> None:
    tree = agent(fleet)
    commit(tree, "Docs: The source breaks", {"src/a.txt": "", "src/b.txt": '"b": 2'})
    write(fleet.root, "tools/generate.py", "raise SystemExit(3)\n")
    git(fleet.root, "commit", "-qam", "Repo: The generator breaks")
    assert "exited 3 on the merged tree" in stopped(fleet, 4)


def test_a_merged_tree_failing_a_check_aborts_the_merge(fleet: Fleet) -> None:
    tree = agent(fleet)
    commit(tree, "Docs: A bad source", {"src/a.txt": '"a": "bad"'})
    said = stopped(fleet, 5)
    assert "tools/check.py` in . exited 1 on the merged tree:" in said
    assert "fails: a.txt" in said


def test_a_check_the_session_branch_already_fails_is_its_own_exit(fleet: Fleet) -> None:
    """A red already at HEAD is no fault of the branch, so no landing is sent back to an agent no rebase helps."""
    tree = agent(fleet)
    commit(tree, "Docs: A clean source", {"src/a.txt": '"a": 3'})
    commit(fleet.root, "Docs: A bad source", {"src/b.txt": '"b": "bad"'})
    said = stopped(fleet, 8)
    assert "the session branch already fails" in said
    assert "fails: b.txt" in said


def test_a_step_whose_program_is_found_and_cannot_start_is_a_failed_step(tmp_path: Path) -> None:
    """A file with no execute bit, no shebang or no program Windows runs directly is found, and fails to start.

    The landing's abort on a failed step is held by the other exit-5 cases.
    """
    (tmp_path / "check.txt").write_bytes(b"no program\n")
    with pytest.raises(land.Stop) as stopped_at:
        land.run_step(tmp_path, Step("check", ("src/",), ("./check.txt",)))
    assert stopped_at.value.code == land.FAILED["check"]
    assert stopped_at.value.lines[0].startswith("`./check.txt` could not run: ")


def test_a_step_writing_a_tracked_file_it_does_not_declare_aborts_the_merge(fleet: Fleet) -> None:
    tree = agent(fleet)
    write(fleet.root, "tools/check.py", "from pathlib import Path\nPath('notes.txt').write_bytes(b'checked')\n")
    git(fleet.root, "commit", "-qam", "Repo: A checker that writes")
    commit(tree, "Docs: The source grows", {"src/a.txt": '"a": 2'})
    assert "a step wrote tracked files no `writes` names: notes.txt" in stopped(fleet, 5)


def test_a_merge_commit_the_hooks_refuse_aborts_the_merge(fleet: Fleet) -> None:
    # The merge commit's subject names the agent, which asks the fixture hook to refuse it.
    tree = agent(fleet, "REFUSE")
    commit(tree, "Docs: A note", {"notes.txt": "second\n"})
    assert "refused by the fixture hook" in stopped(fleet, 6, "REFUSE")


def test_git_failing_during_the_merge_is_its_own_exit(fleet: Fleet) -> None:
    """Exit 2 says nothing was started; a failure once the merge has begun must not read as that.

    The branch deletes the regenerated document and the generator writes nothing, so staging it names no file.
    """
    write(fleet.root, "tools/generate.py", "")
    git(fleet.root, "commit", "-qam", "Repo: The generator writes nothing")
    tree = agent(fleet)
    git(tree, "rm", "-q", "doc.json")
    commit(tree, "Docs: No document", {"src/a.txt": '"a": 2'})
    said = stopped(fleet, 7)
    assert "git add -A -- doc.json exited" in said


def _refused(fleet: Fleet, name: str = "WORKER") -> str:
    before = head(fleet)
    code, _, err = fleet.run("land", name)
    assert code == 2, err
    assert head(fleet) == before, "a refused landing committed something"
    return err


def test_another_agents_record_that_does_not_read_refuses_the_landing_before_its_merge(fleet: Fleet) -> None:
    """The bookkeeping reads every record, so one that does not read would stop it after the merge commit."""
    commit(agent(fleet), "Docs: A note", {"notes.txt": "second\n"})
    write(fleet.state, "agents/BROKEN.json", "{")
    assert "BROKEN.json" in _refused(fleet)


def test_a_checkout_holding_changes_is_refused(fleet: Fleet) -> None:
    commit(agent(fleet), "Docs: A note", {"notes.txt": "second\n"})
    write(fleet.root, "stray.txt", "uncommitted\n")
    assert "would take in: stray.txt; land from a clean tree" in _refused(fleet)


def test_an_agent_still_at_work_is_refused_and_one_whose_turn_ended_lands(fleet: Fleet) -> None:
    """The transcript decides whatever the record reads.

    A takeover leaves a live agent `stopped`, which holds nothing back once its transcript ends its turn
    and is settled by the landing: a row still routed to it keeps its tree, so `landed` is the landing's
    own word, not the prune's.
    """
    commit(agent(fleet), "Docs: A note", {"notes.txt": "second\n"})
    route(fleet, "A-F1", "WORKER")
    model.update_agent(fleet.state, "WORKER", lambda held: replace(held, status="stopped"))
    ran(fleet, "a1", ended=False)
    assert "WORKER may still be working (its transcript is mid-turn" in _refused(fleet)
    ran(fleet, "a1", ended=True)
    code, _, err = fleet.run("land", "WORKER")
    assert code == 0, err
    assert model.read_agent(fleet.state, "WORKER").status == "landed"


def test_an_agent_not_yet_judged_is_refused_naming_what_settles_it(fleet: Fleet) -> None:
    """A landed agent's report can no longer be judged, so its findings would never be banked."""
    commit(agent(fleet), "Docs: A note", {"notes.txt": "second\n"})
    ran(fleet, "a1")
    for status in ("briefed", "running"):
        model.update_agent(fleet.state, "WORKER", lambda held, status=status: replace(held, status=status))
        assert f"WORKER is {status} and has not reported: land it once its report is judged" in _refused(fleet)
    model.update_agent(fleet.state, "WORKER", lambda held: replace(held, status="reported", outcome="DONE"))
    said = "WORKER's round 1 report is not judged: `orchestra judge WORKER` banks its findings first"
    assert said in _refused(fleet)
    model.update_agent(fleet.state, "WORKER", lambda held: replace(held, outcome="BLOCKED"))
    assert "WORKER stopped BLOCKED: answer it by message, and judge the report it writes" in _refused(fleet)
    # A stopped agent's report that reads is judged too; one that does not read holds nothing back.
    write(fleet.state, "reports/WORKER.md", text("writer", "WORKER"))
    model.update_agent(fleet.state, "WORKER", lambda held: replace(held, status="stopped", reason="usage limit"))
    said = "WORKER is stopped (usage limit), and its round 1 report reads: `orchestra judge WORKER` banks"
    assert f"{said} its findings first" in _refused(fleet)
    model.update_agent(fleet.state, "WORKER", lambda held: replace(held, status="judged"))
    code, _, err = fleet.run("land", "WORKER")
    assert code == 0, err


def test_a_merge_left_in_progress_is_refused_naming_its_abort(fleet: Fleet) -> None:
    """Committing it would conclude a merge whose steps never ran, so the refusal names the abort, not a commit."""
    commit(agent(fleet), "Docs: A note", {"notes.txt": "second\n"})
    git(fleet.root, "merge", "-q", "--no-ff", "--no-commit", "worktree-agent-a1")
    assert "`git merge --abort`" in _refused(fleet)


def test_uncommitted_work_in_the_agents_worktree_is_refused(fleet: Fleet) -> None:
    tree = agent(fleet)
    commit(tree, "Docs: A note", {"notes.txt": "second\n"})
    write(tree, "unsaved.txt", "work\n")
    assert "holds uncommitted work" in _refused(fleet)


def test_a_stash_entry_on_the_branch_is_refused(fleet: Fleet) -> None:
    """git lists a named entry as "On <branch>:" and an unnamed one as "WIP on <branch>:"."""
    tree = agent(fleet)
    commit(tree, "Docs: A note", {"notes.txt": "second\n"})
    for named in (("-m", "parked"), ()):
        write(tree, "notes.txt", "stashed\n")
        git(tree, "stash", "push", "-q", *named)
        assert "a stash entry is on worktree-agent-a1" in _refused(fleet), named
        git(tree, "stash", "drop", "-q")


def test_a_branch_carrying_a_patch_the_session_already_holds_is_refused(fleet: Fleet) -> None:
    tree = agent(fleet)
    picked = commit(tree, "Docs: A note", {"notes.txt": "second\n"})
    commit(tree, "Docs: Another file", {"other.txt": "new\n"})
    # `-x` marks the copy, which would otherwise be the very same commit when made within the same second.
    git(fleet.root, "cherry-pick", "-x", picked)
    said = _refused(fleet)
    assert "1 commit(s) whose patch the session branch already holds" in said
    assert "Docs: A note" in said
    assert "Docs: Another file" not in said


def test_a_branch_changing_a_path_outside_its_owns_is_refused_naming_each(fleet: Fleet) -> None:
    tree = agent(fleet)
    commit(tree, "Docs: Strays", {"notes.txt": "second\n", "stray.txt": "x\n", "tools/x.py": "x\n"})
    said = _refused(fleet)
    assert "changes 2 path(s) its brief's `Owns:` does not name:" in said
    assert "stray.txt" in said
    assert "tools/x.py" in said
    assert "notes.txt" not in said


def test_the_preview_names_what_the_landing_would_take_and_changes_nothing(fleet: Fleet) -> None:
    tree = agent(fleet)
    first = commit(tree, "Docs: A note", {"notes.txt": "second\n"})
    commit(tree, "Docs: Strays", {"other.txt": "new\n", "stray.txt": "x\n"})
    write(tree, "unsaved.txt", "work\n")
    before = head(fleet)
    code, out, err = fleet.run("land", "WORKER", "--preview")
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0] == f"WORKER: judged, branch worktree-agent-a1, report {fleet.state / 'reports' / 'WORKER.md'}"
    assert f"{tree}: 1 uncommitted file(s) git does not ignore" in lines
    assert "not merged into the session branch" in lines
    assert "outside its `Owns:`: stray.txt" in lines
    assert f"{first[:7]} Docs: A note (1 file(s))" in lines
    assert any(line.startswith(" stray.txt") for line in lines)
    assert lines[-1] == "WORKER: 2 commit(s) to land, 1 path(s) outside its `Owns:`"
    assert head(fleet) == before
    assert model.read_agent(fleet.state, "WORKER").status == "judged"
