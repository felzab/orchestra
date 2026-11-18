# Lens: contract

**Question.** Does each side of a boundary between slices, layers or tools still hold what the other relies on?

**Read first.** Each boundary the change crosses (an interface, a schema, a file format, a command's flags), then each side's use of it.

**Checklist.**

1. Each changed signature, field, status or exit code: is every caller and reader changed with it?
2. Does a consumer rely on what the producer never promised (an order, a default, a field)?
3. Is a generated file (a client, a schema, a lockfile) regenerated from the changed source?
4. Do slices written apart assume one name, path, unit and encoding?
5. Does an error keep the shape its handler matches on?
6. Is a renamed or removed thing still cited, or a changed behaviour described as it was, in code, comments, tests or prose?
7. Does a consumer of the old version keep working, or is the break stated?
8. Is a value validated on one side to the range the other side accepts?
9. Where a rule names its enforcing check, does the check enforce all of it?
10. Each unchanged function the change newly calls: does it do what its name and documentation say, order and ties included? Name what reading cannot settle for a driver.

**Not its business.** Defects inside one side (`data`, `security`), whether the boundary should exist (`essential`).

**Silent.** Yes: each side passes its own tests; the mismatch shows only where both run.

**Applies.** paths `**` except `**/*.md`.

**Run by.** auditor.
