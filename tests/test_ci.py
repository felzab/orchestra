"""`ci`, `ci expect` and `ci gate`: recorded logs and recorded `gh` answers, `gh` faked at its process edge.

The program is called in this process, but one read runs it as the launcher does, with
`PYTHONIOENCODING` set to a Windows code page, so a glyph in a log reaches it as bytes it must decode as
UTF-8 itself.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from conftest import MakeRepo, git, inprocess, orchestra, write_config
from orchestra import ci, config, log, model, store

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ci"

# A project's own lines: a check's summary closing on its duration, a relay pointing at the other
# jobs, a test runner's and a type checker's finding lines, and a runner's failing case, its error next.
VERDICT = r"^\s*✗\s+(?P<text>.+?)(?:\s+(?:\d+m\s*)?[\d.]+m?s)?$"
RELAY = r"^Some jobs failed\b"
FINDINGS = (r"^\s*(?P<text>(?:FAILED|ERROR) \S+::\S.*)$", r"^\s*(?P<text>\S+\(\d+,\d+\): error TS\d+: .+)$")
CASES = (r"^\s*FAIL\s+(?:\|[^|]+\|\s+|\S+\s{2,}(?=\S))?(?P<name>\S.*?)(?:\s+\[ [^\]]+ \])?\s*$",)
CI: dict[str, object] = {
    "workflow": "ci",
    "verdict": VERDICT,
    "relay": RELAY,
    "findings": list(FINDINGS),
    "cases": list(CASES),
}
PATTERNS = ci.patterns(config.Ci(verdict=VERDICT, relay=RELAY, findings=FINDINGS, cases=CASES))

# Answers `gh` from `answers/<key>`, or `answers/<key>.<n>` on its n-th call, `@SHA@` replaced by the
# pushed commit; `answers/<key>.exit` makes it fail. Each call's arguments are appended to `calls`.
GH = r"""
import sys
from pathlib import Path

here = Path(__file__).parent
args = sys.argv[1:]
with (here / "calls").open("ab") as calls:
    calls.write((" ".join(args) + "\n").encode("utf-8"))
if args[:2] == ["run", "list"]:
    key = "runs"
elif args[:1] == ["api"]:
    key = "checks" if "check-runs" in args[1] else "statuses"
else:
    key = "log" if "--log-failed" in args else "view"
answers = here / "answers"
count = here / f"{key}.count"
n = int(count.read_bytes() or b"0") + 1 if count.exists() else 1
count.write_bytes(str(n).encode())
if (answers / f"{key}.exit").exists():
    sys.stderr.buffer.write(b"HTTP 502: Bad Gateway\n")
    sys.exit(int((answers / f"{key}.exit").read_bytes()))
