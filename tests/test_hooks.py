"""`hook`: each event driven through the command line with inputs shaped as Claude Code's hooks.md shows them.

The probes of a live session recorded the same shapes: a `PreToolUse` on `Edit` inside
`orchestra:writer` carries `agent_type` and a backslashed absolute `file_path`; the `Agent` result
carries `agentId` with no prefix; `SubagentStop`'s `cwd` is the agent's worktree.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import LIB, MakeRepo, git, inprocess, write_agent, write_config
from orchestra import context, events, findings, hooks, log, model, process, queue, report, store
from test_context import line, transcript
from test_report import text

SID = "c0ffee00-0000-4000-8000-000000000001"
OTHER = "c0ffee00-0000-4000-8000-000000000002"
HOOKS_JSON = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"
# The events whose call waits on the hook's answer.
GUARDING = {"PreToolUse", "PermissionRequest"}
BODY = (
    "GOAL      The module exists.\nWORK      1. Write it. At: lib/a.py. Accept: its test passes.\n"
    "PREMISES  none\nTRAPS     none\n"
)
# The no-fleet path run as the launcher runs it, then every module it loaded that it must not.
NO_FLEET = (
    "import runpy, sys\n"
    "sys.argv = ['orchestra', 'hook', 'Stop']\n"
    "try:\n"
    "    runpy.run_module('orchestra', run_name='__main__')\n"
    "except SystemExit as done:\n"
    "    print(done.code)\n"
    "heavy = {'orchestra.cli', 'orchestra.config', 'orchestra.store', 'orchestra.events', 'argparse', 'subprocess'}\n"
    "print(sorted(heavy & set(sys.modules)))\n"
)
# One event answered as the launcher answers it, then which of the fleet's heavier modules it loaded.
FLEET = (
    "import json, sys\n"
    "from orchestra import hooks\n"
    "hooks.main(sys.argv[1])\n"
    "print('', json.dumps(sorted({'orchestra.events', 'orchestra.status'} & set(sys.modules))), sep='\\n')\n"
)


def run(*args: str, cwd: Path, session: str = SID) -> subprocess.CompletedProcess[str]:
    """A verb run from a session's Bash tool, the coordinator's unless told."""
    return inprocess(*args, cwd=cwd, env={store.SESSION_ID: session})


def hook(
    event: str,
    cwd: Path,
    *parts: dict[str, object],
    process: str | None = SID,
    quiet: bool = True,
    env: dict[str, str] | None = None,
    **more: object,
) -> tuple[int, object]:
    """The event answered, its input the common fields, then `parts` and `more`: its exit code and its stdout.

    `quiet` holds stderr empty; an event that cannot be judged says why there. `env` adds to the hook's environment.
    """
    data: dict[str, object] = {"session_id": SID, "cwd": str(cwd), "hook_event_name": event}
    for part in (*parts, more):
        data |= part
    env = ({store.SESSION_ID: process} if process else {}) | (env or {})
    done = inprocess("hook", event, cwd=cwd, stdin=json.dumps(data).encode(), env=env)
    assert done.stderr == "" or not quiet, done.stderr
    if not done.stdout:
        return done.returncode, None
    try:
        return done.returncode, json.loads(done.stdout)
    except json.JSONDecodeError:
        return done.returncode, done.stdout


def field(said: object, *keys: str) -> object:
    """The value under `keys` in a JSON answer."""
    for key in keys:
        assert isinstance(said, dict), said
        said = cast("dict[str, object]", said)[key]
    return said


def decision(said: object) -> tuple[object, object]:
    """A `PreToolUse` answer's decision and its reason, or its updated input."""
    out = field(said, "hookSpecificOutput")
    reason = cast("dict[str, object]", out).get("permissionDecisionReason")
    return field(out, "permissionDecision"), reason if reason is not None else field(out, "updatedInput")


def context_of(said: object, event: str) -> str:
    assert field(said, "hookSpecificOutput", "hookEventName") == event
    return str(field(said, "hookSpecificOutput", "additionalContext"))


def changed(state: Path, name: str, **values: object) -> None:
    """A record's fields set as a verb or a later hook would leave them."""
    model.update_agent(state, name, lambda held: dataclasses.replace(held, **values))


@pytest.fixture
def state(project: Path) -> Path:
    (project / "lib").mkdir()
    (project / "lib" / "a.py").write_bytes(b"x\n")
    git(project, "add", ".")
    git(project, "commit", "-m", "lib")
    assert run("init", "s1", cwd=project).returncode == 0
    return store.state_dir(project)


def briefed(project: Path, name: str, kind: str, *, checked: bool = True, **lists: str) -> Path:
    """A brief written by the verb, each list label `none` unless `lists` names it, and checked unless told not."""
    done = run("brief", name, "--type", kind, "--slice", "S1", cwd=project)
    assert done.returncode == 0, done.stderr
    path = store.state_dir(project) / "briefs" / f"{name}.md"
    held = path.read_bytes().decode()
    for label in ("Owns", "Rows", "Writes", "Lens"):
        held = held.replace(f"\n{label}:\n", f"\n{label}: {lists.get(label.lower(), 'none')}\n")
    path.write_bytes(held.replace("GOAL\nWORK\nPREMISES\nTRAPS\n", BODY).encode())
    if checked:
        done = run("brief", name, "--check", cwd=project)
        assert done.returncode == 0, done.stdout
    return path


def dispatched(project: Path, name: str, kind: str, ident: str) -> object:
    """The dispatch's PostToolUse, as a background launch returns it; its answer."""
    given = {"description": name, "prompt": "Brief: x", "subagent_type": f"orchestra:{kind}", "name": name}
    response = {"isAsync": True, "status": "async_launched", "agentId": ident, "description": name}
    code, said = hook("PostToolUse", project, tool_name="Agent", tool_input=given, tool_response=response)
    assert code == 0
    return said


def inside(project: Path, ident: str, kind: str, cwd: Path | None = None, **fields: object) -> dict[str, object]:
    """The common fields of an event fired inside an agent."""
    return {"agent_id": ident, "agent_type": f"orchestra:{kind}", "cwd": str(cwd or project), **fields}


def edit(target: Path) -> dict[str, object]:
    # Claude Code hands a file tool's path absolute, with backslashes on Windows (hooks.md, "PreToolUse input").
    return {"tool_name": "Edit", "tool_input": {"file_path": str(target), "old_string": "a", "new_string": "b"}}


# --- the registrations and the dispatcher ---------------------------------------------------------


def test_each_event_is_one_registration_calling_the_launcher_by_the_plugin_root() -> None:
    held = json.loads(HOOKS_JSON.read_bytes())
    # The registrations, and at most a `modules` key beside them.
    assert "hooks" in held
    assert set(held) <= {"hooks", "modules"}
    assert set(held["hooks"]) == set(hooks.EVENTS)
    matchers = {}
    for event, groups in held["hooks"].items():
        assert len(groups) == 1, event
        (group,) = groups
        (handler,) = group["hooks"]
        # A hook's PATH holds no plugin `bin/`, and the variable is quoted for a path with spaces.
        assert handler["command"] == f'"${{CLAUDE_PLUGIN_ROOT}}/bin/orchestra" hook {event}'
        assert handler["type"] == "command"
        # Each budget inside the next: a lock wait inside the hook's, the hook's inside Claude Code's cancel.
        if event == "SessionEnd":
            # Its 1.5-s budget is shared and fixed: a plugin hook's `timeout` never raises it (hooks.md).
            assert "timeout" not in handler
            assert events.SESSION_END_SECONDS < 1.5
        else:
            assert handler["timeout"] > events.BUDGET_SECONDS > store.LOCK_SECONDS, event
        # A hook that cannot start, crashes or times out lets its call through, so a broken install blocks
        # no session. Set where the call waits on it: on Windows without Git Bash an unset value blocks a
        # `PreToolUse` or a `PermissionRequest` (hooks.md, "Block the action when a hook fails").
        assert handler.get("onFailure") == ("continue" if event in GUARDING else None), event
        matchers[event] = group.get("matcher")
    # Each tool a `PreToolUse` handler judges, written out rather than read from the code, so a tool dropped
    # from both the code and the registration still turns this red.
    judged = ["Agent", "Edit", "Write", "NotebookEdit", "SendMessage", "AskUserQuestion", "ExitPlanMode"]
    assert ["Agent", *events.WRITERS, "SendMessage", *hooks.ASKING] == judged
    assert matchers == {
        "PreToolUse": "|".join(judged),
        # Every prompt, a background agent's included, so a session away never waits on one.
        "PermissionRequest": None,
        "PermissionDenied": None,
        "PostToolUse": "Agent|SendMessage",
        # The colon puts the matcher on the regular-expression path, which is unanchored unless it says so.
        "SubagentStop": "^orchestra:",
        "StopFailure": None,
        "Stop": None,
        "SessionStart": "compact|resume|clear",
        "SessionEnd": "clear",
        "UserPromptSubmit": None,
    }
    assert (hooks.POINTERS, hooks.AWAY) == (store.POINTERS, store.AWAY)


def test_a_session_with_no_fleet_loads_no_state_module_and_runs_no_git(project: Path) -> None:
    """Every session with the plugin pays this path on every hook, so it imports nothing heavy and starts nothing."""
    data = json.dumps({"session_id": SID, "cwd": str(project), "hook_event_name": "Stop"}).encode()
    # No git on PATH: a path that ran it would fail here rather than pass unseen.
    env = {**os.environ, "PYTHONPATH": str(LIB), "PATH": str(Path(sys.executable).parent)}
    done = subprocess.run((sys.executable, "-P", "-c", NO_FLEET), input=data, capture_output=True, env=env, check=False)
    assert (done.returncode, done.stderr) == (0, b"")
    assert done.stdout.decode().splitlines() == ["0", "[]"]


def loaded(project: Path, event: str, *parts: dict[str, object]) -> list[str]:
    """The fleet's heavier modules one event loads, answered in a process of its own as the launcher runs it."""
    data: dict[str, object] = {"session_id": SID, "cwd": str(project), "hook_event_name": event}
    for part in parts:
        data |= part
    env = {**os.environ, "PYTHONPATH": str(LIB)}
    done = subprocess.run(
        (sys.executable, "-P", "-c", FLEET, event),
        input=json.dumps(data).encode(),
        capture_output=True,
        env=env,
        check=False,
    )
    assert (done.returncode, done.stderr) == (0, b""), done.stderr
    return json.loads(done.stdout.splitlines()[-1])


