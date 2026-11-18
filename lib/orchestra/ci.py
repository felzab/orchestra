"""`ci`: a commit's CI read whole, each failing log line named expected or NEW, and the project's gate.

`orchestra ci [--wait]` reads every workflow run, check run and commit status of the commit the
session branch was last pushed at, found by that commit and never by recency: one push starts several
workflows, a landing since the push moves HEAD past it, and a newer push cancels the run before it.
The configured workflow's failed log is read line by line against the expected reds in `ci.json`;
every other run, check and status is read by its conclusion. `ci expect RUN --cleared-by NAME` records a
run's NEW lines as expected reds, and `ci gate` runs the configured gate in the primary checkout.

A verdict comes from a conclusion or an exit code first, and a pattern only labels it, so a failure no
pattern recognises reads NEW, never expected or green. Every child's output is read as bytes and
decoded as UTF-8: a Windows code page turns a glyph in a pattern into a silent miss.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal, NamedTuple

from orchestra import DONE, REMAINS, Refused, cli, config, log, model, process, store

# How often a wait asks GitHub again.
POLL_SECONDS = 15.0
# A wait outlasting one network blip asks again; gh failing this many times running is no blip.
GH_FAILURES = 3
# The conclusions that fail nothing. GitHub's others, such as failure, cancelled, timed_out,
# action_required and startup_failure, each fail the commit.
PASSED = frozenset({"success", "neutral", "skipped"})
# The app whose check runs are workflow jobs, which their workflow run already reports.
ACTIONS_APP = "github-actions"
# The state of a commit status that has not concluded.
PENDING = "pending"
RUN_FIELDS = "databaseId,workflowName,status,conclusion,headSha,event,createdAt,startedAt,updatedAt"
# The readings `ci.json` keeps, the oldest dropped first.
KEPT = 50
GATE = "gate"

# GitHub's own log format, the same for every project: `gh run view --log-failed` prints each line as
# `<job>\t<step>\t<stamp> <text>`, a job's first stamp behind a byte-order mark and the escape
# character spelled `^[`.
STAMP_RE = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
COLOUR_RE = re.compile(r"(?:\x1b|\^\[)\[[0-9;]*m")
# The runner trims a line's leading space before it reads a workflow command, so an annotation a
# script prints indented is raised and logged like any other.
ANNOTATION_RE = re.compile(r"^##\[error\](?P<text>.+)$")
# Every failed job's last annotation, which names no finding.
EXIT_RE = re.compile(r"^Process completed with exit code \d+\.")
# What a failed job's lines say where no pattern names a finding among them.
UNRECOGNISED = "no finding line recognised; read its log"

# success: every run, check and status passed. expected: the configured workflow failed on expected reds
# alone. none: the project runs no CI, so nothing can fail. failure: anything else.
Conclusion = Literal["success", "expected", "none", "failure"]
# Where GitHub reads a repository's workflows (GitHub's "Workflow syntax for GitHub Actions").
WORKFLOWS = ".github/workflows/"


@dataclass(frozen=True)
class Expected:
    """A failing line known before a run: its job, the text a line must hold, and what clears it."""

    job: str
    matches: str
    run: str
    commit: str
    cleared_by: str
    time: str = field(default_factory=model.now)
    # The evidence it was cleared on; None while it is red.
    cleared: str | None = None


@dataclass(frozen=True)
class Reading:
    """One read of a commit whose every run, check and status had concluded."""

    commit: str
    conclusion: Conclusion
    # From the first run's or check's start to the last one's end; None where GitHub gave no times.
    minutes: float | None
    time: str


class Item(NamedTuple):
    """A workflow run, a check run or a commit status on the commit."""

    kind: Literal["run", "check", "status"]
    id: str
    name: str
    status: str
    conclusion: str | None
    started: str | None
    ended: str | None
    event: str = ""

    @property
    def done(self) -> bool:
        return self.status == "completed"

    @property
    def passed(self) -> bool:
        return self.done and self.conclusion in PASSED

    def minutes(self) -> float | None:
        start, end = _instant(self.started), _instant(self.ended)
        return None if start is None or end is None or not self.done else (end - start).total_seconds() / 60


class Line(NamedTuple):
    text: str
    # An annotation, as against a runner's own line: the verdict of the runner lines just before it.
    annotation: bool
    # A job's summary, or the stand-in for a job no pattern recognised, rather than a finding.
    generic: bool = False


@dataclass
class Job:
    found: list[Line] = field(default_factory=list[Line])
    verdicts: list[str] = field(default_factory=list[str])
    # The job only points at the other jobs, whose lines carry its findings.
    relay: bool = False


class Patterns(NamedTuple):
    verdict: re.Pattern[str] | None
    relay: re.Pattern[str] | None
    findings: tuple[re.Pattern[str], ...]
    cases: tuple[re.Pattern[str], ...]


class Verdict(NamedTuple):
    expected: list[tuple[Expected, str]]
    red: list[Expected]
    cleared: list[Expected]
    relays: list[str]
    # Each NEW line: its job, its text, and whether it is generic.
    new: list[tuple[str, str, bool]]


def _instant(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- the state ------------------------------------------------------------------------------------


def _parsed(held: store.Record, path: Path) -> tuple[list[Expected], list[Reading]]:
    rows = [model.from_record(Expected, row, path) for row in held.get("expected", [])]
    return rows, [model.from_record(Reading, read, path) for read in held.get("readings", [])]


def _load(state: Path) -> tuple[list[Expected], list[Reading]]:
    path = state / store.CI
    return _parsed(store.read_json(path), path) if path.exists() else ([], [])


def _update(
    state: Path, change: Callable[[list[Expected], list[Reading]], tuple[list[Expected], list[Reading]]]
) -> None:
    """`ci.json` changed under its lock, read afresh: a wait outlasts other writes."""
    path = state / store.CI

    def edit(held: store.Record | None) -> store.Record:
        rows, reads = change(*_parsed(held or {}, path))
        return {"expected": [model.to_record(row) for row in rows], "readings": [model.to_record(r) for r in reads]}

    store.update_json(path, edit)


def readings(state: Path) -> list[Reading]:
    """Every concluded read of the pushed commit's CI this session kept, newest first."""
    return list(reversed(_load(state)[1]))


