# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-11-21

The fixes from the third project session that ran on the plugin.

### Added

- The plugin's first mod, `hooks/compact.js`, gives the coordinator `mcp__orchestra__compact`. The
  coordinator calls it at each natural seam, where the state holds all in flight (a wave has landed
  or a checkpoint has rewritten `NOW.md`, and no report waits to be judged), attended or away. Below
  60% of the compaction window the tool answers that compacting is not needed yet; from it, the
  conversation compacts once the turn ends, and a one-line prompt then resumes the session. The tool
  refuses a call from an agent and from a checkout with no orchestra session, and, after a
  compaction it scheduled was vetoed, failed or not seen to run, every call until the owner's next
  prompt or a compaction it sees run; where Claude Code's built-in guard loads it sees none, so
  only the owner's prompt clears it. Where the tool is absent or fails, the coordinator tells the
  owner to type `/compact` at a seam once the prompt hook has said its context nears the compaction
  window. Its schedule, compaction and resume have been driven in headless sessions only.
  `claude plugin test .` runs its tests, in CI too.
- The prompt hook tells the coordinator, from 75% of a compaction window that a settings file or the
  environment sets, that the compact tool compacts at the next seam. Past the window, which has then
  not applied, the coordinator reads that the tool compacts only from 60% of the window Claude Code
  applied, which may be larger, so the owner's `/compact` compacts at the next seam; the owner is told
  to type `/compact` at the coordinator's next seam, unless the tool compacts there or answers that
  none is needed.
- `orchestra brief NAME --type writer --slice S --body FILE` writes the brief's header above a file
  holding its lists and sections and checks it in the same call. Where the check refuses, the brief
  stays written, and the refusal says to edit it and run `orchestra brief NAME --check`, or to settle
  it with `orchestra prune --discard NAME`.
- `orchestra status --measure` prints the longest chain of slices still to do by their `after:` edges,
  with up to three of the chains tied for it and a count of the rest, and the median and longest
  minutes from a landing to the next writer's dispatch; a reader or a driver sent meanwhile does not
  end the wait.
- `orchestra status` names a path on a slice still to do that the session deleted or moved.
- `orchestra status --end` names a slice whose agents landed while its line is unticked.
- `orchestra queue add --when landed:NAME <what>` accepts a slice `PLAN.md` lists, taken or not, and
  the item falls due once the slice lands, whatever its agent's name.

### Changed

- `orchestra status` reads a slice as done once its agents land, before its line is ticked, and the
  slice of an agent settled with nothing landed, discarded or pruned before its first commit, as
  still to do, its `Owns:` excusing no drift.
- `orchestra status --measure` prints an unticked slice as its id, its count of files and its `after:`
  edges rather than its whole line.
- The checkpoint falls due at a wave's end, when no dispatched agent is live and a writer has landed
  since the last estimate, so a drafted brief holds it off no longer, or after eight judged reports;
  only a writer's dispatch makes the first one due.
- A verb's notice that it cut its output goes to standard error, and the overview after a compaction
  leaves out pruned agents and judged agents with nothing left to land.