def test_a_fleets_edit_loads_no_status_and_a_prompt_at_work_loads_no_events(project: Path, state: Path) -> None:
    """`events` imports `status` only where it reads the overview, and a prompt decides only while away.

    A turn's end with the checkpoint due reads the plan through the model, never through the status view.
    """
    assert loaded(project, "PreToolUse", edit(project / "lib" / "a.py")) == ["orchestra.events"]
    assert loaded(project, "PermissionRequest", BASH) == []
    assert loaded(project, "PreToolUse", QUESTION) == []
    store.write_text(state / store.PLAN, "## Slices\n\n- [ ] S1: the store\n")
    # Only a writer's dispatch makes the first checkpoint due.
    write_agent(state, model.Agent("A", "writer", "running"))
    log.record(state, log.DISPATCH, "A as a1 on the inherited model")
    stopped = context_of(hook("Stop", project, stop_hook_active=False)[1], "Stop")
    assert "1 plan line is unticked: the checkpoint in checkpoint.md is due" in stopped
    assert loaded(project, "Stop", {"stop_hook_active": False}) == ["orchestra.events"]
    assert run("mode", "away", "07:30", cwd=project).returncode == 0
    assert loaded(project, "PermissionRequest", BASH) == ["orchestra.events"]


def test_an_agents_message_loads_no_events_and_no_fault_denies_it(project: Path, state: Path) -> None:
    """Only the coordinator's message resumes an agent or routes its rows, so an agent's decides nothing.

    Loaded, `events` could only fault on it, and a fault denies a plugin agent's call: the agent's word to its
    coordinator would be refused.
    """
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    message: dict[str, object] = {"tool_name": "SendMessage", "tool_input": {"to": "main", "message": "F1 blocks"}}
    fields = inside(project, "a1", "auditor")
    assert loaded(project, "PreToolUse", fields, message) == []
    assert loaded(project, "PostToolUse", fields, message, {"tool_response": {}}) == []
    # A record an older or newer build wrote faults every event that reads the records.
    (state / "agents" / "Z.json").write_bytes(b'{"name": "Z", "type": "auditor", "laterField": 1}\n')
    assert hook("PreToolUse", project, fields, message) == (0, None)
    # The same fault still denies the agent's write, which the records decide.
    assert decision(hook("PreToolUse", project, fields, edit(project / "lib" / "a.py"), quiet=False)[1])[0] == "deny"


@pytest.mark.usefixtures("state")
def test_another_session_in_the_same_checkout_gets_no_answer(project: Path) -> None:
    assert hook("Stop", project, session_id=OTHER, process=OTHER) == (0, None)
    assert hook("UserPromptSubmit", project, session_id=OTHER, process=OTHER, prompt="hi") == (0, None)
    assert hook("UserPromptSubmit", project, prompt="hi")[1] is not None


def test_a_coordinator_whose_shell_left_the_checkout_is_found_from_the_project_root(
    project: Path, state: Path, make_repo: MakeRepo
) -> None:
    """A Bash `cd` moves the input's `cwd`, and the state's folder is a working directory a `cd` stays in.

    Claude Code exports the project root where the session started as `CLAUDE_PROJECT_DIR`, which no `cd` moves;
    an agent's `cwd` stays its own folder, from which its branch is read. A `cwd` in a clone holding a session
    of its own is answered from that session, the project root's left unread.
    """
    root = {"CLAUDE_PROJECT_DIR": str(project)}
    briefed(project, "A", "auditor")
    call = {"description": "A", "prompt": "A", "subagent_type": "orchestra:auditor"}
    assert decision(hook("PreToolUse", state, tool_name="Agent", tool_input=call, env=root)[1])[0] == "allow"
    assert decision(hook("PreToolUse", state, edit(state / "lease.json"), env=root)[1])[0] == "deny"
    said = hook("UserPromptSubmit", state, prompt="go", env=root)[1]
    assert context_of(said, "UserPromptSubmit").startswith("The clock reads ")
    briefed(project, "W", "writer", owns="lib/a.py")
    dispatched(project, "W", "writer", "w1")
    tree = project.parent / "trees" / "agent-w1"
    git(project, "worktree", "add", "-b", "worktree-agent-w1", str(tree))
    assert hook("PreToolUse", project, inside(project, "w1", "writer", cwd=tree), edit(tree / "README"), env=root) == (
        0,
        None,
    )
    assert model.read_agent(state, "W").branch == "worktree-agent-w1"
    second = make_repo(project.parent / "second")
    write_config(second, {"stateRoot": (project.parent / "state").as_posix()})
    assert run("init", "s2", cwd=second).returncode == 0
    briefed(second, "B", "auditor")
    call = {"description": "B", "prompt": "B", "subagent_type": "orchestra:auditor"}
    # Only the second clone's session holds B's brief, so the first's would refuse this dispatch.
    assert decision(hook("PreToolUse", second, tool_name="Agent", tool_input=call, env=root)[1])[0] == "allow"


def test_an_agents_stop_is_recorded_whichever_session_holds_the_lease(project: Path, state: Path) -> None:
    """An agent's own facts pass the lease: after a coordinator's `/clear` or a takeover its stop still lands."""
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    report.path(state, "A").write_bytes(text("auditor", "A", status="BLOCKED: the store is missing").encode())
    lease = {"owner": OTHER, "since": model.now(), "stopped": False}
    (state / store.LEASE).write_bytes(json.dumps(lease).encode())
    assert hook("SubagentStop", project, inside(project, "a1", "auditor", last_assistant_message="x")) == (0, None)
    agent = model.read_agent(state, "A")
    assert (agent.status, agent.outcome) == ("reported", "BLOCKED")
    assert log.entries(state)[-1].text == "A round 1: BLOCKED"


def test_a_clear_stops_the_lease_so_the_new_session_takes_it_over_at_once(project: Path, state: Path) -> None:
    assert hook("SessionEnd", project, reason="prompt_input_exit") == (0, None)
    assert not store.lease(state)["stopped"]
    # The hook's process may carry the new session's id already.
    assert hook("SessionEnd", project, process=OTHER, reason="clear") == (0, None)
    assert store.lease(state)["stopped"]
    assert log.entries(state)[-1].text.startswith(f"stopped by /clear in session {SID}")
    # The new session is told what the `/clear` stopped, and not that the stopped session was its own.
    said = context_of(hook("SessionStart", project, session_id=OTHER, process=OTHER, source="clear")[1], "SessionStart")
    assert said.startswith(f"A /clear ended session {SID} ")
    assert f"whose state is {state}." in said
    refused = run("note", "after the clear", cwd=project, session=OTHER)
    assert refused.returncode == 2
    assert 'continue it here with `orchestra mode takeover "<reason>"`' in refused.stderr
    assert run("mode", "takeover", "the coordinator cleared", cwd=project, session=OTHER).returncode == 0
    assert store.lease(state)["owner"] == OTHER


def test_a_session_opened_on_a_clear_long_after_a_stop_is_offered_no_takeover(project: Path, state: Path) -> None:
    """A `/clear` past the window is no continuation of the session the earlier one stopped."""
    assert hook("SessionEnd", project, process=OTHER, reason="clear") == (0, None)
    held = store.read_jsonl(state / store.LOG)
    past = datetime.datetime.now().astimezone() - datetime.timedelta(seconds=events.CLEARED_SECONDS + 60)
    held[-1]["time"] = past.isoformat(timespec="seconds")
    (state / store.LOG).write_bytes(b"".join(json.dumps(entry).encode() + b"\n" for entry in held))
    assert hook("SessionStart", project, session_id=OTHER, process=OTHER, source="clear") == (0, None)


def test_a_hook_gives_up_on_a_held_lock_within_its_budget(project: Path, state: Path) -> None:
    """A lock held past the budget is given up on, logged, rather than the hook cancelled mid-write.

    `SessionEnd`'s one-second budget is the cheap one to drive; the same call sets every event's.
    """
    with store.locked(state / store.LEASE):
        start = time.monotonic()
        code, _ = hook("SessionEnd", project, reason="clear", quiet=False)
        took = time.monotonic() - start
    assert code == 0
    assert took < store.LOCK_SECONDS / 2, took
    assert not store.lease(state)["stopped"]


# --- the dispatch ---------------------------------------------------------------------------------


def test_a_dispatch_is_refused_until_its_brief_is_checked_then_filled(project: Path, state: Path) -> None:
    profiles = {"default": {"model": "opus", "effort": "high"}}
    write_config(project, {"stateRoot": (project.parent / "state").as_posix(), "models": profiles})
    path = briefed(project, "W", "writer", owns="lib/a.py", checked=False)
    call = {"description": "W", "prompt": "W", "subagent_type": "orchestra:writer"}
    code, said = hook("PreToolUse", project, tool_name="Agent", tool_input=call)
    assert code == 0
    assert decision(said) == (
        "deny",
        "W's brief is not checked, or changed since its check: run `orchestra brief W --check`",
    )
    assert run("brief", "W", "--check", cwd=project).returncode == 0
    code, said = hook("PreToolUse", project, tool_name="Agent", tool_input=call)
    verdict, updated = decision(said)
    assert verdict == "allow"
    # The report's title names round 1, which only the dispatch knows: an agent's name may suggest another.
    assert updated == {
        "description": "W",
        "prompt": f"Brief: {path}\nMessages: {path.with_name('W.messages.md')}\nReport: {report.path(state, 'W')},"
        " titled `# W · writer · round 1`\nAddendum: none",
        "subagent_type": "orchestra:writer",
        "name": "W",
        "model": "opus",
        "effort": "high",
        "isolation": "worktree",
    }
    # A project's addendum for the type is named, so the agent reads it rather than probing for one.
    addendum = project / ".claude" / "orchestra" / "auditor.md"
    addendum.parent.mkdir(parents=True)
    addendum.write_bytes(b"# Auditor\n")
    briefed(project, "A", "auditor")
    call = {"description": "A", "prompt": "A", "subagent_type": "orchestra:auditor"}
    prompt = decision(hook("PreToolUse", project, tool_name="Agent", tool_input=call)[1])[1]
    assert str(field(prompt, "prompt")).splitlines()[-1] == f"Addendum: {addendum}"


def test_a_dispatch_its_record_does_not_allow_is_refused(project: Path, state: Path) -> None:
    """A dispatch as another type would hand an auditor's brief a worktree, and a second one would take over a
    running agent's report."""
    briefed(project, "A", "auditor")
    call = {"description": "A", "prompt": "A", "subagent_type": "orchestra:writer"}
    said = hook("PreToolUse", project, tool_name="Agent", tool_input=call)[1]
    assert decision(said) == ("deny", "A is auditor, and this dispatch names orchestra:writer")
    assert model.read_agent(state, "A").status == "briefed"
    dispatched(project, "A", "auditor", "a1")
    said = hook("PreToolUse", project, tool_name="Agent", tool_input=call | {"subagent_type": "orchestra:auditor"})[1]
    running = "A is running: a dispatch starts a briefed agent, and SendMessage resumes this one"
    assert decision(said) == ("deny", running)
    assert model.read_agent(state, "A").status == "running"


def test_a_dispatch_from_a_session_holding_no_lease_names_when_a_takeover_succeeds(project: Path, state: Path) -> None:
    """The holder wrote a moment ago, so a takeover now would be refused; the dispatch says when it would not."""
    briefed(project, "A", "auditor")
    at = store.takeover_at(state)
    assert at is not None
    call = {"description": "A", "prompt": "A", "subagent_type": "orchestra:auditor"}
    said = hook("PreToolUse", project, tool_name="Agent", tool_input=call, session_id=OTHER, process=OTHER)[1]
    assert str(decision(said)[1]).endswith(
        f"a takeover succeeds at {store.clock_time(at)}, unless that session writes again before"
    )


