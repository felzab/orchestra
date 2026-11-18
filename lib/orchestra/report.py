"""A report: the file every agent writes, read by its fixed grammar, so a finding is read and never guessed.

The grammar, each heading once and in this order:

    # <NAME> · <type> · round <n>
    ## Summary              the status line, one sentence, the commit line, one line per finding
                            repeating its heading, `Not verified: ...`, `Wrong in the brief: ...`
    ## Done
    ## Findings             `### F<n> · <defect or optional> · <silent or loud> · <anchor>`, its first
                            line `Harm: ... Likelihood: ...`; or the line `none`
    ## Not verified
    ## Wrong in the brief
    ## Outside the scope

A verifier's report holds `## Verdicts` in place of `## Findings`, each `### <NAME>-F<n> · <verdict>`.
`check` names every way a file breaks the grammar, each by its line, for the hook that refuses an
agent's stop while its report would be misread.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from orchestra import Refused, config, model, store

SEP = " · "
Status = Literal["DONE", "DONE-WITH-CONCERNS", "BLOCKED", "NEEDS-ANSWER"]
STATUSES: tuple[Status, ...] = typing.get_args(Status)
# The two statuses an agent stops on with work left, each carrying its reason after a colon.
STOPPED: frozenset[str] = frozenset({"BLOCKED", "NEEDS-ANSWER"})
Kind = Literal["defect", "optional"]
VerdictWord = Literal["CONFIRMED", "CORRECTED", "REFUTED", "NOT JUDGED"]
VERDICTS: tuple[VerdictWord, ...] = typing.get_args(VerdictWord)
TYPES: tuple[str, ...] = typing.get_args(model.AgentType)
# The coordinator reads the summary alone before it opens a finding, so its lines but the one per
# finding or verdict stay near one kilobyte.
SUMMARY_BYTES = 1500
# The types whose work is commits in a worktree; the others commit nothing.
COMMITTING: frozenset[str] = frozenset({"writer", "driver"})
HEADINGS = ("Summary", "Done", "Findings", "Not verified", "Wrong in the brief", "Outside the scope")
# The heading levels of a section and of an item, a finding or a verdict, within one.
SECTION, ITEM = 2, 3
# The summary's fixed lines: the status, the sentence, the commit line and the two closing lines.
SUMMARY_LINES = 5
# How many lines of `git status --porcelain` a refusal quotes.
QUOTED = 3

TITLE_RE = re.compile(rf"# (?P<name>{model.NAME_RE.pattern}) · (?P<type>\S+) · round (?P<round>[1-9][0-9]*)")
STATUS_RE = re.compile(r"(?P<status>[A-Z][A-Z-]*)(?:: (?P<reason>\S.*))?")
COMMITS_RE = re.compile(r"Commits: (?P<count>0|[1-9][0-9]*), tip (?P<tip>[0-9a-f]{7,40})")
NO_COMMITS = "Commits: none"
FINDING_RE = re.compile(r"F(?P<n>[1-9][0-9]*) · (?P<kind>defect|optional) · (?P<noise>silent|loud) · (?P<anchor>\S.*)")
VERDICT_RE = re.compile(rf"(?P<row>{model.ROW_RE.pattern}) · (?P<verdict>{'|'.join(VERDICTS)})")
HARM_RE = re.compile(r"Harm: \S.* Likelihood: \S.*")
TAILS = ("Not verified: ", "Wrong in the brief: ")
HEADING_RE = re.compile(r"(?P<marks>#{2,3}) (?P<words>.*)")
# A fenced block's lines are its content, never a heading: a report often quotes Markdown.
FENCE_RE = re.compile(r" {0,3}(?P<fence>`{3,}|~{3,})")
# A finding written under any other heading, which no verb would read as one.
STRAY_RE = re.compile(r"F[0-9]+\b")
FINDING_FORM = "`### F<n> · <defect or optional> · <silent or loud> · <path :: anchor>`"


@dataclass(frozen=True)
class Finding:
    number: int
    kind: Kind
    silent: bool
    anchor: str
    # Every line under the heading, its `Harm: ... Likelihood: ...` line first.
    body: str
    line: int

    @property
    def heading(self) -> str:
        return SEP.join((f"F{self.number}", self.kind, "silent" if self.silent else "loud", self.anchor))


@dataclass(frozen=True)
class Verdict:
    row: str
    verdict: VerdictWord
    # The proof; a CORRECTED verdict's opens with the corrected heading line.
    body: str
    line: int

    @property
    def heading(self) -> str:
        return f"{self.row}{SEP}{self.verdict}"

    @property
    def first(self) -> str:
        return self.body.split("\n", 1)[0]


@dataclass(frozen=True)
class Report:
    name: str
    type: str
    round: int
    status: Status
    # What blocks a BLOCKED agent, or a NEEDS-ANSWER agent's question.
    reason: str | None
    # A writer's or a driver's commit count and tip; None for a type that commits nothing.
    commits: int | None
    tip: str | None
    findings: tuple[Finding, ...]
    verdicts: tuple[Verdict, ...]


@dataclass(frozen=True)
class Violation:
    """One way a report breaks the grammar: the line at fault, None where the fault is an absence."""

    line: int | None
    text: str
    problem: str

    def __str__(self) -> str:
        return self.problem if self.line is None else f"line {self.line} `{self.text}`: {self.problem}"


def stem(name: str, round_: int = 1) -> str:
    """A round's report name and its rows' prefix: the agent's name, then `.<round>` from the second round."""
    return name if round_ == 1 else f"{name}.{round_}"


