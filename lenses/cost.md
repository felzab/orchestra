# Lens: cost

**Question.** What does one event on this path cost in time, memory or tokens, and how often is it paid? Reading only nominates suspects, so each answer is a measurement, as a launch review's load test is.

**Read first.** The paths the project declares hot and the changed code on them, then the commit the change starts from, which is the baseline.

**Checklist.**

1. Which changed code runs per request, per event, per file or per tool call, and how often in a session?
2. Its cost at the baseline and at the tip, with what else ran.
3. Does it do on every event work that only a rare case needs?
4. Does its work grow faster than its input: a loop inside a loop, a read per item?
5. Does it start a process, load a module or read a file on each event that it could hold once?
6. Does its output grow with the data, so that every reader pays to read it?
7. Where a gain is within the spread, is it reported as no gain?

**Not its business.** Whether the path is correct, or should exist (`essential`).

**Silent.** Yes: a slow path passes every test, and its cost shows only at scale.

**Applies.** declared.

**Run by.** driver.
