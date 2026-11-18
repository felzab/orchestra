# Lens: data

**Question.** Can two writes, a retry or a crash leave stored state wrong?

**Read first.** The stored shapes the change touches (schema, constraints, indexes), then every path that writes them.

**Checklist.**

1. Each write that reads first: what happens when a second writer lands between the read and the write?
2. Is each invariant held by a constraint or one atomic write, where application code alone would race?
3. A change spanning several writes: what state does a crash after the first leave, and who repairs it?
4. Is a retried or repeated request safe to apply twice?
5. Does a save detect that the record changed since it was shown?
6. Is a derived or copied value able to go stale when its source changes?
7. Does a delete or rename leave references pointing at nothing?
8. Do records written before the change still read correctly after it?
9. Is a failed write surfaced to the caller, or swallowed into success?
10. Do a background job and a request touching the same record agree on who wins?
11. Does a cache serve state the write should have cleared?

**Not its business.** Who may write (`security`), whether callers agree on a shape (`contract`), query speed unless it changes correctness.

**Silent.** Yes: a lost update or a half-applied change passes every single-writer test and shows only as wrong data later.

**Applies.** declared.

**Run by.** auditor.
