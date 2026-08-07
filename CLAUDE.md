# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Claude Piggy Bank (CPB) ingests the JSONL session transcripts Claude Code writes
to `~/.claude/projects/` into SQLite, then serves a single-page report over them
— answering where a project's context and token spend actually went. It is an
independent, unofficial tool; it only reads files already on the user's machine.

The project is *about* measurement, so the code is unusually opinionated about
what counts as a trustworthy number. Most of the rules below exist because a
number was once wrong in a way that looked right.

## Commands

```bash
python3 cpb.py ingest                      # ingest this project's own transcripts (idempotent, incremental)
python3 cpb.py serve                       # report at http://127.0.0.1:8377/
python3 cpb.py --version                   # `cpb <VERSION>` -- the build that produced a number
python3 cpb.py --help                      # the command list, generated from COMMANDS

python3 ingest.py --projects-dir ~/.claude/projects/<name>   # ingest a different project
python3 ingest.py --all-projects           # backfill EVERY project on the machine into one DB (#97)
python3 ingest.py --transcript ~/.claude/projects/<name>/<session-id>.jsonl  # exactly one file
python3 ingest.py --prune-missing          # DELETE rows for sources gone from disk (see Durability)
python3 serve.py --port 9000 --db path.db
```

`cpb.py` (#21) is the entry point; it **composes** the two scripts rather than
replacing them. Each keeps its own `argparse` parser, so every flag has one
definition and one help text, everything after the subcommand belongs to the
subcommand (`cpb.py ingest --help`), and `python3 ingest.py` keeps working for
anyone who has it in a script. `_exit_status()` reproduces CPython's own
`SystemExit` handling, because a wrapper that returned 0 over a refusal would
report a run that measured nothing as a run that measured zero. `VERSION` lives
in `cpb.py`; `.claude-plugin/plugin.json` repeats it as a literal because the
plugin loader reads that JSON without running Python, and `tests/test_cpb.py`
pins the two equal. CPB is **<!--cpb:version-->4.0.0<!--/cpb:version-->** under SemVer (`cpb.VERSION` is the
authority; this line lagged a release once already and may again), and what a
bump *means* is `docs/versioning.md` — see Commits and PRs below.

Both scripts default `--db` to `db/usage.db` (gitignored); `ingest.py` also
reads `CPB_DB`, `serve.py` deliberately does not (pass `--db "$CPB_DB"`).
`serve.py` exits rather than starting if the DB is absent. **That default is
plugin-aware and both scripts share one function for it** (`ingest.
default_database()`, #94): beside the script is correct in a checkout and is
`${CLAUDE_PLUGIN_ROOT}/db/usage.db` inside an install, which the next update
deletes — so there it resolves `${CLAUDE_PLUGIN_DATA}/usage.db` where Claude
Code named that directory and **refuses** where it did not. Being an install is
established from two independent observations (the root variable *containing
this script*, or the script sitting in Claude Code's plugin store), each dated
and measured in `docs/plugin.md`, because a false positive would refuse the
plain-clone path that `db/usage.db` is right for. `--db` and `CPB_DB` never
reach it: the default is computed only when neither named a database.

```bash
python3 -m unittest discover tests -v                        # full suite
python3 -m unittest tests.test_ingest.StreamedRecordDedupeTest -v         # one class
python3 -m unittest discover -s tests -p test_serve.py -k ScopeLabellingTest -v   # one class in test_serve
python3 -m unittest discover -s tests -p test_serve.py -k test_<name> -v          # one test in test_serve
```

Most modules take the dotted `tests.<module>.<Class>` form. **`test_serve` does
not**: `tests/test_serve.py:38` does `from test_ingest import build_corpus`, a
top-level import that resolves only with `tests/` itself on `sys.path` — which
`discover -s tests` arranges and `-m unittest tests.test_serve...` does not, so
the dotted form dies with `ModuleNotFoundError: No module named 'test_ingest'`
before running anything. Use `discover -p test_serve.py -k` for that module.
Tests insert the repo root on `sys.path` themselves, so run them from anywhere.

## Four load-bearing constraints

These are enforced or defended in review; a change that breaks one gets pushback
even if the code is good.

1. **Standard library only**, Python 3.10+. No pip, no Node, no build step. The
   `stdlib-only` CI job ASTs every shipped module and fails on any import that
   is neither `sys.stdlib_module_names` nor local. A second job tests **intent**
   in two tiers, **narrowed by #50** from the older "no manifest may exist":
   `requirements*.txt`, `Pipfile`, the lockfiles, `package.json`, `setup.py` and
   `setup.cfg` are rejected **on sight** — they exist only to drive an installer,
   so declaring nothing in one is a file with no purpose. `pyproject.toml` is
   **inspected** with `tomllib` and passes iff it declares no runtime, optional
   or grouped dependency; `build-system.requires` is not counted, and
   `dynamic = ["dependencies"]` fails because a check that cannot see its answer
   must refuse rather than report a clean one. Adding such a file is still a
   reviewed decision — it is no longer an automatic build break.
2. **Nothing leaves the machine.** No network calls, no telemetry, no CDN. The
   page renders the user's own prompts, paths and source code, so a third-party
   script in it is a privacy and supply-chain surface. Vendor assets into
   `vendor/`, as **both** browser libraries already are — Chart.js for the plot
   and Alpine.js for the bindings — served through a path-escape-checked
   handler. `vendor/README.md` records each one's version, origin and SHA-256,
   and the suite re-checks those digests and that neither bundle can reach the
   network.
3. **No model produces a figure.** Three claims with three different scopes,
   written out separately because the old one-line form ("no model in the
   loop; running CPB must stay free") collapsed them into one and so forbade,
   read literally, something now allowed.
   - **Every figure is SQL, arithmetic or JSON parsing** — absolute,
     everywhere, unchanged. Nothing a model says becomes a number CPB shows.
   - **The report runs free and offline** — absolute, unchanged, and it is the
     whole of `ingest.py`, `serve.py` and `index.html`. Ingesting and reading
     the report spends no tokens and makes no network call, whatever else
     changes.
   - **Guidance, in session, may use the model already present** — new, and
     bounded to `commands/cpb.md`. `/cpb` runs inside a session the user is
     already paying for, and *it does not produce measurement, it produces
     guidance*.

   The boundary, stated positively rather than as an exception, because an
   exception invites the next reader to look for more of them: **a model may
   read a finished measurement and explain it; it may never produce, compute,
   estimate or fill in a figure.** A model asked for a number it was not given
   will supply a fluent, plausible one — that is *absence rendered as a value*
   (below) arriving by a route no `Optional[int]` can catch, and it is why the
   line sits exactly here rather than anywhere looser. So `/cpb` may summarise
   what the report already computed; it may not compute.

   **This is CPB's own constraint, not an Anthropic requirement.** Claude Code
   command files *are* prompts — `commands/cpb.md` is one, and `docs/plugin.md`
   lists it as the `/cpb` command — and the plugin reference imposes no limit
   on what a command may ask the model to do (external:
   `code.claude.com/docs/en/plugins-reference`, the revision
   `tests/test_plugin_manifest.py` cites as checked 2026-08-05; that reference
   is silent on the question rather than permissive about it, and was not
   re-fetched when this was written). CPB declines latitude it has, for
   figures, and takes it for guidance.

   **The cost claim is scoped, not dropped.** "Running CPB costs nothing" was
   true of the whole tool and no longer is. The report stays free; `/cpb`
   spends tokens in a session already being paid for, which Claude Code
   surfaces as a per-plugin *context cost* in the `/plugin` panel
   (product-owner report, 2026-08-05; not verified against the Claude Code
   docs here). Two claims, two scopes — do not restore the sentence that made
   them one.
4. **Do not overwhelm the UI.** A new detector earns space by displacing or
   annotating something, not by appending another table.

## Absence is never rendered as a value

The rule most likely to come up in review. A read that cannot produce a
trustworthy answer must refuse — `None`, INCONCLUSIVE, or a loud
operator-visible message — rather than return a plausible number.

- Never default a missing measurement to `0`. A real `0` is a healthy sample and
  must stay distinguishable from *no sample*; count samples separately from the
  aggregate.
- Never let an exception handler return a benign default. Parse failures are
  counted (`ingest_state.unparsed_records`) and surfaced, never swallowed.
- An aggregate must name the set it ranges over. Main-thread-only figures that
  read like session totals are wrong numbers even when every input was right —
  hence `SCOPE_*` labelling throughout `serve.py`.
- A ranking must name the key it orders by, and the name must be the key. "Top
  subagent dispatches (by spend)" ordered by `cache_read DESC` for its whole
  life; the heading and the query were free to disagree because nothing tied
  them together. `serve.RANKED_BY` is that tie — one phrase, used by the
  `ORDER BY`, the payload field and the panel heading, asserted equal in tests.

Worked examples in the code: `ContentBlock.chars` is `Optional[int]` because
Claude Code persists thinking blocks with empty text — recording `0` would make
the composition table state that thinking is free. `subagent_runs.status =
'unavailable'` means a dispatch is proven but its transcript is gone. Whether
that is unmeasured spend depends on *when* it was reaped: a run reaped before
CPB ever read it has no rows and reports `unavailable`, every measured field
null; a run reaped *after* ingest keeps its `api_calls` rows, and those are the
only surviving copy — it reports `archived` and shows them. The panel derives
that distinction from whether call rows exist, never from a stored flag.
`_last_ingest_run()` returns `None` for more than one reason — a database
predating run stamping, one upgraded but not yet re-ingested, or a run that
raised before stamping — so the payload carries `stale_unknown_reason` and the
page never asserts which. `ingest.stale` is tri-state, so "never recorded" and
a negative age both read as an unknown age rather than as a verdict.

**What `ingest_runs` means, decided at #105.** `finished_at` is *when an ingest
run last completed, of either mode* — not "when the corpus was last rescanned",
which is what it silently meant while directory mode was its only writer.
`ingest.py --transcript` is `ingest.py` completing, over one file, and it is the
**only** mode the plugin's hooks ever use; withholding the stamp did not say
"one file", it said "no run has ever completed here", so every plugin install
read the loudest thing the report can say — *your ingest may have failed* — on a
working install, with advice that could never clear it. Widening the field to
what it always published is a **correction** under `docs/versioning.md`, not a
redefinition. The narrower fact moved into its own column, `corpus_finished_at`,
surfaced as `last_full_scan_at` with a three-valued `full_scan_unknown_reason`
beside it (no run table / no recorded scope / no full scan yet) — the same shape
as `stale_unknown_reason` and for the same reason: three absences with three
remedies must not collapse into one. A hook-maintained database is current
**and** has never been swept; those are two facts, and one field could only
express the first by denying both. The three freshness surfaces — the staleness
verdict, the report banner and the data-age line — still read `last_run_at` and
nothing else, so widening it moved all three at once. **A run that raises still
stamps nothing**, and must: both modes stamp last and only on success, so a
failed ingest stays indistinguishable from no ingest, because it is. Note what
the v12→v13 migration declines to do: every stamp a pre-v13 build wrote *was* a
corpus run, so back-filling `corpus_finished_at = finished_at` would be true —
and it is refused anyway, because the column exists to separate a scan that
reported itself from one nobody has evidence of, and a migration writing into it
makes an inference indistinguishable from an observation.

**No dollar figures anywhere (#30).** Tokens are measured; dollars were derived
from a hand-maintained list-rate table that went stale twice and diverged from
real spend by >2.5x, so the estimate was removed — schema column, module and
all — rather than qualified. A precise-looking figure wrong by a factor of two
is worse than none, because the reader cannot see the error. Do not reintroduce
a cost estimate, a "relative cost index" or any money-shaped field; rank by
total tokens and show the model so the reader weighs the tiers themselves.

## Architecture

Four modules, one direction of flow: `ingest.py` (transcripts → SQLite) →
`db/usage.db` → `serve.py` (SQLite → JSON) → `index.html` (JSON → charts).
`cpb.py` is the entry point above ingest and serve, importing whichever
subcommand is asked for and nothing else. `context_window.py` is a leaf that
only `serve.py` reads.

**Packaging.** The repository *is* the Claude Code plugin *and* its marketplace
— same code path, no build step. `.claude-plugin/plugin.json` is the manifest
and `.claude-plugin/marketplace.json` the catalog, whose single entry points
back at the repository with `"source": "./"`; `hooks/hooks.json` declares three
triggers (`SubagentStop`, `Stop`, `SessionEnd`) that each run
`hooks/cpb_ingest_hook.py`, which spawns `ingest.py --transcript` for **exactly
one file**; `skills/cpb/SKILL.md` is the `/cpb` skill (it was `commands/cpb.md`
until #73 — `commands/` is the legacy flat-file layout and the reference says
to use `skills/` for new plugins). Its decided behaviour
([#79](https://github.com/vlad-ko/claude-piggy-bank/issues/79)): **summarise,
then always link to the report — never become the only path to a number.** The
page is the reproducible artifact; two model runs produce two different
summaries, and a measurement has to be reproducible where a conversation does
not. Every database-touching invocation in that file names its database with
`--db`, both `serve.py` **and** `ingest.py`
([#94](https://github.com/vlad-ko/claude-piggy-bank/issues/94)): only the serve
half was pinned by a test, and the ingest half quietly wrote the user's only
copy of their history into `${CLAUDE_PLUGIN_ROOT}/db/` while the report kept
serving an empty file. The **improvised** invocation — somebody typing `python3
ingest.py` with no flag — was the surviving half of that issue and is now the
plugin-aware default described above, applied to `serve.py` by the same
function rather than to `ingest.py` alone. `${CLAUDE_PLUGIN_DATA}`
holds the database because `${CLAUDE_PLUGIN_ROOT}` is replaced on every update
and the DB is not regenerable — *measured* on 2026-08-07 across a real install
and update, not inferred. **`version` in `plugin.json` is the cache key Claude
Code decides updates by**, so a merged change with an unbumped version reaches
nobody while looking shipped; `.github/scripts/check_plugin_version_bump.py`
fails any change to a shipped path that leaves it alone. The reasoning, with
its sources, is in `docs/plugin.md` — read it before changing any of those
files.

**Sources.** Two globs are data — `<project>/<session>.jsonl` (main thread) and
`<project>/<session>/subagents/agent-<id>.jsonl` (subagents). A third path, the
harness task directory under the OS temp dir, is an **index, not a source**:
most entries are symlinks into the canonical subagent store, so ingesting them
as data would double every subagent figure. It is read only for dispatch
attribution and to detect runs whose transcript has been reaped.
`default_projects_dir()` derives the project directory from the repo root using
Claude Code's slug convention (path separators folded to `-`), and refuses with
a list of real candidates rather than reporting an empty run.

**One API call = one `message.id`.** `_dedupe_calls()` is the correctness core.
Claude Code writes one record per streamed content block, each repeating the
same `message.usage`, so counting records inflated aggregates 1.9–2.4x — and by
*different* factors per scope, distorting comparisons in shape. The record with
the **greatest `output_tokens`** survives, ties going to the last (what
`reversed()` in the `max()` is for). **Not "last wins"** — that shorthand was
accurate only while `output_tokens` never fell across an id's records, which is
a dated tendency rather than an invariant: 26,998 of 27,106 multi-record ids,
99.6%, over 49 local main-thread transcripts on 2026-08-05, against 4,928 of
4,928 when the rule was first derived on a smaller, older corpus. Both are
samples from a corpus that grows between scans, not constants. `max` is right
*because* it does not depend on that tendency: on the 108 ids where the
sequence falls, a literal last-record rule picks a *smaller* `output_tokens`
than a record already seen — 107,810 tokens understated in aggregate, 6,858 on
one id — the same inflation defect running in reverse.
`NonMonotonicOutputDedupeTest` pins both halves, so a "simplification" to
`group[-1]` fails. Where records of one id disagree beyond `output_tokens`, one
*whole* record survives — never a per-field maximum, which would describe an API
response that never happened — and the ambiguity is counted (a *different* set
from the 108 above, and re-measure before quoting any of these). Records with no
`message.id` each stay their own call; `NULL` is not a shared key.

**Schema** (`SCHEMA_VERSION = 13`, `PRAGMA user_version`): `sessions`, `turns`,
`api_calls`, `agent_dispatches`, `subagent_runs`, `task_index_sessions`,
`ingest_state`, `ingest_runs`, `source_shape`. Ingest is incremental per file,
keyed on size+mtime in `ingest_state`; re-ingesting a changed file deletes its
rows by `source_path` first, so every hot path has a `source_path` index.
`task_index_sessions` distinguishes "scanned, dispatched nothing" (a real zero)
from "never scanned".

**`source_shape` censuses the format** (#15), per source file, because the
transcript format is internal to Claude Code and documented to change between
releases. It counts the Claude Code `version` each figure's records came from,
any record `type` never seen before, any new `usage` key, and any of the four
token keys going *missing* — an absent key reads as a real `0`, which is right
for a token class that did not occur and silently catastrophic if a release
renames `output_tokens`. A row there is a positive observation, so a source
with **no** rows has not been censused rather than been found clean.

**The `context` block in `/api/summary`** (#31) is where two rules meet.
`context_window.py` holds a per-model window table dated `WINDOWS_AS_OF` —
readmitted after #30 deleted the rate table because its output is a
**denominator**, not an invented figure, and because a stale window fails
*loudly* (utilisation above 100%, counted in `over_window_calls`) where a stale
rate failed silently. The median displaced the mean as the headline card, the
mean staying only as evidence of the skew (`mean_over_median`,
`share_above_mean`). **Two provenances that must never merge:** the window is
documented and cited (`window_provenance`, `WINDOWS_AS_OF`); where the band
boundaries sit is a dated product-owner judgment Anthropic publishes nothing
about (`band_provenance`, `BANDS_AS_OF`). They cross the API as two fields with
two dates so the page can never present the judged one in the documented one's
voice. An unknown model keeps its context and loses its utilisation; a
zero-context row is `unmeasured_calls`, never banded as the most frugal call.

**Provenance is per boundary, not per table.** The rule above generalises:
where several judgments are presented together, **each carries its own
provenance**, and a judged value must never inherit a cited value's credibility
by sitting next to it. One provenance line for a whole table is not enough,
because the reader attaches it to whichever row they are reading. The case that
forced the generalisation is the recommendation table under
[#78](https://github.com/vlad-ko/claude-piggy-bank/issues/78), which keys
advice on ranges: its `1.0` boundary on cache reads-per-write is **documented**
— TA-8 in `docs/claude-api-token-accounting.md` (Documented, checked
2026-08-04) works the arithmetic out, `1.25 + 0.1` against `2 x 1.0` at two
sends, 1.35 against 2.00, so the *first* read already repays the 5-minute write
markup and one read per write is where it is repaid — while its `0.25` boundary
on main-session saturation is **product-owner judgment with no source at all**.
A single table-level provenance line would let the second borrow the first's
authority: `band_provenance`'s failure mode reappearing one level down. Note
what the citation does and does not cover: TA-8 documents `1.0` for the
**5-minute** write only. The 1-hour write is 2x and needs **two** reads (2.10
against 2.00 at n=2, still a loss), and TA-8 warns in as many words against
compressing either into "repays on the second hit" — so a `1.0` boundary that
claimed to settle 1-hour cache writes on that citation would be a judged number
wearing a cited number's clothes, which is the same defect this rule exists to
stop.

**How that was settled, because the paragraph above once ended by hedging it.**
`recommendations.py` shipped with #78 and was wired into `/api/summary` and the
report by #85. There are now **two** cache-repayment metrics rather than one
boundary doing two jobs, and each carries the citation it has actually earned.
`cache_reads_per_write` is the flat ratio over every call, and its `1.0` is
cited for exactly what it claims — below one read per write **no** cache write
is repaid, whichever TTL it asked for — with the band between one read and two
left explicitly unresolved, because one flat total cannot name the TTL.
`cache_write_repayment_at_own_ttl` (#84) is the second: CPB reads the per-TTL
split from `usage.cache_creation` per call, so every measured write token is
weighted at its **own** break-even — one read token per 5-minute write token,
two per 1-hour — and `1.0` there is cited *because* the arithmetic is per TTL
rather than in spite of it. Neither break-even moved; which one applies stopped
being unknown wherever the split was read. The second metric ranges over the
calls whose split was measured and takes its reads from **those same calls**: a
row written before #84 is unmeasured on both sides, never a 5-minute write and
never a zero, so on a database not re-ingested since the split was first read
the metric is `None` and is named in `unmeasured` rather than banded. Both
metrics exist permanently — a transcript past `cleanupPeriodDays` can never be
re-ingested to acquire a split, so for most history the flat ratio is the only
one of the two that can be computed at all.

**Serve** is `http.server` + `sqlite3`, bound to loopback, with a Host-header
check against DNS rebinding. Routes: `/api/summary`, `/api/timeseries`,
`/api/sessions`, `/api/session?id=`, `/api/outliers`, `/api/agents`, plus `/`
and `/vendor/*`. Every handler path returns a status — a missing required
param is a 400, an unknown id a 404, and an uncaught exception a 500 — because
a dropped connection makes the page silently stop updating. `index.html` is
plain JS with no build step and fetches those endpoints directly.

## Durability — the DB is not regenerable

Claude Code deletes transcripts after `cleanupPeriodDays` (default 30). Past
that window `db/usage.db` is the only copy of the history, not derived data.
Consequences encoded in the code, which must be preserved:

- A source that vanishes is **archived, not deleted**: excluded from
  "currently on disk" coverage, kept in every historical total. Deletion is
  opt-in via `--prune-missing`.
- **Opt-in was not enough, so the destructive command shows its work and asks**
  (#92). Opt-in protects against the default; it does not protect against a
  typo or a half-remembered flag, and this is the one operation that destroys
  measurements nothing can regenerate. `--prune-missing` now prints the census
  — sources, rows per table, and the **date range of the calls involved**,
  because a count with no range does not say *which* history goes — and then
  requires the word `delete`. `--yes` is the scripted answer, `--dry-run`
  prints the identical census and prunes nothing, and a **non-interactive
  stdin refuses**: a stream that cannot be asked has not said yes, so a hook or
  a pipe exits non-zero having deleted nothing rather than reporting 0 over a
  deletion that did not happen. The structural half is the part to preserve:
  `plan_prune()` censuses a path list, `execute_prune()` deletes **that
  `PrunePlan`'s own list**, and both iterate one `PRUNE_TABLES` with one key
  column per table — so the summary somebody approves and the rows that go
  cannot range over different sets. That divergence, not the missing prompt, is
  this project's recurring defect (`RANKED_BY`, one layer up). In the library,
  `prune_missing=True` says a prune was *requested*; `approve_prune` is
  consent, and **no approver means none was obtained**, never that one was
  given.
- `_prepare_schema()` **refuses to run** (`SystemExit`) if a schema rebuild
  would drop rows whose source file no longer exists. It asks the filesystem,
  not `archived_at`, because that column postdates the older DBs the guard has
  to protect.
- Which is why a schema change that loses no measurement must not go through
  that rebuild: on any corpus past the retention window the guard would refuse a
  change that risks nothing, and an untrue refusal is the same class of defect
  as an untrue number. `IN_PLACE_UPGRADE_FROM` (currently
  `{6, 7, 8, 9, 10, 11, 12}`) lists the versions whose delta to the **current**
  shape can be applied without losing one. Seven hops exist, and a v6 database
  makes all seven at once: v6→v7 adds `ingest_runs`; v7→v8 drops
  `api_calls.cost_usd` (#30) via `ALTER TABLE ... DROP COLUMN`, gated on a
  **runtime** check of `sqlite3.sqlite_version` ≥ 3.35; v8→v9 adds
  `source_shape` (#15); v9→v10 adds the UNIQUE `idx_agent_dispatches_task_id`,
  which `_dedupe_dispatch_task_ids()` has to precede because the index cannot be
  built over a table already holding duplicates (#36); v10→v11 adds the three
  nullable cache-miss diagnostic columns to `api_calls` (#5); v11→v12 adds
  the two nullable per-TTL cache-write columns to the same table (#84); and
  v12→v13 adds the nullable `ingest_runs.corpus_finished_at` (#105), where the
  interesting half is the back-fill **not** taken — see the `ingest_runs`
  paragraph above. It is
  re-decided at every bump against the current shape, never extended by habit,
  and the sub-3.35 fallback goes through the rebuild path **including** the
  guard — never around it.
- **The bar moved at the v10 bump**, which is what re-deciding rather than
  extending is for. It read "the delta preserves every **row**"; v9→v10 deletes
  rows, because a duplicate `task_id` is one dispatch recorded by a second
  transcript, not a second dispatch. What that hop preserves is every
  *dispatch* — one **whole** row of each pair survives, by the same rule
  `store_source()` applies, and what it discards is counted and printed. The bar
  now is the **measurement**: a delta that loses one belongs nowhere near this
  set, whatever it does to row counts.
- Two bounded permissions keep "in place" from meaning "whatever is convenient",
  and they are the same statement about different shapes. A table may be created
  from nothing only if it is in `IN_PLACE_CREATABLE_TABLES` — only, that is, if
  an **empty** one is a true statement (#35); `CREATE TABLE IF NOT EXISTS` would
  otherwise silently grant that to every table, including `turns`, where empty
  is a lie. A column may be added to a table that already holds rows only if it
  is in `IN_PLACE_ADDABLE_COLUMNS` — only if **NULL is a true statement about
  the rows already there**. NULL in `cache_miss_outcome` (#5) means "written
  before CPB read `message.diagnostics`", i.e. unmeasured, which is true of
  every row a pre-v11 build wrote and is a *different* statement from `absent`
  or `no-divergence`; back-filling either would manufacture an observation
  nobody made, over a whole corpus at once. NULL in `cache_write_5m` (#84) says
  the same thing about the per-TTL cache-write split, and the tempting back-fill
  there is sharper: `cache_write_5m = cache_write` looks like a safe default and
  would state that no session in the corpus ever asked for the 1-hour TTL —
  inventing the answer to the exact question the columns were added to ask. A
  NOT NULL column cannot be added this way at all.
- Windows containing archived sources are flagged in the report banner: totals
  are complete but no longer reproducible by re-ingesting.

## Testing conventions

- Assert the **state change**, not that something succeeded.
- A fixture must not make the defect undetectable: when two quantities could
  diverge, pin them deliberately unequal (the fixtures give every token class a
  different value so a swapped column mapping cannot pass).
- Verify a test has teeth by mutation — change one implementation line, confirm
  red, change it back. A test that survives a deliberate mutation is not a test.
- **Assert the rule over the whole pair, and over the seam.** Three defects in
  two weeks were one shape: a rule written while looking at one mode, with
  nobody asking what its sibling does. #94 passed `--db` on every `serve`
  invocation in the skill and not on its `ingest` twin, writing the user's only
  copy of their history where the next plugin update deletes it. #105 stamped
  `ingest_runs` from directory mode and not from single-file mode — the only
  mode the plugin's hooks use. #108 refused a wrong scope in one direction and
  reported a confident zero in the other. Each was covered by a test ranging
  over the member somebody had in mind. So the unit of assertion is the axis,
  not the instance: `EveryIngestModeStampsARunTest` derives its haystack from
  the module (every public function taking a `db_path` is a mode that must
  stamp or is declared not to be one), and
  `BothScopesRefuseTheOthersShapeTest` runs each scope against the other's
  shape. And #105 could ship green because both paths were tested and the
  **seam** between them was not — `HookToReportSeamTest` fires the real hook and
  asks the report what it then says, which is the only place that defect was
  visible.
- Fixtures are synthetic and hand-built. **Never commit captured session
  content** — real transcripts contain prompts, file paths and source code.
- CI runs the suite on 3.10–3.13 with `fail-fast: false`; branch protection
  requires only the `suite` fan-in job, which fails unless every dependency
  reports `success`.

## Commits and PRs

- **No tool attribution anywhere.** No `Co-Authored-By` trailer on any commit,
  and no "generated with" footer on any PR body, whatever tooling offers to add
  one. If your harness appends either by default, strip it before committing or
  opening the PR.
- Branch per concern, conventional-commit subject (`fix:`, `ci:`, `docs:`), and
  a body that explains *why* rather than restating the diff. Where a change
  turns on a measurement, put the numbers in the body.
- Branch protection requires a PR and the `suite` check; never commit to `main`
  directly.
- **Know whether your change is breaking before you open the PR.**
  `docs/versioning.md` is the rule and the only home for it: SemVer here
  governs the CLI (exit statuses included), the HTTP API, and **what a figure
  measures** — a stable field name over a changed definition is breaking, a
  *correction* to a figure that was wrong is not. It explicitly does **not**
  govern `SCHEMA_VERSION`, which is why 6 → 11 was not five major releases; that
  exclusion holds only while an existing database upgrades without data loss
  and without a refusal, so a schema change that cannot offer that is a major
  release rather than a reason to widen `IN_PLACE_UPGRADE_FROM`.

## Provenance

CPB was extracted in August 2026 from a private monorepo; history was not
carried across, so design rationale lives in this file, the README, and code
comments rather than in commit messages. Comments citing `#NNNN` issue numbers
and "CLAUDE.md rule #N" refer to that private repository — they record *why* a
decision was made and are kept for that reason, but the references do not
resolve publicly and the numbered rules are not the rules in this file.

If you add or change a number, say where it came from and when it was checked.
Facts about the Claude API are model-dependent — state which models a claim
covers and cite the source (the checked ones are recorded in
`docs/claude-api-token-accounting.md`, indexed from `docs/README.md`), or say
in the code that it is unverified (see
`transcript_slug()`, which documents its Windows encoding as a best-known value
with the symptom to look for if it is wrong, and separately asserts the property
the code actually guarantees).
