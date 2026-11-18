"""`prune`: the worktree and branch of every agent that has landed, or that ran and left nothing unlanded.

A tree is kept, its reason named, while its agent's transcript says it may be working, whatever its
record reads (a resumed agent's worktree carries no lock, so the lock proves nothing), its record is
`running` and no transcript of it can be read, its report waits to be judged or its question to be
answered, its branch is the session's or the base, a row is routed to the agent, its branch holds a
commit the session branch lacks, it holds uncommitted files git does not ignore, or a process holds
one of its files. Ignored files never keep a tree: the agent's work is committed, and an ignored file
is rebuilt by definition.

A removal renames the folder into a trash folder beside it, which is instant on one volume, runs
`git worktree prune` and `git branch -D`, and leaves the deleting to a detached process, so no
command waits on a large tree. Candidates come from `git worktree list` and a listing of the
harness's worktrees folder, never from a glob.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from pathlib import Path

from orchestra import DONE, REMAINS, cli, config, context, findings, log, model, process, report, store
from orchestra.config import Checkout

# Where the harness puts an agent's worktree, `agent-<id>`, under the primary checkout.
WORKTREES = Path(".claude", "worktrees")
# Beside the trees it holds, so the rename never crosses a volume; a project ignoring its worktrees
# folder ignores this too.
TRASH = ".orchestra-trash"
# Written beside a trash entry the deleter gave up on, holding why; the next prune names it.
FAILED = ".failed"
# How often the deleter asks again for a file Windows refuses, a scanner or an indexer holding it.
RETRIES = 6
ROUNDS = 3
POOL = 8
# Rows a kept line names before it counts the rest.
SHOWN = 5


@dataclass(frozen=True)
class Tree:
    path: Path
    branch: str | None = None
    head: str = ""
    locked: bool = False
    # Whether git lists it: a folder git dropped can be judged by nothing inside it.
    listed: bool = True
    # The folder's identity, read when the tree is made: it outlives a rename into the trash, and two
    # spellings of one folder share it, such as a redirected one in Claude's Windows app.
    key: object = field(init=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _key(self.path))


@dataclass
class Outcome:
    lines: list[str] = field(default_factory=list[str])
    failed: list[str] = field(default_factory=list[str])
    removed: list[str] = field(default_factory=list[str])


def _said(done: subprocess.CompletedProcess[bytes]) -> str:
    return done.stderr.decode("utf-8", "replace").strip()


def _key(path: Path) -> object:
    """A folder's volume and file number; its spelling, in the file system's case, where it has none."""
    try:
        held = path.stat()
    except OSError:
        held = None
    if held is None or not held.st_ino:
        return os.path.normcase(path.absolute())
    return held.st_dev, held.st_ino


def trees(root: Path) -> list[Tree]:
    """Every worktree of the clone but the primary checkout, and every folder in the worktrees folder git dropped."""
    found: list[Tree] = []
    for fields in config.worktrees(root)[1:]:
        branch = fields.get("branch")
        # A listed tree whose `.git` link is gone answers git from the primary checkout: judge it as a folder.
        listed = "prunable" not in fields
        found.append(Tree(Path(fields["worktree"]), branch, fields.get("HEAD", ""), "locked" in fields, listed))
    known = {tree.key for tree in found}
    folder = root / WORKTREES
    if folder.is_dir():
        for entry in sorted(folder.iterdir()):
            if entry.is_dir() and entry.name != TRASH and _key(entry) not in known:
                found.append(Tree(entry, listed=False))
    return found


def branch_exists(root: Path, branch: str) -> bool:
    return config.run_git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def branch_of(agent: model.Agent, session: str | None = None) -> str | None:
    """The agent's branch: its record's, else the one the harness's file beside its transcript names.

    `session` is the one a hook's input names, whose folder is searched first, as `context.transcript` takes it.
    """
    if agent.branch:
        return agent.branch
    named = _meta(agent, session).get("worktreeBranch")
    return named if isinstance(named, str) and named else None


def _meta(agent: model.Agent, session: str | None = None) -> dict[str, object]:
    """The harness's `agent-<id>.meta.json` beside the transcript; empty where there is none."""
    path = context.transcript(agent, session)
    if path is None:
        return {}
    try:
        return context.as_object(json.loads(path.with_suffix(".meta.json").read_bytes().decode("utf-8")))
    except (OSError, ValueError):
        return {}


