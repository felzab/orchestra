"""The pages a model reads: each within its budget, its links and commands real, its frontmatter as decided.

`claude plugin validate --strict` proves only that a page's frontmatter parses, so the fields each
agent definition must carry, and the one skill the model may load, are held here.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from conftest import git
from orchestra import Refused, cli

ROOT = Path(__file__).resolve().parents[1]

# The most bytes each page under these folders may hold, counted as git stores it.
BUDGETS = {
    "skills/run/SKILL.md": 14288,
    "skills/run/start.md": 2597,
    "skills/run/brief.md": 4723,
    "skills/run/review.md": 5623,
    "skills/run/checkpoint.md": 2306,
    "skills/run/end.md": 2600,
    "skills/run/handoff.md": 2770,
    "skills/run/resume.md": 4445,
    "skills/status/SKILL.md": 500,
    "skills/estimate/SKILL.md": 3298,
    "skills/checkpoint/SKILL.md": 500,
    "skills/away/SKILL.md": 2366,
    "skills/back/SKILL.md": 1550,
    "skills/end/SKILL.md": 500,
    "skills/resume/SKILL.md": 500,
    "skills/plan/SKILL.md": 1913,
    "skills/agent/SKILL.md": 7130,
    "agents/writer.md": 6763,
    "agents/driver.md": 4311,
    "agents/researcher.md": 3844,
    "agents/auditor.md": 2734,
    "agents/verifier.md": 3500,
    **{
        f"lenses/{name}.md": 1500
        for name in (
            "security",
            "data",
            "contract",
            "tests",
            "document",
            "prompt",
            "conformance",
            "necessity",
            "cost",
            "essential",
            "practice",
            "use",
            "operations",
            "accessibility",
        )
    },
    # Its sixth item names the plugin's own merge subjects as no hit, which outgrew the shared budget.
    "lenses/disclosure.md": 1672,
    # Its checklist asks after every kind of saving the owner named: timing, scoping, batching and the rest.
    "lenses/estimate.md": 2114,
}
BUDGETED = ("skills/", "agents/", "lenses/")
# Every folder whose pages name commands and links: the plugin's own, and the documents beside them.
READ = (*BUDGETED, "docs/")
# The one skill an agent definition preloads, which `disable-model-invocation` would keep from loading.
PRELOADED = "agent"

# Rules a session showed missing, each held at the page whose reader acts on it, so a trim that drops one fails.
RULES = {
    "skills/agent/SKILL.md": (
        "A turn you end to wait on your own background command or monitor is a stop",
        "so append no `; echo exit=$?`",
        "in a worktree, run each command plain from its root, with no `;`, `$?`, `$VAR`, `$(...)`, loop, heredoc"
        " or `git -C`",
        "A worktree with no change goes at the stop, and only commits land, so a writer commits what is done before it"
        " stops",
        # An earlier rewording gave the handover to the driver alone, and a writer with nothing done lost its own.
        "and a writer with nothing yet to commit cannot be resumed",
        "since a successor takes the answer",
    ),
    "skills/run/SKILL.md": (
        "a dispatch waits for nothing but its `after:` edges and its files",
        "To give a running writer a file outside its `Owns:`, edit its brief's `Owns:`, run `orchestra brief",
    ),
}
# Claude Code re-attaches each invoked skill's body after a compaction, cut to its first 5,000 tokens, while a
# tool result is summarized (https://code.claude.com/docs/en/skills, "Skill content lifecycle"), so `!` output
# is sent again with every call. The run page alone injects, its output last so the cut drops it first; every
# other command page runs its command as a step.
INJECTING = ("skills/run/SKILL.md",)
# An injection: `!` and a backtick at a line's start or after whitespace, where Claude Code runs it.
INJECTION_RE = re.compile(r"(?:^|\s)!`")

# The fields every agent definition carries, and those a committing agent adds. The validator passes a
# misspelt field and Claude Code ignores it: a `tools:` line lost that way hands an auditor a shell, and
# a writer every tool of the session, such as a 48,000-character schema it never calls.
SHARED = frozenset({"name", "description", "tools", "background", "autoCompactWindow", "skills"})
COMMITTING = frozenset({"isolation"})

FIELD_RE = re.compile(r"(?P<key>[A-Za-z_-]+):(?: (?P<value>.+))?")
ITEM_RE = re.compile(r"  - (?P<value>\S.*)")
QUOTED_RE = re.compile(r'"(?P<text>[^"\\]*)"')
# A character that opens a YAML construct, and a sequence that ends a plain value early.
INDICATORS = "[]{}>|*&!%@`'\"#,?"
BREAK_RE = re.compile(r": | #|:$")
LINK_RE = re.compile(r"\[[^\]]*\]\((?P<target>[^)\s]+)\)")
ROOTED_RE = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/(?P<path>[\w./-]*)")
SPAN_RE = re.compile(r"`(?P<command>orchestra [^`]+)`")
# A shell operator ends the command a page names; what follows is the shell's.
OPERATORS = frozenset({"||", "&&", "|", ";"})


def _sizes() -> dict[str, int]:
    """Each Markdown file of the working tree that git tracks or would add, sized as git stores it.

    The files are read as they are on disk, so a page grown or added is judged before it is staged; a
    CRLF is counted as the LF git stores, so no checkout's line endings change the size.
    """
    listed = git(ROOT, "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.md").splitlines()
    on_disk = (path for path in dict.fromkeys(listed) if (ROOT / path).is_file())
    return {path: len((ROOT / path).read_bytes().replace(b"\r\n", b"\n")) for path in on_disk}


SIZES = _sizes()
PAGES = sorted(path for path in SIZES if "/" not in path or path.startswith(READ))


def _text(page: str) -> str:
    return (ROOT / page).read_bytes().decode("utf-8")


def _scalar(value: str) -> str:
    """A value as YAML reads it, for the two shapes these pages use: plain, and double-quoted."""
    if quoted := QUOTED_RE.fullmatch(value):
        return quoted["text"]
    if value[0] in INDICATORS or BREAK_RE.search(value):
        raise ValueError(f"`{value}` is no plain value: quote it")
    return value


def frontmatter(text: str) -> dict[str, str | list[str]]:
    """A page's frontmatter: `key: value` lines, and a key followed by `  - item` lines; any other line is refused."""
    lines = text.split("\n")
    if lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError("no frontmatter between two `---` lines")
    held: dict[str, str | list[str]] = {}
    items: list[str] | None = None
    for line in lines[1 : lines.index("---", 1)]:
        if (item := ITEM_RE.fullmatch(line)) and items is not None:
            items.append(_scalar(item["value"]))
        elif (field := FIELD_RE.fullmatch(line)) and field["key"] not in held:
            items = None if field["value"] else []
            held[field["key"]] = _scalar(field["value"]) if items is None else items
        else:
            raise ValueError(f"`{line}` is neither a new `key: value` nor a list item under a key")
    return held


