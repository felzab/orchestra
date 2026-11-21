"""`init` and `doctor`: a session's state made on its branch, and the checks a session needs first.

`init` writes the pointer last, so a session that failed halfway is never found; it refuses a
session that exists rather than mix two sessions' state. `doctor` names each failure and exits 1
while one remains.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import typing
from pathlib import Path

from orchestra import DONE, REMAINS, Refused, cli, config, diff, log, model, process, prune, store

# A programme's and a session's name is a folder name on every platform.
FOLDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
FORCE = "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"
# Set, it runs every agent in the foreground, and a foreground dispatch gets its id only once it ends
# (sub-agents.md), so the agent's hooks find no record of it while it works.
FOREGROUND = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"
# Set, an Agent call carrying a `name` and no `isolation` launches a teammate rather than a sub-agent
# (sub-agents.md, "Subagent names"), whose turn ends on `TeammateIdle`, which the plugin does not answer.
TEAMS = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"
# The settings key naming a working directory.
DIRECTORIES = "additionalDirectories"
# The auto-compact window `init` sets where none is set: each call sends the whole conversation again, and on
# one long coordinator day of a model with a 1M context, replayed request by request, this window cost about 0.67
# of the default there, which compacts near 967,000 tokens, and 600,000 about 1.1 of this one.
WINDOW_TOKENS = 500_000

# The request first, in the owner's words verbatim, so a session resumed after a crash or a `/clear` reads
# what was asked rather than a paraphrase. The slices' heading is `model`'s, where the plan's readers find it.
PLAN_HEADINGS = ("## The request", model.SLICES, "## Landing order", "## Ending")
# Only what the state cannot give: where the session waits, the newest estimate and the last gate run
# are in the log and the state's folders, and `status` prints them.
NOW_LABELS = ("Next action:", "Why it is next:")
# A preflight command is a quick probe such as `docker info`; one hanging this long is reported as failing.
PREFLIGHT_SECONDS = 60


def add(verb: str, parser: argparse.ArgumentParser) -> None:
    if verb == "init":
        parser.add_argument("session", help="the session's name, a folder name")
        parser.add_argument(
            "--programme",
            help="the programme it belongs to; the primary checkout's folder name unless given,"
            " refused where another checkout's sessions hold that name",
        )
        parser.add_argument(
            "--rulings-from", type=int, metavar="N", help="the number the programme's first ruling takes"
        )
        # A programme's sessions branch from its own branch, which belongs in no file every collaborator shares.
        parser.add_argument(
            "--base",
            metavar="BRANCH",
            help="the branch the session is cut from, recorded as its base; the configuration's `base` unless given",
        )


def run(args: argparse.Namespace) -> int:
    return init(args) if args.verb == "init" else doctor(args)


def _name(value: str, what: str) -> str:
    if not FOLDER_RE.fullmatch(value):
        raise Refused(f"`{value}` is no {what} name: letters, digits, `_`, `.` and `-`, a letter or digit first")
    return value


def init(args: argparse.Namespace) -> int:
    here = config.checkout()
    if here.branch is None:
        raise Refused(
            "no branch is checked out here, and a session belongs to its branch: cut the session's branch first"
        )
    settings = config.load(here.root)
    session = _name(args.session, "session")
    programme = _name(args.programme or here.root.name, "programme")
    if args.rulings_from is not None and args.rulings_from < 1:
        raise Refused("--rulings-from takes a ruling number, 1 or more")
    pointer = store.pointer(here.common_dir, here.branch)
    if pointer.exists() and (held := Path(store.read_bytes(pointer).decode("utf-8").strip())).is_dir():
        raise Refused(
            f"branch {here.branch} already holds the session {held}: resume it, or cut a new branch for a new session"
        )
    state = _state_root(settings.state_root_path(here.root), here.root, Path(programme, session))
    if args.programme is None and (others := _other_checkouts(state.parent, here.root)):
        raise Refused(
            f"the programme {programme} at {state.parent} holds sessions of another checkout,"
            f" {', '.join(others)}, or of this one before it moved: give this project a programme of its own"
            f" with `--programme <name>`, or join that one with `--programme {programme}`"
        )
    base, base_commit, source = _base(here.root, here.branch, args.base, settings.base)
    state.parent.mkdir(parents=True, exist_ok=True)
    try:
        state.mkdir()
    except FileExistsError:
        raise Refused(
            f"the session {programme}/{session} exists at {state}: choose another name, or resume it"
        ) from None
    try:
        for folder in store.FOLDERS:
            (state / folder).mkdir()
        facts = model.Session(
            programme,
            session,
            here.branch,
            base,
            base_commit,
            model.now(),
            rulings_from=args.rulings_from,
            checkout=str(here.root),
        )
        model.write_session(state, facts)
        store.write_text(state / store.PLAN, "# Plan\n\n" + "\n\n".join(PLAN_HEADINGS) + "\n")
        store.write_text(state / store.NOW, "# Now\n\n" + "\n".join(NOW_LABELS) + "\n")
        started = f"session {programme}/{session} on {here.branch}, based on {base} ({source}) at {base_commit[:9]}"
        log.record(state, "init", started)
        # The programme's folder alone: a grant of the whole root would open every other project's state too.
        # Before the pointer, so a grant that faults leaves no session and `init` runs again.
        said = grant(here.root, state.parent)
        store.write_text(pointer, f"{state}\n")
    except BaseException:
        shutil.rmtree(state, ignore_errors=True)
        raise
    print(f"{started}: {state}")
    print(said)
    return doctor(args)


def _base(root: Path, head: str, given: str | None, configured: str) -> tuple[str, str, str]:
    """The session's base, the commit where HEAD leaves it, and where the base was named; refused where unusable.

    `head` is the branch checked out. `given` is `--base`; without it, the configuration's `base`, and a branch
    holding commits that base lacks, as one cut from a programme's branch does, is refused below, naming `--base`.
    """
    if given is not None:
        base, source = given, "named by --base"
    else:
        base, source = configured, "the configuration's base"
    # A clone holding no branch of that name, which no edit of the shared configuration repairs. A local branch
    # alone, so the record holds its short name, by which `prune` spares the base among the branches it deletes:
    # `show-ref --verify` takes an exact ref path (git-show-ref(1)), where `rev-parse` reads a suffix such as
    # `~0` or `^` after it as another commit (gitrevisions(7)).
    branch = f"refs/heads/{base}"
    if config.run_git(root, "show-ref", "--verify", "--quiet", branch).returncode != 0:
        remote = f"refs/remotes/origin/{base}"
        if config.run_git(root, "show-ref", "--verify", "--quiet", remote).returncode == 0:
            raise Refused(
                f"the base {base} ({source}) is no branch in this clone: make it from origin's with"
                f" `git branch {base} origin/{base}`, and run init again"
            )
        raise Refused(
            f"the base {base} ({source}) is no branch in this clone, nor is origin/{base}: fetch it,"
            " or name the branch this session is cut from with `--base <branch>`"
        )
    try:
        base_commit = config.git(root, "merge-base", "HEAD", branch).strip()
    except Refused:
        shared = f", or set `base` in {config.CONFIG_FILE.as_posix()} for the project"
        raise Refused(
            f"the base {base} ({source}) shares no commit with HEAD here: name the branch this session is cut from"
            f" with `--base <branch>`{shared if given is None else ''}"
        ) from None
    # A session's branch is cut from its base just before `init`, so it holds nothing the base does not. One that
    # does was cut from another branch, as a programme's session is from the programme's branch, and the base
    # recorded here would count that branch's work as the session's own; only `--base` names such a base.
    ahead = 0 if given is not None else int(config.git(root, "rev-list", "--count", f"{base_commit}..HEAD"))
    if ahead:
        raise Refused(
            f"{head} holds {ahead} commit{'s' if ahead > 1 else ''} {base} does not, so a session based on {base}"
            f" ({source}) would count them as its own: name the branch it was cut from with `--base <branch>`,"
            f" or `--base {base}` to keep it"
        )
    return base, base_commit, source


def _other_checkouts(folder: Path, root: Path) -> list[str]:
    """Each checkout but `root` that a session of the programme's folder was made in.

    The default programme is the checkout's folder name, which an unrelated clone may share; a session
    an earlier build wrote names no checkout, and one that cannot be read is judged where it is used.
    Each checkout is named once, by its first spelling in order: sessions may record one under several.
    """
    spellings: set[str] = set()
    for path in folder.glob(f"*/{store.SESSION}"):
        try:
            held = model.read_session(path.parent).checkout
        except Refused:
            continue
        if held is not None and not config.identical(Path(held), root):
            spellings.add(held)
    others: list[str] = []
    for held in sorted(spellings):
        if not any(config.identical(Path(held), Path(named)) for named in others):
            others.append(held)
    return others


def _state_root(folder: Path, root: Path, session: Path) -> Path:
    """The session's state under the state root, refused where the grant would open too much or the checkout carry it.

    A root holding the checkout puts the state inside it where the programme is the checkout's folder name.
    """
    where = (
        "the home folder or one holding it"
        if config.within(Path.home(), folder)
        else "a file system's root"
        if folder.parent == folder
        else "inside the checkout, whose working tree would carry the state"
        if config.within(folder, root) or config.within(folder / session, root)
        else None
    )
    if where is not None:
        raise Refused(
            f"`stateRoot` resolves to {folder}, {where}: set it to a folder of its own outside the checkout"
            f" in {config.CONFIG_FILE.as_posix()}, or remove it for the platform's folder for application data"
        )
    return folder / session


def granted(root: Path, folder: Path) -> bool:
    """Whether a settings file makes the folder, or one holding it, a working directory of the session."""
    listed = [config.setting(path, "permissions", DIRECTORIES) for path in config.settings_files(root)]
    return any(
        isinstance(entry, str) and config.within(folder, root / Path(entry).expanduser())
        for held in listed
        if isinstance(held, list)
        for entry in typing.cast("list[object]", held)
    )


def _base_ref(root: Path) -> tuple[object, Path | None]:
    """`worktree.baseRef` as the session reads it, from the first settings file that sets it, and that file."""
    for path in config.settings_files(root):
        if (held := config.setting(path, "worktree", "baseRef")) is not None:
            return held, path
    return None, None


def _window(root: Path) -> str | None:
    """Why `init` sets no auto-compact window: the one in force, or one set for a model alone; None where none is set.

    `config.WINDOW_ENV` in this process's environment outranks every settings key, so a key written then would
    do nothing. A settings file's `env` reaches that environment only where Claude Code started the process, so
    run from a plain terminal, `init` does not see a window set there. A window `/autocompact` saved for one
    model under `modelSettings` binds that model alone, and a top-level key in the local file would override
    it, so that leaves `init` writing none too, saying a session on another model runs at Claude Code's
    default.
    """
    if held := os.environ.get(config.WINDOW_ENV):
        return (
            f"`{config.WINDOW_ENV}` is {held} in the environment, which outranks every settings key, so init sets"
            " no window"
        )
    for path in config.settings_files(root):
        # Parsed once for both keys.
        parsed = config.setting(path)
        if (found := config.setting_in(parsed, config.WINDOW)) is not None:
            return f"`{config.WINDOW}` is {json.dumps(found)} in {path}, so init sets no window"
        models = config.setting_in(parsed, "modelSettings")
        if not isinstance(models, dict):
            continue
        for name, entry in typing.cast("dict[str, object]", models).items():
            if (found := config.setting_in(entry, config.WINDOW)) is not None:
                return (
                    f"`modelSettings.{name}.{config.WINDOW}` is {json.dumps(found)} in {path}, which binds {name}"
                    " alone: a session on another model runs at Claude Code's default window, and init sets no"
                    " window that would override it"
                )
    return None


def _on(name: str) -> bool:
    # An on-or-off variable is on at `1`, `true`, `yes` or `on`, in any casing (env-vars.md).
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def tracked_local(root: Path) -> bool:
    """Whether the checkout's own settings file is tracked, which no ignore rule then reaches (gitignore(5))."""
    try:
        config.git(root, "ls-files", "--error-unmatch", "--", config.SETTINGS_LOCAL.as_posix())
    except Refused:
        return False
    return True


