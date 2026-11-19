"""Fixture repositories, and the program run as the launcher runs it or called in this process."""

from __future__ import annotations

import contextvars
import dataclasses
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from orchestra import __main__ as entry
from orchestra import model, store

LIB = Path(__file__).resolve().parents[1] / "lib"
# What `make_repo` gives a case: a call making a repository at the folder it names.
MakeRepo = Callable[[Path], Path]

# So no case depends on the machine's git configuration.
IDENTITY = (("user.name", "fixture"), ("user.email", "fixture@example.invalid"), ("commit.gpgsign", "false"))
DOCTOR_FAILS = frozenset(
    {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"}
)


def git(root: Path, *args: str) -> str:
    done = subprocess.run(("git", "-C", str(root), *args), capture_output=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout.decode("utf-8").strip()


def _git_alone(patched: pytest.MonkeyPatch, folder: Path) -> None:
    """Every git run under `patched` reads only its repository's own configuration.

    Git for Windows' system file sets `core.autocrlf`, and a user file may set hooks, colours or an
    external diff, each of which changes the bytes a case compares. Git documents both variables in
    `git config` ("ENVIRONMENT"). With no `core.excludesFile` set, git reads the user's ignore file
    from `$XDG_CONFIG_HOME/git/ignore`, else `~/.config/git/ignore` (gitignore(5)), so that variable
    names an empty folder too.
    """
    empty = folder / "global"
    empty.write_bytes(b"")
    patched.setenv("GIT_CONFIG_NOSYSTEM", "1")
    patched.setenv("GIT_CONFIG_GLOBAL", str(empty))
    patched.setenv("XDG_CONFIG_HOME", str(folder))


@pytest.fixture(autouse=True)
def git_alone(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every git a case runs reads only its repository's own configuration, and no case sees the caller's session
    or window.
    """
    _git_alone(monkeypatch, tmp_path_factory.mktemp("git-config"))
    # A case run inside a Claude Code session would otherwise claim its state for that session.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    # The hooks find a session from the project root it names, so it would lead a case into the caller's checkout.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    # A window the contributor set would outrank every settings file `init` reads; a case wanting one sets it.
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)


# A stand-in for the Claude Code program: it prints its folder's `answer.jsonl`, and its `stderr` file to
# stderr where there is one, exits with the code in its `code` file, 0 where there is none, and appends
# its arguments to `calls.jsonl`.
CLAUDE_STUB = """import json, sys
from pathlib import Path
here = Path(__file__).parent
with (here / "calls.jsonl").open("ab") as calls:
    calls.write(json.dumps(sys.argv[1:]).encode() + b"\\n")
sys.stdout.buffer.write((here / "answer.jsonl").read_bytes())
if (here / "stderr").exists():
    sys.stderr.buffer.write((here / "stderr").read_bytes())
sys.exit(int((here / "code").read_bytes()) if (here / "code").exists() else 0)
"""
# The usage report `/usage` gives headless, as the Agent SDK's `SDKUsageReport` shapes it, in the stream around it.
USAGE_ROWS = [
    {"kind": "session", "group": "session", "percent": 12, "resets_at": "2026-11-18T03:00:00Z", "severity": "normal"},
    {"kind": "weekly_all", "group": "weekly", "percent": 61, "resets_at": "2026-11-20T08:00:00Z", "scope": None},
    {
        "kind": "weekly_scoped",
        "group": "weekly",
        "percent": 87,
        "resets_at": "2026-11-20T08:00:00Z",
        "scope": {"model": {"display_name": "Opus"}, "surface": None},
        "severity": "warning",
        "is_active": True,
    },
]


def usage_answer(limits: object = USAGE_ROWS) -> bytes:
    """The stream-json lines of a headless `/usage` run carrying `limits`; an account line it must never print."""
    report = {"session": {"total_cost_usd": 0}, "rate_limits": None if limits is None else {"limits": limits}}
    lines = [
        {"type": "system", "subtype": "init", "apiKeySource": "none", "account": "someone@example.invalid"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "someone@example.invalid"}]}}
        | {"usage_report": report},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 0},
    ]
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def claude_stub(folder: Path) -> Path:
    """The stand-in program in `folder`, run as `claude` is: a script on POSIX, a batch file on Windows."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "stub.py").write_bytes(CLAUDE_STUB.encode())
    (folder / "answer.jsonl").write_bytes(usage_answer())
    if sys.platform == "win32":
        program = folder / "claude.cmd"
        program.write_bytes(f'@"{sys.executable}" "{folder / "stub.py"}" %*\r\n'.encode())
    else:
        program = folder / "claude"
        program.write_bytes(f'#!/bin/sh\nexec "{sys.executable}" "{folder / "stub.py"}" "$@"\n'.encode())
        program.chmod(0o755)
    return program


@pytest.fixture(autouse=True)
def claude_on_path(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The stand-in first on PATH in every case, so none reaching `status --measure` starts the machine's Claude Code.

    Every case, since a case naming it is one a forgotten mark would let start the real one with the suite green.
    """
    folder = tmp_path_factory.mktemp("claude")
    claude_stub(folder)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
    return folder


@pytest.fixture(scope="session")
def repo_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A repository on `main` holding one commit, made once per run.

    A repository `git init` made names no path outside its own folder, so each copy of it is a
    repository of its own. A session `init` made is never copied: it writes absolute paths.
    """
    root = tmp_path_factory.mktemp("template") / "repo"
    root.mkdir()
    # Session-scoped, so it is made before any case's own `git_alone` applies.
    with pytest.MonkeyPatch.context() as patched:
        _git_alone(patched, tmp_path_factory.mktemp("git-config"))
        git(root, "init", "-b", "main")
        # Ignored as a project configured for the plugin ignores it, which `doctor` checks.
        (root / ".git" / "info" / "exclude").write_bytes(b".claude/worktrees/\n")
        for name, value in IDENTITY:
            git(root, "config", name, value)
        (root / "README").write_bytes(b"fixture\n")
        git(root, "add", "README")
        git(root, "commit", "-m", "start")
    return root


@pytest.fixture
def make_repo(repo_template: Path) -> MakeRepo:
    """Copies the template to a folder, beside user settings that cut worktrees from HEAD.

    `inprocess` below gives the program the folder beside the checkout as its harness's folder, and
    `doctor` fails a session whose `worktree.baseRef` is not `head`.
    """

    def made(root: Path) -> Path:
        user = root.parent / "claude-config" / "settings.json"
        if not user.exists():
            user.parent.mkdir(parents=True, exist_ok=True)
            user.write_bytes(b'{\n  "worktree": {\n    "baseRef": "head"\n  }\n}\n')
        shutil.copytree(repo_template, root)
        return root

    return made


def usage(read: int) -> dict[str, object]:
    """A call's usage as the harness records it, its nested counts included."""
    return {
        "input_tokens": 2,
        "cache_creation_input_tokens": 472,
        "cache_read_input_tokens": read,
        "output_tokens": 684,
        "output_tokens_details": {"thinking_tokens": 515},
        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 472},
        "service_tier": "standard",
    }


def line(
    role: str,
    ident: str | None = None,
    read: int | None = None,
    *,
    calls: bool = False,
    model: str = "claude-test",
    **outer: object,
) -> bytes:
    """One transcript line, the harness's own keys around a message; an assistant's that `calls` a tool."""
    message: dict[str, object] = {"role": role, "content": []}
    if role == "assistant":
        blocks: list[object] = [{"type": "text", "text": "done"}]
        blocks += [{"type": "tool_use", "name": "Bash", "input": {}}] if calls else []
        message |= {"id": ident, "model": model, "usage": usage(read or 0), "content": blocks}
    entry = {"agentId": "a1", "isSidechain": True, "sessionId": "s", "type": role, "message": message, **outer}
    return json.dumps(entry).encode() + b"\n"


def transcript(
    config: Path,
    ident: str,
    *lines: bytes,
    session: str = "session",
    meta: dict[str, object] | None = None,
    minutes: float | None = None,
) -> Path:
    """A subagent's transcript and meta file as the harness lays them out under `config`, `minutes` old."""
    path = config / "projects" / "project" / session / "subagents" / f"agent-{ident}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(lines))
    path.with_name(f"agent-{ident}.meta.json").write_bytes(
        json.dumps(meta or {"agentType": "orchestra:writer"}).encode()
    )
    if minutes is not None:
        stamp = time.time() - minutes * 60
        os.utime(path, (stamp, stamp))
    return path


def write_agent(state: Path, agent: model.Agent) -> None:
    """A record written whole, stamped in its status unless it already is, as a verb or a hook leaves one."""
    since = agent.since if agent.status in agent.since else {**agent.since, agent.status: model.now()}
    store.write_json(model.agent_path(state, agent.name), model.to_record(dataclasses.replace(agent, since=since)))


def write_config(root: Path, settings: dict[str, object]) -> None:
    path = root / ".claude" / "orchestra.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(settings).encode("utf-8"))


@pytest.fixture
def project(tmp_path: Path, make_repo: MakeRepo) -> Path:
    """A repository on `main` whose state lives under the case's own folder."""
    root = make_repo(tmp_path / "project")
    write_config(root, {"stateRoot": (tmp_path / "state").as_posix()})
    return root


def orchestra(
    *args: str,
    cwd: Path,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """The program run as `bin/orchestra` runs it, its streams decoded as utf-8; `env` adds to its environment.

    Past `timeout` seconds the program is killed and `subprocess.TimeoutExpired` raised.
    """
    # The harness running these cases may set them, and `doctor` fails on each.
    inherited = {name: value for name, value in os.environ.items() if name not in DOCTOR_FAILS}
    # A folder holding no settings, so the machine's own user settings never reach a case.
    isolated = {"CLAUDE_CONFIG_DIR": str(cwd.parent / "claude-config"), "PYTHONPATH": str(LIB)}
    env = {**inherited, **isolated, **(env or {})}
    done = subprocess.run(
        (sys.executable, "-P", "-m", "orchestra", *args),
        cwd=cwd,
        input=stdin,
        capture_output=True,
        env=env,
        check=False,
        timeout=timeout,
    )
    return subprocess.CompletedProcess(
        done.args, done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")
    )


def inprocess(
    *args: str, cwd: Path, stdin: bytes | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """`orchestra` above with the entry point called in this process, for a case about a verb, not the process.

    A process start costs three times a bare interpreter's, the package's imports included, and most cases
    pay it for every verb and hook they run.
    """
    out, err = io.BytesIO(), io.BytesIO()
    # Text streams over bytes, as a process has, so a hook's `.buffer` reads and writes reach them.
    streams = (
        io.TextIOWrapper(io.BytesIO(stdin or b""), encoding="utf-8"),
        io.TextIOWrapper(out, encoding="utf-8", write_through=True),
        io.TextIOWrapper(err, encoding="utf-8", write_through=True),
    )
    # A process prints a warning through logging's last resort, which pytest's own handlers keep from firing
    # here, so a case holding stderr empty would pass a hook that logs.
    handler = logging.StreamHandler(streams[2])
    handler.setLevel(logging.WARNING)
    logger = logging.getLogger("orchestra")
    # The whole environment is put back afterwards, since the program itself changes it too: it restores
    # the caller's own values of the variables the launcher sets.
    environ = os.environ.copy()
    with pytest.MonkeyPatch.context() as patched:
        patched.chdir(cwd)
        for name, stream in zip(("stdin", "stdout", "stderr"), streams, strict=True):
            patched.setattr(sys, name, stream)
        patched.setattr(logger, "handlers", [handler])
        patched.setattr(logger, "propagate", False)
        try:
            for name in DOCTOR_FAILS:
                os.environ.pop(name, None)
            os.environ.update({"CLAUDE_CONFIG_DIR": str(cwd.parent / "claude-config"), **(env or {})})
            # A context of its own, so no context variable the call sets outlives it.
            code = contextvars.copy_context().run(entry.main, list(args))
        except SystemExit as stop:
            # As the interpreter exits for `sys.exit`: None is 0, and any other object 1.
            code = stop.code if isinstance(stop.code, int) else int(stop.code is not None)
        finally:
            os.environ.clear()
            os.environ.update(environ)
        for stream in streams[1:]:
            stream.flush()
        said = out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8")
    return subprocess.CompletedProcess(("orchestra", *args), code, *said)