numbered = sorted((int(p.suffix[1:]), p) for p in answers.glob(f"{key}.*") if p.suffix[1:].isdigit())
held = [p for k, p in numbered if k <= n] or [answers / key]
sys.stdout.buffer.write(held[-1].read_bytes().replace(b"@SHA@", (here / "sha").read_bytes()))
"""


def _gh(tmp_path: Path, sha: str) -> dict[str, str]:
    """A `gh` on PATH running the stub with this interpreter: a `.bat` Windows finds, and a shell script elsewhere."""
    folder = tmp_path / "bin"
    (folder / "answers").mkdir(parents=True)
    stub = folder / "gh_stub.py"
    stub.write_bytes(GH.encode("utf-8"))
    (folder / "sha").write_bytes(sha.encode())
    if sys.platform == "win32":
        (folder / "gh.bat").write_bytes(f'@"{sys.executable}" "{stub}" %*\r\n'.encode())
    else:
        launcher = folder / "gh"
        launcher.write_bytes(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n'.encode())
        launcher.chmod(0o755)
    return {"PATH": f"{folder}{os.pathsep}{os.environ['PATH']}", "PYTHONIOENCODING": "cp1252"}


def _answer(tmp_path: Path, key: str, fixture: str) -> None:
    shutil.copyfile(FIXTURES / fixture, tmp_path / "bin" / "answers" / key)


def _calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "bin" / "calls"
    return path.read_bytes().decode().splitlines() if path.exists() else []


@pytest.fixture
def repo(tmp_path: Path, make_repo: MakeRepo) -> Path:
    return make_repo(tmp_path / "project")


class Project:
    def __init__(self, root: Path, settings: dict[str, object] | None = None) -> None:
        """A session on the repository at `root`, pushed at `sha`, with a landing made since that moved HEAD past it."""
        self.tmp = tmp_path = root.parent
        self.root = root
        state_root = (tmp_path / "state").as_posix()
        write_config(self.root, {"stateRoot": state_root, "ci": CI, **(settings or {})})
        (self.root / ".gitignore").write_bytes(b".claude/settings.local.json\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-m", "configure")
        git(self.root, "remote", "add", "origin", "https://example.invalid/project.git")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.root, "branch", "--set-upstream-to", "origin/main")
        self.sha = git(self.root, "rev-parse", "HEAD")
        (self.root / "landed.txt").write_bytes(b"landed\n")
        git(self.root, "add", "landed.txt")
        git(self.root, "commit", "-m", "a landing during the run")
        # The session holding the state's lease, whatever session runs these cases.
        self.env = {**_gh(tmp_path, self.sha), store.SESSION_ID: "session-owner"}
        # Most projects post no commit status; a case about one answers its own.
        _answer(tmp_path, "statuses", "statuses-none.json")
        done = inprocess("init", "s", cwd=self.root, env=self.env)
        assert done.returncode == 0, (done.stdout, done.stderr)
        self.state = store.state_dir(self.root)

    def answer(self, **answers: str) -> None:
        for key, fixture in answers.items():
            _answer(self.tmp, key, fixture)

    def run(self, *args: str, session: str = "session-owner", process: bool = False) -> tuple[int, str, str]:
        """`ci` called in this process, or as the launcher runs it where the case is about its streams."""
        call = orchestra if process else inprocess
        done = call("ci", *args, cwd=self.root, env={**self.env, store.SESSION_ID: session})
        assert "Traceback" not in done.stderr, done.stderr
        return done.returncode, done.stdout, done.stderr

    def expect(self, *rows: tuple[str, str]) -> None:
        held = [model.to_record(ci.Expected(job, text, "90", self.sha, "a fix")) for job, text in rows]
        # Written as the program writes it, past the lease, which this test process holds no part of.
        store.replace_bytes(self.state / store.CI, json.dumps({"expected": held, "readings": []}).encode())

    def rows(self) -> list[ci.Expected]:
        path = self.state / store.CI
        return [model.from_record(ci.Expected, row, path) for row in store.read_json(path).get("expected", [])]

    def kinds(self) -> list[str]:
        return [entry.kind for entry in log.entries(self.state)]


# --- a failed log, read in process ---------------------------------------------------------------


def _row(job: str, text: str) -> ci.Expected:
    return ci.Expected(job, text, "90", "0" * 40, "a fix")


ROWS = [
    _row("lint", "is set twice"),
    _row("status", "past its limit of"),
    _row("test-py", "test_parser.py::test_empty_input"),
    _row("lint", "never printed:"),
]


def _classify(fixture: str, rows: list[ci.Expected], passed: frozenset[str] = frozenset()) -> ci.Verdict:
    return ci.classify(ci.parse((FIXTURES / fixture).read_bytes().decode("utf-8"), PATTERNS), rows, passed)


def test_every_line_of_every_failed_job_is_named_expected_or_new() -> None:
    """A job holding an expected red never hides a second, unlisted line; a row takes one line, so a second
    line holding its text is NEW; a job no pattern reads, or one printing only its summary, is NEW."""
    verdict = _classify("failed.log", ROWS)
    assert [(job, text) for job, text, _ in verdict.new] == [
        ("lint", "notes.txt:7: trailing whitespace"),
        ("test-js", "src/cart.test.ts > cart totals > adds the tax to every line"),
        ("test-py", "pyright reported 1 error."),
        ("typecheck", "src/api.ts(41,9): error TS2339: Property 'total' does not exist on type 'Order'."),
        ("deps", "the lockfile is out of date."),
        ("build", ci.UNRECOGNISED),
        ("status", "build ran 9 min, past its limit of 8 min"),
    ]
    assert [generic for _, _, generic in verdict.new] == [False] * 4 + [True, True, False]
    assert [(row.job, line) for row, line in verdict.expected] == [
        ("lint", "config.yml:3: key 'timeout' is set twice"),
        ("status", "test-py ran 12 min, past its limit of 10 min"),
        ("test-py", "FAILED tests/test_parser.py::test_empty_input - AssertionError: 2 != 3"),
    ]
    assert verdict.red == [ROWS[3]]
    assert verdict.cleared == []


def test_each_failing_case_is_named_and_the_runners_annotation_is_its_repeat() -> None:
    verdict = _classify("cases.log", [])
    assert [text for _, text, _ in verdict.new] == [
        "src/queue.test.ts > a queue: when empty > returns nothing, not null",
        "src/queue.test.ts > keeps the order of two pushes",
        "src/clock.test.ts",
    ]


def test_one_unlisted_line_among_listed_ones_is_new_and_no_summary_is() -> None:
    """An annotation closing a runner's lines is their verdict, and a summary under a finding is none."""
    verdict = _classify("listed.log", [_row("lint", "a known problem"), _row("lint", "t_known")])
    assert [text for _, text, _ in verdict.new] == ["a new problem"]
    assert len(verdict.expected) == 2


