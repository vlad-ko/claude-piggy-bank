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
- **Nothing leaves your machine.** No network calls, no telemetry, no CDN. Both
  browser libraries the report uses — Chart.js for the plot, Alpine.js for the
  bindings — are vendored into `vendor/` and served from there, so the page
  renders fully offline. `vendor/README.md` records each one's version, origin
  and SHA-256, and the test suite re-checks those digests and that neither
  bundle contains a way to reach the network.
- **No model in the loop.** Every number is arithmetic, SQL, or JSON parsing.
  Running CPB costs nothing.

That last point is deliberate. This is a tool for understanding what your
sessions consume; it would be a poor one if using it consumed anything.

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

The factor differed by scope — **2.36x against 1.91x** — so any main-thread
versus subagent comparison was distorted in **shape**, not merely in
magnitude: the two sides were scaled by different amounts before being read
against each other. A conclusion, not just a scale, was wrong.

Ingest now emits one row per distinct `message.id`, and the surviving row is
the record with the **greatest `output_tokens`**, ties resolving to the later
record. Taking the first would have under-counted output by roughly 99%.

That rule is often described as "the last record wins", and on the corpus above
the two were the same record every time: `output_tokens` was non-decreasing
over the records of one id in **4,928 of 4,928** cases, so the greatest record
*was* the final one. That is a measurement, not a guarantee, and it has since
drifted. Re-measured **2026-08-05** across 49 main-thread transcripts on one
machine, non-decreasing holds for **26,998 of 27,106** multi-record ids
(**99.6%**). On the **108** ids where output falls, keeping the last record
would report a *smaller* finished total than a record already seen — 107,810
output tokens understated in aggregate, up to 6,858 on a single id. `max` is
kept precisely because it does not depend on the tendency holding. Both
percentages are dated samples from a corpus that keeps growing, not constants:
the denominator moved from 27,106 to 27,110 within ten minutes of that scan,
because the session doing the measuring was being transcribed into it. Expect a
different denominator, and re-measure before quoting either figure.

Two residual cases are counted and printed rather than hidden. On the earlier
corpus, **109 ids** whose records disagreed on something other than
`output_tokens` keep one whole real record, never a per-field maximum -- that
would report a call which both wrote and did not write cache, a combination
present in no real response. That count ranges over a **different set** from
the 108 above (disagreeing beyond `output_tokens`, versus `output_tokens`
falling) on a different corpus and date; on the 2026-08-05 corpus those two
sets happened to be the same 108 ids, which is an observation about that corpus
rather than a property of either rule. Records with **no** `message.id` each
stay their own call; on this corpus there were none, but dropping them would
delete real spend and grouping them would merge unrelated calls.

CPB no longer converts any of this into money — see "No dollar figures" below.

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
same place as its parse-quality and archived-source warnings, saying the
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
recording. Upgrading a v6 or v7 database does not re-parse anything: both hops
to the current shape are applied in place and keep every row — the run-stamp
table is added, and the retired cost column is dropped. (Dropping a column
needs SQLite 3.35+, which CPB detects at runtime; on an older library it falls
back to a full rebuild, which still refuses outright if any tracked source has
already been reaped.)

