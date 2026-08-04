"""Ingest Claude Code session transcripts (JSONL) into SQLite (#4948, #4966).

Usage:
    python3 ingest.py [--projects-dir <dir>] [--db db/usage.db]

`--projects-dir` defaults to this project's own transcript directory, derived
from the working directory via Claude Code's naming convention -- see
`default_projects_dir()`.

TWO transcript sources per session (#4966) -- a session's API calls are NOT
all in one file:

    <projects-dir>/<session-id>.jsonl                      main thread
    <projects-dir>/<session-id>/subagents/agent-<id>.jsonl one per subagent

Every record in a `subagents/` transcript carries `isSidechain: true` and a
`sessionId`, so subagent calls are stored under a session (never under the
`agent-<id>` filename, which is not a session) and tagged
`source_kind = 'subagent'`. Ingesting only the top-level glob -- what #4948
did -- left `is_sidechain` 0 on every row and made every reported figure
main-thread-only while it read as a session total.

Plus a third path that is an INDEX, not a source:

    <tasks-dir>/<session-id>/tasks/<agent-id>.output

Measured on this host 2026-08-02: 2834 of these are SYMLINKS into the
canonical `subagents/` store with byte-identical content, 238 are regular
files carrying no `usage` records at all (background bash output shares the
directory), and 211 are DANGLING -- their target session directory is gone.
So the task directory is read as an index and never ingested as data; doing
otherwise would double every subagent figure. What it uniquely supplies:

  * DISPATCH attribution -- which session actually launched an agent. The
    storing directory is just whichever session was live when the file was
    written and differs in 749 of 2834 real cases (session resumption), so
    spend follows the DISPATCHER when the index knows it.
  * REAPED-transcript detection -- a dangling entry proves a dispatch
    happened whose transcript is gone. That session's subagent figure is
    UNAVAILABLE, which is not the same fact as zero and must not render
    like it.

Idempotent + incremental: `ingest_state` records (path, size, mtime) per
SOURCE FILE; unchanged files are skipped; a changed file has ITS OWN rows
deleted and re-parsed. The delete scope is the FILE (`source_path`), not the
session -- two sources share one session_id, so a session-scoped delete would
wipe the main thread's rows every time a subagent transcript grew.

Rule #12 (absence is not a value): any line that fails json.loads, or any
assistant/user record whose shape defeats a parser expectation, increments the
per-file `unparsed_records` counter -- it is stored, printed, and surfaced in
the UI. A parse failure NEVER silently becomes a zero. Likewise a session with
NO `subagents/` tree on disk is "subagent spend not measured", which the
summary reports separately from a measured zero.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pricing import cost_usd

SOURCE_MAIN = "main"
SOURCE_SUBAGENT = "subagent"

STATUS_INGESTED = "ingested"
STATUS_UNAVAILABLE = "unavailable"

AGENT_FILE_PREFIX = "agent-"

# EVERY table this tool creates. ONE list, used by both the probe and the drop
# loop in `_prepare_schema` -- two lists is how `subagent_runs` and
# `task_index_sessions` were omitted from the rebuild in the first place.
DERIVED_TABLES = (
    "api_calls",
    "turns",
    "agent_dispatches",
    "sessions",
    "ingest_state",
    "subagent_runs",
    "task_index_sessions",
)

# Bumped whenever the shape below changes. The DB is a pure DERIVED rendering
# of the transcripts (regenerable in full by re-running this script), so an
# older shape is rebuilt from scratch rather than migrated in place -- see
# `_prepare_schema`.
SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    path TEXT,
    first_ts REAL,
    last_ts REAL,
    size_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    ts REAL,
    turn_type TEXT NOT NULL,
    preview TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    agent_id TEXT,
    turn_id INTEGER,
    ts REAL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    cache_read INTEGER NOT NULL,
    cache_write INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    context_size INTEGER NOT NULL,
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL
);
CREATE TABLE IF NOT EXISTS agent_dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    turn_id INTEGER,
    task_id TEXT,
    agent_type TEXT,
    description TEXT,
    subagent_tokens INTEGER
);
-- One row per subagent DISPATCH (#4966). `status` is the load-bearing column:
-- 'unavailable' means the task index proves the dispatch happened but its
-- transcript is gone from /private/tmp, so that session's subagent spend is
-- UNMEASURED -- never to be rendered as a zero.
CREATE TABLE IF NOT EXISTS subagent_runs (
    agent_id TEXT PRIMARY KEY,
    session_id TEXT,
    dispatching_session_id TEXT,
    storing_session_id TEXT,
    agent_type TEXT,
    description TEXT,
    tool_use_id TEXT,
    spawn_depth INTEGER,
    status TEXT NOT NULL,
    source_path TEXT,
    -- Epoch seconds from the task-index ENTRY's own mtime; NULL when it could
    -- not be read. It is the only timestamp a reaped run has, and it is what
    -- lets a window-scoped view ask whether THIS window is missing spend.
    dispatched_at REAL
);
-- Sessions whose tasks/ directory was actually scanned. Distinguishes
-- "scanned, dispatched nothing" (a real zero) from "never scanned" (unknown).
CREATE TABLE IF NOT EXISTS task_index_sessions (
    session_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS ingest_state (
    path TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    unparsed_records INTEGER NOT NULL,
    first_ts REAL,
    last_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls (ts);
CREATE INDEX IF NOT EXISTS idx_api_calls_session ON api_calls (session_id);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns (session_id);
-- #4955 code review: Api.sessions() now joins agent_dispatches -> turns and
-- filters on turns.ts to respect the from/to window. Without these, that
-- join+range predicate has no supporting index and degrades to a scan as
-- the DB grows.
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns (ts);
CREATE INDEX IF NOT EXISTS idx_agent_dispatches_turn_id ON agent_dispatches (turn_id);
-- #4966: the per-file delete scope and the main-vs-subagent split are both
-- hot paths now (one subagent transcript changing must not scan every row).
CREATE INDEX IF NOT EXISTS idx_api_calls_source_path ON api_calls (source_path);
CREATE INDEX IF NOT EXISTS idx_api_calls_source_kind ON api_calls (source_kind);
CREATE INDEX IF NOT EXISTS idx_turns_source_path ON turns (source_path);
CREATE INDEX IF NOT EXISTS idx_agent_dispatches_source_path
    ON agent_dispatches (source_path);
CREATE INDEX IF NOT EXISTS idx_api_calls_agent_id ON api_calls (agent_id);
CREATE INDEX IF NOT EXISTS idx_subagent_runs_dispatching
    ON subagent_runs (dispatching_session_id);
CREATE INDEX IF NOT EXISTS idx_subagent_runs_status ON subagent_runs (status);
"""

TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
TOOL_USE_ID_RE = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
SUBAGENT_TOKENS_RE = re.compile(r"<subagent_tokens>(\d+)</subagent_tokens>")
CRON_TICK_RE = re.compile(r"PR-cycle|cron|recurring tick", re.IGNORECASE)


@dataclass(frozen=True)
class Source:
    """One transcript FILE and the session its API calls are charged to.

    For a subagent transcript `session_id` is the DISPATCHING session when the
    task index knows it, else the storing directory -- never the `agent-<id>`
    filename, which is not a session.
    """

    path: Path
    session_id: str
    kind: str
    agent_id: Optional[str] = None
    storing_session_id: Optional[str] = None


@dataclass
class SubagentRun:
    """One subagent dispatch: its metadata and whether we could read it."""

    agent_id: str
    session_id: Optional[str]
    dispatching_session_id: Optional[str]
    storing_session_id: Optional[str]
    agent_type: Optional[str]
    description: Optional[str]
    tool_use_id: Optional[str]
    spawn_depth: Optional[int]
    status: str
    source_path: Optional[str]
    # Only ever set for an UNAVAILABLE run (see TaskIndex.dispatched_at). An
    # ingested run needs no proxy: its own api_calls carry real timestamps.
    dispatched_at: Optional[float] = None


@dataclass
class TaskIndex:
    """The harness task directory read as an INDEX (never as a data source).

    `available` is False when no task directory exists on this host -- in
    which case dispatch attribution falls back to the storing directory and
    reaped transcripts are simply unknown rather than invented.
    """

    available: bool = False
    # resolved canonical transcript path -> dispatching session id
    dispatcher_by_path: dict[Path, str] = field(default_factory=dict)
    # dispatches whose transcript is gone: agent_id -> dispatching session
    unavailable: dict[str, str] = field(default_factory=dict)
    # agent_id -> mtime of the task-INDEX ENTRY (epoch seconds). This is the
    # ONLY timestamp a reaped run has: its transcript is gone, so it owns no
    # api_calls row and no turn. It dates the DISPATCH (when the harness wrote
    # the index entry), not the agent's own work, and is labelled that way
    # wherever it surfaces. Without it, "was this gap in the selected window?"
    # is unanswerable and the status has to over-report on every window.
    dispatched_at: dict[str, float] = field(default_factory=dict)
    scanned_sessions: set = field(default_factory=set)
    # regular-file task outputs NOT reachable from the canonical store
    extra_sources: list = field(default_factory=list)


@dataclass
class Turn:
    ts: Optional[float]
    turn_type: str
    preview: str


@dataclass
class ApiCall:
    turn_index: Optional[int]
    ts: Optional[float]
    model: str
    input_tokens: int
    cache_read: int
    cache_write: int
    output_tokens: int
    is_sidechain: bool

    @property
    def context_size(self) -> int:
        return self.input_tokens + self.cache_write + self.cache_read


@dataclass
class Dispatch:
    turn_index: Optional[int]
    task_id: Optional[str]
    agent_type: Optional[str]
    description: Optional[str]
    subagent_tokens: Optional[int]


@dataclass
class ParseResult:
    turns: list[Turn] = field(default_factory=list)
    calls: list[ApiCall] = field(default_factory=list)
    dispatches: dict[str, Dispatch] = field(default_factory=dict)
    unparsed_records: int = 0
    records_parsed: int = 0
    # Per-failure diagnostics ("<file>:<line>: <reason>"), one per unparsed
    # record -- the INCONCLUSIVE count is otherwise unactionable (Qodo finding).
    unparsed_details: list[str] = field(default_factory=list)


def parse_ts(record: dict) -> Optional[float]:
    """Epoch seconds from a record's ISO8601 `timestamp`, or None."""
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def classify_turn(text: str) -> str:
    """Classify a turn from the first ~600 chars of its text."""
    head = text[:600]
    if "<task-notification>" in head:
        return "task-notification"
    if "<system-reminder>" in head:
        return "system-reminder"
    if "<local-command" in head:
        return "local-command"
    if "wakeup" in head.lower():
        return "wakeup"
    if CRON_TICK_RE.search(head):
        return "cron-tick"
    return "human"


def user_text(content: Any) -> Optional[str]:
    """Text of a user record, or None when it is a tool round-trip (no turn).

    A content LIST containing only tool_result blocks is a tool round-trip.
    Raises ValueError when the shape defeats expectations (counts as unparsed).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if not isinstance(block, dict):
                raise ValueError("non-dict content block")
            if block.get("type") == "text":
                texts.append(str(block.get("text", "")))
        return "\n".join(texts) if texts else None
    raise ValueError("user content is neither string nor list")


def default_tasks_dir(projects_dir: Path) -> Path:
    """Where the harness keeps this project's per-session task directories."""
    return Path(f"/private/tmp/claude-{os.getuid()}") / projects_dir.name


