"""The store: no append lost to a concurrent one, no lock outliving its holder, no replace lost to a reader."""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import LIB, MakeRepo, git
from orchestra import Refused, store

# Each writer appends its lines one call at a time, so twenty writers interleave throughout.
APPENDER = (
    "import sys; from pathlib import Path; from orchestra import store\n"
    "for k in range(25): store.append_jsonl(Path(sys.argv[1]), {'writer': int(sys.argv[2]), 'k': k})\n"
)
# A writer that takes the lock, says so, and holds it until it is killed.
HOLDER = (
    "import sys, time; from pathlib import Path; from orchestra import store\n"
    "with store.locked(Path(sys.argv[1])):\n    print('held', flush=True)\n    time.sleep(600)\n"
)


def _python(code: str, *args: str, stdout: int | None = None) -> subprocess.Popen[bytes]:
    return subprocess.Popen((sys.executable, "-c", code, *args), cwd=LIB, stdout=stdout)


def test_twenty_writers_appending_at_once_lose_no_line(tmp_path: Path) -> None:
    path = tmp_path / "findings.jsonl"
    writers = [_python(APPENDER, str(path), str(n)) for n in range(20)]
    assert [writer.wait(60) for writer in writers] == [0] * 20
    held = store.read_jsonl(path)
    assert sorted((record["writer"], record["k"]) for record in held) == [(n, k) for n in range(20) for k in range(25)]


