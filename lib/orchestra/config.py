"""The project's configuration: `.claude/orchestra.json` in the primary checkout, every key optional.

The file is read from the primary checkout even when a command runs inside an agent's worktree, so
every agent reads the coordinator's file as it is on disk, never a worktree's older copy. The
dataclasses below are the whole of what the file may hold; `schema/orchestra.schema.json` describes
the same keys for an editor, and a test holds the two equal.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import types
import typing
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple, TypeVar

from orchestra import Refused, process

CONFIG_FILE = Path(".claude", "orchestra.json")

# A JSON key is its field's name in camel case, unless the field names its key.
KEY = "key"
# A list that must hold an item, such as a command, which names its program first.
NONEMPTY = "nonempty"
# A number that must be more than 0, such as a bound on a wait.
POSITIVE = "positive"
# A list of paths compared as written, which a `*` would never match.
LITERAL = "literal"
# The longest value a refusal quotes; a longer one is named by its JSON kind.
SHOWN = 40
# The checkout's own settings file, which Claude Code keeps out of commits.
SETTINGS_LOCAL = Path(".claude", "settings.local.json")
# The auto-compact window's settings key, and the variable that outranks every settings key (model-config.md).
WINDOW = "autoCompactWindow"
WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"

StepKind = Literal["install", "regenerate", "check"]
# An Agent call's `model` takes an alias alone, never a full model id (sub-agents.md, "Choose a model").
Alias = Literal["sonnet", "opus", "haiku", "fable"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class Model:
    """The model and effort the dispatch hook gives an agent whose brief names this profile."""

    model: Alias
    effort: Effort | None = None


@dataclass(frozen=True)
class Preflight:
    """A command `doctor` runs, which must exit 0 before a session can work."""

    run: tuple[str, ...] = field(metadata={NONEMPTY: True})
    why: str


@dataclass(frozen=True)
class Step:
    """A command `land` runs after a merge touching a path under one of `when`, from `cwd`."""

    kind: StepKind
    when: tuple[str, ...] = field(metadata={LITERAL: True})
    run: tuple[str, ...] = field(metadata={NONEMPTY: True})
    cwd: str = "."
    writes: tuple[str, ...] = field(default=(), metadata={LITERAL: True})


@dataclass(frozen=True)
class Land:
    steps: tuple[Step, ...] = ()


@dataclass(frozen=True)
class Trigger:
    """The paths that make a lens due in this project, beside any its file names.

    An empty `when` makes a `declared` lens due on nothing; a lens whose file
    says `always` or names paths stays due as the file says.
    """

    name: str
    when: tuple[str, ...]


@dataclass(frozen=True)
class Lenses:
    triggers: tuple[Trigger, ...] = ()


def _pattern(where: str, pattern: str, group: str | None = None) -> None:
    """A pattern the program cannot use, refused at the session's start rather than after a wait on a run."""
    try:
        compiled = re.compile(pattern)
    except re.error as error:
        raise Refused(f"{CONFIG_FILE.as_posix()}: `{where}` is no regular expression: {error}") from None
    if group is not None and group not in compiled.groupindex:
        raise Refused(f"{CONFIG_FILE.as_posix()}: `{where}` needs a group named `{group}`: (?P<{group}>...)")


@dataclass(frozen=True)
class Ci:
    """The workflow whose failed log is read line by line, the bound on a wait, and the project's own log lines."""

    # The workflow's `name:` as `gh run list` shows it, which a run carries; its file name appears on none.
    workflow: str | None = None
    # A queued run can wait hours for a runner, so a wait on a commit's runs ends at a bound of its own.
    wait_minutes: float = field(default=30.0, metadata={POSITIVE: True})
    verdict: str | None = None
    relay: str | None = None
    findings: tuple[str, ...] = ()
    cases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        named = {"verdict": "text", "relay": None, "findings": "text", "cases": "name"}
        for name, group in named.items():
            value: str | tuple[str, ...] | None = getattr(self, name)
            patterns = (value,) if isinstance(value, str) else value or ()
            for k, pattern in enumerate(patterns):
                _pattern(f"ci.{name}" + (f"[{k}]" if name in {"findings", "cases"} else ""), pattern, group)