def _unread(agent: model.Agent) -> str | None:
    """Why a `running` record whose transcript cannot be read keeps its tree: nothing shows its turn ended."""
    if agent.status != "running":
        return None
    quiet, why = context.ended(agent, datetime.datetime.now().astimezone())
    return f"its record is running, and {why}" if quiet is None else None


def _busy(state: Path, agent: model.Agent, branch: str | None, never: Collection[str], *, discard: bool) -> str | None:
    """Why the agent's tree stays whatever it holds; None where nothing keeps it.

    A tree its agent may be working in is never moved, a folder git dropped included, nor one whose report
    waits: a pruned agent's report can no longer be judged, nor its question answered. A `running` record
    no transcript shows is kept unless discarded, the coordinator's own word, since nothing else settles it.
    """
    unread = None if discard else _unread(agent)
    return context.working(agent) or unread or findings.waiting(state, agent) or _spared(branch, never)


def _discarded(agent: model.Agent, rows: list[str], out: Outcome) -> str | None:
    """Why a discard keeps the agent's tree: rows routed to it, which `route` moves first; None where none keeps it.

    `route` refuses a live record's rows, so a discard waiting on them would wait for good: they route once it
    is settled, and the line says so.
    """
    if rows and findings.is_live(agent):
        out.lines.append(f"{agent.name}'s rows route once it is settled: {', '.join(rows)}")
        return None
    return _routed_to(rows)


def _spared(branch: str | None, never: Collection[str]) -> str | None:
    """Why a record naming the session's branch or its base, by a hand edit or a hook run in the wrong checkout,
    keeps it: removing either would cost the session."""
    return "it is the session's branch or its base, which prune never deletes" if branch in never else None


def _routed(state: Path) -> dict[str, list[str]]:
    held: dict[str, list[str]] = {}
    for row, event in model.read_findings(state).items():
        if event.status == "ROUTED" and event.owner:
            held.setdefault(event.owner, []).append(row)
    return held


def _ahead(root: Path, session: str, ref: str) -> int:
    return int(config.git(root, "rev-list", "--count", f"refs/heads/{session}..{ref}").strip() or 0)


def uncommitted(tree: Path) -> int:
    """The entries `git status` names in the tree, ignored files left out."""
    return len([entry for entry in config.git(tree, "status", "--porcelain", "-z").split("\0") if entry])


def _routed_to(rows: list[str]) -> str | None:
    """Why an agent holding rows keeps its tree: a successor's brief would be refused them."""
    if not rows:
        return None
    more = f" and {len(rows) - SHOWN} more" if len(rows) > SHOWN else ""
    return f"rows are routed to it: {', '.join(rows[:SHOWN])}{more}"


def _kept(root: Path, session: str, agent: model.Agent, tree: Tree | None, rows: list[str]) -> str | None:
    """Why a finished agent's branch and tree stay; None where both can go."""
    if held := _routed_to(rows):
        return held
    branch = branch_of(agent)
    # A detached tree's commits are on no branch: its head is read as well.
    refs = [branch] if branch and branch_exists(root, branch) else []
    refs += [tree.head] if tree is not None and tree.head and not tree.branch else []
    for ref in refs:
        if ahead := _ahead(root, session, ref):
            return f"{ahead} commit(s) of it are not on {session}"
    if tree is not None and tree.listed and tree.path.is_dir() and (dirty := uncommitted(tree.path)):
        return f"{dirty} uncommitted file(s) git does not ignore"
    return None


def _trash(tree: Path) -> Path:
    """The tree renamed into the trash beside it; OSError where a process holds a file in it."""
    folder = tree.parent / TRASH
    folder.mkdir(exist_ok=True)
    target = folder / f"{tree.name}-{uuid.uuid4().hex[:8]}"
    tree.rename(target)
    return target


