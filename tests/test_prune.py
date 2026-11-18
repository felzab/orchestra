"""`prune`: each agent's tree judged by its record, its transcript, its rows and its branch, removed without a wait."""

from __future__ import annotations

import stat
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from conftest import LIB, MakeRepo, git, inprocess, line, transcript, write_agent, write_config
from orchestra import model, prune, queue, store
from test_report import text as report_text

IGNORED = b".claude/worktrees/\n.claude/settings.local.json\n__pycache__/\nnode_modules/\n.env*\n"
TRASH = ".orchestra-trash"
# What every fixture brief owns.
OWNS = "notes.txt, other.txt, docs/"
# A deleter's work is done in seconds; this bound only stops a case that would otherwise hang.
GONE_SECONDS = 120
# Sets the named agent's record `reported` in the given state, as the stop hook does when its report arrives.
REPORTS = (
    "import sys\nfrom dataclasses import replace\nfrom pathlib import Path\nfrom orchestra import model\n"
    "model.update_agent(Path(sys.argv[1]), sys.argv[2], lambda held: replace(held, status='reported'))\n"
)


@dataclass(frozen=True)
class Fleet:
    root: Path
    state: Path

    def run(self, *args: str) -> tuple[int, str, str]:
        done = inprocess(*args, cwd=self.root)
        return done.returncode, done.stdout, done.stderr

    def tree(self, ident: str) -> Path:
        return self.root / ".claude" / "worktrees" / f"agent-{ident}"


def make_fleet(root: Path, settings: dict[str, object] | None = None) -> Fleet:
    """The repository at `root` with its configuration committed, and a session made on it."""
    write_config(root, {"stateRoot": (root.parent / "state").as_posix(), **(settings or {})})
    (root / ".gitignore").write_bytes(IGNORED)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Repo: The configuration")
    done = inprocess("init", "s", cwd=root)
    assert done.returncode in (0, 1), done.stderr
    return Fleet(root, store.state_dir(root))


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def commit(root: Path, subject: str, files: dict[str, str]) -> str:
    for rel, text in files.items():
        write(root, rel, text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "--no-verify", "-m", subject)
    return git(root, "rev-parse", "HEAD")


def add_agent(
    fleet: Fleet,
    name: str,
    ident: str,
    status: model.AgentStatus = "judged",
    files: dict[str, str] | None = None,
) -> Path:
    """An agent with its record, its brief, and a worktree on its own branch holding `files` as one commit."""
    branch = f"worktree-agent-{ident}"
    tree = fleet.tree(ident)
    git(fleet.root, "worktree", "add", "-q", "-b", branch, str(tree))
    if files:
        commit(tree, f"Docs: {name}'s work", files)
    write_agent(fleet.state, model.Agent(name, "writer", status=status, ids=(ident,), branch=branch))
    header = (
        f"Agent: {name}            Type: writer            Slice: S1\nBase: main at 0000000\n"
        f"Report: {fleet.state / 'reports' / f'{name}.md'}\nOwns: {OWNS}\nRows: none\nWrites: none\nLens: none\n"
    )
    write(
        fleet.state, f"briefs/{name}.md", header + "\nGOAL      x\nWORK      1. x Accept: x\nPREMISES  x\nTRAPS     x\n"
    )
    return tree


def ran(
    fleet: Fleet,
    ident: str,
    *,
    ended: bool = True,
    meta: dict[str, object] | None = None,
    minutes: float | None = None,
) -> None:
    """The harness's transcript of an agent, `minutes` old: its last message calls no tool where it `ended`."""
    lines = (
        line("user"),
        line("assistant", "m1", calls=not ended),
        # The line a usage limit writes, which is no turn of the agent's.
        line("assistant", "m2", model="<synthetic>"),
    )
    transcript(fleet.root.parent / "claude-config", ident, *lines, meta=meta, minutes=minutes)


