"""`diff`: the session's change captured for a lens round, a tree that stays put, and the lenses it makes due.

The change runs from the commit the session was based on to the session branch's tip. git writes the
diff and the span's commit messages itself (`--output`), so their bytes are git's whatever the
files' encodings. A lens reader that
needs more than the change reads `--tree`'s detached worktree, which stays at the captured commit
while the session branch moves on; `prune` removes it at the ending.

Each lens file states when it is due and which agent type runs it, in two lines the program reads:
`**Applies.**` and `**Run by.**`. A file of the project's own under `.claude/orchestra/lenses/`
replaces the plugin's of its name, and `lenses.triggers` in the configuration adds the project's
paths, so the lenses due follow from the changed paths rather than from a list someone remembers.
"""

from __future__ import annotations

import argparse
import re
import typing
from dataclasses import dataclass
from pathlib import Path

from orchestra import DONE, Refused, brief, cli, config, log, model, report, store

# The log kind naming a tree made, for a reader of the log; `prune --ending` finds the trees by their folder.
KIND = "tree"
# A user's configured colour, external diff tool or text conversion would change the bytes a reader gets.
PLAIN = ("--no-color", "--no-ext-diff", "--no-textconv")
# Each commit with its author and committer as recorded: a user's mailmap, decoration or signature
# check would change what a reader of what gets published sees.
MESSAGES = ("--no-color", "--no-decorate", "--no-show-signature", "--no-mailmap", "--format=fuller")

PLUGIN_LENSES = Path(__file__).resolve().parents[2] / "lenses"
PROJECT_LENSES = Path(".claude", "orchestra", "lenses")
# Each label's value runs to the line's end, one closing full stop dropped; a project's file checked out
# with CRLF line ends, as Git for Windows' `core.autocrlf` does, ends each line on a carriage return.
APPLIES_RE = re.compile(r"^\*\*Applies\.\*\*[ \t]+(?P<value>.*?)\.?[ \t\r]*$", re.MULTILINE)
RUN_BY_RE = re.compile(r"^\*\*Run by\.\*\*[ \t]+(?P<value>.*?)\.?[ \t\r]*$", re.MULTILINE)
LIST = r"`[^`]+`(?:, `[^`]+`)*"
PATHS_RE = re.compile(rf"paths (?P<paths>{LIST})(?: except (?P<except>{LIST}))?")
PATTERN_RE = re.compile(r"`([^`]+)`")
RUNNERS = ("auditor", "driver", "researcher")
# The forms of `**Applies.**` besides `paths`: `declared` lenses are due only through the project's triggers.
ALWAYS, DECLARED, ON_REQUEST = "always", "declared", "on request"
FORMS = (ALWAYS, DECLARED, ON_REQUEST)
# The note declining a due lens, which the ending reads: `orchestra note "Lens declined: <name>, <reason>"`.
DECLINED_RE = re.compile(r"Lens declined: (?P<name>[^,\s]+), (?P<reason>\S.*)")


@dataclass(frozen=True)
class Lens:
    name: str
    path: Path
    # One of FORMS, or "paths" with the patterns below.
    applies: str
    paths: tuple[str, ...]
    excepted: tuple[str, ...]
    run_by: model.AgentType


@dataclass(frozen=True)
class Due:
    """A lens the change makes due, or one the project has not declared, which is due on nothing."""

    lens: Lens
    # The changed paths that made it due; empty where every change makes it due, or where it is undeclared.
    files: tuple[str, ...] = ()
    every: bool = False
    # Whether its `declared` trigger is missing from the configuration.
    undeclared: bool = False

    @property
    def due(self) -> bool:
        return self.every or bool(self.files)


def _value(pattern: re.Pattern[str], text: str, label: str, path: Path) -> str:
    found = [match["value"] for match in pattern.finditer(text)]
    if len(found) != 1:
        raise Refused(f"{path} holds {len(found)} `**{label}.**` lines, and a lens file holds one")
    return found[0]


def parse(name: str, path: Path, text: str) -> Lens:
    """A lens file's two lines read; a line missing, repeated or holding no form it takes refused."""
    applies = _value(APPLIES_RE, text, "Applies", path)
    run_by = _value(RUN_BY_RE, text, "Run by", path)
    paths: tuple[str, ...] = ()
    excepted: tuple[str, ...] = ()
    form = applies
    if match := PATHS_RE.fullmatch(applies):
        form = "paths"
        paths = tuple(PATTERN_RE.findall(match["paths"]))
        excepted = tuple(PATTERN_RE.findall(match["except"] or ""))
    elif form not in FORMS:
        shown = ", ".join(f"`{held}`" for held in FORMS)
        raise Refused(
            f"{path}: `**Applies.** {applies}` is none of {shown} or `paths` and backticked patterns,"
            " `except` and more before any it leaves out"
        )
    if run_by not in RUNNERS:
        raise Refused(f"{path}: `**Run by.** {run_by}` names none of {', '.join(RUNNERS)}")
    return Lens(name, path, form, paths, excepted, typing.cast("model.AgentType", run_by))


def lenses(root: Path) -> dict[str, Lens]:
    """Every lens by name: the plugin's, each replaced by a file of the project's own of its name."""
    found: dict[str, Path] = {}
    for folder in (PLUGIN_LENSES, root / PROJECT_LENSES):
        if folder.is_dir():
            found |= {path.stem: path for path in sorted(folder.glob("*.md"))}
    held: dict[str, Lens] = {}
    for name, path in found.items():
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise Refused(f"{path} cannot be read: {error}") from None
        held[name] = parse(name, path, text)
    return held


