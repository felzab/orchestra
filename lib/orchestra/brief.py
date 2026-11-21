"""`brief`: a brief's header written from the state and git, and the brief checked before its dispatch.

The tool writes the header, so no value in it is typed, and a writer's `Owns:` with the files its
slice's line in PLAN.md names in backticks; the coordinator fills `Rows:`, `Writes:`, `Lens:` and
any `Owns:` left empty, adds `Model: <profile>` where the agent needs a profile of the configuration's
`models` other than `default`, and writes the four sections below them; or writes all of that to a
file first and gives it as `--body`, which writes the brief and checks it in one call. `--check`
judges only what has a fixed shape, and stamps the agent's record with the SHA-256 of the brief's
bytes, so an edit made after the check leaves the brief unchecked.

An `Owns:` entry is a path, a folder holding every path under it, or a glob in which `*` matches
within one folder and a `**` folder matches any number of folders; `?` and `[` are literal, since a
route folder such as `[id]` is a real name. A file still to be made is marked `<path> (new)`.
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import hashlib
import importlib
import itertools
import re
import typing
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from orchestra import DONE, REFUSED, Refused, cli, config, findings, model, report, store

BRIEFS = "briefs"
LISTS = ("Owns", "Rows", "Writes", "Lens")
# The optional label naming the agent's model profile; the configuration's `default` profile where it is absent.
MODEL = "Model"
DEFAULT = "default"
SECTIONS = ("GOAL", "WORK", "PREMISES", "TRAPS")
NONE = "none"
NEW = "(new)"
# The one type whose brief needs a slice of the plan: a writer's commits land. A reader changes no file,
# and a driver plants in its own worktree and lands nothing, so either may work outside the plan.
SLICED = "writer"

FIRST_RE = re.compile(
    r"^Agent:[ \t]*(?P<agent>\S*)[ \t]+Type:[ \t]*(?P<type>\S*)[ \t]+Slice:[ \t]*(?P<slice>.*?)[ \t]*$"
)
BASE_RE = re.compile(r"^Base:[ \t]*(?P<branch>\S+)[ \t]+at[ \t]+(?P<commit>[0-9a-f]{4,40})[ \t]*$")
LABEL_RE = re.compile(r"^(?P<label>[A-Z][a-z]+):(?:[ \t]+(?P<value>.*?))?[ \t]*$")
SECTION_RE = re.compile(rf"^(?P<section>{'|'.join(SECTIONS)})(?:[ \t]+(?P<text>.*))?$")
ITEM_RE = re.compile(r"^[ \t]*[1-9][0-9]*\.(?:[ \t]|$)")

# --- the `Owns:` glob ---------------------------------------------------------------------------

# The two classes an edge reads beside one literal character, each longer than one character.
SLASHLESS = "[^/]"
ANYTHING = "**"
# An automaton: each state's edges as (class, next state), and its accepting states.
Edges = list[list[tuple[str, int]]]


# A pattern is compared with every tracked path, so each is built once; nothing changes an automaton built.
@functools.cache
def _automaton(pattern: str, *, exact: bool = False) -> tuple[Edges, set[int]]:
    """The paths a pattern names, as an automaton with no empty moves; `exact` reads it as one path alone."""
    edges: Edges = [[]]

    def step(here: int, read: str) -> int:
        edges.append([])
        edges[here].append((read, len(edges) - 1))
        return len(edges) - 1

    pattern = pattern.replace("\\", "/").removeprefix("./")
    here = 0
    if exact or "*" not in pattern:
        for char in pattern if exact else pattern.rstrip("/"):
            here = step(here, char)
        if exact:
            return edges, {here}
        # A path names itself and, as a folder, every path under it.
        below = step(here, "/")
        edges[below].append((ANYTHING, below))
        return edges, {here, below}
    segments = pattern.split("/")
    for k, segment in enumerate(segments):
        last = k == len(segments) - 1
        if segment == "**":
            if last:
                edges[here].append((ANYTHING, here))
            else:
                # Any number of whole folders, each closed by its `/`, back to this state.
                inside = step(here, SLASHLESS)
                edges[inside] += [(SLASHLESS, inside), ("/", here)]
            continue
        for char in segment:
            if char == "*":
                edges[here].append((SLASHLESS, here))
            else:
                here = step(here, char)
        if not last:
            here = step(here, "/")
    return edges, {here}


def _meet(one: str, other: str) -> bool:
    """Whether two edge classes share a character."""
    if ANYTHING in (one, other):
        return True
    if one == SLASHLESS:
        return other != "/"
    if other == SLASHLESS:
        return one != "/"
    return one == other


def _share(one: tuple[Edges, set[int]], other: tuple[Edges, set[int]]) -> bool:
    """Whether some path is named by both automata: a walk of the pairs of their states."""
    (left, left_end), (right, right_end) = one, other
    seen = {(0, 0)}
    todo = [(0, 0)]
    while todo:
        here, there = todo.pop()
        if here in left_end and there in right_end:
            return True
        for read, to in left[here]:
            for other_read, other_to in right[there]:
                if (to, other_to) not in seen and _meet(read, other_read):
                    seen.add((to, other_to))
                    todo.append((to, other_to))
    return False


def owned(patterns: Iterable[str], path: str) -> bool:
    """Whether a repository path, `/`-separated as git prints it, is one the patterns name."""
    held = _automaton(path, exact=True)
    return any(_share(held, _automaton(pattern)) for pattern in patterns)


@functools.cache
def _start(pattern: str) -> str:
    """The text every path the pattern names begins with: its literal part before the first `*`."""
    pattern = pattern.replace("\\", "/").removeprefix("./")
    return pattern.partition("*")[0] if "*" in pattern else pattern.rstrip("/")


def overlap(one: str, other: str) -> bool:
    """Whether two patterns name a path in common, whether or not that path exists yet.

    Two patterns whose literal starts diverge share none, which settles most pairs without the automata.
    """
    left, right = _start(one), _start(other)
    if not (left.startswith(right) or right.startswith(left)):
        return False
    return _share(_automaton(one), _automaton(other))


# --- the header ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    """What a brief's header holds; a value absent from the file is empty."""

    agent: str
    type: str
    slice: str
    base: str
    report: str
    # The `Owns:` patterns, `(new)` removed, and those of them marked new.
    owns: tuple[str, ...]
    new: tuple[str, ...]
    rows: tuple[str, ...]
    writes: tuple[str, ...]
    lens: tuple[str, ...]
    # The model profile the `Model:` line names; None where the brief has none.
    model: str | None
    # Each list label the header carries, with its value as written.
    labels: dict[str, str]