def red(state: Path) -> list[Expected]:
    return [row for row in _load(state)[0] if row.cleared is None]


# --- GitHub ---------------------------------------------------------------------------------------


def _gh(root: Path, *args: str) -> bytes:
    program = config.program(("gh",), root)
    if program is None:
        raise Refused("gh is not on PATH: install GitHub's CLI and sign in with `gh auth login`")
    done = process.run((program, *args), cwd=root)
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip()
        raise Refused(f"gh {' '.join(args)} exited {done.returncode}: {said}")
    return done.stdout


def _json(root: Path, *args: str) -> Any:
    try:
        return json.loads(_gh(root, *args).decode("utf-8", "replace"))
    except json.JSONDecodeError as error:
        raise Refused(f"gh {' '.join(args)} printed no JSON: {error}") from None


def remote(root: Path) -> bool:
    """Whether the clone names a remote: with none, nothing is pushed and no CI runs."""
    return bool(config.git(root, "remote").strip())


def pushed(root: Path) -> str:
    """The commit the session branch was last pushed at, its upstream's."""
    try:
        return config.git(root, "rev-parse", "@{upstream}").strip()
    except Refused:
        raise Refused(
            "the session branch has no upstream, so no pushed commit is known: push it with `git push -u` first"
        ) from None