def route(fleet: Fleet, row: str, owner: str) -> None:
    model.write_findings(fleet.state, lambda _: [model.FindingEvent(row, "route", "ROUTED", owner=owner)])


def wait_gone(path: Path) -> None:
    deadline = time.monotonic() + GONE_SECONDS
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not path.exists(), f"{path} is still there after {GONE_SECONDS} s: {list(path.rglob('*'))[:5]}"


def branches(fleet: Fleet) -> list[str]:
    return git(fleet.root, "for-each-ref", "--format=%(refname:short)", "refs/heads/worktree-agent-*").split()


@pytest.fixture
def fleet(tmp_path: Path, make_repo: MakeRepo) -> Iterator[Fleet]:
    made = make_fleet(make_repo(tmp_path / "project"))
    yield made
    # A case that failed midway can leave a deleter running; its trash is waited out, never left to the next case.
    for trash in (made.root / ".claude" / "worktrees" / TRASH, made.state / "trees" / TRASH):
        if trash.exists():
            wait_gone(trash)


def test_each_agent_is_judged_by_its_record_its_rows_its_branch_and_its_tree(fleet: Fleet) -> None:
    """Landed and commit-less agents go whatever ignored files they hold; every other tree stays, its reason named."""
    landed = add_agent(fleet, "LANDED", "a1", "landed", {"notes.txt": "landed\n"})
    git(fleet.root, "merge", "-q", "--no-ff", "worktree-agent-a1", "-m", "Merge LANDED")
    # An install, bytecode and a secret file are ignored: the work is committed, so none of them keeps a tree.
    write(landed, "node_modules/x/index.js", "x")
    write(landed, ".env.local", "A=1\n")
    git(fleet.root, "worktree", "lock", "--reason", "claude agent agent-a1 (pid 1)", str(landed))
    empty = add_agent(fleet, "EMPTY", "b2", "judged")
    # A record naming no branch is read through the harness's own file.
    model.update_agent(fleet.state, "EMPTY", lambda held: replace(held, branch=None))
    ran(fleet, "b2", meta={"worktreeBranch": "worktree-agent-b2"})
    running = add_agent(fleet, "RUNNING", "c3", "running")
    ran(fleet, "c3", ended=False)
    unlanded = add_agent(fleet, "UNLANDED", "d4", "judged", {"other.txt": "work\n"})
    routed = add_agent(fleet, "ROUTED", "e5", "judged")
    route(fleet, "AUDIT-F1", "ROUTED")
    dirty = add_agent(fleet, "DIRTY", "f6", "judged")
    write(dirty, "unsaved.txt", "work\n")
    resumed = add_agent(fleet, "RESUMED", "a7", "judged")
    ran(fleet, "a7", ended=False)
    stranger = fleet.root.parent / "elsewhere"
    git(fleet.root, "worktree", "add", "-q", "-b", "elsewhere", str(stranger))

    code, out, err = fleet.run("prune")

    assert code == 0, err
    assert f"removed {landed} and worktree-agent-a1 (LANDED)" in out
    assert f"removed {empty} and worktree-agent-b2 (EMPTY)" in out
    # A crashed session's agent reads as working until the time named, so the coordinator waits for it.
    assert (
        f"kept {running} (RUNNING): its transcript is mid-turn, written 0 min ago;"
        " it counts as ended once silent past 15 min, at " in out
    )
    assert f"kept {unlanded} (UNLANDED): 1 commit(s) of it are not on main" in out
    assert f"kept {routed} (ROUTED): rows are routed to it: AUDIT-F1" in out
    assert f"kept {dirty} (DIRTY): 1 uncommitted file(s) git does not ignore" in out
    assert f"kept {resumed} (RESUMED): its transcript is mid-turn, written 0 min ago" in out
    assert f"left {stranger} (elsewhere): no agent of this session holds it" in out
    assert out.splitlines()[-1] == "prune: removed 2, kept 5"
    assert not landed.exists()
    assert not empty.exists()
    assert all(tree.exists() for tree in (running, unlanded, routed, dirty, resumed, stranger))
    assert branches(fleet) == [f"worktree-agent-{ident}" for ident in ("a7", "c3", "d4", "e5", "f6")]
    assert "agent-a1" not in git(fleet.root, "worktree", "list", "--porcelain")
    assert [model.read_agent(fleet.state, name).status for name in ("LANDED", "EMPTY")] == ["pruned", "pruned"]
    wait_gone(fleet.root / ".claude" / "worktrees" / TRASH)