def exclude_local(root: Path) -> str | None:
    """The checkout's own settings file kept out of git where no rule ignores it; what was done, None for nothing.

    Claude Code adds it to the global excludes only when it writes the file first (settings.md), and an
    untracked file holding this machine's paths stops every landing and is one `git add` from a commit.
    """
    path = config.SETTINGS_LOCAL.as_posix()
    try:
        config.git(root, "check-ignore", "-q", "--", path)
    except Refused:
        exclude = Path(config.git(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
        exclude.parent.mkdir(parents=True, exist_ok=True)
        held = exclude.read_bytes() if exclude.is_file() else b""
        with exclude.open("ab") as out:
            out.write((b"" if not held or held.endswith(b"\n") else b"\n") + f"/{path}\n".encode())
        return f"added /{path} to {exclude}, so git ignores it"
    return None


def grant(root: Path, folder: Path) -> str:
    """The folder and the auto-compact window added to the checkout's own untracked settings; what was done.

    A write outside the working directories prompts in the default mode, and an agent's report is
    such a write. The window is added only where none is set (`_window`), since a window chosen
    already is the user's. The file is rewritten only where it reads back byte for byte as the harness
    formats JSON, so no key of the owner's changes; any other shape is left for the owner to edit.
    Whoever wrote it, an untracked file is kept out of git, since it stops every landing either way.
    """
    # Asked once: the exclusion between here and the write cannot change whether the file is tracked.
    tracked = tracked_local(root)
    # Before the write, so this machine's paths are never one `git add` from a commit.
    excluded = None if tracked else exclude_local(root)
    said = _grant(root, folder, tracked=tracked)
    return "\n".join(said if excluded is None else [excluded, *said])


def _grant(root: Path, folder: Path, *, tracked: bool) -> list[str]:
    said: list[str] = []
    wanted: list[str] = []
    needs_grant = not granted(root, folder)
    if needs_grant:
        wanted.append(f"{folder} to its `permissions.{DIRECTORIES}`")
    else:
        said.append(f"{folder} is a working directory already")
    window = _window(root)
    if window is None:
        wanted.append(f'`"{config.WINDOW}": {WINDOW_TOKENS}`')
    else:
        said.append(window)
    if not wanted:
        return said
    yourself = f"add {' and '.join(wanted)} yourself"
    return [
        *said,
        *_rewrite(root, folder if needs_grant else None, yourself, set_window=window is None, tracked=tracked),
    ]


def _rewrite(root: Path, folder: Path | None, yourself: str, *, set_window: bool, tracked: bool) -> list[str]:
    """The folder granted where given and the window set where asked, in one write of the local file; what was done.

    `yourself` says what to add by hand where the file is left as it is; `tracked`, whether git tracks the file.
    """
    path = root / config.SETTINGS_LOCAL
    if tracked:
        # Its every change would be one `git add` from a commit carrying this machine's paths.
        return [f"{path} is tracked, so it is left as it is: {yourself}"]
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        text = None
    except (OSError, UnicodeDecodeError):
        return [f"{path} cannot be read: {yourself}"]
    try:
        held: object = {} if text is None else json.loads(text)
    except ValueError:
        held = None
    settings = typing.cast("dict[str, object]", held)
    shaped = isinstance(held, dict) and (text is None or dumped(settings, text) == text)
    unshaped = [f"{path} is not in the shape orchestra rewrites, so it is left as it is: {yourself}"]
    if not shaped:
        return unshaped
    said: list[str] = []
    if folder is not None:
        permissions = settings.setdefault("permissions", {})
        listed = (
            typing.cast("dict[str, object]", permissions).setdefault(DIRECTORIES, [])
            if isinstance(permissions, dict)
            else None
        )
        if not isinstance(listed, list):
            return unshaped
        typing.cast("list[object]", listed).append(str(folder))
        said.append(f"added {folder} to `permissions.{DIRECTORIES}` in {path}")
    if set_window:
        settings[config.WINDOW] = WINDOW_TOKENS
        # Claude Code applies most edits to a running session, and its page names this key nowhere among those read
        # only at the start (settings.md, "When edits take effect"); but a desktop session started from a pre-warmed
        # process did not apply the window the project's file or this one sets (anthropics/claude-code issue 105316),
        # so the owner is told what was seen to reach one, and what is expected to but untested. The user's settings
        # reach every project, so writing there is the owner's call.
        said.append(f"set `{config.WINDOW}` to {WINDOW_TOKENS} in {path}, as no settings file sets a window")
        said.append(
            "a desktop session started from a pre-warmed process did not apply the window the project's or the local"
            " settings file sets (anthropics/claude-code issue 105316); `/autocompact 500k` typed inside such a"
            f" session was seen to apply, saving a window for its model in {config.claude_dir() / 'settings.json'},"
            f' and `"{config.WINDOW}": {WINDOW_TOKENS}` at the top level of that file is expected to reach it, untested'
        )
    store.replace_bytes(path, dumped(settings, text).encode("utf-8"))
    return said


def dumped(held: object, before: str | None) -> str:
    """Settings as Claude Code writes them, two-space indents and characters unescaped, ending as `before` did."""
    ending = "\n" if before is None or before.endswith("\n") else ""
    return json.dumps(held, indent=2, ensure_ascii=False) + ending


def _programs(here: config.Checkout, settings: config.Config) -> list[str]:
    """A line for each program the configuration names, found or not."""
    commands: list[tuple[tuple[str, ...], Path, str]] = []
    if settings.ci.workflow is not None:
        commands.append((("gh",), here.root, "`ci.workflow` is set"))
    commands += [(step.run, here.root / step.cwd, f"a `{step.kind}` landing step") for step in settings.land.steps]
    commands += [(check.run, here.root, check.why) for check in settings.preflight]
    if settings.gate is not None:
        commands.append((settings.gate.run, here.root, "`gate.run`, which `ci gate` runs"))
    return [
        f"{'ok' if config.program(command, cwd) else 'MISSING':<8} {command[0]} ({why})"
        for command, cwd, why in dict.fromkeys(commands)
    ]


def _harness(here: config.Checkout, settings: config.Config) -> list[str]:
    """A line for each setting of Claude Code's a session rests on."""
    lines: list[str] = []
    # While it is on, "Claude can't pass a model when it starts a subagent" (sub-agents.md), so `models` does nothing.
    if settings.models and _on(FORCE):
        lines.append(
            f"IGNORED  `models`: {FORCE} is set, so every agent runs on one model; unset it to choose by brief"
        )
    if _on(FOREGROUND):
        lines.append(
            f"SET      {FOREGROUND}: every agent runs in the foreground, and its record gets its id only"
            " once it ends, so its hooks find none while it works: unset it"
        )
    if _on(TEAMS):
        lines.append(
            f"SET      {TEAMS}: an auditor, researcher or verifier dispatched by name launches as a teammate,"
            " whose turn's end no hook of this plugin answers: unset it"
        )
    # The harness puts each agent's worktree there, and an unignored one is a change in this checkout.
    folder_rule = f"{prune.WORKTREES.as_posix()}/"
    if config.run_git(here.root, "check-ignore", "-q", "--no-index", "--", f"{folder_rule}agent").returncode != 0:
        lines.append(
            f"MISSING  an ignore rule for {folder_rule}, so each agent's worktree is a change in this checkout,"
            f" which a landing refuses: add the line `{folder_rule}` to .gitignore"
        )
    # A writer's worktree cut from the default branch lacks everything the session has landed.
    ref, where = _base_ref(here.root)
    if ref == "head":
        lines.append('ok       worktree.baseRef is "head"')
    else:
        shown = "unset" if where is None else f"is {json.dumps(ref)} in {where}"
        # The project's file, which every collaborator's checkout reads; a file nearer the top of the list wins.
        lines.append(
            f'MISSING  worktree.baseRef "head" ({shown}), so a writer\'s worktree is cut from the default branch,'
            " not this one: set it in .claude/settings.json"
        )
    configured = settings.state_root_path(here.root)
    if any(os.path.normcase(part) == ".claude" for part in configured.parts):
        lines.append(
            f"FAILED   `stateRoot` is {configured}, under a .claude folder, where every agent's write prompts or is"
            " refused: set it outside one"
        )
    try:
        # The session's programme folder, which `init` grants; before `init` there is none to check.
        folder: Path | None = store.state_dir(held=here).parent
    except Refused:
        folder = None
    if folder is not None and granted(here.root, folder):
        lines.append(f"ok       {folder} is a working directory")
    elif folder is not None:
        lines.append(
            f"MISSING  {folder} as a working directory, so an agent's report write prompts:"
            f" add it to `permissions.{DIRECTORIES}` in {config.SETTINGS_LOCAL.as_posix()}"
        )
    # The pointer finds this session from anywhere, but the next `init` reads `stateRoot`: a root changed since,
    # or one a packaged app such as Claude's Windows app redirects, puts that session elsewhere.
    if folder is not None and not config.within(folder, configured):
        lines.append(
            f"DIFFERS  `stateRoot` resolves to {configured}, and this session's state is under {folder.parent}:"
            " set `stateRoot` to a folder that resolves alike in every process, outside AppData"
        )
    return lines


def _preflight(here: config.Checkout, check: config.Preflight) -> str | None:
    """The preflight command's line once run; None where its program is missing, which a line names already."""
    found = config.program(check.run, here.root)
    if found is None:
        return None
    shown = " ".join(check.run)
    try:
        done = process.run((found, *check.run[1:]), cwd=here.root, timeout=PREFLIGHT_SECONDS)
    except subprocess.TimeoutExpired:
        return f"FAILED   `{shown}` ran past {PREFLIGHT_SECONDS} s ({check.why})"
    except OSError as error:
        return f"FAILED   `{shown}` could not run: {error} ({check.why})"
    if done.returncode == 0:
        return f"ok       `{shown}`"
    said = (done.stderr or done.stdout).decode("utf-8", "replace").strip().splitlines()
    return f"FAILED   `{shown}` exited {done.returncode} ({check.why})" + (f": {said[-1]}" if said else "")


def doctor(args: argparse.Namespace) -> int:
    here = config.checkout()
    settings = config.load(here.root)
    lines = [
        f"ok       {config.CONFIG_FILE.as_posix()}"
        + ("" if (here.root / config.CONFIG_FILE).exists() else " (missing: every default)"),
        *_programs(here, settings),
        *diff.checked(here.root, settings),
        *_harness(here, settings),
        *filter(None, (_preflight(here, check) for check in settings.preflight)),
    ]
    failed = sum(not line.startswith("ok ") for line in lines)
    cli.emit(args, lines, f"doctor: {failed} failing" if failed else "doctor: every check holds")
    return REMAINS if failed else DONE