def test_each_page_is_within_its_budget() -> None:
    pages = {page for page in SIZES if page.startswith(BUDGETED)}
    assert pages == set(BUDGETS), "every page has a budget, and every budget a page"
    assert {page: SIZES[page] for page in pages if SIZES[page] > BUDGETS[page]} == {}


@pytest.mark.parametrize("page", PAGES)
def test_every_file_a_page_points_at_exists(page: str) -> None:
    text = _text(page)
    relative = [found["target"].partition("#")[0] for found in LINK_RE.finditer(text)]
    missing = [
        target
        for target in relative
        if target and "://" not in target and not (ROOT / page).parent.joinpath(target).exists()
    ]
    rooted = [found["path"].rstrip(".") for found in ROOTED_RE.finditer(text)]
    assert missing + [path for path in rooted if not (ROOT / path).exists()] == []


def test_each_phase_file_is_linked_from_the_core() -> None:
    linked = {found["target"] for found in LINK_RE.finditer(_text("skills/run/SKILL.md"))}
    beside = {Path(page).name for page in SIZES if page.startswith("skills/run/")} - {"SKILL.md"}
    assert beside
    assert beside <= linked


def commands(text: str) -> list[str]:
    """Each `orchestra ...` command a page names: a code span, an injection, or a line of a fenced block."""
    found: list[str] = []
    for k, part in enumerate(re.split(r"^```.*$", text, flags=re.MULTILINE)):
        if k % 2:
            lines = (line.strip().removeprefix("$ ") for line in part.splitlines())
            found += [line for line in lines if line.startswith("orchestra ")]
        else:
            found += [span["command"] for span in SPAN_RE.finditer(part)]
    return found