def _clear(agent: model.Agent, mine: list[Tree], keep: str | None, out: Outcome, moved: list[Tree]) -> list[str] | None:
    """Each of the agent's trees moved to the trash unless `keep` holds it; what was removed, None where any stays."""
    removed: list[str] = []
    whole = keep is None
    for tree in mine:
        # A folder git dropped is no worktree to work in, and nothing in it can be judged.
        if keep is not None and tree.listed:
            out.lines.append(f"kept {tree.path} ({agent.name}): {keep}")
            continue
        try:
            _trash(tree.path)
        except FileNotFoundError:
            continue
        except OSError as error:
            out.failed.append(f"kept {tree.path} ({agent.name}): in use, a process holds a file in it ({error})")
            whole = False
            continue
        moved.append(tree)
        removed.append(str(tree.path))
    if not whole and removed:
        out.lines.append(f"removed {', '.join(removed)} ({agent.name}): a folder git no longer lists")
    return removed if whole else None


def prune(
    here: Checkout,
    state: Path,
    only: Collection[str] | None = None,
    *,
    ending: bool = False,
    discard: bool = False,
) -> Outcome:
    """Each removable agent's tree and branch removed and its record `pruned`; every tree kept named with why.

    `only` limits it to those agents, as `land` asks for the one it landed; `ending` also removes each
    frozen tree `diff --tree` made for a lens round; `discard` removes them whatever commits or files
    they hold, once the coordinator has saved what it keeps, but never while its report waits or rows
    are routed to one that `route` could move them from.
    """
    root = here.root
    session = model.read_session(state)
    never = {session.branch, session.base}
    out = Outcome()
    routed = _routed(state)
    every = trees(root)
    by_branch = {tree.branch: tree for tree in every if tree.branch and tree.listed}
    by_folder = {tree.path.name: tree for tree in every}
    claimed: set[object] = set()
    moved: list[Tree] = []
    # Each agent whose trees went, with what was removed, and the branch still to delete.
    gone: dict[str, list[str]] = {}
    branches: dict[str, str] = {}
    for agent in (agents := model.agents(state)):
        if only is not None and agent.name not in only:
            continue
        branch = branch_of(agent)
        found = [by_branch[branch]] if branch in by_branch else []
        found += [by_folder[f"agent-{ident}"] for ident in agent.ids if f"agent-{ident}" in by_folder]
        mine = list({tree.key: tree for tree in found}.values())
        claimed |= {tree.key for tree in mine}
        held = branch if branch is not None and branch_exists(root, branch) else None
        stopped = agent.status == "stopped" and agent.type in report.COMMITTING
        # A writer or a driver stopped with neither left can never be resumed, so its record is settled too;
        # one whose branch nothing names may have a tree elsewhere holding its work, so it keeps its record.
        lost = discard or (stopped and branch is not None and held is None)
        if not mine and held is None and not lost:
            if stopped and branch is None:
                out.lines.append(
                    f"kept its record ({agent.name}): its branch is unknown; `orchestra prune --discard {agent.name}`"
                    " settles it once you have saved what it holds"
                )
            continue
        if why := _busy(state, agent, branch, never, discard=discard):
            out.lines += [f"kept {tree.path} ({agent.name}): {why}" for tree in mine] or [
                f"kept {held or 'its record'} ({agent.name}): {why}"
            ]
            continue
        listed = next((tree for tree in mine if tree.listed), None)
        rows = routed.get(agent.name, [])
        keep = _discarded(agent, rows, out) if discard else _kept(root, session.branch, agent, listed, rows)
        if keep is not None and listed is None:
            out.lines.append(f"kept {held or 'its record'} ({agent.name}): {keep}")
        removed = _clear(agent, mine, keep, out, moved)
        if removed is not None:
            gone[agent.name] = removed
            if held is not None:
                branches[agent.name] = held
    frozen = _frozen(state, out) if ending else []
    moved += frozen
    claimed |= {tree.key for tree in frozen}
    if only is None:
        out.lines += strangers(every, claimed)
    _unregister(root, moved, branches, gone, out)
    # A record whose status moved since this read, a report arriving meanwhile, is left for that report.
    _settle(state, gone, {agent.name: agent.status for agent in agents}, out, discard=discard)
    out.removed += [str(tree.path) for tree in frozen]
    folders = {tree.path.parent / TRASH for tree in moved} | {root / WORKTREES / TRASH, state / store.TREES / TRASH}
    out.lines += sweep(folders, out)
    return out