def test_an_override_serves_one_refusal_of_the_caller_it_names(project: Path, state: Path) -> None:
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    briefed(project, "B", "auditor", checked=False)
    call = {"description": "B", "prompt": "B", "subagent_type": "orchestra:auditor"}
    write = (inside(project, "a1", "auditor"), edit(project / "lib" / "a.py"))
    # No hook has refused the coordinator, so the override would answer a verb's refusal, which it never reaches.
    early = run("override", "prune keeps the tree", cwd=project)
    assert early.returncode == 2, early.stdout
    assert "no hook refusal of the coordinator waits unserved" in early.stderr
    assert decision(hook("PreToolUse", project, tool_name="Agent", tool_input=call)[1])[0] == "deny"
    armed = run("override", "the stamp is stale", cwd=project)
    assert armed.returncode == 0, armed.stderr
    assert ", was the dispatch of B (B's brief is not checked, or changed since its check" in armed.stdout
    # The coordinator's override is no agent's: A's refusal leaves it waiting.
    assert decision(hook("PreToolUse", project, *write)[1])[0] == "deny"
    assert decision(hook("PreToolUse", project, tool_name="Agent", tool_input=call)[1])[0] == "allow"
    assert decision(hook("PreToolUse", project, tool_name="Agent", tool_input=call)[1])[0] == "deny"
    assert run("override", "--agent", "A", "the fence is wrong", cwd=project).returncode == 0
    assert decision(hook("PreToolUse", project, tool_name="Agent", tool_input=call)[1])[0] == "deny"
    assert hook("PreToolUse", project, *write) == (0, None)
    # A served refusal is cleared: arming again waits on the next.
    assert run("override", "--agent", "A", "again", cwd=project).returncode == 2
    assert decision(hook("PreToolUse", project, *write)[1])[0] == "deny"
    used = [entry.text for entry in log.entries(state) if entry.text.startswith("used on")]
    assert [text.rsplit(": ", 1)[1] for text in used] == ["the stamp is stale", "the fence is wrong"]


def test_a_dispatch_records_the_id_and_routes_the_briefs_rows_to_the_agent(project: Path, state: Path) -> None:
    model.write_findings(state, lambda _: [model.FindingEvent("X-F1", "bank", "OPEN")])
    briefed(project, "A", "auditor", rows="X-F1")
    assert dispatched(project, "A", "auditor", "a1") is None
    agent = model.read_agent(state, "A")
    assert (agent.ids, agent.status, agent.reports) == (("a1",), "running", {1: "reports/A.md"})
    held = model.read_findings(state)["X-F1"]
    assert (held.status, held.owner) == ("ROUTED", "A")


# --- the write fence ------------------------------------------------------------------------------


def test_an_auditor_writes_only_its_report_its_writes_and_the_scratch_folders(project: Path, state: Path) -> None:
    out = project.parent / "out"
    briefed(project, "A", "auditor", writes=f"{out.as_posix()}/*.md")
    dispatched(project, "A", "auditor", "a1")
    scratch = project.parent / "scratch"
    fields = inside(project, "a1", "auditor", scratchpad_dir=str(scratch))
    allowed = (report.path(state, "A"), out / "notes.md", scratch / "x.txt", state / "scratch" / "A" / "page.html")
    for target in allowed:
        assert hook("PreToolUse", project, fields, edit(target)) == (0, None), target
    # A sibling whose name only begins with an allowed folder's lies outside it, and the `Writes:` pattern
    # names its folder's `.md` files alone.
    siblings = (state / "scratch" / "AB" / "page.html", project.parent / "scratch2" / "x.txt")
    unnamed = (out / "x.py", out / "sub" / "a.md")
    for target in (project / "lib" / "a.py", state / "scratch" / "B" / "page.html", *siblings, *unnamed):
        code, said = hook("PreToolUse", project, fields, edit(target))
        assert code == 0
        assert decision(said)[0] == "deny", target
        assert "A is an auditor and writes only its report" in str(decision(said)[1])
    notebook: dict[str, object] = {
        "tool_name": "NotebookEdit",
        "tool_input": {"notebook_path": str(project / "a.ipynb")},
    }
    assert decision(hook("PreToolUse", project, fields, notebook)[1])[0] == "deny"


def test_an_unreadable_record_denies_an_agents_write_rather_than_letting_it_through(project: Path, state: Path) -> None:
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    (state / "agents" / "Z.json").write_bytes(b'{"name": "Z", "type": "auditor", "laterField": 1}\n')
    code, said = hook(
        "PreToolUse", project, inside(project, "a1", "auditor"), edit(project / "lib" / "a.py"), quiet=False
    )
    assert code == 0
    verdict, reason = decision(said)
    assert verdict == "deny"
    assert str(reason).startswith(f"orchestra cannot judge this call, so it refuses it: {state / 'agents' / 'Z.json'}")


@pytest.mark.usefixtures("state")
def test_a_plugin_agent_no_record_holds_is_refused_and_any_other_agent_passes(project: Path) -> None:
    fields = inside(project, "zz9", "writer")
    reason = decision(hook("PreToolUse", project, fields, edit(project / "lib" / "a.py"))[1])[1]
    assert str(reason).startswith("no orchestra record holds agent zz9")
    plain: dict[str, object] = {"agent_id": "e1", "agent_type": "Explore"}
    assert hook("PreToolUse", project, plain, edit(project / "lib" / "a.py")) == (0, None)


def test_a_writer_keeps_out_of_the_state_and_its_branch_is_read_from_its_worktree(project: Path, state: Path) -> None:
    """Its stop reads the branch and the uncommitted work from the worktree, and so does its first write."""
    briefed(project, "W", "writer", owns="lib/a.py")
    dispatched(project, "W", "writer", "w1")
    tree = project.parent / "trees" / "agent-w1"
    git(project, "worktree", "add", "-b", "worktree-agent-w1", str(tree))
    report.path(state, "W").write_bytes(text("writer", "W").encode())
    (tree / "loose.txt").write_bytes(b"x\n")
    said = hook("SubagentStop", project, inside(project, "w1", "writer", cwd=tree, last_assistant_message="x"))[1]
    assert "`git status --porcelain`" in str(field(said, "reason"))
    assert model.read_agent(state, "W").branch == "worktree-agent-w1"
    changed(state, "W", branch=None)
    fields = inside(project, "w1", "writer", cwd=tree)
    for allowed in (project / "README", report.path(state, "W"), state / "scratch" / "W" / "x.txt"):
        assert hook("PreToolUse", project, fields, edit(allowed)) == (0, None), allowed
    assert model.read_agent(state, "W").branch == "worktree-agent-w1"
    # The programme's folder beyond its own session too: the handoffs and other sessions later ones read.
    beyond = (model.rulings_path(state), state.parent / "HANDOFF.md", state.parent / "s0" / "NOW.md")
    for refused in (state / "override.json", state / "agents" / "W.json", *beyond):
        assert decision(hook("PreToolUse", project, fields, edit(refused))[1])[0] == "deny", refused


def test_the_coordinator_keeps_out_of_the_files_commands_and_hooks_write(project: Path, state: Path) -> None:
    briefed(project, "W", "writer", owns="lib/a.py")
    upper = Path(str(state).upper()) if sys.platform == "win32" else state
    refused = (
        upper / "agents" / "W.json",
        state / "briefs" / "W.messages.md",
        state / "lease.json",
        model.rulings_path(state),
    )
    for target in refused:
        assert decision(hook("PreToolUse", project, edit(target))[1])[0] == "deny", target


def spellings(folder: Path, link: Path) -> list[Path]:
    """Other spellings of the folder: a link to it, and on Windows the long-path form too."""
    if sys.platform == "win32":
        import _winapi  # noqa: PLC0415

        # A junction needs no privilege to make, where a folder's symbolic link does.
        _winapi.CreateJunction(str(folder), str(link))
        # The long-path form, which Windows' file calls take as written.
        return [link, Path("\\\\?\\" + str(folder))]
    link.symlink_to(folder)
    return [link]


def test_a_state_file_spelled_through_a_link_or_the_long_path_form_is_fenced_as_itself(
    project: Path, state: Path
) -> None:
    """Every spelling of a folder names the same files, so the fences judge the folder, never its spelling."""
    briefed(project, "W", "writer", owns="lib/a.py")
    dispatched(project, "W", "writer", "w1")
    writer = inside(project, "w1", "writer")
    for folder in spellings(state.parent, project.parent / "link"):
        held = folder / state.name
        for target in (held / "agents" / "W.json", held / "override.json", folder / store.RULINGS):
            for fields in ({}, writer):
                assert decision(hook("PreToolUse", project, fields, edit(target))[1])[0] == "deny", (fields, target)
        # The scratch folder, not made yet, is the writer's own however it is spelled.
        assert hook("PreToolUse", project, writer, edit(held / "scratch" / "W" / "x.txt")) == (0, None), folder


def test_an_allowed_file_spelled_through_a_link_or_the_long_path_form_is_allowed_as_itself(
    project: Path, state: Path
) -> None:
    """The allow side judges a state file as the deny side does, by identity, so no spelling refuses its write."""
    out = project.parent / "out"
    out.mkdir()
    briefed(project, "A", "auditor", writes=f"{out.as_posix()}/**")
    dispatched(project, "A", "auditor", "a1")
    auditor = inside(project, "a1", "auditor")
    report.path(state, "A").write_bytes(b"# A\n")
    for folder in spellings(state.parent, project.parent / "link"):
        held = folder / state.name
        # The auditor's report; the coordinator's own files and a brief.
        assert hook("PreToolUse", project, auditor, edit(held / "reports" / "A.md")) == (0, None), folder
        for target in (held / "PLAN.md", held / "NOW.md", held / "briefs" / "A.md"):
            assert hook("PreToolUse", project, edit(target)) == (0, None), target
        # A messages file stays the hook's, however the briefs folder is spelled.
        assert decision(hook("PreToolUse", project, edit(held / "briefs" / "A.messages.md"))[1])[0] == "deny"
    # A `Writes:` path below its folder, and the folder alone, which `**` does not name.
    assert hook("PreToolUse", project, auditor, edit(out / "notes" / "a.md")) == (0, None)
    assert decision(hook("PreToolUse", project, auditor, edit(out))[1])[0] == "deny"


# --- a resume -------------------------------------------------------------------------------------


