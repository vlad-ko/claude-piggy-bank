#!/usr/bin/env python3
"""Ingest one transcript, on the event that produced it.

This is the only executable code Claude Piggy Bank's plugin ships. Claude Code
runs it on `Stop`, `SubagentStop` and `SessionEnd`, hands it the event JSON on
stdin, and it spawns `ingest.py --transcript <path>` for exactly the one file
that event names. It scans no directories and opens no database itself: the
`Stop` trigger fires on every assistant turn, so anything more than "read one
field, spawn one bounded child" would tax every turn of every session forever.

Standard library only, like the rest of the project.


Why the failure policy looks like this
--------------------------------------

Two of this project's rules point in opposite directions here, and the
resolution is deliberate rather than a compromise.

CPB is a *passive* measurement tool. It must never interrupt someone's work
because an ingest failed. But `CLAUDE.md` is equally firm that a failure must
never be swallowed into a benign default: a hook that quietly exits 0 after a
failed ingest leaves the report showing stale numbers that look complete, which
is the precise class of wrong-but-plausible figure this project exists to
catch.

Claude Code's exit-code contract is what makes both achievable at once
(https://code.claude.com/docs/en/hooks#exit-code-output, checked 2026-08-05):

- **Exit 2 is a blocking error.** On `Stop` it prevents Claude from stopping
  and continues the conversation; on `SubagentStop` it prevents the subagent
  from stopping. A measurement tool that returned 2 would hijack the session.
  This module therefore never returns 2 -- `EXIT_BLOCKING` exists only to be
  named in the guard at the bottom and asserted against in the tests.
- **Any other non-zero code is a non-blocking error.** The action proceeds and
  the transcript shows a `<hook name> hook error` notice followed by *the first
  line* of stderr. That is a loud, operator-visible message that costs the user
  nothing but a line of text -- exactly the channel the rule asks for. Hence
  `_one_line()`: a message that wraps loses everything after the newline.

The transcript notice is loud but ephemeral, so every failure is also appended
to `cpb-hook.log` in the plugin's persistent data directory, beside the
database. A failure that scrolled past is still on disk, and the report can
surface it later from a fixed, known location. Successes write nothing: `Stop`
fires every turn, and a success line per turn would grow without bound in a
directory that deliberately survives plugin updates.

Everything this module refuses to do is a case where it would otherwise have to
guess: a missing `agent_transcript_path`, a relative path, a database location
that is not durable. Each refusal names the thing that was missing.


Concurrency
-----------

Claude Code runs all matching hooks in parallel, and `Stop` and `SubagentStop`
can fire close together, so two ingests of *different* transcripts can overlap
on one SQLite file -- as can two Claude Code sessions in different projects
sharing one database. Correctness is never at risk: SQLite serialises writers
with a file lock, and `ingest.py` opens the DB through `sqlite3.connect()`,
whose `timeout` defaults to 5 seconds, so a second writer waits rather than
corrupting anything.

What is at risk is the *signal*. Past that 5 seconds `ingest.py` exits non-zero
with `database is locked`, which would reach the user as a `hook error` notice.
Given two triggers that routinely fire together, contention is likely rather
than theoretical, and a notice users see often enough to learn to ignore is
worse than no notice at all -- it spends the one loud channel this hook has on
a condition that resolves itself.

So contention, and *only* contention, is retried: at most `MAX_INGEST_ATTEMPTS`
attempts, sharing one `INGEST_TIMEOUT_SECONDS` budget, with a short backoff.
Both bounds matter in opposite directions. Retrying indefinitely would convert
a loud failure into a hang, which is the one outcome worse than a false alarm.
Retrying nothing would convert routine contention into a reported error. And
persistent contention is reported in different words from a real fault, so the
two never blur together -- with the note that nothing was lost either way,
because ingest is incremental and idempotent and the next turn re-reads the
file.

Every other failure is spawned once and reported. An unreadable transcript or
a bad flag would reproduce identically on a second attempt; retrying it would
only delay the report.

No lock is taken here. A lock file would add a hang risk -- the thing `timeout`
exists to prevent -- to solve a problem SQLite already solves.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

HOOK_NAME = "cpb-ingest-hook"

EXIT_OK = 0
#: Reported failure. Claude Code shows a non-blocking `hook error` notice and
#: the session continues untouched.
EXIT_NONBLOCKING_ERROR = 1
#: Never returned. On `Stop` and `SubagentStop` this blocks the session; the
#: constant is here so the guard below and the tests can name it.
EXIT_BLOCKING = 2

#: `SubagentStop` carries two paths: `transcript_path` is the *parent
#: session's* transcript and `agent_transcript_path` is the subagent's own.
#: Reading the wrong one would re-ingest the main thread, report success, and
#: never capture the subagent -- whose transcript is the one that gets reaped.
#: (https://code.claude.com/docs/en/hooks#subagentstop, checked 2026-08-05)
TRANSCRIPT_FIELD_BY_EVENT = {"SubagentStop": "agent_transcript_path"}
DEFAULT_TRANSCRIPT_FIELD = "transcript_path"

#: Ceiling on the spawned ingest, shared across all attempts. Well above a
#: normal incremental run and well below anything a user would call a hang.
INGEST_TIMEOUT_SECONDS = 20

#: Lock contention is the one failure worth retrying -- see "Concurrency".
MAX_INGEST_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 0.5
#: Do not start an attempt that has less than this much budget left; a spawn
#: killed on the way in tells us nothing that the first failure did not.
MIN_ATTEMPT_SECONDS = 3.0
#: SQLite's own words for "another writer holds the lock". Matched on text
#: because the exit code cannot distinguish contention from a real fault.
CONTENTION_MARKERS = ("database is locked", "database table is locked")

DB_FILENAME = "usage.db"
LOG_FILENAME = "cpb-hook.log"
LOG_MAX_BYTES = 256 * 1024

#: The plugin root is this file's parent's parent: the repository is the
#: plugin. Derived from `__file__` rather than `${CLAUDE_PLUGIN_ROOT}` because
#: it is correct even when the handler is run straight from a checkout.
INGEST_SCRIPT = Path(__file__).resolve().parent.parent / "ingest.py"


class Refusal(Exception):
    """A condition under which the hook must not guess.

    Carries the one-line message an operator sees. Raising this is how every
    "I cannot produce a trustworthy answer" path exits.
    """


def _one_line(text: str) -> str:
    """Collapse to a single line: only the first line reaches the transcript."""
    return " ".join(str(text).split())


def data_dir(env: Mapping[str, str]) -> Optional[Path]:
    """The plugin's persistent directory, or `None` outside a plugin install.

    `${CLAUDE_PLUGIN_DATA}` survives plugin updates; `${CLAUDE_PLUGIN_ROOT}`
    does not. Claude Code exports both to hook processes.
    """
    raw = (env.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if not raw:
        return None
    return Path(os.path.expanduser(raw))


class Reporter:
    """Announces a failure on stderr and records it where it will outlive the run."""

    def __init__(self, stderr, log_path: Optional[Path]):
        self._stderr = stderr
        self._log_path = log_path

    def report(self, message: str) -> None:
        line = f"{HOOK_NAME}: {_one_line(message)}"
        print(line, file=self._stderr)
        self._append(line)

    def _append(self, line: str) -> None:
        if self._log_path is None:
            return
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate()
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} {line}\n")
        except OSError:
            # The log is a durability courtesy; stderr has already carried the
            # message. Escalating a logging failure into a second failure would
            # obscure the first one, which is the one that matters.
            pass

    def _rotate(self) -> None:
        """Keep one previous log. Unbounded growth in a persistent dir is a bug."""
        try:
            size = self._log_path.stat().st_size
        except OSError:
            return
        if size > LOG_MAX_BYTES:
            os.replace(self._log_path, self._log_path.with_name(LOG_FILENAME + ".1"))


def read_payload(stdin) -> dict:
    """Parse the event JSON Claude Code writes to the hook's stdin."""
    raw = stdin.read()
    if not raw or not raw.strip():
        raise Refusal("no hook payload on stdin; nothing was ingested")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise Refusal(f"could not parse the hook payload on stdin: {exc}") from exc
    if not isinstance(payload, dict):
        raise Refusal(
            f"the hook payload on stdin is a {type(payload).__name__}, not an object"
        )
    return payload