@dataclass(frozen=True)
class _Summary:
    status: Status = "DONE"
    reason: str | None = None
    count: int | None = None
    tip: str | None = None
    # Each item line by the id it opens with: its index and its text.
    named: dict[str, tuple[int, str]] = field(default_factory=dict[str, tuple[int, str]])


def path(state: Path, name: str, round_: int = 1) -> Path:
    return state / store.REPORTS / f"{stem(name, round_)}.md"


def row(name: str, round_: int, number: int) -> str:
    return f"{stem(name, round_)}-F{number}"


def located(row_id: str) -> tuple[str, int, int]:
    """The agent, the round and the finding number a row id names; refused where it is no row id."""
    held = model.ROW_RE.fullmatch(row_id)
    if held is None:
        raise Refused(f"`{row_id}` is no row id: <NAME>-F<n>, or <NAME>.<round>-F<n> from the second round")
    return held["agent"], int(held["round"] or 1), int(held["n"])


def _named(file: Path) -> tuple[str | None, int | None]:
    """The agent and the round a report's file name declares; None for a name of no report."""
    held = model.ROW_RE.fullmatch(f"{file.stem}-F1")
    return (held["agent"], int(held["round"] or 1)) if held else (None, None)


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """Each second- and third-level heading outside a fenced block: its index, its level and its words."""
    found: list[tuple[int, int, str]] = []
    fence: str | None = None
    for k, line in enumerate(lines):
        if fence is not None:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*", line):
                fence = None
        elif opened := FENCE_RE.match(line):
            fence = opened["fence"]
        elif heading := HEADING_RE.fullmatch(line.rstrip()):
            found.append((k, len(heading["marks"]), heading["words"]))
    return found