def listing(root: Path, sha: str) -> list[Item]:
    """Every workflow run whose head is the commit, every check run on it that no workflow job is, then every status.

    A commit status is GitHub's older interface beside check runs, which a CI outside Actions, a
    coverage service or a deploy preview may report through alone.
    """
    items = [
        Item(
            "run",
            str(run["databaseId"]),
            str(run.get("workflowName") or ""),
            str(run.get("status") or ""),
            run.get("conclusion") or None,
            run.get("startedAt") or run.get("createdAt"),
            run.get("updatedAt"),
            str(run.get("event") or ""),
        )
        for run in _json(root, "run", "list", "--commit", sha, "--limit", "200", "--json", RUN_FIELDS)
        if run.get("headSha") == sha
    ]
    pages = _json(
        root, "api", f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs?per_page=100", "--paginate", "--slurp"
    )
    for page in pages:
        for check in page.get("check_runs", []):
            app: Any = check.get("app") or {}
            if app.get("slug") == ACTIONS_APP:
                continue
            items.append(
                Item(
                    "check",
                    str(check["id"]),
                    str(check.get("name") or ""),
                    str(check.get("status") or ""),
                    check.get("conclusion") or None,
                    check.get("started_at"),
                    check.get("completed_at"),
                )
            )
    # The combined status holds each context's latest status: `pending`, or one of the conclusions
    # `success`, `failure` and `error` (GitHub's REST reference, "Commit statuses"). Each state is a
    # status of its own, created when it was set, so its times measure no run and none are kept.
    pages = _json(root, "api", f"repos/{{owner}}/{{repo}}/commits/{sha}/status?per_page=100", "--paginate", "--slurp")
    for page in pages:
        for status in page.get("statuses", []):
            state = str(status.get("state") or "")
            done = state != PENDING
            items.append(
                Item(
                    "status",
                    str(status["id"]),
                    str(status.get("context") or ""),
                    "completed" if done else state,
                    state if done else None,
                    None,
                    None,
                )
            )
    return items


def runs_no_ci(root: Path, sha: str, workflow: str | None) -> bool:
    """Whether the project runs no CI of its own at the commit: no workflow configured, and none in its tree."""
    return workflow is None and not config.git(root, "ls-tree", "-r", "--name-only", sha, "--", WORKFLOWS).strip()


def _waiting(items: list[Item], workflow: str | None, *, no_ci: bool) -> str | None:
    """What a read still waits on; None once every run, check and status exists and has concluded.

    A project running no CI waits on nothing once no run and no check exists, which a wait takes only
    after two reads a poll apart agree, so a check another service posts late is still met. A missing
    workflow run names the workflows that ran, since `ci.workflow` written as a file name never appears.
    """
    if not items:
        return None if no_ci else "no workflow run, check run or status exists"
    if workflow is not None and not any(item.kind == "run" and item.name == workflow for item in items):
        ran = ", ".join(sorted({item.name for item in items if item.kind == "run"})) or "none"
        return f"no {workflow} run exists (workflows run here: {ran}; `ci.workflow` takes a workflow's `name:`)"
    pending = [item for item in items if not item.done]
    if pending:
        named = ", ".join(f"{item.kind} {item.name} {item.status}" for item in pending)
        return f"{len(pending)} of {len(items)} not concluded: {named}"
    return None


def collect(
    root: Path, sha: str, workflow: str | None, wait: float | None, *, no_ci: bool = False
) -> tuple[list[Item], str | None]:
    """The commit's items once every one has concluded, and None; at the bound, those read and what waits.

    Without a bound a read that is not concluded is refused. Waiting, the set is taken once two reads
    a poll apart agree, since a check such as code scanning is created only after the run it reads
    has concluded; at the bound one concluded read is taken.
    """
    if wait is None:
        items = listing(root, sha)
        if (waiting := _waiting(items, workflow, no_ci=no_ci)) is not None:
            raise Refused(f"{sha[:9]}: {waiting}: read it once it has concluded, or with --wait")
        return items, None
    deadline = time.monotonic() + wait * 60
    failures, items, before = 0, list[Item](), None
    while True:
        try:
            items = listing(root, sha)
        except Refused as failed:
            failures += 1
            if failures >= GH_FAILURES or time.monotonic() >= deadline:
                raise Refused(f"gh failed while waiting on {sha[:9]}, {failures} time(s) running: {failed}") from None
            print(f"gh failed while waiting ({failures} of {GH_FAILURES}), asking again: {failed}", file=sys.stderr)
        else:
            failures = 0
            waiting = _waiting(items, workflow, no_ci=no_ci)
            now = frozenset(items) if waiting is None else None
            if now is not None and (now == before or time.monotonic() >= deadline):
                return items, None
            if waiting is not None and time.monotonic() >= deadline:
                return items, waiting
            before = now
        time.sleep(max(0.0, min(POLL_SECONDS, deadline - time.monotonic())))


