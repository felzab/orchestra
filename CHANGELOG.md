# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-11-19

The fixes from the second project session that ran on the plugin.

### Added

- The coordinator's prompt hook reads its own context from the session's transcript and, past the
  compaction window, tells the coordinator to suggest `/compact` and shows the owner the same line.
- `orchestra status` names two unticked slices of the plan that share a file with no `after:` edge
  between them.
- `orchestra status --end` holds a line that no frozen review tree is left.
- `orchestra brief NAME --type writer --slice S` fills `Owns:` from the slice's line in `PLAN.md`.

### Changed

- The review's width is the coordinator's judgement, made from what changed and what a defect there
  could break, and recorded as a note: lenses of one type may share a reader over a small span, a
  change that cuts across the tree gets a reader of its own, and the fixes are read again while a
  reading finds defects in them. `orchestra status --end` counts a lens read beside others.
- The `waits` lines of `judge` and `land` name each holding agent once with a count of its files.
- `orchestra status --measure` measures an agent from its dispatch, not from its last resume.
- The dispatch names the first report's title and its round.
- An agent's rules say a turn ended to wait on its own background command is a stop, that a long
  check runs in the foreground with its timeout named, and that a command naming `git` in a
  worktree runs plain, with no `; echo exit=$?`.
- Every command page puts its injected output last, so a compaction's cut keeps its orders.
- The estimate projects the weekly usage window beside the end time, and the first estimate may
  follow the researchers that map the plan.
- The ending's cold audit also reads what is left undone, before the coordinator says finished.
- `init`'s message, and the usage guide, say that a desktop session started from the app's
  pre-warmed process did not apply the `autoCompactWindow` the project's or the local settings file
  sets, that `/autocompact 500k` typed inside it did, and that a top-level window in the user
  settings is expected to reach it but untested.

### Fixed

- A writer stopped on a question past `contextCeiling` can be answered, where every route out of
  that state was refused.
- The hooks find the session from `CLAUDE_PROJECT_DIR` when the coordinator's shell has moved
  outside the checkout, where every guard but an agent's own went off without a sign.
- A lens file's `**Applies.**` and `**Run by.**` rows read as Markdown paragraphs, so a formatter's
  rewrap neither stops `orchestra diff` nor drops patterns silently.
- A frozen review tree another session left refuses no brief.

## [0.2.0] - 2026-11-18

The fixes from the first project session that ran on the plugin.

### Added

- `orchestra init <session> --base <branch>` names the branch a session is cut from; without it,
  init takes the configuration's `base`. Its first line names the base, where it came from and its
  commit.
- A driver can be briefed with no `--slice`; a driver on no slice is no drift in `orchestra status`.
- The `tests` lens asks whether a case reaches each added behaviour, and is due on every change but
  Markdown.
- The review tests a change to a rule or page meant to change what agents do: one small task goes to
  two researchers, the text before the change pasted into one brief and the text after into the
  other, and alike answers go to the owner. A file every agent loads by itself is tested this way on
  the writer's branch, before it lands.
- The `essential` lens asks whether a change adds a second way to do a job, fixes a symptom rather
  than its cause, switches off a check or adds production code only a test calls, and whether a
  fact is kept twice.

### Changed

- `orchestra land NAME` closes the rows a writer's latest report names on its summary's
  `Rows fixed:` line, no longer reading commit bodies, so a pushed history names no review row; the
  writer page and the disclosure lens say a commit message names none. The line is a writer's alone,
  a driver's faults, and it is left out of the summary's byte cap.
- A landing that carries in another agent's branch credits that agent's rows only where its turn
  has ended and its report is judged; an agent still at work is credited when it lands itself.
- `init` refuses, where `--base` is not given, a branch already holding commits its base does not,
  as a session cut from a programme's branch does.
- `init` takes a base only as a local branch's exact name, refusing a revision such as `main~0`, and
  tells a base this clone has no branch of, naming `git branch <base> origin/<base>`, from a base
  that shares no commit with `HEAD`, naming `--base`.
- `orchestra status` marks a row for a re-plan once it has gone to three distinct agents, a row
  routed again after it closed included, and no longer counts routes to a queue or repeated routes
  to one agent.
- The turn-end hook leaves a report whose brief's `Lens:` names `disclosure` alone out of the
  reports that make a checkpoint due, and plain notes out of the log entries that make `NOW.md`
  stale; a note opening `Decision:` still counts.
- The coordinator's pages:
  - Run the estimate's steps before the first dispatch, all but its acting one, applying the second
    look's changes to the plan; its second reader goes out with the first wave.
  - Ask the owner before cutting a branch where another live session shares the checkout, sync the
    base with `git pull --ff-only`, and delete only the session branches `git branch --merged`
    lists.
  - Read `get_usage` in the desktop app where `orchestra` reads usage unknown.
  - Wait on no agent with a blocking read.
  - Change the away mode only on the owner's own words; the page says why.
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
  state folder, which a repository with long tracked paths reaches, and refuses a tree folder its
  log never recorded, such as a checkout stopped half-way, naming
  `git -c core.longpaths=true worktree remove --force`.
- `orchestra land NAME` finds the landing of a branch that came in through another agent's merged
  branch, where it said there was nothing to land and left the agent unsettled.
- `orchestra brief NAME --check` reads a frozen tree with `core.longpaths`, so a path past 260
  characters is no false change that refuses a brief, and leaves out a driver's worktree, which
  commits nothing.

## [0.1.0] - 2026-11-18

### Added

- The `orchestra` program and its hooks, five agent types, sixteen lenses and nine slash commands.

[unreleased]: https://github.com/felzab/orchestra/commits/main
[0.3.0]: https://github.com/felzab/orchestra/pull/3
[0.2.0]: https://github.com/felzab/orchestra/pull/2
[0.1.0]: https://github.com/felzab/orchestra/commit/364d4ec
