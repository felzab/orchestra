# Using orchestra

This guide is for the person who runs a session: you type the slash commands, and your Claude Code
session, the coordinator, types the `orchestra` commands. [The README](../README.md) covers the
requirements and the install.

## Setting up a project

Three settings make a project ready, each in the project's `.claude/settings.json`:

- **`worktree.baseRef` set to `"head"`.** Claude Code cuts each writer's worktree from the
  repository's default branch otherwise, so a writer would not see what the session has already
  landed.
- **`Bash(orchestra *)` allowed.** The run page's own grant lasts until your next message, so every
  later `orchestra` command would prompt. Allowing it allows each program the configuration's
  `gate`, `land.steps`, `preflight` and `claude` name too, since `orchestra` runs them.
- **The plugin enabled at project scope**, which [the install](../README.md#install) writes.

```json
{
  "worktree": { "baseRef": "head" },
  "permissions": { "allow": ["Bash(orchestra *)"] }
}
```

Claude Code applies the `allow` rules of a project's `.claude/settings.json` only in a folder you
have trusted, and a headless or Agent SDK session shows no trust prompt (the
[permissions page](https://code.claude.com/docs/en/permissions), "Project allow rules and workspace
trust"), so start one interactive session in a new clone and accept its trust prompt.

On Windows, run `git config --global core.longpaths true` once: the frozen tree `diff --tree` makes
sits deep under the state folder, and a git command an agent runs there without the key reads a
path past 260 characters as deleted, with exit code 0.

Add `.claude/worktrees/` to `.gitignore`: Claude Code puts the agents' worktrees there. Commit
`.claude/settings.json` and `.gitignore` before the first session, and keep
`.claude/settings.local.json` out of git: `init` adds it to the clone's `.git/info/exclude`
wherever the file exists untracked and no ignore rule covers it. A landing is refused while the
checkout holds any change, an untracked file included. A project that needs more than the
defaults adds [a configuration file](#the-configuration-file) and
[addenda](#the-projects-addenda).

**Compact at 500,000 tokens.** Each call sends the whole conversation again, so a long session's
cost is mostly that re-reading: on the maintainer's seven long coordinator sessions of a model with
a 1M context, a 500,000-token window cost about 0.7 of Claude Code's default there, which compacts
near 1M. Claude Code caps the window at the model's context, so on a model with a smaller context
the window changes nothing. A compaction replaces the conversation with a summary, and the
session-start hook gives the state back from its files; what it can lose is detail still in flight,
so where you can, run `/compact` yourself at a natural break, such as after a wave lands.

`init` sets the window for you. Where the environment sets no `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, in
the shell or a settings file's `env`, which outranks every settings key, and none of the checkout's
`.claude/settings.local.json`, the project's `.claude/settings.json` and your user settings sets
`autoCompactWindow`, at the top level or for a model under `modelSettings`, it writes
`"autoCompactWindow": 500000` into `.claude/settings.local.json`; where one does, it names that
window and writes none. A window under `modelSettings` binds its model alone, so a session on
another model runs at Claude Code's default window. The local file outranks the project's file and
your user settings, the window `/autocompact` saves there included. `claude --autocompact 500k`
sets the window for one terminal session over every file; the desktop app takes no flag.

A desktop session the app starts from its pre-warmed process did not apply the `autoCompactWindow`
the project's or the local settings file sets
([issue 105316](https://github.com/anthropics/claude-code/issues/105316), open; other keys were not
tested), so `init`'s window may not reach it. Typing `/autocompact 500k` inside such a session was
seen to apply: it saved a window for the session's model under `modelSettings` in
`~/.claude/settings.json`, and the session then compacted by itself at 467,099 tokens. A top-level
key in that file covers every model and is expected to reach such a session as well, but nobody has
tested it:

```json
{ "autoCompactWindow": 500000 }
```

Your user settings reach every project on the machine, which is why `init` writes none there. Each
agent definition also sets 500,000, which lowers a larger window an agent would inherit. Where the
window still does not apply, the hook on each of your prompts tells you and the coordinator once its
context has passed the window, since only you can type `/compact`: Claude Code lets no model run it
(the [skills page](https://code.claude.com/docs/en/skills), "Restrict Claude's skill access").

## Starting a session

1. In the project's primary checkout, the folder you cloned rather than a worktree, cut the branch
   the session's work lands on.
2. Start Claude Code at the checkout's root, since Claude Code reads the project's
   `.claude/settings.json`, and on Windows `.claude/settings.local.json` too, from the folder a
   session starts in; a desktop session started from the app's pre-warmed process did not apply the
   window either file sets, so give it one as [compacting at 500,000 tokens](#setting-up-a-project)
   says. Type `/orchestra:run`. The coordinator reads its rules and says whether this
   branch already holds a session.
3. For a new session, tell the coordinator what the session is for. It names the session, as in
   `orchestra init checkout-rework`, and then writes the session's plan.

`init` refuses outside the primary checkout, on a detached head, and on a branch that already holds
a session. It takes three options:

- `--base`, the branch the session is cut from, named by its local branch's short name. Without it,
  a session takes the configuration's `base`, and `init` refuses a branch that already holds
  commits that base does not, as a branch cut from another one does, so a programme whose sessions
  branch from a branch of their own names it on each session. `init`'s first line names the base,
  where it came from and its commit, and `orchestra log` keeps that line.
- `--programme`, the name of the larger piece of work the session belongs to, which defaults to the
  primary checkout's folder name, and which `init` refuses to default where that programme's folder
  holds sessions of another checkout, so an unrelated clone sharing the folder name gets a programme
  of its own.
- `--rulings-from`, the number the programme's first ruling takes, for a programme whose rulings
  were numbered elsewhere before.

`init` then runs `orchestra doctor`, which the coordinator can also run at any time. It refuses a
configuration file that does not parse with exit 2, before any check runs, and exits 1 while a check
fails, naming each one:

- each program the configuration names is found: `gh` where `ci.workflow` is set, and the program
  of each landing step, each preflight command and the gate;
- each lens file's `**Applies.**` and `**Run by.**` lines can be read, and each lens the `lenses`
  key names has a file, the project's or the plugin's;
- each preflight command exits 0;
- `worktree.baseRef` is `"head"`, and the session's state is a working directory of the session;
- an ignore rule covers `.claude/worktrees/`, without which each agent's worktree is a change in the
  checkout that a landing refuses;
- `stateRoot` is under no `.claude` folder, and resolves to the folder holding the session's state,
  which a changed setting or a packaged app's folder redirect breaks;
- `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` is unset where the configuration names models, since it puts
  every agent on one model;
- `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` is unset, since an agent run in the foreground gets its id
  only once it ends, and until then the hooks refuse its writes;
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` is unset, since with it an auditor, researcher or verifier
  launches as a teammate, whose turn's end no hook of the plugin answers.

To pick up a session that a pause, a crash or a usage limit interrupted, start Claude Code on the
session's branch and type `/orchestra:resume`.

## Agents

Each agent is one of five types, dispatched as `orchestra:<type>`.

| Type         | What it does                                                                         |
| ------------ | ------------------------------------------------------------------------------------ |
| `writer`     | changes the files its brief owns in its own worktree and commits them                |
| `driver`     | proves a check can fail by planting a violation in its own worktree; commits nothing |
| `auditor`    | judges a captured diff, a document or a plan, with no shell                          |
| `researcher` | answers a question with a read-only shell and the web                                |
| `verifier`   | tries to refute each finding it is given, before anyone is sent to fix it            |

A hook keeps the `Write` and `Edit` tools of an auditor, a researcher and a verifier to their
report, their scratch folder, the paths their brief names under `Writes:` and Claude Code's
scratchpad, and keeps a writer and a driver out of the programme's folder but for their report and
their scratch folder. This fence guards against mistakes and is no security boundary: nothing checks
what an agent's shell writes. Outside the primary checkout, `orchestra` runs only what reads the
session's record: `doctor`, `status`, `show`, `findings`, `log`, `queue` with no action, `ask` with
no question, `diff` without `--tree` and `land NAME --preview`. Every other command, `note` and the
plain `ci` among them, exits 2 there, naming the primary checkout, so a writer or a driver does not
change the record by mistake from its worktree. Like the fence, this guards against mistakes and is
no security boundary: it tells an agent from the coordinator by the working directory alone, so a
`cd` into the primary checkout passes it, and a researcher or a verifier, which runs in the
coordinator's checkout, is kept from nothing.

## Lenses

A lens is one question an agent reads a change with, through a fixed checklist. A brief names its
lenses on its `Lens:` line. The plugin's sixteen are the files of [`lenses/`](../lenses), each
opening with its question and ending with two lines: `**Applies.**`, when the lens is due, and
`**Run by.**`, the agent type that runs it. Those lines are written in the files alone, so read
them there.

| Lens            | Its question, in short                                      |
| --------------- | ----------------------------------------------------------- |
| `security`      | can a caller reach what it should not                       |
| `contract`      | does each side of a boundary hold what the other expects    |
| `essential`     | should each thing the change adds exist                     |
| `conformance`   | is every ruling and plan item met, every closed row closed  |
| `use`           | can a newcomer carry out a plain request with it            |
| `data`          | can two writes, a retry or a crash leave stored state wrong |
| `tests`         | can each check fail, does a case reach each added behaviour |
| `necessity`     | does each test case guard a harm nothing else guards        |
| `prompt`        | will a model holding only this page do the right thing      |
| `document`      | is each claim of a page a person reads true                 |
| `operations`    | who learns of a failure, and how is it rolled back          |
| `practice`      | does each mechanism follow its tool's documentation         |
| `cost`          | what does one event on this path cost, measured             |
| `disclosure`    | does anything published name a person, machine or secret    |
| `accessibility` | can every person operate what changed                       |
| `estimate`      | can the remaining work finish sooner                        |

At the review, `orchestra diff` prints each lens due from the changed paths, with the files that
made it due and the agent type that runs it. The coordinator gives each to an agent of that type,
alone or with other lenses of that type on one brief's `Lens:` line, as
[the review page](../skills/run/review.md) says when, or declines it with a recorded reason. The
session's ending holds only once every due lens has a judged report from such an agent whose brief
names that lens, alone or with others, or such a note.
A lens due only where the project declares it, and not declared, is printed as `undeclared` and is
read or declined like a due one; [the `lenses` key](#the-configuration-file) declares it.

## Following the session

The coordinator works by [its own pages](../skills/run/SKILL.md): for each slice of its plan it
writes a brief, dispatches an agent, judges the agent's report and lands a writer's branch, and it
pushes once a wave, the agents dispatched together, which land before the next push, and reads CI.
Judging gives each finding of a report an owner or a closing; a finding so recorded is a row, such
as `BILLING-F1`. Once the session's files stop moving, agents read the whole change with each lens
due, a verifier tests their findings, one round of writers fixes what holds, a driver new to the
work uses what ships, and fresh readers read the fixes where a defect in them could pass unseen.
How many readers go out, which lenses share one, and whether a cross-cutting change gets a reader of
its own or the fixes a further round, the coordinator judges from what changed and what a defect
there could break, and records each choice as a `Review:` note in the log. The ending, the lines
under the plan's `## Ending`, runs at `/orchestra:end`.

To see where the session stands:

- **`/orchestra:status`** shows the agents by status, the reports not judged, what is due, the
  unclosed rows, the last CI reading and the drift from the plan.
- **`PLAN.md` and `NOW.md`** in [the state folder](#where-the-state-lives) hold the slices with
  their files, each ticked once it lands, and the next action with why it is next.
- **The coordinator runs any other command you ask for**, such as `orchestra findings`, which lists
  every unclosed row by owner, `orchestra show BILLING-F1`, which prints a finding whole, and
  `orchestra log`. The program's `--help` lists every command.

A command prints at most forty lines unless given `--all`, and ends on one summary line. It exits 0
when done, 1 when done with something remaining and 2 when it refused; `land` has codes of its own,
which its `--help` lists. `status` exits 0 whatever it finds, since a skill injects its output, so a
script reads its lines rather than its code. A fault in the program, rather than a verdict, exits
70, its trace printed under a line naming the command and saying "failed: a fault, not a verdict".

### The usage limits

`orchestra status --measure` opens on your plan's usage limits, one `usage` line for each row the
server sends: its kind, such as `weekly_all`, the share used, when it resets, and the server's
severity where it is not `normal`, such as `warning`. A five-hour row appears only where the server
sends one. The coordinator tells you at once when a weekly row names a severity, since only you can
lift a weekly limit.

The program reads them by running Claude Code's `/usage` headless,
`claude -p /usage --safe-mode --no-session-persistence --output-format stream-json --verbose`, which
uses no model tokens; it prints the same reading again for five minutes, saying `cached`. The rows
need a claude.ai login with a plan, so on usage-based billing, or where the request is refused or
rate limited, the line says so instead.

It runs `claude` found on PATH, where Claude Code's installers put it, else the program the
[configuration's `claude` key](#the-configuration-file) names, and the line says which. The
desktop app puts no `claude` on PATH and keeps its own program in a folder named for its version,
so a path to it goes stale at each update. There, unless the `claude` key names the app's current
program, the `usage` line reads unknown with its reason, and the coordinator reads the same limits
from the app's own `get_usage` tool instead.

## Your commands

[The README](../README.md#commands) lists the nine commands. Each loads only its own page, and costs
nothing until you type it. Words after `/orchestra:plan` give the programme's goal, and words after
`/orchestra:estimate` narrow what it estimates. Words after `/orchestra:away` say until when and
give any last order, which the coordinator records as a ruling and, where it is a step of the
session's end, as a line of the plan's ending; [the away mode](#the-away-mode) says what that
command changes.

The coordinator asks you a question the moment it arises. While you are away it holds the question
instead, and records your answer later in your own words:

```sh
orchestra ask "Keep the old endpoint for a release?" --option "Keep: clients move over a release" --option "Remove: one breaking release" --recommend 1
orchestra rule 1 "Keep it for one release."
```

`orchestra ask` alone lists the open questions. Each carries the number the state gives it, which
the coordinator shows you, so answer by that number; a question asked again with `--supersedes`
closes the one it replaces.

## The configuration file

`.claude/orchestra.json` holds what is true of one project. Every key is optional, and a project
without the file gets every default. The file is read from the primary checkout even where a
command runs in an agent's worktree, so every agent sees the file as it is on disk. A key orchestra
does not know is refused, naming its path in the file.

```json
{
  "$schema": "https://raw.githubusercontent.com/felzab/orchestra/main/schema/orchestra.schema.json",
  "base": "main",
  "models": {
    "default": { "model": "opus", "effort": "high" },
    "mechanical": { "model": "sonnet", "effort": "medium" }
  },
  "queues": ["review"],
  "preflight": [{ "run": ["docker", "info"], "why": "the integration tests start a database container" }],
  "land": {
    "steps": [
      { "kind": "install", "when": ["pyproject.toml", "uv.lock"], "run": ["uv", "sync", "--frozen"] },
      { "kind": "install", "when": ["web/package.json", "web/package-lock.json"], "cwd": "web", "run": ["npm", "ci"] },
      {
        "kind": "regenerate",
        "when": ["api/"],
        "run": ["uv", "run", "python", "-m", "api.openapi"],
        "writes": ["api/openapi.json"]
      },
      { "kind": "check", "when": ["api/"], "run": ["uv", "run", "pyright"] }
    ]
  },
  "lenses": {
    "triggers": [
      { "name": "data", "when": ["api/db/"] },
      { "name": "cost", "when": ["api/routes/"] },
      { "name": "disclosure", "when": ["**"] },
      { "name": "accessibility", "when": [] }
    ]
  },
  "ci": {
    "workflow": "ci",
    "waitMinutes": 45,
    "findings": ["^FAILED (?P<text>.+)$"]
  },
  "gate": { "run": ["bash", "./scripts/check.sh"] }
}
```

- **`$schema`** names [the schema](../schema/orchestra.schema.json), so an editor completes and
  checks the file.
- **`stateRoot`** is the folder every session's state lives under. `~` is the home folder, and a
  relative path is read from the primary checkout. Leave it out for the default that
  [where the state lives](#where-the-state-lives) names. `init` refuses the home folder, a folder
  holding it, a file system's root and a folder inside the checkout, and a folder under `.claude`
  makes every agent's report write prompt.
- **`base`** is the branch a session branch is cut from, `main` by default;
  [`init`'s `--base`](#starting-a-session) names another for one session. `init` records
  the commit where the session branch leaves it, and refuses a base this clone has no local branch
  of, naming `git branch <base> origin/<base>` where `origin` has it.
- **`models`** maps a profile's name to a `model` alias (`opus`, `sonnet`, `haiku` or `fable`) and an
  optional `effort` (`low`, `medium`, `high`, `xhigh` or `max`). An agent gets the profile its
  brief's `Model:` line names, such as `Model: mechanical`, and `default` where the brief has no such
  line; a name the configuration does not hold is refused. With no `default`, the dispatch sets no
  model and Claude Code chooses.
- **`contextCeiling`** is the context, in tokens, past which a hook refuses the coordinator's message
  that would resume an agent, except the answer to an agent stopped on a question and still waiting
  on it, since a resume sends the agent's whole context again. The default is 200,000.
- **`queues`** adds owners a row can be routed to besides an agent, beyond the built-in `owner`,
  `ending` and `handoff`.
- **`preflight`** lists commands that must exit 0 before a session can work, each with `run`, the
  program and then one argument an item, and `why`, what fails without it. `doctor` runs them.
- **`land.steps`** lists what `land` runs after a merge that touched a path under one of a step's
  `when` paths: a file, or a folder where it ends in `/`. Each step has a `kind` and a `run`
  command, and runs from `cwd`, the root by default. Installs run first, then regenerations, then
  checks. A regeneration names its outputs under `writes`, and a merge conflict in one of them is
  answered by running the regeneration. A path holding `*`, under `when` or `writes`, is refused
  when the file is read, since paths are compared as written. A check that fails stops the
  landing. A step runs as long as it takes, with no time limit.
- **`lenses.triggers`** declares where a lens is due in this project. Each trigger names a lens and
  its `when` paths, which add to the paths the lens's own `**Applies.**` line names. A path is read
  as a brief's `Owns:` line reads it: `*` matches within one folder, `**` any number of folders,
  and a path with no `*` names itself and everything under it. Declare the paths holding stored
  state for `data`, the code run on every request or event for `cost`, and the interface for
  `accessibility`; a project that publishes its tree declares `disclosure` over `**`. A trigger's
  paths are not filtered by the lens's own `except`, so a trigger can make `security` due on the
  Markdown files that hold a project's agent rules, which that lens's own line leaves out. An empty
  `when` makes a `declared` lens due on nothing, so it no longer prints as `undeclared`. It leaves a
  lens whose own line is `always` or names paths due as that line says: to change such a lens,
  replace its file under `.claude/orchestra/lenses/`. A trigger may name a lens of the project's
  own.
- **`ci`** tells `orchestra ci` how to read the project's CI. `workflow` names the workflow whose
  failed log is read line by line, by the workflow's `name:` as `gh run list` shows it, not its
  file name; every other run, check and commit status is read by its conclusion alone. A project
  with no `workflow` here, no workflow file in the pushed commit and no run, check or status on it
  runs no CI: its reading is `none`, which the session's ending takes as green. `waitMinutes`, 30
  by default, bounds how long `--wait` waits for the runs, checks and statuses to appear and
  conclude.
  The rest are regular expressions over a failed log: `verdict`, the project's own summary line of a
  failed check, its text in a group named `text`; `relay`, an annotation that only points at the
  jobs carrying the findings; `findings`, a line naming one finding, in a group named `text`; and
  `cases`, a test runner's line naming a failing case, in a group named `name`. A pattern that does
  not compile, or lacks its group, is refused when the file loads.
- **`gate`** is the project's whole check, which `orchestra ci gate` runs in the primary checkout.
  `run` is the command; on Windows a shell script runs as `["bash", "./script.sh"]`. Its exit code
  alone is the verdict: 0 is green, and any other is red. It runs as long as it takes, with no time
  limit, so the coordinator runs it in the background.
- **`claude`** is the Claude Code program that reads [the usage limits](#the-usage-limits) where
  `claude` is not on PATH, read as `stateRoot` is.

## The project's addenda

A project adds what is true of it alone in Markdown files beside the configuration. Each agent reads
them with its brief, from the primary checkout as they are on disk, like the configuration:

- **`.claude/orchestra/<type>.md`**, such as `.claude/orchestra/writer.md`, holds the rules one
  agent type follows in this project: the checks to run and how, the installs a worktree needs, how
  commits are written, and the ports and services an agent must leave alone.
- **`.claude/orchestra/lenses/<name>.md`** is a lens of the project's own, or replaces the plugin's
  lens of the same name. It keeps the plugin's five parts: the question, what to read first, a
  numbered checklist, what is not its business, and whether a failure it looks for is silent. It
  ends with the two lines the program reads, each read as Markdown reads a paragraph, so a row a
  formatter soft-wraps over several lines reads as one line. `**Run by.**` is `auditor`, `driver` or `researcher`.
  `**Applies.**` is one of `always`; `paths` and backticked patterns separated by commas,
  optionally followed by `except` and more patterns; `declared`, due only where a trigger names
  it, and held at the ending as a due lens is while no trigger names it; and `on request`, which
  its own line never makes due, for a lens a step names, as the estimate's second reader does,
  though a trigger naming it adds paths as for any lens. A file lacking either line is refused, as
  one holding a value of no form is, and `orchestra doctor` names it.

## Where the state lives

A session's state is the folder `<stateRoot>/<programme>/<session>/`, outside the repository. The
default `stateRoot` is the `orchestra` folder in the platform's folder for a user's application
data: `%LOCALAPPDATA%` on Windows, `~/Library/Application Support` on macOS, and `$XDG_DATA_HOME` or
`~/.local/share` elsewhere. Uninstalling the plugin leaves it in place. Under the desktop app on
Windows, the folder can sit in the app's own packaged AppData instead, where Windows redirects a
packaged app's writes; the first line of `orchestra status` names the folder in use.

`init` writes the folder's path into `<git common dir>/orchestra/<branch>`, the branch name
URL-quoted, a file no commit carries and every worktree of the clone shares, so every command finds
the state from any worktree. Two sessions on two branches of one clone keep two separate states.

| What                                   | Written by                                                                                                                                                                                |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PLAN.md`, `NOW.md`                    | the coordinator                                                                                                                                                                           |
| `session.json`                         | `init`                                                                                                                                                                                    |
| `away.json`                            | `mode`, while the session is away                                                                                                                                                         |
| `refusals.jsonl`                       | the hooks, one line for each call refused while the session is away                                                                                                                       |
| `lease.json`                           | the first write of a Claude Code session, `mode`                                                                                                                                          |
| `agents/<NAME>.json`                   | the hooks and the commands, one per agent                                                                                                                                                 |
| `briefs/<NAME>.md`, its `.messages.md` | `brief` and the coordinator; the messages hook                                                                                                                                            |
| `reports/<NAME>.md`                    | the agent                                                                                                                                                                                 |
| `scratch/`, `scratch/<NAME>/`          | the coordinator; the agent                                                                                                                                                                |
| `findings.jsonl`, `queue.jsonl`        | the commands and the hooks, one line an event                                                                                                                                             |
| `log.jsonl`                            | the hooks and each command that moves the session on (`init`, `judge`, `land`, `prune`, `ci` but `ci expect`, `diff --tree`, `mode`, `ask`, `rule`, `override`, `queue done`), and `note` |
| `ci.json`, `gate/`                     | `ci`: its readings, and each gate run's output                                                                                                                                            |
| `usage.json`                           | `status --measure`: the usage reading it prints again for five minutes                                                                                                                    |
| `override.json`, `prompt.json`         | `override` and the hooks, which keep each caller's latest refusal no override served; the hook on each of your prompts                                                                    |
| `estimates/`                           | the checkpoint and `/orchestra:estimate`                                                                                                                                                  |
| `diffs/`, `trees/`                     | `diff`: the review's capture and its frozen trees                                                                                                                                         |
| `../rulings.jsonl`                     | `ask` and `rule`, one file for the programme                                                                                                                                              |

Read the state through `orchestra status`, `orchestra findings` and `orchestra log`. The coordinator
writes only `PLAN.md`, `NOW.md`, the briefs, `estimates/` and `scratch/`, and a hook refuses it any
other file there.

A programme's plan, its starters and each session's handoff stay in the programme's folder,
`<stateRoot>/<programme>`, beside the sessions' folders. Set `stateRoot` to keep them where you
want to read them.

**What `init` writes on your machine.** An agent writes its report into the state folder, and a
write outside the session's working directories prompts. `init` therefore adds the programme's
folder, `<stateRoot>/<programme>`, to `permissions.additionalDirectories` in the checkout's
`.claude/settings.local.json`. In the same write it sets `autoCompactWindow` where neither the
environment nor a settings file sets a window, though a desktop session started from the app's
pre-warmed process did not apply it, as [compacting at 500,000 tokens](#setting-up-a-project) says.
It rewrites the file only where it is in the JSON layout Claude Code itself writes, so no
other key changes, and otherwise tells you what to add. It adds no folder where a settings file already grants it. A
tracked `.claude/settings.local.json` is left alone too. The file then holds a path of this
machine, so where no ignore rule covers it, `init` first adds it to the clone's `.git/info/exclude`,
which no commit carries.

## The away mode

`/orchestra:away` runs `orchestra mode away`, so that no permission prompt and no question waits on
you while you are gone. It writes the state's `away.json` and no settings file. Until
`/orchestra:back`, the plugin's hooks answer for the Claude Code session holding the state, the
coordinator's, and for every agent it runs, its background agents included:

- **A permission prompt is refused.** A `PermissionRequest` hook denies every call that would wait
  on a prompt, so a call no permission rule or mode allows is refused where it used to prompt.
- **A question or a plan's approval is refused.** The `PreToolUse` hook denies `AskUserQuestion`
  and `ExitPlanMode`.
- **Each refusal is recorded** in the state's `refusals.jsonl`, with each refusal auto mode makes
  meanwhile.

Every other Claude Code session, in this checkout or any other, prompts as before, but for a fault:
where the program's event handling meets one, `away.json` alone decides, so a permission prompt or
a question of any session in this checkout is refused too.

Before you leave:

- **Allow what the session runs**, since a refused call stops that work. Accept edits by mode or
  allow `Edit` and `Write`, and allow `Bash(orchestra *)` as [the setup](#setting-up-a-project)
  does, the `git` and `gh` commands a commit, a push and a pull request run, and each check and
  install the project's addenda name.
- **Answer the workspace trust dialog and any approval of a project MCP server**, since no hook
  answers either. A sandboxed command's network prompt waits too: Claude Code runs no
  `PermissionRequest` hook for it.

A call a deny rule matches is refused before any hook sees it, so it is never recorded.

While you are away, the coordinator holds its questions with `orchestra ask` and records each
decision it takes alone as a `Decision:` note. `/orchestra:back` runs `orchestra mode attended`,
which ends the away and prints the log's lines since it began, the decisions taken alone among
them, each refused call once with how often and by whom, and the open questions.

## Moving a session to another Claude Code session

One Claude Code session writes a session's state at a time: the first that writes holds it, and a
write from any other is refused, naming the holder. To move the session on purpose, ask the old
session's coordinator to run `orchestra mode stop`, then the new one's to run
`orchestra mode takeover "the old window closed"`: each needs the id of the Claude Code session it
runs in, which a terminal lacks. A takeover without a stop succeeds once nothing has written the
state for ten minutes, as after a usage limit or a crash, and the refusal names the time: "a
takeover succeeds at 14:32, unless that session writes again before". `/clear` gives the
conversation a new session id, so a hook stops the state as it ends the old one, and the
coordinator's next write is refused naming the takeover, which then succeeds at once.

A takeover marks every agent the old session left running `stopped`, since no Claude Code session
can resume an agent another one dispatched. The coordinator lands or saves what each left and
briefs a successor for its work.