# --- a failed log ---------------------------------------------------------------------------------


def patterns(settings: config.Ci) -> Patterns:
    def one(pattern: str | None) -> re.Pattern[str] | None:
        return None if pattern is None else re.compile(pattern)

    return Patterns(
        one(settings.verdict),
        one(settings.relay),
        tuple(re.compile(pattern) for pattern in settings.findings),
        tuple(re.compile(pattern) for pattern in settings.cases),
    )


def _first(held: Iterable[re.Pattern[str]], text: str) -> re.Match[str] | None:
    return next((found for pattern in held if (found := pattern.search(text))), None)


def parse(text: str, held: Patterns) -> dict[str, Job]:
    """Each failed job's findings in the order printed, its summary lines, and whether it is a relay."""
    jobs: dict[str, Job] = {}
    # Per job: whether a failing case waits for its error's first line, and every such line read, by
    # which the runner's own annotation repeating it is known as no finding of its own.
    waits: dict[str, bool] = {}
    errors: dict[str, set[str]] = {}
    for raw in text.splitlines():
        name, _, rest = raw.partition("\t")
        _, tab, rest = rest.partition("\t")
        if not tab:
            continue
        said = COLOUR_RE.sub("", STAMP_RE.sub("", rest)).rstrip()
        job = jobs.setdefault(name, Job())
        annotation = ANNOTATION_RE.match(said)
        if case := _first(held.cases, said):
            job.found.append(Line(case["name"], annotation=False))
            waits[name] = True
        elif waits.get(name) and said.strip() and not annotation:
            errors.setdefault(name, set()).add(said.strip())
            waits[name] = False
        elif annotation:
            words = annotation["text"].strip()
            if words in errors.get(name, ()):
                continue
            if held.relay is not None and held.relay.search(words):
                job.relay = True
            elif not EXIT_RE.match(words):
                job.found.append(Line(words, annotation=True))
        elif finding := _first(held.findings, said):
            job.found.append(Line(finding["text"], annotation=False))
        elif held.verdict is not None and (verdict := held.verdict.search(said)):
            job.verdicts.append(verdict["text"])
    return jobs


def _lines(job: Job) -> list[Line]:
    """The lines a job is judged by: its findings, else its summaries, else the stand-in for none."""
    if job.found:
        return list(dict.fromkeys(job.found))
    return [Line(text, annotation=False, generic=True) for text in job.verdicts] or [
        Line(UNRECOGNISED, annotation=False, generic=True)
    ]


def classify(jobs: dict[str, Job], rows: list[Expected], passed: frozenset[str]) -> Verdict:
    """Each line matched to the first red row of its job holding it, one line a row; the rest NEW.

    A red row no line matched is cleared where its job passed, and stays red otherwise.
    """
    taken: dict[int, str] = {}
    relays: list[str] = []
    new: list[tuple[str, str, bool]] = []
    for name, job in jobs.items():
        if not job.found and job.relay and len(jobs) > 1:
            relays.append(name)
            continue
        # Runner lines read since the last annotation; the annotation closing them is their verdict.
        unit = 0
        for line in _lines(job):
            if line.annotation and unit:
                unit = 0
                continue
            unit = 0 if line.annotation else unit + 1
            k = next(
                (k for k, row in enumerate(rows) if k not in taken and row.job == name and row.matches in line.text),
                None,
            )
            if k is None:
                new.append((name, line.text, line.generic))
            else:
                taken[k] = line.text
    expected = [(row, taken[k]) for k, row in enumerate(rows) if k in taken]
    left = [row for k, row in enumerate(rows) if k not in taken]
    cleared = [row for row in left if row.job in passed]
    return Verdict(expected, [row for row in left if row.job not in passed], cleared, relays, new)