def test_a_job_printing_only_its_summary_is_new_by_that_summary() -> None:
    verdict = _classify("summaries.log", [_row("lint", "a known problem")])
    assert verdict.new == [("lint", "3 problems found", True)]


def test_a_row_is_cleared_by_its_own_job_passing_and_no_other() -> None:
    """Two jobs can hold one text: a row belongs to its job, so one passing clears its own row alone."""
    rows = [_row("format", "shared"), _row("lint", "shared"), _row("lint", "never printed:")]
    verdict = _classify("summaries.log", rows, frozenset({"format"}))
    assert verdict.cleared == [rows[0]]
    assert verdict.red == rows[1:]


def test_a_row_takes_a_line_of_its_own_job_alone() -> None:
    """The same failure in a second job, such as a matrix's other platform, is NEW until a row of that job holds it."""
    # The other job prints first, so a row matching any job would take its line and leave its own job's NEW.
    text = "test-js\tRun\t2025-04-02T08:03:41Z ##[error]x failed\nlint\tRun\t2025-04-02T08:03:41Z ##[error]x failed\n"
    verdict = ci.classify(ci.parse(text, PATTERNS), [_row("lint", "x failed")], frozenset())
    assert verdict.new == [("test-js", "x failed", False)]


def test_a_relay_alone_is_new_and_beside_other_jobs_is_none() -> None:
    relay = "status\tRun\t2025-04-02T08:03:41Z ##[error]Some jobs failed: read each failed job's log.\n"
    alone = ci.classify(ci.parse(relay, PATTERNS), [], frozenset())
    assert alone.new == [("status", ci.UNRECOGNISED, True)]
    beside = ci.classify(ci.parse(relay + "lint\tRun\t2025-04-02T08:03:41Z ##[error]x\n", PATTERNS), [], frozenset())
    assert beside.relays == ["status"]
    assert beside.new == [("lint", "x", False)]


# --- a read, through `gh` -------------------------------------------------------------------------


def test_a_failed_run_names_its_new_lines_last_and_a_glyph_line_survives_a_code_page(
    tmp_path: Path, repo: Path
) -> None:
    """The `deps` job's summary opens with U+2717: read through a code page, it would match no pattern and
    read as a job with no line recognised."""
    project = Project(repo)
    project.answer(runs="runs-failed.json", checks="checks.json", view="view-failed.json", log="failed.log")
    project.expect(("lint", "is set twice"), ("format", "an unformatted file"))
    code, out, _ = project.run(process=True)
    assert code == 1, out
    # A Windows pipe's text mode would end every line in `\r\n`.
    assert "\r" not in out
    lines = out.splitlines()
    assert "NEW       deps: the lockfile is out of date." in lines
    first_new = next(k for k, line in enumerate(lines) if line.startswith("NEW"))
    assert all(line.startswith("NEW") for line in lines[first_new:-1]), out
    assert lines[-1] == f"failure at {project.sha[:9]}: 3 runs, checks and statuses, 1 failed; 9 NEW, 1 expected"
    assert "expected  lint: is set twice" in lines
    # `format` passed in this run, so its row is cleared and written so.
    assert "CLEARED   format: an unformatted file" in lines
    assert [row.cleared for row in project.rows()] == [None, f"its job passed at {project.sha[:9]}"]
    # The Actions app's check run is the workflow's job, already read; the license scanner's is read.
    assert "success   check license scan, 4.0 min" in lines
    assert "ci / lint" not in out
    assert not any("--limit 1 " in call for call in _calls(tmp_path))


