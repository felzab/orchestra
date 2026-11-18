"""`land`: one finished agent's branch merged into the session branch, its rows closed and its tree pruned.

`git merge --no-ff` brings the branch in whole: the agent's commits keep the hooks they ran under, and
the merge commit, `Merge <NAME> into <session branch>`, goes through the checkout's hooks as a merge.
Each configured `land.steps` entry whose `when` names a path the merge touches runs, installs first,
then regenerations, then checks. A conflict in a path a regeneration writes is answered by running it;
any other conflict, and any failing step, aborts the merge and leaves the tree as it was. Every stop
before the merge commit leaves the session branch where it stood; `EXITS` gives each code.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from orchestra import Refused, brief, cli, config, context, findings, log, model, process, prune, report, status, store
from orchestra.config import Checkout, Step

# A rebase of commits another branch already merged lands each twice under two hashes, which the
# preflight's twin check then refuses.
MERGE_BACK = (
    "the agent merges the session branch into its own branch, rebasing only where no other branch holds its commits"
)
REFUSED, CONFLICT, GIT_FAILED = 2, 3, 7
EXITS = {
    0: "merged, or nothing to merge",
    2: "refused before the merge",
    3: f"a conflict no regeneration answers; the merge is aborted, and {MERGE_BACK}",
    4: "a regeneration failed; the merge is aborted",
    5: "an install or a check failed on the merged tree, or could not run; the merge is aborted",
    6: "a hook refused the merge commit; the merge is aborted",
    7: "git failed during the merge; read `git status`",
    8: "the session branch fails that check before the merge; the merge is aborted and the branch was not judged",
    10: "merged, and the bookkeeping after the merge failed: the merge stands, and landing the agent again finishes it",
}
ORDER = {"install": 0, "regenerate": 1, "check": 2}
FAILED = {"install": 5, "regenerate": 4, "check": 5}
# The most lines of a failing command's output a stop quotes, and the most changed paths it names.
TAIL = 15
SHOWN = 5


class Stop(Exception):
    """A landing that cannot go on: its exit code and the lines the coordinator reads."""

    def __init__(self, code: int, *lines: str, recheck: Step | None = None) -> None:
        super().__init__(lines[0] if lines else "")
        self.code = code
        self.lines = lines
        # A failed check, run again at `HEAD` once the merge is aborted.
        self.recheck = recheck


def _git(root: Path, *args: str, code: int = 2) -> str:
    done = config.run_git(root, *args)
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip()
        raise Stop(code, f"git {' '.join(args)} exited {done.returncode}: {said}")
    return done.stdout.decode("utf-8", "replace")


def _ok(root: Path, *args: str) -> bool:
    return config.run_git(root, *args).returncode == 0


def _names(root: Path, *args: str, code: int = 2) -> list[str]:
    """The paths a git command prints, NUL-separated so no name is quoted."""
    return [path for path in _git(root, *args, "-z", code=code).split("\0") if path]


def fixed_rows(state: Path, names: list[str]) -> list[str]:
    """Every row the agents' latest reports name on their summary's `Rows fixed:` line.

    The latest round alone speaks, since it names every row the branch fixes: an earlier round's claim
    on a row routed back since would otherwise close it again. A report that does not read names
    nothing, since a stopped agent's broken report does not hold its landing back; the rows it would
    have named are then printed as still routed.
    """
    named: set[str] = set()
    for name in names:
        agent = model.read_agent(state, name)
        try:
            named.update(report.read(report.path(state, name, max(agent.reports, default=1)), agent.type).rows)
        except Refused:
            continue
    return sorted(named)


def preflight(root: Path, branch: str) -> None:
    """Every state in which a merge would take the wrong thing, ruled out before it starts."""
    if not prune.branch_exists(root, branch):
        raise Stop(2, f"{branch} is no local branch")
    # A landing interrupted mid-merge leaves one in progress, whose commit would skip every step's check.
    if _ok(root, "rev-parse", "-q", "--verify", "MERGE_HEAD"):
        raise Stop(2, "a merge is in progress here, as an interrupted landing leaves: `git merge --abort`, then land")
    if changed := _git(root, "status", "--porcelain").splitlines():
        # Named, so an unignored worktrees folder reads as the cause rather than as work to clean away.
        more = f" and {len(changed) - SHOWN} more" if len(changed) > SHOWN else ""
        named = ", ".join(line[3:] for line in changed[:SHOWN]) + more
        raise Stop(
            2, f"this checkout holds changes, which the merge commit would take in: {named}; land from a clean tree"
        )
    for tree in prune.trees(root):
        if tree.branch == branch and tree.listed and _git(tree.path, "status", "--porcelain").strip():
            raise Stop(2, f"{tree.path} holds uncommitted work, which no merge takes: the agent commits it first")
    # git names a stash "On <branch>:" when given a message and "WIP on <branch>:" when not.
    if re.search(rf"(?:^|: )(?:WIP on|On) {re.escape(branch)}:", _git(root, "stash", "list"), re.MULTILINE):
        raise Stop(2, f"a stash entry is on {branch}, and a stashed edit lands nowhere: the agent commits or drops it")
    # A patch landed twice merges clean as text, and a later edit to one copy doubles a hunk unseen.
    twins = [line[2:] for line in _git(root, "cherry", "-v", "HEAD", branch).splitlines() if line.startswith("- ")]
    if twins:
        session = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        raise Stop(
            2,
            f"{branch} carries {len(twins)} commit(s) whose patch the session branch already holds under another hash:",
            *twins,
            # A rebase leaves out each commit whose change the new base holds already (git-rebase(1)).
            f"message the agent to run `git rebase {session}` in its worktree, which drops them, then land it again",
        )


def outside(state: Path, name: str, root: Path, branch: str) -> list[str]:
    owns = brief.header(state, name).owns
    changed = _names(root, "diff", "--name-only", "--no-renames", f"HEAD...{branch}")
    return [path for path in changed if not brief.owned(owns, path)]


def _due(step: Step, touched: list[str]) -> bool:
    """Whether the merge touches a path the step names: a file, or a folder written with its `/`."""
    return any(path == when or (when.endswith("/") and path.startswith(when)) for path in touched for when in step.when)


def _writes(step: Step, path: str) -> bool:
    return any(path == out.rstrip("/") or path.startswith(out.rstrip("/") + "/") for out in step.writes)


def run_step(root: Path, step: Step) -> subprocess.CompletedProcess[bytes]:
    cwd = root / step.cwd
    program = config.program(step.run, cwd)
    if program is None:
        raise Stop(FAILED[step.kind], f"{step.run[0]} is not found, so the {step.kind} step could not run")
    try:
        return process.run((program, *step.run[1:]), cwd=cwd)
    except OSError as error:
        # Found and not startable: a script Windows cannot run directly, or one with no execute bit or shebang.
        raise Stop(FAILED[step.kind], f"`{' '.join(step.run)}` could not run: {error}") from None


def _tail(done: subprocess.CompletedProcess[bytes]) -> list[str]:
    return (done.stdout + done.stderr).decode("utf-8", "replace").strip().splitlines()[-TAIL:]


def _merge(root: Path, message: str, branch: str, steps: tuple[Step, ...], written: list[str]) -> list[str]:
    """The branch merged and committed through the steps with `message`; what each step did.

    The message names the agent, since git's own names the harness's branch, a local id no reader needs,
    and goes to the merge, so the commit hands a `prepare-commit-msg` hook the source `merge` (githooks(5)).
    """
    merged = _ok(root, "merge", "--no-ff", "--no-commit", "-m", message, branch)
    conflicted = _names(root, "diff", "--name-only", "--diff-filter=U", code=7)
    if not merged and not conflicted:
        raise Stop(7, f"git merge --no-ff --no-commit {branch} failed with no file in conflict")
    regenerations = [step for step in steps if step.kind == "regenerate"]
    if unanswered := [path for path in conflicted if not any(_writes(step, path) for step in regenerations)]:
        raise Stop(3, *(f"CONFLICT in {path}" for path in unanswered))
    touched = sorted({*_names(root, "diff", "--cached", "--name-only", "HEAD", code=7), *conflicted})
    said: list[str] = []
    for step in sorted(steps, key=lambda step: ORDER[step.kind]):
        answers = step.kind == "regenerate" and any(_writes(step, path) for path in conflicted)
        if not (_due(step, touched) or answers):
            continue
        shown = " ".join(step.run)
        written += step.writes
        done = run_step(root, step)
        if done.returncode != 0:
            head = f"`{shown}` in {step.cwd} exited {done.returncode} on the merged tree:"
            raise Stop(FAILED[step.kind], head, *_tail(done), recheck=step if step.kind == "check" else None)
        if step.writes:
            _git(root, "add", "-A", "--", *step.writes, code=7)
        said.append(f"{step.kind} `{shown}`")
    if _names(root, "diff", "--name-only", "--diff-filter=U", code=7):
        raise Stop(3, "a conflict is still unresolved after the regenerations")
    # A step writing a tracked file it does not declare would leave it out of the merge commit.
    if wrote := _names(root, "diff", "--name-only", code=7):
        written += wrote
        raise Stop(5, f"a step wrote tracked files no `writes` names: {', '.join(wrote)}")
    # `strip` drops the `# Conflicts:` lines git adds to the message where a regeneration answered one.
    done = config.run_git(root, "commit", "-q", "--no-edit", "--cleanup=strip")
    if done.returncode != 0:
        raise Stop(6, f"the merge commit was refused (exit {done.returncode}):", *_tail(done))
    return said


def _others(state: Path, name: str, root: Path) -> list[tuple[model.Agent, str]]:
    found: list[tuple[model.Agent, str]] = []
    for agent in model.agents(state):
        if agent.name != name and (branch := prune.branch_of(agent)) and prune.branch_exists(root, branch):
            found.append((agent, branch))
    return found


def _abort(root: Path, branch: str, stop: Stop, written: list[str], others: list[tuple[model.Agent, str]]) -> Stop:
    """The merge aborted and this run's writes put back; the stop, its lines completed."""
    if stop.code == REFUSED:
        stop.code = GIT_FAILED
    config.run_git(root, "merge", "--abort")
    # The abort keeps unstaged edits, and the preflight found the tree clean, so every file still dirty
    # under a path a step writes is this run's own.
    status = config.run_git(root, "status", "--porcelain", "-z")
    dirty = {entry[3:] for entry in status.stdout.decode("utf-8", "replace").split("\0") if entry}
    if mine := sorted(
        path for path in dirty if any(path == out or path.startswith(out.rstrip("/") + "/") for out in written)
    ):
        config.run_git(root, "restore", "--source=HEAD", "--staged", "--worktree", "--", *mine)
    status = config.run_git(root, "status", "--porcelain")
    clean = status.returncode == 0 and not status.stdout.strip()
    stop.lines = (*stop.lines, "the merge is aborted" if clean else "read `git status` before anything else")
    # A red already at `HEAD` is no fault of the branch, and no rebase of it can clear it.
    if clean and stop.recheck is not None and run_step(root, stop.recheck).returncode != 0:
        stop.code = 8
        shown = " ".join(stop.recheck.run)
        head = f"the session branch already fails `{shown}` before this merge, so {branch} was not judged:"
        stop.lines = (head, "route the red at HEAD to its owner and land again once it clears", *stop.lines)
    if stop.code == CONFLICT:
        conflicted = {line.removeprefix("CONFLICT in ") for line in stop.lines if line.startswith("CONFLICT in ")}
        for agent, other in others:
            if _ok(root, "merge-base", "--is-ancestor", other, "HEAD"):
                continue
            shared = sorted(conflicted & set(_names(root, "diff", "--name-only", f"HEAD...{other}", code=7)))
            if shared:
                stop.lines = (*stop.lines, f"{', '.join(shared)} also changed by {agent.name} ({other})")
        stop.lines = (*stop.lines, MERGE_BACK)
    return stop