def failed_jobs(root: Path, run: str, held: Patterns) -> tuple[dict[str, Job], frozenset[str]]:
    """A failed run's jobs as its log names them, and the names of its jobs that passed."""
    view = _json(root, "run", "view", run, "--json", "jobs")
    listed: list[Any] = view.get("jobs") or []
    passed = frozenset(str(job.get("name")) for job in listed if job.get("conclusion") in PASSED)
    jobs = parse(_gh(root, "run", "view", run, "--log-failed").decode("utf-8", "replace"), held)
    # A failed run whose log names no job, such as one that never started, is no failure to miss.
    return jobs or {f"run {run}": Job()}, passed


# --- the verbs ------------------------------------------------------------------------------------


def add(_verb: str, parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--wait",
        action="store_true",
        help="wait until every run, check and status of the commit exists and has concluded",
    )
    actions = parser.add_subparsers(dest="action", metavar="<action>")
    said = "record a failed run's NEW lines as expected reds"
    expect_parser = actions.add_parser("expect", help=said, description=said)
    expect_parser.add_argument("run", help="the run's id, as `orchestra ci` prints it")
    expect_parser.add_argument("--cleared-by", required=True, metavar="NAME", help="the landing that clears them")
    said = "run the configured gate in the primary checkout"
    gate_parser = actions.add_parser(GATE, help=said, description=said)
    gate_parser.add_argument(
        "--final", action="store_true", help="refuse a tree with changes, or a HEAD that is not its upstream"
    )
    gate_parser.add_argument("args", nargs="*", help="handed to the gate; put -- before the first that opens with -")


def run(args: argparse.Namespace) -> int:
    if args.action is not None and args.wait:
        raise Refused(f"--wait belongs to a read, not to `ci {args.action}`")
    if args.action == "expect":
        return expect(args)
    if args.action == GATE:
        return gate(args)
    return read(args)


def _item_line(item: Item) -> str:
    word = item.conclusion if item.done and item.conclusion else item.status
    label = f"{item.kind} {item.name}" + (f" {item.id} ({item.event})" if item.kind == "run" else "")
    took = item.minutes()
    return f"{word:<9} {label}" + (f", {took:.1f} min" if took is not None else "")


def _span(items: list[Item]) -> float | None:
    starts = [start for item in items if (start := _instant(item.started)) is not None]
    ends = [end for item in items if item.done and (end := _instant(item.ended)) is not None]
    return None if not starts or not ends else round((max(ends) - min(starts)).total_seconds() / 60, 1)


