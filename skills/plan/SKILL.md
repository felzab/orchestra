---
description: Type it for work too large for one session. Plans a programme of several orchestra sessions and writes its plan and first starter, both audited.
disable-model-invocation: true
argument-hint: "[the programme's goal]"
allowed-tools: Bash(orchestra *)
---

!`orchestra status || true`

The programme to plan: $ARGUMENTS

A programme is planned in a session of its own. Where the block above shows none, read `${CLAUDE_PLUGIN_ROOT}/skills/run/start.md` and start one with `orchestra init <session> --programme <programme>`, since the audit below needs an agent and one programme name gives every session one list of rulings.

1. **Ask first.** Send the owner one batch of questions on the goal, the constraints and what is out of scope.
2. **The path.** Lay the whole programme out as sessions, each one branch and one pull request. Size a session to be worth its own branch without filling one coordinator's context, and give a phase whose verdict must stand alone a session of its own.
3. **Each session's entry.** Its scope, its exit condition, the files it is expected to write, and what it needs from an earlier session.
4. **Each mechanism the plan adds** names the harm it prevents, how likely that harm is and who would see it without the mechanism. One that cannot is cut from the plan.
5. **The critical path.** Which sessions depend on which.
6. **The first starter**, in the form `${CLAUDE_PLUGIN_ROOT}/skills/run/handoff.md` gives.
7. **The audit.** An auditor with `Lens: prompt` reads the plan and the starter cold. Fix what it finds before the owner sees either.
8. **Where they live.** The plan, the starters and the handoffs stay in the programme's folder, the one holding each session's state, which `init` made a working directory. Each starter names the programme for its session's `init`.

Reply with the plan's path, the starter's path and the audit's verdict.