def brief_path(state: Path, name: str) -> Path:
    # Refuses a name no agent can hold, which would make no safe file name either.
    model.agent_path(state, name)
    return state / BRIEFS / f"{name}.md"


def _entries(value: str) -> list[str]:
    if value.strip().lower() == NONE:
        return []
    return [entry.strip().strip("`") for entry in value.split(",") if entry.strip().strip("`")]


def _head(lines: list[str]) -> list[str]:
    """The header's lines: every line above the first section."""
    end = next((k for k, line in enumerate(lines) if SECTION_RE.match(line)), len(lines))
    return lines[:end]


def parse(text: str) -> Header:
    """The header of a brief's text."""
    lines = text.splitlines()
    head = _head(lines)
    first = FIRST_RE.match(head[0]) if head else None
    labels: dict[str, str] = {}
    base = report = ""
    profile: str | None = None
    for line in head[1:]:
        if line.startswith("Base:"):
            base = line.removeprefix("Base:").strip()
        elif line.startswith("Report:"):
            report = line.removeprefix("Report:").strip()
        elif line.startswith(f"{MODEL}:"):
            profile = line.removeprefix(f"{MODEL}:").strip() or None
        elif (found := LABEL_RE.match(line)) and found["label"] in LISTS:
            # A label written twice is read as one list, so no path is dropped in silence.
            held = labels.get(found["label"])
            value = found["value"] or ""
            labels[found["label"]] = value if held is None else ", ".join(part for part in (held, value) if part)
    owns: list[str] = []
    new: list[str] = []
    for entry in _entries(labels.get("Owns", "")):
        pattern = entry.removesuffix(NEW).strip().strip("`")
        owns.append(pattern)
        if entry.endswith(NEW):
            new.append(pattern)
    return Header(
        agent=first["agent"] if first else "",
        type=first["type"] if first else "",
        slice=first["slice"] if first else "",
        base=base,
        report=report,
        owns=tuple(owns),
        new=tuple(new),
        rows=tuple(_entries(labels.get("Rows", ""))),
        writes=tuple(_entries(labels.get("Writes", ""))),
        lens=tuple(_entries(labels.get("Lens", ""))),
        model=profile,
        labels=labels,
    )


