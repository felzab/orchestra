# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-11-18

The fixes from the first project session that ran on the plugin.

### Added

- `orchestra init --base <branch>` names the branch one session is cut from, where it is not the
  configuration's `base`. init now refuses a base this clone has no branch of, naming the
  `git branch` command that makes one from origin's, and a base that shares no commit with `HEAD`.
- A driver can be briefed with no `--slice`; a driver on no slice is no drift in `orchestra status`.
- The `tests` lens asks which added behaviour no case reaches, and is due on every change but
  Markdown.
- The review gives a change to a rule or page meant to change what agents do to two researchers,
  one shown the change and one denied it: alike answers are a finding against it.

### Changed

- `orchestra status` marks a row for a re-plan once it has gone to three distinct agents, and no
  longer counts routes to a queue or repeated routes to one agent.
- The turn-end hook leaves a disclosure-only read out of the reports that make a checkpoint due,
  and plain notes out of the log entries that make `NOW.md` stale.
- The coordinator's pages:
  - Run the estimate before the first dispatch.
  - Ask the owner before cutting a branch where another live session shares the checkout.
  - Read `get_usage` in the desktop app where `orchestra` reads usage unknown.
  - Wait on no agent with a blocking read.
  - Change the away mode only on the owner's own words.
  - Have a fix to landed work written by a new writer.
  - Name each page the owner should look at with its link.
  - Leave whether an agent the owner stopped resumes to the owner.
- The disclosure lens reads, at a push after the first, only the commits since the upstream.
- The writer's page checks that the index holds one commit's files alone before rewording it, and
  the agents' shared page says to pass text holding a backslash by file.

### Fixed

- `orchestra diff --tree` checks out a tree whose paths pass Windows' 260 characters, as they do
  under the desktop app's redirected `AppData\Local`.

## [0.1.0] - 2026-11-18

### Added

- The `orchestra` program and its hooks, five agent types, sixteen lenses and nine slash commands.

[unreleased]: https://github.com/felzab/orchestra/commits/main
[0.1.0]: https://github.com/felzab/orchestra/commit/364d4ec
