# Where the data comes from

What CPB reads, what it refuses to read, how it detects the format moving under
it, and why the database it builds is not regenerable.

- [The two globs, and the third path that is not a source](#the-two-globs-and-the-third-path-that-is-not-a-source)
- [The format CPB reads is internal, and Anthropic says so](#the-format-cpb-reads-is-internal-and-anthropic-says-so)
- [Counting the shape it saw](#counting-the-shape-it-saw)
- [Transcripts expire — back up the database](#transcripts-expire--back-up-the-database)

## The two globs, and the third path that is not a source

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
its four token classes: input, cache-read, cache-write, and output. One API
call is one `message.id`, not one record — the correction that established that
is in [`corrections.md`](corrections.md).

The cache-write class is **stored three ways**, because one number cannot answer
"did this write pay for itself?". The flat total is kept exactly as the API
reports it, and beside it the per-TTL split from `usage.cache_creation` — a
5-minute write costs 1.25x base input tokens and is repaid by its *first* read,
a 1-hour write costs 2x and needs its *second*
([TA-8](claude-api-token-accounting.md#ta-8)). Both columns are **nullable and
mean unmeasured**: a call recorded before CPB read that field, or one whose
record carried only one of the two TTLs, has no split, and the repayment figure
ranges over the calls that do — taking its reads from those same calls, so the
two sides of the ratio cover one set. Until you re-ingest, that figure reads
*not measured* rather than reading zero.

## The format CPB reads is internal, and Anthropic says so

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
is written as many records sharing one `message.id`
([`corrections.md`](corrections.md)).

## Counting the shape it saw

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

## Transcripts expire — back up the database

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

This is also why the plugin's uninstall takes `--keep-data`
([`install.md`](install.md#uninstall)): removing the plugin from its last scope
otherwise deletes the data directory the database lives in.
