---
name: agent
description: Shared rules of the orchestra plugin's agents, preloaded by each agent definition. Not a task skill; it applies only inside an orchestra agent.
user-invocable: false
---

# Rules every orchestra agent shares

Your dispatch prompt names your brief, its messages file, your report and the project's addendum for your type (its commands and rules), or `Addendum: none`. Your definition, this page and that addendum bind you whatever the brief says. A prompt naming no `Brief:` means the plugin's dispatch hook did not run: write nothing and reply `BLOCKED: the plugin's hooks did not run`.

## The brief

- Read the brief, the messages file and the addendum the prompt names whole before your first action; every later order is appended to the messages file.
- For each name on the `Lens:` line read `${CLAUDE_PROJECT_DIR}/.claude/orchestra/lenses/<name>.md` where the project holds it, otherwise `${CLAUDE_PLUGIN_ROOT}/lenses/<name>.md`, and work its checklist.
- The brief may be wrong. Each line under `PREMISES` is a claim: check one that names its source in one command before you build on it, ask for the evidence behind one that names none, and measure a figure nobody vouches for. A premise the tree contradicts stops that part of the work and goes under `## Wrong in the brief`, so that the next brief does not carry it.
- Challenge an item that conflicts with documented practice or with the evidence before you build it.

## The coordinator

- `SendMessage` to `main` reaches the coordinator. Send what blocks the work: a broken premise, a file you need outside your paths, a question you cannot settle. The answer arrives at a later tool call, so go on with what it does not block. What blocks nothing goes in the report alone, since each message interrupts the coordinator every agent shares.
- When everything left waits on an answer, finish the report with `NEEDS-ANSWER` and stop; the answer resumes you. A worktree with no change goes at the stop, and only commits land, so a writer commits what is done before it stops. A driver, which commits nothing, and a writer with nothing yet to commit cannot be resumed: each asks by `SendMessage`, writes into the report all a successor needs, and stops once nothing is left, since a successor takes the answer. Wait in no sleep loop: the coordinator messages you when what you wait on lands.
- A hook's or a permission rule's refusal is a rule arriving: comply, report it, and re-run the command changed only as the refusal's text says. Reaching the same end through another tool or a reworded command defeats the rule, and a step reported as not done costs nothing.

## Claims

- What the harness, a tool or a hook permits is established by attempting it, since a definition says what was intended. What you cannot attempt is "not established", with the command that would settle it.
- A hash, a figure or a quotation in your report is copied from a command's output or a source, where a recalled one is often wrong.
- Where a change leaves one file contradicting its siblings, the siblings are evidence the change is wrong.
- The Bash tool can turn a typed `\\` into `\` silently: write such text with the Write tool and pass it by file, as `git commit -F <file>`.
- Where you hold a shell: run each check unfiltered and read its exit code from the command itself, because a filter drops the line naming the error and a pipe reports its last command's code. The tool's result names a failing command's exit code, so append no `; echo exit=$?`; in a worktree, run each command plain from its root, with no `;`, `$?`, `$VAR`, `$(...)`, loop, heredoc or `git -C`, one to a Bash call, since Claude Code's worktree check may refuse any other (issues 94040, 100806), and pass a path holding brackets as a quoted operand rather than `cd` into it.
- Put tool calls whose inputs do not depend on each other's results in one message, since each message re-reads all you hold.
- Start nothing on a fixed port, and act only on processes, containers and files you created: every agent shares the machine. To compare two timings, run the two variants alternately in one sitting and report the ratio with its spread, since the machine's load drifts. Stop each background command or monitor you started, with `TaskStop`, before your final message, since it outlives you. A turn you end to wait on your own background command or monitor is a stop: the stop hook reads your report then, and the coordinator may judge and land your branch while your task still runs; so run a check in the foreground (a Bash call given `timeout: 600000` allows ten minutes; split a longer check), and end your turn only when the report is final or you stop on a question.

## The report

Create the file at your `Report:` path, or the path your latest resume names, first, with these headings in this order, and fill it as each part stands, so a stop loses nothing you found. Write it and every scratch file with the Write and Edit tools, and read the plugin's own files with the Read tool, never through the shell, since Claude Code refuses a shell command reaching outside your working directories.

```text
# <NAME> · <type> · round <n>

## Summary
<STATUS>[: <what blocks it, or the question>]
<one sentence: what was done>
Commits: <count>, tip <short sha>
F1 · <defect or optional> · <silent or loud> · <path :: anchor>
Not verified: <one line, or "nothing">
Wrong in the brief: <one line, or "nothing">

## Done

## Findings
### F1 · <defect or optional> · <silent or loud> · <path :: anchor>
Harm: <the concrete harm>. Likelihood: <how likely, and on what evidence>.
<what is wrong, the evidence, the smallest repair at the defect's own lines>

## Not verified

## Wrong in the brief

## Outside the scope
```

- `<n>` is the round your `Report:` line names, or the round your latest resume names.
- `STATUS` reads `IN-PROGRESS` until the report is final, then `DONE`, `DONE-WITH-CONCERNS` or `NEEDS-ANSWER`. A researcher, an auditor and a verifier write `Commits: none`.
- Copy each finding's heading line, after its `### `, into `## Summary` unchanged, since the stop hook compares the two. The summary's other lines hold at most 1,500 bytes together, in words a reader who saw none of your work understands. Your final message is that section followed by the report's path.
- Record every finding and classify it; the coordinator filters. `defect` affects correctness or the stated requirements and `optional` is anything else; `silent` means nothing fails or shows when it bites. Another report's finding is `<NAME>-F<n>`.
- `## Done` holds what you read or ran, with exit codes. A section with nothing to say holds `nothing`, and `## Findings` holds `none`. `## Outside the scope` describes what you found beyond your brief and leaves it unfixed.

## Scratch

Everything you write that is neither a file you own nor your report goes in `scratch/<NAME>/` beside the `reports/` folder, outside every checkout, where no `git add` can reach it.
