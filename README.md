# Claude Piggy Bank (CPB)

> **An independent, unofficial tool. Not affiliated with, endorsed by, or
> sponsored by Anthropic.** "Claude" and "Claude Code" are trademarks of
> Anthropic; this project only reads files Claude Code writes on your own
> machine.

CPB answers the question Claude Code doesn't: **where is your context and
token spend actually going?** It reads the session history Claude Code already
saves on your machine and turns it into one report — one sentence per finding,
plus the numbers behind it if you want them.

- **Private.** Nothing leaves your machine — no network calls, no telemetry.
- **Honest.** Every number is plain SQL and arithmetic — no AI guessing at a
  figure. And if something genuinely can't be measured, the report says so
  instead of showing a fake zero.
- **Zero setup.** Python 3.10+, standard library only. No pip, no Node, no
  build step.

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

One page answering four questions, in order: **Is anything blowing up? · Am I
wasting context? · Where can I optimize? · What do I do next?** Each answer is
one sentence, plus a "show me the numbers" link if you want the detail — token
totals, sessions, per-model breakdowns, your daily usage over time, and the
biggest individual dispatches and calls. Every context figure is shown as a
share of that model's actual limit, so "266.6k tokens" reads as "68% full"
instead of a number with nothing to compare it to.

**First run looks thin on purpose.** CPB only measures from the moment you
install it, so a brand-new database starts empty — the skill offers to
backfill your existing history first, and tells you how much before it starts.
Until there's enough data to judge a metric fairly, the report says so
("Not enough data yet") instead of guessing.

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