def credit(state: Path, names: list[str], merge: str) -> list[str]:
    """Each row a landed agent's report names fixed that is routed to an agent landed here closed FIXED;
    what is left named."""
    named = fixed_rows(state, names)
    said: list[str] = []

    def close(rows: dict[str, model.FindingEvent]) -> list[model.FindingEvent]:
        closed: list[model.FindingEvent] = []
        for row in named:
            held = rows.get(row)
            if held is not None and held.status == "FIXED" and held.owner in names:
                # Closed by an earlier landing of the same agent, whose later report names it again.
                continue
            if held is not None and held.status == "ROUTED" and held.owner in names:
                closed.append(model.FindingEvent(row, "close", "FIXED", owner=held.owner, evidence=merge))
                said.append(f"FIXED {row} by {merge}")
            elif held is not None and held.status == "OPEN":
                # An OPEN row was never routed, so no fixer was asked for it: the claim is the coordinator's to judge.
                said.append(f"{row} is named fixed while OPEN: `orchestra close FIXED {merge} {row}` if it is")
            else:
                state_of = f"{held.status} {held.owner or ''}".strip() if held else "no row"
                said.append(f"{row} is named fixed and is {state_of}, so it stands as it is")
        landed = set(names)
        left = [
            row for row, held in rows.items() if held.status == "ROUTED" and held.owner in landed and row not in named
        ]
        if left:
            said.append(
                f"still routed to {', '.join(names)} that no report's `Rows fixed:` line names: {', '.join(left)}"
            )
        return closed

    model.write_findings(state, close)
    return said