def test_a_passing_run_clears_every_row_and_keeps_a_reading(repo: Path) -> None:
    project = Project(repo)
    project.answer(runs="runs-passed.json", checks="checks-none.json")
    project.expect(("lint", "a"), ("test-js", "b"))
    code, out, _ = project.run()
    assert code == 0, out
    assert (
        out.splitlines()[-1]
        == f"success at {project.sha[:9]}: 2 runs, checks and statuses, 0 failed; 0 NEW, 0 expected"
    )
    assert all(row.cleared for row in project.rows())
    assert ci.readings(project.state) == [ci.Reading(project.sha, "success", 6.0, ci.readings(project.state)[0].time)]
    assert project.kinds().count("ci") == 1


def test_a_run_not_concluded_is_refused_without_wait_and_gh_failing_is_its_exit(tmp_path: Path, repo: Path) -> None:
    project = Project(repo)
    project.answer(runs="runs-running.json", checks="checks-none.json")
    code, _, err = project.run()
    assert code == 2, err
    assert "1 of 2 not concluded: run ci in_progress" in err, err
    (tmp_path / "bin" / "answers" / "checks.exit").write_bytes(b"1")
    code, _, err = project.run()
    assert code == 2, err
    assert "exited 1: HTTP 502: Bad Gateway" in err, err
    assert ci.readings(project.state) == []
    assert "ci" not in project.kinds()


def test_an_item_that_did_not_pass_fails_the_read_whoever_judges_it(repo: Path) -> None:
    """A cancelled run of the workflow, another workflow's failing run and a failing check run each fail it.
    A cancelled run's log is not read, since some of its jobs never ran, yet it passed nothing."""
    project = Project(repo)
    project.answer(runs="runs-cancelled.json", checks="checks-none.json")
    project.expect(("lint", "a"))
    code, out, _ = project.run()
    assert code == 1, out
    assert "cancelled run ci 101 (push), 4.0 min" in out.splitlines()
    assert project.rows()[0].cleared is None
    project.answer(runs="runs-other-failing.json")
    code, out, _ = project.run()
    assert code == 1, out
    assert "failure   run codeql 103 (push), 4.0 min" in out.splitlines(), out
    assert out.splitlines()[-1].startswith(f"failure at {project.sha[:9]}: 2 runs, checks and statuses, 1 failed")
    project.answer(runs="runs-passed.json", checks="checks-failing.json")
    code, out, _ = project.run()
    assert code == 1, out
    assert "failure   check Code scanning results, 4.0 min" in out.splitlines(), out
    assert ci.readings(project.state)[0].conclusion == "failure"


def test_a_project_running_no_ci_reads_none_and_one_holding_a_workflow_waits_for_its_run(
    tmp_path: Path, repo: Path
) -> None:
    project = Project(repo, {"ci": {}})
    (tmp_path / "bin" / "answers" / "runs").write_bytes(b"[]")
    project.answer(checks="checks-none.json")
    code, out, _ = project.run()
    assert code == 0, out
    assert out.splitlines()[-1].startswith(f"none at {project.sha[:9]}: no CI")
    assert ci.readings(project.state)[0].conclusion == "none"
    # A workflow in the pushed commit whose run has not appeared yet is waited on, never read as none.
    (project.root / ".github" / "workflows").mkdir(parents=True)
    (project.root / ".github" / "workflows" / "ci.yml").write_bytes(b"on: push\n")
    git(project.root, "add", "-A")
    git(project.root, "commit", "-m", "a workflow")
    git(project.root, "update-ref", "refs/remotes/origin/main", "HEAD")
    code, _, err = project.run()
    assert code == 2, err
    assert "no workflow run, check run or status exists" in err, err


