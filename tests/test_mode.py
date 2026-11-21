"""`mode`: away and attended over a temporary home and project, and the lease's stop and takeover."""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from conftest import MakeSession, git, inprocess, orchestra, write_agent
from orchestra import config, log, model, store

OWNER = {store.SESSION_ID: "session-owner"}
OTHER = {store.SESSION_ID: "session-other"}


def _run(*args: str, cwd: Path, env: dict[str, str] = OWNER) -> subprocess.CompletedProcess[str]:
    return inprocess(*args, cwd=cwd, env=env)


def _session(project: Path) -> None:
    assert _run("init", "s1", cwd=project).returncode == 0


@pytest.fixture
def session(make_session: MakeSession) -> Path:
    """A session made by the owner; its state."""
    return make_session("mode", _session)


def _user(project: Path) -> Path:
    # The folder `conftest.inprocess` gives the program as CLAUDE_CONFIG_DIR.
    return project.parent / "claude-config" / "settings.json"


def _files(project: Path) -> dict[Path, bytes | None]:
    paths = [project / config.SETTINGS_LOCAL, _user(project)]
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def test_away_and_attended_change_no_settings_file(project: Path, session: Path) -> None:
    """The hooks answer for the away session, so no other Claude Code session of the user's is touched."""
    user = _user(project)
    user.write_bytes(b'{ "theme": "dark" }')
    before = _files(project)
    away = _run("mode", "away", "07:30", cwd=project)
    assert away.returncode == 0, away.stderr
    assert away.stdout.splitlines()[-1] == (
        "away until 07:30: this session's prompts are refused and its questions denied, each recorded;"
        " `orchestra mode attended` ends it"
    )
    assert store.lease(session)["owner"] == OWNER[store.SESSION_ID]
    assert "away      since " in inprocess("status", cwd=project).stdout
    again = _run("mode", "away", cwd=project)
    assert (again.returncode, again.stderr) == (
        2,
        "orchestra mode: the session is away already: `orchestra mode attended` ends it\n",
    )
    back = _run("mode", "attended", cwd=project)
    assert back.returncode == 0, back.stdout + back.stderr
    assert not (session / store.AWAY).exists()
    assert _files(project) == before


def test_away_needs_the_session_it_holds_for(project: Path, session: Path) -> None:
    """The hooks know the away session by the lease, which a terminal's command never takes."""
    done = inprocess("mode", "away", cwd=project)
    assert done.returncode == 2
    assert "CLAUDE_CODE_SESSION_ID is unset, so no Claude Code session would be away" in done.stderr
    assert not (session / store.AWAY).exists()


def test_attended_prints_the_log_since_away_with_each_decision_taken_alone(project: Path, session: Path) -> None:
    del session
    assert _run("note", "before the away", cwd=project).returncode == 0
    assert _run("mode", "away", cwd=project).returncode == 0
    assert _run("note", "Decision: kept the cap at 20; reversing costs one commit", cwd=project).returncode == 0
    back = _run("mode", "attended", cwd=project)
    assert "Decision: kept the cap at 20" in back.stdout
    assert "before the away" not in back.stdout
    assert "; 2 log line(s) since away, 1 decision(s) taken alone, 0 call(s) refused, 0 question(s) open" in back.stdout


def test_a_long_away_keeps_its_latest_lines_and_names_the_log_for_the_rest(project: Path, session: Path) -> None:
    """A second `attended` finds no away record, so what the first cuts is the log's to print."""
    assert _run("mode", "away", cwd=project).returncode == 0
    stamp = model.now()
    store.append_jsonl(session / store.LOG, *({"time": stamp, "kind": "note", "text": f"n{k}"} for k in range(45)))
    back = _run("mode", "attended", cwd=project)
    assert back.stderr.endswith(" earlier line(s): `orchestra log --all` prints every one\n")
    assert back.stdout.splitlines()[-2].endswith("n44")