def _reflog(root: Path, ref: str) -> list[str]:
    """The commit each entry of the ref's reflog names, newest first; none where git keeps no reflog for it."""
    done = config.run_git(root, "log", "--walk-reflogs", "--format=%H", ref)
    return done.stdout.decode("utf-8", "replace").split() if done.returncode == 0 else []


def _brought_in(root: Path, session: str, branch: str) -> tuple[str, str] | None:
    """The span of the session branch that brought the branch in, as (before, after); None where nothing did.

    A merge commit on the session branch's first-parent line brought it in, or a fast-forward, which
    `git merge` makes by default: then the session branch's reflog holds the branch's tip, and the
    entry before it is where the session branch stood. A branch with no commit of its own sits on the
    first-parent line too, and its own reflog holds nothing but its creation (git-reflog(1)).
    """
    line = _git(root, "rev-list", "--first-parent", "--ancestry-path", f"{branch}..HEAD").split()
    if line and not _ok(root, "merge-base", "--is-ancestor", branch, f"{line[-1]}^1"):
        return f"{line[-1]}^1", line[-1]
    tip = _git(root, "rev-parse", f"refs/heads/{branch}").strip()
    own = _reflog(root, f"refs/heads/{branch}")
    if not own or own[-1] == tip:
        return None
    moves = _reflog(root, f"refs/heads/{session}")
    at = moves.index(tip) if tip in moves else len(moves)
    if at + 1 >= len(moves) or not _ok(root, "merge-base", "--is-ancestor", moves[at + 1], tip):
        return None
    return moves[at + 1], tip


