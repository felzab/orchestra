# orchestra

orchestra is a Claude Code plugin for work too large for one agent. Your Claude Code session becomes
the coordinator: it splits the work into slices of files, writes a brief for each agent, and
dispatches the agents, each writer in its own git worktree. It then judges every finding each agent
reports, merges each branch into the session branch and reads CI. A program, `orchestra`, keeps the
session's state in files outside your repository, so a session survives a compaction, a crash or a
usage limit. Hooks hold the rules a program can check: an agent is refused until its brief is
checked, and an agent that stops with its report incomplete is sent back to complete it.

## Requirements

- **Claude Code 2.1.323 or later**, the first release that reads the version range the plugin's
  manifest states in `engines`. A release from 2.1.76 to 2.1.322 loads the plugin without checking
  it, and one from 2.0.15 to 2.1.75 rejects the manifest for the field.
- **Python 3.11 or later**, found on `PATH` as `python3`, `python` or `py -3`. The program uses the
  standard library alone, so nothing is installed.
- **git 2.31 or later**, the first release that gives `rev-parse` absolute paths, which every
  command asks for. A session runs on a branch of a git repository, and each writer in a git
  worktree.
- **The GitHub CLI, `gh`, 2.48 or later, signed in**, for `orchestra ci`, which reads the pushed
  commit's runs with `gh api --slurp`, added in that release.
- **Git Bash on Windows.** Claude Code needs it for its Bash tool, and the plugin's hooks run its
  `orchestra` program, a POSIX shell script that runs through it: without it the hooks cannot run,
  as [When the hooks cannot run](#when-the-hooks-cannot-run) says.

## Install

The repository is its own marketplace. In a Claude Code session in your project:

```text
/plugin marketplace add felzab/orchestra
/plugin install orchestra@orchestra
```

The second command opens the plugin's details. Choose **Install for all collaborators on this
repository (project scope)**. An enabled plugin puts its agents' names and descriptions into every
turn and runs its hooks in every session, and its mod adds one deferred tool,
`mcp__orchestra__compact`, to every session, so project scope keeps that cost to the projects that
use it.

To set it up from a shell in the project's folder instead, and declare the marketplace in the
project's `.claude/settings.json`, which a collaborator's terminal session adds once the folder is
trusted:

```sh
claude plugin marketplace add felzab/orchestra --scope project
claude plugin install orchestra@orchestra --scope project
```

Commit `.claude/settings.json`. Each collaborator then runs the install command once in each clone,
since a project's settings enable a plugin but install it on no machine.

**The desktop app and other Agent SDK sessions** show no trust dialog, so the marketplace a project
declares is never added there and the plugin does not load. From a terminal, run both commands above
once in each clone, then start a new session. `claude plugin list`, run in the clone, shows
`orchestra@orchestra` with its `Status` enabled. To load a clone of this repository instead, start a
terminal session with `claude --plugin-dir <path>`, or pass the path in an Agent SDK app's `plugins`
option; the desktop app takes neither.

The manifest names a version, so an update arrives only when a release raises it. Auto-update is off
for marketplaces other than Anthropic's: turn it on under the **Marketplaces** tab of `/plugin`, or
update by hand with `claude plugin update orchestra@orchestra`.

## Commands

You type these; the coordinator runs the `orchestra` program itself.

| Command                       | What it does                                                         |
| ----------------------------- | -------------------------------------------------------------------- |
| `/orchestra:run`              | first in a conversation: makes it the coordinator of this branch     |
| `/orchestra:status`           | shows the session's state and what waits on you; changes nothing     |
| `/orchestra:estimate [words]` | forecasts the end time, then acts on what would bring it forward     |
| `/orchestra:checkpoint`       | forces a wave's bookkeeping now, which the coordinator runs itself   |
| `/orchestra:away [words]`     | before you leave: runs with nobody to answer, records last orders    |
| `/orchestra:back`             | when you return: lists each refused call, asks what waited           |
| `/orchestra:resume`           | picks a session up after a pause, a crash, a usage limit or `/clear` |
| `/orchestra:end`              | when the work is done: runs the ending and starts no new work        |
| `/orchestra:plan [words]`     | for work too large for one session: plans a programme of several     |

## When the hooks cannot run

The plugin's hooks start `bin/orchestra`, which looks for Python 3.11 or newer and hands it the
event. Claude Code lets the call go ahead wherever a hook fails before the program's event handling
has judged the event: on Windows without Git Bash, where no such Python is found, where that Python
crashes or meets a fault before its event handling runs, such as an import an interrupted update
broke, and where the hook runs past its 30-second timeout. The plugin then judges nothing for that
call: an agent goes out without its brief, no write is fenced, no report is checked, and the away
mode refuses no prompt or question.

- A hook that fails or times out shows a `<event> hook error` notice naming the failure, such as
  `PreToolUse:Agent hook error`, on events such as `PreToolUse`.
- Where every name the program tries is missing, too old, Windows' Store alias or cannot be run, it
  exits 0 and its line, `orchestra: no Python 3.11 or newer answered the hook: …`, shows only in the
  log `claude --debug` writes.
- An agent dispatched this way replies `BLOCKED: the plugin's hooks did not run`, which is often the
  first sign.

Once the program's event handling runs, a fault on an event of the coordinator's session or of one
of its agents shows a notice naming it and writes a `hook` line to the session's log, which
`orchestra log` prints and `/orchestra:back` shows from the away's start. A fault while judging a
dispatch or an agent's call also refuses that call, and while the session is away a fault on a
permission prompt or a question still refuses it.

The coordinator's compact tool comes from the plugin's mod, so it is absent where mods are off, as
under an organization's `allowManagedModsOnly`; the coordinator then asks you to type `/compact` at
each seam, as [the usage guide](docs/usage.md#setting-up-a-project) says. `--safe-mode` and
`"disableAllHooks": true` turn mods off too, but they also stop the hooks above, so no session can
run: `--safe-mode` loads no plugin, and under `disableAllHooks` each agent replies `BLOCKED: the
plugin's hooks did not run`.

## Turning it off

Disable the plugin from `/plugin`, or from a shell in the project's folder:
`claude plugin disable orchestra@orchestra --scope local` turns it off for you alone, and
`--scope project` for every collaborator, through the committed `.claude/settings.json`.
`claude plugin uninstall orchestra@orchestra --scope project` removes it.

Neither removes what the program's `init` wrote outside your repository's files. Delete each by hand
once no session needs it:

- each session's state, in the folder [the usage guide](docs/usage.md#where-the-state-lives) names;
- the programme's folder under `permissions.additionalDirectories`, and the `autoCompactWindow`
  where `init` set it, in the checkout's `.claude/settings.local.json`;
- the line `/.claude/settings.local.json` in the clone's `.git/info/exclude`, where `init` added it;
- the file `<git common dir>/orchestra/<branch>` for each session's branch, the branch name
  URL-quoted, which keeps `init` refusing that branch while the folder it names exists.

## Documentation

- [The usage guide](docs/usage.md): starting a session, the agents and lenses, following a
  session, the configuration file and the away mode.
- [Contributing](CONTRIBUTING.md), [the security policy](SECURITY.md) and
  [the code of conduct](CODE_OF_CONDUCT.md).
- [The changelog](CHANGELOG.md).

## Licence

[MIT](LICENSE).
