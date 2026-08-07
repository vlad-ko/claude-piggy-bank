# The Claude Code plugin

CPB ships as a Claude Code plugin so that ingest happens **when the transcript
is written**, rather than whenever someone remembers to run `ingest.py`. The
plugin is the repository: `.claude-plugin/plugin.json` sits at the repository
root and the hooks invoke the same `ingest.py` and `serve.py` a manual user
runs. There is no second code path and no packaging step. Since
[#73](https://github.com/vlad-ko/claude-piggy-bank/issues/73) the repository is
also its own **marketplace**, so the same tree is catalog, plugin and checkout.

Nor is one needed. A packaging step would exist to install dependencies, and
CPB has none — the `stdlib-only` CI job resolves every import in every shipped
module against `sys.stdlib_module_names` on an interpreter with nothing
installed. (A second job rejects manifests that *declare* a dependency. Since
[#50](https://github.com/vlad-ko/claude-piggy-bank/issues/50) it inspects
`pyproject.toml` rather than banning it by filename, so packaging is a reviewed
decision rather than an automatic build break — but nothing about the plugin
requires it.)

This document records *why* the packaging looks the way it does. The JSON files
cannot carry comments, and every value in them turns on a documented property of
the plugin system rather than on taste.

**Provenance.** Every specification claim below is *documented* — cited to the
official Claude Code docs with the date it was checked. Claims about CPB's own
behaviour are *measured here* and marked as such. The two are never merged.

## What is shipped

| path | what it is |
|---|---|
| `.claude-plugin/plugin.json` | the manifest: name, version, license, repository |
| `.claude-plugin/marketplace.json` | the catalog — see [The marketplace layer](#the-marketplace-layer) |
| `hooks/hooks.json` | the three ingest triggers |
| `hooks/cpb_ingest_hook.py` | the handler the triggers run |
| `skills/cpb/SKILL.md` | the `/cpb` skill that opens the report |

The two manifests belong inside `.claude-plugin/` and nothing else does;
`hooks/` and `skills/` must be at the plugin root or they are silently never
loaded. (Documented: [Plugins reference § Plugin structure
overview](https://code.claude.com/docs/en/plugins-reference#plugin-directory-structure),
checked 2026-08-07. `tests/test_plugin_manifest.py` asserts it, because "silently
never loaded" is not a failure anyone notices.)

### Why `skills/`, not `commands/`

`/cpb` was `commands/cpb.md` until [#73](https://github.com/vlad-ko/claude-piggy-bank/issues/73).
The [File locations
reference](https://code.claude.com/docs/en/plugins-reference#file-locations-reference)
(checked 2026-08-07) lists `commands/` as "Skills as flat Markdown files. **Use
`skills/` for new plugins**", so the file moved to `skills/cpb/SKILL.md`. The
prompt is unchanged; the only edit was adding `name: cpb` to the frontmatter,
because in a *plugin* skill that field sets the last segment of the command
([Skills § How a skill gets its command
name](https://code.claude.com/docs/en/skills#how-a-skill-gets-its-command-name),
checked 2026-08-07) and writing it down beats depending on the directory
staying called `cpb`. The command is `/claude-piggy-bank:cpb`, and `/cpb`
resolves to it while no other plugin ships that name. *Measured here*
(2026-08-07, Claude Code 2.1.223): after a marketplace install,
`claude plugin details claude-piggy-bank@claude-piggy-bank` reported
`Skills (1)  cpb`.

**A migrated plugin reports `0 skills` on `/reload-plugins`.** The reload
summary counts only `commands/`, so a plugin that correctly uses `skills/`
shows nothing there. That is the summary being wrong, not the migration.
(*Product-owner report*, 2026-08-05, via
[#73](https://github.com/vlad-ko/claude-piggy-bank/issues/73) — **not verified
here**; `/reload-plugins` is interactive and this document's other measurements
were taken headlessly. The skill's presence was confirmed through
`claude plugin details` instead, above.)

## What the plugin may ask the model to do

Four of those five files are code and configuration. The fifth,
`skills/cpb/SKILL.md`, is a **prompt** — that is what a skill file is — so it is
the one place in the plugin where a model is asked to do anything at all, and
the project's third constraint had to say precisely how far that reaches.

**The line:** a model may read a **finished** measurement and explain it; it may
never produce, compute, estimate or fill in a figure. Stated positively, not as
an exception, so there is nothing to reason outward from. `/cpb` does not
produce measurement, it produces guidance.

| surface | model | why |
|---|---|---|
| `ingest.py`, `serve.py`, `index.html` | **none, ever** | every figure is SQL, arithmetic or JSON parsing; the report runs free and offline |
| `hooks/cpb_ingest_hook.py` | **none, ever** | it spawns one `ingest.py --transcript` and exits; no hook handler is of type `prompt` or `agent`, and `tests/test_plugin_manifest.py` asserts that |
| `skills/cpb/SKILL.md` | **yes, bounded** | it runs in a session the user is already paying for, and summarises figures the report has already computed |

The reason the line sits exactly there: a model asked for a number it was not
given will produce a fluent, plausible one. That is the project's *absence is
never rendered as a value* failure arriving in better prose — no `Optional[int]`
catches it, and no reader can see the error. A summary of a computed figure has
a source; a figure a model arrived at has none.

Two things this is **not**:

- **Not a restriction Claude Code imposes.** The plugin reference places no
  limit on what a skill may ask the model to do; skills are prompts by
  design. (External: <https://code.claude.com/docs/en/plugins-reference> — the
  revision `tests/test_plugin_manifest.py` records as checked 2026-08-07. It is
  *silent* on the question rather than permissive about it, which is weaker
  evidence than a positive statement, and it was not re-fetched when this
  section was written.) The constraint is CPB's own.
- **Not a claim that `/cpb` is free.** Ingest and the report are free and
  offline, absolutely and unchanged. `/cpb` spends tokens in a session already
  being paid for; Claude Code surfaces that as a per-plugin *context cost* in
  the `/plugin` panel. (Product-owner report, 2026-08-05, **unverified** — not
  checked against the Claude Code documentation here.) Those are two claims
  with two scopes and the older single sentence, "running CPB costs nothing",
  collapsed them.

**And `/cpb` never becomes the only path to a number**
([#79](https://github.com/vlad-ko/claude-piggy-bank/issues/79)): it summarises,
then always links to the report. Two runs of a model produce two different
summaries, and a measurement has to be reproducible where a conversation does
not — so the page stays the artifact, and the summary is a way into it.

## Why hooks, and why these three

Plugin-shipped hooks in `hooks/hooks.json` activate when the plugin is enabled.
**No edit to any `.claude/settings.json` is required**, which is the fact the
whole distribution decision rests on. (Documented: [Plugins reference §
Hooks](https://code.claude.com/docs/en/plugins-reference#hooks) — location
`hooks/hooks.json` in plugin root — and [File locations
reference](https://code.claude.com/docs/en/plugins-reference#file-locations-reference),
checked 2026-08-05.)

| trigger | timeout | why it earns its place |
|---|---|---|
| `SubagentStop` | 30s | the load-bearing one — see below |
| `Stop` | 30s | fires per assistant turn, so the report is near-live |
| `SessionEnd` | 5s | last call for the finished session, best-effort |

None of the three sets a `matcher`. An omitted matcher means "match all"
(documented: [Hooks § Matcher
patterns](https://code.claude.com/docs/en/hooks#matcher-patterns), checked
2026-08-05). `Stop` supports no matcher at all; `SessionEnd` would filter on exit
reason and `SubagentStop` on agent type, and filtering either would drop
measurable spend on the floor — which the report would then render as a smaller
number, not as a gap.

### `SubagentStop` is the one that matters

Subagent transcripts are the largest measured share of spend and the most
perishable. *Measured here* on the reference corpus (scanned 2026-08-04):
subagents are **~78% of all API calls**, and **211 subagent runs are already
permanently `unavailable`** — a dispatch is proven to have happened but its
transcript is gone, so that spend can never be measured, only known to be
missing. Ingesting at the moment the subagent finishes is the difference between
capturing that spend and losing it for good.

**This hook must read `agent_transcript_path`, not `transcript_path`.** On
`SubagentStop` the common `transcript_path` field is the *parent session's*
transcript; `agent_transcript_path` is the subagent's own, in the nested
`subagents/` folder. (Documented: [Hooks §
SubagentStop](https://code.claude.com/docs/en/hooks#subagentstop), checked
2026-08-05.) A handler that read `transcript_path` would re-ingest the main
thread, exit 0, and never touch the subagent — a success report for work that
did not happen. `TRANSCRIPT_FIELD_BY_EVENT` in the handler encodes the
distinction and a test pins it with both fields pointing at different files, so
reading the wrong one cannot pass.

*Measured here* (2026-08-05), by running a throwaway probe plugin under
`claude --plugin-dir` and dumping the real hook payloads: one `SubagentStop`
carried both fields, pointing at two different files —

```
transcript_path       .../projects/<project>/<session>.jsonl
agent_transcript_path .../projects/<project>/<session>/subagents/agent-<id>.jsonl
```

— and `agent_transcript_path` matches CPB's canonical subagent glob exactly.
The same probe confirmed the other facts this packaging rests on. All three
triggers fired with **no** hook entry in any `settings.json`. `transcript_path`
arrived absolute rather than `~`-relative, despite the tilde in the published
examples. `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PLUGIN_DATA` were both exported to
the hook process, even in `--plugin-dir` development mode, which is why the
handler's refusal when the data directory is absent describes an anomalous
state rather than an everyday one. And the hook's working directory was the
*user's project*, not the plugin root — which is why every path in `hooks.json`
goes through `${CLAUDE_PLUGIN_ROOT}`, and why a relative `transcript_path` is
refused rather than resolved against the cwd.

### `SessionEnd` is best-effort, and that is not a defect

SessionEnd hooks share a **1.5-second budget**, and a per-hook `timeout` set by
a *plugin* does not raise it — only timeouts in settings files do. (Documented:
[Hooks § SessionEnd](https://code.claude.com/docs/en/hooks#sessionend), checked
2026-08-05.) So the 5-second `timeout` in `hooks.json` is an honest declaration
of intent that Claude Code will cap in practice.

This is survivable because `Stop` already ingested the session through its last
assistant turn, and because ingest is idempotent and incremental: a `SessionEnd`
killed mid-run leaves the SQLite write rolled back, and a later `python3
ingest.py` picks the file up unchanged. A user who wants the full run can raise
the budget themselves:

```bash
CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS=5000 claude
```

### Why every timeout is explicit

The default timeout for a `command` hook is **600 seconds** (documented: [Hooks §
Common fields](https://code.claude.com/docs/en/hooks#common-fields), checked
2026-08-05). A passive measurement tool holding a session for ten minutes is not
a tolerable failure mode, so no hook here relies on the default. The handler
independently bounds the `ingest.py` child it spawns at
`INGEST_TIMEOUT_SECONDS = 20`, so a hung ingest is killed by CPB before Claude
Code has to intervene.

### Why no hook is `async`

`async: true` would keep the session from waiting on ingest, but async hooks are
**not deduplicated across firings** — "each execution creates a separate
background process" (documented: [Hooks §
Limitations](https://code.claude.com/docs/en/hooks#limitations), checked
2026-08-05). That trades a bounded wait for an unbounded number of concurrent
writers on one SQLite file, and it moves failure reporting into a channel that
is suppressed unless the user runs with `--verbose`. Both are bad trades for a
tool whose entire job is to be trustworthy about what it did and did not
measure.

## Where the database lives

The hooks write to `${CLAUDE_PLUGIN_DATA}/usage.db`, passed to `ingest.py`
through the `CPB_DB` environment variable. `${CLAUDE_PLUGIN_DATA}` is a
persistent directory that **survives plugin updates**; `${CLAUDE_PLUGIN_ROOT}`
is replaced on every update. (Documented: [Plugins reference § Persistent data
directory](https://code.claude.com/docs/en/plugins-reference#persistent-data-directory),
checked 2026-08-05.)

That distinction is not a nicety here. Past Claude Code's ~30-day transcript
reap the database is the **only surviving copy** of the history, not derived
data. A database inside the plugin root would be destroyed by the next plugin
update, silently, with no way to rebuild it.

So the handler resolves the location in this order, and **refuses** rather than
guessing:

1. `CPB_DB` already set in the environment — the user has chosen; pass it
   through untouched.
2. `CLAUDE_PLUGIN_DATA` set — use `<data>/usage.db`.
3. `CLAUDE_PLUGIN_ROOT` set but `CLAUDE_PLUGIN_DATA` not — **refuse.** We are
   installed as a plugin with nowhere durable to write, and falling back to the
   plugin root would destroy measurements on the next update.
4. Neither set — not a plugin install at all, so leave `ingest.py`'s own default
   (`db/usage.db` beside it in the checkout) alone. It is correct and durable
   there.

Uninstalling the plugin from its last scope deletes the data directory by
default. Pass `--keep-data` to keep the database, or back it up first — see the
uninstall note in the README.

### The improvised run: a bare `ingest.py` inside an install

The rule above is the *hook's*. It leaves a hole one size smaller, and
[#94](https://github.com/vlad-ko/claude-piggy-bank/issues/94)'s last open
criterion is that hole: somebody types `python3 ingest.py` themselves, with no
`--db` and no `CPB_DB`, from inside an installed plugin. The default —
`db/usage.db` beside the script — is then `${CLAUDE_PLUGIN_ROOT}/db/usage.db`,
and everything the section above says about that directory applies.

So `ingest.default_database()` decides the built-in default the same way the
handler decides `CPB_DB`, and `serve.py` calls the *same function* rather than
carrying a second copy of the decision. Three outcomes:

| where the script is | what happens |
|---|---|
| a checkout | `db/usage.db` beside it — unchanged, unannounced, correct and durable |
| an install where Claude Code named `${CLAUDE_PLUGIN_DATA}` | `${CLAUDE_PLUGIN_DATA}/usage.db`, announced on stdout |
| an install with no knowable durable location | **refuses**, naming that path and `--db` |

`--db` and `CPB_DB` are untouched: the default is computed only when neither
named a database, so a run that says where to write is never refused over a
fallback it does not reach.

**Deciding "this is an install" is the part that had to be measured**, because
a wrong answer in *either* direction is worse than none — a false negative is
the bug, and a false positive refuses the plain-clone path that
`db/usage.db` is exactly right for. Two independent observations, either
sufficient, each covering the other's blind spot:

1. **`${CLAUDE_PLUGIN_ROOT}` names the directory the script is in.** Set alone
   it is evidence about the *process*, not about this script — another
   plugin's hook can spawn a shell that runs a CPB checkout — so containment is
   required, not the variable's presence. *Measured here* (2026-08-07): a Bash
   tool call inside a Claude Code session with plugins enabled had **neither**
   `CLAUDE_PLUGIN_ROOT` nor `CLAUDE_PLUGIN_DATA` in its environment. Claude
   Code exports them to a plugin's own hook and skill invocations (measured
   2026-08-05, above), not to shells in general — which is precisely why this
   check cannot be the only one. *(Scope: one Bash tool call, in a subagent, on
   this host. It is evidence that the variable is not universally present, not
   a claim about every invocation shape.)*
2. **The script sits in Claude Code's own plugin store.** *Measured here*
   (2026-08-07, this host's `~/.claude/plugins/`), across two plugins installed
   from two different marketplaces:

   ```
   ~/.claude/plugins/cache/vercel-vercel-plugin/vercel-plugin/0.24.0/
   ~/.claude/plugins/cache/laravel/laravel-simplifier/1.0.0/
   ~/.claude/plugins/marketplaces/<marketplace>/
   ~/.claude/plugins/data/<marketplace>-<plugin>/
   ```

   The cache path carries the **version**, so an update builds a new directory;
   the data path carries none. That is the same shape the install-and-update
   run above recorded independently. `ingest.PLUGIN_STORE_ANCESTORS` matches on
   the adjacent pair `plugins/cache` or `plugins/marketplaces` in the resolved
   path — the marketplace clone is included because CPB's `"source": "./"`
   makes it a full checkout of this repository that Claude Code re-fetches and
   deletes with the marketplace. There is **no `.git` anywhere under `cache/`**,
   so an install is a copy rather than a clone.

   This layout is **internal to Claude Code and undocumented**, so it is only
   ever the second piece of evidence. If a release moves it, this detector
   stops firing — a false negative check (1) still covers — and it cannot start
   firing on a clone anywhere a person would actually put one.

**The data directory is paired with the root, never taken alone.**
`${CLAUDE_PLUGIN_DATA}` on its own says a plugin process is running, not that
it is *this* plugin; writing another plugin's data directory would be a guess
with an answer's shape. So the resolve outcome needs (1); evidence (2) on its
own refuses.

This is the version that also breaks CPB's own CLI compatibility promise on
purpose — a run that exited 0 can now exit non-zero — which is why it is a
major release. See [`releases.md`](releases.md).

### The update path, measured rather than reasoned

The paragraph above is the *documented* claim. Because losing a user's only
copy of their history is not a failure worth finding out about later, it was
also **measured here** end to end (2026-08-07, Claude Code 2.1.223): a
throwaway marketplace was served over smart HTTP into an isolated
`CLAUDE_CONFIG_DIR`, the plugin installed from it, a session run so the hooks
wrote a real database, the version bumped and republished, and
`claude plugin update` run.

| | before the update | after |
|---|---|---|
| `${CLAUDE_PLUGIN_ROOT}` | `…/plugins/cache/claude-piggy-bank/claude-piggy-bank/1.6.0` | `…/1.7.0` — a directory that did not exist before |
| `${CLAUDE_PLUGIN_DATA}` | `…/plugins/data/claude-piggy-bank-claude-piggy-bank` | unchanged — no version anywhere in the path |
| `usage.db` | SHA-256 `78f98623…` | `78f98623…`, byte-identical |

The plugin root carries the version and is therefore replaced on every update;
the data directory is keyed on the plugin *identity* and is not. After the
update a further session's `SessionEnd` hook ran from
`…/1.7.0/hooks/cpb_ingest_hook.py` and appended its session to the *same*
database, which then held both the pre-update session and the post-update one.
That is the property the whole location decision rests on, and it is now an
observation rather than an inference.

## The marketplace layer

`/plugin marketplace add vlad-ko/claude-piggy-bank` then `/plugin install`
requires a **catalog**, which is a separate file from the manifest:
`.claude-plugin/marketplace.json`. CPB is a single-plugin repository, so it is
its own marketplace — the repository is both catalog and plugin, and the entry
points back at the directory the catalog lives in:

```json
{
  "name": "claude-piggy-bank",
  "description": "…",
  "owner": { "name": "Vlad Ko", "url": "https://github.com/vlad-ko" },
  "plugins": [
    { "name": "claude-piggy-bank", "source": "./" }
  ]
}
```

Every choice in those few lines is a documented property rather than taste
([Plugin marketplaces](https://code.claude.com/docs/en/plugin-marketplaces),
checked 2026-08-07):

- **`"source": "./"`** — a relative path resolves against the marketplace
  *root*, the directory containing `.claude-plugin/`, not `.claude-plugin/`
  itself. It must start with `./` and may not contain `..`. A `github` source
  would fetch a second copy of a repository Claude Code has already cloned.
- **No `version` in the entry.** Version resolution takes `plugin.json` first
  and "always uses the `plugin.json` value without warning", so a version here
  could only ever be a second copy free to disagree. *Measured here*
  (2026-08-07): an entry declaring `9.9.9` against a manifest declaring `1.6.0`
  validated with a warning naming the mismatch, and installed `1.6.0`.
- **No `description` in the entry** for the same reason. *Measured here*:
  with the entry silent, `claude plugin details` showed the description from
  `plugin.json`.
- **The marketplace and the plugin share the name `claude-piggy-bank`**, so
  the install string is `claude-piggy-bank@claude-piggy-bank`. Doubled, and
  deliberate: the marketplace name is what a user types after `@`, and the
  repository name is the only string they can predict without reading
  anything. It is not one of the [reserved
  names](https://code.claude.com/docs/en/plugin-marketplaces#marketplace-schema);
  `tests/test_plugin_manifest.py` carries that list so a future rename cannot
  land on one.

**The one thing `claude plugin validate` does not check is the one most likely
to be wrong.** *Measured here* (2026-08-07): with `source` pointed at
`./plugins/claude-piggy-bank`, a directory that does not exist, the validator
printed `✔ Validation passed`. The install fails later, on a user's machine.
`tests/test_plugin_manifest.py` resolves the source itself and demands a
`plugin.json` at the end of it, because nothing else does.

Adding the catalog also changed what `claude plugin validate .` validates — see
[Validation](#validation) below.

## The version field is the cache key

`version` in `plugin.json` is no longer only a label. Claude Code resolves a
plugin's version from the first of: `version` in `plugin.json`, `version` in
the marketplace entry, the git commit SHA, then `unknown` ([Plugins reference §
Version
management](https://code.claude.com/docs/en/plugins-reference#version-management),
checked 2026-08-07). CPB sets the first, so **that string decides whether an
update exists**, and the plugin cache is literally a directory named after it.

The consequence is sharp enough to be worth stating as a failure mode:
**a merged change with an unbumped version reaches nobody, and looks shipped.**
*Measured here* (2026-08-07), the two halves side by side:

| published | `claude plugin update` said | what the install received |
|---|---|---|
| `SKILL.md` changed, `version` 1.6.0 → 1.7.0 | `updated from 1.6.0 to 1.7.0` | the new file, from a new cache directory |
| `SKILL.md` changed, `version` left at 1.7.0 | `already at the latest version (1.7.0)` | nothing — the marketplace clone had the change, the installed copy did not |

Because `marketplace.json` lists the plugin with `"source": "./"`, the
marketplace *is* this repository and the ref users resolve is the default
branch: **every merge to `main` is a release**, with no tag in between. So the
bump is enforced where it can still block, on the pull request, by
`.github/scripts/check_plugin_version_bump.py`. If a change touches a path that
reaches an installed user — the manifest, `hooks/`, `skills/`, `vendor/`,
`index.html`, any top-level module — the version must move forward or the
`plugin version` job fails. A changed path the script cannot classify is a
**refusal**, not a pass, so a new plugin component directory forces a decision
rather than inheriting a silent gap.

The version numbers in the table above are the throwaway install's, chosen to
make the two runs distinguishable; they are not a statement about what CPB
ships next. `cpb.VERSION` is the only place to read that.

Five places state the version and must move together: `cpb.VERSION` (the
authority), `.claude-plugin/plugin.json`, `README.md`, `CLAUDE.md` and
`docs/versioning.md`. `tests/test_cpb.py` pins all five, and the CI check reads
`cpb.VERSION` itself and refuses if the manifest disagrees with it — so the
check cannot end up policing a copy that has drifted from the constant.

## The failure policy

Two project rules point in opposite directions: CPB must never interrupt
someone's work because an ingest failed, and CPB must never swallow a failure
into a benign default. Claude Code's exit-code contract resolves both at once
(documented: [Hooks § Exit code
output](https://code.claude.com/docs/en/hooks#exit-code-output), checked
2026-08-05):

- **Exit 2 blocks.** On `Stop` it prevents Claude from stopping and continues
  the conversation; on `SubagentStop` it prevents the subagent from stopping.
  The handler never returns 2, and a sweep in `tests/test_plugin_hook.py`
  asserts that no input can make it.
- **Exit 1 is a non-blocking error.** The session proceeds and the transcript
  shows a `hook error` notice with **the first line** of stderr. Loud,
  operator-visible, costs the user one line of text.

Because only the first line is shown, every message the handler emits is
collapsed to one line. Because the transcript notice scrolls away, every failure
is *also* appended to `cpb-hook.log` in the persistent data directory, beside
the database, capped at 256 KiB with one rotation. Successes write nothing —
`Stop` fires every turn, and a success line per turn would grow without bound in
a directory that deliberately outlives plugin updates.

## Concurrency

`Stop` and `SubagentStop` can fire close together, all matching hooks run in
parallel, and several projects can share one database.

**Correctness is never at risk.** SQLite serialises writers with a file lock,
and `ingest.py` connects through `sqlite3.connect()`, whose `timeout` defaults
to 5 seconds. A second writer waits; it does not corrupt. Past that timeout
`ingest.py` exits non-zero with `database is locked`.

**The signal is at risk.** Because CPB ships two triggers that routinely fire
together, contention is likely rather than theoretical, and left alone it would
surface as a `hook error` notice during ordinary use. A notice users see often
enough to learn to ignore is worse than no notice at all: it spends the only
loud channel this hook has on a condition that resolves itself.

So the handler retries **contention, and only contention** — matched on
SQLite's own `database is locked` text, since the exit code alone cannot
distinguish it from a real fault:

- at most `MAX_INGEST_ATTEMPTS` (2) attempts, so a lock that never clears
  cannot turn a loud failure into a hang;
- both attempts draw on **one** `INGEST_TIMEOUT_SECONDS` budget, so a retry
  cannot push the hook past the bound it declares in `hooks.json`;
- no attempt starts with less than `MIN_ATTEMPT_SECONDS` left, because a spawn
  killed on the way in says nothing the first failure did not;
- persistent contention is reported in **different words** from a real fault,
  so the two never blur together.

Every other failure is spawned once and reported. An unreadable transcript or a
bad flag reproduces identically on a second attempt; retrying it would only
delay the report.

Nothing is lost in either case. Ingest is incremental and idempotent, so the
next turn re-reads the file. No lock is taken in the handler — a lock file
would add a hang risk to solve a problem SQLite already solves.

### Do not add staleness checks here

Claude Code may still be writing a transcript when the hook fires, so a hook
ingesting a file mid-append is the *normal* case, not an edge case. Handling
that belongs in `ingest.py`, which is where it now lives. The handler passes
the path and nothing else; a size or mtime check here would be a second,
diverging copy of that logic.

## Validation

**Which manifest `claude plugin validate .` picks is decided by which files
exist**, and adding the catalog changed the answer. *Measured here* (2026-08-07,
Claude Code 2.1.223):

| command | what it validates | verdict | with `--strict` |
|---|---|---|---|
| `claude plugin validate .` | `.claude-plugin/marketplace.json`, plus a per-entry pass over the local-path plugin's `plugin.json` | `✔ Validation passed` | passes |
| `claude plugin validate .claude-plugin/plugin.json` | the plugin | `✔ Validation passed with warnings` (one, below) | **fails** |

The per-entry pass has real teeth on the manifest — renaming the plugin to
`Claude Piggy Bank` produced `plugins[0] plugin.json → name: Plugin name cannot
contain spaces` — but it stops there: a deliberately broken
`skills/cpb/SKILL.md` frontmatter, and a `source` pointing at a directory that
does not exist, both validated clean. Those two gaps are covered by
`tests/test_plugin_manifest.py` instead.

### The `CLAUDE.md` warning: accepted, with the reason

> `⚠ root: CLAUDE.md at the plugin root is not loaded as project context. To
> ship context with your plugin, use a skill (skills/<name>/SKILL.md) instead.`

The warning is accurate — the reference states plainly that "A `CLAUDE.md` file
at the plugin root is not loaded as project context" ([Plugin directory
structure](https://code.claude.com/docs/en/plugins-reference#plugin-directory-structure),
checked 2026-08-07) — and here it is **irrelevant**. CPB's `CLAUDE.md` is the
working ruleset for people editing this repository. It is not context to ship
*with* the plugin, and moving it would break the thing it is actually for:
Claude Code loads a repository's root `CLAUDE.md` for anyone working *on* CPB.
Migrating `/cpb` to `skills/` did not change this, and could not: the warning is
about `CLAUDE.md` existing, not about the absence of a skill.

So the decision is to **accept it**, and three facts make that cheap rather
than a shrug:

1. `claude plugin validate .` — the command the README and this document tell
   people to run — no longer surfaces it at all, because it now validates the
   marketplace. It passes with `--strict` too.
2. The warning is reachable only by naming the plugin manifest explicitly, and
   only there does `--strict` fail. **CI does not run `--strict` on that path**,
   and this paragraph is why.
3. The community review pipeline runs `claude plugin validate ./your-plugin`
   and "Warnings don't fail validation; add `--strict` to treat them as errors"
   ([Plugins § Submit your plugin to the community
   marketplace](https://code.claude.com/docs/en/plugins#submit-your-plugin-to-the-community-marketplace),
   checked 2026-08-07). So the warning does not block a submission either. That
   contradicts the assumption in
   [#73](https://github.com/vlad-ko/claude-piggy-bank/issues/73) that the
   pipeline's `--strict` behaviour made this urgent; the documentation is the
   authority and it says otherwise.

If a future release does start failing submissions on it, the fix is to move
`CLAUDE.md` out of the plugin root — which costs contributors their
automatically-loaded ruleset — and that trade should be made then, on evidence,
not pre-emptively now.
