# Lens: essential

**Question.** Should each thing the change adds or keeps exist?

**Read first.** The project's goals as its brief or its page names them, then the list of mechanisms in the change: each check, guard, helper, option, document step and step a person must take.

**Checklist.**

1. For each mechanism, on what evidence is its harm likely?
2. Does a library, the platform or a static check already guarantee it?
3. Does it handle a case that cannot occur, or serve a caller that does not exist?
4. A test case is `necessity`'s to judge; judge here a test helper or fixture.
5. Is an abstraction drawn from fewer than three real instances?
6. Does a documented standard way replace something hand-built here?
7. Is a fact kept twice, as a comment or document restating the code?
8. Was it added only to close a finding, with no harm named?
9. What could be deleted with the change still meeting its goal?
10. Does it add a second way to do a job, a fix at a symptom rather than its cause, a switched-off check, or production code only a test calls?

Record a mechanism whose harm is unlikely or already seen as a finding whose repair is its removal: `defect` where the goals rule it out, `optional` otherwise.

**Not its business.** Whether what stays is correct; that is every other lens's. Propose no new mechanism.

**Silent.** Yes: a needless mechanism passes every check, and every other lens rewards adding one.

**Applies.** always.

**Run by.** auditor.