def transcript_field_for(event: Optional[str]) -> str:
    return TRANSCRIPT_FIELD_BY_EVENT.get(event or "", DEFAULT_TRANSCRIPT_FIELD)


def resolve_transcript(payload: Mapping[str, Any], event: Optional[str]) -> Path:
    """Pick and validate the transcript this event names. Never guesses one.

    Deliberately does *not* require the path to sit under `~/.claude/projects`:
    `CLAUDE_CONFIG_DIR` legitimately relocates that store, and a hard prefix
    check would refuse a valid setup and record zero spend for it. There is no
    injection risk in accepting the path -- the child is spawned from an
    argument list with no shell.
    """
    field = transcript_field_for(event)
    raw = payload.get(field)
    if raw is None:
        raise Refusal(
            f"{event or 'hook'} payload has no {field}; the transcript it names"
            " is unmeasured, not empty"
        )
    if not isinstance(raw, str) or not raw.strip():
        raise Refusal(f"{event or 'hook'} payload has an unusable {field}: {raw!r}")

    path = Path(os.path.expanduser(raw.strip()))
    if not path.is_absolute():
        raise Refusal(
            f"{field} {str(path)!r} is not absolute; hooks run in the user's"
            " project directory, so resolving it there would be a guess"
        )
    if path.suffix != ".jsonl":
        raise Refusal(f"{field} {str(path)!r} is not a .jsonl transcript")
    if not path.is_file():
        raise Refusal(
            f"{field} {str(path)!r} does not exist; its spend is unmeasured, not zero"
        )
    return path


