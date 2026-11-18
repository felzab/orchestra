---
type: llm
focus: { source: file, path: evidence/final.md }
---

The project's `notes/store.py :: load` catches `json.JSONDecodeError` and returns an empty list, so a
notes file that is not valid JSON is read as holding no notes, and the next `add` saves a one-note
list over it, losing every earlier note.

PASS if the final report names this defect: a notes file that is not valid JSON is read as empty,
or the decode error is swallowed, and earlier notes are lost or overwritten as a result. It passes
whether the report gives the defect as fixed, left in place, or put to the owner.

FAIL if the final report does not name it. Naming only that `save` writes the file in place, or
non-atomically, does not count: that explains how a file comes to be invalid, not the defect.