- The writer and driver definitions name the tools they use.
- The pages:
  - the estimate's second look asks every kind of saving (timing, waits, scoping, batching, repeats,
    context, owner waits) and applies each that lowers no review, check or reading, at the session's
    start too; a session's first estimate scales the week from the handoff's points per agent;
  - the review's steps start as soon as their inputs exist, its briefs drafted while the last writers
    run, with a fresh reader over a landing whose defect could reach later work;
  - an agent's report reads `IN-PROGRESS` until it is final; a writer commits what is done before it
    stops, and a driver, or a writer with nothing yet to commit, asks by `SendMessage`, writes all a
    successor needs into its report and stops with `DONE-WITH-CONCERNS`, since no answer can resume
    it;
  - a checked brief that will not go out is settled with `orchestra prune --discard NAME`, and a brief
    names the parts of an earlier report its agent needs;
  - the disclosure lens passes the plugin's two merge subjects while still reading their agent names;
  - the agent definitions, the shared agent page, three lenses and the coordinator's start and review
    pages drop the rules their reader already holds in the same load or in a hook's refusal, and an
    agent that stops on a question writes `NEEDS-ANSWER` alone, though the program still reads
    `BLOCKED`;
  - a defect an agent finds beyond its brief is recorded as a finding rather than under
    `## Outside the scope`, except in a verifier's report, so judging banks it and the owner is not
    also asked about it;
  - the review sends a verifier every row a fixer would take and every `defect` row it would close
    as no defect or moot, holding the latter open until the verdict;
  - the coordinator writes a ruling that changes the work into `PLAN.md` in the turn it is given,
    asks the owner only what no ruling, page or command settles, after researching what the owner
    would have it find out, such as a cost or a documented practice, and gives an agent a run of
    calls that needs none of its context;
  - a brief names a cheaper model profile only for work it spells out, never for a lens, a verifier
    or work of finding, judging or designing;
  - the handoff writes the next session's work list, one line an item, into the programme's plan
    where one exists and otherwise into itself, and the estimate's second reader also runs at each
    estimate the owner starts and names each row it corrected the last time;
  - the shared agent page says the Bash tool moves a call into the background after about 180
    seconds, whatever its timeout, so an agent splits a longer check.
- The guide carries the measured compaction figures, why 500,000 is kept and why a seam compacts
  from 60% of it, names `/status`'s setting sources, says which switches turn the mod off and what
  the built-in guard changes for it, and says when a question's timeout counts down; the README
  says what the mod's hook on every prompt and its calls do.
- `contextCeiling` refuses a resume only once the agent's prompt cache has lapsed, five minutes after
  its transcript was last written; a resume within them reads the context from the cache and goes
  ahead, and the refusal names the transcript's age.
- The hooks wait on less: an attended turn's end reads no agent's transcript, the prompt and
  post-call hooks take the primary checkout from `session.json` where its `.git` folder holds the
  session's pointer, rather than from two git runs, and an agent's `SendMessage` loads nothing.
- A coordinator's prompt reads the compaction window without loading `init`'s modules, a few
  milliseconds less on each prompt, and a command finds the checkout once rather than two or three
  times, about 85 milliseconds less on each command that changes the record.
- The guide's sample `mechanical` profile runs Sonnet at `high` effort rather than `medium`.
- `judge` names a new finding at the anchor of a closed row too, with the row's close and its
  evidence, so a defect raised again is judged with the first judgement in view.
- A verifier's row records as its evidence the verdict's first line that is no finding heading; a
  corrected heading leads it only where it differs from the banked one.
- `judge` warns, refusing nothing, where a row whose finding or verdict says its change is the
  owner's word or call is routed to an agent; a route to a queue is not warned of.
- A verifier's `CONFIRMED` verdict on a row closed `FIXED`, `RULED` or `HANDOFF` takes no
  disposition and moves nothing; one on a row closed `NOT A DEFECT` or `MOOT` still takes one.
- A question asked again with `--supersedes` may replace a ruled one too, which marks its ruling
  replaced, and `rule`'s refusal of a second ruling names `--supersedes`. Where the question was
  asked under another session, the summary and the log line name that session.
- `orchestra status` and the session-start hook test a changed path against a brief's `Owns:`
  pattern only where the path begins with the pattern's literal start, which makes the drift check
  of a long session faster with the same lines, and the turn-end hook hands the queue the records
  and the log it has already read.
- `orchestra land NAME`, `orchestra prune` and `judge` wait on less: a landing reads the other
  agents' branches only where a conflict names who else changed a path, and passes over agents
  already landed or pruned, though it still reads their records before its merge, so one that does
  not read refuses it; no researcher's, auditor's or verifier's transcript is searched for a
  branch; prune looks up each branch once; and `judge` reads which agents are live only for a
  verifier's report, and finds a new finding's repeat from the anchor a row's bank line in
  `findings.jsonl` now records, rather than by reading every report holding a banked row. A row
  banked by an earlier version records none, so its report is still read, and a verifier's report,
  which banks no finding, reads none. A version before 0.4.0 cannot read a `findings.jsonl` this one
  has banked into: it refuses the file, and with it every verb that reads the findings.
