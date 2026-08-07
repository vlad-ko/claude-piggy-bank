---
name: cpb
description: Open the Claude Piggy Bank report — where this project's context and token spend went
argument-hint: [port]
disable-model-invocation: true
---

# Open the Claude Piggy Bank report

Start the local report server and give the user its URL. If Claude Piggy Bank
has not measured the transcripts already sitting on this machine, offer to —
once, and only by asking.

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

Nothing is ingested. Go to step 3 and open the report.

"Not now" is a complete answer, not a deferral you should chase. Tell the user
how to come back to it, in these terms: run `/cpb` again and the offer reappears
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

## 3. Open the report

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

## 4. If the report will not start

If `serve.py` exits because the database does not exist, say so plainly. The
database is created by the plugin's ingest hooks on the first `Stop`,
`SubagentStop` or `SessionEnd` after the plugin is enabled — or immediately by
running:

```
python3 "${CLAUDE_PLUGIN_ROOT}/ingest.py" --db "${CLAUDE_PLUGIN_DATA}/usage.db"
```

**It must name the same database step 3 serves**, or the ingest succeeds, prints
a summary that looks like a result, and the report stays empty because the file
that was filled is not the file being read.

Do not invent a figure to fill the gap. If there is nothing measured yet, say
that there is nothing measured yet.
