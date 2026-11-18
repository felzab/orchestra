# Lens: necessity

**Question.** Does each test case guard a harm that would go unnoticed and that nothing lower already guards? A test is code every change pays to run and keep.

**Read first.** The cases the change adds or alters, the suite's time by file as the runner's own durations report gives it, then the code each case exercises.

**Checklist.**

1. Each case: what harm reaches a user once it is removed, and would a reader, a static check or another case see it?
2. How likely is that harm here, and on what evidence?
3. Does a type check, a library's guarantee or the platform already hold it?
4. Does a lower tier or another case already see the same behaviour?
5. Do cases that differ only in data become one parametrised case, or keep only the edge values?
6. Does a case assert a constant, a text or the code's own definition?
7. Which file costs the most seconds, and what would cutting it lose?

Give each case a verdict of keep, remove or merge, and the suite's time by file before and after the cuts, measured.

**Not its business.** Whether a kept case can fail (`tests`), whether the behaviour is right.

**Silent.** Yes: a needless case stays green, and its cost is paid on every run.

**Applies.** paths `**/tests/**`, `**/test/**`, `**/test_*`, `**/*_test.*`, `**/*.test.*`, `**/*.spec.*`.

**Run by.** driver.
