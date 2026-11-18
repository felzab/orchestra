#!/usr/bin/env bash
# The project of the notes-tags case: a small notes program on `main` whose store reads a notes file
# that is not valid JSON as holding no notes, so the next `add` overwrites it. The review should find it.
#
# `proj/` is the git checkout the coordinator works in, and `state/` beside it holds the orchestra state,
# named by `stateRoot` in `.claude/orchestra.json`: a run's sandboxed shell writes only inside the
# workspace, and `orchestra init` refuses a state folder inside the checkout. `evidence/` receives the
# outputs the prompt's last step saves for the graders. `.claude/settings.json` sets the worktree base
# a project using the plugin sets, although a run loads no project settings.
set -eu

put() {
  mkdir -p "$(dirname "$1")"
  cat >"$1"
}

mkdir -p proj state evidence

put proj/.gitignore <<'EOF'
__pycache__/
.claude/worktrees/
EOF

put proj/.claude/settings.json <<'EOF'
{
  "worktree": { "baseRef": "head" }
}
EOF

put proj/.claude/orchestra.json <<'EOF'
{
  "stateRoot": "../state"
}
EOF

put proj/README.md <<'EOF'
# notes

A small command-line notebook. Notes live in one JSON file, `~/.notes.json` unless `--file` or
`NOTES_FILE` names another.

    python3 -m notes add "buy milk"
    python3 -m notes list

Run the tests with `python3 -m unittest`.
EOF

put proj/notes/__init__.py <<'EOF'
EOF

put proj/notes/__main__.py <<'EOF'
import sys

from notes.cli import main

sys.exit(main())
EOF

put proj/notes/cli.py <<'EOF'
"""The `notes` command line: `add <text>` and `list`."""

import argparse
import os
import sys

from notes import store


def main(argv=None, out=sys.stdout):
    parser = argparse.ArgumentParser(prog="notes")
    parser.add_argument("--file", default=os.environ.get("NOTES_FILE", store.DEFAULT_PATH))
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add a note")
    add.add_argument("text")
    sub.add_parser("list", help="list every note")
    args = parser.parse_args(argv)

    if args.command == "add":
        note = store.add(args.file, args.text)
        print(f"added {note['id']}", file=out)
    elif args.command == "list":
        for note in store.load(args.file):
            print(f"{note['id']}: {note['text']}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
EOF

put proj/notes/store.py <<'EOF'
"""Notes kept in one JSON file: a list of objects, each with an id and a text."""

import json
import os

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".notes.json")


def load(path):
    """Return the notes stored at `path`, or an empty list where there are none."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save(path, notes):
    """Write `notes` to `path`."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(notes, f, indent=2)


def add(path, text):
    """Append a note with the next id and return it."""
    notes = load(path)
    note = {"id": max((n["id"] for n in notes), default=0) + 1, "text": text}
    notes.append(note)
    save(path, notes)
    return note
EOF

put proj/tests/__init__.py <<'EOF'
EOF

put proj/tests/test_cli.py <<'EOF'
import io
import os
import tempfile
import unittest

from notes import cli


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "notes.json")

    def tearDown(self):
        self.dir.cleanup()

    def run_cli(self, *args):
        out = io.StringIO()
        code = cli.main(["--file", self.path, *args], out=out)
        return code, out.getvalue()

    def test_add_prints_the_id(self):
        self.assertEqual(self.run_cli("add", "milk"), (0, "added 1\n"))

    def test_list_prints_each_note(self):
        self.run_cli("add", "milk")
        self.run_cli("add", "bread")
        self.assertEqual(self.run_cli("list"), (0, "1: milk\n2: bread\n"))


if __name__ == "__main__":
    unittest.main()
EOF

put proj/tests/test_store.py <<'EOF'
import os
import tempfile
import unittest

from notes import store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "notes.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_loads_empty(self):
        self.assertEqual(store.load(self.path), [])

    def test_add_then_load(self):
        store.add(self.path, "first")
        store.add(self.path, "second")
        self.assertEqual(
            store.load(self.path),
            [{"id": 1, "text": "first"}, {"id": 2, "text": "second"}],
        )

    def test_ids_follow_the_highest(self):
        store.save(self.path, [{"id": 7, "text": "old"}])
        self.assertEqual(store.add(self.path, "new")["id"], 8)


if __name__ == "__main__":
    unittest.main()
EOF

cd proj
git init --quiet --initial-branch=main
# A run's home folder is empty, so the checkout names who commits; the sandbox lets no command write it.
git config user.name "Notes Owner"
git config user.email "owner@example.com"
# A system configuration converting line endings would make the commit differ between machines.
git config core.autocrlf false
git add --all
# Fixed dates keep the fixture's commit the same in every run.
GIT_AUTHOR_DATE="2026-01-05T09:00:00Z" GIT_COMMITTER_DATE="2026-01-05T09:00:00Z" \
  git commit --quiet --message "Notes: add and list notes in a JSON file"