def read(args: argparse.Namespace) -> int:
    here = config.checkout()
    settings = config.load(here.root)
    root, workflow = here.root, settings.ci.workflow
    state = store.state_dir(held=here)
    sha = pushed(root)
    # Refused now rather than after a wait whose reading another session's lease would refuse to keep.
    store.check_lease(state)
    none = runs_no_ci(root, sha, workflow)
    items, waiting = collect(root, sha, workflow, settings.ci.wait_minutes if args.wait else None, no_ci=none)
    if waiting is not None:
        lines = [_item_line(item) for item in items]
        bound = f"{settings.ci.wait_minutes:g}"
        cli.emit(args, lines, f"not concluded at {sha[:9]} within {bound} min: {waiting}", tail=True)
        return REMAINS
    held = patterns(settings.ci)
    judged = [item for item in items if item.kind == "run" and item.name == workflow and item.conclusion != "cancelled"]
    failing = [item for item in judged if not item.passed]
    jobs: dict[str, Job] = {}
    passed: frozenset[str] = frozenset()
    for item in failing:
        found, ran = failed_jobs(root, item.id, held)
        passed |= ran
        for name, job in found.items():
            merged = jobs.setdefault(name, Job())
            merged.found += job.found
            merged.verdicts += job.verdicts
            merged.relay = merged.relay or job.relay
    rows = red(state)
    verdict = classify(jobs, rows, passed) if failing else None
    # Every run of the workflow passed: so did the job of every row.
    cleared = verdict.cleared if verdict is not None else (rows if judged else [])
    others = [item for item in items if not item.passed and item not in failing]
    new = verdict.new if verdict is not None else []
    conclusion: Conclusion = (
        "failure" if others or new else "expected" if failing else "none" if none and not items else "success"
    )
    lines = [_item_line(item) for item in items if item.passed or item in failing]
    if verdict is not None:
        lines += [f"expected  {row.job}: {row.matches}" for row, _ in verdict.expected]
        lines += [f"RED       {row.job}: {row.matches}  (no line matched; its job did not pass)" for row in verdict.red]
        lines += [f"relay     {name}: its findings are the other jobs' lines" for name in verdict.relays]
    lines += [f"CLEARED   {row.job}: {row.matches}" for row in cleared]
    lines += [_item_line(item) for item in others]
    lines += [f"NEW       {job}: {text}" for job, text, _ in new]
    failed = len(others) + len(failing)
    expected = len(verdict.expected) if verdict is not None else 0
    summary = (
        f"none at {sha[:9]}: no CI, since no workflow is configured or in the commit and no run, check or status exists"
        if conclusion == "none"
        else f"{conclusion} at {sha[:9]}: {len(items)} runs, checks and statuses, {failed} failed; {len(new)} NEW,"
        f" {expected} expected"
    )
    cli.emit(args, lines, summary, tail=True)
    _record(state, sha, cleared, Reading(sha, conclusion, _span(items), model.now()))
    log.record(state, "ci", summary)
    return DONE if conclusion != "failure" else REMAINS


def _record(state: Path, sha: str, cleared: list[Expected], reading: Reading) -> None:
    """The cleared rows marked with the commit that cleared them, and the reading kept."""
    gone = {(row.job, row.matches, row.time) for row in cleared}
    evidence = f"its job passed at {sha[:9]}"

    def change(rows: list[Expected], reads: list[Reading]) -> tuple[list[Expected], list[Reading]]:
        marked = [
            Expected(row.job, row.matches, row.run, row.commit, row.cleared_by, row.time, evidence)
            if row.cleared is None and (row.job, row.matches, row.time) in gone
            else row
            for row in rows
        ]
        return marked, [*reads, reading][-KEPT:]

    _update(state, change)


def expect(args: argparse.Namespace) -> int:
    here = config.checkout()
    settings = config.load(here.root)
    root, workflow, run_id = here.root, settings.ci.workflow, str(args.run)
    if workflow is None:
        raise Refused(f"no workflow's lines are read: set `ci.workflow` in {config.CONFIG_FILE.as_posix()}")
    if not run_id.isdigit():
        raise Refused(f"`{run_id}` is no run id: give the number `orchestra ci` prints after `run {workflow}`")
    if not str(args.cleared_by).strip():
        raise Refused("--cleared-by names the landing that clears these reds")
    state = store.state_dir(held=here)
    store.check_lease(state)
    sha = pushed(root)
    view = _json(root, "run", "view", run_id, "--json", "status,conclusion,workflowName,headSha")
    if view.get("workflowName") != workflow or view.get("headSha") != sha:
        shown = f"{view.get('workflowName')}'s for {str(view.get('headSha'))[:9]}"
        raise Refused(f"run {run_id} is {shown}, not {workflow}'s for the pushed commit {sha[:9]}")
    if view.get("status") != "completed":
        raise Refused(f"run {run_id} is {view.get('status')}: expect its lines once it has concluded")
    if view.get("conclusion") == "cancelled":
        raise Refused(f"run {run_id} was cancelled, so some of its jobs never ran: expect the run that replaced it")
    if view.get("conclusion") in PASSED:
        cli.emit(args, [], f"run {run_id} passed: nothing to expect")
        return DONE
    jobs, _ = failed_jobs(root, run_id, patterns(settings.ci))
    verdict = classify(jobs, red(state), frozenset())
    specific = [(job, text) for job, text, generic in verdict.new if not generic]
    written: list[Expected] = []

    def change(rows: list[Expected], reads: list[Reading]) -> tuple[list[Expected], list[Reading]]:
        held = {(row.job, row.matches) for row in rows if row.cleared is None}
        written.extend(
            Expected(job, text, run_id, sha, args.cleared_by) for job, text in specific if (job, text) not in held
        )
        return [*rows, *written], reads

    if specific:
        _update(state, change)
    # A job's summary, or the stand-in for a job no pattern read, would expect every later failure of that job.
    left = [(job, text) for job, text, generic in verdict.new if generic]
    lines = [f"RED       {row.job}: {row.matches}" for row in written]
    lines += [f"not written {job}: {text}  (no single finding: read the job's log)" for job, text in left]
    summary = f"{len(written)} expected red(s) recorded from run {run_id}, cleared by {args.cleared_by}"
    cli.emit(args, lines, summary + (f"; {len(left)} line(s) not written" if left else ""))
    return REMAINS if left else DONE