def agent_id_from_path(path: Path) -> str:
    """Bare agent id, so the two layouts join.

    The canonical store names files `agent-<id>.jsonl`; the task index names
    the same agent `<id>.output`. Stripping the prefix is what makes them the
    same key -- without it the two sides look like disjoint agent sets.
    """
    stem = path.stem
    return stem[len(AGENT_FILE_PREFIX):] if stem.startswith(AGENT_FILE_PREFIX) else stem


def read_agent_meta(transcript: Path) -> dict[str, Any]:
    """Agent metadata from the `.meta.json` sidecar, or {} when absent.

    Absent metadata yields NULL agent_type/description -- never a guessed
    label. A subagent whose type we cannot name still has its SPEND counted;
    only the name is missing (rule #12).
    """
    sidecar = transcript.with_name(transcript.stem + ".meta.json")
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_dispatch_time(index: "TaskIndex", agent_id: str, entry: Path) -> None:
    """Stamp a reaped run with its index entry's own mtime.

    `lstat`, never `stat`: the symlink is DANGLING, so `stat` follows it to a
    file that is gone and raises. `lstat` reads the link itself, which is the
    artifact whose creation time we actually want.

    A failed read leaves the agent OUT of `dispatched_at` rather than
    defaulting to now or to zero (rule #12) -- `serve.py` then reports that run
    as undatable instead of placing it in whatever window happens to be open.
    """
    try:
        index.dispatched_at[agent_id] = entry.lstat().st_mtime
    except OSError:
        pass


def discover_task_index(tasks_dir: Path) -> TaskIndex:
    """Read `<tasks-dir>/<session>/tasks/*.output` as an index.

    Classifies each entry: a live symlink gives dispatch attribution; a
    dangling symlink is a reaped transcript (UNAVAILABLE); a regular file is
    only a candidate data source, and only if the canonical store does not
    already contain it (checked by the caller).
    """
    index = TaskIndex()
    if not tasks_dir.is_dir():
        return index
    index.available = True
    for session_dir in sorted(tasks_dir.iterdir()):
        tasks = session_dir / "tasks"
        if not tasks.is_dir():
            continue
        index.scanned_sessions.add(session_dir.name)
        for entry in sorted(tasks.iterdir()):
            agent_id = agent_id_from_path(entry)
            if entry.is_symlink():
                if entry.exists():
                    try:
                        index.dispatcher_by_path[entry.resolve()] = session_dir.name
                    except OSError:
                        index.unavailable[agent_id] = session_dir.name
                        _record_dispatch_time(index, agent_id, entry)
                else:
                    index.unavailable[agent_id] = session_dir.name
                    _record_dispatch_time(index, agent_id, entry)
            elif entry.is_file():
                index.extra_sources.append((entry, session_dir.name, agent_id))
    return index


# `carries_api_calls` outcomes. THREE, not two: "we looked at the whole file
# and there is no usage record" is a measurement, while "we stopped early" and
# "we could not open it" are not. Collapsing the latter two into False drops
# the file from `sources` and its spend then reads as absent -- the exact
# absence-as-value shape (rule #12) this PR exists to remove, re-entering
# through the discovery step.
CARRIES_YES = "yes"
CARRIES_NO = "no"
CARRIES_TRUNCATED = "truncated"
CARRIES_UNREADABLE = "unreadable"


def carries_api_calls(path: Path, max_lines: int = 2000) -> str:
    """Does this file contain at least one `message.usage` record?

    The task directory mixes two unrelated things: subagent transcripts and
    background-command output. Both end in `.output`, so the name cannot tell
    them apart -- but the CONTENT can, and it is a fact about the file rather
    than a guess about the naming convention. Measured on this host: all 238
    regular-file task outputs carry zero usage records, so none of them is a
    transcript and none should mint a zero-spend agent.

    Returns one of `CARRIES_YES` / `CARRIES_NO` / `CARRIES_TRUNCATED` /
    `CARRIES_UNREADABLE`. Only `CARRIES_NO` is a corroborated negative: the
    scan reached end-of-file and found nothing. The caller excludes the file
    in all three non-YES cases -- but the two INCONCLUSIVE ones are COUNTED
    into the ingest summary so the gap stays visible instead of being absorbed
    into an ordinary-looking exclusion.
    """
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return CARRIES_UNREADABLE
    truncated = False
    try:
        with fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    truncated = True
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                message = record.get("message")
                if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                    return CARRIES_YES
    except OSError:
        # A read that fails PART-WAY is no more conclusive than one that
        # never opened the file.
        return CARRIES_UNREADABLE
    return CARRIES_TRUNCATED if truncated else CARRIES_NO


