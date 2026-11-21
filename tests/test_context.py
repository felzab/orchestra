"""An agent's context size, and whether it works, from a transcript laid out as the harness writes one."""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

from conftest import inprocess, line, transcript, write_agent
from orchestra import context, model, store

if sys.platform == "win32":
    import msvcrt


@pytest.fixture
def config(project: Path) -> Path:
    """The harness's folder, which the command line's environment points `CLAUDE_CONFIG_DIR` at."""
    return project.parent / "claude-config"


def test_the_context_is_the_last_calls_input_and_each_call_counts_once(
    project: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert inprocess("init", "s1", cwd=project).returncode == 0
    state = store.state_dir(project)
    transcript(config, "old1", line("assistant", "m0", 999_999))
    path = transcript(
        config,
        "new2",
        line("user"),
        # One call reaches the transcript as a line per content block, each carrying its usage.
        *[line("assistant", "m1", 1_000)] * 2,
        *[line("assistant", "m2", 276_773)] * 2,
        line("assistant", "m3", 5, isApiErrorMessage=True),
        line("assistant", "m4", 7, model="<synthetic>"),
        b'{"message": {"role": "assist',
        session="later",
    )
    write_agent(state, model.Agent("A", "writer", "running", ids=("old1", "new2")))
    write_agent(state, model.Agent("B", "writer", "briefed"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    agent = model.read_agent(state, "A")
    assert context.transcript(agent) == path
    assert context.size(agent) == 2 + 472 + 276_773
    assert context.size(model.read_agent(state, "B")) is None


def test_a_mid_turn_agent_is_working_until_the_clock_time_it_names(
    project: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed session's agent reads as working, so the reason names when that stops, not only its age."""
    assert inprocess("init", "s1", cwd=project).returncode == 0
    state = store.state_dir(project)
    path = transcript(config, "a1", line("assistant", "m1", 9, calls=True), minutes=5.5)
    write_agent(state, model.Agent("A", "writer", "running", ids=("a1",)))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    written = datetime.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    at = store.clock_time(written + datetime.timedelta(minutes=context.SILENT_MINUTES + 1))
    assert context.working(model.read_agent(state, "A")) == (
        f"its transcript is mid-turn, written 5 min ago; it counts as ended once silent past 15 min, at {at}"
    )
    # The time named is when `ended` first reads it silent.
    later = written + datetime.timedelta(minutes=context.SILENT_MINUTES + 1)
    assert context.ended(model.read_agent(state, "A"), later - datetime.timedelta(seconds=1))[1] is None
    assert context.ended(model.read_agent(state, "A"), later)[1] is not None


def test_a_running_records_ended_turn_counts_only_once_silent_and_a_stopped_records_at_once(
    project: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop hook runs after the turn's last line and can refuse the stop, so a `running` agent may write on."""
    assert inprocess("init", "s1", cwd=project).returncode == 0
    state = store.state_dir(project)
    path = transcript(config, "a1", line("assistant", "m1", 9), minutes=2.5)
    write_agent(state, model.Agent("A", "writer", "running", ids=("a1",)))
    write_agent(state, model.Agent("B", "writer", "reported", ids=("a1",)))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    running, reported = model.read_agent(state, "A"), model.read_agent(state, "B")
    written = datetime.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    at = store.clock_time(written + datetime.timedelta(minutes=context.SILENT_MINUTES + 1))
    assert context.working(running) == (
        "its record is running, and its transcript's turn ended 2 min ago, which its stop hook may still refuse;"
        f" it counts as ended once silent past 15 min, at {at}"
    )
    assert context.ended(running, datetime.datetime.now().astimezone())[1] is None
    later = written + datetime.timedelta(minutes=context.SILENT_MINUTES + 1)
    assert context.ended(running, later)[1] == "its transcript's last message ends its turn"
    assert context.working(reported) is None
    assert (
        context.ended(reported, datetime.datetime.now().astimezone())[1]
        == "its transcript's last message ends its turn"
    )


def test_a_turn_the_last_64_kib_settle_reads_nothing_before_them(
    project: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn's end reads every agent's transcript, so the first read is the small tail, which settles most."""
    if sys.platform != "win32":
        pytest.skip("only Windows refuses a read of a byte range another handle locked")
    assert inprocess("init", "s1", cwd=project).returncode == 0
    state = store.state_dir(project)
    filler = b'{"type": "progress"}\n' * (2 * context.TAIL_BYTES // 21)
    path = transcript(config, "a1", filler, line("user"), line("assistant", "m1", 9))
    write_agent(state, model.Agent("A", "writer", "reported", ids=("a1",)))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    with path.open("rb+") as held:
        # Everything before the last 64 KiB is locked, so a read reaching into it fails.
        msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, path.stat().st_size - context.FIRST_BYTES)
        try:
            ended = context.ended(model.read_agent(state, "A"), datetime.datetime.now().astimezone())
            assert ended[1] == "its transcript's last message ends its turn"
        finally:
            held.seek(0)
            msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, path.stat().st_size - context.FIRST_BYTES)


def test_the_last_call_is_read_from_the_tail_alone(tmp_path: Path) -> None:
    """Each of the coordinator's prompts reads its transcript, which reaches hundreds of megabytes."""
    if sys.platform != "win32":
        pytest.skip("only Windows refuses a read of a byte range another handle locked")
    filler = b'{"type": "progress"}\n' * (2 * context.TAIL_BYTES // 21)
    path = tmp_path / "main.jsonl"
    path.write_bytes(line("assistant", "m1", 9) + filler + line("user") + line("assistant", "m2", 900_000))
    with path.open("rb+") as held:
        # Everything before the last MiB is locked, so a read reaching into it fails.
        msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, path.stat().st_size - context.TAIL_BYTES)
        try:
            last = context.last_call(path)
        finally:
            held.seek(0)
            msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, path.stat().st_size - context.TAIL_BYTES)
    assert last is not None
    assert context.tokens(context.as_object(last.get("usage"))) == 2 + 472 + 900_000
    # A tail holding no call, as after a long tool result, reads as none rather than reading the whole file.
    path.write_bytes(line("assistant", "m1", 9) + filler + line("user"))
    assert context.last_call(path) is None
    path.write_bytes(line("user"))
    assert context.last_call(path) is None


def test_the_last_call_before_a_compaction_reads_as_none_until_the_next_call(tmp_path: Path) -> None:
    """A compaction between turns shrinks the context, so the call before it no longer measures it."""
    # The row the harness writes where a compaction ends the conversation it summarised.
    boundary = b'{"type": "system", "subtype": "compact_boundary", "compactMetadata": {"postTokens": 8160}}\n'
    path = tmp_path / "main.jsonl"
    path.write_bytes(line("assistant", "m1", 400_000) + boundary + line("user"))
    assert context.last_call(path) is None
    path.write_bytes(line("assistant", "m1", 400_000) + boundary + line("user") + line("assistant", "m2", 46_000))
    last = context.last_call(path)
    assert last is not None
    assert last.get("id") == "m2"


def test_a_clock_time_is_rounded_up_to_the_minute() -> None:
    """A time named as the earliest something succeeds must never be early."""
    moment = datetime.datetime.now().astimezone().replace(hour=21, minute=17, second=0, microsecond=0)
    assert store.clock_time(moment) == "21:17"
    assert store.clock_time(moment.replace(second=5)) == "21:18"
    assert store.clock_time(moment.replace(minute=59, second=1)) == "22:00"


def test_the_calling_sessions_folder_is_searched_before_the_whole_tree(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn's end reads every agent's transcript, and the whole tree grows with every session stored."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    own = transcript(config, "a1", line("assistant", "m1"), session="mine", minutes=5.5)
    newer = transcript(config, "a1", line("assistant", "m1"), session="elsewhere")
    agent = model.Agent("A", "writer", "running", ids=("a1",))
    monkeypatch.setenv(store.SESSION_ID, "mine")
    assert context.transcript(agent) == own
    # Dispatched by another session, as before a takeover, it is found in the whole tree, the newest first.
    assert context.transcript(agent, "other") == newer
    # A hook names its session from its input, which the judgement of whether the agent works passes on.
    monkeypatch.delenv(store.SESSION_ID)
    now = datetime.datetime.now().astimezone()
    assert [context.ended(agent, now, session)[0] for session in ("mine", None)] == [5, 0]


def test_a_tail_holding_only_the_last_messages_later_lines_reads_on_to_its_first(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One message reaches the transcript as a line per block, and the tool call may sit before a long block."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    long = "x" * (context.FIRST_BYTES + 1000)
    transcript(
        config,
        "a1",
        line("user"),
        line("assistant", "m9", calls=True),
        line("assistant", "m9", padding=long),
        line("assistant", "m9"),
        minutes=1.5,
    )
    assert context.ended(model.Agent("A", "writer", "running", ids=("a1",)), datetime.datetime.now().astimezone()) == (
        1,
        None,
    )
