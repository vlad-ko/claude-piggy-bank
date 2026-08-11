# Claude Piggy Bank (CPB)

> **An independent, unofficial tool. Not affiliated with, endorsed by, or
> sponsored by Anthropic.** "Claude" and "Claude Code" are trademarks of
> Anthropic; this project only reads files Claude Code writes on your own
> machine.

CPB answers a question Claude Code does not: **where does your context and
token spend actually go?** It reads the session transcripts Claude Code already
writes to your disk, loads them into SQLite, and serves a single-page report
over them — per session, per model, main thread against subagents.

- **Nothing leaves your machine.** No network calls, no telemetry, no CDN. The
  page renders your own prompts, paths and source code, so both browser
  libraries it uses are vendored into `vendor/` and served from there.
- **No model produces a figure.** Every number is SQL, arithmetic or JSON
  parsing. Ingesting and reading the report is free and offline. (The
  in-session skill *reads* the finished report and summarises it, so it spends
  tokens in a session you are already paying for — it never computes a figure.
  The line is drawn in [`docs/plugin.md`](docs/plugin.md).)
- **A number that cannot be trusted refuses.** Absence is reported as
  "not measured" or INCONCLUSIVE, never as a plausible `0`, and there are no
  dollar estimates — [`docs/metrics.md`](docs/metrics.md) is the long form.
- **Python 3.10+, standard library only.** No pip, no Node, no build step. CI
  runs the suite on 3.10–3.13 and fails the build if any import in any shipped
  module resolves outside the standard library.

**Contents:** [Install](#install) · [What you get](#what-you-get) ·
[Documentation](#documentation) · [Contributing](#contributing)

## Install

The plugin is the path to take if you intend to keep using CPB: it ingests each
transcript as it is written, including subagent transcripts, which Claude Code
reaps. From inside Claude Code:

```
/plugin marketplace add vlad-ko/claude-piggy-bank
/plugin install claude-piggy-bank@claude-piggy-bank
```

or, without starting a session:

```bash
claude plugin marketplace add vlad-ko/claude-piggy-bank
claude plugin install claude-piggy-bank@claude-piggy-bank
```

**Both lines are needed.** `marketplace add` registers the catalog and installs
nothing; skipping the second leaves you with a marketplace and no plugin, which
looks like a successful install until the first time you reach for the report.
The name is doubled because the marketplace and the plugin are the same
repository. If the install summary says `Run /reload-plugins to activate.`, run
it.

Then, from any session, open the report with:

```
/claude-piggy-bank:cpb
```

Three hooks (`SubagentStop`, `Stop`, `SessionEnd`) do the ingesting from there
on; no edit to any `.claude/settings.json` is needed, and the database lives in
`${CLAUDE_PLUGIN_DATA}`, which survives plugin updates.

**Or read the code first and run it by hand.** Same code, no dependencies,
nothing added to your `PATH`:

```bash
git clone https://github.com/vlad-ko/claude-piggy-bank.git
cd claude-piggy-bank
python3 cpb.py ingest
python3 cpb.py serve      # then open http://127.0.0.1:8377/
```

Skills-directory installs, upgrading, uninstalling without losing your database,
and where that database lives: [`docs/install.md`](docs/install.md). Flags,
`CPB_DB` and single-transcript ingest: [`docs/cli.md`](docs/cli.md).

## What you get

One page, served on loopback, answering four questions in order — **Is anything
blowing up? · Am I wasting context? · Where can I optimize? · What do I do
next?** — over totals for input, cache-read, cache-write and output tokens,
sessions, API calls split main-thread against subagent, and a headline
**median context per call**.

Under those: daily token usage over time, usage by model, every session with a
per-session breakdown (by scope, turn type and model), the top subagent
dispatches ranked by total tokens, and the outlier calls by cache-read. Each
call's context is divided by **that model's own documented context window**, so
"266.6k tokens" becomes a share of a published hard limit rather than a number
with no referent.

**A first run deliberately looks thinner than you expect.** CPB starts
measuring when you install it, so a new database is empty by construction — the
skill checks whether there is unmeasured history already on disk and offers to
backfill it, stating the size and an estimate before it starts. And until a
metric has enough calls to judge, the page shows you the reading and withholds
the verdict: `TOO FEW`, or **"Not enough data yet"** where a settled corpus
would show a green dot. That is the tool refusing to certify a clean bill of
health on three calls, not a fault.

**A settled run** adds the things only history can show: which sessions and
subagent dispatches actually carry the spend, whether your cache writes are
being repaid, and where context sits against each model's window. Above the
totals it always states when it last *looked* at your transcripts and the
newest call it *found* — two separate facts, because either alone misleads.

What each figure means, and what it refuses to say:
[`docs/metrics.md`](docs/metrics.md).

## Documentation

Longer-form reference lives in [`docs/`](docs/) and is indexed by
[`docs/README.md`](docs/README.md) — a document not listed there is treated as
an orphan.

| | |
|---|---|
| **Getting it running** | [Installing CPB](docs/install.md) · [The command line](docs/cli.md) |
| **Reading the numbers** | [What each figure means](docs/metrics.md) · [Where the data comes from](docs/data-sources.md) · [Claude API token accounting](docs/claude-api-token-accounting.md) |
| **How it is packaged** | [The Claude Code plugin](docs/plugin.md) |
| **What changed, and what was wrong** | [Release record](docs/releases.md) · [The record of corrections](docs/corrections.md) · [Versioning](docs/versioning.md) |

Which release you are running is a question for the build, not for prose: run
`python3 cpb.py --version`. The report answers it too — it names the build that
produced its numbers, and `/api/summary` carries the same string as
`build.version`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), which also records how to run the test
suite and which selection form works for which module. The short version:
measurements need provenance, and a number that cannot be trusted should refuse
rather than guess.

CPB was extracted in August 2026 from a private monorepo, where it began as an
internal tool for measuring one team's Claude Code spend. History was not
carried across, so design rationale lives in `CLAUDE.md`, in `docs/`, and in
code comments rather than in commit messages — including comments citing
`#NNNN` issue numbers from that repository, which record why a decision was
made and will not resolve publicly.

## License

MIT — see [LICENSE](LICENSE).