def triggers(found: dict[str, Lens], settings: config.Config) -> dict[str, tuple[str, ...]]:
    """The project's paths for each lens it declares; refused where one names no lens file."""
    declared: dict[str, tuple[str, ...]] = {}
    for k, trigger in enumerate(settings.lenses.triggers):
        if trigger.name not in found:
            raise Refused(
                f"{config.CONFIG_FILE.as_posix()}: `lenses.triggers[{k}]` names `{trigger.name}`, which is no lens"
                f" file in the plugin's lenses/ or {PROJECT_LENSES.as_posix()}/"
            )
        declared[trigger.name] = declared.get(trigger.name, ()) + trigger.when
    return declared


def weighed(lens: Lens, declared: tuple[str, ...] | None, changed: list[str]) -> Due | None:
    """Whether the changed paths make the lens due, it is undeclared, or neither (None).

    `declared` is the project's paths for it, None where the project names none; the project's paths
    add to the file's own.
    """
    if not changed:
        return None
    if lens.applies == ALWAYS:
        return Due(lens, every=True)
    files = [
        path
        for path in changed
        if (brief.owned(lens.paths, path) and not brief.owned(lens.excepted, path))
        or (declared is not None and brief.owned(declared, path))
    ]
    if files:
        return Due(lens, tuple(files))
    if declared is None and lens.applies == DECLARED:
        return Due(lens, undeclared=True)
    return None


def due(root: Path, settings: config.Config, changed: list[str]) -> list[Due]:
    """Each lens the changed paths make due or the project leaves undeclared, by name."""
    found = lenses(root)
    declared = triggers(found, settings)
    return [held for name, lens in sorted(found.items()) if (held := weighed(lens, declared.get(name), changed))]


def said(held: Due) -> str:
    """A lens's printed line: its name, the agent type that runs it and why it is due."""
    lens = held.lens
    why = (
        "every change"
        if held.every
        else ", ".join(held.files)
        if held.files
        else f"undeclared; give its paths, or none, under `lenses.triggers` in {config.CONFIG_FILE.as_posix()}"
    )
    return f"lens     {lens.name}, by {report.article(lens.run_by)}: {why}"


def changed_paths(root: Path, span: str) -> list[str]:
    """Every path the span touches; a rename as both its paths, so a lens due on the old one is due."""
    return [name for name in config.git(root, "diff", "--name-only", "--no-renames", "-z", span).split("\0") if name]


def declined(state: Path) -> dict[str, str]:
    """Each lens a note declined, by name, with the reason the note gives."""
    return {
        match["name"]: match["reason"]
        for entry in log.entries(state)
        if entry.kind == "note" and (match := DECLINED_RE.fullmatch(entry.text))
    }


def checked(root: Path, settings: config.Config) -> list[str]:
    """`doctor`'s line for the lens files and the configuration's triggers."""
    try:
        found = lenses(root)
        declared = triggers(found, settings)
    except Refused as refused:
        return [f"FAILED   {refused}"]
    return [f"ok       {len(found)} lens files, {len(declared)} declared by {config.CONFIG_FILE.as_posix()}"]


def add(_verb: str, parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tree", action="store_true", help="also check the tip out as a detached worktree a lens can read"
    )


def run(args: argparse.Namespace) -> int:
    here = config.checkout()
    state = store.state_dir(held=here)
    # git writes the files and the worktree past the store's guarded writers.
    store.check_lease(state)
    session = model.read_session(state)
    root = here.root
    tip = config.git(root, "rev-parse", "--verify", f"refs/heads/{session.branch}^{{commit}}").strip()
    span = f"{session.base_commit}..{tip}"
    path = state / store.DIFFS / f"{session.base_commit[:12]}..{tip[:12]}.diff"
    path.parent.mkdir(parents=True, exist_ok=True)
    config.git(root, "diff", *PLAIN, f"--output={path}", span)
    messages = path.with_suffix(".messages")
    config.git(root, "log", *MESSAGES, f"--output={messages}", span)
    changed = changed_paths(root, span)
    lines = [str(path), str(messages)]
    if args.tree:
        lines.append(str(tree(state, root, tip)))
    found = due(root, config.load(root), changed)
    lines += [said(held) for held in found]
    count = sum(held.due for held in found)
    summary = f"{len(changed)} file(s) changed from {session.base_commit[:9]} to {tip[:9]}; {count} lens(es) due"
    if undeclared := sum(not held.due for held in found):
        summary += f", {undeclared} undeclared"
    cli.emit(args, lines, summary)
    return DONE


def tree(state: Path, root: Path, commit: str) -> Path:
    """A detached worktree of the commit under the state's `trees/`, made once and logged for the ending.

    A lens reading the primary checkout reads whatever a landing merges into it meanwhile; a detached
    worktree has no branch to move.
    """
    path = state / store.TREES / commit
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # The state's folder is long on Windows, so a deep tracked path passes 260 characters under it, which Git
        # for Windows refuses ("Filename too long") unless `core.longpaths` is set, whatever Windows' own setting;
        # other platforms ignore the key.
        config.git(root, "-c", "core.longpaths=true", "worktree", "add", "--detach", str(path), commit)
        log.record(state, KIND, str(path))
    return path
