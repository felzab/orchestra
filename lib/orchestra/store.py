"""The session's state on disk: where it lives, the lock on each file, and writes no crash can tear.

A session's state is the folder `<stateRoot>/<programme>/<session>/`. `init` writes its path into
`<git common dir>/orchestra/<branch>`, a file no commit carries and every worktree of the clone
shares, so a command finds the state from the directory it runs in. Each state file has a sidecar
`<file>.lock` that an operating-system lock is taken on: a writer killed while it holds one releases
it with its process, so no lock outlives its writer.

One Claude Code session writes a session's state: `lease.json` names it, from the first write that
carries a session id, and every write function below refuses a write from any other, so a second
coordinator opened on the same branch cannot interleave its decisions with the first's. A hook
recording what an agent did, on that agent's own event, writes inside `unleased()`: such a write is a
fact, not a decision, and the agent outlives a coordinator's `/clear`, stop or takeover.

A hook runs inside `budget()`, so every lock wait and retry ends before Claude Code would cancel it.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import errno
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, BinaryIO, TypeVar
from urllib.parse import quote, unquote

from orchestra import Refused
from orchestra.config import Checkout, checkout

# What a JSON file or a JSONL line holds once parsed.
Record = dict[str, Any]

POINTERS = "orchestra"
SESSION = "session.json"
AGENTS = "agents"
FINDINGS = "findings.jsonl"
QUEUE = "queue.jsonl"
# One for the programme, beside its sessions' folders: a ruling binds every later session.
RULINGS = "rulings.jsonl"
LOG = "log.jsonl"
CI = "ci.json"
PLAN = "PLAN.md"
NOW = "NOW.md"
LEASE = "lease.json"
AWAY = "away.json"
# The undone lines the coordinator's turn-end hook last listed, so that it lists a set once.
LISTED = "listed.json"
OVERRIDE = "override.json"
REPORTS = "reports"
SCRATCH = "scratch"
# Where the coordinator saves each estimate, which the checkpoint's timing and `status` read.
ESTIMATES = "estimates"
FOLDERS = (AGENTS, "briefs", REPORTS, ESTIMATES, SCRATCH)
# The lens rounds' captured diffs and detached worktrees, which `diff` writes and `prune` empties.
DIFFS = "diffs"
TREES = "trees"

# The variable every command the Bash tool runs carries, equal to a hook input's `session_id` and
# changed by `/clear` (Claude Code's env-vars.md). A subagent's commands carry its parent's.
SESSION_ID = "CLAUDE_CODE_SESSION_ID"
# A coordinator that wrote nothing for this long may have hit a usage limit or been closed, so another
# session may take its state over; one merely waiting on its agents is refused afterwards, loudly.
QUIET_SECONDS = 600.0

# A write holds a lock for milliseconds, so a wait this long means a writer that is stuck, not slow.
LOCK_SECONDS = 10.0
POLL_SECONDS = 0.02
# How much of a JSONL file's end one read takes while looking for its last line end.
TAIL_BYTES = 4096

T = TypeVar("T")

# When the running hook must have decided, on the monotonic clock; None outside a hook.
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("deadline", default=None)
# Whether the writes running now record an agent's own facts, which no lease refuses.
_UNLEASED: contextvars.ContextVar[bool] = contextvars.ContextVar("unleased", default=False)


@contextlib.contextmanager
def budget(seconds: float) -> Generator[None, None, None]:
    """Every lock wait and retry inside the block ends `seconds` from now at the latest."""
    token = _DEADLINE.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


@contextlib.contextmanager
def unleased() -> Generator[None, None, None]:
    """The writes inside the block skip the lease: a hook recording an agent's stop, branch or log line."""
    token = _UNLEASED.set(True)
    try:
        yield
    finally:
        _UNLEASED.reset(token)


def _until(seconds: float) -> float:
    """The monotonic time a wait of `seconds` ends, cut to the running hook's budget."""
    end = time.monotonic() + seconds
    held = _DEADLINE.get()
    return end if held is None else min(end, held)


def pointer(common_dir: Path, branch: str) -> Path:
    """The file naming a branch's state; the branch name quoted whole, so `a/b` and `a%2Fb` stay apart."""
    return common_dir / POINTERS / quote(branch, safe="")


def state_dir(cwd: Path | None = None, held: Checkout | None = None) -> Path:
    """The state of the session the primary checkout's branch holds, found from any worktree of the clone.

    An agent's worktree has a branch of its own, which no session is keyed by; the primary checkout's
    branch is the coordinator's wherever the command runs.
    """
    here = checkout(cwd) if held is None else held
    if here.branch is None:
        raise Refused(f"{here.root} has no branch checked out, and a session belongs to one: check out its branch")
    return pointed(pointer(here.common_dir, here.branch))


