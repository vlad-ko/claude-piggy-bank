"""Ingest Claude Code session transcripts (JSONL) into SQLite (#4948, #4966).

Usage:
    python3 ingest.py [--projects-dir <dir>] [--db db/usage.db]
    python3 ingest.py --transcript <path-to-jsonl> [--db db/usage.db]

`--projects-dir` defaults to this project's own transcript directory, derived
from the working directory via Claude Code's naming convention -- see
`default_projects_dir()`.

`--transcript` ingests EXACTLY ONE file and is the mode a Claude Code hook
uses: the hook is handed the path that just changed, so re-scanning the tree
is pure waste. See `ingest_transcript()` for what that mode deliberately does
NOT do. The two modes are mutually exclusive.

The database path resolves highest-first: `--db`, then the `CPB_DB`
environment variable, then `db/usage.db` beside this script. `CPB_DB` exists
so a plugin can point every hook invocation at its own data directory without
threading a flag through each one.

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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

SOURCE_MAIN = "main"
SOURCE_SUBAGENT = "subagent"

STATUS_INGESTED = "ingested"
STATUS_UNAVAILABLE = "unavailable"

AGENT_FILE_PREFIX = "agent-"

# Named once and imported by `serve.py` rather than re-spelled there: the
# stored vocabulary has one owner (same rule as SOURCE_MAIN / STATUS_*).
INGEST_RUNS_TABLE = "ingest_runs"
# The run stamp is ONE row, overwritten -- this is its fixed primary key, not
# a magic number. A history of runs is a different feature; what the report
# needs is "when did this database last look at the transcripts".
INGEST_RUN_ROW_ID = 1
# The on-disk layout, named once. `discover_sources()` globs it and
# `source_for_transcript()` classifies a single path against it; the two must
# describe the SAME layout or single-file mode and directory mode would derive
# different identities for one file.
TRANSCRIPT_SUFFIX = ".jsonl"
SUBAGENTS_DIR = "subagents"
TASKS_DIR = "tasks"

# Environment override for the database path, sitting between `--db` and the
# built-in default. A plugin exports it once instead of passing `--db` on
# every hook invocation.
DB_ENV_VAR = "CPB_DB"

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
    INGEST_RUNS_TABLE,
)

# Bumped whenever the shape below changes. The DB is a pure DERIVED rendering
# of the transcripts (regenerable in full by re-running this script), so an
# older shape is rebuilt from scratch rather than migrated in place -- see
# `_prepare_schema`, and `IN_PLACE_UPGRADE_FROM` below for the narrow case
# where rebuilding would cost rows to arrive at the identical database.
SCHEMA_VERSION = 8

# Versions whose difference from the current shape can be applied WITHOUT
# deleting a row, so they upgrade in place instead of being dropped and
# rebuilt. Two such hops exist, and a v6 database makes both at once:
#
#   v6 -> v7  ADDS `ingest_runs`, supplied by `CREATE TABLE IF NOT EXISTS`.
#   v7 -> v8  DROPS `api_calls.cost_usd` (#30), applied by `ALTER TABLE ...
#             DROP COLUMN`, which rewrites that table's storage in place --
#             no rebuild, no DROP TABLE, no row deleted.
#
# The set was RE-DECIDED at this bump rather than extended, and it is
# deliberately no longer called "additive": v7 -> v8 removes a column, so the
# property that admits a version here is "the delta preserves every row",
# which is the property `_prepare_schema`'s refusal guard actually protects.
# Whoever bumps SCHEMA_VERSION next has to re-decide it again -- a version
# belongs here only while EVERY difference between its shape and the current
# one is row-preserving, and that is not a property that accumulates.
IN_PLACE_UPGRADE_FROM = frozenset({6, 7})

# The two permissions an in-place upgrade needs, named and bounded, because
# `CREATE TABLE IF NOT EXISTS` silently grants the first to EVERY table and
# would have granted it to `turns` (#35).
#
# A table listed here may be CREATED FROM NOTHING by the in-place path. The
# test is not "is it new in this version" but "is an empty one TRUE": the run
# stamp holds no row derived from a transcript, so an empty `ingest_runs` says
# "no run has been recorded", which is exactly a v6 database's state. An empty
# `turns` says a session had no turns, next to an `ingest_state` that still
# marks its source unchanged so it will never be re-read -- a confident empty
# table beside a populated one, which is the failure this project exists to
# prevent. Anything not listed here must be REFUSED, not conjured.
IN_PLACE_CREATABLE_TABLES = frozenset({INGEST_RUNS_TABLE})

# A column listed here may be ADDED to a table that already holds rows, with
# its declared type. The bar is the same one: the value the existing rows get
# (always NULL) has to be TRUE of them. `archived_at` clears it -- NULL means
# "the source is still on disk", which is what every row written before the
# column existed asserted implicitly, and the next run recomputes it from the
# filesystem anyway. A NOT NULL column cannot be added this way at all, and a
# nullable one whose NULL would read as a measurement must not be.
IN_PLACE_ADDABLE_COLUMNS: dict[tuple[str, str], str] = {
    ("ingest_state", "archived_at"): "REAL",
}

# The column the current shape has RETIRED (#30). Named once: the drop, the
# "is there anything to drop" gate and the refusal all have to mean the same
# column, and spelling it three times is how they stop meaning it.
RETIRED_COLUMN_TABLE = "api_calls"
RETIRED_COLUMN = "cost_usd"

# The exits of `_prepare_schema`, in the ONE order that is correct. They are
# named constants returned by `_schema_plan()` rather than the shape of an
# if-chain, because the ordering between them used to live only in a comment:
# a future edit that swapped two branches would have changed behaviour that no
# test described. `SchemaPlanOrderingTest` is the precedence table.
PLAN_CURRENT = "current"  # nothing to do but create what is absent
PLAN_REFUSE_SHAPE = "refuse-shape"  # tables do not match the version claimed
PLAN_IN_PLACE = "in-place"  # row-preserving hop from a listed version
PLAN_REFUSE_REAPED = "refuse-reaped"  # a rebuild would delete the only copy
PLAN_LEGACY_REPAIR = "legacy-repair"  # pre-v5 shape, past the guard
PLAN_REBUILD = "rebuild"  # drop and re-derive from the transcripts

# `ALTER TABLE ... DROP COLUMN` shipped in SQLite 3.35.0 (2021-03-12), well
# after this project's Python floor (3.10, 2021-10) but not implied by it: the
# SQLite a Python build links is the platform's business, and a distro or CI
# runner can pair a new interpreter with an old library. So the capability is
# DETECTED at runtime (`_sqlite_supports_drop_column`) and the upgrade falls
# back to the ordinary rebuild path -- refusal guard included -- when it is
# absent. Assuming it would turn an upgrade into an OperationalError on
# exactly the machines we cannot see.
DROP_COLUMN_MIN_SQLITE = (3, 35, 0)

# AUTHORING RULE, enforced by `SchemaCommentPlacementTest`: no `--` comment may
# appear BETWEEN a table's parentheses. Comment freely above `CREATE TABLE` --
# SQLite stores the statement from `CREATE` onwards, so text above it is not
# part of the stored schema and cannot affect anything.
#
# Why: `ALTER TABLE ... DROP COLUMN` makes SQLite RECONSTRUCT the stored CREATE
# statement. On SQLite 3.45.1, dropping the LAST column of a table whose text
# directly above it is a `--` comment re-appends the closing paren inside the
# commented-out line, so the statement never closes:
#
#     OperationalError: error in table ingest_state after drop column:
#     incomplete input
#
# Measured 2026-08-05: red on all four CI legs (Python 3.10-3.13, SQLite
# 3.45.1), green on a 3.53.3 laptop, which discards the comment block instead.
# The version boundary is measured; the mechanism is inferred from the error
# string, as no 3.45.x was available to instrument.
#
# This is a rule about AUTHORING rather than a runtime fallback because the
# obvious fallback is a dead end: catching the error and rebuilding meets the
# reaped-source guard, which refuses a rebuild exactly when the database is the
# only copy of its measurements, so an old corpus would get *drop refused ->
# rebuild refused* and no upgrade path at all (#42). A comment that is not in
# the stored statement cannot break its reconstruction on any version.
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
-- NO MONEY COLUMN. Tokens are MEASURED; dollars were derived from a
-- hand-maintained list-rate table that went stale twice and diverged from real
-- spend by >2.5x (#30). A precise-looking figure wrong by a factor of two is
-- worse than no figure: the reader cannot see the error.
--
-- `message_id` is the API's own id for this response. NULL is legitimate and is
-- NOT a shared key: two NULL-id records are two calls, never one.
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
    message_id TEXT
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
--
-- `dispatched_at` is epoch seconds from the task-index ENTRY's own mtime; NULL
-- when it could not be read. It is the only timestamp a reaped run has, and it
-- is what lets a window-scoped view ask whether THIS window is missing spend.
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
    dispatched_at REAL
);
-- Sessions whose tasks/ directory was actually scanned. Distinguishes
-- "scanned, dispatched nothing" (a real zero) from "never scanned" (unknown).
CREATE TABLE IF NOT EXISTS task_index_sessions (
    session_id TEXT PRIMARY KEY
);
-- `archived_at` is epoch seconds at which this source stopped being present on
-- disk. NULL = the file is still there. NOT NULL = its measurements are now
-- IRREPLACEABLE: Claude Code deletes transcripts after `cleanupPeriodDays`
-- (default 30), so the rows derived from it can never be regenerated.
CREATE TABLE IF NOT EXISTS ingest_state (
    path TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    unparsed_records INTEGER NOT NULL,
    first_ts REAL,
    last_ts REAL,
    archived_at REAL
);
-- WHEN this tool last ran (#20). One row, or NONE AT ALL -- an empty table is
-- "no run has ever been recorded", which every database written before v7 is,
-- and which the report renders as an unknown age rather than as an age of
-- zero or an ingest at the epoch.
--
-- Deliberately not a column on `ingest_state`: that table is per SOURCE FILE
-- and its `mtime` is the FILE's mtime, a different fact that reads like this
-- one. A run is a fact about the ingester, so it gets its own row, and an
-- all-skipped run -- which touches no `ingest_state` row -- still records it.
-- `finished_at` dates COMPLETION: a run that raised never stamps, so a broken
-- ingest ages visibly instead of claiming to have refreshed.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    finished_at REAL NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_api_calls_message_id ON api_calls (message_id);
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
# Provenance markers, in match order. Each is a literal that Claude Code or the
# CLI itself writes at the START of a turn's text, so its presence there PROVES
# who submitted the turn. Not one of them is prose a person composes.
#
# REFERENCE CORPUS for every count in this region: one developer machine's
# local transcript corpus, 49 main-thread session files, 7,199 user turns,
# checked 2026-08-05. It is one operator's usage, not a sample of Claude Code
# users, and it is live -- re-measuring later will not reproduce these exactly.
# Every marker below sat at offset 0 of its turn in 100% of its occurrences
# there: 2,131 task-notification, 45 system-reminder, 21 local-command-stdout,
# 35 local-command-caveat, 334 command-message/-name, 12 bash-input, 12
# bash-stdout, 105 compaction, 342 skill preamble, 35 interrupt, 33 image.
#
# `<command-message>` and `<command-name>` are BOTH listed because a slash
# command that produces no stdout emits only those two (305 of the 334 turns
# lead with the message, 29 with the name), and testing `"<local-command" in
# text` missed every one -- which is how `/wizard` came to be a "cron tick".
# `<bash-input>`/`<bash-stdout>` are bash mode (`!cmd`): the same class of gap,
# found by inventorying EVERY leading `<tag>` in the corpus rather than by
# waiting for the next one to be noticed. Those eight tags are now the whole
# set that occurs.
#
# The last four are not tags, and #26 left all of them asserting `human`:
#   * the compaction literal is Claude Code's OWN continuation message, a fixed
#     string at offset 0 -- structurally STRONGER evidence than the tick
#     sentinel below. These turns carry a whole rolled-up summary, so booking
#     them to a person misattributes real context spend, not just a label.
#   * the skill preamble is the harness injecting a skill's base directory.
#   * the interrupt notice and the image placeholder are both text the CLI
#     SUBSTITUTES for something the user did not type as prose; one label,
#     because the emitter is one emitter.
# `[Image:` is anchored on the short prefix although the corpus form is
# uniformly `[Image: source: <path>]` (33/33): a variant should degrade into
# the residual's honest refusal, not be asserted, and no prompt a person writes
# begins with those seven characters. Neither bracket marker was found hiding a
# person's words behind it -- 0 of 33 image turns carry any prose after the
# placeholders, and all 35 interrupt notices are the notice and nothing else.
# A composite turn would be labelled by its OPENING, as every marker here is.
TURN_PROVENANCE_MARKERS: tuple[tuple[str, str], ...] = (
    ("<task-notification>", "task-notification"),
    ("<system-reminder>", "system-reminder"),
    ("<local-command-stdout>", "local-command"),
    ("<local-command-caveat>", "local-command"),
    ("<command-message>", "local-command"),
    ("<command-name>", "local-command"),
    ("<bash-input>", "local-command"),
    ("<bash-stdout>", "local-command"),
    ("This session is being continued from a previous conversation", "compaction"),
    ("Base directory for this skill:", "skill-injection"),
    ("[Request interrupted by user", "harness-notice"),
    ("[Image:", "harness-notice"),
)

# The label for a turn whose opening matches NO marker above. It is a refusal,
# not a class: "nothing here says who submitted this". #26 called it `human`,
# which the report then rendered as a peer row beside `task-notification` --
# absence rendered as a value. On the reference corpus that bucket held at
# least 2,143 of 3,877 turns (55.3%) that provably were not a person typing.
UNATTRIBUTED = "unattributed"

# The scheduler sentinels this classifier is willing to ASSERT. Matched at
# offset 0 only -- a sentence CONTAINING one of these is not its speaker -- and
# only as a whole token, so `PIPELINE TICKET` is not `PIPELINE TICK`. No
# closing punctuation is required: #26 demanded one, so a scheduler ending its
# sentinel with a newline failed SILENTLY into the residual.
#
# Weaker evidence than the markers above: a scheduler submits ordinary prompt
# text, so a sentinel is a CONVENTION of the scheduling tool, not something
# Claude Code guarantees. Yield on the reference corpus: 348 turns --
# PIPELINE TICK 259, PR-CYCLE TICK 69, PRECEDENT-GARAGE COHORT TICK 18,
# RESOURCE-MANAGER TICK 2.
CRON_TICK_SENTINELS: tuple[str, ...] = (
    "PIPELINE TICK",
    "PR-CYCLE TICK",
    "PRECEDENT-GARAGE COHORT TICK",
    "RESOURCE-MANAGER TICK",
)
CRON_TICK_LISTED_RE = re.compile(
    "(?:%s)(?![A-Za-z0-9])" % "|".join(re.escape(s) for s in CRON_TICK_SENTINELS)
)

# A LIST cannot grow itself, so drift has to be visible. This shape -- an
# all-caps run ending in the word TICK -- routes an opening that looks like a
# sentinel but is not listed to its own bucket, carrying its own token cost,
# where an operator either adds the name or dismisses it.
#
# It deliberately does NOT assert `cron-tick`, because a shape cannot: #26 used
# one and it had to reject a person shouting about the tick while accepting a
# scheduler name nobody had listed, which are opposite requirements. It failed
# the first -- `STOP THE TICK. it is eating my context` was booked as the tick's
# own cost, which is verbatim the defect #26 says it fixed, now needing caps
# lock. So tick-shaped-but-unlisted is INCONCLUSIVE, and says so.
#
# TICK must END the sentinel (the lookahead rejects a further capitalised word),
# because every measured sentinel ends there and `ALL TICK BOXES MUST BE
# CHECKED` is a shouted sentence, not a scheduler. Bounded `{0,40}` and
# `.match()`-anchored; measured flat at ~0.3 us/call.
CRON_TICK_SHAPE_RE = re.compile(r"[A-Z][A-Z0-9 +/&-]{0,40}\bTICK\b(?![ \t]+[A-Z0-9])")


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
    message_id: Optional[str] = None

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
class ContentBlock:
    """One contributor to the context, measured in CHARACTERS.

    Chars, not tokens, and the distinction is load-bearing: `usage` carries a
    per-CALL token count and nothing per-block, and the tool is stdlib-only so
    there is no tokenizer. Chars are what can be measured honestly. Because
    chars-per-token varies sharply by content -- minified JSON tool output
    against English prose -- a composition stated in chars is NOT a
    composition of the context window, and presenting it as one would overstate
    the dense contributors. Any token-denominated view is an apportionment of
    the measured per-call total and must be labelled as an estimate.

    `tool_name` is set for `tool_use` and `tool_result` alike; a `tool_result`
    names only a `tool_use_id`, so the name is carried forward from the
    `tool_use` that opened it.
    """

    record_type: str
    block_type: str
    tool_name: Optional[str]
    # None = the block exists but its content is NOT PERSISTED in the
    # transcript (thinking blocks), so its size is unknown rather than
    # zero. Every aggregate must treat it as no-sample, never as 0.
    chars: Optional[int]
    turn_index: Optional[int] = None
    ts: Optional[float] = None


@dataclass
class ParseResult:
    turns: list[Turn] = field(default_factory=list)
    calls: list[ApiCall] = field(default_factory=list)
    content_blocks: list[ContentBlock] = field(default_factory=list)
    dispatches: dict[str, Dispatch] = field(default_factory=dict)
    unparsed_records: int = 0
    records_parsed: int = 0
    # Per-failure diagnostics ("<file>:<line>: <reason>"), one per unparsed
    # record -- the INCONCLUSIVE count is otherwise unactionable (Qodo finding).
    unparsed_details: list[str] = field(default_factory=list)
    # Records whose `message.id` was absent, so dedupe had no key. They are
    # KEPT as individual calls; the count is surfaced so a corpus where this
    # is common cannot be mistaken for a clean one.
    calls_without_message_id: int = 0
    # Ids whose records disagreed on something other than the accumulating
    # `output_tokens` -- the genuinely ambiguous case, ~1 in 19,000 on the
    # corpus this was measured against.
    divergent_message_ids: int = 0


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
    """Classify a turn by PROVENANCE -- who submitted it -- from its opening.

    Not by topic. The distinction is the whole point (#4): the original version
    searched `cron`, `PR-cycle` and `wakeup` anywhere in a 600-char window, so
    it answered "what is this turn ABOUT", and a person asking whether the cron
    tick was worth keeping was booked as the cron tick's own cost.

    THERE IS NO `human` LABEL. Every return value below names an emitter that
    a literal at offset 0 proves, or else refuses. `who typed this` is not a
    question a transcript answers: the record of a scheduler's submission and
    of a person's are byte-identical prose. #26 answered it anyway, by making
    `human` the sole exit for an unrecognised opening, and the report rendered
    that as a peer row beside `task-notification`. Re-measured on the reference
    corpus above, at least 2,143 of those 3,877 turns (55.3%) provably were not
    a person typing -- 1,573 `[auto-resume ...]`, 342 skill preambles, 106
    `autonomous ...`, 105 compaction continuations, 33 image placeholders, 35
    interrupt notices. A session table reading "human: 3,877" was wrong by more
    than half. `unattributed` is the same measurement without the claim.

    A marker only counts at the START of the text. Deeper in, it is quoted
    content -- this turn's own text may end with an appended `<system-reminder>`
    without the harness having spoken, and a person asking about compaction
    quotes the compaction literal.

    Three things are deliberately NOT given an emitter:

      * `[auto-resume ...]` (1,573 turns), `autonomous ...` (106) and
        `[wizard heartbeat]` (28). Plainly bulk-injected, but nothing in Claude
        Code writes them: they are one scheduling tool's PROSE convention, and
        a person can type the same characters (one corpus turn quotes them
        while asking about this very classifier). Naming an emitter would be
        the guess #26 set out to delete. Refusing now costs nothing, because
        the residual no longer asserts a person.
      * an opening that is tick-SHAPED but whose sentinel is not listed. It
        gets `cron-tick-unlisted` -- inconclusive, and loud, so that neither a
        scheduler name nobody has added nor a person shouting about the tick is
        silently absorbed by a neighbouring bucket.

    The `wakeup` label removed in #26 stays removed: 117 of 117 were topic
    matches on a word that sat at offset >= 40 every time and at offset 0 never
    once, and no start-anchored wakeup marker exists in the corpus. #26's
    stated reason for the removal -- "a scheduled wakeup announces itself with
    a tick sentinel and is classified `cron-tick`" -- is FALSE and is corrected
    here: replaying those 117 turns yields `{'human': 114, 'local-command': 3}`
    and zero `cron-tick` (checked 2026-08-05). What actually happened is that
    114 scheduled turns moved into `human` and were read as people typing,
    which is the defect above; their real openings are scheduler prose and,
    three times, the compaction literal. An empty `wakeup` bucket would still
    be worse -- it would read as "measured, none found".
    """
    # `.lstrip()` on a bounded slice: leading whitespace must not defeat the
    # anchor, and the slice keeps this off the length of a large paste.
    head = text[:600].lstrip()
    for marker, turn_type in TURN_PROVENANCE_MARKERS:
        if head.startswith(marker):
            return turn_type
    if CRON_TICK_LISTED_RE.match(head):
        return "cron-tick"
    if CRON_TICK_SHAPE_RE.match(head):
        return "cron-tick-unlisted"
    return UNATTRIBUTED


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
        tasks = session_dir / TASKS_DIR
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
        for p in sorted(projects_dir.glob(f"*{TRANSCRIPT_SUFFIX}"))
    ]
    canonical: set = set()
    for p in sorted(projects_dir.glob(f"*/{SUBAGENTS_DIR}/*{TRANSCRIPT_SUFFIX}")):
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


def source_for_transcript(path: Path) -> Source:
    """Classify ONE path into the Source `discover_sources()` would have made.

    Single-file mode must derive the same identity for a file as a directory
    scan does, or which session pays for a subagent's tokens would depend on
    which command happened to ingest it -- and since an unchanged file is
    thereafter SKIPPED, a divergence would never be revised. So this reads the
    same layout constants the globs do and reuses `agent_id_from_path()`.

    Raises ValueError when the path matches neither shape. That is a refusal,
    not a no-op: a hook pointed at the wrong file must fail loudly rather than
    report a successful ingest of nothing.
    """
    if path.suffix != TRANSCRIPT_SUFFIX:
        raise ValueError(
            f"not a transcript: expected a {TRANSCRIPT_SUFFIX} file, either"
            f" <project>/<session>{TRANSCRIPT_SUFFIX} or"
            f" <project>/<session>/{SUBAGENTS_DIR}/"
            f"{AGENT_FILE_PREFIX}<id>{TRANSCRIPT_SUFFIX}"
        )
    if path.parent.name == SUBAGENTS_DIR:
        storing = path.parent.parent.name
        if not storing:
            # `<...>/subagents/agent-x.jsonl` with no session directory above
            # it. The glob can never produce this; a caller can. Refuse rather
            # than storing the calls under a session id of "".
            raise ValueError(
                f"no session directory above {SUBAGENTS_DIR}/ -- cannot say"
                " which session these calls belong to"
            )
        return Source(
            path=path,
            # The DISPATCHING session overrides this when the task index knows
            # it; the storing directory is only the fallback.
            session_id=storing,
            kind=SOURCE_SUBAGENT,
            agent_id=agent_id_from_path(path),
            storing_session_id=storing,
        )
    return Source(path=path, session_id=path.stem, kind=SOURCE_MAIN)


def dispatcher_for_agent(
    tasks_dir: Path, agent_id: str, transcript: Path
) -> Optional[str]:
    """Which session DISPATCHED this ONE agent, per the task index.

    The targeted counterpart of `discover_task_index()`, and it exists because
    attribution is not optional: the storing directory is merely whichever
    session was live when the file was written, and differs from the
    dispatcher in 749 of 2834 real cases (measured 2026-08-02). Ingesting one
    subagent transcript without consulting the index would book a quarter of
    them against the wrong session.

    Only the entry naming THIS agent is examined, and only when it resolves to
    THIS transcript -- the same key and the same identity check the full scan
    uses, so the two cannot disagree. Nothing here is ingested as data; the
    task directory remains an index (most entries are symlinks into the
    canonical store, so reading them as data would double every subagent
    figure).

    Returns None when no index, no entry, or an unreadable one -- an unknown
    dispatcher, never an invented one.
    """
    if not tasks_dir.is_dir():
        return None
    target = _resolve(transcript)
    dispatcher: Optional[str] = None
    try:
        session_dirs = sorted(tasks_dir.iterdir())
    except OSError:
        return None
    for session_dir in session_dirs:
        tasks = session_dir / TASKS_DIR
        if not tasks.is_dir():
            continue
        for entry in sorted(tasks.iterdir()):
            if agent_id_from_path(entry) != agent_id:
                continue
            # A dangling entry proves a dispatch but names no readable
            # transcript, and cannot be about the file we are holding.
            if not entry.is_symlink() or not entry.exists():
                continue
            if _resolve(entry) == target:
                # Last match wins, matching the dict-overwrite semantics of
                # `discover_task_index()` over the same sorted order.
                dispatcher = session_dir.name
    return dispatcher


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
    # tool_use_id -> tool name, so a later tool_result can name its tool.
    tool_names: dict[str, str] = {}

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
                    _parse_assistant(
                        record, result, current_turn, tool_use_meta, tool_names
                    )
                    result.records_parsed += 1
                elif rtype == "user":
                    new_turn = _parse_user(
                        record, result, tool_use_meta, tool_names, collect_turns
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
    result.calls = _dedupe_calls(result)
    return result


# The four token classes an API response reports. `output_tokens` is excluded
# from the agreement check on purpose -- it is the one that legitimately grows.
_STATIC_USAGE = ("input_tokens", "cache_read", "cache_write")


def _dedupe_calls(result: ParseResult) -> list[ApiCall]:
    """One row per API call, keyed on `message.id`.

    Claude Code writes one transcript record per streamed content block, and
    every record repeats the SAME `message.usage` object. Treating each as a
    call inflated every aggregate by 1.9-2.4x. That is worse than a scale
    error: the factor differs between main-thread and subagent transcripts, so
    comparisons between them were distorted in SHAPE, not merely in magnitude.

    **Last wins, and the rule is measured rather than assumed.** Across the
    corpus this was derived from, `output_tokens` is non-decreasing over the
    records of a single id in 4,928 of 4,928 cases, and only the final record
    carries the finished total -- 942 where every earlier record said 2. So
    summing over-counts, and first-wins would have UNDER-counted output by
    roughly 99%.

    **A whole record survives, never a synthesis of fields.** Where records of
    one id disagree beyond `output_tokens`, taking a per-field maximum would
    report a call that both wrote and did not write cache -- a combination
    that occurred in no real API response. One real record is kept and the
    ambiguity is COUNTED, so a corpus where it is common cannot pass as clean
    (rule #12).

    **A missing id is not a shared key.** Records without `message.id` each
    stay their own call. Dropping them would silently delete real spend, and
    grouping them together would merge unrelated calls; both are worse than
    keeping them and saying how many there were.
    """
    keyed: dict[str, list[ApiCall]] = {}
    unkeyed: list[ApiCall] = []
    for call in result.calls:
        if call.message_id is None:
            unkeyed.append(call)
        else:
            keyed.setdefault(call.message_id, []).append(call)

    result.calls_without_message_id = len(unkeyed)

    deduped: list[ApiCall] = list(unkeyed)
    for group in keyed.values():
        if len(group) > 1 and len(
            {tuple(getattr(c, f) for f in _STATIC_USAGE) for c in group}
        ) > 1:
            result.divergent_message_ids += 1
        # max() is stable and returns the FIRST maximum, so iterate reversed
        # to make the LAST record win a tie -- ties are the common case, since
        # every record before the final one repeats the same usage.
        deduped.append(max(reversed(group), key=lambda c: c.output_tokens))

    # Restore transcript order; the caller pairs calls with turns positionally
    # in places, and a reordered list would silently mis-associate them.
    order = {id(c): i for i, c in enumerate(result.calls)}
    deduped.sort(key=lambda c: order[id(c)])
    return deduped


def _parse_assistant(
    record: dict,
    result: ParseResult,
    current_turn: Optional[int],
    tool_use_meta: dict[str, tuple[Optional[str], Optional[str]]],
    tool_names: dict[str, str],
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
            message_id=(
                message["id"] if isinstance(message.get("id"), str) and message["id"]
                else None
            ),
        )
    )

    # This scan runs AFTER the ApiCall above is appended to result.calls: a
    # malformed tool_use block here must never raise, or the call would be
    # BOTH stored as parsed AND counted as unparsed (CodeRabbit finding) --
    # over-warning is tolerable, double-counting is not. Skip defensively.
    content = message.get("content")
    _collect_assistant_blocks(record, message, result, current_turn, tool_names)
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


def _collect_assistant_blocks(
    record: dict,
    message: dict,
    result: ParseResult,
    current_turn: Optional[int],
    tool_names: dict[str, str],
) -> None:
    """Attribute an assistant message's content blocks (#4967).

    Defensive throughout, for the same reason the Agent scan below is: this
    runs after the ApiCall has been recorded, so raising here would count one
    record as parsed AND unparsed. A block whose shape defeats expectations is
    skipped, never guessed at.

    `tool_names` accumulates `tool_use_id -> tool name` so the matching
    `tool_result` in a LATER user record can be attributed to the tool that
    produced it -- the result block itself carries only the id.
    """
    content = message.get("content")
    if not isinstance(content, list):
        return
    ts = parse_ts(record)
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            chars = len(str(block.get("text", "")))
        elif btype == "thinking":
            # Measured on the real corpus: 0 of 14,918 thinking blocks retain
            # their text. Claude Code persists `type` + an EMPTY `thinking` +
            # a `signature`, so thinking CONTENT is structurally absent from
            # every transcript -- while thinking TOKENS are still billed in
            # `output_tokens`.
            #
            # `chars = 0` here would make a composition table report
            # "thinking: 0.0%", i.e. that thinking costs nothing. That is
            # absence rendering as a value (rule #12) in the tool built to
            # catch it. `None` keeps the block COUNTED and its size UNKNOWN,
            # which are different facts and must stay different.
            raw = str(block.get("thinking", ""))
            chars = len(raw) if raw else None
        elif btype == "tool_use":
            tool_input = block.get("input")
            if tool_input is None:
                continue
            # The SERIALISED input is what occupies context, not the value of
            # any one argument -- keys and JSON punctuation are paid for too.
            try:
                chars = len(json.dumps(tool_input, sort_keys=True))
            except (TypeError, ValueError):
                continue
            name = block.get("name")
            if isinstance(block.get("id"), str) and isinstance(name, str):
                tool_names[block["id"]] = name
            result.content_blocks.append(
                ContentBlock(
                    record_type="assistant",
                    block_type="tool_use",
                    tool_name=name if isinstance(name, str) else None,
                    chars=chars,
                    turn_index=current_turn,
                    ts=ts,
                )
            )
            continue
        else:
            continue
        result.content_blocks.append(
            ContentBlock(
                record_type="assistant",
                block_type=btype,
                tool_name=None,
                chars=chars,
                turn_index=current_turn,
                ts=ts,
            )
        )


def _collect_user_blocks(
    record: dict,
    message: dict,
    result: ParseResult,
    turn_index: Optional[int],
    tool_names: dict[str, str],
) -> None:
    """Attribute a user record's content: tool results, and the human's own text.

    A tool RESULT is the largest single contributor in most sessions and is not
    the user talking, so it is never folded into the human-prompt bucket. The
    tool name comes from `tool_names`, populated when the `tool_use` was seen;
    an id with no recorded name yields `tool_name=None` -- honestly
    unattributed rather than attributed to a guess.
    """
    content = message.get("content")
    ts = parse_ts(record)
    if isinstance(content, str):
        result.content_blocks.append(
            ContentBlock("user", "user_text", None, len(content), turn_index, ts)
        )
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_result":
            payload = block.get("content")
            if isinstance(payload, str):
                chars = len(payload)
            else:
                try:
                    chars = len(json.dumps(payload, sort_keys=True))
                except (TypeError, ValueError):
                    continue
            tool_use_id = block.get("tool_use_id")
            result.content_blocks.append(
                ContentBlock(
                    record_type="user",
                    block_type="tool_result",
                    tool_name=tool_names.get(tool_use_id)
                    if isinstance(tool_use_id, str)
                    else None,
                    chars=chars,
                    turn_index=turn_index,
                    ts=ts,
                )
            )
        elif btype == "text":
            result.content_blocks.append(
                ContentBlock(
                    "user", "user_text", None,
                    len(str(block.get("text", ""))), turn_index, ts,
                )
            )


def _parse_user(
    record: dict,
    result: ParseResult,
    tool_use_meta: dict[str, tuple[Optional[str], Optional[str]]],
    tool_names: dict[str, str],
    collect_turns: bool = True,
) -> Optional[int]:
    """Handle a user record; return the new turn index if one starts."""
    message = record.get("message")
    if not isinstance(message, dict):
        raise ValueError("user record without message object")
    # BEFORE the early returns below, deliberately. A tool round-trip is not a
    # turn, but its tool_result is content and is typically the largest single
    # contributor in the session -- returning first would drop it from every
    # composition figure while the totals still read as complete. Same for a
    # subagent transcript: it has no turns but it does have content.
    _collect_user_blocks(
        record, message, result, len(result.turns) - 1 if result.turns else None,
        tool_names,
    )
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


def is_unchanged(state: Optional[tuple], stat: os.stat_result) -> bool:
    """Has this source file changed since the run that recorded `state`?

    ONE definition, used by both modes. When directory mode and single-file
    mode disagreed about what "unchanged" means, a file one of them skipped
    would be re-parsed by the other forever -- or worse, a file that really did
    change would be skipped by whichever mode a hook happened to invoke.

    `state` is the `(size, mtime, ...)` row from `ingest_state`, or None when
    the file has never been ingested.
    """
    return state is not None and state[0] == stat.st_size and state[1] == stat.st_mtime


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


def store_source(
    conn: sqlite3.Connection,
    source: Source,
    parsed: ParseResult,
    stat: Optional[os.stat_result] = None,
) -> None:
    """Replace one transcript FILE's rows with a fresh parse (delete + insert).

    `stat` is the size+mtime observed BEFORE the file was read, and both
    callers pass it. Re-statting here instead would record the state the file
    is in AFTER the parse -- so a transcript appended to while it was being
    read would be recorded as fully ingested at its new size, and every
    subsequent run would skip it. The appended calls would then be lost for
    good, silently. Recording the pre-read stat makes the worst case one
    redundant re-parse on the next run instead.

    That race is not hypothetical for `--transcript`: a hook fires on the file
    Claude Code is still writing.
    """
    path = source.path
    source_path = str(path)
    stat = stat if stat is not None else path.stat()
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
            " output_tokens, context_size, is_sidechain, message_id)"
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
                call.message_id,
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


def run_for_source(source: Source, dispatching_session_id: Optional[str]) -> SubagentRun:
    """The `subagent_runs` row for one INGESTED subagent transcript.

    Shared by the wholesale rebuild and by single-file mode so the two cannot
    describe the same agent differently.
    """
    meta = read_agent_meta(source.path)
    depth = meta.get("spawnDepth")
    return SubagentRun(
        agent_id=source.agent_id or agent_id_from_path(source.path),
        session_id=source.session_id,
        dispatching_session_id=dispatching_session_id,
        storing_session_id=source.storing_session_id,
        agent_type=meta.get("agentType"),
        description=meta.get("description"),
        tool_use_id=meta.get("toolUseId"),
        spawn_depth=depth if isinstance(depth, int) else None,
        status=STATUS_INGESTED,
        source_path=str(source.path),
    )


def store_run(conn: sqlite3.Connection, run: SubagentRun) -> None:
    """Write one dispatch row. REPLACE, so a run whose transcript came back
    stops being reported as unavailable."""
    conn.execute(
        "INSERT OR REPLACE INTO subagent_runs (agent_id, session_id,"
        " dispatching_session_id, storing_session_id, agent_type, description,"
        " tool_use_id, spawn_depth, status, source_path, dispatched_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            run.agent_id, run.session_id, run.dispatching_session_id,
            run.storing_session_id, run.agent_type, run.description,
            run.tool_use_id, run.spawn_depth, run.status, run.source_path,
            run.dispatched_at,
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
        runs[source.agent_id] = run_for_source(
            source, index.dispatcher_by_path.get(_resolve(source.path))
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
        store_run(conn, run)


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


def record_ingest_run(conn: sqlite3.Connection, finished_at: float) -> None:
    """Stamp WHEN an ingest run completed (epoch seconds), replacing the last.

    Called once per successful `ingest()`, INCLUDING a run that ingested
    nothing: "we looked and everything was unchanged" is a completed run, and
    conflating it with "nobody has looked" is the whole defect in #20. The
    caller owns the transaction.
    """
    conn.execute(
        f"INSERT OR REPLACE INTO {INGEST_RUNS_TABLE} (id, finished_at)"
        " VALUES (?, ?)",
        (INGEST_RUN_ROW_ID, finished_at),
    )


def _sqlite_supports_drop_column(version: Optional[str] = None) -> bool:
    """Can THIS SQLite run `ALTER TABLE ... DROP COLUMN`? (3.35.0+)

    Reads the live `sqlite3.sqlite_version` by default -- a version baked in
    at authoring time would describe the machine that wrote this line, not the
    one running it.

    Conservative on anything it cannot parse: an unreadable version string
    yields False, so the upgrade takes the slower rebuild path instead of
    attempting a statement that may not exist. An input we cannot read is not
    a permission (the same rule the rest of this file applies to measurements).
    """
    raw = sqlite3.sqlite_version if version is None else version
    parts = raw.split(".")[: len(DROP_COLUMN_MIN_SQLITE)]
    try:
        # Padded, so "3.35" reads as (3, 35, 0) rather than comparing short.
        parsed = tuple(int(p) for p in parts)
    except ValueError:
        return False
    if len(parsed) < 2:  # "3" says nothing about a minor release
        return False
    parsed += (0,) * (len(DROP_COLUMN_MIN_SQLITE) - len(parsed))
    return parsed >= DROP_COLUMN_MIN_SQLITE


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of `table`, or an empty set if it does not exist."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _needs_retired_column_drop(conn: sqlite3.Connection) -> bool:
    """Does THIS database still carry the retired column? (#42)

    A fact about the DATABASE, deliberately separate from
    `_sqlite_supports_drop_column()`, which is a fact about the LIBRARY. The
    upgrade gate needs both and they are not the same question: a database that
    already lacks the column needs no DROP COLUMN at all, and refusing it for
    want of a statement it will never execute pushed it onto the rebuild path,
    where a reaped source refuses it forever (#42(a)).
    """
    return RETIRED_COLUMN in _table_columns(conn, RETIRED_COLUMN_TABLE)


def _objects_referencing(conn: sqlite3.Connection, column: str) -> list[str]:
    """Non-table schema objects whose SQL names `column`, as "type name".

    Used only to say WHAT is blocking a drop. A textual match over
    `sqlite_master.sql` over-reports (a comment or a like-named column in
    another object counts), which is the right direction for a message whose
    job is to give the operator somewhere to look.
    """
    rows = conn.execute(
        "SELECT type, name FROM sqlite_master"
        " WHERE type != 'table' AND sql IS NOT NULL AND sql LIKE ?"
        " ORDER BY type, name",
        (f"%{column}%",),
    ).fetchall()
    return [f"{kind} {name}" for kind, name in rows]


def _drop_retired_cost_column(conn: sqlite3.Connection) -> None:
    """Remove `api_calls.cost_usd` where an older shape still carries it (#30).

    ONE statement, applied to that table's own storage: SQLite's DROP COLUMN
    is not a rebuild, so every row survives it and none is re-derived. Callers
    must have established `_sqlite_supports_drop_column()` first -- this is a
    3.35+ statement, and attempting it on an older library raises rather than
    degrading. Idempotent: a database that never had the column is left alone.

    SQLite can still REFUSE the drop structurally -- most plausibly because a
    user's own index or view references the column, which this project's
    durability design positively encourages people to create (#42(b)). That
    refusal is caught and re-raised as an operator-visible one NAMING what to
    remove, and deliberately NOT as a fall-through to the rebuild path: the
    rebuild would meet the reaped-source guard, and on a corpus older than the
    retention window the two correct refusals compose into no upgrade path at
    all. A refusal with a next step in it is not a dead end; a fallback into a
    second refusal is. `with conn:` rolls the failed statement back, so the
    database is left exactly as it was found.
    """
    if not _needs_retired_column_drop(conn):
        return
    try:
        with conn:
            conn.execute(
                f"ALTER TABLE {RETIRED_COLUMN_TABLE} DROP COLUMN {RETIRED_COLUMN}"
            )
    except sqlite3.OperationalError as exc:
        blockers = _objects_referencing(conn, RETIRED_COLUMN)
        named = (
            "\n\nThese schema objects name that column:\n  "
            + "\n  ".join(blockers)
            if blockers
            else "\n\nNo index, view or trigger of this database names that "
            "column, so this is SQLite's own reconstruction of the table's "
            "CREATE statement failing -- please report it with the error above."
        )
        raise SystemExit(
            f"REFUSING to upgrade the database.\n\n"
            f"SQLite would not drop the retired `{RETIRED_COLUMN_TABLE}."
            f"{RETIRED_COLUMN}` column (#30):\n\n"
            f"  {exc}\n\n"
            "SQLite refuses to drop a column that an index, view, trigger or "
            "generated column references." + named + "\n\n"
            "Nothing was changed: the statement was rolled back, and the "
            "database still holds every row, the column, and its previous "
            "schema version. Drop the object(s) above and re-run -- the "
            "upgrade then completes in place, without deleting a row. The "
            "rebuild path is NOT taken automatically here, because it would "
            "refuse over any reaped source and leave no upgrade path at all."
        ) from exc


def _target_shape() -> dict[str, set[str]]:
    """The column names the CURRENT schema requires, per table.

    Read from `SCHEMA` itself by building it in memory rather than restated as
    a constant: a hand-maintained copy of the shape is a second source of truth
    that drifts silently, which is the same failure mode that once left
    `subagent_runs` out of the rebuild's drop list.
    """
    probe = sqlite3.connect(":memory:")
    try:
        probe.executescript(SCHEMA)
        return {table: _table_columns(probe, table) for table in DERIVED_TABLES}
    finally:
        probe.close()


def _shape_problems(conn: sqlite3.Connection) -> list[str]:
    """Ways this database's tables differ from `SCHEMA` that NO upgrade can fix.

    `PRAGMA user_version` is a CLAIM about shape, and `CREATE TABLE IF NOT
    EXISTS` verifies nothing: it accepts any pre-existing table of that name
    and creates any absent one empty. So the number could be stamped onto a
    database with a missing column (which raises on the next read) or a missing
    TABLE (which does not raise at all -- it renders an empty "By turn type"
    beside a populated "By model", with `ingest_state` still marking every
    source unchanged so nothing is ever re-read). See #35.

    What is NOT a problem here:

      * a table in `IN_PLACE_CREATABLE_TABLES`, which an upgrade may create
        empty because empty is TRUE of it;
      * a column in `IN_PLACE_ADDABLE_COLUMNS`, which an upgrade may add
        because NULL is TRUE of the rows that already exist;
      * an EXTRA column. `cost_usd` on a v7 database is one, and is shed by its
        own gate; anything else a user added is inert, since every query in
        this project names its columns.

    Returns one line per problem, naming the table and the columns, because an
    operator who cannot see WHICH table is wrong cannot act on the refusal.
    """
    problems = []
    for table, required in sorted(_target_shape().items()):
        present = _table_columns(conn, table)
        if not present:
            if table not in IN_PLACE_CREATABLE_TABLES:
                problems.append(f"{table}: the whole table is missing")
            continue
        missing = sorted(
            column
            for column in required - present
            if (table, column) not in IN_PLACE_ADDABLE_COLUMNS
        )
        if missing:
            problems.append(f"{table}: missing column(s) {', '.join(missing)}")
    return problems


def _pending_additions(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """The `IN_PLACE_ADDABLE_COLUMNS` this database is missing, with their type.

    An EXISTING table missing one of them only: a column cannot be added to a
    table that is not there, and a table that is not there is either created by
    `SCHEMA` complete or is a shape problem.
    """
    pending = []
    for (table, column), decl in sorted(IN_PLACE_ADDABLE_COLUMNS.items()):
        present = _table_columns(conn, table)
        if present and column not in present:
            pending.append((table, column, decl))
    return pending


def _apply_pending_additions(conn: sqlite3.Connection) -> None:
    """Add every missing addable column. Row-preserving by construction."""
    for table, column, decl in _pending_additions(conn):
        with conn:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _unreadable_sources(conn: sqlite3.Connection) -> list[str]:
    """Tracked source paths with no file behind them any more.

    Is a rebuild LOSSY? The original justification -- "a re-ingest reproduces
    the DB exactly" -- holds only while every source is still readable. Ask the
    FILESYSTEM, not the schema: a tracked path with no file behind it can never
    be re-read, so its rows are the only copy.

    Keying on the filesystem rather than on `archived_at` is deliberate. The
    column only exists AFTER the v5 upgrade, so a guard reading it can never
    protect the upgrade that installs it -- and the real database this was
    tested against turned out to be v0, older than the shape the first attempt
    assumed. The filesystem check works for every version, including ones
    written before any of this existed.
    """
    try:
        tracked = conn.execute("SELECT path FROM ingest_state").fetchall()
    except sqlite3.OperationalError:
        return []  # no ingest_state at all: nothing tracked, nothing to lose
    return [path for (path,) in tracked if not Path(path).exists()]


def _indented_sample(lines: Sequence[str], limit: int = 5) -> str:
    """Up to `limit` lines, indented one per line, with a count of the rest.

    Truncated because a refusal an operator scrolls past is a refusal they did
    not read; the count keeps the omission visible rather than silent.
    """
    shown = sorted(lines)[:limit]
    more = f"\n  ...and {len(lines) - limit} more" if len(lines) > limit else ""
    return "  " + "\n  ".join(shown) + more


def _schema_plan(
    *,
    version: int,
    has_tables: bool,
    shape_problems: Sequence[str],
    pending_additions: bool,
    needs_column_drop: bool,
    can_drop_column: bool,
    any_source_unreadable: Callable[[], bool],
) -> str:
    """Decide WHICH exit `_prepare_schema` takes, from facts alone.

    Pure, and separate from the work, because the order of these exits is the
    load-bearing part and it used to live only in a comment. The order, and why
    each step precedes the next:

    1. A database with none of our tables is FRESH -- there is nothing to
       verify and nothing to lose.
    2. A database whose stamp is the CURRENT version, or one hop from it, is
       claiming a shape. Where the tables contradict that claim the answer is
       to REFUSE, ahead of every other exit: stamping would certify data that
       is not there, and rebuilding would delete rows over a defect we cannot
       attribute -- a crashed migration and a hand-edit look identical from
       here. Refusing preserves both the rows and the operator's choice.

       This is deliberately NOT applied to an older version. There, "the shape
       differs from current" is not damage, it is what an old version MEANS,
       and the rebuild below is the exit written for it. Refusing a v0 database
       for having a v0 shape would strand exactly the users the in-place work
       exists to serve.
    3. A listed version upgrades IN PLACE, before the reaped-source guard is
       even consulted. That guard protects a REBUILD; applied here it would
       refuse a change that deletes nothing, telling exactly the users whose
       database is the only copy of their history to stay on the old version.
       Note `can_drop_column` is only consulted when there is something to drop
       (#42(a)): asking whether the library CAN drop a column that is not there
       refused a no-op, and refused it forever on a reaped corpus.
    4. Only then the reaped-source guard, gating everything that follows --
       both remaining exits can reach the rebuild.
    5. The pre-v5 repair, past the guard. It does the same row-preserving work
       as (3) and is NOT promoted above the guard, because matching COLUMNS is
       not the same fact as a row-preserving DELTA: an unlisted version is
       precisely one whose row semantics nobody has re-decided (#38), and the
       shape probe cannot see that. It IS gated on the shape probe, for the
       same reason (3) is: it stamps.
    6. Otherwise the default: drop and re-derive from the transcripts.

    `any_source_unreadable` is a CALLABLE so that "was the guard consulted?" is
    observable in a test, and so the filesystem is not scanned on a path that
    does not need it.
    """
    if not has_tables:
        return PLAN_CURRENT
    # Can an in-place route arrive at the CURRENT shape, or only at most of it?
    # Stamping a version onto a table that is not that version is #35 by
    # another door, so an unshedable retired column disqualifies the shortcut.
    reaches_current_shape = can_drop_column or not needs_column_drop
    if version == SCHEMA_VERSION:
        if shape_problems:
            return PLAN_REFUSE_SHAPE
        # Stamped current and structurally sound -- but a database the
        # unprobed path stamped can still be missing a repairable column, and
        # at the current version no migration branch ever runs again (#35).
        # Repair it here or nothing ever will.
        if pending_additions or (needs_column_drop and can_drop_column):
            return PLAN_IN_PLACE
        return PLAN_CURRENT
    if version in IN_PLACE_UPGRADE_FROM:
        if shape_problems:
            return PLAN_REFUSE_SHAPE
        if reaches_current_shape:
            return PLAN_IN_PLACE
    if any_source_unreadable():
        return PLAN_REFUSE_REAPED
    if pending_additions and reaches_current_shape and not shape_problems:
        return PLAN_LEGACY_REPAIR
    return PLAN_REBUILD


def _prepare_schema(conn: sqlite3.Connection) -> bool:
    """Create the schema, rebuilding from scratch if an older shape is present.

    Returns True when a pre-existing older DB was discarded. The DB holds no
    original data -- it is a derived rendering of the transcripts and a full
    re-ingest reproduces it exactly -- so an obsolete shape is dropped and
    rebuilt rather than migrated in place. This is CLAUDE.md rule #14 domain
    (4): a regenerable artifact, deleted through a legitimate capability.

    Which of the six exits is taken, and in what order they are considered, is
    `_schema_plan()`'s decision and is documented there. This function only
    executes it, so a future edit cannot reorder the exits by accident.

    The shape probe and the drop loop BOTH range over `DERIVED_TABLES` -- the
    same set, deliberately. When they were two hand-maintained lists, a table
    added to the schema was silently absent from the rebuild, and `CREATE TABLE
    IF NOT EXISTS` then preserved the stale column shape until the next INSERT
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
    problems = _shape_problems(conn) if has_tables else []
    plan = _schema_plan(
        version=version,
        has_tables=has_tables,
        shape_problems=problems,
        pending_additions=bool(_pending_additions(conn)),
        needs_column_drop=_needs_retired_column_drop(conn),
        can_drop_column=_sqlite_supports_drop_column(),
        any_source_unreadable=lambda: bool(_unreadable_sources(conn)),
    )

    if plan == PLAN_REFUSE_SHAPE:
        # The reaped count is part of the ADVICE, not part of the decision:
        # where every source is still on disk the operator can move the
        # database aside and re-ingest, and where one has been reaped that
        # same action destroys the only copy. A refusal that does not
        # distinguish them invites the destructive reading.
        unreadable = _unreadable_sources(conn)
        loss = (
            "\n\nEvery tracked source is still on disk, so a rebuild would "
            "reproduce this database exactly."
            if not unreadable
            else f"\n\nWARNING: {len(unreadable)} tracked source(s) are already "
            "GONE from disk, so a rebuild would NOT reproduce them -- this "
            "database is their only copy:\n" + _indented_sample(unreadable)
        )
        raise SystemExit(
            f"REFUSING to stamp this database as schema version "
            f"{SCHEMA_VERSION}.\n\n"
            f"Its `user_version` says {version}, which asserts a shape this "
            f"tool can carry to version {SCHEMA_VERSION} without deleting a "
            "row. The tables contradict that:\n\n"
            + _indented_sample(problems, limit=len(problems))
            + "\n\n`CREATE TABLE IF NOT EXISTS` cannot repair that -- it "
            "accepts whatever table is already there, and creates a missing "
            "one EMPTY. Stamping a version number over it would either raise "
            "on the next read or, worse, report a confident empty table beside "
            "a populated one (#35).\n\n"
            "Nothing was changed: the version, the tables and every row are as "
            "they were found." + loss + "\n\n"
            "Restore the database from a backup, or move it aside and re-run "
            "to build a new one from the transcripts still on disk."
        )

    if plan == PLAN_REFUSE_REAPED:
        unreadable = _unreadable_sources(conn)
        raise SystemExit(
            f"REFUSING to rebuild the database.\n\n"
            f"{len(unreadable)} tracked source(s) no longer exist on disk, so "
            "their measurements CANNOT be regenerated by re-ingesting -- this "
            "database is their only copy. Claude Code deletes transcripts "
            "after `cleanupPeriodDays` (default 30), so this is expected on "
            "any corpus older than a month.\n\n"
            f"{_indented_sample(unreadable)}\n\n"
            "A schema rebuild would delete them permanently. Either back up "
            "the database file and re-run, or keep using the version that "
            "wrote it."
        )

    # NO ROW IS DELETED on the in-place routes. Every statement they run leaves
    # each existing row where it is:
    #
    #   * `ALTER TABLE ... ADD COLUMN` for a column NULL is true of;
    #   * `CREATE TABLE IF NOT EXISTS` for a table empty is true of
    #     (`ingest_runs`, for a v6 database);
    #   * `ALTER TABLE api_calls DROP COLUMN cost_usd`, which SQLite applies to
    #     the table's own storage -- not a rebuild, not a DROP/re-CREATE, so no
    #     row is destroyed and none is re-derived.
    in_place = plan in (PLAN_IN_PLACE, PLAN_LEGACY_REPAIR)
    if in_place:
        _apply_pending_additions(conn)
    rebuilt = plan == PLAN_REBUILD
    if rebuilt:
        with conn:
            for table in DERIVED_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA)
    if in_place:
        _drop_retired_cost_column(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return rebuilt


def ingest(
    projects_dir: Path,
    db_path: Path,
    tasks_dir: Optional[Path] = None,
    prune_missing: bool = False,
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
            "files_archived": 0,
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
            # Dedupe diagnostics (#2). Both are counts of records the keying
            # rule could not treat as ordinary, surfaced rather than hidden so
            # a corpus where either is common cannot read as a clean one.
            "calls_without_message_id": 0,
            "divergent_message_ids": 0,
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
            if is_unchanged(state, stat):
                summary["files_skipped"] += 1
                summary["unparsed_records"] += state[2]
                continue
            parsed = parse_file(source.path, collect_turns=not is_subagent)
            with conn:
                store_source(conn, source, parsed, stat)
            summary["files_ingested"] += 1
            if is_subagent:
                summary["subagent_files_ingested"] += 1
            summary["records_parsed"] += parsed.records_parsed
            summary["unparsed_records"] += parsed.unparsed_records
            summary["calls_without_message_id"] += parsed.calls_without_message_id
            summary["divergent_message_ids"] += parsed.divergent_message_ids
            summary["unparsed_details"].extend(parsed.unparsed_details)

        # Reconcile: a transcript deleted/renamed on disk must not leave its
        # rows behind to keep appearing in the UI forever (Qodo finding).
        # Single transaction per stale path so the DB never sits half-pruned.
        current_path_strs = {str(s.path) for s in sources}
        tracked_paths = {
            row[0] for row in conn.execute("SELECT path FROM ingest_state").fetchall()
        }
        now = datetime.now(timezone.utc).timestamp()
        for stale_path in sorted(tracked_paths - current_path_strs):
            if prune_missing:
                with conn:
                    delete_source_rows(conn, stale_path)
                summary["files_pruned"] += 1
            else:
                with conn:
                    conn.execute(
                        "UPDATE ingest_state SET archived_at = COALESCE(archived_at, ?)"
                        " WHERE path = ?",
                        (now, stale_path),
                    )
                summary["files_archived"] += 1
        # A source that came BACK is live again -- the mark describes current
        # state, not a one-way latch (a remounted worktree, a restore).
        with conn:
            conn.executemany(
                "UPDATE ingest_state SET archived_at = NULL"
                " WHERE path = ? AND archived_at IS NOT NULL",
                [(p,) for p in sorted(current_path_strs)],
            )

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
        # LAST, and only on the success path (#20): the stamp claims "this
        # database has seen the transcripts as of now", which is false if we
        # never got here. Read fresh rather than reusing the `now` computed
        # for archiving above, so it dates the run's END.
        with conn:
            record_ingest_run(conn, datetime.now(timezone.utc).timestamp())
        return summary
    finally:
        conn.close()


def ingest_transcript(
    transcript: Path, db_path: Path, tasks_dir: Optional[Path] = None
) -> dict[str, Any]:
    """Ingest EXACTLY ONE transcript file. Returns a summary.

    The mode a Claude Code hook uses: the hook already knows which file
    changed, so a full scan is waste. Measured against a local transcript
    corpus of 2,891 files on 2026-08-04, a directory run costs 1.18-2.44 s
    when nothing changed and 4.09 s when one file did, because it stats every
    file regardless.

    Incremental and idempotent exactly as directory mode is: unchanged
    (size+mtime) is skipped, changed has its own rows deleted by `source_path`
    and re-parsed.

    **What this mode deliberately does NOT do, and why.** It looks at one
    file, so it is evidence about one file. Three reconciliation steps of
    directory mode are therefore withheld, because each of them concludes
    something about sources it did not examine:

      * ARCHIVING. Directory mode marks every tracked source that is no longer
        on disk. Inferring that from a single path would mark an entire corpus
        as gone the first time a hook fired -- and `archived_at` is what tells
        the report its totals are no longer reproducible. Directory mode still
        performs the sweep; this mode simply never claims to have looked.
      * PRUNING. Same reason, with deletion at the end of it. `--prune-missing`
        is rejected outright alongside `--transcript`.
      * The WHOLESALE subagent ledger rebuild. `store_subagent_runs()` clears
        `subagent_runs` and `task_index_sessions` and re-derives them from the
        sources of that run; reusing it here would delete every dispatch the
        one file does not mention, including the `unavailable` rows whose
        entire purpose is to record spend that can no longer be measured. Only
        the ingested agent's own row is written, through the same builder.

    What IS global is `rebuild_sessions()`, and safely so: it re-derives the
    roll-up from the whole of `ingest_state`, which this mode leaves intact
    apart from the one file. A session spans N sources, so nothing narrower
    would be correct.

    The summary carries no `files_archived` / `files_pruned` keys at all.
    Reporting them as 0 would state that a check was run and found nothing,
    which is not what happened (rule #12: absence is not a value).
    """
    if not transcript.exists():
        raise SystemExit(
            f"transcript not found: {transcript}\n\n"
            "Single-file mode ingests the ONE file it is given, so a path that "
            "is not there is an error, not an empty run."
        )
    if not transcript.is_file():
        raise SystemExit(f"not a file: {transcript}")
    try:
        source = source_for_transcript(transcript)
    except ValueError as exc:
        raise SystemExit(f"{transcript}: {exc}") from exc

    dispatcher: Optional[str] = None
    if source.kind == SOURCE_SUBAGENT:
        # <projects-dir>/<session>/subagents/agent-<id>.jsonl
        projects_dir = transcript.parent.parent.parent
        dispatcher = dispatcher_for_agent(
            tasks_dir if tasks_dir is not None else default_tasks_dir(projects_dir),
            source.agent_id or agent_id_from_path(transcript),
            transcript,
        )
        if dispatcher is not None:
            source = replace(source, session_id=dispatcher)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        schema_rebuilt = _prepare_schema(conn)
        source_path = str(transcript)
        summary: dict[str, Any] = {
            "mode": "transcript",
            "transcript": source_path,
            "source_kind": source.kind,
            "session_id": source.session_id,
            "dispatching_session_id": dispatcher,
            "files_scanned": 1,
            "files_ingested": 0,
            "files_skipped": 0,
            "schema_rebuilt": schema_rebuilt,
            "records_parsed": 0,
            "unparsed_records": 0,
            "calls_without_message_id": 0,
            "divergent_message_ids": 0,
            "unparsed_details": [],
        }
        stat = transcript.stat()
        state = conn.execute(
            "SELECT size, mtime, unparsed_records FROM ingest_state WHERE path = ?",
            (source_path,),
        ).fetchone()
        if is_unchanged(state, stat):
            summary["files_skipped"] = 1
            summary["unparsed_records"] = state[2]
            # This one file is demonstrably present, so its archive mark (if a
            # directory run left one) is stale. Scoped to the path we looked
            # at -- the same evidence rule as everything else here.
            with conn:
                conn.execute(
                    "UPDATE ingest_state SET archived_at = NULL"
                    " WHERE path = ? AND archived_at IS NOT NULL",
                    (source_path,),
                )
        else:
            parsed = parse_file(
                transcript, collect_turns=source.kind != SOURCE_SUBAGENT
            )
            with conn:
                # `store_source` does INSERT OR REPLACE into ingest_state,
                # which rewrites the whole row and so clears `archived_at`.
                # `stat` is the PRE-read one -- see `store_source`.
                store_source(conn, source, parsed, stat)
                if source.kind == SOURCE_SUBAGENT:
                    store_run(conn, run_for_source(source, dispatcher))
            summary["files_ingested"] = 1
            summary["records_parsed"] = parsed.records_parsed
            summary["unparsed_records"] = parsed.unparsed_records
            summary["calls_without_message_id"] = parsed.calls_without_message_id
            summary["divergent_message_ids"] = parsed.divergent_message_ids
            summary["unparsed_details"] = list(parsed.unparsed_details)

        # Unconditional, including on the skip path: it is two statements over
        # a small table, and it makes the roll-up self-healing rather than
        # dependent on which mode last ran.
        with conn:
            rebuild_sessions(conn)
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


def resolve_db_path(
    flag: Optional[Path], env_value: Optional[str], default: Path
) -> Path:
    """Where to write, highest precedence first: `--db`, `CPB_DB`, default.

    An empty or whitespace-only `CPB_DB` REFUSES rather than falling back. A
    plugin that exports the variable wrongly would otherwise write every
    measurement into a different database than the one it reads, and say
    nothing -- and this tool's whole job is to not do that quietly.
    """
    if flag is not None:
        return flag.expanduser()
    if env_value is not None:
        if not env_value.strip():
            raise SystemExit(
                f"{DB_ENV_VAR} is set but empty. Unset it to use the default "
                f"database, or give it a path -- silently falling back would "
                f"write to a database you did not choose."
            )
        return Path(env_value).expanduser()
    return default


def print_dedupe_note(summary: dict[str, Any]) -> None:
    """Dedupe outcomes, surfaced identically by both modes."""
    if summary["divergent_message_ids"] or summary["calls_without_message_id"]:
        # Both are dedupe outcomes the keying rule could not treat as ordinary.
        # A divergent id is genuinely ambiguous -- one real record was kept,
        # but which one is a judgement -- and a missing id means dedupe had no
        # key at all. Neither is an error, and neither should be invisible.
        print(
            "NOTE: dedupe --"
            f" ids whose records disagreed beyond output_tokens:"
            f" {summary['divergent_message_ids']} (one whole record kept, never"
            " a per-field synthesis);"
            f" records with no message.id: {summary['calls_without_message_id']}"
            " (kept as individual calls, never merged and never dropped)."
        )


def print_inconclusive_note(summary: dict[str, Any]) -> None:
    """A parse failure is never silent, in either mode."""
    if summary["unparsed_records"]:
        print(
            f"INCONCLUSIVE: {summary['unparsed_records']} record(s) failed to parse;"
            " totals may undercount. See ingest_state.unparsed_records per file."
        )


def run_transcript_mode(transcript: Path, db_path: Path) -> None:
    """Single-file mode: one file in, one line out."""
    summary = ingest_transcript(transcript, db_path)
    for detail in summary["unparsed_details"]:
        print(f"  unparsed: {detail}")
    if summary["schema_rebuilt"]:
        # Loud in this mode too: a hook that silently discarded the DB and
        # then re-ingested one file would leave a corpus looking empty.
        print(
            "schema version changed: existing DB discarded and rebuilt. Only"
            " THIS transcript has been re-ingested -- run `python3 ingest.py`"
            " to restore the rest."
        )
    print(
        f"transcript: {summary['transcript']}"
        f" | kind: {summary['source_kind']}"
        f" | session: {summary['session_id']}"
        f" | ingested: {summary['files_ingested']}"
        f" | skipped (unchanged): {summary['files_skipped']}"
        f" | records parsed: {summary['records_parsed']}"
        f" | unparsed_records: {summary['unparsed_records']}"
    )
    # Said out loud rather than implied by absent counters: this run examined
    # ONE file and so reports nothing about any other source's presence.
    print(
        "NOTE: single-file mode -- no archive or prune sweep was performed;"
        " no claim is made about any other source."
    )
    print_dedupe_note(summary)
    print_inconclusive_note(summary)


def main() -> None:
    default_db = Path(__file__).resolve().parent / "db" / "usage.db"
    parser = argparse.ArgumentParser(description="Ingest Claude Code transcripts into SQLite")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--projects-dir",
        type=Path,
        default=None,
        help=(
            "Directory of transcripts to scan. Defaults to THIS project's own, "
            "derived from the repository root via Claude Code's naming "
            "convention."
        ),
    )
    mode.add_argument(
        "--transcript",
        type=Path,
        default=None,
        help=(
            "Ingest EXACTLY ONE transcript file (main-thread or subagent) and "
            "nothing else -- the cheap path for a Claude Code hook, which is "
            "handed the path that just changed. Makes no claim about any other "
            "source: it neither archives nor prunes."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help=(
            f"Database path. Falls back to the {DB_ENV_VAR} environment "
            f"variable, then to db/usage.db beside this script."
        ),
    )
    parser.add_argument(
        "--prune-missing",
        action="store_true",
        help=(
            "DELETE the measurements for any source no longer on disk. Off by "
            "default: Claude Code reaps transcripts after ~30 days, and those "
            "rows are then the only surviving copy. Use only to discard a "
            "source you removed deliberately."
        ),
    )
    args = parser.parse_args()
    db_path = resolve_db_path(args.db, os.environ.get(DB_ENV_VAR), default_db)

    if args.transcript is not None:
        if args.prune_missing:
            parser.error(
                "--prune-missing cannot be combined with --transcript: "
                "single-file mode examines ONE file and has no basis to "
                "conclude that any other source has gone"
            )
        run_transcript_mode(args.transcript.expanduser(), db_path)
        return

    default_projects = default_projects_dir()
    summary = ingest(
        (args.projects_dir if args.projects_dir is not None else default_projects)
        .expanduser(),
        db_path,
        prune_missing=args.prune_missing,
    )
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
        f" | archived (source gone, rows KEPT): {summary['files_archived']}"
        f" | pruned (deleted): {summary['files_pruned']}"
        f" | records parsed: {summary['records_parsed']}"
        f" | unparsed_records: {summary['unparsed_records']}"
    )
    print_dedupe_note(summary)
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
    print_inconclusive_note(summary)


if __name__ == "__main__":
    main()
