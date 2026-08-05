---
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

   **Always pass `--db`.** Unlike `ingest.py`, `serve.py` does not read the
   `CPB_DB` environment variable, so without the flag it opens its own default
   database — which is not the one the plugin's hooks write to. The report
   would come up empty or stale and look like a measured result.

   If the user has set `CPB_DB` themselves, that is the database the hooks
   write to, so pass that instead: `--db "$CPB_DB"`.

2. Report the URL it prints (`http://127.0.0.1:8377/` by default) and stop.
   Do not summarise, interpret, or restate any figure from the report: every
   number in Claude Piggy Bank is arithmetic over measured tokens, and a
   model's paraphrase of it is not. The user reads the page.

3. If `serve.py` exits because the database does not exist, say so plainly and
   tell the user the database is created by the plugin's ingest hooks on the
   first `Stop`, `SubagentStop` or `SessionEnd` after the plugin is enabled —
   or immediately by running `python3 "${CLAUDE_PLUGIN_ROOT}/ingest.py"`. Do
   not invent a figure to fill the gap.
