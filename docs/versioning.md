# Versioning

CPB follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html),
and is at **<!--cpb:version-->3.0.0<!--/cpb:version-->** (checked against `cpb.VERSION` on 2026-08-07; that constant
is the authority and this line has lagged a release before).

`cpb.VERSION` is the source of truth; `.claude-plugin/plugin.json` repeats it
as a literal because Claude Code's plugin loader reads that JSON without
running any Python, and `tests/test_cpb.py` pins the two equal so they cannot
drift.

## The manifest version gates whether users receive anything

Read this before the rest of the file. Everything below is about what a version
number *means*; this section is about what it *does*, and it is the part that
breaks a user rather than confusing one.

Claude Code resolves a plugin's version from the first of these that is set
([Plugins reference § Version
management](https://code.claude.com/docs/en/plugins-reference#version-management),
checked 2026-08-07):

1. `version` in `.claude-plugin/plugin.json`
2. `version` in the marketplace entry
3. the git commit SHA of the plugin's source
4. `unknown`, for npm sources and local directories outside a git repository

CPB sets the first. **That string is the cache key Claude Code decides updates
by**, and because `.claude-plugin/marketplace.json` lists the plugin with
`"source": "./"`, the marketplace is this repository and the ref users resolve
is the default branch — so every merge to `main` is a release, with no tag in
between.

The consequence, *measured here* on 2026-08-07 against Claude Code 2.1.223 with
a throwaway marketplace and an isolated `CLAUDE_CONFIG_DIR`:

| published | `claude plugin update` said | what the install received |
|---|---|---|
| a shipped file changed, `version` 1.6.0 → 1.7.0 | `updated from 1.6.0 to 1.7.0` | the change, from a new cache directory named `1.7.0` |
| a shipped file changed, `version` left at 1.7.0 | `already at the latest version (1.7.0)` | nothing; the marketplace clone had it, the installed copy did not |

Those digits are the throwaway install's, not a plan: what the next release is
numbered is decided by the rules further down this file, and `cpb.VERSION` is
the only place to read the current one.

So a merged fix with an unbumped version **is invisible to every existing
install**. It looks shipped and is not, and no test of the code would notice.
Had CPB left `version` unset, every commit would have counted as a new version
and this trap would not exist — but then `cpb.py --version` could not name the
build that produced a number, which is a load-bearing property here.

The bump is therefore enforced rather than remembered.
`.github/scripts/check_plugin_version_bump.py` fails any change that touches a
path reaching an installed user — the manifest, `hooks/`, `skills/`,
`vendor/`, `index.html`, any top-level module — while `version` stays where it
was, or moves backwards. A changed path it cannot classify is a **refusal**,
not a pass: a new plugin component directory has to be classified deliberately
rather than slip through a set nobody re-read. Prose changes, `tests/`, `docs/`
and the catalog itself are not shipped behaviour and need no release.

Five places state the version and move together:

| where | pinned by |
|---|---|
| `cpb.VERSION` | the authority; everything else is compared to it |
| `.claude-plugin/plugin.json` | `tests/test_cpb.py`, asserted equal to `cpb.VERSION` |
| `README.md`, `CLAUDE.md`, this file | `DocsStateTheShippedVersionTest`, via the `cpb:version` HTML-comment marks |

The marketplace entry deliberately declares **no** version: `plugin.json` wins
silently over it, so a sixth copy could only ever be a copy free to disagree.

This file exists because the number is meaningless without it. A version says
"this release did not break you" only if something states what *break* covers,
and for a measurement tool the obvious reading — the schema changed, therefore
it is breaking — is both wrong and, on this codebase, wrong several times over.
Claims below about how upgrades behave were checked against `ingest.py` on
2026-08-05; each names the code it rests on, and where one names a schema
version, `SCHEMA_VERSION` in that file is the authority and will have moved on.
The mechanism is what this file describes; the digits are illustration.

## What the version governs

Three surfaces. These are what a user builds a habit, a script or a screenshot
on, so a change to any of them is a **major** release.

**1. The command line.** The subcommands (`cpb.py ingest`, `cpb.py serve`),
their flags, the `CPB_DB` environment variable, and running `ingest.py` or
`serve.py` directly — which `cpb.py` composes rather than replaces, so both
remain interface. **Exit statuses are part of this surface**, not an
implementation detail: both scripts refuse by raising `SystemExit`, and a
release that turned a refusal into a `0` would report a run that measured
nothing as a run that measured zero. That is the failure this project exists
not to commit, so it is breaking by definition rather than by convention.

**2. The HTTP API.** The routes `serve.py` answers and the fields in their
payloads. Removing a field, renaming one, or changing its type is breaking;
`index.html` is a client of that API like any other, and the fact that this
repository ships the only client does not make the boundary private.

**3. What a figure means.** A field whose name is stable but whose definition
has changed is a **breaking change**, even though no client fails to parse it.

That third clause is not standard SemVer, and it is the one this project most
needs. The payload is a set of measurements. Someone who wrote down a number
last month is relying on it still ranging over the same set — and a silent
redefinition is worse than a removed field, because a removed field raises
where a redefined one just reads differently. This is the compatibility form of
the rule that an aggregate must name the set it ranges over.

The plugin's behaviour is covered under (1): which hooks fire, the name `/cpb`
resolves under, and the fact that the database resolves to
`${CLAUDE_PLUGIN_DATA}` (a directory that survives plugin updates) rather than
somewhere a reinstall would take with it. Moving it would strand a database
that, past Claude Code's transcript retention, is the only copy of a user's
history.

Moving `/cpb` from `commands/cpb.md` to `skills/cpb/SKILL.md` (#73) is **not**
a change to that surface: a plugin namespaces both layouts the same way, so the
command was `/claude-piggy-bank:cpb` before the move and is after it. The file
that ships changed; nothing a user types did. It is still a release, because
the shipped plugin changed and an install that did not receive it would be
running a layout the docs no longer describe.

### Where a break is announced

Classifying a break is not telling anyone about it, and for one release this
file did only the first. **A change to any of the three surfaces above is
announced in [`releases.md`](releases.md)**, under the version that shipped it,
in the terms a caller meets it in: what stopped working, and what to do
instead. `tests/test_release_notes.py` asserts the shipped `cpb.VERSION` has an
entry there, so a release cannot ship a break with nowhere to read about it —
the same mechanism, and the same reason, as the version spans this file's
opening paragraph is pinned by.

That record is not a changelog and the decision below stands: a **correction**
to a figure still goes on the README record, beside the number. A break is not
a wrong number, so it gets its own place rather than diluting one that is
specifically about figures that were wrong.

### Correction is not redefinition

The sharp edge of clause 3, and the exemption it needs.

- A **redefinition** — the figure now measures a different thing — is major.
- A **correction** — the figure now measures what it always claimed to — is
  not. It is a fix, and it takes a patch or minor bump on its own merits.

The dedupe fix is the worked example: it moved main-thread call counts by 2.36x
and subagent counts by 1.91x (the figures in the README's record). Enormous
movement, no redefinition — `api_calls` always claimed to count API calls, and
had been counting transcript records. Calling that breaking would put the tool
in the position of promising to keep reporting a number it knows is wrong.

What a correction owes instead is disclosure: a correction that moves a
headline figure goes on the record in `README.md`, with what the number was,
what it is, and how that was measured. The version number is not the place a
user learns their old figures were wrong.

## What the version does not govern

- **`SCHEMA_VERSION` and the database's internal shape.** The next section is
  entirely about this clause.
- **Reading `db/usage.db` with your own SQL.** Nothing stops you and nothing
  ever will, but the tables are internal: they are excluded above, so a query
  written against them is on the unsupported side of this line by construction.
  The HTTP API is the supported way to read CPB's measurements.
- **The transcript format CPB ingests.** That is Claude Code's, documented by
  Anthropic as internal and subject to change between releases. CPB cannot
  promise stability in a format it does not own; what it does instead is census
  the shape it saw (`source_shape`, #15) so a change surfaces as a named count
  rather than as a total that quietly got smaller.
- **Internal module, function and constant names**, the test suite, and the
  fixtures.

## Why a schema bump is not a major bump

This is the load-bearing clause, and it needs stating precisely because the
naive reading gets it badly wrong: `SCHEMA_VERSION` moved 6 → 9 within a single
session's work, and reached 10 days later. Treating each hop as breaking would
have put CPB past 4.0.0 before it ever reached 1.0.0 — a run of major releases
in which no user's command, request or figure changed at all.

**A user never sees `SCHEMA_VERSION`.** It is a `PRAGMA user_version` stamp
inside a file CPB owns and manages, and the migration machinery absorbs the
difference on the next run. **That machinery is the compatibility guarantee**,
which is why it, and not the version number, is what has to hold:

- `_schema_plan()` is pure and has six named exits, so the precedence between
  them is code rather than a comment. The row-preserving one is `PLAN_IN_PLACE`.
- `IN_PLACE_UPGRADE_FROM` lists the versions whose delta to the *current* shape
  deletes no row. It is re-decided at every bump against the current shape,
  never extended by habit.
- Two bounded permissions keep "in place" from meaning "whatever is convenient":
  a table may be created empty only if it is in `IN_PLACE_CREATABLE_TABLES` —
  only, that is, where an **empty one is a true statement** — and a column may
  be added to a populated table only if it is in `IN_PLACE_ADDABLE_COLUMNS`,
  where NULL is true of the rows that already exist. `CREATE TABLE IF NOT
  EXISTS` would otherwise silently grant the first to every table, including
  ones where empty is a lie.
- Where an upgrade cannot preserve rows, it **refuses and changes nothing**:
  `PLAN_REFUSE_REAPED` when a rebuild would delete measurements whose source
  file is gone from disk, `PLAN_REFUSE_SHAPE` when the tables contradict the
  version stamped on them.

So the rule, in one sentence:

> **A schema bump is a minor or patch change so long as a database at the
> previous version upgrades without data loss and without a refusal. The first
> time a change cannot offer that, the release is major.**

The second half is what makes the first honest. If a future change forces a
rebuild — or forces a refusal on any corpus with a reaped source — then the
user's upgrade instruction becomes "back up and re-ingest, and accept that
what was reaped is gone" or "stay on the old version". That is precisely what a
major version is for, and saying so in advance is the difference between 1.0.0
as a commitment and 1.0.0 as an optimistic label.

### Three things this promise does not cover

Stated rather than implied, because a guarantee with unstated conditions is how
a version number starts lying.

**The promise runs forward from the shape 1.0.0 ships with** — whatever
`SCHEMA_VERSION` reads at that commit, which is the only copy of that fact
worth keeping. A database older than the floor of `IN_PLACE_UPGRADE_FROM` is
dropped and re-derived from the transcripts still on disk — or, if any tracked
source has already been reaped, refused outright by the guard. That predates
1.0.0 and is not retroactively covered by it; the guard means the outcome is a
refusal rather than silent loss, which is the property actually worth having.

**One hop is conditional on the SQLite the interpreter links.** The v7 → v8 hop
drops the retired `cost_usd` column with `ALTER TABLE ... DROP COLUMN`, which
needs SQLite 3.35+ (2021-03-12). CPB detects that at runtime rather than
inferring it from the Python version, and where it is absent the upgrade falls
back to the rebuild path *including* the reaped-source guard — so on an old
SQLite paired with a corpus past the retention window, that hop is a refusal.
"Upgrades in place" is therefore a claim about a machine, and checking it on
the ones we cannot see is part of the work of calling a schema bump minor.

**A refusal is a supported outcome, not a broken one.** `_prepare_schema()`
raising `SystemExit` over a database it will not risk is the guarantee
operating, not failing — but per the rule above, a *change* that newly causes
that refusal on a previously-upgradable database is a breaking change and takes
a major bump.

## Deciding which part to bump

| bump | what it is for |
|---|---|
| **major** | any change to the three governed surfaces: a removed or renamed flag, route or payload field; a changed exit status; a figure that now measures a different thing; a schema change that cannot upgrade an existing database in place |
| **minor** | new capability that leaves all three intact: a new route, a new payload field, a new flag whose default preserves today's behaviour, a new detector, a row-preserving schema hop |
| **patch** | a fix that changes nothing a caller depends on — including a **correction** to a figure that was wrong, per the exemption above |

### Worked examples

Three changes this project actually made. Each is here because it separates
this rule from a weaker one that would have called them patches.

| change | this rule says | why |
|---|---|---|
| Removing cost estimates entirely ([#30](https://github.com/vlad-ko/claude-piggy-bank/issues/30)) | **major** | A payload field and a whole class of figure were removed, and panels that ranked "by spend" now rank by total tokens. A client reading a dollar figure gets nothing. |
| `Avg context/call` → a median ([#31](https://github.com/vlad-ko/claude-piggy-bank/issues/31), [#51](https://github.com/vlad-ko/claude-piggy-bank/issues/51)) | **major** | Clause 3, and the reason it exists. Measured 2026-08-05 the mean was 237,153 tokens against a median of 155,255 — a 1.53x difference in the headline figure. Even had the field name been kept, the number now describes a different statistic, and only the meaning clause catches that. |
| `subagent_tokens` → `peak_context_tokens` ([#10](https://github.com/vlad-ko/claude-piggy-bank/issues/10), [#27](https://github.com/vlad-ko/claude-piggy-bank/issues/27)) | **major** | A rename at the API boundary — a client reading `subagent_tokens` finds no such field. The rename was itself a correction of meaning (a peak-context high-water mark had been presented as spend), which is exactly what clause 3 is for. |

All three landed **before 1.0.0**, when 0.x offered no compatibility promise
and none of them was announced as breaking. That is not a defect being
confessed; it is the reason this file exists. Under this rule each would now be
a major release, and a rule that would have called any of them a patch would be
too weak to be worth writing.

## Pre-release and the record

There is no changelog. Two records exist instead, and each covers what the
other is the wrong shape for:

- **Corrections** that moved a headline number are kept in `README.md` under
  "The record", in full, because a tool about measurement should show its own
  corrections rather than quietly restate them — and because a number's history
  is more useful to a reader beside the number than in a file of one-line
  entries.
- **Breaks** — a change to one of the three governed surfaces — are announced
  in [`releases.md`](releases.md), per release, in the terms a caller meets
  them in. A release with nothing to migrate says so there too: that is a
  useful fact, not an empty entry.

Neither is a list of commits, and neither is a dead end — each links the other.

Pre-release identifiers (`1.2.0-rc.1`) are available if a change ever needs
soak time, and carry SemVer's ordinary meaning: lower precedence than the
release they precede, and no compatibility promise of their own.
