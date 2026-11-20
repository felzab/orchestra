# Writing a brief

A brief is read once, by an agent that knows nothing else about the work. Its definition holds the bar and every standing rule, so the brief holds only this work. The program writes the header's first three lines and a writer's `Owns:` from its slice's line; your `--body` file holds the other lists and the four sections, and an `Owns:` of its own where `(new)` is due.

```text
Agent: API-RETRY    Type: writer    Slice: S3
Base: retry-policy at 4f2a91c
Report: /state/reports/API-RETRY.md
Owns: src/client/retry.py, tests/client/test_retry.py (new)
Rows: REVIEW-F3
Writes: none
Lens: none

GOAL      A request that times out is retried at most twice. Today `send` retries without limit, because its first caller made one call that was safe to repeat.

WORK      1. Bound the retries in `send`. At: src/client/retry.py :: send. Harm: an unbounded retry holds a worker through an outage and nothing fails; likely, since REVIEW-F3 found it in a log. Accept: tests/client/test_retry.py fails with the bound removed and passes with it.

PREMISES  Only `sync_all` calls `send` (`git grep -n "send(" src` prints one line).
          The server's writes are safe to repeat (REVIEW-F3 says so; nobody has checked).

TRAPS     The tests share one fake clock: advance it, since a sleep makes the suite slow and flaky.
```

## The header

- `Owns:` lists every path the agent may write: a file, a folder, or a glob where `*` stays within a folder and `**` crosses folders. A file still to be made is written `<path> (new)`, which `--check` otherwise refuses.
- `Rows:` lists the rows a writer's commits answer, which the dispatch routes to it so that its landing closes those its report names fixed, and `none` otherwise. A verifier's rows are named in its `WORK`.
- `Writes:` lists paths outside the checkout and the state that a researcher, auditor or verifier may write besides its report and its scratch folder.
- `Lens:` names lenses. The agent reads each file, so paste none.
- `Model:` is a line you may add, naming a profile of the project's `models`; without it the agent gets `default`.

## The sections

- `GOAL` says what is to be true afterwards and why the thing is as it is today, because an order written from a measurement alone removes what the code was built to do.
- `WORK` is numbered items. Each carries `Accept:`, the check named by what it asserts; a writer's names a check that runs in its worktree, since an `orchestra` verb reads the session's branch and the primary checkout and so sees no unlanded commit. Where `Rows:` names rows, each item carries `At:`, a symbol, a path or a rule id, since a line number moves. An item that adds a mechanism, a new test included, carries `Harm:`, with the harm it prevents here, how likely that is and who would see it without the mechanism; an item you cannot fill that slot for is not briefed. A change a person sees is accepted by the look or the drive the session owes, not by a new test.
- `PREMISES` holds everything the work rests on that the agent has not seen, each with the command that shows it at `Base:`, or with its source and, where nobody has checked it, words saying so. Another agent's conclusion goes here as a claim.
- `TRAPS` holds only what is true of this work and stands in no definition.
- A part the brief asks of the report is a `###` heading under `## Done`, since the stop hook refuses any other `## ` heading, and findings number from 1 without gaps.

## Sizing

Size a brief by what losing its agent's whole output would cost. It carries every path in full, each figure with its source, for a changed signature its callers and the tests asserting the call, and for a judgement its parameters and one worked verdict.

## By type

- An auditor has no shell. Give it each path `orchestra diff` printed, and send what must be run or planted to a driver.
- An auditor or a driver gets the intent and the change without the writer's report, which would tell it what to believe.

## The check

`orchestra brief NAME --check` stamps the brief, and an edit after it clears the stamp, so check again. A checked brief you will not dispatch goes on refusing every brief that overlaps it; `orchestra prune --discard NAME` removes it and returns its slice to `planned`.