def resolve_db_override(env: Mapping[str, str]) -> Optional[Path]:
    """The database to hand `ingest.py`, or `None` to leave its default alone.

    `None` covers two cases that both mean "inject nothing": the user has
    already set `CPB_DB`, and we are not running inside an installed plugin at
    all (a plain checkout, where `db/usage.db` beside `ingest.py` is both
    correct and durable).
    """
    if (env.get("CPB_DB") or "").strip():
        return None

    directory = data_dir(env)
    if directory is not None:
        return directory / DB_FILENAME

    if (env.get("CLAUDE_PLUGIN_ROOT") or "").strip():
        # Installed as a plugin but with no persistent directory. Falling back
        # to `db/usage.db` inside the plugin root would put the database in a
        # tree Claude Code replaces on the next plugin update -- and past
        # Claude Code's ~30-day transcript reap that database is the only
        # surviving copy of the history. Refuse rather than destroy it later.
        raise Refusal(
            "CLAUDE_PLUGIN_DATA is not set, so there is no durable place for the"
            " database; refusing to write inside CLAUDE_PLUGIN_ROOT, which is"
            " replaced on the next plugin update"
        )
    return None


def build_argv(transcript: Path) -> list[str]:
    """The single documented invocation: one file, ingested incrementally.

    The database is passed through the environment rather than `--db` so there
    is exactly one mechanism deciding it, and a user who exports `CPB_DB`
    themselves is not silently overridden by a flag they cannot see.
    """
    if not INGEST_SCRIPT.is_file():
        raise Refusal(f"{INGEST_SCRIPT} is missing from the plugin; nothing was ingested")
    return [sys.executable, str(INGEST_SCRIPT), "--transcript", str(transcript)]


def build_env(env: Mapping[str, str], db: Optional[Path]) -> dict:
    """The child's environment, with `CPB_DB` either set correctly or absent.

    `ingest.py` treats an *empty* `CPB_DB` as an error rather than falling back
    to its default, deliberately, so a misconfigured wrapper cannot quietly
    write to a database nobody reads. That makes an inherited empty value a
    hazard rather than a no-op: passing it through would turn a stray variable
    in the user's shell into a failure on every single turn, reported against a
    cause that has nothing to do with CPB. So it is removed, not forwarded.
    """
    child = dict(env)
    if db is not None:
        child["CPB_DB"] = str(db)
    elif "CPB_DB" in child and not (child.get("CPB_DB") or "").strip():
        del child["CPB_DB"]
    return child