def discover_sources(
    projects_dir: Path, index: Optional[TaskIndex] = None
) -> tuple[list[Source], dict[str, int]]:
    """Every transcript file to ingest, main-thread and subagent.

    Explicit globs rather than one recursive `**/*.jsonl`: the sibling
    `tool-results/` and `workflows/` trees are NOT transcripts and a blanket
    recurse would silently start counting them as API calls.

    Returns `(sources, inconclusive)`. `inconclusive` counts the candidate
    task-directory files whose content check could NOT reach a verdict --
    `truncated` (a usage record could still sit past the scan limit) and
    `unreadable`. They are excluded from `sources` exactly as a corroborated
    negative is, so the counts are what keeps the two distinguishable
    downstream; a silent exclusion would read as "there was nothing there".
    """
    index = index or TaskIndex()
    inconclusive = {CARRIES_TRUNCATED: 0, CARRIES_UNREADABLE: 0}
    sources = [
        Source(path=p, session_id=p.stem, kind=SOURCE_MAIN)
        for p in sorted(projects_dir.glob("*.jsonl"))
    ]
    canonical: set = set()
    for p in sorted(projects_dir.glob("*/subagents/*.jsonl")):
        storing = p.parent.parent.name
        # `_resolve` -- NOT a bare `p.resolve()` with the OSError swallowed. A
        # failed resolve left the path out of `canonical`, so the extra-source
        # check below compared against a set missing this entry and ingested
        # the same transcript twice. `_resolve` falls back to the unresolved
        # path, which is the SAME fallback that check uses.
        canonical.add(_resolve(p))
        sources.append(
            Source(
                path=p,
                # Spend follows the DISPATCHER when the index knows it; the
                # storing directory is only a fallback.
                session_id=index.dispatcher_by_path.get(_resolve(p), storing),
                kind=SOURCE_SUBAGENT,
                agent_id=agent_id_from_path(p),
                storing_session_id=storing,
            )
        )
    # A regular-file task output is ingested ONLY when the canonical store
    # does not already hold it -- that check is what prevents the index from
    # double-counting the 2834 symlinked transcripts.
    for path, session_id, agent_id in index.extra_sources:
        if _resolve(path) in canonical:
            continue
        carries = carries_api_calls(path)
        if carries != CARRIES_YES:
            if carries in inconclusive:
                inconclusive[carries] += 1
            continue
        sources.append(
            Source(
                path=path,
                session_id=session_id,
                kind=SOURCE_SUBAGENT,
                agent_id=agent_id,
                storing_session_id=session_id,
            )
        )
    return sources, inconclusive


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def parse_file(path: Path, collect_turns: bool = True) -> ParseResult:
    """Parse one transcript file defensively.

    `collect_turns=False` for a SUBAGENT transcript: a turn is a main-thread
    concept (a human prompt, a task-notification, a system-reminder), and a
    subagent's internal prompts are none of those. Recording them would put an
    agent's task instructions in the session's `human` turn bucket and corrupt
    the turn-type breakdown, so subagent calls are stored with `turn_id` NULL
    -- honestly unattributed rather than attributed to the wrong thing.
    Record-shape validation still runs, so a malformed subagent record is
    still counted as unparsed rather than skipped.
    """
    result = ParseResult()
    current_turn: Optional[int] = None
    tool_use_meta: dict[str, tuple[Optional[str], Optional[str]]] = {}

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("record is not an object")
            except (json.JSONDecodeError, ValueError) as exc:
                result.unparsed_records += 1
                result.unparsed_details.append(
                    f"{path.name}:{line_no}: {exc} -- {line.strip()[:120]!r}"
                )
                continue

            rtype = record.get("type")
            try:
                if rtype == "assistant":
                    _parse_assistant(record, result, current_turn, tool_use_meta)
                    result.records_parsed += 1
                elif rtype == "user":
                    new_turn = _parse_user(
                        record, result, tool_use_meta, collect_turns
                    )
                    if new_turn is not None:
                        current_turn = new_turn
                    result.records_parsed += 1
                # Other record types (mode, attachment, queue-operation, ...)
                # are known-irrelevant and skipped without counting.
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                result.unparsed_records += 1
                result.unparsed_details.append(
                    f"{path.name}:{line_no}: {rtype or '(no type)'}: {exc}"
                )
    return result


def _parse_assistant(
    record: dict,
    result: ParseResult,
    current_turn: Optional[int],
    tool_use_meta: dict[str, tuple[Optional[str], Optional[str]]],
) -> None:
    message = record.get("message")
    if not isinstance(message, dict):
        raise ValueError("assistant record without message object")
    usage = message.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("assistant record without usage object")
    model = str(message.get("model") or "<unknown>")

    def tok(key: str) -> int:
        """Token count for `key`, or 0 when the key is genuinely ABSENT.

        Rule #12: a key missing from `usage` means that token class did not
        occur (a real 0) -- but a key that IS present with a non-numeric
        value is a shape failure, never silently coerced to 0. It must
        raise so the caller counts this record as unparsed/INCONCLUSIVE,
        not as a confident (and wrong) zero.
        """
        if key not in usage:
            return 0
        value = usage[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"non-numeric usage value for {key}: {value!r}")
        return int(value)

    result.calls.append(
        ApiCall(
            turn_index=current_turn,
            ts=parse_ts(record),
            model=model,
            input_tokens=tok("input_tokens"),
            cache_read=tok("cache_read_input_tokens"),
            cache_write=tok("cache_creation_input_tokens"),
            output_tokens=tok("output_tokens"),
            is_sidechain=bool(record.get("isSidechain")),
        )
    )

    # This scan runs AFTER the ApiCall above is appended to result.calls: a
    # malformed tool_use block here must never raise, or the call would be
    # BOTH stored as parsed AND counted as unparsed (CodeRabbit finding) --
    # over-warning is tolerable, double-counting is not. Skip defensively.
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "Agent"
                and isinstance(block.get("id"), str)
            ):
                tool_input = block.get("input")
                if not isinstance(tool_input, dict):
                    continue
                tool_use_meta[block["id"]] = (
                    tool_input.get("subagent_type"),
                    tool_input.get("description"),
                )