class _Reader:
    """One report read through: what it holds, and every fault met, each naming its line."""

    def __init__(self, text: str, agent_type: str | None, name: str | None, round_: int | None) -> None:
        self.lines = text.removeprefix("\N{BYTE ORDER MARK}").replace("\r\n", "\n").split("\n")
        self.said: list[Violation] = []
        self.name = name
        self.round = round_
        self.type = agent_type

    def fault(self, k: int | None, problem: str) -> None:
        self.said.append(Violation(None if k is None else k + 1, "" if k is None else self.lines[k], problem))

    def filled(self, start: int, end: int) -> list[tuple[int, str]]:
        return [(k, self.lines[k].rstrip()) for k in range(start, end) if self.lines[k].strip()]

    def title(self) -> tuple[str, int]:
        held = TITLE_RE.fullmatch(self.lines[0].rstrip())
        if held is None:
            self.fault(0, "the first line is no title: `# <NAME> · <type> · round <n>`")
            return self.name or "", 0
        if held["type"] not in TYPES:
            self.fault(0, f"`{held['type']}` is no agent type: one of {', '.join(TYPES)}")
        elif self.type is not None and held["type"] != self.type:
            self.fault(0, f"the title names {article(held['type'])}, and this agent is {article(self.type)}")
        if self.name is not None and held["name"] != self.name:
            self.fault(0, f"the title names {held['name']}, and this is {self.name}'s report")
        if self.round is not None and int(held["round"]) != self.round:
            self.fault(0, f"the title names round {held['round']}, and this is round {self.round}'s report")
        self.type = self.type or held["type"]
        return held["name"], int(held["round"])

    def sections(self, heads: list[tuple[int, int, str]], wanted: tuple[str, ...]) -> dict[str, tuple[int, int]]:
        """Each heading's span, from its line to the next second-level heading; every misplaced one named."""
        tops = [k for k, level, _ in heads if level == SECTION]
        spans: dict[str, tuple[int, int]] = {}
        reached = -1
        for k, level, words in heads:
            if level != SECTION:
                continue
            if words not in wanted:
                self.fault(k, f"no heading of {article(self.type or 'agent')}'s report: they are " + _listed(wanted))
            elif words in spans:
                self.fault(k, f"a second `## {words}`")
            else:
                if wanted.index(words) < reached:
                    self.fault(k, f"stands after `## {wanted[reached]}`: the headings run " + _listed(wanted))
                reached = max(reached, wanted.index(words))
                spans[words] = (k, next((top for top in tops if top > k), len(self.lines)))
        for words in wanted:
            if words not in spans:
                self.fault(None, f"`## {words}` is missing: the headings run " + _listed(wanted))
        return spans

    def summary(self, start: int, end: int, items: re.Pattern[str]) -> _Summary:
        """The summary's parts, each line of it read in its place."""
        rows = self.filled(start + 1, end)
        # Only the lines around the findings count, so a review with many findings still fits.
        fixed = "\n".join(line for _, line in rows if not items.fullmatch(line))
        if (size := len(fixed.encode("utf-8"))) > SUMMARY_BYTES:
            self.fault(
                start, f"the summary's fixed lines hold {size} bytes, over {SUMMARY_BYTES}: keep each to its part"
            )
        if len(rows) < SUMMARY_LINES:
            self.fault(
                start,
                f"the summary holds {len(rows)} line(s), and it needs the status, one sentence, the commit line,"
                " `Not verified:` and `Wrong in the brief:`",
            )
            return _Summary()
        first, sentence, commits, *middle, verified, wrong = rows
        if sentence[1].startswith("Commits:"):
            self.fault(sentence[0], "the sentence saying what was done is missing before the commit line")
        for (k, line), tail in ((verified, TAILS[0]), (wrong, TAILS[1])):
            if not (line.startswith(tail) and line.removeprefix(tail).strip()):
                self.fault(k, f"the summary's line here is `{tail}<one line, or nothing>`")
        status, reason = self.status(*first)
        count, tip = self.commits(*commits)
        return _Summary(status, reason, count, tip, self.named(middle, items))

    def status(self, k: int, line: str) -> tuple[Status, str | None]:
        held = STATUS_RE.fullmatch(line)
        if held is None or held["status"] not in STATUSES:
            self.fault(k, "the summary's first line is no status: one of " + ", ".join(STATUSES))
            return "DONE", None
        status, reason = typing.cast("Status", held["status"]), held["reason"]
        if status in STOPPED and reason is None:
            self.fault(k, f"{status} carries its reason after a colon on the same line")
        return status, reason

    def commits(self, k: int, line: str) -> tuple[int | None, str | None]:
        if self.type in COMMITTING:
            if made := COMMITS_RE.fullmatch(line):
                return int(made["count"]), made["tip"]
            self.fault(k, f"{article(self.type)}'s third summary line is `Commits: <count>, tip <short sha>`")
        elif self.type is not None and line != NO_COMMITS:
            self.fault(k, f"{article(self.type)} commits nothing, so its third summary line is `{NO_COMMITS}`")
        return None, None

    def named(self, middle: list[tuple[int, str]], items: re.Pattern[str]) -> dict[str, tuple[int, str]]:
        """Each item line by the id it opens with."""
        named: dict[str, tuple[int, str]] = {}
        for k, line in middle:
            held = items.fullmatch(line)
            if held is None:
                self.fault(k, "no item line: each line here repeats one heading under `## Findings` or `## Verdicts`")
            elif held[1] in named:
                self.fault(k, f"a second summary line for {held[1]}")
            else:
                named[held[1]] = (k, line)
        return named

    def items(self, start: int, end: int, subs: list[int], section: str) -> list[tuple[int, int]]:
        """Each `###` heading's span in the section; a fault for text before the first, or for no item and no `none`."""
        if not subs:
            if [line for _, line in self.filled(start + 1, end)] != ["none"]:
                self.fault(start, f"`## {section}` holds no `###` heading, so it holds the line `none`")
            return []
        if stray := self.filled(start + 1, subs[0]):
            self.fault(stray[0][0], "stands before the first `###` heading, so it belongs to no item")
        return list(zip(subs, [*subs[1:], end], strict=True))

    def body(self, start: int, end: int) -> str:
        return "\n".join(self.lines[start + 1 : end]).strip("\n")

    def findings(self, subs: list[tuple[int, int]]) -> list[Finding]:
        found: list[Finding] = []
        expected = 1
        for k, end in subs:
            held = FINDING_RE.fullmatch(self.lines[k].rstrip().removeprefix("### "))
            if held is None:
                self.fault(k, f"the finding heading does not parse: {FINDING_FORM}")
                continue
            number = int(held["n"])
            if number != expected:
                self.fault(k, f"F{number} where F{expected} is next: the numbers start at 1 and do not skip")
            expected = number + 1
            rows = self.filled(k + 1, end)
            if not rows or not HARM_RE.fullmatch(rows[0][1]):
                self.fault(k, "its first line is no `Harm: <the concrete harm>. Likelihood: <how likely, and why>.`")
                continue
            kind = typing.cast("Kind", held["kind"])
            body = self.body(k, end)
            found.append(Finding(number, kind, held["noise"] == "silent", held["anchor"], body, k + 1))
        return found

    def verdicts(self, subs: list[tuple[int, int]]) -> list[Verdict]:
        found: list[Verdict] = []
        for k, end in subs:
            held = VERDICT_RE.fullmatch(self.lines[k].rstrip().removeprefix("### "))
            if held is None:
                self.fault(k, "the verdict heading does not parse: `### <NAME>-F<n> · " + " or ".join(VERDICTS) + "`")
                continue
            if any(verdict.row == held["row"] for verdict in found):
                self.fault(k, f"a second verdict on {held['row']}")
                continue
            verdict = Verdict(held["row"], typing.cast("VerdictWord", held["verdict"]), self.body(k, end), k + 1)
            if not verdict.body.strip():
                self.fault(k, "the verdict names no proof: the command and its output, or the passage")
            elif verdict.verdict == "CORRECTED" and (
                not (corrected := FINDING_RE.fullmatch(verdict.first.rstrip())) or corrected["n"] != held["n"]
            ):
                self.fault(k, f"a CORRECTED verdict's body opens with the corrected heading line, `F{held['n']} · ...`")
            found.append(verdict)
        return found


