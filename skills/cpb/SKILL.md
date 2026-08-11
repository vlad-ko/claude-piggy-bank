---
name: cpb
description: Open the Claude Piggy Bank report — where this project's context and token spend went
argument-hint: [port]
disable-model-invocation: true
---

# Open the Claude Piggy Bank report

Measure what has happened since the last ingest, then start the local report
server and give the user its URL. If Claude Piggy Bank has not measured the
transcripts already sitting on this machine, offer to — once, and only by
asking.

**Refresh before serving, every time.** The plugin's hooks fire on `Stop`,
`SubagentStop` and `SessionEnd`, so the turn the user is *in* when they ask for
the report has never been ingested — and if the hooks are not running at all,
nothing has. A refresh is incremental and keyed on size and mtime, so it
re-reads only what changed. That is step 3, and it is the reason the report
does not need a manual ingest.

**Do not tell the user how long it will take.** That figure depends on their
disk and their history, and this file has measured neither. Report what the
command printed, as it printed it.

**Always pass `--db`, in every step below.** `serve.py` does not read the
`CPB_DB` environment variable, so without the flag it opens its own default
database — which is not the one the plugin's hooks write to. The report would
come up empty or stale and look like a measured result. `ingest.py` does read
`CPB_DB`, but nothing sets it in this session — the hook sets it only for the
child it spawns — so without the flag *it* falls back to its own default too,
`${CLAUDE_PLUGIN_ROOT}/db/usage.db`, which the next plugin update deletes.
**No script in this file may be run without `--db`.**

If the user has set `CPB_DB` themselves, that is the database the hooks write
to, so pass that instead everywhere: `--db "$CPB_DB"`.

## 1. Check whether there is unmeasured history on disk

Run this. It reads file sizes, opens the database read-only, and ingests
nothing:

```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cpb_backfill_plan.py" --db "${CLAUDE_PLUGIN_DATA}/usage.db"
```

It prints a `verdict:` line. Act on that line and nothing else:

- **`PENDING`** — there are transcripts on disk this database has not
  measured. Go to step 2.
- **`UP_TO_DATE`**, **`NO_TRANSCRIPTS`**, **`UNKNOWN`** or **`WRONG_SCOPE`** —
  do not offer a backfill for this project. Go to step 3. If the user asks why
  the report is thin, repeat the verdict line as it was printed; do not
  reinterpret it and do not guess at a cause.

If the user has already answered the step 2 question once in this session, skip
to step 3 whatever the verdict says. Ask once.

## 2. Offer the backfill — ask, never assume

Say this much, plainly, and without alarm. **This is a new install, not a
fault.** Claude Piggy Bank only starts measuring when it is installed, so a
fresh database is empty by construction — but Claude Code has been writing
transcripts on this machine all along, and it deletes them after
`cleanupPeriodDays` (30 by default). The history on disk today is the most
there will ever be.

Show the size and the estimate **that the step 1 output printed**. Quote its
`transcript file(s)`, `on disk` and `estimate ~` figures as it printed them.
**Never state a size or a duration this project did not print** — not from this
file, not from memory, not from a machine you have read about.

Then ask, presenting exactly these three choices and no others:

1. **This project only** — ingest the transcripts already on disk for the
   project you are working in now.
2. **Every project on this machine** — ingest every project directory under
   `~/.claude/projects` into the same database.
3. **Not now** — open the report as it stands. Nothing is ingested.

Say that it is safe to interrupt: every file that lands is committed, and
re-running skips what is already in.

**Never pick choice 2 for the user, and never present it as the recommended or
default answer.** It reads the directory names of every project on the machine,
and those names are the user's own paths — their work, their clients, their
private repositories. Whether Claude Piggy Bank looks at them is theirs to
decide. Wait for an answer.

### If they choose 1 — this project only

```
python3 "${CLAUDE_PLUGIN_ROOT}/ingest.py" --db "${CLAUDE_PLUGIN_DATA}/usage.db"
```

### If they choose 2 — every project on this machine

First show what it would cover, because this is the scope whose size they have
not seen yet:

```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cpb_backfill_plan.py" --db "${CLAUDE_PLUGIN_DATA}/usage.db" --all-projects
```

