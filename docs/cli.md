# The command line

Everything CPB does from a terminal. Installing it is
[`install.md`](install.md); what the report then shows you is
[`metrics.md`](metrics.md).

- [The `cpb` command](#the-cpb-command)
- [Ingesting](#ingesting)
- [Ingesting a single transcript](#ingesting-a-single-transcript)
- [Where the database lives](#where-the-database-lives)

## The `cpb` command

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
you say which build produced a figure, and the authoritative answer to "which
release am I running". That is a different thing from the database's schema
version, which describes the shape of the file and says nothing about the code
that filled it. What the number promises is in
[`versioning.md`](versioning.md); what each release changed is in
[`releases.md`](releases.md).

`cpb.py serve` and the page it serves involve no model at all, whether you
reach them from a checkout or through the plugin.

## Ingesting

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
| `--prune-missing` | **DELETE** the rows for sources no longer on disk. Off by default, and it asks before deleting; see [Transcripts expire](data-sources.md#transcripts-expire--back-up-the-database) and the 2.0.0 entry in [`releases.md`](releases.md) |

## Ingesting a single transcript

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

## Where the database lives

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
`${CLAUDE_PLUGIN_DATA}/usage.db`, the file the hooks write and the skill serves,
and says so; where that directory cannot be known it **refuses** and names the
path and the flag. A silent fall-back to a path an update deletes is the one
option ruled out. Steps 1 and 2 are unaffected — a run that names its database
never consults the default — and a plain clone is unaffected entirely.

`serve` does **not** read `CPB_DB`; point it at the same file explicitly:

```bash
python3 cpb.py serve --db "$CPB_DB"
```