def pointed(path: Path) -> Path:
    """The state a branch's pointer names; refused where the pointer or its folder is missing."""
    branch = unquote(path.name)
    try:
        named = read_bytes(path).decode("utf-8").strip()
    except FileNotFoundError:
        raise Refused(f"no orchestra session on {branch}: run `orchestra init <session>` on its branch") from None
    state = Path(named)
    if not state.is_dir():
        raise Refused(
            f"branch {branch}'s session is {state}, which does not exist: restore that folder,"
            " or cut a new branch and run `orchestra init <session>`"
        )
    return state


def _patient(act: Callable[[], T], path: Path, deadline: float) -> T:
    """`act`, asked again while Windows refuses it a file another process holds open, until the deadline."""
    while True:
        try:
            return act()
        except PermissionError:
            # Windows refuses to replace a file another process has open, and to open one mid-replace.
            if sys.platform != "win32":
                raise
            if time.monotonic() >= deadline:
                raise Refused(
                    f"{path} stayed open in another process past the wait: close what holds it and run this again"
                ) from None
            time.sleep(POLL_SECONDS)


if sys.platform == "win32":
    import msvcrt

    def _take(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            # The one refusal meaning another handle holds the byte; any other is a fault to raise.
            if error.errno not in {errno.EACCES, errno.EDEADLK}:
                raise
            return False
        return True

    def _give(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _take(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _give(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def locked(path: Path) -> Generator[None, None, None]:
    """The file's lock held; a second taker waits, refused naming the file after `LOCK_SECONDS` or a hook's budget.

    The sidecar file stays after the lock is given back: deleting it would let a writer that opened
    it before the delete and one that opens it after both hold a lock.
    """
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        start = time.monotonic()
        deadline = _until(LOCK_SECONDS)
        while not _take(fd):
            if time.monotonic() >= deadline:
                raise Refused(
                    f"{path.name} is held by another orchestra command ({lock}) after {deadline - start:.1f} s:"
                    " run this again once it ends"
                )
            time.sleep(POLL_SECONDS)
        try:
            yield
        finally:
            _give(fd)
    finally:
        os.close(fd)


def read_bytes(path: Path) -> bytes:
    """A state file's bytes, read past a replace in flight."""
    return _patient(path.read_bytes, path, _until(LOCK_SECONDS))


def replace_bytes(path: Path, data: bytes) -> None:
    """The file's bytes swapped whole: a spare written beside it and renamed over it.

    It takes no lock: a state file's writer holds the file's own, and a file another program also
    writes, such as the harness's settings, gets no sidecar of ours beside it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, spare = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".writing")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            # Without it a crash can leave the renamed file empty on a file system that delays the data.
            os.fsync(out.fileno())
        _patient(lambda: Path(spare).replace(path), path, _until(LOCK_SECONDS))
    except BaseException:
        Path(spare).unlink(missing_ok=True)
        raise


def _encode(record: Record, indent: int | None = None) -> bytes:
    return json.dumps(record, ensure_ascii=False, indent=indent).encode("utf-8")


# --- the lease ------------------------------------------------------------------------------------


def caller() -> str | None:
    """The Claude Code session running this command; None in a terminal or a test, which no lease refuses."""
    return os.environ.get(SESSION_ID) or None


def lease(state: Path) -> Record:
    """The lease: `owner`, the session id holding the state, `since` when, and `stopped`; empty where none is held."""
    path = state / LEASE
    return read_json(path) if path.exists() else {}


def _write_lease(state: Path, owner: str | None, since: str, *, stopped: bool) -> None:
    # Written past the lease check, which only `check_lease`, `release_lease` and `take_lease` decide on.
    held = {"owner": owner, "since": since, "stopped": stopped}
    replace_bytes(state / LEASE, _encode(held, indent=2) + b"\n")


def now() -> str:
    """The local time to the second with its offset: the program reads the clock, never a typed time."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def clock_time(moment: datetime.datetime) -> str:
    """A moment as the local clock's hour and minute, rounded up, so a time named as the earliest is never early."""
    local = moment.astimezone()
    whole = local.replace(second=0, microsecond=0)
    return f"{whole + datetime.timedelta(minutes=1) if whole < local else whole:%H:%M}"


def refusal(held: Record, me: str, at: datetime.datetime | None = None) -> str | None:
    """Why a write from session `me` is refused under the lease `held`; None where it may write.

    `at` is when a takeover of a holder that has not stopped succeeds, as `takeover_at` answers.
    """
    owner = held.get("owner")
    if owner != me and held.get("stopped"):
        return (
            f"session {owner} stopped this session's state, and this command runs in session {me}:"
            ' continue it here with `orchestra mode takeover "<reason>"`'
        )
    if owner != me:
        when = "now" if at is None else f"at {clock_time(at)}, unless that session writes again before"
        return (
            f"session {owner or 'none'} holds this session's state, and this command runs in session {me}:"
            " work from that session, or once it has stopped or been quiet ten minutes,"
            f' run `orchestra mode takeover "<reason>"`; a takeover succeeds {when}'
        )
    if held.get("stopped"):
        return (
            "this session stopped its state with `orchestra mode stop`, so its writes are refused:"
            ' the session continuing the work runs `orchestra mode takeover "<reason>"`'
        )
    return None


def check_lease(state: Path) -> None:
    """Refused where another session holds the state, or this one stopped it; the first write with an id claims it."""
    me = caller()
    if me is None or _UNLEASED.get():
        return
    held = lease(state)
    if not held:
        with locked(state / LEASE):
            held = lease(state)
            if not held:
                _write_lease(state, me, now(), stopped=False)
                return
    # The takeover time walks the state, so it is read only for the refusal that words it, never on the owner's writes.
    at = takeover_at(state) if held.get("owner") != me and not held.get("stopped") else None
    if (why := refusal(held, me, at)) is not None:
        raise Refused(why)


def _guard(path: Path) -> None:
    """The lease of the session whose state holds `path`: a state file sits in the state or one folder below it."""
    if path.name == SESSION:
        check_lease(path.parent)
        return
    for folder in (path.parent, path.parent.parent):
        if (folder / SESSION).is_file():
            check_lease(folder)
            return


def release_lease(state: Path, session: str | None) -> bool:
    """The state marked stopped where `session` holds it, refusing its owner's writes; False where it does not.

    False too where it is stopped already. `orchestra mode stop` passes its own session, or the holder
    where it has no id; `SessionEnd` passes the session `/clear` replaced, whose id never writes again.
    None stops a state no session holds yet.
    """
    with locked(state / LEASE):
        held = lease(state)
        if held.get("owner") != session or held.get("stopped"):
            return False
        _write_lease(state, session, held.get("since") or now(), stopped=True)
        return True


def last_write(state: Path) -> float | None:
    """When the state was last written: the newest file but a report, a scratch file, a lens round's, the lease
    and a lock, which moves the takeover clock.

    An agent writes the first two while its coordinator may be gone, so they never count. The hooks'
    writes for an agent past the lease do count: its record, as its branch, stop or report is noted,
    `log.jsonl`, `refusals.jsonl` and `override.json`, so an agent still at work puts the takeover off.
    """
    times: list[float] = []
    # os.walk, so the lens rounds' worktrees are never entered, and a folder removed mid-walk is skipped.
    for folder, subfolders, files in os.walk(state):
        if Path(folder) == state:
            subfolders[:] = [name for name in subfolders if name not in {REPORTS, SCRATCH, DIFFS, TREES}]
        for name in files:
            if name == LEASE or name.endswith(".lock"):
                continue
            # A file removed between the listing and the read, such as by `prune`'s detached deleter, is absent.
            with contextlib.suppress(FileNotFoundError):
                times.append(Path(folder, name).stat().st_mtime)
    return max(times, default=None)


def takeover_at(state: Path) -> datetime.datetime | None:
    """When a takeover of a holder that has not stopped succeeds: `QUIET_SECONDS` after the state's last write,
    unless the holder writes again; None where it would succeed now."""
    last = last_write(state)
    if last is None or time.time() - last >= QUIET_SECONDS:
        return None
    return datetime.datetime.fromtimestamp(last + QUIET_SECONDS).astimezone()


def take_lease(state: Path) -> Record:
    """The state handed to the calling session after a stop or `QUIET_SECONDS` with no write; the lease before."""
    me = caller()
    if me is None:
        raise Refused(f"{SESSION_ID} is unset, so no session can take the state: run this from the Claude Code session")
    with locked(state / LEASE):
        held = lease(state)
        owner: str | None = held.get("owner")
        if owner == me and not held.get("stopped"):
            return held
        if held and owner != me and not held.get("stopped") and (at := takeover_at(state)) is not None:
            quiet = QUIET_SECONDS - (at - datetime.datetime.now().astimezone()).total_seconds()
            raise Refused(
                f"session {owner} holds this session's state, has not stopped,"
                f" and wrote it {quiet / 60:.0f} min ago: take it over once it runs `orchestra mode stop`"
                f" or {QUIET_SECONDS / 60:.0f} min pass with no write; a takeover succeeds at {clock_time(at)},"
                " unless that session writes again before"
            )
        _write_lease(state, me, now(), stopped=False)
        return held


# --- writes ---------------------------------------------------------------------------------------


def write_text(path: Path, text: str) -> None:
    """A whole file written under its lock, its line ends as given."""
    _guard(path)
    with locked(path):
        replace_bytes(path, text.encode("utf-8"))


def read_json(path: Path) -> Record:
    """A JSON state file's object; refused naming the file where it holds anything else."""
    try:
        held = json.loads(read_bytes(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Refused(
            f"{path} is no JSON ({error}): orchestra keeps no copy of it, so repair it by hand or remove it"
        ) from None
    if not isinstance(held, dict):
        raise Refused(f"{path} holds no JSON object: orchestra keeps no copy of it, so repair it by hand or remove it")
    return held  # pyright: ignore[reportUnknownVariableType]


def write_json(path: Path, record: Record) -> None:
    _guard(path)
    with locked(path):
        replace_bytes(path, _encode(record, indent=2) + b"\n")


def update_json(path: Path, edit: Callable[[Record | None], Record]) -> Record:
    """The file's object, None where there is none yet, replaced by what `edit` returns, under one lock."""
    _guard(path)
    with locked(path):
        held = read_json(path) if path.exists() else None
        record = edit(held)
        if record != held:
            replace_bytes(path, _encode(record, indent=2) + b"\n")
        return record


def remove(path: Path) -> None:
    """A state file deleted under its lock; nothing where it is gone already."""
    _guard(path)
    with locked(path):
        _patient(lambda: path.unlink(missing_ok=True), path, _until(LOCK_SECONDS))


def read_jsonl(path: Path) -> list[Record]:
    """Every line of a JSONL file, none where the file is missing.

    A last line with no line end is an append still being written, and is left for the next read.
    """
    return _records(path, whole_tail=False)


def _records(path: Path, *, whole_tail: bool) -> list[Record]:
    """Every line of a JSONL file, and with `whole_tail` a last line with no line end that is whole JSON.

    Under the file's lock no append is under way, so such a line is one `_append` keeps.
    """
    try:
        data = read_bytes(path)
    except FileNotFoundError:
        return []
    lines = data.split(b"\n")
    if not (whole_tail and _whole(lines[-1])):
        lines.pop()
    records: list[Record] = []
    for number, line in enumerate(lines, 1):
        try:
            held = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            held = None
        if not isinstance(held, dict):
            raise Refused(
                f"{path} line {number} is no JSON object: orchestra keeps no copy of this file,"
                " so repair the line by hand or remove it"
            )
        records.append(held)  # pyright: ignore[reportUnknownArgumentType]
    return records


def _torn(out: BinaryIO) -> int | None:
    """Where the file's last line end leaves off, where a line with no end follows it; None where none does.

    Every append ends on a line end under the lock, so such a tail, met under the lock, is a writer that died.
    """
    cut = out.seek(0, os.SEEK_END)
    if cut == 0:
        return None
    out.seek(cut - 1)
    if out.read(1) == b"\n":
        return None
    while cut > 0:
        start = max(0, cut - TAIL_BYTES)
        out.seek(start)
        found = out.read(cut - start).rfind(b"\n")
        if found >= 0:
            return start + found + 1
        cut = start
    return 0


def _whole(tail: bytes) -> bool:
    """Whether a last line with no line end is a JSON object whole, rather than an append cut short."""
    try:
        return isinstance(json.loads(tail.decode("utf-8")), dict)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False


def _append(path: Path, records: list[Record]) -> None:
    if records:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Opened to append and read: the writes land at the end whatever the read moved.
        with path.open("a+b") as out:
            head = b""
            if (cut := _torn(out)) is not None:
                out.seek(cut)
                if _whole(out.read()):
                    # A line repaired by hand in an editor that saves no final line end: it is kept.
                    head = b"\n"
                else:
                    # Welded onto a torn line, every later line would be unreadable with it.
                    out.truncate(cut)
            # One write call for every line, so a reader never meets half of a batch.
            out.write(head + b"".join(_encode(record) + b"\n" for record in records))
            out.flush()
            # As in `replace_bytes`: a later replace must never reach the disk ahead of this append.
            os.fsync(out.fileno())


def append_jsonl(path: Path, *records: Record) -> None:
    _guard(path)
    with locked(path):
        _append(path, list(records))


def edit_jsonl(path: Path, edit: Callable[[list[Record]], list[Record]]) -> list[Record]:
    """The records `edit` makes from every line the file holds, appended under one lock; those records.

    A writer that decides from what is already written, such as the next number, holds the lock across
    the read and the append, so two such writers never decide from the same lines.
    """
    _guard(path)
    with locked(path):
        records = edit(_records(path, whole_tail=True))
        _append(path, records)
        return records
