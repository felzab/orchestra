"""`diff`: the session's change captured byte for byte, and a tree that stays at the captured commit."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import git, inprocess
from orchestra import diff, log, model, prune, store


def commit(root: Path, path: str, data: bytes) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_bytes(data)
    git(root, "add", path)
    git(root, "commit", "-m", f"change {path}")


def test_the_change_from_the_base_to_the_tip_is_captured_whole(project: Path) -> None:
    assert inprocess("init", "s1", cwd=project).returncode == 0
    state = store.state_dir(project)
    base = model.read_session(state).base_commit
    # Bytes no decoding may touch: Latin-1, a carriage return and a line with no end.
    commit(project, "docs/latin.txt", b"caf\xe9\r\nline\r\n")
    commit(project, "lib/a.py", b"x = 1\ny = 2")
    commit(project, ".gitattributes", b"*.txt diff=upper\n")
    # A user's colour, external diff tool and text conversion, each of which would rewrite the change.
    for key, value in (
        ("color.diff", "always"),
        ("diff.external", "echo EXTERNAL"),
        ("diff.upper.textconv", "sed s/line/CONVERTED/"),
    ):
        git(project, "config", key, value)
    done = inprocess("diff", cwd=project)
    assert done.returncode == 0, done.stderr
    tip = git(project, "rev-parse", "main")
    path = state / store.DIFFS / f"{base[:12]}..{tip[:12]}.diff"
    messages = path.with_suffix(".messages")
    lines = done.stdout.splitlines()
    assert lines[:2] == [str(path), str(messages)]
    assert lines[-1].startswith(f"3 file(s) changed from {base[:9]} to {tip[:9]}; ")
    plain = ("--no-color", "--no-ext-diff", "--no-textconv", f"{base}..{tip}")
    expected = subprocess.run(("git", "-C", str(project), "diff", *plain), capture_output=True, check=True).stdout
    assert b"+caf\xe9\r\n+line\r\n" in expected
    assert path.read_bytes() == expected


def test_the_spans_commits_are_captured_with_their_authors_as_recorded(project: Path) -> None:
    """What a public push carries beside the diff; a mailmap or a decoration would change what a reader sees."""
    assert inprocess("init", "s1", cwd=project).returncode == 0
    state = store.state_dir(project)
    base = model.read_session(state).base_commit
    commit(project, "lib/a.py", b"x\n")
    commit(project, "lib/b.py", b"y\n")
    (project / ".mailmap").write_bytes(b"Someone Else <else@example.invalid> <fixture@example.invalid>\n")
    for key, value in (("log.decorate", "full"), ("log.showSignature", "true"), ("log.mailmap", "true")):
        git(project, "config", key, value)
    done = inprocess("diff", cwd=project)
    assert done.returncode == 0, done.stderr
    tip = git(project, "rev-parse", "main")
    messages = state / store.DIFFS / f"{base[:12]}..{tip[:12]}.messages"
    held = messages.read_bytes()
    assert held.count(b"\ncommit ") + held.startswith(b"commit ") == 2
    assert held.count(b"Author:     fixture <fixture@example.invalid>\n") == 2
    assert b"    change lib/a.py\n" in held
    assert b"    change lib/b.py\n" in held
    assert b"HEAD" not in held


def test_a_tree_stays_at_the_captured_commit_while_the_branch_moves_on(project: Path) -> None:
    assert inprocess("init", "s1", cwd=project).returncode == 0
    state = store.state_dir(project)
    commit(project, "lib/a.py", b"one\n")
    captured = git(project, "rev-parse", "main")
    done = inprocess("diff", "--tree", cwd=project)
    assert done.returncode == 0, done.stderr
    tree = state / store.TREES / captured
    assert done.stdout.splitlines()[2] == str(tree)
    commit(project, "lib/a.py", b"two\n")
    assert git(tree, "rev-parse", "HEAD") == captured
    assert (tree / "lib" / "a.py").read_bytes() == b"one\n"
    assert git(tree, "status", "--porcelain") == ""
    again = inprocess("diff", "--tree", cwd=project)
    assert again.stdout.splitlines()[2] == str(state / store.TREES / git(project, "rev-parse", "main"))
    made = [entry.text for entry in log.entries(state) if entry.kind == diff.KIND]
    assert made == [str(tree), str(state / store.TREES / git(project, "rev-parse", "main"))]
    assert inprocess("diff", "--tree", cwd=project).returncode == 0
    assert len([entry for entry in log.entries(state) if entry.kind == diff.KIND]) == 2


def test_a_tree_folder_no_log_entry_names_is_refused_naming_its_removal(project: Path) -> None:
    """A checkout stopped part-way leaves the folder with files missing; only a whole tree is logged."""
    assert inprocess("init", "s1", cwd=project).returncode == 0
    state = store.state_dir(project)
    # An earlier commit's tree logged, so only an entry for this folder's own path vouches for it.
    assert inprocess("diff", "--tree", cwd=project).returncode == 0
    commit(project, "lib/a.py", b"one\n")
    tree = state / store.TREES / git(project, "rev-parse", "main")
    git(project, "worktree", "add", "--detach", str(tree), "main")
    (tree / "lib" / "a.py").unlink()
    refused = inprocess("diff", "--tree", cwd=project)
    assert refused.returncode == 2
    # With `core.longpaths`, as the add takes it, since a deep tracked path under the state's folder needs it.
    assert f'`git -c core.longpaths=true worktree remove --force "{tree.as_posix()}"`' in refused.stderr
    # The command it names clears the folder, and the tree is then made whole.
    git(project, "-c", "core.longpaths=true", "worktree", "remove", "--force", tree.as_posix())
    done = inprocess("diff", "--tree", cwd=project)
    assert done.returncode == 0, done.stderr
    assert (tree / "lib" / "a.py").read_bytes() == b"one\n"


@pytest.mark.skipif(sys.platform != "win32", reason="the 260-character path limit is Windows' alone")
def test_a_tree_is_made_whole_where_its_paths_pass_windows_limit(project: Path) -> None:
    """A tracked path short enough in the checkout runs past 260 characters under the state's longer `trees/`."""
    assert inprocess("init", "s1", cwd=project).returncode == 0
    state = store.state_dir(project)
    # As deep as the checkout takes without long paths, 240 leaving a folder under the 248 Windows makes.
    room = 240 - len(str(project)) - 1
    segments = (room - 12) // 10
    deep = "/".join(["deep12345"] * segments) + "/" + "f" * (room - segments * 10 - 4) + ".txt"
    commit(project, deep, b"deep\n")
    done = inprocess("diff", "--tree", cwd=project)
    assert done.returncode == 0, done.stderr
    tree = state / store.TREES / git(project, "rev-parse", "main")
    assert done.stdout.splitlines()[2] == str(tree)
    held = tree / deep
    assert len(str(held)) > 260, len(str(held))
    # Read in the long form, so the assertion holds whatever the machine's own long-path setting.
    assert Path(prune.long_form(held)).read_bytes() == b"deep\n"