def test_a_holder_killed_mid_hold_releases_its_lock(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    holder = _python(HOLDER, str(path), stdout=subprocess.PIPE)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == b"held"
        with (
            pytest.raises(Refused, match=r"queue\.jsonl is held by another orchestra command"),
            store.budget(0.3),
            store.locked(path),
        ):
            pass
    finally:
        # Killed by its handle, so its `finally` never runs: only the operating system can give the lock back.
        holder.kill()
        holder.wait(30)
    store.append_jsonl(path, {"after": True})
    assert store.read_jsonl(path) == [{"after": True}]


def test_an_append_after_a_torn_line_cuts_it_off_first_and_keeps_a_whole_one(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    store.append_jsonl(path, {"k": 1})
    # A writer killed mid-write, its line longer than one read of the tail.
    path.write_bytes(path.read_bytes() + b'{"k": "' + b"x" * (2 * store.TAIL_BYTES))
    store.append_jsonl(path, {"k": 2})
    assert store.read_jsonl(path) == [{"k": 1}, {"k": 2}]
    # A line repaired by hand, as a refusal asks, in an editor that saves no final line end.
    path.write_bytes(path.read_bytes() + b'{"k": 3}')
    store.append_jsonl(path, {"k": 4})
    assert store.read_jsonl(path) == [{"k": 1}, {"k": 2}, {"k": 3}, {"k": 4}]

    def following(held: list[dict[str, object]]) -> list[dict[str, object]]:
        return [{"k": len(held) + 1}]

    # An edit deciding from the lines, such as the next number, decides from the repaired one too, and a
    # torn one it cuts off is no line.
    path.write_bytes(path.read_bytes() + b'{"k": 5}')
    assert store.edit_jsonl(path, following) == [{"k": 6}]
    path.write_bytes(path.read_bytes() + b'{"k": ')
    assert store.edit_jsonl(path, following) == [{"k": 7}]
    assert store.read_jsonl(path) == [{"k": n} for n in range(1, 8)]


@pytest.mark.skipif(sys.platform != "win32", reason="POSIX renames over an open file, so nothing is retried")
def test_a_replace_while_a_reader_holds_the_file_lands_once_it_closes(tmp_path: Path) -> None:
    path = tmp_path / "ci.json"
    store.write_json(path, {"run": 1})
    errors: list[BaseException] = []

    def write() -> None:
        try:
            store.write_json(path, {"run": 2})
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    with path.open("rb"):
        writer = threading.Thread(target=write)
        writer.start()
        time.sleep(0.3)
    writer.join(store.LOCK_SECONDS)
    assert errors == []
    assert not writer.is_alive()
    assert store.read_json(path) == {"run": 2}


def test_the_pointer_resolves_from_a_second_worktree(tmp_path: Path, make_repo: MakeRepo) -> None:
    root = make_repo(tmp_path / "clone")
    state = tmp_path / "state" / "p" / "s"
    state.mkdir(parents=True)
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    store.write_text(store.pointer(common, "main"), f"{state}\n")
    other = tmp_path / "other"
    git(root, "worktree", "add", "-b", "agent-branch", str(other))
    assert store.state_dir(other) == state
    assert store.state_dir(root) == state


# --- the lease ------------------------------------------------------------------------------------

OWNER = "session-owner"
OTHER = "session-other"


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A session's state whose first write carried the owner's session id."""
    path = tmp_path / "programme" / "s1"
    (path / store.AGENTS).mkdir(parents=True)
    monkeypatch.setenv(store.SESSION_ID, OWNER)
    store.write_json(path / store.SESSION, {"session": "s1"})
    return path


WRITES: dict[str, Callable[[Path], object]] = {
    "write_text": lambda state: store.write_text(state / store.NOW, "# Now\n"),
    "write_json": lambda state: store.write_json(state / store.AGENTS / "A.json", {"name": "A"}),
    "update_json": lambda state: store.update_json(state / store.CI, lambda _: {"run": 1}),
    "append_jsonl": lambda state: store.append_jsonl(state / store.LOG, {"text": "x"}),
    "edit_jsonl": lambda state: store.edit_jsonl(state / store.QUEUE, lambda _: [{"number": 1}]),
    "remove": lambda state: store.remove(state / store.SESSION),
}


@pytest.mark.parametrize("write", WRITES.values(), ids=list(WRITES))
def test_another_sessions_write_is_refused_naming_the_owner(
    state: Path, monkeypatch: pytest.MonkeyPatch, write: Callable[[Path], object]
) -> None:
    assert store.lease(state)["owner"] == OWNER
    held = {path: path.read_bytes() for path in state.rglob("*") if path.is_file()}
    monkeypatch.setenv(store.SESSION_ID, OTHER)
    with pytest.raises(Refused, match=f"session {OWNER} holds this session's state"):
        write(state)
    assert {path: path.read_bytes() for path in state.rglob("*") if path.is_file()} == held
    # A terminal or a test carries no session id, and is never refused.
    monkeypatch.setenv(store.SESSION_ID, "")
    write(state)


def test_a_takeover_waits_ten_minutes_with_no_write_but_an_agents_report_or_scratch(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.append_jsonl(state / store.LOG, {"text": "still working"})
    monkeypatch.setenv(store.SESSION_ID, OTHER)
    with pytest.raises(Refused, match="has not stopped"):
        store.take_lease(state)
    quiet = time.time() - store.QUIET_SECONDS - 60
    for path in state.rglob("*"):
        if path.is_file():
            os.utime(path, (quiet, quiet))
    # An agent reports or writes its scratch, or a lens round's tree changes, while its coordinator is gone:
    # no sign it is back.
    (state / store.REPORTS).mkdir()
    (state / store.REPORTS / "A.md").write_bytes(b"# A\n")
    (state / store.SCRATCH / "A").mkdir(parents=True)
    (state / store.SCRATCH / "A" / "notes.md").write_bytes(b"x\n")
    (state / store.TREES / "abc").mkdir(parents=True)
    (state / store.TREES / "abc" / "a.py").write_bytes(b"x\n")
    assert store.take_lease(state)["owner"] == OWNER
    assert store.lease(state)["owner"] == OTHER


def test_a_refused_takeover_and_write_name_the_clock_time_a_takeover_succeeds(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a crash the holder never stops, so the newcomer is told when its takeover succeeds."""
    store.append_jsonl(state / store.LOG, {"text": "still working"})
    written = time.time() - 4 * 60
    for path in state.rglob("*"):
        if path.is_file():
            os.utime(path, (written, written))
    at = store.clock_time(datetime.datetime.fromtimestamp(written + store.QUIET_SECONDS).astimezone())
    monkeypatch.setenv(store.SESSION_ID, OTHER)
    with pytest.raises(Refused) as taken:
        store.take_lease(state)
    assert str(taken.value).endswith(
        "wrote it 4 min ago: take it over once it runs `orchestra mode stop` or 10 min pass with no write;"
        f" a takeover succeeds at {at}, unless that session writes again before"
    )
    with pytest.raises(Refused) as wrote:
        store.append_jsonl(state / store.LOG, {"text": "the newcomer"})
    assert str(wrote.value).endswith(f"; a takeover succeeds at {at}, unless that session writes again before")
