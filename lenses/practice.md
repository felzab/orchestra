# Lens: practice

**Question.** Does each mechanism the change builds follow the way its tool's current documentation gives? A hand-built or outdated way drifts from the one the tool maintains.

**Read first.** The dependencies, platform interfaces and settings the change uses or adds, with the installed versions, then each one's documentation for that version.

**Checklist.**

1. Each mechanism built here: does the tool document a way to do it, and does this follow it?
2. Each interface, option or setting used: is it current for the installed version, or deprecated?
3. Is each limit, default or failure mode relied on as documented?
4. Does a new dependency have a maintained, widely adopted alternative, or a way that needs no dependency?
5. Does a workaround stand where the tool has since fixed the cause, or with no upstream issue named?
6. Does a documented way exist that the change considered and left without a stated reason?

**Not its business.** Whether the mechanism should exist (`essential`), a dependency's rights (`security`).

**Silent.** Yes: the hand-built way works until the tool changes beneath it.

**Applies.** paths `**/package.json`, `**/pyproject.toml`, `**/requirements*.txt`, `**/Cargo.toml`, `**/go.mod`, `**/Gemfile`, `**/pom.xml`, `**/build.gradle*`, `**/*.csproj`, `.claude-plugin/**`, `**/*.lock`, `**/package-lock.json`, `**/pnpm-lock.yaml`, `**/go.sum`.

**Run by.** researcher.
