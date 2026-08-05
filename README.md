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

CPB ships as a **Claude Code plugin**. Enabling it activates three hooks that
ingest each transcript as it is written, so the report stays current without
anyone remembering to run anything.

There is no marketplace yet, so install by cloning into your personal skills
directory. Claude Code loads any folder there that contains a
`.claude-plugin/plugin.json` as a plugin on the next session — no install step,
no copy into a cache:

```bash
git clone https://github.com/vlad-ko/claude-piggy-bank.git \
  ~/.claude/skills/claude-piggy-bank
```

Restart Claude Code and it loads as `claude-piggy-bank@skills-dir`.

To try it without installing anything — for development, or to read the code
before trusting it with your history:

```bash
git clone https://github.com/vlad-ko/claude-piggy-bank.git
claude --plugin-dir ./claude-piggy-bank
```

Either way there are no dependencies: the plugin *is* this repository, the hooks
run the same `ingest.py` you would run by hand, and nothing is compiled or
fetched.

### What the hooks do

Enabling the plugin activates three triggers. **No edit to any
`.claude/settings.json` is needed.**

| trigger | when it fires | what it ingests |
|---|---|---|
| `SubagentStop` | a subagent finishes | that subagent's own transcript |
| `Stop` | Claude finishes a response | the session transcript, per turn |
| `SessionEnd` | the session ends | the session transcript, best-effort |

Each one spawns `ingest.py` for **exactly one file** — no directory scans, no
network, no model. `SubagentStop` is the one that earns its place: subagent
transcripts are reaped, and on the reference corpus 211 subagent runs are
already permanently unmeasurable while subagents are ~78% of all API calls.
Ingesting the moment a subagent finishes is the difference between capturing
that spend and losing it.

If an ingest fails, the hook says so in one line in your transcript and appends
it to `cpb-hook.log` beside the database — and then **gets out of the way**. It
never blocks a turn, never stops a subagent, and never reports success for work
it did not do. The reasoning, and the plugin design generally, is in
[`docs/plugin.md`](docs/plugin.md).

### Where the plugin keeps your database

`${CLAUDE_PLUGIN_DATA}/usage.db` — a directory that survives plugin updates.
Set `CPB_DB` yourself to override it; the hooks will not touch a value you have
chosen.

`/cpb` opens that database for you. If you run the server by hand instead, pass
it explicitly — `serve.py` reads only `--db`, not `CPB_DB`, so without the flag
it opens its own default and shows you an empty report rather than an error:

```bash
python3 serve.py --db ~/.claude/plugins/data/<plugin-id>/usage.db
```

### Upgrade

```bash
git -C ~/.claude/skills/claude-piggy-bank pull
```

Then `/reload-plugins`, or restart — hook changes are not picked up mid-session.
Your database is not in the plugin directory, so an upgrade cannot disturb it.

### Uninstall

Delete the folder; nothing was installed from a marketplace, so there is no
uninstall step. To stop loading it without deleting:

```bash
claude plugin disable claude-piggy-bank@skills-dir
```

**Back up the database first.** It is not in the folder you are deleting, but
if you ever install from a marketplace instead, `claude plugin uninstall`
deletes the plugin's data directory by default unless you pass `--keep-data` —
and past Claude Code's ~30-day transcript retention that database is the only
copy of your history. See
[Transcripts expire](#transcripts-expire--back-up-the-database).

## Use

From inside Claude Code, once the plugin is enabled:

```
/cpb
```

That starts the report server and gives you the URL. Everything below works the
same from a plain checkout, with or without the plugin.

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

### The report tells you how old its data is

`serve.py` reads whatever the database holds, and `ingest.py` never runs on its
own — so the report is exactly as current as your last ingest, and a page left
open for a week would otherwise look identical to one opened a second ago.

Directly above the totals the report states **two facts, never merged into one
"data age"**:

| line | what it means |
|---|---|
| **Last ingest** | when `ingest.py` last completed — when this tool last *looked* at the transcripts |
| **Newest measured call** | the most recent API call in the database — the newest thing it *found*, across **all** ingested data, not the selected window |

Both, because either alone misleads. A fresh ingest on a machine you have not
used since lunch is perfectly healthy and shows an old newest-call. A database
nobody has re-ingested for a week can show a recent newest-call — for the last
thing it ever saw. Only the pair is readable.

**Past 15 minutes since the last ingest, the report raises a banner** in the
same place as its unpriced-model and archived-source warnings, saying the
figures describe the transcripts as of then rather than as of now. The
threshold is measured, not taste: re-ingesting is incremental, and on the
largest corpus available here (2,891 transcripts, 1.9 GB, macOS, checked
2026-08-04) an all-skipped re-run took **1.8 s** against **39.9 s** for a cold
full parse — so anyone re-running `ingest.py` on any reasonable cadence never
sees it. It marks neglect, not latency. The warning applies to the *ingest run*
only; an idle machine that produced no calls for hours is not stale.

A database written before CPB recorded ingest times (schema v6 and earlier)
reads **"Last ingest: not recorded"**. That is an *unknown* age, not an age of
zero and not a permanent staleness warning — run `ingest.py` once and it starts
recording. Upgrading such a database does not re-parse anything: adding this
was a purely additive schema change, applied in place, keeping every row.

There is no automatic refresh yet — refreshing means running `ingest.py` again.
Scheduling that from inside `serve.py` is [issue #20](https://github.com/vlad-ko/claude-piggy-bank/issues/20)'s
second half and ships separately.

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

## Documentation

Longer-form reference lives in [`docs/`](docs/), indexed by
[`docs/README.md`](docs/README.md). The one to know about:

- [Claude API token accounting](docs/claude-api-token-accounting.md) — the API
  accounting facts the analysis rests on, each with its source URL and the date
  it was checked. Thinking is billed as output and is invisible in transcript
  content; whether it is re-billed as input on later turns depends on the
  model; cache writes carry a markup and the cacheable minimum is a per-model
  lookup, not a constant.

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