Relay its figures — directories found, how many hold transcripts, how many hold
none, bytes, and the `estimate ~` line — and confirm the user still wants to go
ahead. Only then:

```
python3 "${CLAUDE_PLUGIN_ROOT}/ingest.py" --db "${CLAUDE_PLUGIN_DATA}/usage.db" --all-projects
```

This one can run for minutes. It prints its own progress in bytes. Let it run;
do not summarise it while it works.

### If they choose 3 — not now

Nothing is ingested. **Skip step 3 entirely** and go to step 4 — the refresh
runs the same command they just declined, so running it anyway would take the
answer back. Open the report as it stands.

"Not now" is a complete answer, not a deferral you should chase. Tell the user
how to come back to it, in these terms: run `/claude-piggy-bank:cpb` again and the offer reappears
while there are still unmeasured transcripts on disk, or run the ingest command
above directly at any time. Do not ask a second time in this session.

### When it finishes

Report what the command printed: files scanned, files ingested, and — for
choice 2 — **how many directories held no transcripts**. That is a real answer
about the machine and not an error; 12 of 19 directories on the machine measured
for this feature held none. Do not omit it and do not apologise for it.

If it was interrupted, say that what landed is kept and re-running resumes.

**Do not promise the report will now be useful.** A backfill gives the report
more to work with; it does not guarantee a verdict. Some figures need a minimum
sample before Claude Piggy Bank will state them at all, and on a small project
they may still read as not enough data — which is the tool refusing to invent a
number, working as intended. If the user asks what the backfill will tell them,
point them at the page rather than predicting what it will say.

## 3. Refresh the measurements

Run this, and then go to step 4 whatever it prints:

```
python3 "${CLAUDE_PLUGIN_ROOT}/ingest.py" --db "${CLAUDE_PLUGIN_DATA}/usage.db"
```

**Skip this step if an ingest already ran in step 2** — choice 1 and choice 2
are this same command with the same database, and running it twice would spend
the user's time on a file it has just read. Skip it if they chose 3, for the
reason given there.

Why it runs at all: the hooks fire when a turn or a session *ends*, so the turn
the user is in now has never been ingested, and on a machine where the hooks are
not running nothing has. It is incremental — it re-reads only files whose size
or mtime changed.

**A failed refresh does not stop the report.** If this command exits non-zero,
say so in one line, quote its last line of output, and go to step 4 anyway. A
stale report that says it is stale is better than no report: every figure the
database already holds is still a measured figure, and the page states its own
data age and staleness. Do not retry it, do not offer to fix it, and above all
do not describe the report as current — the page will say what it is.

## 4. Open the report

Run this in the background, so the session is not held by a long-lived server,
using port `$1` if the user supplied one and the default otherwise:

```
python3 "${CLAUDE_PLUGIN_ROOT}/serve.py" --db "${CLAUDE_PLUGIN_DATA}/usage.db"
```

With a port, add `--port $1`.

Report the URL it prints (`http://127.0.0.1:8377/` by default) and stop. Do not
summarise, interpret, or restate any figure from the report: every number in
Claude Piggy Bank is arithmetic over measured tokens, and a model's paraphrase
of it is not. The user reads the page.

## 5. If the report will not start

If `serve.py` exits because the database does not exist, say so plainly. The
database is created by the plugin's ingest hooks on the first `Stop`,
`SubagentStop` or `SessionEnd` after the plugin is enabled, by step 3's
refresh, or immediately by running:

```
python3 "${CLAUDE_PLUGIN_ROOT}/ingest.py" --db "${CLAUDE_PLUGIN_DATA}/usage.db"
```

**It must name the same database step 4 serves**, or the ingest succeeds, prints
a summary that looks like a result, and the report stays empty because the file
that was filled is not the file being read.

Reaching this step means step 3 found nothing to measure *and* nothing was
already there. If the report does open but says its hooks have never recorded a
run, that is a different fault with its own diagnosis on the page; relay what
the page says rather than restating it.

Do not invent a figure to fill the gap. If there is nothing measured yet, say
that there is nothing measured yet.