def _settle(state: Path, gone: dict[str, list[str]], judged: dict[str, str], out: Outcome, *, discard: bool) -> None:
    """Each record whose trees and branch went set `pruned`; one with nothing left to remove counts as no removal.

    A discarded one says so, since its work never lands and an item queued for its landing never falls due;
    one already landed or pruned did land, so it keeps its reason. A record whose status moved since it was
    judged, a report arriving meanwhile, keeps it, so that report can still be judged.
    """
    moved: list[str] = []

    def settled(held: model.Agent) -> model.Agent:
        if held.status != judged[held.name]:
            moved.append(f"kept {held.name}'s record: it became {held.status} during the prune")
            return held
        lost = discard and held.status not in model.SETTLED
        return replace(held, status="pruned", reason=model.DISCARDED if lost else held.reason)

    for name, removed in gone.items():
        out.lines.append(
            f"removed {' and '.join(removed)} ({name})" if removed else f"settled {name}: nothing of it is left"
        )
        model.update_agent(state, name, settled)
    out.lines += moved
    out.removed += [name for name, removed in gone.items() if removed]
    if gone:
        log.record(state, "prune", f"removed {', '.join(gone)}")


def _unregister(
    root: Path, moved: list[Tree], branches: dict[str, str], gone: dict[str, list[str]], out: Outcome
) -> None:
    """git told the moved trees are gone, and each branch deleted; an agent whose branch git keeps leaves `gone`."""
    # A locked tree's entry survives `git worktree prune`; the record and the transcript judged it ended.
    for tree in moved:
        if tree.locked:
            config.run_git(root, "worktree", "unlock", str(tree.path))
    if moved and (pruned := config.run_git(root, "worktree", "prune")).returncode != 0:
        out.failed.append(f"git worktree prune exited {pruned.returncode}: {_said(pruned)}")
    for name, branch in branches.items():
        if (deleted := config.run_git(root, "branch", "-D", branch)).returncode != 0:
            out.failed.append(f"git branch -D {branch} ({name}) exited {deleted.returncode}: {_said(deleted)}")
            del gone[name]
        else:
            gone[name].append(branch)


def strangers(every: list[Tree], claimed: set[object]) -> list[str]:
    """Each tree no agent of this session holds, left as it is."""
    said: list[str] = []
    for tree in every:
        if tree.key in claimed:
            continue
        what = (
            f"({tree.branch})" if tree.branch else (f"detached at {tree.head[:9]}" if tree.head else "git lists none")
        )
        said.append(f"left {tree.path} {what}: no agent of this session holds it")
    return said


def _frozen(state: Path, out: Outcome) -> list[Tree]:
    """Each frozen tree of the session's lens rounds moved to the trash."""
    folder = state / store.TREES
    moved: list[Tree] = []
    if not folder.is_dir():
        return moved
    for entry in sorted(folder.iterdir()):
        if not entry.is_dir() or entry.name == TRASH:
            continue
        tree = Tree(entry)
        try:
            _trash(entry)
        except OSError as error:
            out.failed.append(f"kept {entry}: in use, a process holds a file in it ({error})")
            continue
        moved.append(tree)
        out.lines.append(f"removed {entry}: the frozen tree of a lens round")
    return moved


def sweep(folders: Collection[Path], out: Outcome) -> list[str]:
    """Every entry of the trash folders handed to one detached deleter; each the last one gave up on named."""
    entries: list[Path] = []
    for folder in folders:
        if not folder.is_dir():
            continue
        for entry in sorted(folder.iterdir()):
            if entry.name.endswith(FAILED):
                if not entry.with_name(entry.name.removesuffix(FAILED)).exists():
                    entry.unlink(missing_ok=True)
                continue
            if (marker := entry.with_name(entry.name + FAILED)).exists():
                said = marker.read_bytes().decode("utf-8", "replace").strip()
                out.failed.append(f"{entry} could not be deleted ({said}): deleting it again")
            entries.append(entry)
        # A trash folder a refused rename left empty goes now; one with entries goes with its last.
        with contextlib.suppress(OSError):
            folder.rmdir()
    if not entries:
        return []
    _detach(entries)
    parents = ", ".join(sorted({str(entry.parent) for entry in entries}))
    return [f"deleting {len(entries)} folder(s) in the background from {parents}"]


