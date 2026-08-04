# Claude Piggy Bank (CPB)

> **An independent, unofficial tool. Not affiliated with, endorsed by, or
> sponsored by Anthropic.** "Claude" and "Claude Code" are trademarks of
> Anthropic; this project only reads files Claude Code writes on your own
> machine.

CPB answers a question Claude Code does not: **where does your context and
token spend actually go?** It ingests the session transcripts Claude Code
already writes locally into SQLite and serves a single-page report over them.

- **Python 3 standard library only.** No pip installs, no Node, no build step.
- **Nothing leaves your machine.** No network calls, no telemetry, no CDN — the
  chart library is vendored so the page renders fully offline.
- **No model in the loop.** Every number is arithmetic, SQL, or JSON parsing.
  Running CPB costs nothing.

That last point is deliberate. This is a tool for understanding cost; it would
be a poor one if using it cost anything.

## Status: early, and honest about it

CPB is pre-1.0 and **some of its headline numbers are known to be wrong.**

`api_calls` currently counts transcript *records*, and Claude Code writes one
record per streamed content block, each repeating the same `message.usage`
object. Measured on one real corpus: **84,986 records against 35,842 distinct
`message.id`** on the main thread. Call counts and the token totals derived
from them are therefore inflated roughly **2–2.8×** — with a *different* factor
for subagents than for the main thread, which distorts comparisons in shape and
not only in magnitude.

This is tracked and being fixed. Until it lands, treat totals as upper bounds.

Publishing a tool whose own README says its numbers are under review is a
choice. The alternative was to fix it privately first, and the point of this
project is that measurement should be inspectable — including when it is wrong.

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
