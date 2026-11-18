# Starting a session

Run once, in this order, before the first agent goes out.

1. Read the files the owner's starter names, in the order it gives.
2. Cut the session's branch in the primary checkout, then run `orchestra init <session>`, with `--programme <programme>` where the starter names one. It makes the state and runs `orchestra doctor`; repair each line that fails before any dispatch, since an agent meets the same fault later and alone.
3. List the work from the tree by command, and tick each slice against that listing: a slice nobody listed is one nobody considers.
4. Fill the four headings of `PLAN.md`.
   - `## The request`: the owner's words that set the session's work, verbatim, since a paraphrase loses what `conformance` holds the tree to.
   - `## Slices and their files`: one line a slice, `- [ ] <SLICE>` then every file it writes in backticks, ending `after: <SLICE>` where it needs that slice's contract. `orchestra status` reads these lines to find drift and what can start, a ticked line owning only its backticked paths: tick a slice when it lands, or your own when you commit it. A reader is briefed with no slice and needs no line here.
   - `## Landing order`: one constraint a line.
   - `## Ending`: one `- [ ]` line for each step of [end.md](end.md) that applies and each of the project's own, ticked as it is done.
5. Fill the two labels of `NOW.md`; `orchestra status` prints the waiting note, the newest estimate and the last gate run.
6. Send the owner every open question as one batch through the ask tool. Work the questions do not block starts at once.