There is no automatic refresh yet — refreshing means running `ingest.py` again.
Scheduling that from inside `serve.py` is [issue #20](https://github.com/vlad-ko/claude-piggy-bank/issues/20)'s
second half and ships separately.
### Ingesting a single transcript

`--transcript` ingests **exactly one file** — a main-thread transcript or a
subagent one — and nothing else:

```bash
python3 ingest.py --transcript ~/.claude/projects/<name>/<session-id>.jsonl
```

This is the cheap path for automation that already knows which file changed,
such as a Claude Code hook: a directory scan stats every transcript in the
tree whether or not anything moved. On a synthetic 2,891-file corpus (macOS,
warm cache, checked 2026-08-04) a no-op directory run took 0.19–0.27 s against
0.08–0.11 s for a single file, most of the latter being interpreter start-up.
On a real corpus of the same file count the directory run measured 1.18–2.44 s
idle and 4.09 s with one file changed, because real transcripts are much
larger.

It is incremental and idempotent exactly as the directory scan is, and it
**makes no claim about any source it did not open**. It never archives and
never prunes: one file is evidence about one file, and concluding from it that
the rest of the corpus had vanished would mark a whole history as gone. Run
`python3 ingest.py` periodically for that reconciliation. `--transcript` and
`--projects-dir` are mutually exclusive, a path that is not a readable
transcript is an error rather than a quiet no-op, and the exit status is 0 only
on success — a hook that cannot see a failure is worse than no hook.

### Where the database lives

`ingest.py` resolves the database path highest-first:

1. `--db <path>`
2. the `CPB_DB` environment variable
3. `db/usage.db` beside the script (the default)

`CPB_DB` lets a wrapper or plugin point every invocation at its own data
directory without threading a flag through each one. It applies to both ingest
modes. Setting it to an empty value is refused rather than falling back, so a
misconfigured wrapper cannot quietly write to a database nobody reads.

`serve.py` does not read `CPB_DB`; point it at the same file with
`python3 serve.py --db "$CPB_DB"`.

### One database can hold several projects, and the report says which

Claude Code keys transcripts on the **working directory**, so every repo — and
every git worktree — is its own project. One database can therefore hold
several of them: `CPB_DB` points every invocation at one file, and the plugin
resolves one database per *install*, so a plugin enabled at user scope ingests
every project you open into the same place.

When that happens every headline figure is a sum across those projects, and the
**scope line** above the totals says so rather than letting a cross-project
total read like one repo's. It states two counts, because either alone
misleads:

| count | what it means |
|---|---|
| projects **in this period** | how many projects the figures on screen actually range over |
| projects **in this database** | how many the file holds at all — a project you ingested but did not use in the selected window is a measured **zero**, not an absent project |

A database with **one** project says nothing new: the line is unchanged, and no
dimension is announced that your data does not have. Calls whose transcript
path does not match the layout above are counted and named separately — they
stay in the totals, and they are never folded into a neighbouring project.

Project *names* are not printed on the page. A project directory is your
absolute working directory with the separators folded to `-`, so it carries
your username and your repo names; `/api/summary` lists them under
`scope.projects` when you want them.

This is the honesty floor, not the feature: filtering and grouping by project
is [issue #7](https://github.com/vlad-ko/claude-piggy-bank/issues/7).

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

### The format CPB reads is internal, and Anthropic says so

This is the tool's central dependency and it should be stated plainly rather
than discovered on a release day. Anthropic's own documentation of
`~/.claude/projects/<project>/<session-id>.jsonl` says:

> Each line is a JSON object for a message, tool use, or metadata entry. **The
> entry format is internal to Claude Code and changes between versions, so
> scripts that parse these files directly can break on any release.**

CPB's entire ingest parses those files directly. That is its premise, not an
oversight: the two supported alternatives cannot answer the questions CPB asks.
`/export` produces a rendered transcript for a person to read, with no `usage`
blocks at all, and `claude -p --resume <id> --output-format json` is structured
output for *one run*, not for the historical corpus. So the direct parse stays,
and the response is to make a break **loud and diagnosable** instead of silent.

The shape has already moved within a single corpus: `usage` carries
`output_tokens_details` on some model/version combinations and not others,
`thinking` blocks persist with empty text plus a signature, and one API response
is written as many records sharing one `message.id` (the defect at the top of
this README).

So ingest now **counts the shape it saw**, per source file, in a `source_shape`
table:

- which Claude Code **`version`** wrote the records each figure is derived
  from — a file whose records span two versions says so, and a record that
  carries no version is counted as an *absence*, never defaulted to a string;
- any record **`type`** this tool has never seen, counted under its own name.
  A new type introduced by a Claude Code release shows up as a named count on
  the run that first reads it, instead of as a total that quietly got smaller;
- any **`usage` key** never seen before, and any of the four keys the token
  columns are read from going *missing*. An absent key reads as a real `0`,
  which is right for a token class that did not occur and silently catastrophic
  if a release renames `output_tokens`.

A row in that table is a positive observation, so a source with no rows has not
been censused rather than been found clean — `ingest.py` prints the ratio, and
an upgraded database censuses each source the next time its file changes.

One test in the suite reads a **real** transcript from `~/.claude/projects` at
run time and fails if any of those assumptions has moved. It asserts only over
the census — key names, type names and counts — and never over content, so no
prompt, path or session id can reach a failure message. On a machine with no
corpus (CI included) it skips **loudly**, printing why: it is the only test here
that can see a format change, because every other one runs on fixtures this
repository wrote, which agree with CPB's assumptions by construction.

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

**No dollar figures.** CPB reports measured tokens and never converts them
into money. It used to: list-rate arithmetic over a hand-maintained rate
table, which modelled no subscription accounting, discount or overage, went
stale twice, and on one real session produced ~$57 where the
subscription-accounted spend was ~$21 — over 2.5× out. A precise-looking
number that is wrong by a factor of two is worse than no number, because the
reader has no way to see the error; that is the rule above turned on the
tool's own headline, so the estimate was removed rather than qualified
([#30](https://github.com/vlad-ko/claude-piggy-bank/issues/30)).

What replaced it is the honest version of the same question: panels that
claimed to rank "by spend" rank by **total tokens** and say so in the heading,
with the **model** shown beside each row. Tokens are not tiers — a large Haiku
dispatch can outrank a small Opus one — and the reader weighs that themselves
rather than trusting a derived figure the tool cannot keep current.

## Tests

```bash
python3 -m unittest discover tests -v
```

Fixtures are hand-built and synthetic — no captured session content. They pin
deliberately unequal values per token class so a swapped column mapping cannot
pass, and include a deliberately malformed line asserting that parse failures
are counted rather than swallowed.

The one exception reads your own transcripts and commits nothing: the shape
smoke test described above opens the most recently written files under
`~/.claude/projects` at run time, asserts only over key names and counts, and
skips loudly where there is no corpus.

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
