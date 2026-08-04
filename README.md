# Claude Piggy Bank (CPB)

> **An independent, unofficial tool. Not affiliated with, endorsed by, or
> sponsored by Anthropic.** "Claude" and "Claude Code" are trademarks of
> Anthropic; this project only reads files Claude Code writes on your own
> machine.

CPB answers a question Claude Code does not: **where does your context and
token spend actually go?** It ingests the session transcripts Claude Code
already writes locally into SQLite and serves a single-page report over them.

- **Python 3.10+, standard library only.** No pip installs, no Node, no build
  step. The floor is tested, not assumed: CI runs the suite on 3.10, 3.11, 3.12
  and 3.13, and a separate job fails the build if any import in any shipped
  module resolves outside the standard library.
- **Nothing leaves your machine.** No network calls, no telemetry, no CDN — the
  chart library is vendored so the page renders fully offline.
- **No model in the loop.** Every number is arithmetic, SQL, or JSON parsing.
  Running CPB costs nothing.

That last point is deliberate. This is a tool for understanding cost; it would
be a poor one if using it cost anything.

## Status: early, and honest about it

CPB is pre-1.0. The defect that made its headline numbers wrong is **fixed**;
what follows is the record, because a tool about measurement should show its
own corrections rather than quietly restate them.

`api_calls` used to count transcript *records*. Claude Code writes one record
per streamed content block, and each repeats the same `message.usage` object,
so a single API response was counted many times. On one real corpus:

| | counted before | actual | inflation |
|---|---|---|---|
| main-thread calls | 85,324 | 36,167 | 2.36x |
| subagent calls | 242,242 | 126,757 | 1.91x |
| estimated cost | $144,697 | $65,776 | 2.20x |

The factor differed by scope, so comparisons were distorted in **shape**, not
merely in magnitude. The clearest casualty: main-thread spend appeared to
dominate subagent spend, $83k against $61k. Deduplicated, it is **$33,073
against $32,703** — a dead heat. A conclusion, not just a scale, was wrong.

Ingest now emits one row per distinct `message.id`. The resolution rule is
measured rather than assumed: across that corpus `output_tokens` is
non-decreasing over the records of one id in **4,928 of 4,928** cases and only
the final record carries the finished total, so the last record wins. Taking
the first would have under-counted output by roughly 99%.

Two residual cases are counted and printed rather than hidden. **109 ids**
whose records disagreed on something other than `output_tokens` keep one whole
real record, never a per-field maximum -- that would report a call which both
wrote and did not write cache, a combination present in no real response.
Records with **no** `message.id` each stay their own call; on this corpus there
were none, but dropping them would delete real spend and grouping them would
merge unrelated calls.

Cost figures remain list-rate estimates, not a bill -- see below.

Publishing a tool whose README documented its own broken numbers was a choice.
The alternative was to fix it privately first, and the point of this project is
that measurement should be inspectable, including when it is wrong.

## Install

```bash
git clone https://github.com/vlad-ko/claude-piggy-bank.git
cd claude-piggy-bank
```

That is the install. There are no dependencies.

## Use

```bash
# Ingest your transcripts (idempotent and incremental — run any time)
python3 ingest.py

# Serve the report
python3 serve.py
# then open http://127.0.0.1:8377/
```

`ingest.py` defaults to **this project's own** transcript directory, derived
from the repository root using Claude Code's naming convention (the absolute
path with each separator folded to `-`). To analyse a different project, pass
it explicitly:

```bash
python3 ingest.py --projects-dir ~/.claude/projects/<name>
```

If the derived directory does not exist, CPB refuses and lists the projects
that *do* have transcripts, rather than reporting an empty run.

## Where the data comes from

Claude Code writes every session to disk as JSONL. CPB reads two globs:

| source | path |
|---|---|
| main thread | `~/.claude/projects/<project>/<session-id>.jsonl` |
| subagents | `~/.claude/projects/<project>/<session-id>/subagents/agent-<id>.jsonl` |

Plus a third path that is an **index, not a source**: the harness task
directory under the OS temp dir. Most of its entries are symlinks into the
canonical subagent store, so ingesting them as data would double every subagent
figure. It is read only for dispatch attribution and for detecting runs whose
transcript has been reaped.

Every `assistant` record carrying a `message.usage` block is one API call, with
its four token classes: input, cache-read, cache-write, and output.

### Transcripts expire — back up the database

Claude Code deletes transcripts after `cleanupPeriodDays`, which **defaults to
30** and is usually left unset. That bound is on the *source*, so it bounds what
CPB can ever measure going forward: a session older than the window cannot be
ingested for the first time, because there is nothing left to read.

What CPB does not do is compound the loss. Once a source is gone its
measurements are **kept** and marked archived; they are excluded from
"what is currently on disk" coverage counts but stay in every historical total.
Pruning them is available as an explicit `--prune-missing`, never a default, and
a schema upgrade that would destroy them **refuses to run** rather than
rebuilding over them.

The practical consequence: past the retention window, **`db/usage.db` is the
only copy of your history**. It is not derived data you can drop and regenerate.
Back it up, and raise `cleanupPeriodDays` in `~/.claude/settings.json` if you
want a longer window — though the tool is designed to be correct at the default,
not only when configured.

Any window containing archived sources says so in the report banner: the totals
are complete, but they are no longer reproducible by re-ingesting.

## Two design rules worth knowing

These explain decisions in the code that otherwise look odd.

**Absence is never rendered as a value.** A read that cannot produce a
trustworthy answer refuses — null, inconclusive, or a loud operator-visible
message — rather than returning a plausible number. A parse failure is counted
and surfaced, never silently coerced to `0`. A session with no subagent
transcripts on disk reports "not measured", a different fact from a measured
zero, and the two stay distinguishable everywhere.

The sharpest case: Claude Code persists thinking blocks with **empty text**, so
their size is recorded as *unknown* rather than `0`. Recording `0` would make a
composition table state that thinking costs nothing — false, and invisible,
because it looks like a measurement.

**Cost figures are an estimate, never a bill.** Every dollar amount is
list-rate arithmetic over measured token counts, using a hand-maintained rate
table carrying the date it was last checked. It does not model subscription
accounting, discounts, or overage. On one real session this tool's arithmetic
produced ~$57 where the subscription-accounted spend was ~$21 — overstating by
more than 2.5×. Token counts (measured) are therefore kept visually and
semantically distinct from dollar figures (derived) throughout.

## Tests

```bash
python3 -m unittest discover tests -v
```

Fixtures are hand-built and synthetic — no captured session content. They pin
deliberately unequal values per token class so a swapped column mapping cannot
pass, and include a deliberately malformed line asserting that parse failures
are counted rather than swallowed.

## Provenance

CPB was extracted in August 2026 from a private monorepo, where it began as an
internal tool for measuring one team's Claude Code spend. History was not
carried across — this repository starts from a single import commit — so design
rationale that had lived in commit messages is stated here and in code comments
instead.

Some code comments cite `#NNNN` issue numbers from that private repository.
They record *why* a decision was made and are kept for that reason; the links
themselves will not resolve publicly.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: measurements need
provenance, and a number that cannot be trusted should refuse rather than
guess.

## License

MIT — see [LICENSE](LICENSE).