# --- the gate -------------------------------------------------------------------------------------


def _final(root: Path) -> None:
    """The ending's condition: nothing uncommitted, and HEAD the commit its upstream holds where a remote exists."""
    changed = config.git(root, "status", "--porcelain").splitlines()
    if changed:
        raise Refused(f"--final runs on a clean tree, and {len(changed)} path(s) are changed or untracked: commit them")
    if not remote(root):
        return
    head, upstream = config.git(root, "rev-parse", "HEAD").strip(), pushed(root)
    if head != upstream:
        raise Refused(f"--final runs on the pushed head, and HEAD {head[:9]} is not its upstream's {upstream[:9]}")


def _new_log(folder: Path, stem: str) -> tuple[Path, IO[bytes]]:
    """A log file of its own, a number added where a run in the same minute holds the name."""
    folder.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        path = folder / (f"{stem}.log" if n == 1 else f"{stem}-{n}.log")
        try:
            return path, path.open("xb")
        except FileExistsError:
            n += 1


def gate(args: argparse.Namespace) -> int:
    here = config.checkout()
    settings = config.load(here.root)
    if settings.gate is None:
        raise Refused(f"no gate is configured: set `gate.run` in {config.CONFIG_FILE.as_posix()}")
    root = here.root
    if args.final:
        _final(root)
    state = store.state_dir(held=here)
    # The gate's log is written outside the store's guarded writers, so the lease is read before it runs.
    store.check_lease(state)
    sha = config.git(root, "rev-parse", "HEAD").strip()
    found = config.program(settings.gate.run, root)
    if found is None:
        raise Refused(f"`gate.run` names {settings.gate.run[0]}, which is not found: install it, or correct the path")
    argv = (found, *settings.gate.run[1:], *args.args)
    path, out = _new_log(state / GATE, f"{sha[:9]}-{model.now()[11:16].replace(':', '')}")
    started = time.monotonic()
    with out:
        try:
            # The output goes to the file as bytes, and the exit code is the child's own, never a pipe's.
            code = process.run(argv, cwd=root, into=out).returncode
        except OSError as error:
            raise Refused(f"{argv[0]} cannot run: {error}") from None
    minutes = (time.monotonic() - started) / 60
    # The child's own exit code is the verdict, as it is for a CI step: a gate exiting 0 has passed.
    verdict = "green" if code == 0 else "red"
    cli.emit(
        args, [f"exit {code}, {minutes:.1f} min; its output is {path}"], f"gate {verdict} at {sha[:9]}: exit {code}"
    )
    shown = " ".join(args.args) or "none"
    log.record(state, GATE, f"{sha[:9]} {verdict}: exit {code}, {minutes:.1f} min, args {shown}")
    return DONE if code == 0 else REMAINS
