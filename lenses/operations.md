# Lens: operations

**Question.** When this fails in the field, who learns, from what text, and how is it rolled back? A failure nobody is told of lasts until someone trips over it, which is why a launch review asks how each failure is detected.

**Read first.** The deploy, CI, install and hook files the change touches, then the document that tells an operator how to run them.

**Checklist.**

1. Each failure the change can meet: who learns of it, and from what message?
2. Does that message name what to do next?
3. Does a guard or check pass on its own error, failing open?
4. Does a step that prints success check that it succeeded?
5. Each step: is there a way back, written where the operator looks?
6. What does a first run on a clean machine need that nothing installs or names?
7. Is each wait, retry and timeout bounded, with its limit stated?
8. Does each setting have a default, or fail with its name when absent?

**Not its business.** Who may call what (`security`), stored state (`data`).

**Silent.** Yes: every check passes, and the gap shows only in an incident.

**Applies.** paths `.github/workflows/**`, `.gitlab-ci.yml`, `**/Dockerfile*`, `**/*compose*.y*ml`, `hooks/**`, `bin/**`, `deploy/**`, `.claude/settings.json`.

**Run by.** auditor.