@dataclass(frozen=True)
class Gate:
    """The project's whole check, which `ci gate` runs in the primary checkout."""

    run: tuple[str, ...] = field(metadata={NONEMPTY: True})


@dataclass(frozen=True)
class Config:
    schema: str | None = field(default=None, metadata={KEY: "$schema"})
    # None is the platform's own folder for a user's application data, `default_state_root`.
    state_root: str | None = None
    base: str = "main"
    models: dict[str, Model] = field(default_factory=dict[str, Model])
    # The context, in tokens, past which a resume is refused: a resume sends the whole context again.
    # 200,000 is the window a session compacts at where a model holds no 1M context (model-config.md),
    # so an agent past it carries more than a standard window would have kept.
    context_ceiling: int = field(default=200_000, metadata={POSITIVE: True})
    queues: tuple[str, ...] = ()
    preflight: tuple[Preflight, ...] = ()
    land: Land = Land()
    lenses: Lenses = Lenses()
    ci: Ci = Ci()
    gate: Gate | None = None
    # The Claude Code program `status --measure` reads the usage with where `claude` is not on PATH.
    claude: str | None = None

    def state_root_path(self, root: Path) -> Path:
        """The folder every programme's state lives under: `~` expanded, a relative path read from the root."""
        if self.state_root is None:
            return default_state_root()
        # Resolved, so no `..` reaches the paths written into the settings, a brief or a prompt.
        return (root / Path(self.state_root).expanduser()).resolve()

    def claude_path(self, root: Path) -> Path | None:
        """The configured Claude Code program, read as `stateRoot` is; None where none is configured."""
        return None if self.claude is None else (root / Path(self.claude).expanduser()).resolve()


def default_state_root() -> Path:
    """The platform's folder for a user's application data, `orchestra` under it.

    Never under a `.claude` folder, whose every file an agent's write prompts for or is refused, and
    never in the plugin's own data folder, which its uninstall deletes.
    """
    if sys.platform == "win32":
        held = os.environ.get("LOCALAPPDATA", "")
        base = Path(held) if Path(held).is_absolute() else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        # The specification has a relative value ignored.
        held = os.environ.get("XDG_DATA_HOME", "")
        base = Path(held) if held and Path(held).is_absolute() else Path.home() / ".local" / "share"
    return base / "orchestra"


def claude_dir() -> Path:
    """Claude Code's folder of the user's own: `CLAUDE_CONFIG_DIR` where it is set, else `~/.claude`."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def settings_files(root: Path) -> list[Path]:
    """The settings files a session in this checkout reads, the one that wins a key first (settings.md).

    Managed settings and `--settings`, which settings.md ranks above the local file, are not read: the
    first sits in a system folder of each platform's own, and the second is known to the session alone.
    """
    return [root / SETTINGS_LOCAL, root / ".claude" / "settings.json", claude_dir() / "settings.json"]


def setting(path: Path, *keys: str) -> object:
    """The value under `keys` in a settings file, the whole file for no key; None where none is or it cannot be read."""
    try:
        held: object = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return setting_in(held, *keys)


def setting_in(held: object, *keys: str) -> object:
    """The value under `keys` in parsed settings; None where they hold none."""
    for key in keys:
        held = typing.cast("dict[str, object]", held).get(key) if isinstance(held, dict) else None
    return held


def window(root: Path, model: str | None) -> int | None:
    """The auto-compact window a session of `model` in this checkout is set to; None where it runs at the model's own.

    `WINDOW_ENV` outranks every settings key; else the first settings file that sets a window for the model
    or for every model decides, its window for the model first (settings-reference.md, "modelSettings"),
    where Claude Code writes each entry under the model's canonical name, the name a transcript records.
    None where nothing sets one, as `init` says such a session runs at Claude Code's default window, a
    figure no file holds; `"auto"`, the model's tuned window, is None too. A window given by `--autocompact`
    for one launch is the session's alone, and not read.
    """
    if held := _tokens(os.environ.get(WINDOW_ENV, "")):
        return held
    for path in settings_files(root):
        # Parsed once for both keys.
        parsed = setting(path)
        found = setting_in(parsed, "modelSettings", model, WINDOW) if model else None
        found = found if found is not None else setting_in(parsed, WINDOW)
        if found is not None:
            return found if isinstance(found, int) and not isinstance(found, bool) else None
    return None


def _tokens(value: str) -> int | None:
    """A token count as `WINDOW_ENV` takes one, `500000`, `500k` or `1M` (model-config.md); None for any other."""
    held = re.fullmatch(r"\s*([0-9]+)\s*([kKmM]?)\s*", value)
    if held is None:
        return None
    return int(held[1]) * {"": 1, "k": 1_000, "m": 1_000_000}[held[2].lower()]


class Checkout(NamedTuple):
    """Where a command runs: the primary checkout, its branch and the clone's shared git folder."""

    root: Path
    common_dir: Path
    # The primary checkout's branch, which keys the session in every worktree; None where detached.
    branch: str | None
    # Whether the command runs in the primary checkout rather than in a linked worktree.
    primary: bool


