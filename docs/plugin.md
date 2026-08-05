# The Claude Code plugin

CPB ships as a Claude Code plugin so that ingest happens **when the transcript
is written**, rather than whenever someone remembers to run `ingest.py`. The
plugin is the repository: `.claude-plugin/plugin.json` sits at the repository
root and the hooks invoke the same `ingest.py` and `serve.py` a manual user
runs. There is no second code path and no packaging step — adding one would
mean adding a dependency manifest, which the `stdlib-only` CI job fails on by
design.

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
| `hooks/hooks.json` | the three ingest triggers |
| `hooks/cpb_ingest_hook.py` | the handler the triggers run |
| `commands/cpb.md` | the `/cpb` command that opens the report |

Only `plugin.json` belongs inside `.claude-plugin/`; `hooks/` and `commands/`
must be at the plugin root or they are silently never loaded. (Documented:
[Plugins reference § Plugin structure
overview](https://code.claude.com/docs/en/plugins-reference#plugin-directory-structure),
checked 2026-08-05. `tests/test_plugin_manifest.py` asserts it, because "silently
never loaded" is not a failure anyone notices.)

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

`claude plugin validate .` passes. It reports one warning, which is expected and
will not be fixed:

> `root: CLAUDE.md at the plugin root is not loaded as project context.`

CPB's `CLAUDE.md` is the working ruleset for people editing this repository, not
plugin context. It is not meant to load into a plugin user's session, so
`--strict` is not used in CI for this check.