def test_the_caller_returns_before_a_large_tree_is_gone(fleet: Fleet) -> None:
    """The rename is all the caller waits on; the deleting runs on in a process of its own."""
    tree = add_agent(fleet, "BIG", "b1", "landed")
    for k in range(3000):
        write(tree, f"node_modules/p{k % 30}/f{k}.js", "x")

    code, out, err = fleet.run("prune")

    trash = fleet.root / ".claude" / "worktrees" / TRASH
    # Read before anything else: a caller that deleted the tree itself would find no entry here.
    held = [entry.name for entry in trash.iterdir()] if trash.is_dir() else []
    assert code == 0, err
    assert not tree.exists()
    assert len(held) == 1, held
    assert held[0].startswith("agent-b1-"), held
    assert f"deleting 1 folder(s) in the background from {trash}" in out
    wait_gone(trash)


@pytest.mark.skipif(sys.platform != "win32", reason="POSIX unlinks a read-only file whose folder is writable")
def test_a_read_only_file_in_a_tree_is_deleted(fleet: Fleet) -> None:
    """Windows refuses to delete a read-only file until its bit is cleared, which git's own pack files carry."""
    tree = add_agent(fleet, "LOCKED", "c1", "landed")
    path = write(tree, "node_modules/x/read-only.js", "x")
    path.chmod(stat.S_IREAD)
    code, _, err = fleet.run("prune")
    assert code == 0, err
    trash = fleet.root / ".claude" / "worktrees" / TRASH
    wait_gone(trash)
    assert not tree.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to rename a folder holding an open file")
def test_a_tree_a_process_holds_a_file_in_is_kept_as_in_use(fleet: Fleet) -> None:
    tree = add_agent(fleet, "BUSY", "d1", "landed")
    with (tree / "README").open("rb"):
        code, out, err = fleet.run("prune")
    assert code == 1, (out, err)
    assert f"kept {tree} (BUSY): in use, a process holds a file in it" in out
    assert tree.exists()
    assert branches(fleet) == ["worktree-agent-d1"]
    assert model.read_agent(fleet.state, "BUSY").status == "landed"


def _orphan(fleet: Fleet, name: str, ident: str, status: model.AgentStatus) -> Path:
    """An agent's folder git no longer lists: a removal stopped part-way, its `.git` link gone."""
    tree = add_agent(fleet, name, ident, status)
    (tree / ".git").unlink()
    git(fleet.root, "worktree", "prune")
    return tree


def test_a_folder_git_no_longer_lists_goes_unless_its_agent_runs(fleet: Fleet) -> None:
    gone = _orphan(fleet, "HALF", "e1", "judged")
    live = _orphan(fleet, "LIVE", "e2", "running")
    ran(fleet, "e2", ended=False)
    unknown = fleet.tree("e3")
    write(unknown, "file.txt", "x")
    code, out, err = fleet.run("prune")
    assert code == 0, err
    assert f"removed {gone} and worktree-agent-e1 (HALF)" in out
    assert f"kept {live} (LIVE): its transcript is mid-turn, written 0 min ago" in out
    assert f"left {unknown} git lists none: no agent of this session holds it" in out
    assert not gone.exists()
    assert live.exists()
    assert unknown.exists()


