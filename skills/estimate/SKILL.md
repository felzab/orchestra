---
description: Type it to ask how long is left. Forecasts when the orchestra session ends, looks for what would bring that forward, and acts on it.
disable-model-invocation: true
argument-hint: "[what to narrow the estimate to]"
allowed-tools: Bash(orchestra *)
---

The owner's words, where given, narrow the estimate: $ARGUMENTS

First run `orchestra status --measure` with the Bash tool. Every figure comes from its output or from a command run now. Mark each `m` for measured or `e` for estimated.

1. **Table A, what remains.** One row for each block still to do, in the plan's order: each unticked slice, the review, the fix round, each line of the ending, each thing the owner must do.

   | Block | Agents | Depends on | Minutes | m or e | Source |
   | --- | --- | --- | --- | --- | --- |

   An `e` row names the measured figure it scales from, for the review the handoff's measured minutes where this session has none. Leave out a row that has no source.
2. **The critical path.** The chain of dependent blocks with the largest sum, the block that dominates it, and that block's share.
3. **The end time.** The clock that output opens with, plus the path: a low figure from the medians and a high one from the longest runs. Beside it, the weekly window's use at that time: the share now of the weekly `usage` row nearest its limit, or of the app's `get_usage` where that reads unknown, plus the points it rose per agent since the earliest saved estimate after its last reset, a week before the reset it names, times the agents table A holds, since one round of agents can spend points of the week in an hour. With no such estimate, give the share now alone and say so. Name the share now with the clock it was read at, and the points per agent.
4. **The second look.** Read `.claude/orchestra/lenses/estimate.md` where the project holds one, otherwise [the plugin's](../../lenses/estimate.md), and answer each of its questions as it says. A wave costs its longest agent, so a change that leaves the dominating block alone saves nothing.
5. **Table B, after.** Table A with the changed figures, and the new end time beside the old.
6. **Act in this turn.** Dispatch each change of step 4, hold it on `orchestra queue`, or decline it with the reason.
7. **Record.** Name each change the previous saved estimate chose and whether it was done, queued or declined, since no check holds step 6. Save this reply in the state's `estimates` folder and name the file and the new end time with `orchestra note <text>`.
8. **A second reader.** At the first estimate after the plan is written and the first after the fix round's slices are added, an auditor with `Lens: estimate` reads cold the saved file, `PLAN.md` and the `waits` lines of that output, pasted into its brief, with the premise that the review runs `use` only after the fixes ([review.md](../run/review.md) step 7).

Reply with the previous changes' fate, table A, the critical path, the answers of step 4, table B, the end time and the weekly window's use before and after, with the figures step 3 names.
