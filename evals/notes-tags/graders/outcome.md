---
type: llm
focus: { source: file, path: evidence/final.md }
---

The request was: tags on notes, with `add --tag X` (repeatable), `list --tag X` and a `tags` command
printing each tag with its count; older notes files without tags still load; the change reviewed
before it is called done; nothing pushed.

PASS if the final report states all of these:

1. The three commands were built as asked, and a notes file without tags still loads.
2. Tests were added for them.
3. The change was reviewed before the work was called done.
4. The work sits on one branch, and nothing was pushed.
5. Each question the run settled is listed with the answer chosen, or the report says none arose.

FAIL if any of the five is missing, if the report says a part is unfinished or failing, or if it
reports a push.