def test_the_frozen_trees_of_lens_rounds_go_at_the_ending(fleet: Fleet) -> None:
    head = git(fleet.root, "rev-parse", "HEAD")
    frozen = fleet.state / "trees" / head
    git(fleet.root, "worktree", "add", "-q", "--detach", str(frozen), head)
    code, out, _ = fleet.run("prune")
    assert code == 0
    assert f"left {frozen} detached at {head[:9]}" in out
    assert frozen.exists()
    code, out, err = fleet.run("prune", "--ending")
    assert code == 0, err
    assert f"removed {frozen}: the frozen tree of a lens round" in out
    assert out.splitlines()[-1] == "prune: removed 1, kept 0"
    assert not frozen.exists()
    assert str(frozen.as_posix()) not in git(fleet.root, "worktree", "list", "--porcelain")
    wait_gone(fleet.state / "trees" / TRASH)


def test_trash_a_deleter_gave_up_on_is_named_and_deleted_again(fleet: Fleet) -> None:
    trash = fleet.root / ".claude" / "worktrees" / TRASH
    left = write(trash, "agent-f1-0000/file.txt", "x").parent
    (trash / "agent-f1-0000.failed").write_bytes(b"PermissionError: held\n")
    code, out, _ = fleet.run("prune")
    assert code == 1
    assert f"{left} could not be deleted (PermissionError: held): deleting it again" in out
    wait_gone(trash)


def test_a_junction_in_a_tree_is_unlinked_and_its_target_left_whole(fleet: Fleet) -> None:
    """A junction into the primary checkout's installs is unlinked, never emptied (shutil.rmtree, Python 3.8)."""
    # A skip inside the case, so the type checker reads the Windows-only import below as Windows code.
    if sys.platform != "win32":
        pytest.skip("a junction is Windows' own link")
    import _winapi  # noqa: PLC0415

    target = write(fleet.root.parent, "installs/kept.js", "x").parent
    tree = add_agent(fleet, "LINKED", "g1", "landed")
    _winapi.CreateJunction(str(target), str(tree / "node_modules"))
    code, _, err = fleet.run("prune")
    assert code == 0, err
    wait_gone(fleet.root / ".claude" / "worktrees" / TRASH)
    assert (target / "kept.js").exists()


def test_a_link_whose_unlink_is_refused_is_removed_and_its_target_left_as_it_was(tmp_path: Path) -> None:
    """The retry clears a read-only bit, and a link's bit read through the link is its target's, outside the tree."""
    target = tmp_path / "outside"
    target.mkdir()
    target.chmod(stat.S_IREAD | stat.S_IEXEC)
    tree = tmp_path / "tree"
    folder = tree / "folder"
    folder.mkdir(parents=True)
    link = folder / "link"
    if sys.platform == "win32":
        import _winapi  # noqa: PLC0415

        _winapi.CreateJunction(str(target), str(link))
        # Windows refuses to remove a read-only folder entry, a junction's included. It takes `follow_symlinks`
        # only from Python 3.13, and before it its `chmod` sets the junction's own bit.
        if sys.version_info >= (3, 13):
            link.chmod(stat.S_IREAD, follow_symlinks=False)
        else:
            link.chmod(stat.S_IREAD)
        assert link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    else:
        link.symlink_to(target)
        # POSIX refuses to unlink an entry of a folder its owner cannot write.
        folder.chmod(stat.S_IREAD | stat.S_IEXEC)
    before = target.stat()
    assert prune.delete(tree) is None
    after = target.stat()
    assert not tree.exists()
    assert (after.st_mode, getattr(after, "st_file_attributes", 0)) == (
        before.st_mode,
        getattr(before, "st_file_attributes", 0),
    )
    target.chmod(stat.S_IRWXU)


@pytest.mark.skipif(sys.platform != "win32", reason="the long-path prefix is Windows' own")
def test_a_path_past_windows_limit_is_spelled_in_the_form_windows_takes_whatever_its_setting() -> None:
    """A machine with long paths on deletes a deep tree either way, so the form itself is what shows it."""
    assert prune.long_form(Path("C:/x/y")) == "\\\\?\\C:\\x\\y"
    assert prune.long_form(Path("//server/share/x")) == "\\\\?\\UNC\\server\\share\\x"
    assert prune.long_form(Path("\\\\?\\C:\\x")) == "\\\\?\\C:\\x"