def article(word: str | None) -> str:
    """The word after its indefinite article: `an auditor`, `a writer`."""
    return f"{'an' if (word or ' ')[0] in 'aeiou' else 'a'} {word}"


def _listed(wanted: tuple[str, ...]) -> str:
    return ", ".join(f"`## {words}`" for words in wanted)


def parse(
    text: str, agent_type: str | None = None, name: str | None = None, round_: int | None = None
) -> tuple[Report | None, list[Violation]]:
    """The report the text holds, None where it breaks the grammar; every violation, each naming its line.

    `agent_type`, `name` and `round_` are what the title must declare; each left out, the title declares it.
    """
    reader = _Reader(text, agent_type, name, round_)
    named, round_ = reader.title()
    verifier = reader.type == "verifier"
    items = "Verdicts" if verifier else "Findings"
    wanted = tuple(items if words == "Findings" else words for words in HEADINGS)
    heads = _headings(reader.lines)
    spans = reader.sections(heads, wanted)
    for words, (start, end) in spans.items():
        if words not in {"Summary", items} and not reader.filled(start + 1, end):
            reader.fault(start, f"`## {words}` holds nothing: write `nothing` where nothing applies")
    for k, level, words in heads:
        inside = items in spans and spans[items][0] < k < spans[items][1]
        if level == ITEM and not inside and STRAY_RE.match(words):
            reader.fault(k, f"a finding outside `## {items}` is read by nothing: move it under `## {items}`")
    found: list[Finding] = []
    verdicts: list[Verdict] = []
    # Each item heading's id, the word it opens with, refused or not.
    ids: set[str] = set()
    if items in spans:
        start, end = spans[items]
        subs = [k for k, level, _ in heads if level == ITEM and start < k < end]
        ids = {words.split(" ", 1)[0] for k, _, words in heads if k in subs}
        bounded = reader.items(start, end, subs, items)
        if verifier:
            verdicts = reader.verdicts(bounded)
        else:
            found = reader.findings(bounded)
    parts = _Summary()
    if "Summary" in spans:
        start, end = spans["Summary"]
        parts = reader.summary(start, end, re.compile(r"(\S+) · .*" if verifier else r"(F[0-9]+) · .*"))
        headings = {item.heading.split(SEP, 1)[0]: (item.line, item.heading) for item in [*found, *verdicts]}
        # A heading refused already has its fault, so its summary line gets no second one.
        _match(
            reader.fault,
            {key: held for key, held in parts.named.items() if key in headings or key not in ids},
            headings,
        )
    if reader.said:
        return None, reader.said
    held = (parts.status, parts.reason, parts.count, parts.tip, tuple(found), tuple(verdicts))
    return Report(named, reader.type or "", round_, *held), []


