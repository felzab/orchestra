# Lens: essential

**Question.** Should each thing the change adds or keeps exist?

**Read first.** The project's goals as its brief or its page names them, then the list of mechanisms in the change: each check, guard, helper, option, document step and step a person must take.

**Checklist.**

1. For each mechanism: what concrete harm does it prevent in this project as it is?
2. How likely is that harm, and on what evidence?
3. Would a reader, a failing run or CI see the harm without the mechanism?
4. Does a library, the platform or a static check already guarantee it?
5. Does it handle a case that cannot occur, or serve a caller that does not exist?
6. A test case is `necessity`'s to judge; judge here a test helper or fixture.
7. Is an abstraction drawn from fewer than three real instances?
8. Does a documented standard way replace something hand-built here?
9. Does a comment or document restate what the code says?
10. Was it added only to close a finding, with no harm named?
11. What could be deleted with the change still meeting its goal?

Record a mechanism whose harm is unlikely or already seen as a finding whose repair is its removal: `defect` where the goals rule it out, `optional` otherwise.

**Not its business.** Whether what stays is correct; that is every other lens's. Propose no new mechanism.

**Silent.** Yes: a needless mechanism passes every check, and every other lens rewards adding one.

**Applies.** always.

**Run by.** auditor.