# The running command's checkout of its working directory, found at the first `checkout()` that asks; None
# outside `once`, as in a hook, which finds each folder's checkout afresh.
_ONCE: contextvars.ContextVar[Callable[[], Checkout] | None] = contextvars.ContextVar("checkout", default=None)


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """One git command run, whatever its exit; refused where git is not on PATH."""
    try:
        return process.run(("git", "-C", str(cwd), *args))
    except FileNotFoundError:
        raise Refused("git is not on PATH: install git, or put it on PATH") from None


def git(cwd: Path, *args: str) -> str:
    """One git command's output, refused with git's own words where it fails."""
    done = run_git(cwd, *args)
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip()
        raise Refused(f"git {' '.join(args)} failed in {cwd}: {said}")
    return done.stdout.decode("utf-8", "replace")


def _same(one: str | Path, other: str | Path) -> bool:
    return os.path.normcase(Path(one).resolve()) == os.path.normcase(Path(other).resolve())


def program(command: tuple[str, ...], cwd: Path) -> str | None:
    """The program a command runs, as a path: a name with a separator read from `cwd`, else found on PATH."""
    name = command[0]
    if "/" in name or "\\" in name:
        path = (cwd / name).resolve()
        return str(path) if path.is_file() else None
    return shutil.which(name)


def within(path: Path, folder: Path) -> bool:
    """Whether the path is the folder or lies under it, the folder known by its identity rather than its spelling.

    Windows' long-path form `\\\\?\\C:\\...`, an admin share and a link each spell one folder another
    way, and `resolve` keeps the first two as written. Both paths are resolved, each existing folder
    is compared by its volume and file number, and the parts below the folder's nearest existing one,
    which no file holds yet, in the file system's case.
    """
    return under(path, folder) is not None


def identical(path: Path, other: Path) -> bool:
    """Whether two paths name one file or folder, as `within` judges a folder: by identity, not spelling."""
    return under(path, other) == []


def under(path: Path, folder: Path) -> list[str] | None:
    """The path's parts below the folder, in the file system's case, as `within` judges it; None outside it.

    The folder itself has none, so a pattern written below a folder can be matched against what is
    left once the folder is found by its identity.
    """
    outer, below = _anchor(folder.resolve())
    inner = path.resolve()
    for depth, held in enumerate((inner, *inner.parents)):
        if _identity(held) == outer:
            tail = _tail(inner, depth)
            return tail[len(below) :] if tail[: len(below)] == below else None
    return None


def _identity(path: Path) -> object | None:
    """A folder's volume and file number, its spelling where it has none; None where it does not exist, but a root."""
    try:
        held = path.stat()
    except OSError:
        return os.path.normcase(path) if path.parent == path else None
    return (held.st_dev, held.st_ino) if held.st_ino else os.path.normcase(path)