def test_a_resume_of_a_writer_whose_worktree_is_gone_is_refused(project: Path, state: Path) -> None:
    briefed(project, "W", "writer", owns="lib/a.py")
    dispatched(project, "W", "writer", "w1")
    changed(state, "W", status="reported", branch="gone")
    said = hook("PreToolUse", project, tool_name="SendMessage", tool_input={"to": "W", "message": "fix F1"})[1]
    assert decision(said) == (
        "deny",
        "W's worktree is gone (gone), so a resume has no worktree to run in: `orchestra judge W` banks its report,"
        " then dispatch a successor briefed with what is left",
    )
    # A limit's stop before the report reads leaves nothing for `judge`, which refuses it, so `prune` settles it.
    changed(state, "W", status="stopped", reason="You've hit your limit")
    report.path(state, "W").write_bytes(text("writer", "W", status="IN-PROGRESS").encode())
    said = hook("PreToolUse", project, tool_name="SendMessage", tool_input={"to": "W", "message": "go on"})[1]
    assert decision(said) == (
        "deny",
        "W's worktree is gone (gone), so a resume has no worktree to run in: `orchestra prune` settles its record,"
        " then dispatch a successor briefed with what is left",
    )
    # One whose report reads, written before the limit, is judged as a reported one is.
    report.path(state, "W").write_bytes(text("writer", "W", status="DONE").encode())
    said = hook("PreToolUse", project, tool_name="SendMessage", tool_input={"to": "W", "message": "go on"})[1]
    assert "`orchestra judge W` banks its report, then dispatch a successor" in str(decision(said)[1])
    # A judged record has nothing left to bank.
    changed(state, "W", status="judged")
    said = hook("PreToolUse", project, tool_name="SendMessage", tool_input={"to": "W", "message": "fix F1"})[1]
    assert decision(said) == (
        "deny",
        "W's worktree is gone (gone), so a resume has no worktree to run in: dispatch a successor briefed with what"
        " is left",
    )


def test_a_resume_past_the_context_ceiling_is_refused_naming_the_figure_once_its_cache_has_lapsed(
    project: Path, state: Path
) -> None:
    """A resume within a subagent's five-minute cache reads its context, which costs about what a successor does."""
    write_config(project, {"stateRoot": (project.parent / "state").as_posix(), "contextCeiling": 100_000})
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    changed(state, "A", status="judged")
    config_dir = project.parent / "claude-config"
    transcript(config_dir, "a1", line("assistant", "m1", 150_000), minutes=events.CACHE_MINUTES + 1)
    said = hook("PreToolUse", project, tool_name="SendMessage", tool_input={"to": "A", "message": "fix F1"})[1]
    assert decision(said) == (
        "deny",
        "A's context is 150,474 tokens, over the configuration's contextCeiling of 100,000, and its transcript was"
        " last written 6 min ago, past the 5-min cache: a resume writes it all to the cache again, so dispatch a"
        " successor briefed with what is left",
    )
    # Written a moment inside the cache's lifetime, the same resume goes ahead.
    transcript(config_dir, "a1", line("assistant", "m1", 150_000), minutes=events.CACHE_MINUTES - 0.5)
    said = hook("PreToolUse", project, tool_name="SendMessage", tool_input={"to": "A", "message": "fix F1"})[1]
    assert decision(said)[0] == "allow"
    changed(state, "A", status="judged")
    transcript(config_dir, "a1", line("assistant", "m1", 150_000), minutes=events.CACHE_MINUTES + 1)
    # Under the ceiling, the same resume goes ahead.
    write_config(project, {"stateRoot": (project.parent / "state").as_posix(), "contextCeiling": 200_000})
    said = hook("PreToolUse", project, tool_name="SendMessage", tool_input={"to": "A", "message": "fix F1"})[1]
    assert decision(said)[0] == "allow"


def test_a_writer_stopped_on_a_question_past_the_ceiling_is_answered_and_a_finished_one_is_not_resumed(
    project: Path, state: Path
) -> None:
    """While its worktree stands and its question waits, `judge`, `land` and `prune --discard` hold it."""
    write_config(project, {"stateRoot": (project.parent / "state").as_posix(), "contextCeiling": 100_000})
    briefed(project, "W", "writer", owns="lib/a.py")
    dispatched(project, "W", "writer", "w1")
    git(project, "worktree", "add", "-b", "worktree-agent-w1", str(project.parent / "trees" / "agent-w1"))
    stale = events.CACHE_MINUTES + 1
    transcript(project.parent / "claude-config", "w1", line("assistant", "m1", 150_000), minutes=stale)
    sent = {"to": "W", "message": "Keep the old name."}
    changed(state, "W", status="judged", outcome="DONE", branch="worktree-agent-w1")
    said = hook("PreToolUse", project, tool_name="SendMessage", tool_input=sent)[1]
    assert str(decision(said)[1]).startswith(
        "W's context is 150,474 tokens, over the configuration's contextCeiling of 100,000"
    )
    for outcome in ("BLOCKED", "NEEDS-ANSWER"):
        changed(state, "W", status="reported", outcome=outcome, reports={1: "reports/W.md"})
        told = f"Keep the old name.\n\nThis continues round 1: complete your report at {report.path(state, 'W')}."
        said = hook("PreToolUse", project, tool_name="SendMessage", tool_input=sent)[1]
        assert decision(said) == ("allow", sent | {"message": told}), outcome
    # A question answered before a usage stop keeps its outcome, but nothing waits on that answer any more.
    changed(state, "W", status="stopped", outcome="NEEDS-ANSWER")
    said = hook("PreToolUse", project, tool_name="SendMessage", tool_input=sent)[1]
    assert decision(said)[0] == "deny"


def test_a_reported_agent_is_set_running_in_its_next_round_before_the_message_reaches_it(
    project: Path, state: Path
) -> None:
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    changed(state, "A", status="judged", outcome="DONE")
    briefed(project, "B", "auditor")
    changed(state, "B", status="running")
    model.write_findings(
        state,
        lambda _: [
            model.FindingEvent("X-F1", "bank", "OPEN"),
            model.FindingEvent("X-F2", "bank", "OPEN"),
            model.FindingEvent("X-F2", "route", "ROUTED", "B"),
        ],
    )
    sent = {"to": "A", "message": "Fix F1.\nRows: X-F2, X-F1", "summary": "fix", "priority": "high"}
    verdict, updated = decision(hook("PreToolUse", project, tool_name="SendMessage", tool_input=sent)[1])
    assert verdict == "allow"
    second = report.path(state, "A", 2)
    told = f"Fix F1.\nRows: X-F2, X-F1\n\nThis is round 2: write its report to {second}, titled `round 2`."
    assert updated == sent | {"message": told}
    agent = model.read_agent(state, "A")
    assert (agent.status, agent.reports) == ("running", {1: "reports/A.md", 2: "reports/A.2.md"})
    code, said = hook("PostToolUse", project, tool_name="SendMessage", tool_input=sent, tool_response={})
    assert code == 0
    # A row a running agent holds stays with it, and keeps no other row from the addressee.
    assert context_of(said, "PostToolUse") == (
        "Not routed to A: X-F2 is routed to B, which is running: its rows stay with it until it reports"
    )
    assert {row: event.owner for row, event in model.read_findings(state).items()} == {"X-F1": "A", "X-F2": "B"}
    kept = (state / "briefs" / "A.messages.md").read_bytes().decode()
    assert kept.endswith(" coordinator\n\nFix F1.\nRows: X-F2, X-F1\n")


def test_a_resume_waits_for_the_report_to_be_judged_and_any_later_status_starts_the_next_round(
    project: Path, state: Path
) -> None:
    """`judge` reads an agent's latest round alone, so a round begun first would leave the report unread.

    A landed or discarded agent sent back, to fix its routed rows, writes a new report beside the judged one.
    """
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    changed(state, "A", status="reported", outcome="DONE")
    sent = {"to": "A", "message": "Fix F1."}
    said = hook("PreToolUse", project, tool_name="SendMessage", tool_input=sent)[1]
    assert decision(said) == ("deny", findings.unjudged(model.read_agent(state, "A")))
    assert "`orchestra judge A`" in str(decision(said)[1])
    assert model.read_agent(state, "A").status == "reported"
    told = f"Fix F1.\n\nThis is round 2: write its report to {report.path(state, 'A', 2)}, titled `round 2`."
    for status in ("landed", "pruned"):
        changed(state, "A", status=status, reports={1: "reports/A.md"})
        said = hook("PreToolUse", project, tool_name="SendMessage", tool_input=sent)[1]
        assert decision(said) == ("allow", sent | {"message": told}), status


def test_an_agent_answered_after_it_stopped_on_a_question_completes_the_same_report(project: Path, state: Path) -> None:
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    changed(state, "A", status="reported", outcome="NEEDS-ANSWER")
    sent = {"to": "A", "message": "The second file."}
    updated = decision(hook("PreToolUse", project, tool_name="SendMessage", tool_input=sent)[1])[1]
    told = f"The second file.\n\nThis continues round 1: complete your report at {report.path(state, 'A')}."
    assert updated == sent | {"message": told}
    agent = model.read_agent(state, "A")
    assert (agent.status, agent.reports) == ("running", {1: "reports/A.md"})


def test_a_resume_sent_again_is_told_its_round_again_and_a_message_to_an_agent_at_work_is_not(
    project: Path, state: Path
) -> None:
    """A send this hook let through can fail to land, another hook denying it, and leave the record `running`."""
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    changed(state, "A", status="judged", outcome="DONE")
    sent = {"to": "A", "message": "Fix F1."}
    told = f"Fix F1.\n\nThis is round 2: write its report to {report.path(state, 'A', 2)}, titled `round 2`."
    for _ in range(2):
        said = hook("PreToolUse", project, tool_name="SendMessage", tool_input=sent)[1]
        assert decision(said) == ("allow", sent | {"message": told})
    assert model.read_agent(state, "A").reports == {1: "reports/A.md", 2: "reports/A.2.md"}
    # A turn that ended, under the `running` record the first send left, is no turn at work.
    transcript(project.parent / "claude-config", "a1", line("user"), line("assistant", "m1"))
    said = hook("PreToolUse", project, tool_name="SendMessage", tool_input=sent)[1]
    assert decision(said) == ("allow", sent | {"message": told})
    transcript(project.parent / "claude-config", "a1", line("user"), line("assistant", "m1", calls=True))
    assert hook("PreToolUse", project, tool_name="SendMessage", tool_input=sent) == (0, None)


# --- an agent's stop ------------------------------------------------------------------------------


def test_a_stop_is_refused_while_the_report_breaks_its_grammar_then_let_through_stopped(
    project: Path, state: Path
) -> None:
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    fields = inside(project, "a1", "auditor", last_assistant_message="Done.", stop_hook_active=False)
    for _ in range(2):
        code, said = hook("SubagentStop", project, fields)
        assert code == 0
        assert field(said, "decision") == "block"
        assert f"no report at {report.path(state, 'A')}" in str(field(said, "reason"))
    assert hook("SubagentStop", project, fields) == (0, None)
    agent = model.read_agent(state, "A")
    assert agent.status == "stopped"
    assert str(agent.reason).startswith("its report was refused 2 times")