def _parse_user(
    record: dict,
    result: ParseResult,
    tool_use_meta: dict[str, tuple[Optional[str], Optional[str]]],
    collect_turns: bool = True,
) -> Optional[int]:
    """Handle a user record; return the new turn index if one starts."""
    message = record.get("message")
    if not isinstance(message, dict):
        raise ValueError("user record without message object")
    text = user_text(message.get("content"))
    if text is None:  # tool round-trip, not a new turn
        return None
    if not collect_turns:  # subagent transcript -- shape validated, no turn
        return None

    turn_type = classify_turn(text)
    preview = " ".join(text.split())[:120]
    result.turns.append(Turn(ts=parse_ts(record), turn_type=turn_type, preview=preview))
    turn_index = len(result.turns) - 1

    if turn_type == "task-notification":
        task_id_m = TASK_ID_RE.search(text)
        tool_use_id_m = TOOL_USE_ID_RE.search(text)
        tokens_m = SUBAGENT_TOKENS_RE.search(text)
        agent_type = description = None
        if tool_use_id_m and tool_use_id_m.group(1) in tool_use_meta:
            agent_type, description = tool_use_meta[tool_use_id_m.group(1)]
        task_id = task_id_m.group(1) if task_id_m else None
        # Same task-id may notify more than once; the LAST notification wins,
        # so per-dispatch totals are never double-counted.
        key = task_id if task_id else f"turn-{turn_index}"
        result.dispatches[key] = Dispatch(
            turn_index=turn_index,
            task_id=task_id,
            agent_type=agent_type,
            description=description,
            subagent_tokens=int(tokens_m.group(1)) if tokens_m else None,
        )
    return turn_index


def delete_source_rows(conn: sqlite3.Connection, source_path: str) -> None:
    """Remove every row derived from ONE transcript file.

    Keyed on `source_path`, not `session_id`: a session has one main-thread
    transcript plus N subagent transcripts, all sharing a session_id, so a
    session-scoped delete would destroy sibling sources' rows (#4966).
    """
    conn.execute("DELETE FROM api_calls WHERE source_path = ?", (source_path,))
    conn.execute("DELETE FROM turns WHERE source_path = ?", (source_path,))
    conn.execute("DELETE FROM agent_dispatches WHERE source_path = ?", (source_path,))
    conn.execute("DELETE FROM ingest_state WHERE path = ?", (source_path,))


def store_source(conn: sqlite3.Connection, source: Source, parsed: ParseResult) -> None:
    """Replace one transcript FILE's rows with a fresh parse (delete + insert)."""
    path = source.path
    source_path = str(path)
    stat = path.stat()
    conn.execute("DELETE FROM api_calls WHERE source_path = ?", (source_path,))
    conn.execute("DELETE FROM turns WHERE source_path = ?", (source_path,))
    conn.execute("DELETE FROM agent_dispatches WHERE source_path = ?", (source_path,))

    timestamps = [t.ts for t in parsed.turns if t.ts is not None] + [
        c.ts for c in parsed.calls if c.ts is not None
    ]
    # No timestamped record at all: fall back to file mtime (a known stand-in,
    # not a fabricated epoch-0 value).
    first_ts = min(timestamps) if timestamps else stat.st_mtime
    last_ts = max(timestamps) if timestamps else stat.st_mtime

    turn_ids: list[int] = []
    for turn in parsed.turns:
        cur = conn.execute(
            "INSERT INTO turns (session_id, source_path, ts, turn_type, preview)"
            " VALUES (?,?,?,?,?)",
            (
                source.session_id,
                source_path,
                turn.ts if turn.ts is not None else stat.st_mtime,
                turn.turn_type,
                turn.preview,
            ),
        )
        turn_ids.append(cur.lastrowid)

    for call in parsed.calls:
        conn.execute(
            "INSERT INTO api_calls (session_id, source_path, source_kind, agent_id,"
            " turn_id, ts, model, input_tokens, cache_read, cache_write,"
            " output_tokens, context_size, is_sidechain, cost_usd)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source.session_id,
                source_path,
                source.kind,
                source.agent_id,
                turn_ids[call.turn_index] if call.turn_index is not None else None,
                call.ts if call.ts is not None else stat.st_mtime,
                call.model,
                call.input_tokens,
                call.cache_read,
                call.cache_write,
                call.output_tokens,
                call.context_size,
                1 if call.is_sidechain else 0,
                cost_usd(call.model, call.input_tokens, call.cache_read,
                         call.cache_write, call.output_tokens),
            ),
        )

    for dispatch in parsed.dispatches.values():
        conn.execute(
            "INSERT INTO agent_dispatches (session_id, source_path, turn_id, task_id,"
            " agent_type, description, subagent_tokens) VALUES (?,?,?,?,?,?,?)",
            (
                source.session_id,
                source_path,
                turn_ids[dispatch.turn_index] if dispatch.turn_index is not None else None,
                dispatch.task_id,
                dispatch.agent_type,
                dispatch.description,
                dispatch.subagent_tokens,
            ),
        )

    conn.execute(
        "INSERT OR REPLACE INTO ingest_state (path, session_id, source_kind, size,"
        " mtime, unparsed_records, first_ts, last_ts) VALUES (?,?,?,?,?,?,?,?)",
        (
            source_path,
            source.session_id,
            source.kind,
            stat.st_size,
            stat.st_mtime,
            parsed.unparsed_records,
            first_ts,
            last_ts,
        ),
    )