# --- a wait ---------------------------------------------------------------------------------------

# A bound of 3 s: long enough for two reads a poll apart, short enough for a case.
BRIEF: dict[str, object] = {"ci": {**CI, "waitMinutes": 0.05}}


def test_a_wait_takes_a_check_created_after_every_run_concluded(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Code scanning creates its check run only once the analysis has concluded, so one read that finds
    everything concluded is not yet the set."""
    # Ended by two equal reads, not by the bound, so a loaded machine's slow first read still gets its second.
    monkeypatch.setattr(ci, "POLL_SECONDS", 0.2)
    project = Project(repo, {"ci": {**CI, "waitMinutes": 1}})
    project.answer(runs="runs-passed.json", **{"checks.1": "checks-none.json", "checks.2": "checks-failing.json"})
    code, out, _ = project.run("--wait")
    assert code == 1, out
    assert "check Code scanning results" in out, out


def test_a_wait_names_a_run_that_never_appears_and_writes_no_reading(repo: Path) -> None:
    """A `ci.workflow` written as the file's name, or in another case, is answered with the names that ran."""
    project = Project(repo, BRIEF)
    project.answer(runs="runs-release-only.json", checks="checks-none.json")
    code, out, _ = project.run("--wait")
    assert code == 1, out
    named = "no ci run exists (workflows run here: release; `ci.workflow` takes a workflow's `name:`)"
    assert out.splitlines()[-1] == f"not concluded at {project.sha[:9]} within 0.05 min: {named}"
    assert ci.readings(project.state) == []
    assert "ci" not in project.kinds()


def test_a_commit_status_is_read_beside_the_runs_and_checks(tmp_path: Path, repo: Path) -> None:
    """A CI outside Actions may report through a status alone, which would otherwise read as no CI at all."""
    project = Project(repo, {"ci": {}})
    (tmp_path / "bin" / "answers" / "runs").write_bytes(b"[]")
    project.answer(checks="checks-none.json", statuses="statuses-failing.json")
    code, _, err = project.run()
    assert code == 2, err
    assert "1 of 2 not concluded: status codecov/patch pending" in err, err
    concluded = (FIXTURES / "statuses-failing.json").read_bytes().replace(b'"pending"', b'"success"')
    (tmp_path / "bin" / "answers" / "statuses").write_bytes(concluded)
    code, out, _ = project.run()
    assert code == 1, out
    assert "failure   status ci/jenkins" in out.splitlines(), out
    assert out.splitlines()[-1].startswith(f"failure at {project.sha[:9]}: 2 runs, checks and statuses, 1 failed")
    assert ci.readings(project.state)[0].conclusion == "failure"


# --- expect ---------------------------------------------------------------------------------------


def test_expect_records_each_specific_new_line_that_the_next_read_expects(repo: Path) -> None:
    """No row is written for a job's summary or for the stand-in of a job no pattern reads: a row
    matching it would expect every later failure of that job."""
    project = Project(repo)
    project.answer(runs="runs-failed.json", checks="checks-none.json", view="view-failed.json", log="failed.log")
    code, out, _ = project.run("expect", "101", "--cleared-by", "the limit fix")
    assert code == 1, out
    assert (
        out.splitlines()[-1]
        == "8 expected red(s) recorded from run 101, cleared by the limit fix; 2 line(s) not written"
    )
    assert "not written deps: the lockfile is out of date.  (no single finding: read the job's log)" in out
    assert {row.cleared_by for row in project.rows()} == {"the limit fix"}
    code, out, _ = project.run()
    assert code == 1, out
    assert out.splitlines()[-1].endswith("2 NEW, 8 expected"), out
    assert [line for line in out.splitlines() if line.startswith("NEW")] == [
        "NEW       deps: the lockfile is out of date.",
        f"NEW       build: {ci.UNRECOGNISED}",
    ]


# A run of another workflow, of another commit, one still running and one cancelled.
REFUSED: list[tuple[dict[str, str], str]] = [
    ({"workflowName": "release"}, "is release's for"),
    ({"headSha": "0" * 40}, "is ci's for 000000000"),
    ({"status": "in_progress"}, "is in_progress"),
    ({"conclusion": "cancelled"}, "was cancelled"),
]


def test_expect_refuses_a_run_that_says_nothing_of_the_pushed_commit(tmp_path: Path, repo: Path) -> None:
    project = Project(repo)
    for change, said in REFUSED:
        view = {**json.loads((FIXTURES / "view-failed.json").read_bytes()), **change}
        (tmp_path / "bin" / "answers" / "view").write_bytes(json.dumps(view).encode())
        code, _, err = project.run("expect", "101", "--cleared-by", "x")
        assert code == 2, err
        assert said in err, err
    assert not (project.state / store.CI).exists()


# --- the gate -------------------------------------------------------------------------------------

# The gate stand-in: its argument is its exit code, and it writes a line holding a glyph as UTF-8 bytes.
GATE = r"""
import sys
sys.stdout.buffer.write('3 checks — all run\n'.encode('utf-8'))
sys.exit(int(sys.argv[1]))
"""


def _gated(repo: Path) -> Project:
    project = Project(repo, {"gate": {"run": [sys.executable, "gate.py"]}})
    (project.root / "gate.py").write_bytes(GATE.encode())
    git(project.root, "add", "gate.py")
    git(project.root, "commit", "-m", "the gate")
    return project


def test_the_gate_is_judged_by_its_own_exit_code_and_its_output_kept_as_bytes(repo: Path) -> None:
    """Every code but 0 is red, and the verb exits 1 for it: its own 2 would say it refused to run the gate."""
    project = _gated(repo)
    sha = git(project.root, "rev-parse", "HEAD")
    for code, verdict in ((0, "green"), (1, "red"), (2, "red")):
        done, out, _ = project.run("gate", "--", str(code))
        assert done == min(code, 1), out
        lines = out.splitlines()
        assert lines[0].startswith(f"exit {code}, ")
        assert lines[-1] == f"gate {verdict} at {sha[:9]}: exit {code}"
        kept = Path(lines[0].rsplit("its output is ", 1)[1])
        assert kept.read_bytes() == "3 checks — all run\n".encode()
        entry = [entry for entry in log.entries(project.state) if entry.kind == "gate"][-1]
        assert entry.text.startswith(f"{sha[:9]} {verdict}: exit {code}, ")
        assert entry.text.endswith(f", args {code}")
    assert len(list((project.state / "gate").glob(f"{sha[:9]}-*.log"))) == 3


def test_a_final_gate_refuses_a_changed_tree_and_a_head_not_pushed(repo: Path) -> None:
    project = _gated(repo)
    code, _, err = project.run("gate", "--final", "--", "0")
    assert code == 2, err
    assert "HEAD" in err, err
    assert "is not its upstream's" in err, err
    git(project.root, "update-ref", "refs/remotes/origin/main", "HEAD")
    (project.root / "loose.txt").write_bytes(b"x\n")
    code, _, err = project.run("gate", "--final", "--", "0")
    assert code == 2, err
    assert "1 path(s) are changed or untracked" in err, err
    (project.root / "loose.txt").unlink()
    assert project.run("gate", "--final", "--", "0")[0] == 0
    assert len(list((project.state / "gate").iterdir())) == 1
    # With no remote, nothing can be pushed: a clean tree alone is final.
    git(project.root, "commit", "--allow-empty", "-qm", "not pushed")
    git(project.root, "remote", "remove", "origin")
    assert project.run("gate", "--final", "--", "0")[0] == 0
    (project.root / "loose.txt").write_bytes(b"x\n")
    assert project.run("gate", "--final", "--", "0")[0] == 2


def test_a_session_not_holding_the_state_is_refused_before_it_waits_or_runs_the_gate(
    tmp_path: Path, repo: Path
) -> None:
    """A wait whose reading the lease would refuse to keep, or a gate whose log lands in another session's state,
    is refused before it starts."""
    project = _gated(repo)
    project.answer(runs="runs-running.json", checks="checks-none.json")
    for args in (("--wait",), ("gate", "--", "0"), ("expect", "101", "--cleared-by", "x")):
        code, _, err = project.run(*args, session="session-other")
        assert code == 2, err
        assert "session session-owner holds this session's state" in err, err
    assert not (project.state / "gate").exists()
    assert _calls(tmp_path) == []
