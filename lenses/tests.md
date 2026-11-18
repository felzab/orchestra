# Lens: tests

**Question.** Can each new or changed check fail, and does it test what its name claims?

**Read first.** The cases, guards and assertions the change adds or alters, then the code each one exercises.

**Checklist.**

1. Each case: which line of the code under test, once broken, turns it red? Plant that break and read the exit code.
2. Does it compare a thing to its own definition, or assert text nothing executed?
3. Does it run the real code, replacing only what the project does not own (network, clock, storage, platform)?
4. Does a stand-in answer as the real thing does, failures included?
5. Does the assertion check the behaviour the name states, or only that something ran?
6. Is the fix pinned at its call site as well as in a helper?
7. Does the case depend on order, time, a shared resource or another case's leftovers?
8. Was a timeout, retry count or tolerance raised, or a case skipped, to make it pass?
9. Does a guard or lint rule match the violation in every spelling the tree uses?
10. Does a changed behaviour leave an old case asserting the former one?

**Not its business.** Whether the tested behaviour is right (`data`, `security`, `contract`), whether a case is needed (`necessity`).

**Silent.** Yes: a case that cannot fail is green forever.

**Applies.** paths `**/tests/**`, `**/test/**`, `**/test_*`, `**/*_test.*`, `**/*.test.*`, `**/*.spec.*`.

**Run by.** driver.
