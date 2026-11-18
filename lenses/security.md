# Lens: security

**Question.** Can a caller reach, read or change what it should not, and does a secret or a person's data leave?

**Read first.** The project's statement of who may do what, then every entry point the change adds or alters: a route, a handler, a command, a job, a hook.

**Checklist.**

1. Each new or changed entry point: who may call it, and where is that checked?
2. Is the check on the server side of the boundary, before any read or write?
3. Can an identifier in the request name another person's record?
4. Does input reach a query, a shell, a path, a template or markup unescaped?
5. Is a redirect or fetch target taken from input checked against a list?
6. Does a secret reach a log, an error message, a response, a commit or the client's bundle?
7. Does a response or a log carry more of a person's data than its reader needs?
8. Does a state-changing request prove it came from the application's own page?
9. Does a new dependency, script or action run with more rights than its job needs, pinned to a version?
10. Does a failure fall back to allowing?
11. Is a limit on attempts or size enforced where the cost is paid?

**Not its business.** Whether stored state stays consistent (`data`), whether a check can fail (`tests`), style.

**Silent.** Yes: a missing check serves every honest request and shows nothing until someone abuses it.

**Applies.** paths `**` except `**/*.md`.

**Run by.** auditor.