def parses(command: str) -> bool:
    """Whether the program's own parser takes the command, read up to its first shell operator."""
    words = shlex.split(command)[1:]
    words = words[: next((k for k, word in enumerate(words) if word in OPERATORS), len(words))]
    try:
        top, _ = cli.parser(words[0] if words and words[0] in cli.VERBS else None)
        top.parse_args(words)
    except (SystemExit, Refused):
        return False
    return True


@pytest.mark.parametrize("page", PAGES)
def test_every_command_a_page_names_parses(page: str) -> None:
    assert [command for command in commands(_text(page)) if not parses(command)] == []


def test_the_core_and_the_owner_skills_name_commands() -> None:
    """The check above passes on a page it finds no command in, so the finder is held to the pages that name some."""
    assert "orchestra status" in commands(_text("skills/status/SKILL.md"))
    assert "orchestra land NAME --preview" in commands(_text("skills/run/SKILL.md"))
    assert not parses("orchestra land NAME --dry-run")


@pytest.mark.parametrize(("page", "rule"), [(page, rule) for page, rules in RULES.items() for rule in rules])
def test_each_page_keeps_its_rule(page: str, rule: str) -> None:
    assert rule in _text(page)


@pytest.mark.parametrize("page", INJECTING)
def test_a_page_s_injected_output_comes_last(page: str) -> None:
    blocks = [block.strip() for block in _text(page).replace("\r\n", "\n").split("\n\n") if block.strip()]
    injected = [k for k, block in enumerate(blocks) if block.startswith("!`")]
    assert injected
    assert injected == list(range(len(blocks) - len(injected), len(blocks)))


@pytest.mark.parametrize(
    "page",
    [page for page in PAGES if page.startswith("skills/") and page.endswith("/SKILL.md") and page not in INJECTING],
)
def test_any_other_skill_runs_its_command_as_a_step(page: str) -> None:
    assert INJECTION_RE.search(_text(page)) is None


@pytest.mark.parametrize("page", [page for page in PAGES if page.startswith("skills/") and page.endswith("/SKILL.md")])
def test_only_the_preloaded_skill_is_open_to_the_model(page: str) -> None:
    # A skill the model may invoke costs every session its description; one an agent preloads must stay loadable.
    wanted = None if Path(page).parent.name == PRELOADED else "true"
    assert frontmatter(_text(page)).get("disable-model-invocation") == wanted


@pytest.mark.parametrize("page", [page for page in PAGES if page.startswith("agents/")])
def test_an_agent_definition_holds_its_type_s_fields_and_no_other(page: str) -> None:
    fields = frontmatter(_text(page))
    # No definition names a model, which the project's configuration chooses for each agent.
    assert set(fields) == SHARED | (COMMITTING if Path(page).stem in {"writer", "driver"} else frozenset())
    assert fields["skills"] == [f"orchestra:{PRELOADED}"]
    # A plain integer: Claude Code reads `500k` as 500, outside the field's range, and ignores the field.
    assert fields["autoCompactWindow"] == "500000"
    assert f"skills/{PRELOADED}/SKILL.md" in SIZES