def _match(
    fault: Callable[[int | None, str], None], said: dict[str, tuple[int, str]], headings: dict[str, tuple[int, str]]
) -> None:
    """Each summary item line equal to its heading's text, and each heading with its summary line."""
    for key, (line, heading) in headings.items():
        if key not in said:
            fault(line - 1, f"{key} has no line in the summary: repeat its heading's text there")
        elif said[key][1] != heading:
            fault(said[key][0], f"differs from its heading at line {line}: `{heading}`")
    for key, (k, _) in said.items():
        if key not in headings:
            fault(k, f"{key} is no heading of this report")


def _parse_file(file: Path, text: str, agent_type: str | None) -> tuple[Report | None, list[Violation]]:
    """The text parsed against the agent and the round its file name declares."""
    name, round_ = _named(file)
    if name is None:
        return None, [Violation(None, "", f"{file.name} is no report's name: `<NAME>.md`, or `<NAME>.<round>.md`")]
    return parse(text, agent_type, name, round_)


def read(file: Path, agent_type: str | None = None) -> Report:
    """The report at the path; refused naming every line that breaks the grammar."""
    try:
        text = file.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise Refused(f"no report at {file}") from None
    except UnicodeDecodeError as error:
        raise Refused(f"{file} is no UTF-8 text: {error}") from None
    report, said = _parse_file(file, text, agent_type)
    if report is None:
        raise Refused(f"{file} breaks the report's grammar:\n" + "\n".join(f"  {violation}" for violation in said))
    return report


def check(file: Path, agent_type: str, worktree: Path | None) -> list[Violation]:
    """Every violation of the report's grammar at the path, and for a writer or a driver an unclean worktree.

    The `SubagentStop` hook calls it and refuses the stop while it returns anything. `worktree` is the
    agent's own checkout; None skips the worktree's check.
    """
    try:
        text = file.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return [Violation(None, "", f"no report at {file}: create it with its six headings first")]
    except UnicodeDecodeError as error:
        return [Violation(None, "", f"{file} is no UTF-8 text: {error}")]
    _, said = _parse_file(file, text, agent_type)
    if agent_type in COMMITTING and worktree is not None:
        try:
            changed = config.git(worktree, "status", "--porcelain").splitlines()
        except Refused as refused:
            said.append(Violation(None, "", str(refused)))
        else:
            if changed:
                shown = "; ".join(f"`{line}`" for line in changed[:QUOTED])
                said.append(
                    Violation(
                        None,
                        "",
                        f"`git status --porcelain` in {worktree} prints {len(changed)} line(s), {shown}:"
                        " commit the work or restore it",
                    )
                )
    return said