@pytest.mark.skipif(sys.platform != "win32", reason="the long-path prefix is Windows' own")
def test_a_name_only_the_long_form_spells_is_deleted(tmp_path: Path) -> None:
    """A trailing dot is dropped from any other spelling, so only `delete`'s own call of the long form removes it."""
    tree = tmp_path / "tree"
    odd = Path(prune.long_form(tree / "dotted."))
    odd.mkdir(parents=True)
    (odd / "file.").write_bytes(b"x")
    assert prune.delete(tree) is None
    assert not Path(prune.long_form(tree)).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="the long-path prefix is Windows' own")
def test_a_path_past_windows_limit_is_deleted(fleet: Fleet) -> None:
    tree = add_agent(fleet, "DEEP", "h1", "landed")
    deep = tree / "node_modules"
    while len(str(deep)) < 300:
        deep = deep / "a-package-nested-in-another"
    # Windows takes a path past its limit through this prefix, whatever the machine's setting.
    Path("\\\\?\\" + str(deep)).mkdir(parents=True)
    Path("\\\\?\\" + str(deep / "f.js")).write_bytes(b"x")
    code, _, err = fleet.run("prune")
    assert code == 0, err
    wait_gone(fleet.root / ".claude" / "worktrees" / TRASH)
    assert not tree.exists()


def test_two_spellings_of_one_tree_name_one_tree(tmp_path: Path) -> None:
    """Claude's Windows app redirects a folder under AppData, so git may list a tree the state names otherwise."""
    real = tmp_path / "tree"
    real.mkdir()
    if sys.platform == "win32":
        # The long-path form spells the same folder with no link, which needs no privilege to make.
        other = Path("\\\\?\\" + str(real))
    else:
        other = tmp_path / "other"
        other.symlink_to(real)
    assert prune.strangers([prune.Tree(other)], {prune.Tree(real).key}) == []


def test_a_stopped_writer_with_no_tree_and_no_branch_left_is_settled(fleet: Fleet) -> None:
    """It can never be resumed, so it would stand under "to resume" until the session ends."""
    gone = add_agent(fleet, "GONE", "g1", "stopped")
    ran(fleet, "g1")
    git(fleet.root, "worktree", "remove", "--force", str(gone))
    git(fleet.root, "branch", "-D", "worktree-agent-g1")
    write_agent(fleet.state, model.Agent("READER", "auditor", status="stopped"))
    # A tree outside the worktrees folder, whose branch neither the record nor the harness names, may hold work.
    lost = fleet.root.parent / "elsewhere-s1"
    git(fleet.root, "worktree", "add", "-q", "-b", "worktree-agent-s1", str(lost))
    commit(lost, "Docs: LOST's work", {"other.txt": "work\n"})
    write_agent(fleet.state, model.Agent("LOST", "writer", status="stopped", ids=("s1",)))
    ran(fleet, "s1")
    code, out, err = fleet.run("prune")
    assert code == 0, err
    assert "settled GONE: nothing of it is left" in out
    assert model.read_agent(fleet.state, "GONE").status == "pruned"
    # A reader runs in the coordinator's checkout and has no tree to lose: a resume still reaches it.
    assert model.read_agent(fleet.state, "READER").status == "stopped"
    assert "kept its record (LOST): its branch is unknown; `orchestra prune --discard LOST` settles it" in out
    assert model.read_agent(fleet.state, "LOST").status == "stopped"
    assert lost.exists()
    # Settling GONE's record removed nothing.
    assert out.splitlines()[-1] == "prune: removed 0, kept 1"


