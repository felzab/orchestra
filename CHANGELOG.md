# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-11-18

The fixes from the first project session that ran on the plugin.

### Added

- `orchestra init <session> --base <branch>` names the branch a session is cut from. Without it,
  init takes the base of the programme's newest session made in this checkout, else the
  configuration's `base`, and its last line names the base, where it came from and its commit.
- A driver can be briefed with no `--slice`; a driver on no slice is no drift in `orchestra status`.
- The `tests` lens asks whether a case reaches each added behaviour, and is due on every change but
  Markdown.
- The review tests a change to a rule or page meant to change what agents do: one small task goes to
  two researchers, the text before the change pasted into one brief and the text after into the
  other, and alike answers go to the owner. A file every agent loads by itself is tested this way on
  the writer's branch, before it lands.
- The `essential` lens asks whether a change adds a second way to do a job or production code only a
  test calls, and whether a fact is kept twice.

### Changed

- `init` takes a base only as a local branch's short name, and tells a base this clone has no branch
  of, naming `git branch <base> origin/<base>`, from a base that shares no commit with `HEAD`,
  naming `--base`.
- `orchestra status` marks a row for a re-plan once it has gone to three distinct agents, and no
  longer counts routes to a queue or repeated routes to one agent.
- The turn-end hook leaves a report whose brief's `Lens:` names `disclosure` alone out of the
  reports that make a checkpoint due, and plain notes out of the log entries that make `NOW.md`
  stale.
- The coordinator's pages:
  - Read the estimate's steps and run them before the first dispatch.
  - Ask the owner before cutting a branch where another live session shares the checkout, sync the
    base with `git pull --ff-only`, and delete only the session branches `git branch --merged`
    lists.
  - Read `get_usage` in the desktop app where `orchestra` reads usage unknown.
  - Wait on no agent with a blocking read.
  - Change the away mode only on the owner's own words, and say why.
  - Send a fix to landed work to a new writer, or make it yourself, since `land` removed its writer.
  - Name each page the owner should look at with its link.
  - Ask the owner about a stopped agent whose row gives no reason the log explains.
  - Push the commit the disclosure read reached.
  - Pass text holding a backslash by file.
- The disclosure lens reads each commit's change with `git log -p --cc`, merges included: over the
  capture's span, and at a later push from the upstream to the commit its brief's `Base:` names.
- The writer rewords its tip with `git commit --amend --only`, which leaves what is staged out; the
  agents' shared page says to pass text holding a backslash by file.
- The away page says a terminal session continues by itself for three resets at most, unless the
  machine slept past one, and a desktop app session where its limit card's box is ticked.

### Fixed

- `orchestra diff --tree` checks out a tree whose paths pass Windows' 260 characters under the
  state folder, which a repository with long tracked paths reaches.

## [0.1.0] - 2026-11-18

### Added

- The `orchestra` program and its hooks, five agent types, sixteen lenses and nine slash commands.

[unreleased]: https://github.com/felzab/orchestra/commits/main
[0.1.0]: https://github.com/felzab/orchestra/commit/364d4ec
