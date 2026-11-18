# Ending the session

Run once, in this order, when one wave and this list remain; start no new work from then on. Draft the pull request body, and the handoff where step 9 applies, as the last wave begins, leaving open only the facts a later step settles.

1. **Land the last wave** and run [review.md](review.md) to its end.
2. **Every slice landed.** Each slice of `PLAN.md` that writes files is ticked against a landed commit, and each other once its reports are judged. Run the start's listing again to find work nobody listed.
3. **Every row closed.** `orchestra findings` exits 0. A row that goes to the next session is closed with `orchestra close HANDOFF "<where the handoff names it>" <NAME>-F1`.
4. **The tree has stopped.** No agent runs or can still report, and no background command or monitor of yours runs but a CI wait. `orchestra prune --ending` removes each agent's worktree and each frozen tree the review made; settle the reason it prints for any it keeps.
5. **Push**, and run `orchestra ci --wait` in the background. Name each check with its conclusion, and report one that has not concluded as pending. A reading still `expected` at this head is a failure, since every landing that was to clear its red is in it.
6. **The gate.** Where `gate.run` is configured, run `orchestra ci gate --final` in the background, since the gate has no time limit of its own; it refuses a tree that differs from what was pushed. Report its exit code once it ends, read from the command itself, since 0 alone is green and a pipe reports its last command's code; a fix it forces is a new commit, a new push and a new run. Otherwise name each check of the project you ran, with its exit code.
7. **The pull request body**, replaced once. Name every file a landing regenerated, which no commit message says.
8. **For the owner**, as one list: each visible change the owner has not yet seen, with where to look, then the owner's steps after the merge in the order they are run.
9. **The handoff**, where a next session follows or a row or question is left to one: the handoff and the next starter, as [handoff.md](handoff.md) says, its audit judged and its rows closed.
10. **The machine's verdict.** `orchestra status --end` prints each line it can check as held or not. The session is finished only when every line is held.

Reply with this list, each line with its evidence.