def _tail(path: Path, depth: int) -> list[str]:
    """The path's last `depth` parts, in the file system's case."""
    return [os.path.normcase(part) for part in path.parts[len(path.parts) - depth :]]


def _anchor(path: Path) -> tuple[object, list[str]]:
    """The identity of the path's nearest existing folder, and the parts below it."""
    depth, held = 0, path
    # A root always has one, so the walk ends.
    while (found := _identity(held)) is None:
        depth, held = depth + 1, held.parent
    return found, _tail(path, depth)


def worktrees(cwd: Path) -> list[dict[str, str]]:
    """Each worktree git lists, the primary checkout first, as its labels and values; `branch` without `refs/heads/`.

    git lists the primary checkout first whatever the layout: a submodule's or a separate git folder's
    common directory has no checkout as its parent.
    """
    found: list[dict[str, str]] = []
    for block in git(cwd, "worktree", "list", "--porcelain").split("\n\n"):
        if block.strip():
            fields = {label: value for label, _, value in (line.partition(" ") for line in block.splitlines())}
            if "branch" in fields:
                fields["branch"] = fields["branch"].removeprefix("refs/heads/")
            found.append(fields)
    return found


@contextlib.contextmanager
def once() -> Generator[None, None, None]:
    """Inside the block, `checkout()` with no `cwd` asks git the first time alone: a command's whole run.

    Nothing a command runs changes its working directory or the primary checkout's branch, so the first
    answer holds to its end, and each verb's own call costs no second pair of git runs. A refusal is not
    kept, so a later call asks again.
    """
    token = _ONCE.set(functools.cache(lambda: _checkout(Path.cwd())))
    try:
        yield
    finally:
        _ONCE.reset(token)


def checkout(cwd: Path | None = None) -> Checkout:
    """The checkout the directory belongs to, from git; refused outside a repository or in a bare one."""
    if cwd is None and (held := _ONCE.get()) is not None:
        return held()
    return _checkout(Path.cwd() if cwd is None else cwd)


def _checkout(here: Path) -> Checkout:
    found = run_git(here, "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel")
    if found.returncode != 0:
        said = found.stderr.decode("utf-8", "replace").strip()
        raise Refused(f"{here} is in no git checkout ({said}): run orchestra from the project's checkout")
    common, top = found.stdout.decode("utf-8", "replace").splitlines()
    primary = worktrees(here)[0]
    if "bare" in primary:
        raise Refused(f"{common} is a bare repository, which has no primary checkout: run orchestra in a checkout")
    root = primary["worktree"]
    return Checkout(Path(root), Path(common), primary.get("branch"), _same(root, top))


def primary_checkout(verb: str) -> Checkout:
    """The checkout, refused outside the primary one, for a verb that changes the session's record (`cli.READS`)."""
    here = checkout()
    if not here.primary:
        raise Refused(
            f"`orchestra {verb}` changes the session's record, which the coordinator does from the primary"
            f" checkout, {here.root}: run it there"
        )
    return here


T = TypeVar("T")


def json_key(spec: dataclasses.Field[Any]) -> str:
    if KEY in spec.metadata:
        return str(spec.metadata[KEY])
    head, *rest = spec.name.split("_")
    return head + "".join(word.title() for word in rest)


def _kind(hint: object) -> str:
    """The JSON kind a type expects, as a refusal names it."""
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        return " or ".join(_kind(arg) for arg in typing.get_args(hint))
    if origin is tuple:
        return "a list"
    if origin is dict or dataclasses.is_dataclass(hint):
        return "an object"
    if origin is Literal:
        return "one of " + ", ".join(json.dumps(arg) for arg in typing.get_args(hint))
    return {str: "a string", int: "a whole number", float: "a number", type(None): "null"}.get(
        typing.cast("type", hint), str(hint)
    )


