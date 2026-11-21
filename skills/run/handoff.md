# The handoff and the next starter

A handoff tells the next session what this one believed and how much of it was verified. Write it once, from the state and from commands, into the programme's folder, the one holding this session's state. A pause inside a session needs none: `NOW.md` serves it.

## The handoff's sections

```text
# Handoff: <this session> to <the next>

## Read these, in this order
<each file and the one question it answers; point at files and restate none>

## Settled, so you do not reopen it
<each decision and where its argument lives; a ruling by its number>

## The single most important thing
<one item>

## What bit this session
<each incident: its mechanism, its cost, the rule it leaves>

## What not to redo
<each rejected option, its reason, the condition that would reopen it>

## Open, and owed to the owner
<each unanswered question and what waits behind it; first what must be raised before the next session starts>

## Verified state
<established by commands between <time> and <time>, each exit code read from its own command>
- the branch and its commit subjects
- the gate: its exit code and the commit it ran at
- each count, with who measured it and when
- the measured minutes from the review's first reader to the end, the slice count and the weekly window's points per agent, each with the log lines it was read from, which the next estimate scales from
- what could not be established, and why
```

Leave out a figure nobody measured again in the state the next session inherits, a file name a pending fix will change, a question the owner has ruled, and anything the project's CLAUDE.md files and `.claude/rules/`, its addenda, the plan or these pages already say. Write the next session's work list, one line an item with its evidence's address, into the programme's plan where `/orchestra:plan` wrote one, in the part the starter names, and otherwise into the handoff.

## The audit

An auditor with `Lens: prompt` that saw none of the work reads the handoff and the starter cold. It also reads what is left undone against the diff file whose path `orchestra diff` prints, named in its brief, and the owner's list of [end.md](end.md) step 8, pasted there, since `orchestra status --end` holds only what the program records. Fix what it finds, because a wrong line here is acted on by a session that cannot ask you.

## The starter

The next session's starter is a map, under a page long, that points at the handoff and copies none of it. The owner pastes it after typing `/orchestra:run`.

```text
- This is session <session> of the programme <programme>.
- Read <the handoff's path> first, then <the plan's part for this session>.
- Where the handoff and the project's CLAUDE.md files, `.claude/rules/` or addenda disagree, they win.
- Your scope is <scope>. It ends when <exit condition>. One pull request.
- Raise each open question the moment it arises.
- End by writing the next handoff and having it audited cold.
```
