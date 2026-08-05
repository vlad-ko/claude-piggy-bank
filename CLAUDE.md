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
python3 ingest.py                          # ingest this project's own transcripts (idempotent, incremental)
python3 ingest.py --projects-dir ~/.claude/projects/<name>   # ingest a different project
python3 ingest.py --prune-missing          # DELETE rows for sources gone from disk (see Durability)
python3 serve.py                           # report at http://127.0.0.1:8377/
python3 serve.py --port 9000 --db path.db

python3 -m unittest discover tests -v                        # full suite
python3 -m unittest tests.test_ingest.StreamedRecordDedupeTest -v         # one class
python3 -m unittest tests.test_serve.ScopeLabellingTest.test_<name> -v    # one test
```

Both entry points default `--db` to `db/usage.db` (gitignored). `serve.py`
exits rather than starting if that DB is absent. Tests insert the repo root on
`sys.path` themselves, so run them from anywhere with `python3 -m unittest`.

## Four load-bearing constraints

These are enforced or defended in review; a change that breaks one gets pushback
even if the code is good.

1. **Standard library only**, Python 3.10+. No pip, no Node, no build step. The
   `stdlib-only` CI job ASTs every shipped module and fails on any import that
   is neither `sys.stdlib_module_names` nor local, and fails outright if a
   `requirements*.txt` / `pyproject.toml` / `setup.py` / `package.json` /
   `Pipfile` appears. Adding any of those files breaks the build by design.
2. **Nothing leaves the machine.** No network calls, no telemetry, no CDN. The
   page renders the user's own prompts, paths and source code, so a third-party
   script in it is a privacy and supply-chain surface. Vendor assets into
   `vendor/` (Chart.js already is, served through a path-escape-checked handler).
3. **No model in the loop.** Every figure is SQL, arithmetic or JSON parsing.
   Running CPB must stay free.
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

**No dollar figures anywhere (#30).** Tokens are measured; dollars were derived
from a hand-maintained list-rate table that went stale twice and diverged from
real spend by >2.5x, so the estimate was removed — schema column, module and
all — rather than qualified. A precise-looking figure wrong by a factor of two
is worse than none, because the reader cannot see the error. Do not reintroduce
a cost estimate, a "relative cost index" or any money-shaped field; rank by
total tokens and show the model so the reader weighs the tiers themselves.

## Architecture

Two modules, one direction of flow: `ingest.py` (transcripts → SQLite) →
`db/usage.db` → `serve.py` (SQLite → JSON) → `index.html` (JSON → charts).

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

**Schema** (`SCHEMA_VERSION`, `PRAGMA user_version`): `sessions`, `turns`,
`api_calls`, `agent_dispatches`, `subagent_runs`, `task_index_sessions`,
`ingest_state`. Ingest is incremental per file, keyed on size+mtime in
`ingest_state`; re-ingesting a changed file deletes its rows by `source_path`
first, so every hot path has a `source_path` index. `task_index_sessions`
distinguishes "scanned, dispatched nothing" (a real zero) from "never scanned".

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
- `_prepare_schema()` **refuses to run** (`SystemExit`) if a schema rebuild
  would drop rows whose source file no longer exists. It asks the filesystem,
  not `archived_at`, because that column postdates the older DBs the guard has
  to protect.
- Which is why a schema change that deletes no row must not go through that
  rebuild: on any corpus past the retention window the guard would refuse a
  change that risks nothing, and an untrue refusal is the same class of defect
  as an untrue number. `IN_PLACE_UPGRADE_FROM` lists the versions whose delta
  is row-preserving (v6 adds `ingest_runs`; v7 drops `api_calls.cost_usd` via
  `ALTER TABLE ... DROP COLUMN`, gated on a **runtime** check of
  `sqlite3.sqlite_version` ≥ 3.35). It is re-decided at every bump, never
  extended by habit, and the sub-3.35 fallback goes through the rebuild path
  **including** the guard — never around it.
- Windows containing archived sources are flagged in the report banner: totals
  are complete but no longer reproducible by re-ingesting.

## Testing conventions

- Assert the **state change**, not that something succeeded.
- A fixture must not make the defect undetectable: when two quantities could
  diverge, pin them deliberately unequal (the fixtures give every token class a
  different value so a swapped column mapping cannot pass).
- Verify a test has teeth by mutation — change one implementation line, confirm
  red, change it back. A test that survives a deliberate mutation is not a test.
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