def test_discard_removes_one_agents_tree_and_branch_whatever_they_hold(fleet: Fleet) -> None:
    """The coordinator saved what it keeps; plain prune would keep both for the commit and the file."""
    dead = add_agent(fleet, "DEAD", "h1", "stopped", {"other.txt": "work\n"})
    write(dead, "unsaved.txt", "work\n")
    ran(fleet, "h1")
    other = add_agent(fleet, "OTHER", "h2", "judged", {"other.txt": "theirs\n"})
    route(fleet, "DEAD-F1", "DEAD")
    code, out, _ = fleet.run("prune", "--discard", "DEAD")
    assert code == 0
    assert f"kept {dead} (DEAD): rows are routed to it: DEAD-F1" in out
    route(fleet, "DEAD-F1", "ending")
    code, out, err = fleet.run("prune", "--discard", "DEAD")
    assert code == 0, err
    assert f"removed {dead} and worktree-agent-h1 (DEAD)" in out
    assert out.splitlines()[-1] == "prune: removed 1, kept 0"
    assert model.read_agent(fleet.state, "DEAD").status == "pruned"
    assert "worktree-agent-h1" not in git(fleet.root, "branch", "--list")
    assert other.exists()
    assert fleet.run("prune", "--discard", "NOBODY")[0] == 2
    # A running record whose transcript is not found may be an agent at work in it, so only a discard,
    # the coordinator's own word, settles it; its rows, which `route` refuses while it is live, route then.
    live = add_agent(fleet, "LIVE", "h3", "running", {"other.txt": "live\n"})
    route(fleet, "LIVE-F1", "LIVE")
    code, out, err = fleet.run("prune")
    assert code == 0, err
    assert f"kept {live} (LIVE): its record is running, and no transcript found for h3" in out
    assert fleet.run("route", "ending", "LIVE-F1")[0] == 2
    code, out, err = fleet.run("prune", "--discard", "LIVE")
    assert code == 0, err
    assert "LIVE's rows route once it is settled: LIVE-F1" in out
    assert f"removed {live} and worktree-agent-h3 (LIVE)" in out
    assert model.read_agent(fleet.state, "LIVE").status == "pruned"
    assert fleet.run("route", "ending", "LIVE-F1")[0] == 0


def test_a_discard_of_a_landed_agent_keeps_its_landing_due(fleet: Fleet) -> None:
    """Its work landed, so an item queued for its landing stays due rather than read as never landing."""
    tree = add_agent(fleet, "DONE", "m1", "landed")
    write(tree, "kept.txt", "a file that keeps the tree\n")
    ran(fleet, "m1")
    (item,) = model.write_queue(fleet.state, lambda _: [model.QueueItem(1, "landed", "push", "DONE")])
    code, out, err = fleet.run("prune", "--discard", "DONE")
    assert code == 0, err
    assert f"removed {tree} and worktree-agent-m1 (DONE)" in out
    agent = model.read_agent(fleet.state, "DONE")
    assert (agent.status, agent.reason) == ("pruned", None)
    assert queue.due(fleet.state) == [item]