def _detach(entries: list[Path]) -> None:
    """This module run on the entries in a process that outlives the caller and holds none of its streams."""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    process.detach(
        (sys.executable, "-P", "-m", "orchestra.prune", *map(str, entries)),
        # Outside the trash: Windows refuses to remove a folder that is a process's working directory.
        cwd=entries[0].parent.parent,
        env=env,
    )


def long_form(path: Path) -> str:
    """The path in the form Windows takes past its 260-character limit, whatever the machine's setting."""
    text = str(path.absolute())
    if sys.platform != "win32" or text.startswith("\\\\?\\"):
        return text
    return "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text


def _junction(path: Path) -> bool:
    """Whether the path is a Windows reparse point, such as a junction, which `is_symlink` does not report."""
    try:
        held = path.lstat()
    except OSError:
        return False
    # The attribute exists on Windows alone, and a POSIX link answers to `is_symlink`.
    attributes: int = getattr(held, "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _again(func: Callable[[str], object], path: str, error: BaseException) -> None:
    """A refused delete asked again: the read-only bit cleared, then retried while Windows holds the file."""
    if isinstance(error, FileNotFoundError):
        return
    for target in (Path(path), Path(path).parent):
        # A link's own bit, never its target's, which lies outside the tree. Windows takes `follow_symlinks`
        # only from Python 3.13, and before it its `chmod` sets the read-only attribute of the path itself,
        # a link's own (os.chmod). A platform without `lchmod`, Linux, keeps no bit of a link's own to clear.
        link = target.is_symlink() or _junction(target)
        with contextlib.suppress(OSError):
            if link and os.chmod in os.supports_follow_symlinks:
                target.chmod(stat.S_IRWXU, follow_symlinks=False)
            elif not link or sys.platform == "win32":
                target.chmod(stat.S_IRWXU)
    for attempt in range(RETRIES):
        try:
            func(path)
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.05 * 2**attempt)
        else:
            return
    raise error


def delete(entry: Path) -> str | None:
    """The folder deleted, a junction inside it unlinked and never followed; why it is not, where it is not."""
    target = long_form(entry)
    said: str | None = None
    for _ in range(ROUNDS):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(target, onexc=_again)
            else:
                shutil.rmtree(target, onerror=lambda func, path, info: _again(func, path, info[1]))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
        except FileNotFoundError:
            return None
        except OSError as error:
            said = f"{type(error).__name__}: {error}"
            time.sleep(0.5)
        else:
            return None
    return said


def main(entries: list[str]) -> int:
    """The detached deleter: each entry deleted in a small pool, a marker left beside each it gave up on."""
    paths = [Path(entry) for entry in entries]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(POOL, len(paths)))) as pool:
        results = list(pool.map(delete, paths))
    for path, said in zip(paths, results, strict=True):
        marker = path.with_name(path.name + FAILED)
        if said is None:
            marker.unlink(missing_ok=True)
        else:
            store.replace_bytes(marker, said.encode("utf-8") + b"\n")
    for folder in {path.parent for path in paths}:
        with contextlib.suppress(OSError):
            folder.rmdir()
    return 0 if all(said is None for said in results) else 1


def add(_verb: str, parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ending", action="store_true", help="also remove each frozen tree `diff --tree` made, at the session's end"
    )
    parser.add_argument(
        "--discard",
        metavar="NAME",
        help="remove that agent's worktree and branch alone, whatever they hold: save what you keep of them first",
    )


def run(args: argparse.Namespace) -> int:
    here = config.checkout()
    state = store.state_dir(held=here)
    # Refused before a tree moves rather than at the first record written after it.
    store.check_lease(state)
    if args.discard is not None:
        model.read_agent(state, args.discard)
    only = None if args.discard is None else {args.discard}
    out = prune(here, state, only, ending=args.ending, discard=args.discard is not None)
    lines = out.failed + out.lines
    kept = sum(line.startswith("kept ") for line in out.lines)
    summary = f"prune: removed {len(out.removed)}, kept {kept}" + (f", {len(out.failed)} failing" if out.failed else "")
    cli.emit(args, lines, summary)
    return REMAINS if out.failed else DONE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
