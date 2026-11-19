---
description: Type it when the work is done. Runs the orchestra session's ending, from the last landing through the review and the gate to the audited handoff where one is owed, and starts no new work.
disable-model-invocation: true
allowed-tools: Bash(orchestra *)
---

Read `${CLAUDE_PLUGIN_ROOT}/skills/run/end.md` and run it to its end, starting from the block below.

!`orchestra status --end || true`