def test_a_report_waiting_keeps_its_agents_tree_discarded_or_not(fleet: Fleet) -> None:
    """A pruned agent's report can no longer be judged, so a commit-less driver's findings would be lost."""
    waits = add_agent(fleet, "WAITS", "k1", "reported")
    ran(fleet, "k1")
    asks = add_agent(fleet, "ASKS", "k2", "reported")
    model.update_agent(fleet.state, "ASKS", lambda held: replace(held, outcome="NEEDS-ANSWER"))
    ran(fleet, "k2")
    for args in (("prune",), ("prune", "--discard", "WAITS")):
        code, out, err = fleet.run(*args)
        assert code == 0, (args, err)
        said = "WAITS's round 1 report is not judged: `orchestra judge WAITS` banks its findings first"
        assert f"kept {waits} (WAITS): {said}" in out
    assert (
        f"kept {asks} (ASKS): ASKS stopped NEEDS-ANSWER: answer it by message, and judge the report it writes"
        in (fleet.run("prune")[1])
    )
    assert [model.read_agent(fleet.state, name).status for name in ("WAITS", "ASKS")] == ["reported", "reported"]
    # A stopped agent's report that reads waits too, discarded or not; one that does not read holds nothing back.
    halted = add_agent(fleet, "HALTED", "k3", "stopped")
    ran(fleet, "k3")
    write(fleet.state, "reports/HALTED.md", report_text("writer", "HALTED"))
    model.update_agent(fleet.state, "HALTED", lambda held: replace(held, reason="usage limit"))
    for args in (("prune",), ("prune", "--discard", "HALTED")):
        said = "HALTED is stopped (usage limit), and its round 1 report reads: `orchestra judge HALTED` banks its"
        assert f"kept {halted} (HALTED): {said} findings first" in fleet.run(*args)[1]
    write(fleet.state, "reports/HALTED.md", "cut off mid-wri")
    assert f"removed {halted} and worktree-agent-k3 (HALTED)" in fleet.run("prune", "--discard", "HALTED")[1]
    model.update_agent(fleet.state, "WAITS", lambda held: replace(held, status="judged"))
    code, out, err = fleet.run("prune")
    assert code == 0, err
    assert f"removed {waits} and worktree-agent-k1 (WAITS)" in out
    assert asks.exists()


def test_a_report_arriving_during_a_prune_keeps_its_record(fleet: Fleet) -> None:
    """The stop hook can set a record `reported` after the prune judged it; set `pruned`, that report could never
    be judged.
    """
    late = add_agent(fleet, "LATE", "l1")
    ran(fleet, "l1")
    hooks = fleet.root.parent / "hooks"
    report = write(hooks, "report.py", REPORTS)
    run = f"PYTHONPATH='{LIB.as_posix()}' '{Path(sys.executable).as_posix()}' '{report.as_posix()}'"
    # Git runs this hook once the prune's own deletion of the agent's branch is committed, which falls between
    # its judging and its settling, and on no other change of a ref.
    hook = (
        '#!/bin/sh\n[ "$1" = committed ] || exit 0\n'
        "grep -q ' 0*0 refs/heads/worktree-agent-l1$' || exit 0\n"
        f"{run} '{fleet.state.as_posix()}' LATE\n"
    )
    # Git runs a hook only where its executable bit is set, which a Windows checkout does not need.
    write(hooks, "reference-transaction", hook).chmod(0o755)
    git(fleet.root, "config", "core.hooksPath", hooks.as_posix())
    code, out, err = fleet.run("prune")
    assert code == 0, err
    assert f"removed {late} and worktree-agent-l1 (LATE)" in out
    assert "kept LATE's record: it became reported during the prune" in out
    assert model.read_agent(fleet.state, "LATE").status == "reported"


def test_no_prune_deletes_the_session_branch_or_the_base(tmp_path: Path, make_repo: MakeRepo) -> None:
    """A record naming either, by a slip of a hand edit or a hook run in the wrong checkout, would cost the session."""
    root = make_repo(tmp_path / "project")
    git(root, "switch", "-q", "-c", "step")
    fleet = make_fleet(root)
    session = model.read_session(fleet.state)
    assert (session.branch, session.base) == ("step", "main")
    for name, branch in (("ONSESSION", "step"), ("ONBASE", "main")):
        write_agent(fleet.state, model.Agent(name, "writer", status="stopped", branch=branch))
    for args in (("prune",), ("prune", "--discard", "ONSESSION"), ("prune", "--discard", "ONBASE")):
        code, out, err = fleet.run(*args)
        assert code == 0, (args, err)
    assert "kept main (ONBASE): it is the session's branch or its base, which prune never deletes" in out
    assert git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split() == ["main", "step"]
    assert [model.read_agent(fleet.state, name).status for name in ("ONSESSION", "ONBASE")] == ["stopped", "stopped"]