def test_a_second_report_with_no_resume_between_makes_an_item_queued_for_it_due(project: Path, state: Path) -> None:
    """An agent resumed by typing into its transcript reports again from `reported`, so its stamp must move."""
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    report.path(state, "A").write_bytes(text("auditor", "A").encode())
    changed(state, "A", status="reported", outcome="DONE")
    earlier = (datetime.datetime.now().astimezone() - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
    model.update_agent(state, "A", lambda held: dataclasses.replace(held, since={**held.since, "reported": earlier}))
    (item,) = model.write_queue(state, lambda _: [model.QueueItem(1, "reported", "judge the next report", "A")])
    assert queue.due(state) == []
    assert hook("SubagentStop", project, inside(project, "a1", "auditor", last_assistant_message="x")) == (0, None)
    assert queue.due(state) == [item]


def test_a_stop_after_its_record_was_judged_or_pruned_keeps_that_status_and_says_so(project: Path, state: Path) -> None:
    """A second judge of a round is refused, so `reported` or `stopped` over it would hold the agent back for good."""
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    report.path(state, "A").write_bytes(text("auditor", "A").encode())
    changed(state, "A", status="judged")
    (item,) = model.write_queue(state, lambda _: [model.QueueItem(1, "reported", "read the late findings", "A")])
    assert hook("SubagentStop", project, inside(project, "a1", "auditor", last_assistant_message="x")) == (0, None)
    assert model.read_agent(state, "A").status == "judged"
    said = "A round 1: DONE, after its record was judged, which it stays"
    assert log.entries(state)[-1].text == f"{said}: read the report for findings written since"
    # Stamped all the same, so an item waiting on its report falls due.
    assert queue.due(state) == [item]
    # A limit ending it keeps it too, and a discarded record is named as one, never as judged.
    changed(state, "A", status="pruned", reason=model.DISCARDED)
    limit = inside(project, "a1", "auditor", last_assistant_message="You've hit your session limit · resets 4pm")
    assert hook("SubagentStop", project, limit) == (0, None)
    assert model.read_agent(state, "A").status == "pruned"
    assert log.entries(state)[-1].text == (
        "A: You've hit your session limit · resets 4pm, after its record was discarded, which it stays:"
        " read the report for findings written since"
    )


def test_an_agent_ended_by_a_limit_is_stopped_never_asked_to_repair(project: Path, state: Path) -> None:
    ended = {"A": "You've hit your session limit · resets 4pm", "B": "API Error: 529 Overloaded"}
    for k, (name, said) in enumerate(ended.items(), 1):
        briefed(project, name, "auditor")
        dispatched(project, name, "auditor", f"a{k}")
        fields = inside(project, f"a{k}", "auditor", last_assistant_message=said)
        assert hook("SubagentStop", project, fields) == (0, None)
        agent = model.read_agent(state, name)
        assert (agent.status, agent.reason) == ("stopped", said)


def test_a_stop_failure_marks_its_agent_stopped(project: Path, state: Path) -> None:
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    fields = inside(project, "a1", "auditor", error="rate_limit", error_details="429 Too Many Requests")
    assert hook("StopFailure", project, fields) == (0, None)
    agent = model.read_agent(state, "A")
    assert (agent.status, agent.reason) == ("stopped", "rate_limit: 429 Too Many Requests")


# --- the coordinator's turn -----------------------------------------------------------------------


def test_the_turn_continues_once_with_what_is_undone(project: Path, state: Path) -> None:
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    changed(state, "A", status="reported", outcome="DONE")
    # Its record reads `running`, and no transcript says it works: a stop hook that never ran leaves this.
    briefed(project, "B", "auditor")
    dispatched(project, "B", "auditor", "b1")
    # A usage limit stopped it, and `status` lists it as to resume.
    briefed(project, "C", "auditor")
    dispatched(project, "C", "auditor", "c1")
    changed(state, "C", status="stopped", reason="usage limit, resets 18:00")
    # Five entries from a fixed second on, and NOW.md written within the first: the log's stamps hold the
    # second alone, so the entry of NOW.md's own second counts as later.
    start = datetime.datetime.now().astimezone().replace(microsecond=0) + datetime.timedelta(hours=1)
    stamps = [start + datetime.timedelta(seconds=k) for k in range(5)]
    store.append_jsonl(state / store.LOG, *({"time": t.isoformat(), "kind": "resume", "text": "x"} for t in stamps))
    written = start.timestamp() + 0.5
    os.utime(state / store.NOW, (written, written))
    code, said = hook("Stop", project, stop_hook_active=False, background_tasks=[])
    assert code == 0
    assert context_of(said, "Stop").splitlines() == [
        "Undone in this orchestra session:",
        "- A reported (DONE) and is not judged",
        "- C stopped (usage limit, resets 18:00) and is not resumed",
        "- NOW.md predates the last 5 log entries",
    ]
    assert hook("Stop", project, stop_hook_active=True, background_tasks=[]) == (0, None)


def test_a_writer_stopped_on_a_question_whose_worktree_is_gone_is_judged_and_leaves_the_turns_end(
    project: Path, state: Path
) -> None:
    """Claude Code removes a commit-less writer's worktree when it stops (worktrees.md), so no answer reaches it."""
    briefed(project, "W", "writer", owns="lib/a.py")
    dispatched(project, "W", "writer", "w1")
    asked = text("writer", "W", status="NEEDS-ANSWER: which queue takes it?").replace("Commits: 2,", "Commits: 0,")
    report.path(state, "W").write_bytes(asked.encode())
    changed(state, "W", status="reported", outcome="NEEDS-ANSWER", branch="worktree-agent-w1")

    def undone() -> list[str]:
        said = hook("Stop", project, stop_hook_active=False, background_tasks=[])[1]
        return [] if said is None else context_of(said, "Stop").splitlines()

    assert undone() == [
        "Undone in this orchestra session:",
        "- W stopped NEEDS-ANSWER, and its worktree is gone (worktree-agent-w1), so no answer reaches it:"
        " `orchestra judge W` banks its report as it stands",
    ]
    # A discard waits for the judge too, since a pruned agent's report can no longer be judged.
    kept = run("prune", "--discard", "W", cwd=project)
    assert "kept its record (W): W stopped NEEDS-ANSWER, and its worktree is gone" in kept.stdout, kept.stdout
    judged = run("judge", "W", "F1=route:owner", "F2=route:owner", "--verdict", "superseded by W2", cwd=project)
    assert judged.returncode == 0, judged.stderr
    assert model.read_agent(state, "W").status == "judged"
    assert undone() == []


def test_a_turns_end_lists_the_worktrees_once_for_every_report_stopped_on_a_question(
    project: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each such report asks whether its worktree is gone, and the turn's end waits on every git run."""
    ran: list[tuple[str, ...]] = []
    started = process.run

    def counted(argv: Sequence[str], **given: Any) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "git":
            ran.append(tuple(argv))
        return started(argv, **given)

    def runs() -> int:
        """The git runs of one turn's end, once its lines are read."""
        ran.clear()
        said = context_of(hook("Stop", project, stop_hook_active=False, background_tasks=[])[1], "Stop")
        assert said.count("so no answer reaches it") == len(model.agents(state)), said
        return len(ran)

    def asked(name: str, ident: str) -> None:
        briefed(project, name, "writer", owns="lib/a.py")
        dispatched(project, name, "writer", ident)
        changed(state, name, status="reported", outcome="NEEDS-ANSWER", branch=f"worktree-agent-{ident}")

    monkeypatch.setattr(process, "run", counted)
    asked("W", "w1")
    one = runs()
    asked("X", "x1")
    asked("Y", "y1")
    assert runs() == one, ran


def test_the_root_comes_from_session_json_where_exact_and_a_git_fault_drops_no_dispatch(
    project: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every prompt and post-call event read the primary checkout, which two git runs found, though the state names it.

    The recorded checkout is the root where its `.git` folder is the common directory holding the branch's
    pointer; any other, a moved checkout among them, asks git. A dispatch reads the root only to route its rows,
    so a git fault leaves its record written and names the rows left unrouted.
    """
    banked = [model.FindingEvent("X-F1", "bank", "OPEN"), model.FindingEvent("X-F2", "bank", "OPEN")]
    model.write_findings(state, lambda _: banked)
    briefed(project, "A", "auditor", rows="X-F1")
    briefed(project, "B", "auditor", rows="X-F2")
    main = project.parent / "main.jsonl"
    main.write_bytes(line("assistant", "m1", 400_000))
    near = "Your context is 400,474 tokens, at least 75% of the 500,000-token compaction window"
    ran: list[tuple[str, ...]] = []
    started = process.run
    broken: list[bool] = []

    def counted(argv: Sequence[str], **given: Any) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "git":
            ran.append(tuple(argv))
            if broken:
                return subprocess.CompletedProcess(argv, 128, b"", b"fatal: not a git repository")
        return started(argv, **given)

    def prompted() -> str:
        said = hook("UserPromptSubmit", project, prompt="go", transcript_path=str(main))[1]
        return context_of(said, "UserPromptSubmit")

    monkeypatch.setattr(process, "run", counted)
    assert near in prompted()
    assert dispatched(project, "A", "auditor", "a1") is None
    assert model.read_findings(state)["X-F1"].owner == "A"
    assert ran == []
    # A checkout recorded elsewhere, as after a move, is no proof: git finds the root, and it is the same.
    facts = model.read_session(state)
    model.write_session(state, dataclasses.replace(facts, checkout=str(project.parent / "moved")))
    assert near in prompted()
    assert [argv[3] for argv in ran] == ["rev-parse", "worktree"]
    # With git failing, the dispatch is recorded all the same, and its rows wait for `orchestra route`.
    broken.append(True)
    said = dispatched(project, "B", "auditor", "b1")
    assert context_of(said, "PostToolUse") == (
        "Not routed to B: git cannot find the primary checkout; `orchestra route B X-F2` routes them"
    )
    agent = model.read_agent(state, "B")
    assert (agent.ids, agent.status) == (("b1",), "running")
    assert model.read_findings(state)["X-F2"].status == "OPEN"


def test_a_writer_stopped_on_a_question_whose_worktree_stands_waits_on_its_answer(project: Path, state: Path) -> None:
    """Its answer resumes it in its worktree, so `judge` waits for the report that answer completes."""
    briefed(project, "W", "writer", owns="lib/a.py")
    dispatched(project, "W", "writer", "w1")
    git(project, "worktree", "add", "-b", "worktree-agent-w1", str(project.parent / "trees" / "agent-w1"))
    report.path(state, "W").write_bytes(text("writer", "W", status="NEEDS-ANSWER: which queue takes it?").encode())
    changed(state, "W", status="reported", outcome="NEEDS-ANSWER", branch="worktree-agent-w1")
    said = context_of(hook("Stop", project, stop_hook_active=False, background_tasks=[])[1], "Stop")
    assert "- W stopped NEEDS-ANSWER and waits on your answer by message" in said.splitlines()
    judged = run("judge", "W", "F1=route:owner", "F2=route:owner", "--verdict", "v", cwd=project)
    assert (judged.returncode, judged.stderr) == (
        2,
        "orchestra judge: W stopped NEEDS-ANSWER: which queue takes it?. Answer it by message, and judge the report"
        " it writes\n",
    )


def test_notes_after_now_md_leave_it_current_and_as_many_decisions_or_other_entries_do_not(
    project: Path, state: Path
) -> None:
    """A note logs a fault or an observation and moves no next action; a waiting note names what is waited on."""
    start = datetime.datetime.now().astimezone().replace(microsecond=0) + datetime.timedelta(hours=1)
    os.utime(state / store.NOW, (start.timestamp(), start.timestamp()))

    def logged(*entries: tuple[str, str]) -> list[str]:
        """The turn's `NOW.md` line once `entries`, each a kind and a text, are logged after NOW.md, a second apart."""
        stamps = [start + datetime.timedelta(seconds=k) for k in range(len(entries))]
        store.write_text(state / store.LOG, "")
        store.append_jsonl(
            state / store.LOG,
            *(
                {"time": t.isoformat(), "kind": kind, "text": text}
                for t, (kind, text) in zip(stamps, entries, strict=True)
            ),
        )
        said = hook("Stop", project, stop_hook_active=False, background_tasks=[])[1]
        return [] if said is None else [line for line in context_of(said, "Stop").splitlines() if "NOW.md" in line]

    stale = ["- NOW.md predates the last 5 log entries"]
    assert logged(*[("note", "x")] * 8) == []
    others = [(kind, "x") for kind in ("waiting", "judge", "land", "ci")]
    assert logged(*others, ("resume", "x")) == stale
    # A decision taken while nobody could be asked moves the next action, so its note counts.
    assert logged(*others, ("note", "Decision: kept the cap at 20; reversing costs one commit")) == stale


def test_an_idle_turn_continues_only_while_nobody_watches(project: Path, state: Path) -> None:
    """Attended, the owner sees the turn end; away, a turn ending with nothing running would stall unseen."""
    assert hook("Stop", project, stop_hook_active=False, background_tasks=[]) == (0, None)
    store.write_json(state / store.AWAY, {"since": model.now(), "until": None, "log": 0, "files": []})
    code, said = hook("Stop", project, stop_hook_active=False, background_tasks=[])
    assert code == 0
    assert context_of(said, "Stop").splitlines() == [
        "Undone in this orchestra session:",
        '- nothing runs, and no `orchestra note --waiting "<what>"` names what the session waits on',
    ]
    briefed(project, "A", "auditor")
    assert run("note", "--waiting", "the owner's answer on the cap", cwd=project).returncode == 0
    assert hook("Stop", project, stop_hook_active=False, background_tasks=[]) == (0, None)
    # An agent's later change of status makes the note stale: what the session waits on may have moved.
    later = (datetime.datetime.now().astimezone() + datetime.timedelta(hours=1)).isoformat(timespec="seconds")
    changed(state, "A", status="stopped", reason="usage limit")
    # Stamped past the note's second, which the log's stamps hold alone.
    changed(state, "A", since={"stopped": later})
    said = context_of(hook("Stop", project, stop_hook_active=False, background_tasks=[])[1], "Stop")
    assert '- nothing runs, and no `orchestra note --waiting "<what>"` names what the session waits on' in said


def test_an_attended_turns_end_reads_no_transcript(project: Path, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whether an agent works matters only while nobody watches, and each read costs the owner's turn its wait."""
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")

    def refused(*_: object) -> None:
        raise AssertionError("a transcript was read")

    with monkeypatch.context() as patched:
        patched.setattr(context, "working", refused)
        patched.setattr(context, "transcript", refused)
        # Quiet: a read would fault the hook, which says so on stderr.
        code, said = hook("Stop", project, stop_hook_active=False, background_tasks=[])
    assert code == 0
    assert "nothing runs" not in str(said)
    # Away, the same turn's end reads A's transcript, finds none, and names the stall as before.
    store.write_json(state / store.AWAY, {"since": model.now(), "until": None, "log": 0, "files": []})
    said = context_of(hook("Stop", project, stop_hook_active=False, background_tasks=[])[1], "Stop")
    assert '- nothing runs, and no `orchestra note --waiting "<what>"` names what the session waits on' in said


def test_a_turns_end_hands_the_queue_what_it_read(project: Path, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The turn-end hook holds the records and the log, so the queue reading them again only adds to the wait."""
    briefed(project, "A", "auditor")
    model.write_queue(state, lambda _: [model.QueueItem(1, "reported", "judge the next report", "A")])
    given: list[tuple[object, object]] = []
    due = queue.due

    def seen(where: Path, agents: object = None, entries: object = None) -> list[model.QueueItem]:
        given.append((agents, entries))
        return due(where, agents, entries)  # type: ignore[arg-type]

    with monkeypatch.context() as patched:
        patched.setattr(queue, "due", seen)
        code, _ = hook("Stop", project, stop_hook_active=False, background_tasks=[])
    assert code == 0
    assert given
    assert all(agents is not None for agents, _ in given)
    assert all(entries is not None for _, entries in given)


# --- the away mode --------------------------------------------------------------------------------

BASH: dict[str, object] = {
    "tool_name": "Bash",
    "tool_input": {"command": "npm  test\n--watch", "description": "Run the tests"},
}
QUESTION: dict[str, object] = {
    "tool_name": "AskUserQuestion",
    "tool_input": {"questions": [{"question": "Keep it?", "options": []}]},
}
PLAN_EXIT: dict[str, object] = {"tool_name": "ExitPlanMode", "tool_input": {}}
# A background agent of the coordinator that is none of the plugin's: its events carry the main session's id.
HELPER: dict[str, object] = {"agent_id": "b1", "agent_type": "general-purpose"}


def prompt_refusal(said: object) -> tuple[object, object]:
    """A `PermissionRequest` answer's behaviour and message."""
    assert field(said, "hookSpecificOutput", "hookEventName") == "PermissionRequest"
    held = field(said, "hookSpecificOutput", "decision")
    return field(held, "behavior"), field(held, "message")


def test_an_away_session_refuses_its_own_prompts_and_questions_and_records_each(project: Path, state: Path) -> None:
    """The coordinator's calls and its agents' are refused; every other session in the checkout prompts as before."""
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    calls = (BASH, QUESTION, PLAN_EXIT)
    # Attended, every prompt and question reaches the owner.
    assert hook("PermissionRequest", project, BASH) == (0, None)
    assert [hook("PreToolUse", project, call) for call in calls[1:]] == [(0, None)] * 2
    assert run("mode", "away", "07:30", cwd=project).returncode == 0
    for parts in ((), (HELPER,), (inside(project, "a1", "auditor"),)):
        code, said = hook("PermissionRequest", project, *parts, BASH)
        assert code == 0
        assert prompt_refusal(said) == ("deny", events.AWAY_PROMPT)
    for call in calls[1:]:
        assert decision(hook("PreToolUse", project, HELPER, call)[1]) == ("deny", events.AWAY_QUESTION)
    reason = "[Irreversible Local Destruction]"
    assert hook("PermissionDenied", project, HELPER, BASH, reason=reason, permission_mode="auto") == (0, None)
    # Another session in the same checkout, and its own background agent, are not away.
    assert hook("PermissionRequest", project, BASH, session_id=OTHER, process=OTHER) == (0, None)
    assert hook("PreToolUse", project, HELPER, QUESTION, session_id=OTHER, process=OTHER) == (0, None)
    assert hook("PermissionDenied", project, BASH, reason=reason, session_id=OTHER, process=OTHER) == (0, None)
    held = store.read_jsonl(state / events.REFUSED)
    assert [(entry["event"], entry["tool"], entry["agent_type"]) for entry in held] == [
        ("PermissionRequest", "Bash", None),
        ("PermissionRequest", "Bash", "general-purpose"),
        ("PermissionRequest", "Bash", "orchestra:auditor"),
        ("PreToolUse", "AskUserQuestion", "general-purpose"),
        ("PreToolUse", "ExitPlanMode", "general-purpose"),
        ("PermissionDenied", "Bash", "general-purpose"),
    ]
    assert held[1]["agent_id"] == "b1"
    # The first text value, on one line; a call holding none is summarised by its JSON.
    summaries = [entry["input"] for entry in held[::3]]
    assert summaries == ["npm test --watch", '{"questions": [{"question": "Keep it?", "options": []}]}']
    assert held[-1]["reason"] == reason
    back = run("mode", "attended", cwd=project)
    assert back.returncode == 0, back.stderr
    # Each call once, with how often and by whom, so the owner sees which allow rule the night lacked.
    refused = [line for line in back.stdout.splitlines() if line.startswith("refused ")]
    assert len(refused) == 3
    (bash,) = [line for line in refused if line.startswith("refused   4x Bash: npm test --watch ")]
    assert "(general-purpose, orchestra:auditor, the coordinator; last at " in bash
    assert bash.endswith(f"; auto mode: {reason})")
    assert back.stdout.splitlines()[-1].endswith(", 6 call(s) refused, 0 question(s) open")
    assert hook("PermissionRequest", project, BASH) == (0, None)


def test_an_agents_prompt_is_refused_away_after_the_lease_moves(project: Path, state: Path) -> None:
    """An agent outlives its coordinator's `/clear` or a takeover, and nobody is there for its prompt."""
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    assert run("mode", "away", "07:30", cwd=project).returncode == 0
    lease = {"owner": OTHER, "since": model.now(), "stopped": False}
    (state / store.LEASE).write_bytes(json.dumps(lease).encode())
    said = hook("PermissionRequest", project, inside(project, "a1", "auditor"), BASH)[1]
    assert prompt_refusal(said) == ("deny", events.AWAY_PROMPT)
    # The main session no longer holds the state, so it is not the away session's.
    assert hook("PermissionRequest", project, BASH) == (0, None)


def test_a_hook_that_cannot_judge_logs_its_fault_and_an_away_session_still_refuses(project: Path, state: Path) -> None:
    """A fault's stderr is shown on no exit 0, and a prompt let through while the owner is away waits for nobody."""
    # A record an older or newer build wrote faults every event inside an agent, which reads them all.
    (state / "agents" / "Z.json").write_bytes(b'{"name": "Z", "type": "auditor", "laterField": 1}\n')
    # Attended, a prompt is the owner's, so its event never reads the state; any other call goes ahead.
    assert hook("PermissionRequest", project, HELPER, BASH) == (0, None)
    code, said = hook("PreToolUse", project, HELPER, edit(project / "lib" / "a.py"), quiet=False)
    assert code == 0
    shown = str(field(said, "systemMessage"))
    assert shown.startswith("orchestra's PreToolUse hook failed: ")
    assert shown.endswith("; `orchestra log` lists it")
    assert run("mode", "away", "07:30", cwd=project).returncode == 0
    code, said = hook("PermissionRequest", project, HELPER, BASH, quiet=False)
    assert code == 0
    assert prompt_refusal(said) == ("deny", events.AWAY_PROMPT)
    for call in (QUESTION, PLAN_EXIT):
        assert decision(hook("PreToolUse", project, HELPER, call, quiet=False)[1]) == ("deny", events.AWAY_QUESTION)
    faults = [entry.text for entry in log.entries(state) if entry.kind == "hook"]
    assert faults[0].startswith(f"PreToolUse (Edit) failed: {state / 'agents' / 'Z.json'} ")
    assert [text.split(" failed: ")[0] for text in faults] == [
        "PreToolUse (Edit)",
        "PermissionRequest (Bash)",
        "PreToolUse (AskUserQuestion)",
        "PreToolUse (ExitPlanMode)",
    ]
    assert run("mode", "attended", cwd=project).returncode == 0
    before = (state / store.LOG).read_bytes()
    # Another session's event is nobody's: it writes and shows nothing, as it gets no answer when judged.
    stop: dict[str, object] = {"stop_hook_active": False}
    assert hook("Stop", project, HELPER, stop, session_id=OTHER, process=OTHER, quiet=False) == (0, None)
    assert (state / store.LOG).read_bytes() == before
    # A plugin agent's fault is a fact logged past the lease, so it claims none.
    (state / store.LEASE).unlink()
    inner = inside(project, "z1", "auditor", tool_name="Edit", tool_input={"file_path": "x"})
    assert decision(hook("PreToolUse", project, inner, process=OTHER, quiet=False)[1])[0] == "deny"
    assert (state / store.LOG).read_bytes() != before
    assert not (state / store.LEASE).exists()


PACED_PLAN = """# Plan

## Slices and their files

- [ ] S1 `lib/a.py`
- [ ] S2 `docs/b.md`

## Landing order

- [ ] S1 before S2
"""


def _due(project: Path) -> list[str]:
    """The checkpoint's line among the turn's undone lines, if any."""
    code, said = hook("Stop", project, stop_hook_active=False, background_tasks=[])
    assert code == 0
    return [] if said is None else [line for line in context_of(said, "Stop").splitlines() if "checkpoint" in line]


def _judged(project: Path, name: str, kind: str, lens: str = "none") -> subprocess.CompletedProcess[str]:
    """An agent briefed on `lens`, dispatched as its name in lower case, reported and judged, its findings closed."""
    ident = name.lower()
    briefed(project, name, kind, lens=lens)
    dispatched(project, name, kind, ident)
    report.path(store.state_dir(project), name).write_bytes(text(kind, name).encode())
    assert hook("SubagentStop", project, inside(project, ident, kind, last_assistant_message="x"))[0] == 0
    judged = run("judge", name, "F1=close:MOOT:x", "F2=close:MOOT:x", "--verdict", "v", cwd=project)
    assert judged.returncode == 0, judged.stderr
    return judged


def test_a_falsely_sequenced_slice_waits_free_and_judged_reports_not_disclosure_only_make_the_checkpoint_due(
    project: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 needs nothing of S1, so the program names it free; the estimate is looked at again every `JUDGED` reports.

    A researcher whose `Lens:` names disclosure alone frees no planned work, before a push or in the review, in
    any round; a report whose brief cannot be read, or whose `Lens:` names another lens beside, counts. The count
    is lowered for the case, since each report it counts costs a brief, a dispatch, a stop and a judge.
    """
    monkeypatch.setattr(events, "JUDGED", 3)
    count = events.JUDGED
    store.write_text(state / store.PLAN, PACED_PLAN)
    briefed(project, "W", "writer", owns="lib/a.py")
    free = "waits     slice S2, which no agent took: its files are free"
    assert _due(project) == []
    # A researcher settling the plan's premises goes out before the first estimate (start.md, step 5).
    briefed(project, "MAP", "researcher")
    dispatched(project, "MAP", "researcher", "map")
    assert _due(project) == []
    dispatched(project, "W", "writer", "w1")
    assert free in run("status", "--measure", cwd=project).stdout.splitlines()
    first = "- a writer has been dispatched, and no estimate is saved, and 3 plan lines are unticked"
    assert _due(project) == [f"{first}: the checkpoint in checkpoint.md is due"]
    # Once per turn: a turn the hook already continued is let end.
    assert hook("Stop", project, stop_hook_active=True, background_tasks=[]) == (0, None)
    saved = state / "estimates" / "first.md"
    saved.write_bytes(b"# Estimate\n")
    assert _due(project) == []
    # A disclosure-only read's first round, and its second once resumed for the next push: neither counts.
    _judged(project, "DISCLOSE", "researcher", lens="disclosure")
    resume = {"to": "DISCLOSE", "message": "Read the next push."}
    assert decision(hook("PreToolUse", project, tool_name="SendMessage", tool_input=resume)[1])[0] == "allow"
    report.path(state, "DISCLOSE", 2).write_bytes(text("researcher", "DISCLOSE", round_=2).encode())
    stop = inside(project, "disclose", "researcher", last_assistant_message="x")
    assert hook("SubagentStop", project, stop)[0] == 0
    second = run("judge", "DISCLOSE", "F1=close:MOOT:x", "F2=close:MOOT:x", "--verdict", "v", cwd=project)
    assert second.returncode == 0, second.stderr
    at = datetime.datetime.fromtimestamp(saved.stat().st_mtime).astimezone()
    last = f"- {count} reports were judged since the last saved estimate ({at:%H:%M}), and 3 plan lines are unticked"
    # W and MAP stay at work throughout, so no wave ends: the count alone makes the checkpoint due.
    for k in range(1, count + 1):
        # A lens beside disclosure makes the read more than a disclosure-only one, so its report counts.
        judged = _judged(project, f"A{k}", "auditor", lens="disclosure, contract" if k == count else "none")
        assert free in judged.stdout.splitlines()
        if k == 1:
            # A brief that cannot be read is counted, as the report of any other agent is.
            (state / "briefs" / "A1.md").unlink()
        assert _due(project) == ([] if k < count else [f"{last}: the checkpoint in checkpoint.md is due"])
    # A landing closes on what still waits.
    tree = project.parent / "trees" / "agent-w1"
    git(project, "worktree", "add", "-b", "worktree-agent-w1", str(tree))
    (tree / "lib" / "a.py").write_bytes(b"y\n")
    git(tree, "commit", "-am", "Lib: The module")
    changed(state, "W", status="judged", branch="worktree-agent-w1")
    landed = run("land", "W", cwd=project)
    assert landed.returncode == 0, landed.stderr
    assert free in landed.stdout.splitlines()


def test_the_checkpoint_is_due_at_a_waves_end_once_no_agent_is_live_and_a_writer_has_landed(
    project: Path, state: Path
) -> None:
    """checkpoint.md runs at each wave's end; a landing while another agent works ends none.

    A brief drafted and not dispatched holds none open, since review.md drafts the review's while the last writers run.
    """
    store.write_text(state / store.PLAN, PACED_PLAN)
    briefed(project, "W", "writer", owns="lib/a.py")
    dispatched(project, "W", "writer", "w1")
    briefed(project, "R", "auditor")
    dispatched(project, "R", "auditor", "r1")
    saved = state / "estimates" / "first.md"
    saved.write_bytes(b"# Estimate\n")
    at = datetime.datetime.fromtimestamp(saved.stat().st_mtime).astimezone()
    assert _due(project) == []
    changed(state, "W", status="landed")
    assert _due(project) == []
    changed(state, "R", status="judged")
    briefed(project, "N", "auditor")
    ended = f"- no agent is live, and 1 writer(s) landed since the last saved estimate ({at:%H:%M})"
    assert _due(project) == [f"{ended}, and 3 plan lines are unticked: the checkpoint in checkpoint.md is due"]
    # An estimate saved after the landing starts the next wave's count.
    newer = state / "estimates" / "second.md"
    newer.write_bytes(b"# Estimate\n")
    later = time.time() + 2
    os.utime(newer, (later, later))
    assert _due(project) == []


def test_a_compaction_or_a_resume_reads_the_state_back_from_disk(project: Path, state: Path) -> None:
    briefed(project, "A", "auditor")
    dispatched(project, "A", "auditor", "a1")
    code, said = hook("SessionStart", project, source="compact")
    assert code == 0
    context = context_of(said, "SessionStart")
    assert context.startswith(f"This session coordinates the orchestra session whose state is {state}.")
    # No summary need keep anything: the state's path, the branch and each running agent come back from disk.
    assert f"\nsession project/s1 on main, state {state}\n" in context
    # A record reading `running` is listed whatever its transcript says; this one has none.
    assert "\nsuspect   A (auditor, round 1): running, but no transcript found for a1\n" in context
    assert "\nstatus: 1 running" in context
    assert context.endswith("\nNOW.md:\n" + (state / "NOW.md").read_bytes().decode())
    # NOW.md follows whole, so status's `now` lines would give it twice.
    assert not any(line.startswith("now ") for line in context.splitlines())


def test_after_a_compaction_now_md_takes_the_room_first_and_every_cut_is_named(
    project: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past `CONTEXT_CHARS` Claude Code keeps a preview and asks nobody to read the rest, so nothing is cut unseen.

    NOW.md holds the next action and its reason, which the state does not, so the overview's rows give way to it;
    a NOW.md longer than the room keeps its head and names the file that holds it whole.
    """
    rows = [f"row {k:02} " + "x" * 90 for k in range(60)]

    def overview(*_: object) -> tuple[list[str], str]:
        return rows, "status: 60 rows"

    # `events` imports the status view at the call, so the module's own attribute is the one it reads.
    monkeypatch.setattr("orchestra.status.coordinator", overview)
    now = state / store.NOW

    def started() -> str:
        # The hook's last cut, at `CONTEXT_CHARS`, falls in its tail, which each case below reads whole.
        return context_of(hook("SessionStart", project, source="compact")[1], "SessionStart")

    whole = "".join(f"- line {k:03} of what comes next and why\n" for k in range(130))
    now.write_bytes(whole.encode())
    said = started()
    assert said.endswith("\nNOW.md:\n" + whole)
    lines = said.splitlines()
    (count,) = [line for line in lines if line.startswith("... ")]
    shown = [line for line in lines if line.startswith("row ")]
    assert shown == rows[: len(shown)]
    assert count == f"... {60 - len(shown)} more line(s): `orchestra status` lists them"
    # The summary stays, below the count.
    assert lines[lines.index(count) + 1] == "status: 60 rows"
    # Rows that fit the room but not a verb's forty lines are cut where `orchestra status` cuts them.
    rows[:] = [f"row {k:02}" for k in range(45)]
    lines = started().splitlines()
    assert [line for line in lines if line.startswith(("row ", "... "))] == [
        *rows[:38],
        "... 7 more line(s): `orchestra status` lists them",
    ]
    # A NOW.md past the room keeps its head, cut at a line end, and names what it left out.
    rows[:] = [f"row {k:02} " + "x" * 90 for k in range(60)]
    longer = whole * 3
    now.write_bytes(longer.encode())
    said = started()
    held = said.split("\nNOW.md:\n", 1)[1]
    head, _, cut = held.rpartition("\n")
    assert longer.startswith(head + "\n")
    assert cut == f"... {len(longer) - len(head):,} more character(s) of NOW.md left out: {now} holds it whole"
    assert "... 60 more line(s): `orchestra status` lists them" in said.splitlines()


def test_the_overview_after_a_compaction_leaves_out_judged_and_pruned_agents_and_keeps_what_waits(
    project: Path, state: Path
) -> None:
    """Nothing after a compaction acts on a finished agent; `agents` still counts it and `status` still names it."""
    for name, ident in (("RUN", "r1"), ("TOJUDGE", "t1"), ("ASKS", "q1"), ("DONE", "d1"), ("GONE", "g1")):
        briefed(project, name, "auditor")
        dispatched(project, name, "auditor", ident)
    changed(state, "TOJUDGE", status="reported", outcome="DONE")
    changed(state, "ASKS", status="reported", outcome="NEEDS-ANSWER")
    changed(state, "DONE", status="judged", outcome="DONE")
    changed(state, "GONE", status="pruned", outcome="DONE")
    lines = context_of(hook("SessionStart", project, source="compact")[1], "SessionStart").splitlines()
    assert "agents    1 running, 2 reported, 1 judged, 1 pruned" in lines
    assert "suspect   RUN (auditor, round 1): running, but no transcript found for r1" in lines
    assert "to judge  TOJUDGE (auditor, round 1, DONE)" in lines
    assert "NEEDS-ANSWER ASKS (auditor, round 1): its report waits on you" in lines
    assert not [line for line in lines if line.startswith(("judged ", "pruned "))]
    shown = run("status", cwd=project).stdout.splitlines()
    assert [line for line in shown if line.startswith(("judged ", "pruned "))] == ["judged    DONE", "pruned    GONE"]


def test_each_prompt_carries_the_clock_and_a_gap_past_half_an_hour_names_usage(project: Path, state: Path) -> None:
    said = hook("UserPromptSubmit", project, prompt="go")[1]
    first = context_of(said, "UserPromptSubmit")
    assert first.startswith("The clock reads ")
    assert "minutes" not in first
    earlier = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=47)
    (state / "prompt.json").write_bytes(json.dumps({"time": earlier.isoformat(timespec="seconds")}).encode())
    later = context_of(hook("UserPromptSubmit", project, prompt="go")[1], "UserPromptSubmit")
    assert "47 minutes have passed since the previous prompt." in later
    assert later.endswith("A usage figure read before that is stale: `orchestra status --measure` reads it again.")
    for minutes, said in ((1, "1 minute has"), (0, "Less than a minute has"), (30, "30 minutes have")):
        earlier = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes, seconds=5)
        (state / "prompt.json").write_bytes(json.dumps({"time": earlier.isoformat(timespec="seconds")}).encode())
        shown = context_of(hook("UserPromptSubmit", project, prompt="go")[1], "UserPromptSubmit")
        assert shown.endswith(f" {said} passed since the previous prompt.")
    # Past half an hour, and no sooner, a usage figure is stale.
    earlier = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=31, seconds=5)
    (state / "prompt.json").write_bytes(json.dumps({"time": earlier.isoformat(timespec="seconds")}).encode())
    shown = context_of(hook("UserPromptSubmit", project, prompt="go")[1], "UserPromptSubmit")
    assert " 31 minutes have passed since the previous prompt. A usage figure read before that is stale:" in shown


@pytest.mark.usefixtures("state")
def test_a_prompt_nearing_the_compaction_window_tells_the_coordinator_and_past_it_the_owner(project: Path) -> None:
    """From three quarters of the window the coordinator reads that its compact tool compacts at the next seam.

    Past the window, which a desktop session started from a pre-warmed process was seen skipping, the owner reads
    it too. The window is the one `init` wrote to the local settings; the transcript's last call decides, not its
    largest.
    """
    main = project.parent / "main.jsonl"
    seam = "`mcp__orchestra__compact`, where the session has it, or else the owner's `/compact`, compacts"
    seam += " at the next seam."
    main.write_bytes(line("assistant", "m0", 900_000) + line("user") + line("assistant", "m1", 500_000))
    said = hook("UserPromptSubmit", project, prompt="go", transcript_path=str(main))[1]
    assert context_of(said, "UserPromptSubmit").endswith(
        " Your context is 500,474 tokens, past the 500,000-token compaction window, which has not applied; " + seam
    )
    assert field(said, "systemMessage") == (
        "orchestra: this session's context is 500,474 tokens, past its 500,000-token compaction window, which has not"
        " applied: the coordinator compacts at its next seam where its compact tool loads, or type `/compact` then."
    )
    # 375,000 is three quarters of the window; the line adds 474 tokens to the figure given.
    main.write_bytes(line("assistant", "m0", 900_000) + line("user") + line("assistant", "m1", 374_526))
    said = hook("UserPromptSubmit", project, prompt="go", transcript_path=str(main))[1]
    assert context_of(said, "UserPromptSubmit").endswith(
        " Your context is 375,000 tokens, at least 75% of the 500,000-token compaction window; " + seam
    )
    assert "systemMessage" not in cast("dict[str, object]", said)
    main.write_bytes(line("assistant", "m0", 900_000) + line("user") + line("assistant", "m1", 374_525))
    said = hook("UserPromptSubmit", project, prompt="go", transcript_path=str(main))[1]
    assert "compaction" not in context_of(said, "UserPromptSubmit")
    assert "systemMessage" not in cast("dict[str, object]", said)
    # With no window set the session runs at Claude Code's default, a figure no file holds, so none is assumed.
    local = project / ".claude" / "settings.local.json"
    settings = json.loads(local.read_bytes())
    del settings["autoCompactWindow"]
    local.write_bytes(json.dumps(settings).encode())
    main.write_bytes(line("assistant", "m1", 900_000))
    said = hook("UserPromptSubmit", project, prompt="go", transcript_path=str(main))[1]
    assert "compaction" not in context_of(said, "UserPromptSubmit")
    assert "systemMessage" not in cast("dict[str, object]", said)


def test_a_hook_reads_each_agents_transcript_in_the_session_its_input_names(project: Path, state: Path) -> None:
    """Whether Claude Code puts the session's id in a hook's environment is not established; its input names it.

    The input's session folder holds the transcripts of the agents that session dispatched, so it decides
    though another session's folder holds a newer transcript under the same id, saying the opposite.
    """
    write_config(project, {"stateRoot": (project.parent / "state").as_posix(), "contextCeiling": 100_000})
    config_dir = project.parent / "claude-config"
    for name, ident in (("A", "a1"), ("B", "b1")):
        briefed(project, name, "auditor")
        dispatched(project, name, "auditor", ident)
    transcript(config_dir, "a1", line("user"), line("assistant", "m1", calls=True), session=SID, minutes=5.5)
    transcript(config_dir, "a1", line("assistant", "m1"), session="elsewhere")
    transcript(config_dir, "b1", line("assistant", "m1", 150_000), session=SID, minutes=5)
    transcript(config_dir, "b1", line("assistant", "m1", 1_000), session="elsewhere")
    started = context_of(hook("SessionStart", project, source="compact", process=None)[1], "SessionStart")
    assert "\nrunning   A (auditor, round 1): transcript written 5 min ago\n" in started
    # A mid-turn agent reads a message at its next turn, so no resume is refused.
    changed(state, "A", status="reported", outcome="DONE")
    sent = {"to": "A", "message": "Fix F1."}
    assert hook("PreToolUse", project, tool_name="SendMessage", tool_input=sent, process=None) == (0, None)
    changed(state, "B", status="judged", outcome="DONE")
    sent = {"to": "B", "message": "Fix F1."}
    said = hook("PreToolUse", project, tool_name="SendMessage", tool_input=sent, process=None)[1]
    assert str(decision(said)[1]).startswith("B's context is 150,474 tokens")
    # Away, a turn's end with an agent mid-turn is no session stalled unseen.
    changed(state, "A", status="running")
    store.write_json(state / store.AWAY, {"since": model.now(), "until": None, "log": 0, "files": []})
    said = hook("Stop", project, stop_hook_active=False, background_tasks=[], process=None)[1]
    assert "nothing runs" not in str(said)


def test_a_message_routes_rows_away_from_an_agent_only_its_own_sessions_transcript_shows_gone(
    project: Path, state: Path
) -> None:
    """The route a message makes reads liveness in the input's session, as the hook's other transcript reads do."""
    config_dir = project.parent / "claude-config"
    briefed(project, "GONE", "auditor")
    dispatched(project, "GONE", "auditor", "g1")
    briefed(project, "NEXT", "auditor")
    model.write_findings(
        state,
        lambda _: [model.FindingEvent("X-F1", "bank", "OPEN"), model.FindingEvent("X-F1", "route", "ROUTED", "GONE")],
    )
    silent = context.SILENT_MINUTES + 5
    transcript(config_dir, "g1", line("assistant", "m1"), session=SID, minutes=silent)
    transcript(config_dir, "g1", line("assistant", "m1", calls=True), session="elsewhere")
    sent = {"to": "NEXT", "message": "Take this.\nRows: X-F1"}
    assert hook("PostToolUse", project, tool_name="SendMessage", tool_input=sent, process=None) == (0, None)
    assert model.read_findings(state)["X-F1"].owner == "NEXT"


def test_a_dispatch_routes_its_briefs_rows_away_from_an_agent_only_its_own_sessions_transcript_shows_gone(
    project: Path, state: Path
) -> None:
    """The route a dispatch makes reads liveness in the input's session, as a message's route does."""
    config_dir = project.parent / "claude-config"
    briefed(project, "GONE", "auditor")
    dispatched(project, "GONE", "auditor", "g1")
    model.write_findings(
        state,
        lambda _: [model.FindingEvent("X-F1", "bank", "OPEN"), model.FindingEvent("X-F1", "route", "ROUTED", "GONE")],
    )
    silent = context.SILENT_MINUTES + 5
    transcript(config_dir, "g1", line("assistant", "m1"), session=SID, minutes=silent)
    transcript(config_dir, "g1", line("assistant", "m1", calls=True), session="elsewhere")
    briefed(project, "NEXT", "auditor", rows="X-F1")
    given = {"description": "NEXT", "prompt": "Brief: x", "subagent_type": "orchestra:auditor", "name": "NEXT"}
    response = {"isAsync": True, "status": "async_launched", "agentId": "n1", "description": "NEXT"}
    said = hook("PostToolUse", project, tool_name="Agent", tool_input=given, tool_response=response, process=None)
    assert said == (0, None)
    assert model.read_findings(state)["X-F1"].owner == "NEXT"


def test_a_session_start_finds_an_agents_branch_in_the_session_its_input_names(project: Path, state: Path) -> None:
    """A record with no branch is looked up in the harness's file beside the transcript, its own session's first."""
    config_dir = project.parent / "claude-config"
    briefed(project, "C", "writer")
    dispatched(project, "C", "writer", "c1")
    changed(state, "C", status="judged", outcome="DONE", branch=None)
    git(project, "checkout", "-q", "-b", "lane-c")
    (project / "lib" / "c.py").write_bytes(b"c\n")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "c")
    git(project, "checkout", "-q", "-")
    transcript(config_dir, "c1", line("assistant", "m1"), session=SID, meta={"worktreeBranch": "lane-c"}, minutes=5)
    transcript(config_dir, "c1", line("assistant", "m1"), session="elsewhere", meta={"worktreeBranch": "no-such"})
    started = context_of(hook("SessionStart", project, source="compact", process=None)[1], "SessionStart")
    assert "\nto land   C (writer, round 1, DONE)\n" in started