def _settle(
    here: Checkout, state: Path, name: str, span: tuple[str, str], short: str
) -> tuple[list[str], list[str], prune.Outcome]:
    """The bookkeeping of the span that brought the agent's branch in: who landed, what was said, what was pruned.

    Every step is safe to run again, so a landing stopped after its merge commit is finished by the next.
    """
    root = here.root
    before, after = span
    # A merge agent's branch carries others' commits: each agent whose branch this span brought in landed too.
    carried = [
        agent.name
        for agent, other in _others(state, name, root)
        if agent.status not in model.SETTLED
        and not _ok(root, "merge-base", "--is-ancestor", other, before)
        and _ok(root, "merge-base", "--is-ancestor", other, after)
    ]
    names = [name, *carried]
    lines = credit(state, names, short)
    working: list[str] = []

    def landed(held: model.Agent) -> model.Agent:
        # An agent still at work keeps its record: its stop reports, and landing it then settles it.
        if held.status in findings.LIVE:
            working.append(
                f"{held.name} is {held.status} and has not reported, so its record stays: land it again then"
            )
            return held
        if why := context.working(held):
            working.append(f"{held.name} may still be working ({why}), so its record stays: land it again then")
            return held
        # A carried agent's report not yet judged keeps its record too, so `judge` can still bank it.
        if waits := findings.waiting(state, held):
            working.append(f"{waits}; its record stays: land it again then")
            return held
        return replace(held, status="landed")

    for each in names:
        model.update_agent(state, each, landed)
    log.record(state, "land", f"{', '.join(names)} landed as {short}")
    return names, [*lines, *working], prune.prune(here, state, set(names))


def _finish(
    here: Checkout, state: Path, name: str, span: tuple[str, str], first: Callable[[str], str]
) -> tuple[list[str], str]:
    """The landing's lines and summary once its bookkeeping is done; `first` words its opening by the short hash."""
    short = _git(here.root, "rev-parse", "--short", span[1], code=10).strip()
    try:
        names, lines, said = _settle(here, state, name, span, short)
    except (Stop, Refused, OSError) as failed:
        why = "; ".join(failed.lines) if isinstance(failed, Stop) else str(failed)
        raise Stop(10, first(short), f"the bookkeeping after the merge stopped: {why}", EXITS[10]) from None
    return [first(short), *lines, *said.failed, *said.lines], f"landed {', '.join(names)} as {short}"