def _value(hint: object, value: object, where: str, named: object = None) -> object:
    """The value read as the type expects, refused naming its path where it is another.

    `named` is the union the type was taken from, whose kind the refusal names, `null` included.
    """
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        # Every union of the file's keys is one type or null.
        (held,) = (arg for arg in typing.get_args(hint) if arg is not type(None))
        return None if value is None else _value(held, value, where, hint)
    if origin is tuple and isinstance(value, list):
        (item, _) = typing.get_args(hint)
        return tuple(_value(item, held, f"{where}[{k}]") for k, held in enumerate(typing.cast("list[object]", value)))
    if origin is dict and isinstance(value, dict):
        (_, item) = typing.get_args(hint)
        return {
            name: _value(item, held, f"{where}.{name}")
            for name, held in typing.cast("dict[str, object]", value).items()
        }
    if origin is Literal and value in typing.get_args(hint):
        return value
    if dataclasses.is_dataclass(hint) and isinstance(value, dict):
        return _build(typing.cast("type[object]", hint), typing.cast("dict[str, object]", value), where)
    if hint is str and isinstance(value, str):
        return value
    if hint is float and _number(value):
        return float(typing.cast("float", value))
    if hint is int and _whole(value):
        return value
    shown = json.dumps(value) if len(json.dumps(value)) <= SHOWN else type(value).__name__
    raise Refused(f"{CONFIG_FILE.as_posix()}: `{where}` must be {_kind(named or hint)}, not {shown}")


def _number(value: object) -> bool:
    # JSON's `true` is no number, though Python's bool is an int.
    return isinstance(value, int | float) and not isinstance(value, bool)


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _build(cls: type[T], held: dict[str, object], where: str = "") -> T:
    """The dataclass made from one object of the file, every key it does not know refused by its path."""
    specs = {json_key(spec): spec for spec in dataclasses.fields(typing.cast("Any", cls))}
    hints = typing.get_type_hints(cls)
    path = f"{where}." if where else ""
    if unknown := sorted(set(held) - set(specs)):
        known = ", ".join(sorted(specs))
        raise Refused(
            f"{CONFIG_FILE.as_posix()}: `{path}{unknown[0]}` is no key orchestra reads; the keys here are {known}"
        )
    values: dict[str, object] = {}
    for key, spec in specs.items():
        if key in held:
            values[spec.name] = _value(hints[spec.name], held[key], f"{path}{key}")
            if spec.metadata.get(NONEMPTY) and not values[spec.name]:
                raise Refused(f"{CONFIG_FILE.as_posix()}: `{path}{key}` needs one item at least")
            if spec.metadata.get(POSITIVE) and not typing.cast("float", values[spec.name]) > 0:
                raise Refused(f"{CONFIG_FILE.as_posix()}: `{path}{key}` must be more than 0")
            if spec.metadata.get(LITERAL):
                for k, held_path in enumerate(typing.cast("tuple[str, ...]", values[spec.name])):
                    if "*" in held_path:
                        raise Refused(
                            f"{CONFIG_FILE.as_posix()}: `{path}{key}[{k}]` is compared as written, so it takes no `*`:"
                            " name the file, or the folder ending in `/`"
                        )
        elif spec.default is dataclasses.MISSING and spec.default_factory is dataclasses.MISSING:
            raise Refused(f"{CONFIG_FILE.as_posix()}: `{where}` needs the key `{key}`")
    return cls(**values)


def load(root: Path) -> Config:
    """The configuration in the primary checkout; every default where the file is missing."""
    path = root / CONFIG_FILE
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return Config()
    except (OSError, UnicodeDecodeError) as error:
        raise Refused(f"{path} cannot be read: {error}") from None
    try:
        held = json.loads(text)
    except json.JSONDecodeError as error:
        raise Refused(f"{path} is no JSON: {error.msg} at line {error.lineno}, column {error.colno}") from None
    if not isinstance(held, dict):
        raise Refused(f"{path} must hold one JSON object")
    return _build(Config, typing.cast("dict[str, object]", held))