def test_the_lease_refuses_another_session_and_hands_over_only_after_a_stop(project: Path, session: Path) -> None:
    """The takeover stops each agent the old session left running, which no other session can resume."""
    write_agent(session, model.Agent("LEFT", "writer", "running"))
    write_agent(session, model.Agent("DONE", "writer", "reported"))
    refused = _run("note", "a second coordinator", cwd=project, env=OTHER)
    assert refused.returncode == 2
    assert "session session-owner holds this session's state" in refused.stderr
    early = _run("mode", "takeover", "the owner hit a limit", cwd=project, env=OTHER)
    assert early.returncode == 2
    assert "has not stopped" in early.stderr
    assert _run("mode", "stop", cwd=project).returncode == 0
    after = _run("note", "after the stop", cwd=project)
    assert after.returncode == 2
    assert "stopped its state" in after.stderr
    taken = _run("mode", "takeover", "the owner hit a limit", cwd=project, env=OTHER)
    assert taken.returncode == 0, taken.stderr
    assert "from session session-owner (stopped): the owner hit a limit" in taken.stdout
    assert "stopped LEFT: session session-other took this state over" in taken.stdout
    left = model.read_agent(session, "LEFT")
    assert (left.status, left.reason) == ("stopped", "session session-other took this state over")
    assert model.read_agent(session, "DONE").status == "reported"
    assert _run("note", "the new coordinator", cwd=project, env=OTHER).returncode == 0
    # Run again, it stops what a takeover cut short after the lease left, and none of the session's own.
    hour = datetime.timedelta(hours=1)
    for name, start in (("STRAY", -hour), ("OWN", hour)):
        stamp = (datetime.datetime.now().astimezone() + start).isoformat(timespec="seconds")
        write_agent(session, model.Agent(name, "writer", "running", since={"running": stamp}))
    again = _run("mode", "takeover", "finish it", cwd=project, env=OTHER)
    assert again.returncode == 0, again.stderr
    assert again.stdout.splitlines() == [
        "stopped STRAY: session session-other took this state over",
        "session session-other holds this state already",
    ]
    assert model.read_agent(session, "OWN").status == "running"


def test_a_stop_from_a_process_with_no_session_id_stops_the_holder(project: Path, session: Path) -> None:
    """An owner may stop a session from a plain shell, which carries no Claude Code session id."""
    stopped = _run("mode", "stop", cwd=project, env={})
    assert stopped.returncode == 0, stopped.stderr
    assert stopped.stdout.startswith("stopped: no session writes this state")
    assert store.lease(session)["stopped"] is True
    assert log.entries(session)[-1].text == "stopped by session with no id"


def test_a_stop_whose_lease_another_session_took_meanwhile_is_refused_naming_the_holder(
    project: Path, session: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease can move between the stop's log line and its write, and the stop then claims nothing it did not do."""
    recorded = log.record

    def raced(state: Path, kind: str, text: str) -> log.Entry:
        done = recorded(state, kind, text)
        taken = {"owner": OTHER[store.SESSION_ID], "since": model.now(), "stopped": False}
        (state / store.LEASE).write_bytes(json.dumps(taken).encode())
        return done

    monkeypatch.setattr(log, "record", raced)
    stopped = _run("mode", "stop", cwd=project)
    assert stopped.returncode == 2, stopped.stdout
    said = "session session-other holds this session's state, so only it stops it; this is session session-owner"
    assert said in stopped.stderr
    assert store.lease(session)["stopped"] is False


def test_a_waiting_takeover_takes_the_state_once_its_holder_falls_quiet(project: Path, session: Path) -> None:
    """A resume runs it once in the background, so its exit code says whether the state was taken."""
    # `init` has just written the state, so its holder falls quiet ten minutes on, however loaded the runner.
    assert _run("mode", "takeover", "a limit", cwd=project, env=OTHER).returncode == 2
    # The holder's last write is set to fall quiet three seconds from now, so the wait sleeps once.
    stamp = time.time() - store.QUIET_SECONDS + 3
    for path in session.rglob("*"):
        if path.is_file():
            os.utime(path, (stamp, stamp))
    # The flag after the reason: Python 3.11's argparse leaves a positional list empty before a flag.
    taken = _run("mode", "takeover", "a limit", "--wait", cwd=project, env=OTHER)
    assert taken.returncode == 0, taken.stderr
    assert "took this state over from session session-owner (quiet): a limit" in taken.stdout
    # A refusal no wait can change comes at once. Run as a process, a wait is killed at the bound rather than
    # sat through until the holder falls quiet ten minutes on.
    alone = orchestra("mode", "takeover", "a limit", "--wait", cwd=project, timeout=30)
    assert (alone.returncode, "CLAUDE_CODE_SESSION_ID is unset" in alone.stderr) == (2, True)
    assert _run("mode", "attended", "--wait", cwd=project, env=OTHER).returncode == 2


def _snapshot(project: Path, session: Path) -> dict[str, object]:
    files = {path: path.read_bytes() for path in sorted(session.rglob("*")) if path.is_file()}
    return {"files": files, "settings": _files(project), "trees": git(project, "worktree", "list", "--porcelain")}


def test_a_session_not_holding_the_state_changes_nothing(project: Path, session: Path) -> None:
    """Each verb acting on git or the settings is refused before its first change, not at the write after it."""
    assert _run("mode", "away", cwd=project).returncode == 0
    before = _snapshot(project, session)
    for args in (("land", "W"), ("prune",), ("mode", "attended"), ("diff", "--tree")):
        refused = _run(*args, cwd=project, env=OTHER)
        assert refused.returncode == 2, (args, refused.stdout + refused.stderr)
        assert "session session-owner holds this session's state" in refused.stderr, args
        assert _snapshot(project, session) == before, args
