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
- **No model produces a figure.** Every number is arithmetic, SQL, or JSON
  parsing. No model computes, estimates, rounds or fills in a figure CPB shows,
  anywhere. **Ingesting and reading the report is free and offline** —
  `cpb.py ingest`, `cpb.py serve` and the page itself spend no tokens and make
  no network call.

That last point is deliberate, and so is the scope of it. This is a tool for
understanding what your sessions consume; it would be a poor one if
*measuring* consumed anything. Measuring is free.

**One place a model is involved, and the line is drawn precisely.**
`/claude-piggy-bank:cpb`, the
in-session skill, reads the finished report and summarises it — so it spends
tokens, in a session you are already paying for. Claude Code shows that as a
per-plugin *context cost* in the `/plugin` panel, so you can see it before you
use it. (That is how the panel behaves as reported to this project on
2026-08-05; it is not a claim checked against Claude Code's documentation
here.) The boundary is: **a model may read a finished measurement and explain
it; it may never produce, compute, estimate or fill in a figure.** A model
asked for a number it does not have will give you a fluent one, which is the
same failure as [rendering absence as a
value](#absence-is-never-rendered-as-a-value) with better prose. The skill
summarises the report and always links you to it; see [Use](#use). Everything
that ships the numbers themselves — ingest, the server, the page — has no model
in it at all. This is a rule CPB imposes on itself, not one Claude Code imposes
on plugins.

CPB is **<!--cpb:version-->4.0.0<!--/cpb:version-->** and follows [Semantic
Versioning](https://semver.org/spec/v2.0.0.html). What that promises you is
written down rather than left to be guessed at: the command line, the HTTP API,
and **what each figure measures** are the interface, so a number that changes
what it counts is a breaking change even when its name does not move. The
database's internal schema is deliberately **not** part of the promise, because
upgrading it is the tool's job rather than yours — an older database is
migrated in place, and where that cannot be done without losing a measurement
CPB refuses rather than rebuilding over it. The rule, and the exact condition
that exclusion depends on, is in [`docs/versioning.md`](docs/versioning.md).

**What each release changed for you, and anything you have to do about it, is
in [`docs/releases.md`](docs/releases.md)** — including the one break so far,
in 2.0.0, and its migration. A release with nothing to migrate says so there
too.

The defect that once made its headline numbers wrong is
fixed, and the whole correction is kept on the record below under
[The record](#the-record-the-defect-that-made-the-headline-numbers-wrong) —
a tool about measurement should show its own corrections rather than quietly
restate them.

## Install

CPB is one repository that can be used two ways. **The plugin and the checkout
are the same code** — the hooks run the same `ingest.py` you would run by hand,
nothing is compiled, and there are no dependencies either way.

| | **as a Claude Code plugin** | **as a plain checkout** |
|---|---|---|
| suits | using CPB on your own ongoing work | reading the code before trusting it with your history; analysing one project on demand; developing on CPB |
| install | `/plugin install` from CPB's own marketplace, or a clone into your skills directory | `git clone` |
| ingest | automatic — three hooks ingest each transcript as it is written | manual — you run `cpb.py ingest` when you want it |
| database | `${CLAUDE_PLUGIN_DATA}/usage.db`, one per install, survives updates | `db/usage.db` beside the checkout |
| report | `/claude-piggy-bank:cpb` from inside a session | `python3 cpb.py serve` |

**Prefer the plugin if you intend to keep using CPB.** Subagent transcripts are
reaped, and its `SubagentStop` hook is the difference between capturing that
spend and losing it — see [What the hooks do](#what-the-hooks-do). Prefer the
checkout if you want to look first; you can enable the plugin later over the
same clone.

### As a plugin, from the marketplace

CPB is its own marketplace: the repository is both the catalog and the plugin.
From inside Claude Code:

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
nothing — it only lets you browse what is in it. The second line is the one that
puts CPB on your machine, and skipping it leaves you with a marketplace and no
plugin, which looks like a successful install until the first time you reach for
the report.

The name is doubled because the marketplace and the plugin are the same
repository — `<plugin>@<marketplace>` is the form Claude Code installs by. If
the install summary says `Run /reload-plugins to activate.`, run it.

Updates arrive through `/plugin update`, and your database is not in the plugin
directory, so an update cannot disturb it — see [Where the plugin keeps your
database](#where-the-plugin-keeps-your-database).

### As a plugin, from a clone you read first

The marketplace install copies the code into a cache. If you would rather read
it before it runs on your machine — which, for a tool that reads your
transcripts, is a reasonable thing to want — clone into your personal skills
directory instead. Claude Code loads any folder there that contains a
`.claude-plugin/plugin.json` as a plugin on the next session, in place, with no
install step and no copy into a cache. The reference calls these
*skills-directory plugins*, and they are as documented as the marketplace path:

```bash
git clone https://github.com/vlad-ko/claude-piggy-bank.git \
  ~/.claude/skills/claude-piggy-bank
```

Restart Claude Code and it loads as `claude-piggy-bank@skills-dir`. Update it
with `git pull`; there is nothing to uninstall.

**The command is the same either way.** A skills-directory folder holding a
`.claude-plugin/plugin.json` is a *plugin*, not a loose skill, so the skill it
bundles is namespaced by the plugin exactly as it is after a marketplace
install: `/claude-piggy-bank:cpb`. Clone into a folder of that name, as above,
and the two paths agree. (Documented: [Plugins reference § Skills-directory
plugins](https://code.claude.com/docs/en/plugins-reference#skills-directory-plugins)
and [Skills § How a skill gets its command
name](https://code.claude.com/docs/en/skills#how-a-skill-gets-its-command-name),
both checked 2026-08-07. Not separately measured on a skills-directory install
here — the measurement below is of the marketplace path.)

To load it for one session without installing it anywhere:

```bash
git clone https://github.com/vlad-ko/claude-piggy-bank.git
claude --plugin-dir ./claude-piggy-bank
```

### As a plain checkout

```bash
git clone https://github.com/vlad-ko/claude-piggy-bank.git
cd claude-piggy-bank
python3 cpb.py ingest
python3 cpb.py serve      # then open http://127.0.0.1:8377/
```

Nothing is installed onto your system and nothing is added to your `PATH`:
`cpb.py` is a file in the checkout that you run with `python3`, which is why
every command in this README names it that way.

### What the hooks do

Enabling the plugin activates three triggers. **No edit to any
`.claude/settings.json` is needed.**

| trigger | when it fires | what it ingests |
|---|---|---|
| `SubagentStop` | a subagent finishes | that subagent's own transcript |
| `Stop` | Claude finishes a response | the session transcript, per turn |
| `SessionEnd` | the session ends | the session transcript, best-effort |

Each one spawns `ingest.py --transcript` for **exactly one file** — no directory
scans, no network, no model. `SubagentStop` is the one that earns its place:
subagent transcripts are reaped, and on the reference corpus 211 subagent runs
are already permanently unmeasurable while subagents are ~78% of all API calls.
Ingesting the moment a subagent finishes is the difference between capturing
that spend and losing it.

If an ingest fails, the hook says so in one line in your transcript and appends
it to `cpb-hook.log` beside the database — and then **gets out of the way**. It
never blocks a turn, never stops a subagent, and never reports success for work
it did not do. The reasoning, and the plugin design generally, is in
[`docs/plugin.md`](docs/plugin.md).

### Where the plugin keeps your database

`${CLAUDE_PLUGIN_DATA}/usage.db` — a directory that survives plugin updates,
which is the whole reason it is there rather than beside the plugin's code.
Set `CPB_DB` yourself to override it; the hooks will not touch a value you have
chosen.

That is not only the documented behaviour; it was measured. Installing from the
marketplace, running a session, bumping the version, publishing and updating
(2026-08-07, Claude Code 2.1.223; the two versions below are that throwaway
install's, not a release plan): the plugin directory moved from
`…/plugins/cache/claude-piggy-bank/claude-piggy-bank/1.6.0` to `…/1.7.0`, the
data directory did not move at all, and `usage.db` came through the update
byte-identical. The session recorded before the update was still in it
afterwards, next to the one recorded after.

`/claude-piggy-bank:cpb` opens that database for you. If you run either script
by hand instead,
pass it explicitly — `serve.py` reads only `--db`, not `CPB_DB`, and while
`ingest.py` does read `CPB_DB`, nothing sets it in a session you started
yourself. Without the flag each falls back to its own default beside the code,
so you would fill one database and read another:

```bash
python3 cpb.py serve  --db ~/.claude/plugins/data/<plugin-id>/usage.db
python3 cpb.py ingest --db ~/.claude/plugins/data/<plugin-id>/usage.db
```

### Upgrade

Installed from the marketplace:

```
/plugin update claude-piggy-bank@claude-piggy-bank
```

Cloned into your skills directory:

```bash
git -C ~/.claude/skills/claude-piggy-bank pull
```

Then `/reload-plugins`, or restart — hook changes are not picked up mid-session.
Your database is not in the plugin directory, so an upgrade cannot disturb it.

**An update only reaches you when CPB's version number moves.** Claude Code
keys its plugin cache on the `version` in the plugin manifest, so a fix
published without a bump would leave `/plugin update` reporting you were
already current. CI fails any change to the shipped plugin that leaves that
field alone; the mechanism is in [`docs/versioning.md`](docs/versioning.md).

### Uninstall

Installed from the marketplace:

```bash
claude plugin uninstall claude-piggy-bank@claude-piggy-bank --keep-data
```

**Pass `--keep-data`, or back the database up first.** Without it,
uninstalling from the last scope deletes the plugin's data directory — and past
Claude Code's ~30-day transcript retention that database is the only copy of
your history. See
[Transcripts expire](#transcripts-expire--back-up-the-database).

Cloned into your skills directory, delete the folder; nothing was installed, so
there is no uninstall step, and your database is not in the folder you are
deleting. To stop loading it without deleting:

```bash
claude plugin disable claude-piggy-bank@skills-dir
```

## Use

From inside Claude Code, once the plugin is enabled:

```
/claude-piggy-bank:cpb
```

**The prefix is not optional here, and that is the whole name.** A plugin's
skills are namespaced by the plugin, so the command is
`<plugin-name>:<skill-name>` — the plugin is `claude-piggy-bank` and the skill
is `cpb`. Claude Code will also answer the bare skill name, without the prefix,
*while nothing else you have installed has claimed it* — but that is a condition
of your machine, not a promise CPB can make, so every invocation in this README
is written the long way. (Documented: [Skills § How a skill gets its command
name](https://code.claude.com/docs/en/skills#how-a-skill-gets-its-command-name),
checked 2026-08-07. Measured here 2026-08-07 on Claude Code 2.1.223: after a
marketplace install, `claude plugin details claude-piggy-bank@claude-piggy-bank`
reports `Skills (1)  cpb` under the plugin `claude-piggy-bank`.)

If it is not there at all, check that you ran **both** install lines above —
`/plugin marketplace add` on its own installs nothing.

That starts the report server, summarises what it found, and **always gives you
the URL**. The summary is a way in, not a substitute for the page: two runs of a
model produce two different summaries, and a measurement has to be reproducible
where a conversation does not. The page is the artifact you can cite, screenshot
or come back to — so the skill will never be the only path to a number, and it
will never state a figure the report does not already show.

Everything below works the same from a plain checkout, with or without the
plugin. `cpb.py ingest` and `cpb.py serve` involve no model at all.

### The `cpb` command

`cpb.py` is the entry point, with one subcommand per thing CPB does:

```bash
python3 cpb.py ingest        # read your transcripts into SQLite (idempotent, incremental)
python3 cpb.py serve         # serve the report at http://127.0.0.1:8377/
python3 cpb.py --version     # which build produced a number
python3 cpb.py --help        # the command list
```

Everything after the subcommand belongs to the subcommand, so every flag
documented below works there too, and `python3 cpb.py ingest --help` prints the
same help `ingest.py` does. It **composes** the two scripts rather than
replacing them — `python3 ingest.py` and `python3 serve.py` keep working
unchanged for anyone who has them in a script, and each keeps its own flags,
with one definition and one help text.

`cpb.py --version` reports the version of **CPB itself** — what you name when
you say which build produced a figure. That is a different thing from the
database's schema version, which describes the shape of the file and says
nothing about the code that filled it.

### Ingesting

```bash
python3 cpb.py ingest
```

`ingest` defaults to **this project's own** transcript directory, derived from
the repository root using Claude Code's naming convention (the absolute path
with each separator folded to `-`). To analyse a different project, pass it
explicitly:

```bash
python3 cpb.py ingest --projects-dir ~/.claude/projects/<name>
```

If the derived directory does not exist, CPB refuses and lists the projects
that *do* have transcripts, rather than reporting an empty run.

Two more flags, both about not destroying measurements:

| flag | what it does |
|---|---|
| `--transcript <path>` | ingest **exactly one** file and nothing else — see below |
| `--prune-missing` | **DELETE** the rows for sources no longer on disk. Off by default; see [Transcripts expire](#transcripts-expire--back-up-the-database) |

#### Ingesting a single transcript

`--transcript` ingests **exactly one file** — a main-thread transcript or a
subagent one — and nothing else:

```bash
python3 cpb.py ingest --transcript ~/.claude/projects/<name>/<session-id>.jsonl
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
`python3 cpb.py ingest` periodically for that reconciliation. `--transcript`
and `--projects-dir` are mutually exclusive, a path that is not a readable
transcript is an error rather than a quiet no-op, and the exit status is 0 only
on success — a hook that cannot see a failure is worse than no hook.

### Where the database lives

`ingest` resolves the database path highest-first:

1. `--db <path>`
2. the `CPB_DB` environment variable
3. `db/usage.db` beside the script (the default)

`CPB_DB` lets a wrapper or plugin point every invocation at its own data
directory without threading a flag through each one. It applies to both ingest
modes. Setting it to an empty value is refused rather than falling back, so a
misconfigured wrapper cannot quietly write to a database nobody reads.

**Step 3 is plugin-aware, and `serve` obeys it too**
([#94](https://github.com/vlad-ko/claude-piggy-bank/issues/94)). Beside the
script is right in a checkout and doomed inside an installed *plugin*, where it
is `${CLAUDE_PLUGIN_ROOT}/db/usage.db` — the directory the next plugin update
replaces. So from inside an install the default resolves
`${CLAUDE_PLUGIN_DATA}/usage.db`, the file the hooks write and
`/claude-piggy-bank:cpb` serves,
and says so; where that directory cannot be known it **refuses** and names the
path and the flag. A silent fall-back to a path an update deletes is the one
option ruled out. Steps 1 and 2 are unaffected — a run that names its database
never consults the default — and a plain clone is unaffected entirely.

`serve` does **not** read `CPB_DB`; point it at the same file explicitly:

```bash
python3 cpb.py serve --db "$CPB_DB"
```

## What the report tells you

### How big your calls are, and whether that is a lot

The headline card is **`Median context/call`**. It used to be the mean, and the
change is not cosmetic: measured 2026-08-05 over the reference corpus, the mean
was 237,153 tokens against a median of 155,255 — **1.53x** — with only **28.7%**
of calls above the mean. A figure that 71.3% of calls fall below is not
describing them. The mean is still on the page, but only as **evidence of the
skew** it demonstrates, printed beside the ratio and the share above it.

A context size on its own is a fact with no referent — nothing says whether
266.6k is a lot. Self-comparison cannot supply one, because a percentile of your
own calls compares waste to waste and says nothing if every call is wasteful. So
the referent is external: each call's context is divided by **that model's own
documented context window**, a published hard limit, and the calls are grouped
into four bands.

| band | reading |
|---|---|
| 90% or more of the window | probably wrong |
| 50–90% | likely wasteful |
| 25–50% | *(no verdict)* |
| under 25% | *(no verdict)* |

**Only the top two bands carry a verdict, because only those two were judged.**
A call under half its window was not judged at all, so its label is a range and
nothing more; a word there would invent a verdict nobody decided.

### Two provenances, kept apart on purpose

That paragraph mixes two kinds of claim, and the report never lets them blur:

- **The window is documented.** Per-model, from Anthropic's published model
  overview, carrying the date it was last checked (`WINDOWS_AS_OF`, currently
  2026-08-05). It is per-model rather than one constant because the difference
  is large: Haiku's window is 200K where the current Opus, Sonnet and Fable
  families are 1M. Measured 2026-08-05, Haiku's largest call on the reference
  corpus carries 111,700 tokens — 55.9% of its real window, but 11.2% of a
  wrongly assumed 1M one, a 5x misread that moves the call across two band
  boundaries.
- **The band boundaries are not Anthropic's.** Anthropic publishes the window;
  it publishes no guidance that half a window is wasteful. Where the boundaries
  sit is a **product-owner judgment**, separately dated (`BANDS_AS_OF`), and the
  page says so in those words.

The two travel across the API as two fields with two dates, and are rendered as
two separate statements with the judged one visually marked. Presenting a
judgment in a documented fact's voice would borrow an authority this project has
not earned — which is the whole reason the split exists.

**And the split is per boundary, not per table.** Wherever CPB shows several
judgments together, each one carries its own provenance. One "sources" line at
the foot of a table is not enough, because you will read it as covering the row
you happen to be looking at — so a judged number sitting beside a cited one
would quietly inherit its credibility. The case that settled it is the
recommendation table in
[#78](https://github.com/vlad-ko/claude-piggy-bank/issues/78), which shipped it. It keys advice on ranges, and two of its
boundaries are different kinds of thing: **1.0** cache reads per write is
*documented*, because a single cache read already repays the 5-minute
cache-write markup (the arithmetic and its source are in
[TA-8](docs/claude-api-token-accounting.md#ta-8), checked 2026-08-04), while
**0.25** main-session saturation has **no source at all** and is a dated
judgment. Two numbers that look alike and are not alike, so each says for
itself which it is.

### What the bands refuse to say

- **A model CPB has no window for keeps its context and loses its utilisation.**
  Its size was measured, so it counts in the spread; its window is not something
  this tool knows, so it is counted and **named** as `UNKNOWN, not low` rather
  than banded against a guess. Defaulting to 1M would file every Haiku call four
  bands too low.
- **A call with no context accounting at all is `UNMEASURED, not zero`.** It
  stays out of the median, the mean and the bands. Banded, it would file as the
  most frugal call in the corpus.
- **Calls measuring above 100% of their window read `INCONCLUSIVE`,** and say
  that this build's window table has gone stale — treat the bands as suspect
  rather than the calls as extraordinary. That is the point of a hand-maintained
  *denominator*: a stale window fails loudly and absurdly, where a stale rate
  would have failed silently inside a plausible number.

Banded + unknown + unmeasured is the window's whole call count, and the
denominator of every band share is published beside them.

### How old the data is

`serve` reads whatever the database holds, and `ingest` never runs on its own —
so the report is exactly as current as your last ingest, and a page left open
for a week would otherwise look identical to one opened a second ago.

Directly above the totals the report states **two facts, never merged into one
"data age"**:

| line | what it means |
|---|---|
| **Last ingest** | when `ingest` last completed — when this tool last *looked* at the transcripts |
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
full parse — so anyone re-running ingest on any reasonable cadence never sees
it. It marks neglect, not latency. The warning applies to the *ingest run*
only; an idle machine that produced no calls for hours is not stale.

A database written before CPB recorded ingest times (schema v6 and earlier)
reads **"Last ingest: not recorded"**. That is an *unknown* age, not an age of
zero and not a permanent staleness warning — run ingest once and it starts
recording. Upgrading an older database does not re-parse anything: every hop
from v6 to the current shape is applied in place, and **no measurement is lost
on any of them** — the run-stamp table is added, the retired cost column is
dropped, the format-census table is added, duplicate dispatch rows are resolved
to one row per dispatch (a dispatch recorded by two transcripts is one
dispatch, and the discarded rows are counted out loud), and the cache-miss
diagnostic columns are added empty, because no row written before CPB read them
ever measured one. (Dropping a column needs SQLite 3.35+, which CPB detects at
runtime; on an older library it falls back to a full rebuild, which still
refuses outright if any tracked source has already been reaped.)

There is no automatic refresh yet — refreshing means running ingest again.
Scheduling that from inside `serve.py` is [issue #20](https://github.com/vlad-ko/claude-piggy-bank/issues/20)'s
second half and ships separately.

### Which projects a total covers

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
path does not match the layout below are counted and named separately — they
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

The cache-write class is **stored three ways**, because one number cannot answer
"did this write pay for itself?". The flat total is kept exactly as the API
reports it, and beside it the per-TTL split from `usage.cache_creation` — a
5-minute write costs 1.25x base input tokens and is repaid by its *first* read,
a 1-hour write costs 2x and needs its *second*
([TA-8](docs/claude-api-token-accounting.md#ta-8)). Both columns are **nullable
and mean unmeasured**: a call recorded before CPB read that field, or one whose
record carried only one of the two TTLs, has no split, and the report's
repayment figure ranges over the calls that do — taking its reads from those
same calls, so the two sides of the ratio cover one set. Until you re-ingest,
that figure reads *not measured* rather than reading zero.

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
is written as many records sharing one `message.id` (the defect recorded at the
bottom of this README).

So ingest **counts the shape it saw**, per source file, in a `source_shape`
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
been censused rather than been found clean — ingest prints the ratio, and an
upgraded database censuses each source the next time its file changes.

One test in the suite reads a **real** transcript from `~/.claude/projects` at
run time and fails if any of those assumptions has moved. It asserts only over
the census — key names, type names and counts — and never over content, so no
prompt, path or session id can reach a failure message. On a machine with no
corpus (CI included) it skips **loudly**, printing why: it is the only test here
that can see a format change, because every other one runs on fixtures this
repository wrote, which agree with CPB's assumptions by construction.

## What not to trust, and why

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

The practical consequence: past the retention window, **your database is the
only copy of your history**. It is not derived data you can drop and regenerate.
Back it up, and raise `cleanupPeriodDays` in `~/.claude/settings.json` if you
want a longer window — though the tool is designed to be correct at the default,
not only when configured.

Any window containing archived sources says so in the report banner: the totals
are complete, but they are no longer reproducible by re-ingesting.

### Absence is never rendered as a value

A read that cannot produce a trustworthy answer refuses — null, inconclusive, or
a loud operator-visible message — rather than returning a plausible number. A
parse failure is counted and surfaced, never silently coerced to `0`. A session
with no subagent transcripts on disk reports "not measured", a different fact
from a measured zero, and the two stay distinguishable everywhere.

The sharpest case: Claude Code persists thinking blocks with **empty text**, so
their size is recorded as *unknown* rather than `0`. Recording `0` would make a
composition table state that thinking costs nothing — false, and invisible,
because it looks like a measurement.

This is the rule the whole report is built on, and the bands above are it
applied to the newest surface: unknown model, unmeasured call and
over-100% window each get their own honest answer instead of a number.

### There are no dollar figures

CPB reports measured tokens and never converts them into money. It used to:
list-rate arithmetic over a hand-maintained rate table, which modelled no
subscription accounting, discount or overage, went stale twice, and on one real
session produced ~$57 where the subscription-accounted spend was ~$21 — over
2.5x out. A precise-looking number that is wrong by a factor of two is worse
than no number, because the reader has no way to see the error; that is the rule
above turned on the tool's own headline, so the estimate was removed rather than
qualified ([#30](https://github.com/vlad-ko/claude-piggy-bank/issues/30)).

What replaced it is the honest version of the same question: panels that
claimed to rank "by spend" rank by **total tokens** and say so in the heading,
with the **model** shown beside each row. Tokens are not tiers — a large Haiku
dispatch can outrank a small Opus one — and the reader weighs that themselves
rather than trusting a derived figure the tool cannot keep current.

## The record: the defect that made the headline numbers wrong

There is no changelog; this is kept in the README because a tool about
measurement should show its own corrections, and because a number's history is
more useful beside the number than in a file of one-line entries. A correction
like this one is **not** a breaking change — the figure always claimed to count
API calls and had been counting transcript records — which is a distinction
[`docs/versioning.md`](docs/versioning.md) draws deliberately.

This section is corrections only. A **break** — behaviour a script depended on
that no longer holds — is announced per release in
[`docs/releases.md`](docs/releases.md) instead, because someone whose script
stopped working is not reading a record of corrected figures.

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

Publishing a tool whose README documented its own broken numbers was a choice.
The alternative was to fix it privately first, and the point of this project is
that measurement should be inspectable, including when it is wrong.

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

Running one class or one test is not uniform across the suite — see
[CONTRIBUTING.md](CONTRIBUTING.md#tests), which records which form works for
which module and why.

## Documentation

Longer-form reference lives in [`docs/`](docs/), indexed by
[`docs/README.md`](docs/README.md):

- [Claude API token accounting](docs/claude-api-token-accounting.md) — the API
  accounting facts the analysis rests on, each with its source URL and the date
  it was checked. Thinking is billed as output and is invisible in transcript
  content; whether it is re-billed as input on later turns depends on the
  model; cache writes carry a markup and the cacheable minimum is a per-model
  lookup, not a constant; and the context window every utilisation figure
  divides by is per model too — 1M on the current Opus, Sonnet and Fable
  families against 200K on Haiku 4.5.
- [Versioning](docs/versioning.md) — what the version number promises: the
  three surfaces SemVer governs here, why the database schema is excluded and
  the condition that exclusion rests on, which part to bump, and where a break
  is announced.
- [Release record](docs/releases.md) — what each release changed for a user and
  what to do about it. The one break so far is 2.0.0's:
  `ingest.py --prune-missing` used to delete and exit 0 in a pipe and now
  refuses and exits 1, with `--yes` as the migration. Where a release carries
  nothing to migrate, it says so.
- [The Claude Code plugin](docs/plugin.md) — why the packaging looks the way it
  does: which three hooks fire and why `SubagentStop` is load-bearing, why every
  timeout is explicit, where the database lives and when the hook refuses to
  decide, and how failing loudly is reconciled with never interrupting a
  session.

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