def store_subagent_runs(
    conn: sqlite3.Connection, sources: list[Source], index: TaskIndex
) -> None:
    """Rewrite the per-dispatch ledger: one row per subagent, ingested or not.

    Rebuilt wholesale rather than incrementally -- it is a small derived index
    over state we have fully in hand at this point, and a partial update is
    how a stale 'unavailable' row would outlive the transcript coming back.
    """
    conn.execute("DELETE FROM subagent_runs")
    conn.execute("DELETE FROM task_index_sessions")
    for session_id in sorted(index.scanned_sessions):
        conn.execute(
            "INSERT OR REPLACE INTO task_index_sessions (session_id) VALUES (?)",
            (session_id,),
        )

    runs: dict[str, SubagentRun] = {}
    for source in sources:
        if source.kind != SOURCE_SUBAGENT or source.agent_id is None:
            continue
        meta = read_agent_meta(source.path)
        depth = meta.get("spawnDepth")
        runs[source.agent_id] = SubagentRun(
            agent_id=source.agent_id,
            session_id=source.session_id,
            dispatching_session_id=index.dispatcher_by_path.get(_resolve(source.path)),
            storing_session_id=source.storing_session_id,
            agent_type=meta.get("agentType"),
            description=meta.get("description"),
            tool_use_id=meta.get("toolUseId"),
            spawn_depth=depth if isinstance(depth, int) else None,
            status=STATUS_INGESTED,
            source_path=str(source.path),
        )
    # A reaped transcript still gets a row -- with NO call rows and NO
    # fabricated totals. Its whole purpose is to make the gap countable.
    for agent_id, dispatching in index.unavailable.items():
        if agent_id in runs:
            continue
        runs[agent_id] = SubagentRun(
            agent_id=agent_id,
            session_id=dispatching,
            dispatching_session_id=dispatching,
            storing_session_id=None,
            agent_type=None,
            description=None,
            tool_use_id=None,
            spawn_depth=None,
            status=STATUS_UNAVAILABLE,
            source_path=None,
            dispatched_at=index.dispatched_at.get(agent_id),
        )

    for run in runs.values():
        conn.execute(
            "INSERT INTO subagent_runs (agent_id, session_id, dispatching_session_id,"
            " storing_session_id, agent_type, description, tool_use_id, spawn_depth,"
            " status, source_path, dispatched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run.agent_id, run.session_id, run.dispatching_session_id,
                run.storing_session_id, run.agent_type, run.description,
                run.tool_use_id, run.spawn_depth, run.status, run.source_path,
                run.dispatched_at,
            ),
        )


def rebuild_sessions(conn: sqlite3.Connection) -> None:
    """Re-derive the `sessions` roll-up from the per-file `ingest_state`.

    A session spans N source files, so its row cannot be written by any single
    file's ingest. `path` names the MAIN transcript and is NULL when a session
    has only subagent sources on disk -- an honest "no main transcript here",
    not a substituted subagent path (rule #12).
    """
    conn.execute("DELETE FROM sessions")
    conn.execute(
        "INSERT INTO sessions (id, path, first_ts, last_ts, size_bytes)"
        " SELECT session_id,"
        "        MIN(CASE WHEN source_kind = ? THEN path END),"
        "        MIN(first_ts), MAX(last_ts), COALESCE(SUM(size), 0)"
        " FROM ingest_state GROUP BY session_id",
        (SOURCE_MAIN,),
    )
    # A session whose every subagent transcript was reaped has no ingest_state
    # row at all, yet it definitely existed and definitely spent. Give it a
    # session row so its UNAVAILABLE status is visible instead of the session
    # silently not appearing (which reads as "never happened").
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, path, first_ts, last_ts, size_bytes)"
        " SELECT DISTINCT session_id, NULL, NULL, NULL, 0 FROM subagent_runs"
        " WHERE session_id IS NOT NULL AND status = ?",
        (STATUS_UNAVAILABLE,),
    )


