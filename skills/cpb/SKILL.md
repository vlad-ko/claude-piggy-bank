---
name: cpb
description: Open the Claude Piggy Bank report — where this project's context and token spend went
argument-hint: [port]
disable-model-invocation: true
---

# Open the Claude Piggy Bank report

Start the local report server and give the user its URL.

1. Run this in the background, so the session is not held by a long-lived
   server, using port `$1` if the user supplied one and the default otherwise:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/serve.py" --db "${CLAUDE_PLUGIN_DATA}/usage.db"
   ```

   With a port, add `--port $1`.

   **Always pass `--db`, here and in step 3.** `serve.py` does not read the
   `CPB_DB` environment variable, so without the flag it opens its own default
   database — which is not the one the plugin's hooks write to. The report
   would come up empty or stale and look like a measured result. `ingest.py`
   does read `CPB_DB`, but nothing sets it in this session — the hook sets it
   only for the child it spawns — so without the flag *it* falls back to its
   own default too, `${CLAUDE_PLUGIN_ROOT}/db/usage.db`, which the next plugin
   update deletes. Neither script may be run here without `--db`.

   If the user has set `CPB_DB` themselves, that is the database the hooks
   write to, so pass that instead: `--db "$CPB_DB"`.

2. Report the URL it prints (`http://127.0.0.1:8377/` by default) and stop.
   Do not summarise, interpret, or restate any figure from the report: every
   number in Claude Piggy Bank is arithmetic over measured tokens, and a
   model's paraphrase of it is not. The user reads the page.

3. If `serve.py` exits because the database does not exist, say so plainly and
   tell the user the database is created by the plugin's ingest hooks on the
   first `Stop`, `SubagentStop` or `SessionEnd` after the plugin is enabled —
   or immediately by running:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/ingest.py" --db "${CLAUDE_PLUGIN_DATA}/usage.db"
   ```

   **It must name the same database step 1 serves**, or the ingest succeeds,
   prints a summary that looks like a result, and the report stays empty
   because the file that was filled is not the file being read. Use
   `--db "$CPB_DB"` here too if the user set it.

   Do not invent a figure to fill the gap.