def land(here: Checkout, state: Path, name: str, args: argparse.Namespace) -> int:
    root = here.root
    agent = model.read_agent(state, name)
    branch = prune.branch_of(agent)
    if branch is None:
        raise Stop(2, f"{name} has no branch: its record names none and the harness's file names none")
    # A landed agent's report can no longer be judged, so a report is judged before its agent lands.
    if agent.status in findings.LIVE:
        raise Stop(2, f"{name} is {agent.status} and has not reported: land it once its report is judged")
    if why := context.working(agent):
        raise Stop(2, f"{name} may still be working ({why}), so its branch may still move: land it once its turn ends")
    if waits := findings.waiting(state, agent):
        raise Stop(2, waits)
    preflight(root, branch)
    if _ok(root, "merge-base", "--is-ancestor", branch, "HEAD"):
        merged = (
            None if agent.status in model.SETTLED or here.branch is None else _brought_in(root, here.branch, branch)
        )
        if merged is None:
            said = prune.prune(here, state, {name})
            cli.emit(args, said.failed + said.lines, f"nothing to land: the session branch already holds {branch}")
            return 0
        # A landing stopped after its merge commit, or a merge or fast-forward made by hand: what it left
        # undone is done now.
        lines, summary = _finish(
            here,
            state,
            name,
            merged,
            lambda short: f"{branch} was merged already, as {short}: its bookkeeping is done now",
        )
        cli.emit(args, [*lines, *status.paced(state)], summary)
        return 0
    if strays := outside(state, name, root, branch):
        raise Stop(
            2,
            f"{branch} changes {len(strays)} path(s) its brief's `Owns:` does not name:",
            *strays,
            "add each to the brief's `Owns:` if it is the agent's, or have the agent revert it",
        )
    others = _others(state, name, root)
    steps = config.load(root).land.steps
    written: list[str] = []
    try:
        # `Merge <a> into <b>` is a subject commit-message linters pass as a merge.
        message = f"Merge {name} into {model.read_session(state).branch}"
        did = _merge(root, message, branch, steps, written)
    except Stop as stop:
        raise _abort(root, branch, stop, written, others) from None
    except Refused as refused:
        raise _abort(root, branch, Stop(7, str(refused)), written, others) from None
    merge = _git(root, "rev-parse", "HEAD", code=10).strip()
    said = f": {'; '.join(did)}" if did else ""
    lines, summary = _finish(
        here, state, name, (f"{merge}^1", merge), lambda short: f"landed {name} ({branch}) as {short}{said}"
    )
    # What the landing freed is named where the coordinator reads it, so a slice no agent took is seen.
    cli.emit(args, [*lines, *status.paced(state)], summary)
    return 0


def preview(here: Checkout, state: Path, name: str, args: argparse.Namespace) -> int:
    """What the landing would take, changing nothing."""
    root = here.root
    agent = model.read_agent(state, name)
    branch = prune.branch_of(agent)
    written = agent.reports[max(agent.reports)] if agent.reports else str(report.path(state, name))
    lines = [f"{name}: {agent.status}, branch {branch or 'none'}, report {written}"]
    if branch is None or not prune.branch_exists(root, branch):
        cli.emit(args, lines, f"{name} has no branch to land")
        return 0
    tree = next((tree for tree in prune.trees(root) if tree.branch == branch and tree.listed), None)
    if tree is None:
        lines.append("no worktree holds the branch")
    else:
        lines.append(f"{tree.path}: {prune.uncommitted(tree.path)} uncommitted file(s) git does not ignore")
    merged = _ok(root, "merge-base", "--is-ancestor", branch, "HEAD")
    lines.append("already merged into the session branch" if merged else "not merged into the session branch")
    strays = outside(state, name, root, branch)
    lines += [f"outside its `Owns:`: {path}" for path in strays]
    log_lines = _git(root, "log", "--format=%x00%h %s", "--name-only", "--no-renames", f"HEAD..{branch}")
    for entry in log_lines.split("\0")[1:]:
        head, *files = [line for line in entry.splitlines() if line]
        lines.append(f"{head} ({len(files)} file(s))")
    stat = _git(root, "diff", "--stat", f"HEAD...{branch}").splitlines()
    lines += stat
    commits = len(log_lines.split("\0")) - 1
    cli.emit(args, lines, f"{name}: {commits} commit(s) to land, {len(strays)} path(s) outside its `Owns:`")
    return 0


def add(_verb: str, parser: argparse.ArgumentParser) -> None:
    exits = "; ".join(f"{code} {meaning}" for code, meaning in EXITS.items())
    parser.description = f"{parser.description}. Exit codes: {exits}"
    parser.add_argument("name", help="the agent whose branch lands")
    parser.add_argument("--preview", action="store_true", help="print what the landing would take and change nothing")


def run(args: argparse.Namespace) -> int:
    here = config.checkout()
    state = store.state_dir(held=here)
    if not args.preview:
        # Refused before the merge rather than after it, where the bookkeeping's first write would be.
        store.check_lease(state)
    try:
        return preview(here, state, args.name, args) if args.preview else land(here, state, args.name, args)
    except Stop as stop:
        for line in stop.lines:
            print(f"orchestra land: {line}" if line is stop.lines[0] else line, file=sys.stderr)
        return stop.code