def is_contention(stderr: Optional[str]) -> bool:
    """Did this failure look like another writer holding the SQLite lock?"""
    text = (stderr or "").lower()
    return any(marker in text for marker in CONTENTION_MARKERS)


def spawn(argv: Iterable[str], *, env: Mapping[str, str], timeout: float):
    """Default runner. Injected in tests so no test spawns a real process."""
    return subprocess.run(
        list(argv),
        env=dict(env),
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def main(
    *,
    stdin=None,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Callable] = None,
    stderr=None,
    sleeper: Optional[Callable] = None,
) -> int:
    """Returns `EXIT_OK` or `EXIT_NONBLOCKING_ERROR`. Never `EXIT_BLOCKING`."""
    stdin = sys.stdin if stdin is None else stdin
    env = os.environ if env is None else env
    runner = spawn if runner is None else runner
    stderr = sys.stderr if stderr is None else stderr
    sleeper = time.sleep if sleeper is None else sleeper

    directory = data_dir(env)
    reporter = Reporter(stderr, None if directory is None else directory / LOG_FILENAME)

    try:
        payload = read_payload(stdin)
        event = payload.get("hook_event_name")
        transcript = resolve_transcript(payload, event if isinstance(event, str) else None)
        db = resolve_db_override(env)
        if db is not None:
            try:
                db.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise Refusal(
                    f"cannot create the database directory {db.parent}: {exc}"
                ) from exc
        argv = build_argv(transcript)
    except Refusal as refusal:
        reporter.report(str(refusal))
        return EXIT_NONBLOCKING_ERROR

    child_env = build_env(env, db)
    deadline = time.monotonic() + INGEST_TIMEOUT_SECONDS
    attempt = 0

    while True:
        attempt += 1
        remaining = deadline - time.monotonic()
        try:
            completed = runner(argv, env=child_env, timeout=remaining)
        except subprocess.TimeoutExpired:
            reporter.report(
                f"ingest of {transcript} timed out after {INGEST_TIMEOUT_SECONDS}s and"
                " was killed; those measurements are missing, not zero"
            )
            return EXIT_NONBLOCKING_ERROR
        except OSError as exc:
            reporter.report(f"could not run ingest for {transcript}: {exc}")
            return EXIT_NONBLOCKING_ERROR

        if completed.returncode == 0:
            return EXIT_OK

        # Contention is the only retryable failure. Everything else -- an
        # unreadable transcript, a bad flag, a corrupt DB -- is a real fault
        # that a second attempt would reproduce, so retrying it would only
        # delay the report and double the cost of being wrong.
        contended = is_contention(completed.stderr)
        budget_left = deadline - time.monotonic() - RETRY_BACKOFF_SECONDS
        if contended and attempt < MAX_INGEST_ATTEMPTS and budget_left >= MIN_ATTEMPT_SECONDS:
            sleeper(RETRY_BACKOFF_SECONDS)
            continue
        break

    if contended:
        # Distinguishable on purpose: this says "we waited and it did not
        # clear", not "ingest is broken". Nothing was lost either way -- ingest
        # is incremental and idempotent, so the next Stop re-reads the file.
        reporter.report(
            f"ingest of {transcript} hit SQLite write contention on {attempt} of"
            f" {MAX_INGEST_ATTEMPTS} attempts and did not clear; the next turn will"
            f" re-ingest it: {completed.stderr or '(no stderr)'}"
        )
    else:
        reporter.report(
            f"ingest of {transcript} exited {completed.returncode}; those measurements"
            f" are missing, not zero: {completed.stderr or '(no stderr)'}"
        )
    return EXIT_NONBLOCKING_ERROR


if __name__ == "__main__":
    # The clamp is a guard, not decoration: exit 2 would block the session on
    # both Stop and SubagentStop, so no future edit to main() can leak one.
    sys.exit(EXIT_OK if main() == EXIT_OK else EXIT_NONBLOCKING_ERROR)
