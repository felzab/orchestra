"""The configuration: every default where the file is missing, and a refusal naming the key at fault."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import write_config
from orchestra import Refused, config

FULL: dict[str, object] = {
    "$schema": "https://example.invalid/orchestra.schema.json",
    "stateRoot": "state",
    "base": "trunk",
    "models": {"default": {"model": "opus", "effort": "high"}, "mechanical": {"model": "sonnet"}},
    "contextCeiling": 300000,
    "queues": ["review"],
    "preflight": [{"run": ["docker", "info"], "why": "the database tier needs the engine"}],
    "land": {"steps": [{"kind": "check", "when": ["lib/"], "run": ["pyright"], "cwd": "lib"}]},
    "ci": {
        "workflow": "ci",
        "waitMinutes": 45,
        "verdict": r"^x (?P<text>.+)$",
        "findings": [r"^FAILED (?P<text>.+)$"],
        "cases": [r"^FAIL (?P<name>.+)$"],
    },
    "gate": {"run": ["bash", "./check.sh"]},
}


def test_the_default_state_root_is_the_platforms_folder_for_user_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never under `~/.claude`, whose every file an agent's write prompts for."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = tmp_path
    assert config.Config().state_root_path(tmp_path / "project") == base / "orchestra"


def test_a_full_file_is_read_whole(tmp_path: Path) -> None:
    write_config(tmp_path, FULL)
    held = config.load(tmp_path)
    assert held.base == "trunk"
    assert held.state_root_path(tmp_path) == tmp_path / "state"
    # Resolved, so no `..` reaches a path written into the settings, a brief or a prompt.
    assert config.Config(state_root="../state").state_root_path(tmp_path / "project") == tmp_path / "state"
    assert held.models == {"default": config.Model("opus", "high"), "mechanical": config.Model("sonnet")}
    assert held.context_ceiling == 300_000
    assert held.queues == ("review",)
    assert held.preflight == (config.Preflight(("docker", "info"), "the database tier needs the engine"),)
    assert held.land.steps == (config.Step("check", ("lib/",), ("pyright",), "lib"),)
    assert held.ci.workflow == "ci"
    assert held.ci.cases == (r"^FAIL (?P<name>.+)$",)
    assert held.ci.wait_minutes == 45.0
    assert held.gate == config.Gate(("bash", "./check.sh"))


@pytest.mark.parametrize(
    ("settings", "said"),
    [
        ({"cap": 20}, "`cap` is no key"),
        ({"preflight": [{"run": "docker info", "why": "x"}]}, '`preflight[0].run` must be a list, not "docker info"'),
        ({"ci": {"workflow": 5}}, "`ci.workflow` must be a string or null, not 5"),
        (
            {"models": {"default": {"model": "claude-opus-5-5"}}},
            '`models.default.model` must be one of "sonnet", "opus", "haiku", "fable", not "claude-opus-5-5"',
        ),
        ({"preflight": [{"why": "x"}]}, "`preflight[0]` needs the key `run`"),
        (
            {"land": {"steps": [{"kind": "check", "when": ["lib/"], "run": list[str]()}]}},
            "`land.steps[0].run` needs one item",
        ),
        ({"ci": {"verdict": "^x (?P<line>.+)$"}}, "`ci.verdict` needs a group named `text`"),
        ({"ci": {"findings": ["(unclosed"]}}, "`ci.findings[0]` is no regular expression"),
        ({"ci": {"waitMinutes": 0}}, "`ci.waitMinutes` must be more than 0"),
        ({"ci": {"waitMinutes": True}}, "`ci.waitMinutes` must be a number, not true"),
        ({"contextCeiling": 2.5e5}, "`contextCeiling` must be a whole number, not 250000.0"),
        (
            {"land": {"steps": [{"kind": "check", "when": ["src/", "lib/*.py"], "run": ["make"]}]}},
            "`land.steps[0].when[1]` is compared as written, so it takes no `*`",
        ),
        (
            {"land": {"steps": [{"kind": "regenerate", "when": ["api/"], "run": ["make"], "writes": ["gen/*.ts"]}]}},
            "`land.steps[0].writes[0]` is compared as written, so it takes no `*`",
        ),
    ],
    ids=[
        "unknown key",
        "wrong type",
        "wrong nested type",
        "a full model id",
        "missing key",
        "a command with no program",
        "missing group",
        "no regex",
        "a wait of no time",
        "a boolean for a number",
        "a fraction for a whole number",
        "a glob in a landing step's paths",
        "a glob in a regeneration's outputs",
    ],
)
def test_a_key_the_program_cannot_read_is_refused_by_its_path(
    tmp_path: Path, settings: dict[str, object], said: str
) -> None:
    write_config(tmp_path, settings)
    with pytest.raises(Refused) as refused:
        config.load(tmp_path)
    assert said in str(refused.value)


def test_a_path_below_a_folder_is_within_it_and_identical_only_to_the_folder_itself(tmp_path: Path) -> None:
    """`identical` asks whether two paths name one folder, where `within` takes every path below it too."""
    folder = tmp_path / "project"
    (folder / ".git").mkdir(parents=True)
    assert (config.within(folder / ".git", folder), config.identical(folder / ".git", folder)) == (True, False)
    # Another spelling of the folder is the folder.
    assert config.identical(tmp_path / "elsewhere" / ".." / "project", folder)
    assert not config.within(tmp_path, folder)
