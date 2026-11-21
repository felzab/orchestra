"""`init`, `doctor`, `note` and `log`, called over a fixture repository."""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import MakeRepo, git, inprocess, write_agent, write_config
from orchestra import cli, config, init, model, report, store
from test_report import text


def _state(project: Path) -> Path:
    return project.parent / "state" / "project" / "s1"


def test_init_makes_the_state_the_pointer_and_the_two_skeletons(project: Path) -> None:
    done = inprocess("init", "s1", "--rulings-from", "101", cwd=project)
    assert done.returncode == 0, done.stderr
    state = _state(project)
    assert sorted(path.name for path in state.iterdir() if path.is_dir()) == sorted(store.FOLDERS)
    session = json.loads((state / store.SESSION).read_bytes())
    assert (session["branch"], session["base"], session["rulings_from"]) == ("main", "main", 101)
    assert session["base_commit"] == git(project, "rev-parse", "HEAD")
    assert session["checkout"] == str(project)
    plan = (state / store.PLAN).read_bytes().decode()
    assert [line for line in plan.splitlines() if line.startswith("## ")] == list(init.PLAN_HEADINGS)
    # The owner's words first, which a session resumed after a crash would otherwise know only as a paraphrase.
    assert plan.startswith("# Plan\n\n## The request\n\n## Slices and their files\n")
    assert (state / store.NOW).read_bytes() == b"# Now\n\nNext action:\nWhy it is next:\n"
    assert b"\r" not in (state / store.PLAN).read_bytes()
    common = Path(git(project, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    assert store.state_dir(project) == state == Path(store.pointer(common, "main").read_bytes().decode().strip())
    local = json.loads((project / config.SETTINGS_LOCAL).read_bytes())
    assert local == {
        "permissions": {"additionalDirectories": [str(project.parent / "state" / "project")]},
        "autoCompactWindow": 500000,
    }
    path = project / config.SETTINGS_LOCAL
    assert f"set `autoCompactWindow` to 500000 in {path}, as no settings file sets a window\n" in done.stdout
    # What was seen in a desktop session started from a pre-warmed process, and what is expected but untested.
    user = project.parent / "claude-config" / "settings.json"
    assert (
        "a desktop session started from a pre-warmed process did not apply the window the project's or the local"
        " settings file sets (anthropics/claude-code issue 105316); `/autocompact 500k` typed inside such a session"
        f' was seen to apply, saving a window for its model in {user}, and `"autoCompactWindow": 500000` at the top'
        " level of that file is expected to reach it, untested\n"
    ) in done.stdout
    assert done.stdout.splitlines()[-1] == "doctor: every check holds"


def test_init_records_the_base_its_option_names_over_the_configurations(project: Path) -> None:
    """A programme's sessions branch from its own branch, which belongs in no file every collaborator shares."""
    write_config(project, {"stateRoot": (project.parent / "state").as_posix(), "base": "main"})
    git(project, "switch", "-q", "-c", "follow-up")
    git(project, "commit", "-q", "--allow-empty", "-m", "the programme's work")
    programme = git(project, "rev-parse", "HEAD")
    git(project, "switch", "-q", "-c", "s1")
    git(project, "commit", "-q", "--allow-empty", "-m", "the session's work")
    done = inprocess("init", "s1", "--base", "follow-up", cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    session = json.loads((_state(project) / store.SESSION).read_bytes())
    assert (session["base"], session["base_commit"]) == ("follow-up", programme)
    said = f"session project/s1 on s1, based on follow-up (named by --base) at {programme[:9]}"
    assert done.stdout.startswith(f"{said}: {_state(project)}\n")
    assert store.read_jsonl(_state(project) / store.LOG)[0]["text"] == said


def _programme_branch(project: Path) -> str:
    """The programme's branch `follow-up`, one commit past `main`, checked out; its commit."""
    git(project, "switch", "-q", "-c", "follow-up")
    git(project, "commit", "-q", "--allow-empty", "-m", "the programme's work")
    return git(project, "rev-parse", "HEAD")


def _init_on(project: Path, session: str, *options: str) -> dict[str, object]:
    """`init` run on a new branch named for the session; its record."""
    git(project, "switch", "-q", "-c", session)
    done = inprocess("init", session, *options, cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads((_state(project).parent / session / store.SESSION).read_bytes())


def test_init_without_base_refuses_a_branch_already_past_the_base_it_would_record(project: Path) -> None:
    """A session cut from the programme's branch would record `main` and count the programme's work as its own."""
    _programme_branch(project)
    git(project, "switch", "-q", "-c", "s1")
    done = inprocess("init", "s1", cwd=project)
    assert done.returncode == 2
    assert "s1 holds 1 commit main does not" in done.stderr
    assert "`--base <branch>`" in done.stderr
    assert not _state(project).exists()


@pytest.mark.parametrize(
    ("remote", "said"),
    [
        (
            "refs/remotes/origin/trunk",
            "the base trunk (the configuration's base) is no branch in this clone: make it from origin's with"
            " `git branch trunk origin/trunk`, and run init again",
        ),
        (
            None,
            "the base trunk (the configuration's base) is no branch in this clone, nor is origin/trunk: fetch it,"
            " or name the branch this session is cut from with `--base <branch>`",
        ),
    ],
    ids=["origin holds it", "origin does not"],
)
def test_init_refuses_a_base_missing_from_the_clone_naming_the_command_that_makes_it(
    project: Path, remote: str | None, said: str
) -> None:
    """A single-branch clone lacks the branch, which no edit of the shared configuration repairs."""
    write_config(project, {"stateRoot": (project.parent / "state").as_posix(), "base": "trunk"})
    if remote is not None:
        # As a clone of another branch holds it: origin's branch, and no local one.
        git(project, "update-ref", remote, "HEAD")
    done = inprocess("init", "s1", cwd=project)
    assert done.returncode == 2, done.stdout + done.stderr
    # Not the shared configuration, whose edit would not make the branch.
    assert done.stderr == f"orchestra init: {said}\n"
    assert not _state(project).exists()


@pytest.mark.parametrize("named", ["refs/heads/follow-up", "origin/follow-up", "follow-up~0", "follow-up^"])
def test_init_refuses_a_base_named_other_than_by_its_local_branchs_short_name(project: Path, named: str) -> None:
    """`prune` spares the base by its short name, so a record spelling it otherwise, or naming a commit from it by a
    revision's suffix (gitrevisions(7)), spares no branch."""
    programme = _programme_branch(project)
    git(project, "update-ref", "refs/remotes/origin/follow-up", "HEAD")
    git(project, "switch", "-q", "-c", "s1")
    done = inprocess("init", "s1", "--base", named, cwd=project)
    assert done.returncode == 2, done.stdout + done.stderr
    assert done.stderr == (
        f"orchestra init: the base {named} (named by --base) is no branch in this clone, nor is origin/{named}:"
        " fetch it, or name the branch this session is cut from with `--base <branch>`\n"
    )
    assert not _state(project).exists()
    if named != "origin/follow-up":
        # The arm below reads no parameter, so one runs it.
        return
    # A tag of the branch's name, which git resolves before the branch (gitrevisions(7)), is not the base.
    git(project, "tag", "follow-up", "main")
    session = _init_on(project, "s2", "--base", "follow-up")
    assert (session["base"], session["base_commit"]) == ("follow-up", programme)


@pytest.mark.parametrize(
    ("options", "said"),
    [
        (
            (),
            " (the configuration's base) shares no commit with HEAD here: name the branch this session is cut"
            " from with `--base <branch>`, or set `base` in .claude/orchestra.json for the project",
        ),
        (
            ("--base", "elsewhere"),
            " (named by --base) shares no commit with HEAD here: name the branch this session is cut from with"
            " `--base <branch>`",
        ),
    ],
    ids=["the configuration's", "--base's"],
)
def test_init_refuses_a_base_sharing_no_commit_with_head_naming_each_way_to_name_another(
    project: Path, options: tuple[str, ...], said: str
) -> None:
    """The configuration's `base` reaches only a session `--base` gives none."""
    git(project, "switch", "-q", "--orphan", "elsewhere")
    git(project, "commit", "-q", "--allow-empty", "-m", "an unrelated history")
    git(project, "switch", "-q", "main")
    if not options:
        write_config(project, {"stateRoot": (project.parent / "state").as_posix(), "base": "elsewhere"})
    done = inprocess("init", "s1", *options, cwd=project)
    assert done.returncode == 2, done.stdout + done.stderr
    assert done.stderr == f"orchestra init: the base elsewhere{said}\n"
    assert not _state(project).exists()


def test_two_clones_sharing_a_folder_name_never_share_a_programme_unasked(tmp_path: Path, make_repo: MakeRepo) -> None:
    """The default programme is the folder's name, so an unrelated clone would read the other's rulings."""
    shared: dict[str, object] = {"stateRoot": (tmp_path / "state").as_posix()}
    first, second = make_repo(tmp_path / "a" / "proj"), make_repo(tmp_path / "b" / "proj")
    for root in (first, second):
        write_config(root, shared)
    assert inprocess("init", "s1", cwd=first).returncode == 0
    # The same checkout's next session, on another branch, joins its own programme.
    git(first, "switch", "-q", "-c", "next")
    assert inprocess("init", "s2", cwd=first).returncode == 0, "the checkout's own programme"
    # A session recorded under another spelling of the checkout, as a redirected folder records one, is its own:
    # checkouts are told apart by identity, not spelling. git resolves a link itself, so the record is written so.
    link = tmp_path / "link-a"
    if sys.platform == "win32":
        import _winapi  # noqa: PLC0415

        _winapi.CreateJunction(str(first), str(link))
    else:
        link.symlink_to(first)
    s1 = tmp_path / "state" / "proj" / "s1"
    model.write_session(s1, dataclasses.replace(model.read_session(s1), checkout=str(link)))
    git(first, "switch", "-q", "-c", "linked")
    assert inprocess("init", "s5", cwd=first).returncode == 0, "the checkout's own programme, spelled otherwise"
    refused = inprocess("init", "s3", cwd=second)
    assert refused.returncode == 2, refused.stdout
    folder = tmp_path / "state" / "proj"
    # The first checkout once, though its sessions record it under two spellings.
    assert refused.stderr == (
        f"orchestra init: the programme proj at {folder} holds sessions of another checkout, {first},"
        " or of this one before it moved: give this project a programme of its own with `--programme <name>`,"
        " or join that one with `--programme proj`\n"
    )
    assert sorted(path.name for path in folder.iterdir() if path.is_dir()) == ["s1", "s2", "s5"]
    assert not (second / config.SETTINGS_LOCAL).exists()
    own = inprocess("init", "s3", "--programme", "proj-b", cwd=second)
    assert own.returncode == 0, own.stderr
    assert (tmp_path / "state" / "proj-b" / "s3" / store.SESSION).is_file()
    # Named, the shared programme is joined: a second clone of one project continues its rulings.
    git(second, "switch", "-q", "-c", "joined")
    assert inprocess("init", "s4", "--programme", "proj", cwd=second).returncode == 0


@pytest.mark.parametrize(
    ("root", "said"),
    [("~", "the home folder or one holding it"), ("state", "inside the checkout"), ("..", "inside the checkout")],
    # The folder holding the checkout, whose default programme is the checkout's own name.
    ids=["the home folder", "a relative root inside the checkout", "a root putting the state inside the checkout"],
)
def test_a_state_root_too_wide_or_inside_the_checkout_is_refused(project: Path, root: str, said: str) -> None:
    write_config(project, {"stateRoot": root})
    # A home of the case's own, so a refusal that regressed writes no state into the machine's; beside the
    # case's folder rather than in it, which `..` names.
    home = str(project.parent.parent / f"{project.parent.name}-home")
    done = inprocess("init", "s1", cwd=project, env={"HOME": home, "USERPROFILE": home})
    assert done.returncode == 2, done.stderr
    assert said in done.stderr
    assert not (project / config.SETTINGS_LOCAL).exists()


def test_the_settings_file_init_writes_is_one_git_ignores(project: Path) -> None:
    """It holds this machine's paths, and an untracked file would stop every landing."""
    assert inprocess("init", "s1", cwd=project).returncode == 0
    git(project, "check-ignore", "-q", config.SETTINGS_LOCAL.as_posix())
    assert ".claude/settings.local.json" not in git(project, "status", "--porcelain", "--untracked-files=all")


def test_a_tracked_settings_file_is_never_written(project: Path) -> None:
    """No ignore rule reaches a tracked file, so a change to it carries this machine's paths toward a commit."""
    (project / config.SETTINGS_LOCAL).parent.mkdir(exist_ok=True)
    (project / config.SETTINGS_LOCAL).write_bytes(b"{}\n")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "tracked local settings")
    done = inprocess("init", "s1", cwd=project)
    assert done.returncode == 1, done.stderr
    folder = project.parent / "state" / "project"
    assert (
        f"{project / config.SETTINGS_LOCAL} is tracked, so it is left as it is: add {folder} to its"
        ' `permissions.additionalDirectories` and `"autoCompactWindow": 500000` yourself'
    ) in done.stdout
    assert git(project, "status", "--porcelain") == ""


def test_the_state_root_joins_the_owners_settings_keeping_every_other_byte(project: Path) -> None:
    folders = ["../docs/"]
    owned: dict[str, object] = {
        "permissions": {"allow": ["Bash(git log *)"], "additionalDirectories": folders},
        "env": {"NAME": "Zoë"},
    }
    written = json.dumps(owned, indent=2, ensure_ascii=False) + "\n"
    (project / config.SETTINGS_LOCAL).write_bytes(written.encode())
    assert inprocess("init", "s1", cwd=project).returncode == 0
    folders.append(str(project.parent / "state" / "project"))
    owned["autoCompactWindow"] = 500000
    held = (project / config.SETTINGS_LOCAL).read_bytes().decode()
    assert held == json.dumps(owned, indent=2, ensure_ascii=False) + "\n"
    assert held.startswith(written.split('"../docs/"')[0] + '"../docs/",\n')


def test_settings_formatted_by_hand_are_left_alone_and_doctor_names_the_folder(project: Path) -> None:
    hand = b'{"permissions": {"allow": []}}\n'
    (project / config.SETTINGS_LOCAL).write_bytes(hand)
    done = inprocess("init", "s1", cwd=project)
    assert done.returncode == 1, done.stderr
    assert (project / config.SETTINGS_LOCAL).read_bytes() == hand
    assert "is not in the shape orchestra rewrites, so it is left as it is" in done.stdout
    assert f"MISSING  {project.parent / 'state' / 'project'} as a working directory" in done.stdout
    # Left as it is, it is still kept out of git: an untracked file stops every landing.
    git(project, "check-ignore", "-q", config.SETTINGS_LOCAL.as_posix())


def _settings(project: Path, where: str) -> Path:
    return {
        "local": project / config.SETTINGS_LOCAL,
        "project": project / ".claude" / "settings.json",
        "user": project.parent / "claude-config" / "settings.json",
    }[where]


@pytest.mark.parametrize(
    ("where", "settings"),
    [("local", {"autoCompactWindow": 300000}), ("project", {"autoCompactWindow": 800000})],
    ids=["the local file's", "the project's"],
)
def test_a_window_any_settings_file_sets_is_left_as_it_is_and_named(
    project: Path, where: str, settings: dict[str, object]
) -> None:
    """A window chosen already is the user's or the team's, which the local file would otherwise outrank."""
    path = _settings(project, where)
    path.write_bytes((json.dumps(settings, indent=2) + "\n").encode())
    # A lower-ranked file's window is not the one in force, so the higher one is named.
    _settings(project, "user").write_bytes(b'{"worktree": {"baseRef": "head"}, "autoCompactWindow": 900000}\n')
    done = inprocess("init", "s1", cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"`autoCompactWindow` is {settings['autoCompactWindow']} in {path}, so init sets no window" in done.stdout
    local = json.loads((project / config.SETTINGS_LOCAL).read_bytes())
    assert local.get("autoCompactWindow") == (300000 if where == "local" else None)


def test_the_local_settings_file_is_excluded_before_it_is_written_and_a_failed_exclusion_leaves_no_session(
    project: Path,
) -> None:
    """A write that went first would leave this machine's paths in an untracked file nothing excludes."""
    exclude = Path(git(project, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    held = exclude.read_bytes()
    exclude.unlink()
    # A folder where the file goes, so the exclusion fails.
    exclude.mkdir()
    done = inprocess("init", "s1", cwd=project)
    # Whether the failure is refused or faults is not this case's matter: only that nothing was written.
    assert done.returncode != 0, done.stdout + done.stderr
    assert not (project / config.SETTINGS_LOCAL).exists()
    # Once the exclusion can be written, `init` runs again rather than finding a session.
    exclude.rmdir()
    exclude.write_bytes(held)
    again = inprocess("init", "s1", cwd=project)
    assert again.returncode == 0, again.stdout + again.stderr
    git(project, "check-ignore", "-q", config.SETTINGS_LOCAL.as_posix())


def test_a_window_for_one_model_or_from_the_environment_is_left_as_it_is_and_named_for_what_it_binds(
    project: Path,
) -> None:
    """A model's window binds that model alone, and the variable outranks every settings key (model-config.md)."""
    user = _settings(project, "user")
    models = {"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": "auto"}}}
    user.write_bytes((json.dumps({"worktree": {"baseRef": "head"}, **models}, indent=2) + "\n").encode())
    done = inprocess("init", "s1", cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (
        f'`modelSettings.claude-opus-5-5.autoCompactWindow` is "auto" in {user}, which binds claude-opus-5-5 alone:'
        " a session on another model runs at Claude Code's default window, and init sets no window that would"
        " override it"
    ) in done.stdout
    assert "autoCompactWindow" not in json.loads((project / config.SETTINGS_LOCAL).read_bytes())
    user.write_bytes(b'{"worktree": {"baseRef": "head"}}\n')
    git(project, "switch", "-q", "-c", "next")
    done = inprocess("init", "s2", cwd=project, env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"})
    assert done.returncode == 0, done.stdout + done.stderr
    said = "`CLAUDE_CODE_AUTO_COMPACT_WINDOW` is 400000 in the environment, which outranks every settings key"
    assert f"{said}, so init sets no window" in done.stdout
    assert "autoCompactWindow" not in json.loads((project / config.SETTINGS_LOCAL).read_bytes())


def test_the_window_in_force_is_the_variables_else_the_first_files_for_the_model_or_every_model(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window a coordinator's prompt compares its context with (settings-reference.md, "modelSettings")."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(project.parent / "claude-config"))
    model = "claude-opus-5-5"
    # With nothing set the session runs at Claude Code's default window, which no file names.
    assert config.window(project, model) is None
    models = {"modelSettings": {model: {"autoCompactWindow": 300000}}}
    _settings(project, "user").write_bytes(json.dumps({"autoCompactWindow": 900000, **models}).encode())
    # In one file a model's own window outranks the window for every model, and binds that model alone.
    assert (config.window(project, model), config.window(project, "claude-other"), config.window(project, None)) == (
        300000,
        900000,
        900000,
    )
    # A higher-ranked file's window for every model outranks a lower one's for the model.
    _settings(project, "local").write_bytes(b'{"autoCompactWindow": 700000}')
    assert config.window(project, model) == 700000
    # `auto` is the model's tuned window, a figure no file holds.
    tuned = {"modelSettings": {model: {"autoCompactWindow": "auto"}}}
    _settings(project, "local").write_bytes(json.dumps(tuned).encode())
    assert config.window(project, model) is None
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "400k")
    assert config.window(project, model) == 400_000
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1M")
    assert config.window(project, model) == 1_000_000


def test_init_refuses_a_session_that_exists_and_a_branch_that_holds_one(project: Path) -> None:
    assert inprocess("init", "s1", cwd=project).returncode == 0
    again = inprocess("init", "s2", cwd=project)
    assert again.returncode == 2, again.stderr
    assert "already holds the session" in again.stderr
    git(project, "switch", "-c", "next")
    same = inprocess("init", "s1", cwd=project)
    assert same.returncode == 2, same.stderr
    assert "the session project/s1 exists" in same.stderr
    assert not (_state(project).parent / "s2").exists()


# Every verb as given that changes the session's record, and every one that only reads it.
WRITING = [
    ("init", "s2"),
    ("brief", "A", "--type", "writer", "--slice", "S1"),
    ("brief", "A", "--check"),
    ("judge", "A", "--verdict", "v"),
    ("route", "owner", "A-F1"),
    ("close", "MOOT", "gone", "A-F1"),
    ("land", "A"),
    ("prune",),
    ("prune", "--discard", "A"),
    ("ci",),
    ("ci", "expect", "1", "--cleared-by", "A"),
    ("ci", "gate"),
    ("queue", "add", "--when", "ci", "push"),
    ("queue", "done", "1"),
    ("ask", "a question?"),
    ("rule", "1", "the owner's words"),
    ("note", "Lens declined: data, no data"),
    ("mode", "attended"),
    ("diff", "--tree"),
    ("override", "a reason"),
]
READING = [
    ("doctor",),
    ("status",),
    ("show", "A-F1"),
    ("findings",),
    ("log",),
    ("queue",),
    ("ask",),
    ("diff",),
    ("land", "A", "--preview"),
]


def test_a_verb_changing_the_record_is_refused_in_an_agents_worktree_and_one_reading_it_is_not(project: Path) -> None:
    """An agent's commands carry its coordinator's session id, so only where one runs parts it from the coordinator."""
    assert inprocess("init", "s1", cwd=project).returncode == 0
    # An agent with a report, so each reading verb finds what it reads and answers 0.
    state = store.state_dir(project)
    write_agent(state, model.Agent("A", "auditor", "reported"))
    report.path(state, "A").write_bytes(text("auditor", "A").encode())
    agent = project.parent / "agent"
    git(project, "worktree", "add", "-b", "worktree-agent-a1", str(agent))
    for args in WRITING:
        refused = inprocess(*args, cwd=agent)
        said = f"`orchestra {args[0]}` changes the session's record, which the coordinator does from the primary"
        assert (refused.returncode, said in refused.stderr) == (2, True), (args, refused.stderr)
    for args in READING:
        done = inprocess(*args, cwd=agent)
        assert done.returncode == 0, (args, done.stderr)


def test_a_command_finds_its_checkout_once_however_many_of_its_parts_ask(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard, the verb and `init`'s doctor each ask, and each answer would cost two git runs."""
    listed: list[tuple[str, ...]] = []
    run_git = config.run_git

    def counted(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        if args[:2] == ("worktree", "list"):
            listed.append(args)
        return run_git(cwd, *args)

    monkeypatch.setattr(config, "run_git", counted)
    for args in [("init", "s1"), ("note", "a line")]:
        listed.clear()
        done = inprocess(*args, cwd=project)
        assert (done.returncode, len(listed)) == (0, 1), (args, done.stderr)


def test_doctor_names_each_thing_a_session_rests_on_that_fails(project: Path) -> None:
    """A missing program, a failing preflight, a forced model, foreground agents and worktrees cut elsewhere."""
    write_config(
        project,
        {
            "stateRoot": (project.parent / "state").as_posix(),
            "models": {"default": {"model": "opus"}},
            "preflight": [
                {"run": ["no-such-program-anywhere"], "why": "the tier needs it"},
                {"run": ["git", "rev-parse", "--verify", "no-such-ref"], "why": "the ref must exist"},
                # Found, and no program: no execute bit, no shebang, and no kind Windows starts directly.
                {"run": ["./README"], "why": "the probe must start"},
            ],
            "gate": {"run": ["./no-such-gate.sh"]},
        },
    )
    # The project's file wins the key over the user's `head` (settings.md, "Settings precedence").
    shared = project / ".claude" / "settings.json"
    shared.write_bytes(b'{"worktree": {"baseRef": "fresh"}}')
    forced: dict[str, str] = dict.fromkeys((init.FORCE, init.FOREGROUND, init.TEAMS), "1")
    done = inprocess("doctor", cwd=project, env=forced)
    assert done.returncode == 1, done.stderr
    assert "MISSING  no-such-program-anywhere (the tier needs it)" in done.stdout
    assert "MISSING  ./no-such-gate.sh (`gate.run`, which `ci gate` runs)" in done.stdout
    assert "FAILED   `git rev-parse --verify no-such-ref` exited 128 (the ref must exist)" in done.stdout
    assert "IGNORED  `models`: CLAUDE_CODE_SUBAGENT_MODEL_FORCE is set" in done.stdout
    assert "SET      CLAUDE_CODE_DISABLE_BACKGROUND_TASKS: every agent runs in the foreground" in done.stdout
    assert f'MISSING  worktree.baseRef "head" (is "fresh" in {shared})' in done.stdout
    assert [line for line in done.stdout.splitlines() if "./README" in line][-1].startswith(
        "FAILED   `./README` could not run: "
    )
    assert "SET      CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: an auditor, researcher or verifier dispatched" in done.stdout
    assert done.stdout.splitlines()[-1] == "doctor: 8 failing"


def test_doctor_names_the_ignore_rule_the_agents_worktrees_need(project: Path) -> None:
    """Without it each worktree is a change in the checkout, and the first landing refuses the tree."""
    (project / ".git" / "info" / "exclude").write_bytes(b"")
    done = inprocess("doctor", cwd=project)
    assert done.returncode == 1, done.stderr
    said = "MISSING  an ignore rule for .claude/worktrees/, so each agent's worktree is a change in this checkout,"
    assert f"{said} which a landing refuses: add the line `.claude/worktrees/` to .gitignore" in done.stdout
    (project / ".gitignore").write_bytes(b".claude/worktrees/\n")
    assert inprocess("doctor", cwd=project).stdout.splitlines()[-1] == "doctor: every check holds"


def test_doctor_refuses_a_configuration_that_does_not_parse_before_any_check(project: Path) -> None:
    """Its first check reads the file, so a file it cannot read is a refusal, not a failing line."""
    (project / ".claude" / "orchestra.json").write_bytes(b"{not json")
    done = inprocess("doctor", cwd=project)
    assert done.returncode == 2, done.stdout
    assert done.stdout == ""
    assert "orchestra doctor: " in done.stderr


def test_doctor_fails_a_state_root_under_a_claude_folder(project: Path) -> None:
    """Claude Code prompts for or refuses every write there, an agent's report included."""
    root = project.parent / "home" / ".claude" / "state"
    write_config(project, {"stateRoot": root.as_posix()})
    done = inprocess("doctor", cwd=project)
    assert done.returncode == 1, done.stderr
    assert f"FAILED   `stateRoot` is {root}, under a .claude folder" in done.stdout


def test_doctor_names_a_state_root_that_no_longer_holds_the_session(project: Path) -> None:
    """The pointer still finds the session, but the next `init` would put its state elsewhere."""
    assert inprocess("init", "s1", cwd=project).returncode == 0
    assert "DIFFERS" not in inprocess("doctor", cwd=project).stdout
    moved = project.parent / "elsewhere"
    write_config(project, {"stateRoot": moved.as_posix()})
    done = inprocess("doctor", cwd=project)
    assert done.returncode == 1
    held = project.parent / "state"
    assert f"DIFFERS  `stateRoot` resolves to {moved}, and this session's state is under {held}:" in done.stdout


def test_a_note_is_stamped_by_the_clock_from_its_words_or_stdin(project: Path) -> None:
    assert inprocess("init", "s1", cwd=project).returncode == 0
    earliest = datetime.datetime.now().astimezone().replace(microsecond=0)
    assert inprocess("note", "landed", "A", cwd=project).returncode == 0
    waiting = inprocess("note", "--waiting", "-", cwd=project, stdin=b"CI for `abc`\nrun 7\n")
    assert waiting.returncode == 0, waiting.stderr
    entries = store.read_jsonl(_state(project) / store.LOG)
    assert [(entry["kind"], entry["text"]) for entry in entries[1:]] == [
        ("note", "landed A"),
        ("waiting", "CI for `abc` run 7"),
    ]
    stamped = datetime.datetime.fromisoformat(entries[1]["time"])
    assert earliest <= stamped <= datetime.datetime.now().astimezone()
    shown = inprocess("log", cwd=project)
    assert shown.returncode == 0
    assert "waiting   CI for `abc` run 7" in shown.stdout
    assert shown.stdout.splitlines()[-1].startswith("3 entries in ")


def test_the_log_prints_its_last_lines_and_says_how_many_are_cut(project: Path) -> None:
    assert inprocess("init", "s1", cwd=project).returncode == 0
    for k in range(45):
        store.append_jsonl(
            _state(project) / store.LOG, {"time": "2026-11-17T10:00:00+01:00", "kind": "note", "text": f"n{k}"}
        )
    shown = inprocess("log", cwd=project)
    lines = shown.stdout.splitlines()
    # The notice of the cut stands on standard error, so `orchestra log | grep` never drops it unseen.
    assert shown.stderr == "... 8 earlier line(s): --all prints every one\n"
    assert len(lines) == 39
    assert lines[0].endswith("n7")
    assert lines[-2].endswith("n44")
    assert len(inprocess("log", "--all", cwd=project).stdout.splitlines()) == 47


def test_the_cut_notice_keeps_its_place_where_the_two_streams_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Bash tool and a skill's injection merge standard error into standard output, which a pipe buffers."""
    monkeypatch.setattr(cli, "LINES", 4)
    for tail, order in ((True, ["notice", "l2", "l3", "sum"]), (False, ["l0", "l1", "notice", "sum"])):
        merged = io.BytesIO()
        # Two streams over one sink, as `2>&1` makes them: standard output buffered as a pipe's is.
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(merged, encoding="utf-8"))
        monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(merged, encoding="utf-8", line_buffering=True))
        cli.emit(argparse.Namespace(all=False), ["l0", "l1", "l2", "l3"], "sum", tail=tail)
        sys.stdout.flush()
        said = merged.getvalue().decode().splitlines()
        assert ["notice" if line.startswith("... 2 ") else line for line in said] == order
