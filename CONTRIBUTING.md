# Contributing

Bug reports, ideas and pull requests are welcome. Open an issue from one of the issue forms before
starting a large change, so its design can be agreed first. Report a vulnerability as
[the security policy](SECURITY.md) describes, never in an issue. Everyone taking part follows
[the code of conduct](CODE_OF_CONDUCT.md).

## Set up

You need git, [uv](https://docs.astral.sh/uv/) and Claude Code.

```sh
git clone https://github.com/felzab/orchestra.git
cd orchestra
uv sync
```

## Try the plugin from a clone

```sh
claude --plugin-dir <path to your clone>
```

This loads the plugin for that session only, without installing it. Run `/reload-plugins` after
editing its files. The `orchestra` program it puts on the Bash tool's `PATH` needs Python 3.11 or
newer on your machine. Where the plugin is also installed, a bare `orchestra` was seen running the
installed copy while the hooks ran the clone's, so run the clone's as `bash <clone>/bin/orchestra`.

## Checks

A pull request must pass each of these with exit code 0:

```sh
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest -n auto
claude plugin validate --strict .
claude plugin test .
```

CI runs the first four on Linux and Windows, at Python 3.11 and the latest Python release, and
validates the plugin and runs its mod's tests.

## Evaluations

`evals/` holds one case that `claude plugin eval` runs: `notes-tags`, a whole session that adds one
feature to a small notes program holding a defect the review should find. Run it before merging a
change to what the coordinator or an agent does: a page under `skills/`, `agents/` or `lenses/`, the
hooks, or a command the pages tell them to run. CI does not run it, since every run spends model
usage.

The suite measures only what the harness allows. `claude plugin eval` refuses every agent a worktree
of its own, so no writer or driver can run in it: the case has the coordinator write the change and
decline the lenses a driver runs, and nothing here measures writers working in parallel or a
driver's drive. A run also loads none of the project's `.claude/` settings, so the setup
[the usage guide](docs/usage.md#setting-up-a-project) describes is not measured either.

A run grants the Bash tool, which Claude Code allows only under its sandbox, and native Windows has
none. Run the suite on Linux, or in WSL2 on Windows, with `bubblewrap` and `socat` installed, git
2.31 or later, Python 3.11 or later, which the plugin's hooks need there, and Claude Code installed
and signed in there, 2.1.326 or later, the release the runs below used: an earlier one is untested.
A run fails before its session starts where:

- Docker's configuration folder, `~/.docker` or the folder `DOCKER_CONFIG` names, holds a symbolic
  link inside it, as Docker Desktop's WSL integration makes in `~/.docker/contexts`: keep its
  contents in one plain folder;
- `TMPDIR` is a long path: the sandbox, which puts its sockets under it, failed with one of 52
  characters and started with one of 28;
- `TMPDIR` is unset or under `/tmp`: the run's folder then sits under `/tmp`, which the sandbox the
  harness writes denies, so every shell command fails on a read-only file system and the session
  never starts (score 0.10, $0.00, a few seconds, with 2.1.326). Point it at a short folder of your
home that
  exists, such as `~/t`.

From the root of a clone in WSL's own file system, such as `~/orchestra`, since the sandbox denies
reading `/mnt/c`:

```sh
mkdir -p ~/t
TMPDIR="$HOME/t" claude plugin eval . --scaffold --ablation none --max-cost-usd 30 --allow-tools Bash Write Edit SendMessage
```

- `--scaffold` runs each case's `fixture.sh`, which builds the project the session works on.
- `--ablation none` runs each case with the plugin only: without the plugin, the prompt's
  `/orchestra:run` is no command, so a second arm measures nothing and doubles the cost.
- `--allow-tools` grants the tools the coordinator and its agents use beyond the read-only ones.
- `--max-cost-usd` starts no further run once the estimated spend reaches it.

The first run in a clone asks whether to trust the folder, which a run without a terminal cannot
do: there, pass `--trust-plugin` instead. The case pins its model and runs once, since a run costs
model usage, and a single run is noisy: confirm a change in its score with `--runs 3` before acting
on it. The case scores below 1.0 when a grader fails, and the command then exits 1; the command
writes its report to `evals/results/<time>/report.html`, and `--keep-temp` keeps each run's sandbox
and transcript. Two runs with Claude Code 2.1.326 took 7 and 10 minutes and cost about $4.80 and
$4.40 by the `COST` column, which gives each run's own estimate; a third, with 2.1.326 and `TMPDIR`
under home, took 8 minutes and $5.47.

## What the repository publishes

The maintainer's name in `LICENSE` and in the two manifests under `.claude-plugin/`, the
maintainer's GitHub handle in each address of the repository, and the maintainer's GitHub noreply
address on each commit are published on purpose. No other person's name, no path of one machine
and no private project's name belongs in a file or a commit message.

## Commits and pull requests

Send one pull request per change, and fill in its template.

Each commit subject reads `Scope: what changed`, in at most 72 characters, since GitHub cuts a
longer subject short in its commit lists, for example
`Docs: The install steps name the marketplace command`. The scope is one of these, or two joined by
` + ` for a commit that spans both:

| Scope   | What it covers                                                     |
| ------- | ------------------------------------------------------------------ |
| `Tool`  | `lib/` and `bin/`                                                  |
| `Hooks` | `hooks/`                                                           |
| `Pages` | `skills/`, `agents/` and `lenses/`                                 |
| `Docs`  | the documentation                                                  |
| `Tests` | `tests/`, where a commit changes nothing else                      |
| `CI`    | `.github/`                                                         |
| `Repo`  | the manifests, `evals/`, `pyproject.toml`, the schema and the rest |

A merge commit's subject is `Merge <name> into <branch>`, as `orchestra land NAME` writes it; give
your own merge that subject with `git merge -m`.