- `orchestra status`, its `--measure` and `--end` views, the overview a compaction or a resume
  gives the coordinator and the `waits` lines `judge` and `land` close on read each brief, the plan
  and the log once a view, where they read each brief up to five times and the plan up to seven,
  and a read of the findings, the queue or the rulings builds only each row's last line; the lines
  printed are the same.

### Fixed

- `judge` takes a stopped agent's `BLOCKED` or `NEEDS-ANSWER` report as it stands when its worktree is
  gone, since no answer can reach it; `orchestra prune` then settles its record once neither its tree
  nor its branch is left.
- The `/usage` reading gives up after 90 seconds rather than 120, short of the Bash tool's default limit.
- The report check reads a finding's whole first paragraph for `Harm:` and `Likelihood:`, and accepts
  `Rows fixed: none.`.
- The overview after a compaction or a resume gives `NOW.md` the room first, where the hook's
  10,000-character cap cut its tail without a sign. The status rows give way, ending in a line that
  counts them and names `orchestra status`, and a `NOW.md` longer than the room keeps its head and
  names the characters left out and its path.
- A dispatch is recorded where git cannot find the primary checkout, and the hook names the rows it
  left unrouted and the `orchestra route <owner> <row>` that routes them.
- A hook fault no longer refuses an agent's message to its coordinator.
- `orchestra land NAME` on a branch the session branch already holds stamps the agent landed where
  its judged report says its work is done, and not where it stopped on a question or where a round
  after that report stopped before its own; a writer whose `DONE` branch was lost before it landed
  no longer reads as landed, and its slice stays to do. The judged report's own status decides, also
  where a usage limit ended its round, which left the record holding the round before's, and the log
  records such a landing only where the record was stamped.
- `orchestra land NAME` on a branch with no commit of its own that took in the session branch by a
  fast-forward, as an agent merges the session branch into its own, reads nothing to land, where it
  closed every row the agent's report names fixed with another agent's landing as the evidence. A
  branch moved onto the session branch by a fast-forward made by hand is no longer credited either:
  only a merge commit credits a landing, and the rows it leaves routed are named by
  `orchestra status --end`.
- On Windows, the process that deletes a pruned worktree in the background opens no console window
  where the Python that starts it is a launcher, such as a virtual environment's `python.exe`.
- An agent whose report's summary quotes a usage-limit notice or an API error, as one citing Claude
  Code's errors page does, is read as reported, where it was marked stopped with its report
  unchecked; only a last message that opens with the notice or the error stops it.
- A resume refused because the agent's worktree is gone names `orchestra prune --discard NAME` where
  `orchestra prune` would pass over the record: one still `running` whose branch is gone, and one
  that names no branch. It names `judge`, which banks the report, where the report reads, and
  elsewhere `orchestra prune`, which settles the record or says what keeps it.

## [0.3.0] - 2026-11-19

The fixes from the second project session that ran on the plugin.

### Added

- The coordinator's prompt hook reads its own context from the session's transcript and, past the
  compaction window the environment or a settings file sets, tells the coordinator so and that only
  the owner can type `/compact`, and shows the owner a line asking for it.
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
- `orchestra status --measure` sums an agent's rounds, each from its dispatch or resume to its
  report or stop, where it counted only the last round.
- The dispatch names the first report's title and its round.
- An agent's rules say a turn ended to wait on its own background command is a stop, that a long
  check runs in the foreground with its timeout named, and that a command in a worktree runs plain,
  with no `;`, `$?`, `$(...)`, loop or heredoc.
- The run page puts its injected output last, so a compaction's cut keeps its orders, and every
  other command page runs its command as a step, so a compaction re-attaches no stale output
  with it.
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
[0.4.0]: https://github.com/felzab/orchestra/pull/4
[0.3.0]: https://github.com/felzab/orchestra/pull/3
[0.2.0]: https://github.com/felzab/orchestra/pull/2
[0.1.0]: https://github.com/felzab/orchestra/commit/364d4ec