def header(state: Path, name: str) -> Header:
    """The header of the agent's brief; refused where the brief does not exist."""
    return parse(_read(state, name).decode("utf-8", "replace"))


def profile(head: Header, settings: config.Config) -> config.Model | None:
    """The model profile the brief names, else `default`; None where the configuration holds no `default`.

    Refused where the brief names a profile the configuration lacks, which would leave the agent on a
    model nobody chose.
    """
    if head.model is None:
        return settings.models.get(DEFAULT)
    if head.model not in settings.models:
        known = ", ".join(sorted(settings.models)) or "none"
        raise Refused(
            f"`{MODEL}:` names the profile {head.model}, which `models` in {config.CONFIG_FILE.as_posix()} lacks;"
            f" its profiles: {known}"
        )
    return settings.models[head.model]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stamped(state: Path, agent: model.Agent) -> bool:
    """Whether the agent's brief is, byte for byte, the brief its check passed."""
    if agent.checked is None:
        return False
    try:
        return digest(store.read_bytes(brief_path(state, agent.name))) == agent.checked
    except FileNotFoundError:
        return False


# --- the verb -----------------------------------------------------------------------------------


def add(_verb: str, parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name", help="the agent's name: upper-case letters, digits and hyphens")
    parser.add_argument("--type", choices=typing.get_args(model.AgentType), help="the agent's type")
    parser.add_argument(
        "--slice",
        help=(
            f"the plan's slice the agent works on; a reader's or a driver's is `{NONE}` unless given,"
            " a slice of no plan"
        ),
    )
    parser.add_argument(
        "--body",
        type=Path,
        metavar="FILE",
        help="a file holding the lists and the four sections: written below the header, then the brief checked",
    )
    parser.add_argument(
        "--check", action="store_true", help="check the brief and stamp it, or refuse naming each fault"
    )


def run(args: argparse.Namespace) -> int:
    here = config.checkout()
    state = store.state_dir(held=here)
    if args.check:
        if args.type or args.slice or args.body:
            raise Refused("--check takes the name alone: the header already names the type and the slice")
        return check(args, state, here.root, args.name)
    slice_ = args.slice if args.slice is not None or args.type == SLICED else NONE
    if not args.type or not slice_ or not slice_.strip():
        raise Refused("a new brief needs --type, and --slice for a writer; `--check` checks one written")
    # Read before anything is written, so a body refused leaves no record and no brief behind.
    body = None if args.body is None else _body(args.body)
    made = write(state, here.root, args, cli.one_line(slice_), body)
    return made if body is None else check(args, state, here.root, args.name)


def _body(path: Path) -> list[str]:
    """The lines of a brief's body file: its lists and its four sections, the header's first three lines left out.

    The verb writes those three from the state and git, so a value typed there would stand in for them.
    """
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise Refused(f"the body {path} cannot be read: {error}") from None
    lines = text.splitlines()
    typed = [line for line in _head(lines) if FIRST_RE.match(line) or line.startswith(("Base:", "Report:"))]
    if typed:
        raise Refused(f"the body {path} holds `{typed[0]}`: leave the header's first three lines to the verb")
    return lines


def _planned(state: Path, slice_: str) -> list[str]:
    """The files the plan's line for the slice names in backticks, as `status` reads that line; none without one."""
    # Imported here: the hooks import this module and none of them reads the status view, which imports this one.
    view = importlib.import_module("orchestra.status")
    read = typing.cast("Callable[[str], tuple[str, list[str], list[str]]]", view._slice)
    heading = typing.cast("str", importlib.import_module("orchestra.init").SLICES)
    try:
        lines = model.plan_lines(state)
    except Refused:
        # No plan names the slice's files, so the label is left to be filled, as for a line naming none.
        return []
    for section, _, text in lines:
        if section == heading and text:
            held, files, _ = read(text)
            if held == slice_:
                return files
    return []


def write(state: Path, root: Path, args: argparse.Namespace, slice_: str, body: list[str] | None) -> int:
    """The header of the brief `args` names written from the state and git, with `body`'s lines below it where given.

    A body naming no `Owns:` gets the one its slice's line in PLAN.md names; one naming it keeps its own,
    such as the planned paths with `(new)` added.
    """
    name: str = args.name
    kind: model.AgentType = args.type
    path = brief_path(state, name)
    if path.exists():
        raise Refused(f"{path} exists, and what is written in it would be lost: edit it, or choose another name")
    session = model.read_session(state)
    commit = config.git(root, "rev-parse", "--short", "--verify", f"refs/heads/{session.branch}").strip()
    # A writer's files are those its slice's line names; a reader's or a driver's slice owns none of them.
    owns = _planned(state, slice_) if kind == SLICED else []

    def create(held: store.Record | None) -> store.Record:
        if held is not None:
            raise Refused(f"{name} has a record already, as {held.get('type')}: choose another name")
        made = model.Agent(name, kind, since={"briefed": model.now()})
        return model.to_record(made)

    store.update_json(model.agent_path(state, name), create)
    first = [
        f"Agent: {name}    Type: {kind}    Slice: {slice_}",
        f"Base: {session.branch} at {commit}",
        f"Report: {report.path(state, name)}",
    ]
    if body is None:
        rest = [
            *(f"{label}: {', '.join(owns)}" if label == "Owns" and owns else f"{label}:" for label in LISTS),
            "",
            *SECTIONS,
        ]
    else:
        named = any((found := LABEL_RE.match(line)) and found["label"] == "Owns" for line in _head(body))
        rest = [*([f"Owns: {', '.join(owns)}"] if owns and not named else []), *body]
    store.write_text(path, "\n".join([*first, *rest, ""]))
    store.write_text(path.with_name(f"{name}.messages.md"), "")
    print(path)
    if body is not None:
        # The check that follows says what the brief still lacks.
        return DONE
    briefed = f"{name} briefed as {report.article(kind)} on {slice_}"
    if owns:
        print(
            f"{briefed}: `Owns:` holds the {len(owns)} path(s) its line in PLAN.md names;"
            " fill Rows, Writes and Lens and the four sections, then --check"
        )
    else:
        print(f"{briefed}: fill Owns, Rows, Writes and Lens and the four sections, then --check")
    return DONE


def _sections(lines: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """The sections in the order written, and each one's lines, its label's own line's text first."""
    order: list[str] = []
    held: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines:
        if found := SECTION_RE.match(line):
            order.append(found["section"])
            current = held.setdefault(found["section"], [])
            current.append(found["text"] or "")
        elif current is not None:
            current.append(line)
    return order, held


def _items(work: list[str]) -> list[str]:
    """The WORK section's numbered items, each with the lines that follow it."""
    items: list[str] = []
    for line in work:
        if ITEM_RE.match(line):
            items.append(line)
        elif items:
            items[-1] += "\n" + line
    return items


def _header_faults(state: Path, agent: model.Agent, lines: list[str], head: Header) -> list[str]:
    """Each header value missing, or differing from what the record and the state say it is."""
    faults: list[str] = []
    if not FIRST_RE.match(lines[0] if lines else ""):
        faults.append("the first line is not `Agent: <NAME>    Type: <type>    Slice: <slice>`")
    else:
        if head.agent != agent.name:
            faults.append(f"`Agent:` names {head.agent or 'nobody'}, not {agent.name}")
        if head.type != agent.type:
            faults.append(f"`Type:` says {head.type or 'nothing'}, and the record says {agent.type}")
        if not head.slice:
            faults.append("`Slice:` is empty")
    if not BASE_RE.match(f"Base: {head.base}"):
        faults.append("`Base:` is not `<branch> at <commit>`")
    if head.report != str(report.path(state, agent.name)):
        faults.append(f"`Report:` is not {report.path(state, agent.name)}")
    faults += [
        f"`{label}:` has no value: name what it holds, or write `{NONE}`"
        for label in LISTS
        if not head.labels.get(label, "").strip()
    ]
    return faults + [
        f"`Rows:` entry `{row}` is no row id: <NAME>-F<n>" for row in head.rows if not model.ROW_RE.fullmatch(row)
    ]


def _reach(entry: str) -> str:
    """The folder a `Writes:` entry reaches at the least: its folders before the first one holding `*`."""
    parts = entry.replace("\\", "/").split("/")
    fixed = "/".join(itertools.takewhile(lambda part: "*" not in part, parts))
    return fixed or ("/" if entry.startswith(("/", "\\")) else ".")


def _writes_faults(state: Path, root: Path, head: Header) -> list[str]:
    """Each `Writes:` entry reaching the checkout or the state, or covering the home folder.

    A reader runs in the primary checkout with no worktree of its own, so an entry there lets it write
    any file of the project; the state's files are orchestra's, and its report and scratch folder are
    admitted without an entry.
    """
    home = Path.home()
    faults: list[str] = []
    for entry in head.writes:
        reach = (root / _reach(entry)).resolve()
        for folder, named in ((root, "the checkout"), (state, "the session's state")):
            if config.within(reach, folder) or config.within(folder, reach):
                faults.append(
                    f"`Writes:` entry `{entry}` reaches {named}, {folder}: name a folder outside it;"
                    " the report and the scratch folder need no entry"
                )
        if config.within(home, reach):
            faults.append(f"`Writes:` entry `{entry}` covers the home folder, {Path.home()}: name a folder of its own")
        elif reach.parent == reach:
            # Another drive's root holds no home folder, and is as wide.
            faults.append(f"`Writes:` entry `{entry}` covers a file system's root, {reach}: name a folder of its own")
    return faults


def _body_faults(lines: list[str], head: Header) -> list[str]:
    """Each section missing, empty or out of order, and each WORK item lacking `Accept:`, or `At:` beside rows."""
    faults: list[str] = []
    order, held = _sections(lines[len(_head(lines)) :])
    if order != list(SECTIONS):
        shown = ", ".join(order) or "none"
        faults.append(f"the sections are {shown}: write {', '.join(SECTIONS)}, once each, in order")
    faults += [f"{name} is empty" for name in SECTIONS if name in held and not "".join(held[name]).strip()]
    items = _items(held.get("WORK", []))
    if "WORK" in held and not items:
        faults.append("WORK holds no numbered item: `1. <the change> Accept: <the check>`")
    for k, item in enumerate(items, 1):
        if "Accept:" not in item:
            faults.append(f"WORK item {k} has no `Accept:`: name the check and what it asserts")
        if head.rows and "At:" not in item:
            faults.append(f"WORK item {k} has no `At:`, and the brief carries rows: name the symbol, path or rule")
    return faults


def _tracked(root: Path, branch: str) -> list[str]:
    listed = config.git(root, "ls-tree", "-r", "-z", "--name-only", f"refs/heads/{branch}")
    return [path for path in listed.split("\0") if path]


def _changed(root: Path, branch: str, ref: str, tree: Path | None) -> set[str]:
    """The paths a ref's commits beyond the session branch change, and those a worktree's uncommitted work changes."""
    paths: set[str] = set()
    try:
        config.git(root, "merge-base", "--is-ancestor", ref, f"refs/heads/{branch}")
    except Refused:
        paths |= set(config.git(root, "diff", "-z", "--name-only", f"refs/heads/{branch}...{ref}").split("\0"))
    if tree is not None and tree.is_dir():
        # Without `core.longpaths`, Git for Windows with the `core.fscache` its installer sets reads a path past 260
        # characters, as an agent's worktree under `.claude/worktrees/` can hold, as deleted and still exits 0; other
        # platforms ignore the key.
        listed = config.git(tree, "-c", "core.longpaths=true", "status", "--porcelain", "-z", "--untracked-files=all")
        entries = iter(listed.split("\0"))
        for entry in entries:
            paths.add(entry[3:])
            # A rename or a copy names its source in the entry after it.
            if entry[:1] in {"R", "C"}:
                paths.add(next(entries, ""))
    return paths - {""}


def _frozen(tree: Path) -> bool:
    """Whether a worktree is a review tree `diff --tree` made: a folder in a session state's `trees/`.

    Told by the folder's shape, a `trees/` beside the state's session file, since the desktop app can
    register the redirected physical path, which no configured root names.
    """
    return tree.parent.name == store.TREES and (tree.parent.parent / store.SESSION).is_file()


def _unlanded(root: Path, branch: str, agents: list[model.Agent], name: str) -> dict[str, set[str]]:
    """Each other worktree's and each live agent's branch's changed paths, by who holds it.

    A driver's are left out: it commits nothing and reverts what it plants, so nothing of its worktree lands.
    So is a review tree, any session's, which a lens reader reads and nothing lands from.
    """
    owner = {agent.branch: agent.name for agent in agents if agent.branch}
    planting = {agent.branch for agent in agents if agent.branch and agent.type == "driver"}
    found: dict[str, set[str]] = {}
    seen: set[str] = set()
    # The first is the primary checkout, which holds the session branch itself.
    for tree in config.worktrees(root)[1:]:
        held = tree.get("branch", "")
        if owner.get(held) == name or held in planting or _frozen(Path(tree["worktree"])):
            continue
        seen.add(held)
        who = f"{owner[held]} ({held})" if held in owner else held or tree["worktree"]
        ref = f"refs/heads/{held}" if held else tree.get("HEAD", "")
        found[who] = _changed(root, branch, ref, Path(tree["worktree"]))
    for agent in agents:
        if (
            agent.branch
            and agent.name != name
            and agent.status not in model.SETTLED
            and agent.branch not in seen
            and agent.branch not in planting
        ):
            try:
                config.git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{agent.branch}")
            except Refused:
                continue
            found[f"{agent.name} ({agent.branch})"] = _changed(root, branch, f"refs/heads/{agent.branch}", None)
    return found


def _live(state: Path, others: list[model.Agent]) -> list[model.Agent]:
    """The other agents whose work is under way: running and not ended, or briefed with a check that still holds.

    A briefed agent whose check holds is about to be dispatched, so two briefs checked before either
    is dispatched are compared with each other.
    """
    return [
        other
        for other in others
        if (other.status == "running" and findings.is_live(other))
        or (other.status == "briefed" and stamped(state, other))
    ]


def _collisions(state: Path, root: Path, agent: model.Agent, head: Header) -> list[str]:
    """Each `Owns:` entry no file answers unmarked, each overlap with another agent's work, each row held elsewhere."""
    session = model.read_session(state)
    tracked = _tracked(root, session.branch)
    faults = [
        f"`Owns:` entry `{pattern}` names no file on {session.branch}: correct it, or mark a file still to be made"
        f" `{pattern} {NEW}`"
        for pattern in head.owns
        if pattern not in head.new and not any(owned([pattern], path) for path in tracked)
    ]
    agents = model.agents(state)
    others = [other for other in agents if other.name != agent.name]
    for other in _live(state, others):
        theirs = header(state, other.name)
        faults += [
            f"`{mine}` overlaps {other.name}'s `{pattern}` ({other.status})"
            for mine in head.owns
            for pattern in theirs.owns
            if overlap(mine, pattern)
        ]
        faults += [
            f"`{row}` is in {other.name}'s `Rows:` too ({other.status})" for row in head.rows if row in theirs.rows
        ]
    for who, paths in _unlanded(root, session.branch, agents, agent.name).items():
        faults += [f"{path} is changed by {who}, unlanded" for path in sorted(paths) if owned(head.owns, path)]
    return faults + _held_rows(state, others, head)


def _profile_faults(root: Path, head: Header) -> list[str]:
    """The `Model:` line's profile, where the configuration lacks it."""
    try:
        profile(head, config.load(root))
    except Refused as refused:
        return [str(refused)]
    return []


def _held_rows(state: Path, others: list[model.Agent], head: Header) -> list[str]:
    """Each row the findings file lacks, and each routed to another agent whose branch has not landed.

    A `running` agent whose transcript shows it ended holds no row: the successor's dispatch takes it over.
    """
    rows = model.read_findings(state)
    holders = {
        other.name: other
        for other in others
        if other.status not in model.SETTLED and (other.status != "running" or findings.is_live(other))
    }
    faults: list[str] = []
    for row in head.rows:
        held = rows.get(row)
        if held is None:
            if model.ROW_RE.fullmatch(row):
                faults.append(f"`{row}` is no banked row: `orchestra findings` lists them")
        elif held.status == "ROUTED" and held.owner in holders:
            faults.append(f"`{row}` is routed to {held.owner}, which is {holders[held.owner].status}")
    return faults


def _read(state: Path, name: str) -> bytes:
    path = brief_path(state, name)
    try:
        return store.read_bytes(path)
    except FileNotFoundError:
        raise Refused(
            f"{name} has no brief at {path}: `orchestra brief {name} --type T --body FILE`, with `--slice S` for a"
            " writer, writes one from a file holding its lists and its four sections, and checks it"
        ) from None


def check(args: argparse.Namespace, state: Path, root: Path, name: str) -> int:
    agent = model.read_agent(state, name)
    data = _read(state, name)
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    head = parse(text)
    faults = _header_faults(state, agent, lines, head) + _writes_faults(state, root, head) + _profile_faults(root, head)
    faults += _body_faults(lines, head)
    faults += _collisions(state, root, agent, head)
    # A failed check also clears an earlier stamp: what it found, such as an overlap, arrived since.
    stamp = None if faults else digest(data)
    model.update_agent(state, name, lambda held: dataclasses.replace(held, checked=stamp))
    path = brief_path(state, name)
    if faults:
        cli.emit(args, faults, f"{name}'s brief is not checked: {len(faults)} fault(s), {path}")
        return REFUSED
    cli.emit(args, [], f"{name}'s brief is checked: {path}")
    return DONE