def _prepare_schema(conn: sqlite3.Connection) -> bool:
    """Create the schema, rebuilding from scratch if an older shape is present.

    Returns True when a pre-existing older DB was discarded. The DB holds no
    original data -- it is a derived rendering of the transcripts and a full
    re-ingest reproduces it exactly -- so an obsolete shape is dropped and
    rebuilt rather than migrated in place. This is CLAUDE.md rule #14 domain
    (4): a regenerable artifact, deleted through a legitimate capability.

    The probe and the drop loop BOTH range over `DERIVED_TABLES` -- the same
    set, deliberately. When they were two hand-maintained lists, a table added
    to the schema was silently absent from the rebuild, and `CREATE TABLE IF
    NOT EXISTS` then preserved the stale column shape until the next INSERT
    failed with an `OperationalError`.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    placeholders = ",".join("?" for _ in DERIVED_TABLES)
    has_tables = (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            f" AND name IN ({placeholders})",
            DERIVED_TABLES,
        ).fetchone()[0]
        > 0
    )
    rebuilt = False
    if has_tables and version != SCHEMA_VERSION:
        with conn:
            for table in DERIVED_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        rebuilt = True
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return rebuilt


def ingest(
    projects_dir: Path, db_path: Path, tasks_dir: Optional[Path] = None
) -> dict[str, Any]:
    """Ingest every transcript under projects_dir into db_path. Returns a summary.

    Covers BOTH sources (#4966): the top-level `<session-id>.jsonl`
    main-thread transcripts and the `<session-id>/subagents/agent-*.jsonl`
    subagent transcripts. The unit of work is one FILE.

    `summary["unparsed_details"]` accumulates the per-failure diagnostic
    strings (see `ParseResult.unparsed_details`) rather than printing them
    here -- `ingest()` is a library function and must not perform I/O as a
    side effect (CodeRabbit finding). `main()` prints them after this
    returns.
    """
    if not projects_dir.is_dir():
        # A typo'd --projects-dir must never render as a silent empty run
        # (rule #12): Path.glob on a missing dir yields nothing and would
        # otherwise print "files scanned: 0" with no signal of the cause.
        #
        # The refusal also NAMES what exists. The default is derived, so the
        # path in the message is one the user has never typed and cannot be
        # expected to recognise -- "not found" alone is a dead end. Listing the
        # recorded projects turns it into a next step at the cost of one
        # `iterdir`, which matters most for the first run on a new machine.
        lines = [f"projects dir not found: {projects_dir}"]
        found = available_projects()
        if found:
            lines.append("")
            lines.append("Transcripts ARE recorded for these projects:")
            # BARE paths, one per line -- deliberately NOT a copy-paste-ready
            # shell command. An earlier revision emitted
            # `--projects-dir <shlex.quote(path)>`, and CodeRabbit pointed out
            # that `shlex.quote` produces POSIX single quotes, which `cmd.exe`
            # does not treat as delimiters: on Windows the "safe" form is the
            # unsafe one. Quoting correctly would mean detecting the reader's
            # shell, which this tool cannot do and should not guess at.
            #
            # So the shell is removed from the problem rather than guarded
            # (rule #13). A bare path is unambiguous on every platform, and the
            # reader quotes it the way their own shell requires.
            lines.extend(f"  {p}" for p in found)
        else:
            lines.append("")
            lines.append(
                "No project transcripts found at all under ~/.claude/projects."
            )
        raise SystemExit("\n".join(lines))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        schema_rebuilt = _prepare_schema(conn)
        summary: dict[str, Any] = {
            "files_scanned": 0,
            "files_ingested": 0,
            "files_skipped": 0,
            "files_pruned": 0,
            "subagent_files_scanned": 0,
            "subagent_files_ingested": 0,
            "sessions_with_subagent_transcripts": 0,
            "subagent_transcripts_unavailable": 0,
            # Candidate task-directory files whose content check could not
            # reach a verdict. NOT folded into any other counter: an
            # inconclusive exclusion is not a corroborated absence.
            "candidates_scan_truncated": 0,
            "candidates_unreadable": 0,
            "task_index_available": False,
            "schema_rebuilt": schema_rebuilt,
            "records_parsed": 0,
            "unparsed_records": 0,
            "unparsed_details": [],
        }
        index = discover_task_index(
            tasks_dir if tasks_dir is not None else default_tasks_dir(projects_dir)
        )
        summary["task_index_available"] = index.available
        summary["subagent_transcripts_unavailable"] = len(index.unavailable)
        sources, inconclusive = discover_sources(projects_dir, index)
        summary["candidates_scan_truncated"] = inconclusive[CARRIES_TRUNCATED]
        summary["candidates_unreadable"] = inconclusive[CARRIES_UNREADABLE]
        for source in sources:
            summary["files_scanned"] += 1
            is_subagent = source.kind == SOURCE_SUBAGENT
            if is_subagent:
                summary["subagent_files_scanned"] += 1
            stat = source.path.stat()
            state = conn.execute(
                "SELECT size, mtime, unparsed_records FROM ingest_state WHERE path = ?",
                (str(source.path),),
            ).fetchone()
            if state is not None and state[0] == stat.st_size and state[1] == stat.st_mtime:
                summary["files_skipped"] += 1
                summary["unparsed_records"] += state[2]
                continue
            parsed = parse_file(source.path, collect_turns=not is_subagent)
            with conn:
                store_source(conn, source, parsed)
            summary["files_ingested"] += 1
            if is_subagent:
                summary["subagent_files_ingested"] += 1
            summary["records_parsed"] += parsed.records_parsed
            summary["unparsed_records"] += parsed.unparsed_records
            summary["unparsed_details"].extend(parsed.unparsed_details)

        # Reconcile: a transcript deleted/renamed on disk must not leave its
        # rows behind to keep appearing in the UI forever (Qodo finding).
        # Single transaction per stale path so the DB never sits half-pruned.
        current_path_strs = {str(s.path) for s in sources}
        tracked_paths = {
            row[0] for row in conn.execute("SELECT path FROM ingest_state").fetchall()
        }
        for stale_path in sorted(tracked_paths - current_path_strs):
            with conn:
                delete_source_rows(conn, stale_path)
            summary["files_pruned"] += 1

        with conn:
            store_subagent_runs(conn, sources, index)
            rebuild_sessions(conn)
        # Read the coverage figure off the DB, not off this run's counters: an
        # all-skipped incremental run has ingested 0 subagent files and would
        # otherwise report "no subagent transcripts" for a corpus full of them.
        summary["sessions_with_subagent_transcripts"] = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM ingest_state WHERE source_kind = ?",
            (SOURCE_SUBAGENT,),
        ).fetchone()[0]
        return summary
    finally:
        conn.close()


def project_root(start: Optional[Path] = None) -> Path:
    """The repository root at or above `start`, else `start` itself.

    A `.git` entry terminates the climb whether it is a directory (normal
    clone) or a FILE (a git worktree, where it holds a `gitdir:` pointer). A
    worktree is its own project as far as Claude Code is concerned -- its
    transcripts live under the worktree's own path -- so stopping at the file
    is correct, not a special case to work around.
    """
    here = Path(os.path.abspath(start or Path.cwd()))
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return here


def available_projects(home: Optional[Path] = None) -> list[Path]:
    """Every project directory Claude Code has recorded transcripts for.

    Used to turn a missing-directory refusal into something actionable: the
    derived default is a path the user has never typed, so "not found" alone
    leaves them with nowhere to go. A missing `~/.claude/projects` is an empty
    list, not an error -- having never run Claude Code is a legitimate state.
    """
    root = (home or Path.home()) / ".claude" / "projects"
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)


def transcript_slug(absolute: str) -> str:
    """Fold an absolute path into Claude Code's flat directory name.

    Pure string work on purpose: it is the one piece with platform-specific
    behaviour, and extracting it is what makes the Windows case testable from
    a POSIX host (`os.path.abspath` here can never produce a Windows path).

    BOTH separators are folded and the drive colon replaced, so the result is
    unambiguously RELATIVE. That is the load-bearing property: a slug that is
    still absolute silently replaces the `<home>/.claude/projects` prefix
    instead of extending it.

    The colon is folded to `-` rather than DROPPED. Dropping it also satisfied
    the relativeness property, but produced `C-Users-alice-repo` for
    `C:\\Users\\alice\\repo`, where the reported Claude Code encoding is
    `C--Users-alice-repo`. Two independent signals point at the doubled dash:
    CodeRabbit stated it directly on this PR, and its earlier search of
    community reports described the drive letter as rendering with "an
    additional dash". Folding rather than dropping matches both.

    **Still not verified first-hand** -- this host is macOS and the corpus
    holds no Windows transcript, and the community reports themselves describe
    several competing variants. `test_windows_drive_path_does_not_stay_absolute`
    therefore asserts the PROPERTY (no separator, no colon, cannot be absolute)
    rather than the string; the expected encoding is documented separately so a
    Windows user who finds it wrong knows exactly where to look. If the guess
    is wrong the directory will not exist, and `ingest()` refuses and lists the
    projects that do -- a loud, self-correcting miss.
    """
    return absolute.replace("\\", "-").replace("/", "-").replace(":", "-")


def default_projects_dir(
    cwd: Optional[Path] = None, home: Optional[Path] = None
) -> Path:
    """Where Claude Code keeps THIS project's transcripts.

    The convention is `~/.claude/projects/<cwd>` with every `/` in the absolute
    working directory replaced by `-`, so `/Users/alice/code/myapp` becomes
    `-Users-alice-code-myapp`.

    Derived rather than configured, because the alternative shipped here first:
    the default was hard-coded to this repo's author's path, which resolves to
    nothing on any other machine. `ingest()` refuses a missing directory rather
    than reporting an empty run, so that was a portability defect and never a
    wrong number -- but it made the tool unusable out of the box for everyone
    but one person, which does not survive open-sourcing.

    `cwd` and `home` are injectable so the derivation is testable without
    depending on where the test process happens to be running.

    Deliberately `abspath`, NOT `resolve()`: `resolve()` follows symlinks, and
    on macOS `/home` is a firmlink, so it rewrote `/home/bob/two` to
    `/System/Volumes/Data/home/bob/two` -- a directory name Claude Code never
    creates. `os.getcwd()` is already canonical, so the extra resolution buys
    nothing on the real path and corrupts any other. `abspath` normalises a
    relative path without touching symlinks, which is exactly the contract.

    Derived from the PROJECT ROOT, not the process working directory. The
    README's own instruction is `cd tools/usage-report && python3 ingest.py`,
    so cwd is the tool's own subdirectory; naming the transcript directory
    after it asks for one that has never existed. A live run caught this and
    the unit tests could not, because they inject `cwd` directly.

    The slug is forced RELATIVE before it is joined (CodeRabbit + Qodo, both on
    this PR). `os.path.abspath` on Windows returns `C:\\Users\\alice\\repo`, and
    `replace("/", "-")` leaves both the backslashes and the drive intact -- so
    the "slug" is still an absolute Windows path, and joining it DISCARDS the
    `<home>/.claude/projects` prefix entirely. Verified:

        PureWindowsPath('C:/Users/alice') / '.claude' / 'projects'
            / 'C:\\Users\\alice\\repo'          ->  C:\\Users\\alice\\repo

    So both separators are normalised and the drive colon dropped, which makes
    the component unambiguously relative and the prefix impossible to override.

    **The exact Windows encoding Claude Code uses is NOT verified here** -- this
    host is macOS and the corpus contains no Windows transcript, so the drive
    letter's precise rendering is a guess. What IS guaranteed is that the prefix
    survives; when the guess is wrong the directory simply will not exist, and
    `ingest()` refuses and lists the projects that DO exist (see below). A wrong
    guess is therefore a loud, self-correcting miss rather than a silent one.
    """
    home = home or Path.home()
    return home / ".claude" / "projects" / transcript_slug(
        os.path.abspath(project_root(cwd))
    )


def main() -> None:
    default_projects = default_projects_dir()
    default_db = Path(__file__).resolve().parent / "db" / "usage.db"
    parser = argparse.ArgumentParser(description="Ingest Claude Code transcripts into SQLite")
    parser.add_argument("--projects-dir", type=Path, default=default_projects)
    parser.add_argument("--db", type=Path, default=default_db)
    args = parser.parse_args()

    summary = ingest(args.projects_dir.expanduser(), args.db.expanduser())
    for detail in summary["unparsed_details"]:
        print(f"  unparsed: {detail}")
    if summary["schema_rebuilt"]:
        print(
            "schema version changed: existing DB discarded and rebuilt from the"
            " transcripts (the DB is derived data; nothing original was lost)."
        )
    print(
        f"files scanned: {summary['files_scanned']}"
        f" (subagent: {summary['subagent_files_scanned']})"
        f" | ingested: {summary['files_ingested']}"
        f" (subagent: {summary['subagent_files_ingested']})"
        f" | skipped (unchanged): {summary['files_skipped']}"
        f" | pruned (deleted from disk): {summary['files_pruned']}"
        f" | records parsed: {summary['records_parsed']}"
        f" | unparsed_records: {summary['unparsed_records']}"
    )
    if summary["candidates_scan_truncated"] or summary["candidates_unreadable"]:
        # Loud, and separate from every other count: these candidates were
        # EXCLUDED without a verdict, so any spend they carry is unmeasured
        # rather than absent.
        print(
            "NOTE: candidate task-directory files excluded WITHOUT a verdict --"
            f" scan truncated: {summary['candidates_scan_truncated']},"
            f" unreadable: {summary['candidates_unreadable']}."
            " Their spend is unmeasured, not zero."
        )
    if summary["sessions_with_subagent_transcripts"] == 0:
        # Rule #12: "no subagent transcripts on disk" is NOT "zero subagent
        # spend" -- say which one this is, rather than letting main-thread
        # figures read as session totals.
        print(
            "NOTE: no subagents/ transcripts found -- every figure is"
            " MAIN-THREAD ONLY; subagent spend is unmeasured, not zero."
        )
    else:
        print(
            "sessions with subagent transcripts:"
            f" {summary['sessions_with_subagent_transcripts']}"
        )
    if summary["unparsed_records"]:
        print(
            f"INCONCLUSIVE: {summary['unparsed_records']} record(s) failed to parse;"
            " totals may undercount. See ingest_state.unparsed_records per file."
        )


if __name__ == "__main__":
    main()
