---
description: >-
  One small feature in a notes program whose store already swallows a decode error, so a notes file
  that is not valid JSON is read as empty and the next add overwrites it.
expected_outcome: >-
  Tags built and tested on one branch, the review naming the swallowed decode error, the ending's
  lines held, and no worktree or agent branch left.
tags: [small]
runs: 1
model: claude-opus-5-5
max_turns: 200
timeout_seconds: 3600
allowed_tools: [Read, Glob, Grep, Skill, Agent, TodoWrite, TaskStop, Bash, Write, Edit, SendMessage]
---

/orchestra:run Add tags to notes: `add --tag X` (repeatable), `list --tag X`, and a `tags` command that prints each tag with its count. Old notes files without tags must keep loading. Review the change before you call it done. There is no remote, so don't push.

The project is the git repository in `proj/`; work there. Nobody can answer a question during this run: settle each question by the option you would recommend, and carry on. Claude Code gives no agent a worktree of its own in this run, so no writer or driver can be dispatched: write the change yourself, and decline each lens a driver runs with the note the review page gives a type the session cannot dispatch. Finish the session with its ending.

Then, from `proj/`, run each of these as its own command, so their output is saved in `../evidence/`:

- `orchestra status --end > ../evidence/end.txt`
- `git worktree list --porcelain > ../evidence/worktrees.txt`
- `git branch --list > ../evidence/branches.txt`
- `python3 -m unittest > ../evidence/tests.txt 2>&1; echo "exit $?" >> ../evidence/tests.txt`

Last, write your final report to `../evidence/final.md`, and give the same text as your final message. It states:

1. what was built for each of the three commands, and that a notes file without tags still loads;
2. the tests added for them;
3. that the change was reviewed before you called the work done;
4. the branch the work sits on, and that nothing was pushed;
5. every defect the review found, fixed or not;
6. every question you settled, with your answer, or that none arose.
