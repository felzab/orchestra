---
description: Type it when you return. Ends the away mode, reports what changed, each call refused and each decision taken alone, and asks the questions that waited.
disable-model-invocation: true
allowed-tools: Bash(orchestra *)
---

First run `orchestra mode attended` and then `orchestra status`, each with the Bash tool. The first lists the log since the away began, each call refused meanwhile (its `refused` lines) and the questions that waited; the second is the state. Where the first has no line opening `attended;`, the away still holds: take the state over as its refusal says, then run `orchestra mode attended` again and read that instead. Where the first opens on `earlier line(s)`, read the log it names from its last `away` line on before you reply, since items 1 and 3 rest on those lines. Reply in this order:

1. What changed since the owner left: the landings, each push with its CI verdict, the reports judged, and any usage stop with how the session went on.
2. Each refused call but an `AskUserQuestion` or `ExitPlanMode`, which the away itself refuses, with the permission rule that would have let it through, since the owner decides which to add.
3. Each decision taken alone: what it decided, where it is, and what reversing it costs.
4. What waits on the owner now.

Then ask those questions through the ask tool, four at most in a batch, each naming the provisional build it confirms or reverses, and keep or reverse each build to match its answer, recorded as a ruling.
