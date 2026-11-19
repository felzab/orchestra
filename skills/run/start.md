# Starting a session

Run once, in this order, before the first writer goes out.

1. Read the files the owner's starter names, in the order it gives.
2. Another Claude Code session live in this checkout, which `ListAgents` shows, moves its branch under you: ask the owner first. In the primary checkout, sync the base on a clean tree with `git pull --ff-only`, and delete each session branch `git branch --merged <base>` lists with `git branch -d` and, where GitHub kept it, `git push origin --delete <branch>`; one the listing leaves out, a squash-merged one included, is the owner's to delete, since `git branch -d` also passes a branch merged only into its upstream. Cut the session's branch from the base, then run `orchestra init <session>`, with `--programme <programme>` where the starter names one and `--base <branch>` where the session's base is not the configuration's `base`. It makes the state and runs `orchestra doctor`; repair each line that fails before any dispatch, since an agent meets the same fault later and alone.
3. List the work from the tree by command, and tick each slice against that listing: a slice nobody listed is one nobody considers.
4. Fill the four headings of `PLAN.md`.
   - `## The request`: the owner's words that set the session's work, verbatim, since a paraphrase loses what `conformance` holds the tree to.
   - `## Slices and their files`: one line a slice, `- [ ] <SLICE>` then every file it writes in backticks, ending `after: <SLICE>` where it needs that slice's contract. `orchestra status` reads these lines to find drift and what can start, a ticked line owning only its backticked paths: tick a slice when it lands, or your own when you commit it. A reader or a driver is briefed with no slice and needs no line here.
   - `## Landing order`: one constraint a line.
   - `## Ending`: one `- [ ]` line for each step of [end.md](end.md) that applies and each of the project's own, ticked as it is done.
5. Read [the estimate's steps](../estimate/SKILL.md) and run steps 1 to 5 and 7 from `orchestra status --measure` before the first writer's dispatch, applying each change step 4 chooses to `PLAN.md` in place of step 6, while the plan's order is cheapest to change; its step 8 auditor goes out with the first wave. Researchers that settle the plan's premises may go out before it, since a plan nobody has mapped cannot be sized.
6. Fill the two labels of `NOW.md`; `orchestra status` prints the waiting note, the newest estimate and the last gate run.
7. Send the owner every open question as one batch through the ask tool. Work the questions do not block starts at once.
