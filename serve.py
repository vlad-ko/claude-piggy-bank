"""Serve the usage-report web UI + JSON API from the SQLite DB (#4948).

Usage:
    python3 serve.py [--db db/usage.db] [--port 8377]

Localhost-only stdlib http.server (single-threaded: `Api` holds ONE sqlite3
connection and there is exactly one request in flight at a time, so no
locking is needed -- the concurrency hazard is removed by construction
rather than guarded with a lock around every query). Dates in query strings
are ISO YYYY-MM-DD interpreted in America/New_York (project convention:
times in Eastern).

Rule #12: aggregates are returned alongside their SAMPLE COUNTS (calls,
sessions, files) and SQL NULL sums stay null in the JSON -- an empty period
renders as "no data", never as a fabricated zero. `unparsed_records` is
surfaced on every summary response.

That rule reaches inside a non-empty period too (#25). A call whose `usage`
reports every token class as zero measured NOTHING, so it is counted as a
call and kept out of every CONTEXT mean, which publishes `context_calls` and
`unmeasured_calls` beside it -- see `MEASURED_CONTEXT_MIN`. A window whose
only calls are those reports no average at all rather than 0, which is what a
day with one such call used to draw as a context collapse.

No route emits a dollar figure (#30). Every quantity here is MEASURED --
tokens, calls, sessions, timestamps -- and the list-rate estimate that used to
sit beside them is gone rather than qualified: it was arithmetic over a
hand-maintained table that went stale twice and diverged from real spend by
more than 2.5x, which is this project's own defect (absence rendered as a
value) pointed at the reader. Rankings that claimed to be "by spend" now order
by total tokens and say so -- see `RANKED_BY`.
"""

from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path, PurePath
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from context_window import (
    BAND_PROVENANCE,
    BANDS,
    BANDS_AS_OF,
    WINDOW_PROVENANCE,
    WINDOWS_AS_OF,
    band_for,
    window_for_model,
)
from ingest import (
    INGEST_RUNS_TABLE,
    SOURCE_MAIN,
    SOURCE_SUBAGENT,
    STATUS_INGESTED,
    STATUS_UNAVAILABLE,
    SUBAGENTS_DIR,
    TASKS_DIR,
)
EASTERN = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
VENDOR_DIR = (HERE / "vendor").resolve()

# What the "top dispatches" ranking orders by, named ONCE (#30). The panel
# was headed "by spend" for its whole life while `agents()` ordered by
# `cache_read DESC` -- measured 2026-08-05 over a local transcript corpus, only
# 4 of the 10 most expensive dispatches ever reached it, and the row shown 7th
# was the 343rd most expensive. The defect was not a wrong sort: it was a
# heading and a query free to disagree, with nothing tying them together.
#
# So this string is the tie. `agents()` orders by the column it names, the
# payload carries that column, and `index.html` puts THIS phrase in the
# heading -- one quantity, three places, asserted equal in tests/test_serve.py.
RANKED_BY = "total tokens"
# The quantity itself, in SQL, spelled ONCE for every query that needs it --
# the four MEASURED classes summed. Unqualified on purpose: only `api_calls`
# carries these columns, so the expression is unambiguous in the joined query
# too, and one spelling cannot drift from another.
TOTAL_TOKENS_SQL = "input_tokens + cache_read + cache_write + output_tokens"

# #31: the set every figure in the `context` block ranges over, named ONCE and
# carried in the payload as `sample_is` -- the same tie `RANKED_BY` is for the
# dispatch ranking. "Calls in this window" and "calls in this window whose
# context was measured" are different sets, and the second is the one the
# median describes.
CONTEXT_SAMPLE = "calls with a measured context size"
# The spread published beside the median. Six points, because the shape is
# heavily right-skewed and three would hide the tail: measured 2026-08-05 over
# the reference corpus, p10 77,128 / p25 105,701 / p50 155,330 / p75 261,709 /
# p90 583,748 / p99 958,151 -- the last pressed against a 1M window.
PERCENTILES = (10, 25, 50, 75, 90, 99)
# The headline is the median, and the median IS this percentile -- computed
# once and published in both places, so the card and the spread beside it
# cannot disagree.
MEDIAN_PERCENTILE = 50

# #25: the threshold that separates A MEASUREMENT from NO MEASUREMENT, spelled
# ONCE for every figure that averages, medians or bands a context.
#
# `context_size` is `input_tokens + cache_write + cache_read` -- the whole
# prompt side of a call. Zero of all three is not a small prompt; it is no
# prompt accounting at all, and no request to the Messages API can consist of
# nothing. Measured 2026-08-05 over a local corpus of 38,381 distinct API
# calls: 82 rows report every token class as 0, and every one of them carries
# a COMPLETE `usage` object whose token keys are PRESENT and valued zero. The
# parser is faithful -- it stores exactly what is on disk -- so the defect is
# entirely in what the aggregates then do with those rows.
#
# The threshold is a MINIMUM rather than a `> 0` comparison so that the SQL
# and the Python spellings below share one literal. It is 1, not 0, and the
# distinction is the whole issue: one token is the smallest quantity that can
# be measured, and it IS a measurement. A GENUINE measured zero -- a call that
# really produced 0 output tokens, or read 0 from cache -- is a healthy sample
# in its own dimension and is untouched by this: it is counted, summed and
# averaged like any other row. Only the prompt side going entirely unrecorded
# takes the call out of a CONTEXT mean.
MEASURED_CONTEXT_MIN = 1


def has_context_measurement(context_size: int) -> bool:
    """Whether this row's `context_size` is a measurement at all (#25).

    False for 0 -- no prompt accounting -- and false for a negative, which
    summed token counts cannot produce: a row carrying one is broken, and the
    one thing it must not do is join the sample as the most frugal call on the
    report. The SQL twin is `measured_context_sql()`.
    """
    return context_size >= MEASURED_CONTEXT_MIN


def measured_context_sql(column: str = "context_size") -> str:
    """`has_context_measurement()` as a SQL predicate over `column`.

    Takes the column expression so a joined query can alias it
    (`a.context_size`) without re-spelling the comparison. Two definitions of
    "has a measurement", free to drift apart, is how this class of defect
    recurs; `tests/test_serve.py` asserts the two select the same rows.
    """
    return f"{column} >= {MEASURED_CONTEXT_MIN}"


def context_aggregate_sql(column: str = "context_size") -> str:
    """The three columns a context mean must publish TOGETHER (#25).

    An average, and beside it the size of the sample it ranged over and the
    count of calls it refused -- CLAUDE.md's "count samples separately from
    the aggregate", as SQL. `AVG` skips the NULLs the CASE produces, so a
    window whose every call measured nothing yields NULL rather than 0: that
    window is INCONCLUSIVE for context, and the call counts beside it say
    which of "nothing happened" and "nothing was measured" it was.

    The counts are `COUNT(...)` rather than `SUM(...)`, so an empty window
    gives 0 -- a count over an empty set is a real zero, corroborated by the
    `calls` count beside it -- instead of a NULL that would have to be
    coalesced back into one. And `unmeasured_calls` is the REMAINDER rather
    than the complementary predicate, so the two counts partition the group by
    construction: `context_calls + unmeasured_calls == COUNT(*)` holds for any
    value the column can hold, including a NULL that no predicate would match
    either way.

    `column` is an internal literal chosen by the caller (`context_size` or an
    alias of it), never a request parameter -- nothing here interpolates user
    input into SQL.
    """
    measured = measured_context_sql(column)
    return (
        f"AVG(CASE WHEN {measured} THEN {column} END) avg_context,"
        f" COUNT(CASE WHEN {measured} THEN 1 END) context_calls,"
        f" COUNT(*) - COUNT(CASE WHEN {measured} THEN 1 END) unmeasured_calls"
    )


# #4966: an aggregate must NAME the set it ranges over. `source_kind` in the
# DB is the ingester's vocabulary ('main'/'subagent'); these are the labels
# that cross the API boundary, defined ONCE here (rule #15 outward check).
# The STORED values (`source_kind`, `subagent_runs.status`) are imported from
# `ingest`, never re-spelled here: they are one fact with one owner, and a
# literal copy is the drift this repo files as #4648.
SCOPE_MAIN = "main-thread"
SCOPE_SUBAGENT = "subagent"
SCOPE_LABELS = {SOURCE_MAIN: SCOPE_MAIN, SOURCE_SUBAGENT: SCOPE_SUBAGENT}
SCOPE_INCLUDES_BOTH = "main-thread + subagent"
SCOPE_INCLUDES_MAIN_ONLY = "main-thread only"

# #44: the same rule on a SECOND axis -- which PROJECTS a figure ranges over.
# The plugin resolves its database from `${CLAUDE_PLUGIN_DATA}`, one directory
# per INSTALL, so a user-scope install ingests every project the user opens
# into the same file ("several projects can share one database" --
# docs/plugin.md). Every headline figure is then a sum across projects, in the
# vocabulary of one.
#
# No `project` column is added for this, and none is needed: the dimension is
# already in the stored path, whose shape is the layout `discover_sources()`
# globs for. Naming the set is a read.
#
# The directory that names the project sits one nesting level apart between
# the two source kinds:
#
#   <project>/<session>.jsonl                        SOURCE_MAIN
#   <project>/<session>/subagents/agent-<id>.jsonl   SOURCE_SUBAGENT
#   <project>/<session>/tasks/<entry>                SOURCE_SUBAGENT ingested
#                                                    from the harness task
#                                                    index, which mirrors the
#                                                    same project name under
#                                                    the OS temp dir
PROJECT_HOLDER_DIRS = frozenset({SUBAGENTS_DIR, TASKS_DIR})

# A reaped run whose dispatch time could not be read. Distinct from
# `unavailable` (a gap PROVEN to fall in this window) because "we cannot tell
# whether this window is affected" is a different claim from "it is".
STATUS_UNAVAILABLE_UNDATED = "unavailable-undated"

# A reaped run we HAD ALREADY MEASURED (#41). `subagent_runs.status` is written
# from the task index -- "the transcript is not on disk now" -- so one stored
# value covered two different states of knowledge:
#
#   * reaped BEFORE ingest: no `api_calls` row exists, nothing was ever
#     measured, and the panel must render absence (that stays `unavailable`);
#   * reaped AFTER ingest: the rows are still here, because an archived source
#     is archived and not deleted (CLAUDE.md, "Durability"). This database is
#     now their ONLY copy -- precisely what the durability design exists to
#     keep -- and blanking them rendered a present measurement as absence.
#
# Measured 2026-08-05 on the ranking fixture: one such dispatch counted
# 9,003,000 tokens in the summary cards for the window while every measured
# field of its row in the panel that RANKS by that quantity was null -- one
# database, one window, two answers. Derived at read time, never stored: the
# distinction IS "do we hold call rows for this dispatch in this window", which
# the panel's own LEFT JOIN already computes and then threw away, and anything
# beyond that evidence would be a guess.
# The word matches the vocabulary the rest of the codebase uses for the same
# fact -- `ingest_state.archived_at`, `durability.archived_sources`.
STATUS_ARCHIVED = "archived"

# The `/api/agents` fields that IDENTIFY a dispatch rather than measure it.
# Every other field that endpoint returns is an aggregate over `api_calls`, so
# the blanking for a run with no surviving call rows is derived as "not one of
# these" instead of being hand-listed beside the SELECT. The hand-kept list
# this replaces had to agree with the ORDER BY and the HAVING with nothing
# forcing it to: dropping one alias from it was a one-word edit that shipped a
# fabricated figure. Adding a measured column now blanks by construction.
AGENT_IDENTITY_FIELDS = frozenset({
    "agent_id", "agent_type", "description", "spawn_depth",
    "dispatching_session_id", "storing_session_id", "session_id",
})

# How old the LAST INGEST RUN may get before the page says so in the banner
# (#20). It qualifies the run, never the newest measured call: an idle machine
# legitimately produces no calls for hours, and warning on that would cry stale
# over a database that is perfectly current.
#
# 15 minutes, from two measurements rather than taste:
#
#   * The incident that opened #20 was 1.2 h of unflagged staleness. Any
#     threshold near that would not have fired on the case it exists for, so
#     the ceiling is well under an hour.
#   * The floor is what a refresh costs, or the banner would fire on people
#     who ARE re-ingesting. Measured 2026-08-04 on macOS 15 against the
#     largest transcript corpus on this machine (2,891 files, 1.9 GB): a cold
#     full ingest took 39.9 s, an all-skipped incremental re-run 1.8 s.
#
# 900 s is ~500x that incremental run and ~23x a full cold parse, so anyone
# re-ingesting on any sane cadence never sees the banner. It marks NEGLECT,
# not latency. Re-check the floor if ingest ever stops being incremental.
STALE_AFTER_SECONDS = 15 * 60

# Why `stale` is null, when it is (#34). Spelled ONCE here and read by name in
# `index.html`, because the page used to name a cause of its own -- and named
# the one cause the database could rule out.
#
# No database written before `ingest_runs` existed can record a run, so a
# missing TABLE is the only absence attributable to the schema's age.
STALE_UNKNOWN_NO_RUN_TABLE = "no-run-table"
# The table is here and empty: this database RECORDS run times and no run has
# ever finished over it. Three causes collapse into this one value and the
# database cannot tell them apart -- an ingest that raised after the schema was
# stamped, an in-place upgrade not yet followed by a run, a wrong-shaped table
# that makes the recorder raise every time. It does not claim which; what it
# rules out is "too old to record", which is what the page used to assert.
STALE_UNKNOWN_NO_RUN_RECORDED = "no-run-recorded"
# The stamp is AHEAD of the server's clock, so there is no age to compare.
STALE_UNKNOWN_RUN_IN_FUTURE = "run-in-future"

MIN_LIMIT = 1
MAX_LIMIT = 500
DEFAULT_LIMIT = 20

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def day_bounds(from_str: Optional[str], to_str: Optional[str]) -> tuple[float, float]:
    """Epoch [start, end) for an inclusive Eastern-date range. Raises ValueError."""
    start = 0.0
    end = 32503680000.0  # year 3000: effectively unbounded
    if from_str:
        d = date.fromisoformat(from_str)
        start = datetime(d.year, d.month, d.day, tzinfo=EASTERN).timestamp()
    if to_str:
        d = date.fromisoformat(to_str) + timedelta(days=1)
        end = datetime(d.year, d.month, d.day, tzinfo=EASTERN).timestamp()
    if start >= end:
        # A reversed range ("from" after "to") must be a rejected request,
        # never zero matching rows silently read as "no usage this period"
        # (CodeRabbit finding -- that's the one reading the README rules out).
        raise ValueError("'from' must not be later than 'to'")
    return start, end


def eastern_day(ts: float) -> str:
    """ISO date of an epoch timestamp in America/New_York."""
    return datetime.fromtimestamp(ts, tz=EASTERN).date().isoformat()


def clamp_limit(raw: Optional[str]) -> int:
    """Parse+bound a user-supplied row LIMIT to [MIN_LIMIT, MAX_LIMIT].

    SQLite treats a negative LIMIT as "no limit"; an unbounded or huge value
    would materialize the whole table into memory (Qodo + CodeRabbit
    findings). Raises ValueError on a non-integer value (mapped to HTTP 400
    by the caller), rather than silently substituting the default.
    """
    value = int(raw) if raw is not None else DEFAULT_LIMIT
    return max(MIN_LIMIT, min(value, MAX_LIMIT))


def staleness_verdict(
    last_run_at: Optional[float], as_of: float, run_table_present: bool
) -> tuple[Optional[bool], Optional[str]]:
    """`(stale, why_unknown)` -- tri-state, with the absence NAMED (#34).

    Split out of `_ingest_health` so the boundary can be asserted at the exact
    second without a patched clock: the verdict is a pure function of the two
    timestamps and of whether the database can record a run at all.

    `stale` is True, False, or None for "cannot tell", and `why_unknown` is
    non-null exactly when `stale` is None. Two absences are told apart because
    they are different claims to their reader, not because the distinction is
    cheap: "no run was ever recorded" and "runs are recorded and the last one
    is dated in the future" are both unknown ages with entirely different
    remedies.

    A NEGATIVE age is the arithmetic case this tri-state exists for, and it is
    the one it used to answer. `(as_of - last_run_at) > STALE_AFTER_SECONDS`
    returns False for a stamp 30 days ahead of the server's clock -- "the data
    is fresh" -- while `index.html`'s own `fmtAge()` prints "in the future
    (clock skew)" on the same render. Reproduced 2026-08-05; reachable through
    clock skew, an NTP step mid-run, or a database copied between machines,
    which CLAUDE.md's Durability section puts explicitly in scope.

    It reports None rather than `abs()`'s "stale". `abs()` is a defensible
    choice and is the wrong one here: it converts "these two clocks disagree by
    30 days" into a confident statement that the data is 30 days old, which is
    a measurement nothing took. The field is already tri-state, the honest
    branch already exists, and the remedy differs -- a genuinely stale database
    is fixed by re-running ingest, a skewed one is not. "Cannot tell" is the
    only reading supported by the arithmetic, and it is loud: the page raises a
    notice for it rather than leaving it to the grey data-age line.

    A ZERO age stays a verdict, not an unknown: a run that just finished is the
    freshest sample there is, and a real 0 must stay distinguishable from no
    sample (CLAUDE.md). The comparison is strict, so `STALE_AFTER_SECONDS`
    itself is the last fresh second -- the rule the UI publishes is "older
    than the threshold".
    """
    if last_run_at is None:
        return None, (
            STALE_UNKNOWN_NO_RUN_RECORDED
            if run_table_present
            else STALE_UNKNOWN_NO_RUN_TABLE
        )
    age = as_of - last_run_at
    if age < 0:
        return None, STALE_UNKNOWN_RUN_IN_FUTURE
    return age > STALE_AFTER_SECONDS, None


def nearest_rank(sorted_values: list[int], percentile: int) -> Optional[int]:
    """The `percentile`-th value of an ASCENDING sample, or None if it is empty.

    Nearest rank, so every published percentile is a value some call actually
    carried. The interpolating definition would report a median halfway
    between two calls -- a number no call had, which on an even sample can sit
    on the far side of a band boundary from both of its neighbours.

    Empty in, None out: an empty window has no median, and 0 would read as a
    window full of tiny calls (CLAUDE.md, "absence is never rendered as a
    value").

    Integer arithmetic throughout. `ceil(p / 100 * n)` in floating point is a
    rounding accident waiting to shift an index by one at exactly the round
    percentiles this function is called with.
    """
    n = len(sorted_values)
    if n == 0:
        return None
    index = (percentile * n + 99) // 100 - 1  # ceil(p*n/100) - 1
    return sorted_values[max(0, min(index, n - 1))]


def project_of(source_path: str, source_kind: str) -> Optional[str]:
    """Which PROJECT a stored source path belongs to, or None if it cannot say.

    Not a second parser: the shapes are the ones `ingest.discover_sources()`
    globs for, spelled with the ingester's own constants (see
    `PROJECT_HOLDER_DIRS`), so a layout change has one owner and this follows
    it rather than drifting from it.

    The answer is the directory NAME, not its path. That name is Claude Code's
    slug -- the absolute working directory with each separator folded to `-` --
    which is what identifies a project; the harness mirrors the same slug under
    the OS temp dir, so keying on the full path would split one project in two.

    **Returns None rather than guessing.** A path that fits none of the shapes
    has no project we can name, and the two available guesses are both wrong in
    the direction this issue exists to prevent: the directory that happens to
    sit above the file books one project's tokens under another's name, and a
    synthesised "(unknown)" project inflates the count of a set the figure
    ranges over. The caller counts these separately instead (rule #12).

    Pure string work over `PurePath`: no filesystem is consulted, so a
    transcript reaped months ago still resolves to its project.
    """
    path = PurePath(source_path)
    if source_kind == SOURCE_MAIN:
        holder = path.parent
    elif source_kind == SOURCE_SUBAGENT:
        if path.parent.name not in PROJECT_HOLDER_DIRS:
            return None
        holder = path.parent.parent.parent
    else:
        # A kind this version does not know is not a project it can locate.
        return None
    # An empty `.name` means the walk ran off the top into the anchor ('/' or
    # a relative '.'), i.e. there is no project directory above this file.
    return holder.name or None


class Api:
    """Query layer over the ingested DB. One connection per server (serialized)."""

    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def _has_ingest_runs_table(self) -> bool:
        """Can this database record an ingest run AT ALL?

        The one thing that tells the two no-stamp absences apart, and the
        reason it is asked separately: a database whose schema predates run
        recording cannot have a stamp, while a database that HAS the table and
        no row is one over which no run has ever finished. Those read
        identically as `last_run_at: null` and mean opposite things to the
        person holding them (#34).

        Asked of `sqlite_master` rather than by catching `OperationalError`,
        because that except would also swallow a corrupt or locked database and
        report the failure as a merely old schema.
        """
        return (
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (INGEST_RUNS_TABLE,),
            ).fetchone()
            is not None
        )

    def _last_ingest_run(self) -> Optional[float]:
        """When `ingest.py` last COMPLETED, or None if that was never recorded.

        Two different absences reach this None, and both are an honest "no
        sample" rather than a value:

        * a pre-v7 database has no `ingest_runs` table at all -- `serve.py`
          never migrates, it reads the database as the ingester left it, so a
          user who upgrades CPB and opens the report before re-running ingest
          is in exactly this state;
        * a database WITH the table and no row: no run has ever finished over
          it, whether because one raised, because the in-place upgrade path
          leaves the table empty until a run completes, or because the
          recorder itself is failing.

        Neither is an ingest at the epoch and neither is stale-forever: the
        age is UNKNOWN, and `_ingest_health` says so with `stale: null` --
        naming WHICH absence it has in `stale_unknown_reason`, because the
        second one is a broken ingest and the page used to render it as the
        first, a benign old database.
        """
        if not self._has_ingest_runs_table():
            return None
        row = self.conn.execute(
            f"SELECT finished_at FROM {INGEST_RUNS_TABLE}"
            " ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        return row["finished_at"] if row is not None else None

    def _ingest_health(self) -> dict[str, Any]:
        """Freshness and parse health of the DATABASE -- corpus-wide (#20).

        Alone in `summary()`, this block does NOT range over the selected
        window, and that is the point: it describes how current the database
        is, not the period on screen. Windowed, `newest_call_ts` would call a
        deliberately historical view months stale seconds after an ingest.
        The UI labels it as corpus-wide for the same reason.

        Two timestamps, never conflated, because either alone misleads:
        `last_run_at` is when this tool last LOOKED at the transcripts, and
        `newest_call_ts` is the most recent call it FOUND. A fresh run over an
        idle machine is healthy and shows an old `newest_call_ts`; a database
        nobody has re-ingested for a week can show a recent `newest_call_ts`
        for the last thing it ever saw. Only the pair is readable.

        `as_of` is the server's own clock at the moment this response was
        built, so an age is measured against the machine that owns the data
        rather than against whatever the browser believes the time is.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) files, SUM(unparsed_records) unparsed FROM ingest_state"
        ).fetchone()
        last_run_at = self._last_ingest_run()
        # MAX over an empty set is SQL NULL and stays None: "no calls
        # ingested" is not a call at the epoch (rule #12).
        newest_call_ts = self.conn.execute(
            "SELECT MAX(ts) newest FROM api_calls"
        ).fetchone()["newest"]
        as_of = time.time()
        # Tri-state on purpose: True, False, or None for "cannot tell" -- and
        # when it cannot tell, WHICH unknown it has, because the page cannot
        # be left to guess a cause (#34). `stale_unknown_reason` is non-null
        # exactly when `stale` is null.
        #
        # The table check is asked twice per response (here and inside
        # `_last_ingest_run`) rather than threaded through: it is one indexed
        # `sqlite_master` lookup, and the alternative is a method whose answer
        # depends on the caller having asked the right question first.
        stale, stale_unknown_reason = staleness_verdict(
            last_run_at, as_of, self._has_ingest_runs_table()
        )
        return {
            "files": row["files"],
            "unparsed_records": row["unparsed"],
            "last_run_at": last_run_at,
            "newest_call_ts": newest_call_ts,
            "as_of": as_of,
            "stale_after_seconds": STALE_AFTER_SECONDS,
            "stale": stale,
            "stale_unknown_reason": stale_unknown_reason,
        }

    def _durability(self, start: float, end: float) -> dict[str, Any]:
        """Can the numbers in this window be recomputed, or only read here? (#14)

        Claude Code deletes transcripts after `cleanupPeriodDays` (default 30).
        CPB retains the measurements of a reaped transcript rather than pruning
        them, so the totals stay complete -- but they can no longer be
        regenerated by re-ingesting, because the source is gone and this
        database is their only copy.

        That is a materially different claim from "derived data, safe to drop
        and rebuild", and the two must not render identically (rule #12). This
        block is what makes them distinguishable, and it is scoped to the SAME
        window as the totals it qualifies: a durability warning that ranged
        corpus-wide would condemn windows it does not describe.
        """
        rows = [
            r
            for r in self.conn.execute(
                "SELECT i.path, COUNT(*) calls FROM api_calls a"
                " JOIN ingest_state i ON a.source_path = i.path"
                " WHERE a.ts >= ? AND a.ts < ? AND i.archived_at IS NOT NULL"
                " GROUP BY i.path ORDER BY i.path",
                (start, end),
            )
        ]
        return {
            "archived_sources": len(rows),
            "archived_calls": sum(r["calls"] for r in rows),
            "reproducible": not rows,
            "sources": [r["path"] for r in rows],
        }

    def _projects(self, start: float, end: float) -> dict[str, Any]:
        """Which PROJECTS the figures in this response range over (#44).

        TWO sets, counted separately and never merged, because neither answers
        the other's question:

        * `in_window` ranges over exactly the rows every other figure here does
          -- projects with at least one call in [start, end). It is what the
          totals on screen actually cover, and it is the count that says
          whether a headline number is one project's or several projects'.
        * `in_database` ignores the window and ranges over everything this
          database holds. A project that was ingested but is silent in this
          window is a MEASURED ZERO, not an absent one (rule #12): reporting
          only the window would quietly shrink the set as the reader narrows
          the dates, and a cross-project database would look single-project on
          any day only one project was used.

        Both are named in the payload, so which one a reader is looking at is
        never a matter of inference.

        `unattributed_calls` / `unattributed_sources` count what `project_of()`
        refused to place. Those calls stay in the totals -- their tokens were
        measured -- but they belong to no project here and are NOT folded into
        one, because the only available fold is a guess at a neighbour.
        `unattributed_calls` is window-scoped like `in_window`;
        `unattributed_sources` is database-wide like `in_database`.
        """
        # DISTINCT paths rather than a grouped COUNT: the SET is what the scope
        # line ranges over, and per-path call counts are needed only for paths
        # the layout could not place -- normally none.
        #
        # Measured 2026-08-05 on a synthetic 600k-call / 3,000-source /
        # 18-project database (macOS, warm cache, best of 5), against a
        # `summary()` of ~1.5 s: this block costs 283 ms as written and 422 ms
        # when the window set came from a grouped COUNT. A corpus that DOES
        # hold an unplaceable path pays 637 ms, because the second pass below
        # then runs -- borne by the corpus that has the problem, not by every
        # other one.
        window: set[str] = set()
        window_has_unplaceable = False
        for row in self.conn.execute(
            "SELECT DISTINCT source_path, source_kind FROM api_calls"
            " WHERE ts >= ? AND ts < ?",
            (start, end),
        ):
            name = project_of(row["source_path"], row["source_kind"])
            if name is None:
                window_has_unplaceable = True
            else:
                window.add(name)
        unattributed_calls = 0
        if window_has_unplaceable:
            # Its own pass, run only when there is something to count, and
            # counting CALLS: the number of unplaceable paths is a count of
            # files, and reporting one as the other is this project's own
            # defect class -- a plausible number for a different quantity.
            unattributed_calls = sum(
                row["calls"]
                for row in self.conn.execute(
                    "SELECT source_path, source_kind, COUNT(*) calls FROM api_calls"
                    " WHERE ts >= ? AND ts < ? GROUP BY source_path, source_kind",
                    (start, end),
                )
                if project_of(row["source_path"], row["source_kind"]) is None
            )
        database: set[str] = set()
        unattributed_sources = 0
        # UNION over BOTH tables, deliberately: `ingest_state` is the file
        # ledger and `api_calls` is the measurements, and the durability block
        # already depends on the two agreeing. Should they ever diverge, a
        # union over-reports the set rather than silently shrinking it -- and
        # under-reporting is what this issue is about. `UNION`, not `UNION
        # ALL`: the same (path, kind) in both tables is one source.
        for row in self.conn.execute(
            "SELECT path source_path, source_kind FROM ingest_state"
            " UNION SELECT DISTINCT source_path, source_kind FROM api_calls"
        ):
            name = project_of(row["source_path"], row["source_kind"])
            if name is None:
                unattributed_sources += 1
            else:
                database.add(name)
        return {
            "in_window": len(window),
            "names_in_window": sorted(window),
            "in_database": len(database),
            "names_in_database": sorted(database),
            "unattributed_calls": unattributed_calls,
            "unattributed_sources": unattributed_sources,
        }

    def _scope(self, start: float, end: float) -> dict[str, Any]:
        """Main-thread vs subagent breakdown + transcript COVERAGE (#4966).

        Plus, on its own axis, the PROJECT set these figures range over (#44) --
        see `_projects()`.

        `coverage` is what keeps a real zero distinguishable from an
        unmeasured one (rule #12): `subagent.calls == 0` with
        `sessions_with_subagent_transcripts == 0` means nobody looked, and
        `includes` says so; the same 0 with a nonzero coverage count means the
        subagents genuinely spent nothing in this window.

        **Coverage ranges over the SAME window as the buckets.** It used to be
        corpus-wide, which made the two halves of that sentence range over
        different sets: a window with no subagent calls, in a corpus that has
        subagent transcripts elsewhere, reported `subagent.calls == 0`
        alongside `includes == "main-thread + subagent"` -- claiming the
        subagents genuinely spent nothing in a window whose transcripts were
        never established to exist. The window's sessions are derived from
        `api_calls` and every coverage count is restricted to them.
        """
        rows = {
            r["source_kind"]: dict(r)
            for r in self.conn.execute(
                "SELECT source_kind, COUNT(*) calls, SUM(input_tokens) input,"
                " SUM(cache_read) cache_read, SUM(cache_write) cache_write,"
                " SUM(output_tokens) output, " + context_aggregate_sql() +
                " FROM api_calls WHERE ts >= ? AND ts < ? GROUP BY source_kind",
                (start, end),
            )
        }
        # A scope with no rows at all: every COUNT is a real zero (nothing
        # happened, corroborated by `calls`), and the average is null because
        # there is no sample -- not 0, which would be a context measurement.
        empty = {
            "calls": 0, "input": 0, "cache_read": 0, "cache_write": 0,
            "output": 0, "avg_context": None, "context_calls": 0,
            "unmeasured_calls": 0,
        }

        def bucket(kind: str) -> dict[str, Any]:
            row = rows.get(kind)
            if row is None:
                return dict(empty)
            row.pop("source_kind")
            return row

        window_sessions = (
            "SELECT DISTINCT session_id FROM api_calls WHERE ts >= ? AND ts < ?"
        )
        cov = dict(self.conn.execute(
            "SELECT COUNT(DISTINCT session_id) sessions,"
            " COUNT(DISTINCT CASE WHEN source_kind = ? THEN session_id END)"
            "   sessions_with_subagent_transcripts,"
            # COALESCE so an empty `ingest_state` yields 0 rather than NULL:
            # this one IS a real zero (no files, corroborated), and the UI
            # calls toLocaleString() on it.
            " COALESCE(SUM(source_kind = ?), 0) subagent_files"
            " FROM ingest_state"
            f" WHERE session_id IN ({window_sessions})",
            (SOURCE_SUBAGENT, SOURCE_SUBAGENT, start, end),
        ).fetchone())
        # Window-scoped on the run's OWN `dispatched_at`, not on whether its
        # session happens to have in-window calls -- a long session would
        # otherwise drag every one of its reaped runs into every window.
        gone = self.conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT session_id) sessions"
            " FROM subagent_runs"
            " WHERE status = ? AND dispatched_at >= ? AND dispatched_at < ?",
            (STATUS_UNAVAILABLE, start, end),
        ).fetchone()
        cov["subagent_transcripts_unavailable"] = gone["n"]
        cov["sessions_with_unavailable_subagents"] = gone["sessions"]
        # Corpus-wide ON PURPOSE, and labelled as such: a run with no
        # `dispatched_at` belongs to no window, so it cannot be counted in one.
        # This is the residue after the window-scoping above, and it should be
        # ZERO on a healthy corpus -- a nonzero value means the task index
        # could not be stat'd, which is a real gap worth surfacing.
        cov["runs_undated_unavailable"] = self.conn.execute(
            "SELECT COUNT(*) n FROM subagent_runs"
            " WHERE status = ? AND dispatched_at IS NULL",
            (STATUS_UNAVAILABLE,),
        ).fetchone()["n"]
        return {
            "includes": (
                SCOPE_INCLUDES_BOTH
                if cov["sessions_with_subagent_transcripts"]
                else SCOPE_INCLUDES_MAIN_ONLY
            ),
            "main_thread": bucket(SOURCE_MAIN),
            "subagent": bucket(SOURCE_SUBAGENT),
            "coverage": cov,
            # A SECOND axis beside `includes`, never folded into it: "which
            # source kinds" and "which projects" are different questions, and
            # one string answering both would answer neither (#44).
            "projects": self._projects(start, end),
        }

    def _context(self, start: float, end: float) -> dict[str, Any]:
        """How big the window's calls were, and how much of the model's limit
        they used (#31).

        Two changes to one figure, for two independent reasons.

        **The median replaces the mean as the headline.** `AVG(context_size)`
        describes the tail's pull on an average, not a typical call: measured
        2026-08-05 over the reference corpus, mean 237,153 against median
        155,255 -- 1.53x -- with only 28.7% of calls above the mean. A card
        that 71.3% of calls fall below is not describing them. The mean stays
        in the payload beside `mean_over_median` and `share_above_mean`, which
        is the only thing it is good for here: it is EVIDENCE OF THE SKEW, and
        it is no longer the headline.

        **The bands give the figure a referent.** `context_size / window`
        against the model's own documented window (`context_window.py`), which
        is a hard published limit rather than an invented "healthy size". Where
        the boundaries sit is a dated product-owner judgment and says so; the
        window is documented and says so separately. Both provenances cross the
        API so the page can never present one as the other.

        **Zero-context rows are counted, never sampled or banded.** A row whose
        `input + cache_write + cache_read` is 0 carries no prompt accounting at
        all -- the population #25 is about. Measured 2026-08-05 over 38,381
        distinct calls, all 82 such rows carry a complete `usage` object whose
        token keys are present and zero, so they are faithfully stored records
        of a call that reported nothing, not a `usage` block the parser lost.
        Nothing is a measurement of a context, so the row stays out of the
        median, out of the mean and out of the bands, and is counted in
        `unmeasured_calls` instead. Banded, it would file as the most frugal
        call in the corpus; averaged in, it drags the figure down by exactly
        the proportion of rows nobody measured. `summary()`'s `avg_context`
        now ranges over the SAME sample, through the same predicate
        (`has_context_measurement()`), so the card and this block cannot
        disagree about what was measured.

        **An unknown model keeps its context and loses its utilisation.** Its
        size was measured, so it belongs in the distribution; its window is not
        something this tool knows, so it is counted in `unknown_model_calls`
        and NAMED in `unknown_models` rather than banded against a guess.
        `banded_calls` is therefore the denominator of every band share, and it
        is published beside them: bands + unknown + unmeasured is the window's
        whole call count, which `tests/test_serve.py` asserts against the
        summary card.

        One pass over the window's `(model, context_size)` pairs, banded in
        Python rather than in SQL, because the window lookup is a longest-prefix
        match no `GROUP BY` can express. Measured 2026-08-05 on a synthetic
        600k-call / 3,000-source database of the shape `_projects()` cites
        (macOS, warm cache, best of 5): 470 ms, inside a `summary()` of 2.8 s.
        The two shortcuts below are worth 480 ms of that -- the same loop costs
        950 ms with a `sqlite3.Row` built per row and an unmemoised lookup --
        and neither changes an answer.
        """
        sizes: list[int] = []
        banded = {key: 0 for key, *_ in BANDS}
        unknown_models: set[str] = set()
        unknown_model_calls = 0
        unmeasured_calls = 0
        over_window_calls = 0
        # Windows per model id, resolved once. A window holds a handful of
        # distinct ids and hundreds of thousands of calls, and the lookup walks
        # the whole table per call otherwise. Misses are cached too -- an
        # unknown model is the case that would otherwise pay full price on
        # every row.
        windows: dict[str, Optional[int]] = {}
        # A cursor with the connection's `sqlite3.Row` factory turned OFF: this
        # loop reads two columns positionally, and building a mapping per row
        # is the single largest cost in the block at corpus scale.
        cursor = self.conn.cursor()
        cursor.row_factory = None
        for model, size in cursor.execute(
            "SELECT model, context_size FROM api_calls WHERE ts >= ? AND ts < ?",
            (start, end),
        ):
            if not has_context_measurement(size):
                # The SAME predicate the four SQL means use (#25), not a
                # second spelling of it: this block and `summary()`'s
                # `avg_context` describe one sample, and two definitions free
                # to drift apart is how this defect recurs. It excludes a
                # negative as well as a zero -- a negative context is not
                # reachable from summed token counts, so a row carrying one is
                # broken, and the one thing it must not do is band as the most
                # frugal call on the report.
                unmeasured_calls += 1
                continue
            sizes.append(size)
            if model not in windows:
                windows[model] = window_for_model(model)
            window = windows[model]
            if window is None:
                unknown_model_calls += 1
                unknown_models.add(model)
                continue
            fraction = size / window
            if fraction > 1.0:
                # The loud half of this feature's safety story: a window this
                # table has let go stale shows up as calls over 100% of it,
                # which is absurd on its face -- but only if someone counts it.
                over_window_calls += 1
            banded[band_for(fraction)] += 1
        sizes.sort()
        percentiles = {f"p{p}": nearest_rank(sizes, p) for p in PERCENTILES}
        median = percentiles[f"p{MEDIAN_PERCENTILE}"]
        mean = (sum(sizes) / len(sizes)) if sizes else None
        # Strictly greater: a call AT the mean has not exceeded it.
        calls_above_mean = (
            len(sizes) - bisect.bisect_right(sizes, mean) if mean is not None else None
        )
        banded_calls = sum(banded.values())
        return {
            "sample_is": CONTEXT_SAMPLE,
            "sample_calls": len(sizes),
            "unmeasured_calls": unmeasured_calls,
            "median": median,
            "percentiles": percentiles,
            "mean": mean,
            # Null rather than 1.0 on an empty sample, and guarded against a
            # zero median that the sample cannot produce today but a future
            # change to what counts as measured could.
            "mean_over_median": (
                (mean / median) if (mean is not None and median) else None
            ),
            "calls_above_mean": calls_above_mean,
            "share_above_mean": (
                calls_above_mean / len(sizes) if calls_above_mean is not None else None
            ),
            "utilisation": {
                "windows_as_of": WINDOWS_AS_OF,
                "window_provenance": WINDOW_PROVENANCE,
                "bands_as_of": BANDS_AS_OF,
                "band_provenance": BAND_PROVENANCE,
                "banded_calls": banded_calls,
                "bands": [
                    {
                        "band": key,
                        "label": label,
                        "lower": lower,
                        "upper": upper,
                        "calls": banded[key],
                        # A share of an empty set is not 0% (rule #12).
                        "share": (banded[key] / banded_calls) if banded_calls else None,
                    }
                    for key, label, lower, upper in BANDS
                ],
                "unknown_model_calls": unknown_model_calls,
                "unknown_models": sorted(unknown_models),
                "over_window_calls": over_window_calls,
            },
        }

    def models(self, start: float, end: float) -> list[dict[str, Any]]:
        """Per-model token usage, split by scope -- the same model used by the
        main thread and by a subagent is TWO rows, never one merged figure."""
        return [
            {
                "model": r["model"],
                "scope": SCOPE_LABELS.get(r["source_kind"], r["source_kind"]),
                **{k: r[k] for k in r.keys() if k not in ("model", "source_kind")},
            }
            for r in self.conn.execute(
                "SELECT model, source_kind, COUNT(*) calls, SUM(input_tokens) input,"
                " SUM(cache_read) cache_read, SUM(cache_write) cache_write,"
                " SUM(output_tokens) output"
                " FROM api_calls WHERE ts >= ? AND ts < ?"
                " GROUP BY model, source_kind ORDER BY cache_read DESC",
                (start, end),
            )
        ]

    def agents(self, start: float, end: float, limit: int) -> list[dict[str, Any]]:
        """Per-DISPATCH token usage: which agent consumed what, ranked (#30).

        **Ranked by `RANKED_BY` -- total tokens -- which is the column
        `total_tokens` in the payload and the phrase in the panel heading.**
        The three used to be free to disagree, and did: the heading said "by
        spend" while this query ordered by `cache_read DESC`. Ordering here
        must stay the quantity `RANKED_BY` names.

        `model` sits beside the ranking because tokens are not tiers: an Opus
        dispatch can rank below a larger Haiku one, and the reader weighs that
        themselves rather than being handed a derived dollar figure to trust.
        A run spanning several models reports "N models", never one of them.

        **A reaped transcript is two states, not one (#41).** `subagent_runs`
        records only that the file is gone; whether we ever measured it is a
        different fact, and it is answered by this query's own LEFT JOIN:

          * no surviving call rows -- nothing was ever measured, so every
            figure stays NULL. A 0 there would file a reaped dispatch among the
            smallest ones, and `COUNT(*)` over an unmatched LEFT JOIN hands you
            exactly that plausible-looking zero. Reported as `unavailable`.
          * rows survive -- the source was archived rather than deleted after
            it had been ingested, so the measurement is present and this
            database is its only copy. It is reported in full, ranked on the
            total it actually carries, and flagged `STATUS_ARCHIVED` so the
            page can say those figures cannot be re-derived. Blanking them
            counted 9,003,000 tokens in the summary cards for a window whose
            panel reported that same dispatch as unmeasured.

        Like every figure here the distinction is WINDOW-SCOPED, and says so:
        `unavailable` means "we hold no call rows for this dispatch in this
        window", not "none exist anywhere". A reaped run measured only outside
        `[from, to)` is this window's gap, and the window it WAS measured in
        reports it as `archived` with its figures.

        The FIRST sort key is therefore "has no measurement", not the stored
        status: a run with nothing to rank by lands at the end deliberately
        rather than wherever a NULL happens to collate, while a run that has a
        number ranks on it. SQLite already collates NULL last under `DESC`, so
        that key changes no row today and no fixture can make it bite -- it is
        kept to STATE the property rather than depend on an ordering rule the
        query never says out loud. The blanking, the sort key and the status
        now read the SAME fact -- `n_calls` -- instead of three spellings free
        to disagree, which is how one of them (a hand-kept tuple of column
        aliases) came to be a one-word edit away from a fabricated figure.

        **Both sides of this query are scoped to the window.** Filtering
        only `api_calls` mixed two sets: a dispatch whose calls all fall
        outside `[from, to)` still came back, rendered as in-period activity
        under a panel whose empty state reads "No subagent dispatches in this
        period", and consumed a `LIMIT` slot that a real in-window dispatch
        needed. Same defect class as #4955, one layer down.

        A dispatch belongs to this window if EITHER holds: we have its
        measurements here, or its dispatch is dated here. Both are tested after
        grouping, because the first is not a property of the `subagent_runs`
        row. Gating the row itself on `dispatched_at` (a pre-GROUP `WHERE`)
        dropped a reaped run out of the very window its surviving calls fall
        in: that timestamp is the mtime of the task-index entry -- when the
        index was last touched, not when the agent ran -- so it routinely
        names a different day from the calls. Measured 2026-08-05 on the
        ranking fixture: for the day holding every call, the panel summed
        1,955,100 tokens against the cards' 10,958,100, the missing 9,003,000
        being one archived dispatch the `WHERE` had excluded.
        """
        rows = self.conn.execute(
            "SELECT r.agent_id, r.agent_type, r.description, r.status,"
            " r.spawn_depth, r.dispatching_session_id, r.storing_session_id,"
            " COALESCE(r.dispatching_session_id, r.storing_session_id, r.session_id)"
            "   session_id,"
            " COUNT(a.id) n_calls, SUM(a.input_tokens) input,"
            " SUM(a.cache_read) cache_read, SUM(a.cache_write) cache_write,"
            " SUM(a.output_tokens) output,"
            f" SUM({TOTAL_TOKENS_SQL}) total_tokens,"
            " COUNT(DISTINCT a.model) n_models,"
            " MIN(a.model) any_model, MIN(a.ts) first_ts, MAX(a.ts) last_ts"
            " FROM subagent_runs r"
            " LEFT JOIN api_calls a ON a.agent_id = r.agent_id"
            "   AND a.ts >= ? AND a.ts < ?"
            " GROUP BY r.agent_id"
            " HAVING COUNT(a.id) > 0"
            "   OR (r.status = ? AND r.dispatched_at >= ? AND r.dispatched_at < ?)"
            " ORDER BY n_calls = 0, total_tokens DESC, r.agent_id"
            " LIMIT ?",
            (start, end, STATUS_UNAVAILABLE, start, end, limit),
        ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            reaped = row.pop("status") == STATUS_UNAVAILABLE
            n_calls = row.pop("n_calls")
            n_models = row.pop("n_models")
            any_model = row.pop("any_model")
            row["calls"] = n_calls
            row["model"] = (
                None if n_models == 0
                else (any_model if n_models == 1 else f"{n_models} models")
            )
            if not n_calls:
                # Rule #12: for a run we hold no calls for, every MEASURED
                # figure stays NULL -- derived as "everything that is not
                # identity" so a column added to the SELECT above cannot be
                # forgotten here and ship a fabricated number.
                row = {
                    k: (v if k in AGENT_IDENTITY_FIELDS else None)
                    for k, v in row.items()
                }
            # Assigned last: `status` is the one non-identity field the
            # blanking must not touch, and it reads the same `n_calls` the
            # blanking and the sort key do.
            row["status"] = (
                (STATUS_UNAVAILABLE if not n_calls else STATUS_ARCHIVED)
                if reaped
                else STATUS_INGESTED
            )
            out.append(row)
        return out

    def summary(self, start: float, end: float) -> dict[str, Any]:
        """The window's headline figures.

        `avg_context` ranges over the calls that CARRY a context measurement,
        which is a strictly smaller set than `calls` and says so through
        `context_calls` / `unmeasured_calls` beside it (#25). It is therefore
        the same number as `context.mean`, over the same sample -- one
        definition of "measured", used by both.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) calls, COUNT(DISTINCT session_id) sessions,"
            " SUM(input_tokens) input, SUM(cache_read) cache_read,"
            " SUM(cache_write) cache_write, SUM(output_tokens) output,"
            " " + context_aggregate_sql() +
            " FROM api_calls WHERE ts >= ? AND ts < ?",
            (start, end),
        ).fetchone()
        return {
            **dict(row),
            "ingest": self._ingest_health(),
            "context": self._context(start, end),
            "scope": self._scope(start, end),
            "durability": self._durability(start, end),
            "models": self.models(start, end),
        }

    def timeseries(self, start: float, end: float, by: str) -> dict[str, Any]:
        """One point per day, in the window's own order.

        The fifth context mean, and the one whose failure was VISIBLE (#25):
        it is computed in Python rather than by `AVG`, so it shared none of
        the other four's SQL but all of their defect. On the reference corpus
        two days held exactly one call each, both of them rows carrying no
        measurement, and this series reported `calls: 1, avg_context: 0` --
        which the chart drew as a context collapse. A day with no measured
        call now yields null, which Chart.js leaves as a GAP in the line, and
        `context_calls` / `unmeasured_calls` say per point which it was.
        """
        rows = self.conn.execute(
            "SELECT a.ts, a.input_tokens, a.cache_read, a.cache_write,"
            " a.output_tokens, a.context_size, a.source_kind, t.turn_type"
            " FROM api_calls a LEFT JOIN turns t ON a.turn_id = t.id"
            " WHERE a.ts >= ? AND a.ts < ?",
            (start, end),
        ).fetchall()
        days: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        meta: dict[str, dict[str, int]] = defaultdict(
            lambda: {
                "calls": 0, "context": 0, "context_calls": 0,
                "unmeasured": 0, "main": 0, "subagent": 0,
            }
        )
        for r in rows:
            day = eastern_day(r["ts"])
            scope = SCOPE_LABELS.get(r["source_kind"], r["source_kind"])
            meta[day]["calls"] += 1
            # The call is counted either way -- it happened. Only the mean's
            # numerator and denominator are restricted to measured rows.
            if has_context_measurement(r["context_size"]):
                meta[day]["context"] += r["context_size"]
                meta[day]["context_calls"] += 1
            else:
                meta[day]["unmeasured"] += 1
            meta[day]["subagent" if scope == SCOPE_SUBAGENT else "main"] += 1
            tokens = (
                r["input_tokens"] + r["cache_read"] + r["cache_write"] + r["output_tokens"]
            )
            if by == "turntype":
                # A subagent call has no main-thread turn by construction
                # (#4966), so it gets its OWN bucket rather than silently
                # swelling "(no turn)" alongside genuinely unattributed calls.
                key = (
                    SCOPE_SUBAGENT
                    if scope == SCOPE_SUBAGENT
                    else (r["turn_type"] or "(no turn)")
                )
                days[day][key] += tokens
            elif by == "scope":
                days[day][scope] += tokens
            else:
                days[day]["input"] += r["input_tokens"]
                days[day]["cache_read"] += r["cache_read"]
                days[day]["cache_write"] += r["cache_write"]
                days[day]["output"] += r["output_tokens"]
        ordered = sorted(days)
        series_keys = sorted({k for d in days.values() for k in d})
        return {
            "days": ordered,
            "series": {k: [days[d].get(k, 0) for d in ordered] for k in series_keys},
            "calls": [meta[d]["calls"] for d in ordered],
            # Present under EVERY `by` mode: the main-vs-subagent split is a
            # property of the day, not of the chosen series breakdown.
            "main_thread_calls": [meta[d]["main"] for d in ordered],
            "subagent_calls": [meta[d]["subagent"] for d in ordered],
            "avg_context": [
                (meta[d]["context"] / meta[d]["context_calls"])
                if meta[d]["context_calls"]
                else None
                for d in ordered
            ],
            # The mean's own sample size, per point, beside the call count it
            # is NOT the same as -- CLAUDE.md's "count samples separately from
            # the aggregate", and the only way a reader can tell a gap that
            # means "no calls" from one that means "no measurement".
            "context_calls": [meta[d]["context_calls"] for d in ordered],
            "unmeasured_calls": [meta[d]["unmeasured"] for d in ordered],
        }

    def sessions(self, start: float, end: float) -> list[dict[str, Any]]:
        """The window's sessions, listed from `api_calls`.

        A session known ONLY through a reaped subagent run is not listed here:
        it has no `api_calls` row, so there are no token columns to show for
        it. Its gap is not lost -- `_scope()`'s coverage reports the reaped
        runs for this window (dated by `subagent_runs.dispatched_at`) plus any
        that carry no timestamp at all.

        `avg_context` is the session's mean over its MEASURED calls only, with
        `context_calls` / `unmeasured_calls` beside it (#25): a session whose
        in-window calls all measured nothing gets null, not 0.

        `subagent_status` is what keeps the subagent-call COUNT honest, and it
        is five-valued on purpose: `measured`, `unavailable` (a reaped dispatch
        DATED INTO THIS WINDOW -- the count is a lower bound),
        `unavailable-undated` (a reaped dispatch that cannot be placed in any
        window), `none` (the index was scanned and lists no dispatch -- a real
        zero) and `unknown` (nothing was scanned).
        """
        rows = self.conn.execute(
            "SELECT a.session_id id, MIN(a.ts) first_ts, MAX(a.ts) last_ts,"
            " COUNT(*) calls, SUM(a.input_tokens) input, SUM(a.cache_read) cache_read,"
            " SUM(a.cache_write) cache_write, SUM(a.output_tokens) output,"
            " SUM(a.source_kind = ?) subagent_calls,"
            " SUM(a.source_kind <> ?) main_thread_calls,"
            " " + context_aggregate_sql("a.context_size") +
            " FROM api_calls a WHERE a.ts >= ? AND a.ts < ?"
            " GROUP BY a.session_id ORDER BY last_ts DESC",
            (SOURCE_SUBAGENT, SOURCE_SUBAGENT, start, end),
        ).fetchall()
        dispatches = dict(
            self.conn.execute(
                "SELECT d.session_id, COUNT(*) FROM agent_dispatches d"
                " JOIN turns t ON d.turn_id = t.id"
                " WHERE t.ts >= ? AND t.ts < ?"
                " GROUP BY d.session_id",
                (start, end),
            ).fetchall()
        )
        # Five distinct facts, never collapsed into one "0" (rule #12):
        #   unavailable -- a dispatch DATED INTO THIS WINDOW is known but its
        #                  transcript is gone, so this window's subagent total
        #                  is INCOMPLETE;
        #   unavailable-undated -- same gap, but the dispatch carries no
        #                  timestamp, so whether it falls in this window is
        #                  itself unknown;
        #   measured    -- subagent transcripts were read;
        #   none        -- the task index was scanned and lists no dispatch;
        #   unknown     -- nothing was scanned, so we simply cannot say.
        # WINDOW-SCOPED, on the run's own `dispatched_at` (the task-index
        # entry's mtime -- the only timestamp a reaped run has). Corpus-wide,
        # this marked a long session UNAVAILABLE on every day it appeared,
        # including days its reaped agent never ran (Sentry finding).
        gone = {
            r["session_id"]
            for r in self.conn.execute(
                "SELECT DISTINCT session_id FROM subagent_runs"
                " WHERE status = ? AND dispatched_at >= ? AND dispatched_at < ?",
                (STATUS_UNAVAILABLE, start, end),
            )
        }
        # A run whose index entry could not be stat'd has NO timestamp, so it
        # can be neither placed in this window nor ruled out of it. It gets its
        # own status rather than being rounded into either -- rounding toward
        # `gone` re-creates the over-report above, and rounding toward
        # `measured` claims a completeness nothing established (rule #12).
        undatable = {
            r["session_id"]
            for r in self.conn.execute(
                "SELECT DISTINCT session_id FROM subagent_runs"
                " WHERE status = ? AND dispatched_at IS NULL",
                (STATUS_UNAVAILABLE,),
            )
        }
        scanned = {
            r["session_id"]
            for r in self.conn.execute("SELECT session_id FROM task_index_sessions")
        }

        def status(session_id: str, subagent_calls: int) -> str:
            if session_id in gone:
                return STATUS_UNAVAILABLE
            if session_id in undatable:
                return STATUS_UNAVAILABLE_UNDATED
            if subagent_calls:
                return "measured"
            return "none" if session_id in scanned else "unknown"

        return [
            {
                **dict(r),
                "dispatches": dispatches.get(r["id"], 0),
                "subagent_status": status(r["id"], r["subagent_calls"]),
            }
            for r in rows
        ]

    def session_detail(self, session_id: str) -> dict[str, Any]:
        session = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise KeyError(session_id)
        turn_types = [
            dict(r)
            for r in self.conn.execute(
                "SELECT t.turn_type, COUNT(DISTINCT t.id) turns, COUNT(a.id) calls,"
                " SUM(a.input_tokens) input, SUM(a.cache_read) cache_read,"
                " SUM(a.cache_write) cache_write, SUM(a.output_tokens) output"
                " FROM turns t LEFT JOIN api_calls a ON a.turn_id = t.id"
                " WHERE t.session_id = ? GROUP BY t.turn_type ORDER BY cache_read DESC",
                (session_id,),
            )
        ]
        models = [
            dict(r)
            for r in self.conn.execute(
                "SELECT model, COUNT(*) calls, SUM(input_tokens) input,"
                " SUM(cache_read) cache_read, SUM(cache_write) cache_write,"
                " SUM(output_tokens) output"
                " FROM api_calls WHERE session_id = ? GROUP BY model ORDER BY cache_read DESC",
                (session_id,),
            )
        ]
        # Two DIFFERENT quantities per agent type, never one reconciled figure
        # (#10). `agent_dispatches.subagent_tokens` -- the `<subagent_tokens>`
        # tag the dispatching turn carries -- is the subagent's PEAK CONTEXT, a
        # high-water mark, not what the dispatch cost.
        #
        # Measured 2026-08-02 on one machine's corpus (2026-06-06..2026-08-02,
        # 45 sessions, 1,774 dispatches carrying both figures): the tag over
        # `MAX(api_calls.context_size)` for the same agent has median 1.004
        # (p5 1.001, p25 1.002, p75 1.006, p95 1.012), and 1,740 of 1,774 --
        # 98.1% -- land within +-5% of 1.0. The same tag over transcript-
        # measured tokens has median 0.011: measured spend is 51.6x larger.
        # This is an EMPIRICAL finding about the harness's tag, not a documented
        # contract, which is why tests/test_serve.py pins the relationship --
        # a harness that changes the tag's meaning must go red, not re-diverge
        # silently.
        #
        # Hence MAX, never SUM. Summing high-water marks yields a quantity that
        # means nothing (265M summed against 23.4B measured on that corpus, an
        # 88x artifact) -- and it was rendered under an "Agent dispatch spend"
        # heading, understating dispatch spend by ~50x at the median.
        #
        # Spend comes from the transcripts instead, joined agent-side
        # (`api_calls.agent_id` == the dispatch's `<task-id>`), pre-aggregated
        # so the 1:1 join cannot fan out and inflate `dispatches`.
        #
        # Absence survives both columns: MAX() over an all-NULL group is NULL,
        # and SUM() over unmatched LEFT JOIN rows is NULL, so "no dispatch
        # reported a peak" and "no transcript was measured" stay distinct from
        # a real 0 -- each with its own count so a PARTIAL figure reads as the
        # lower bound it is.
        agent_types = [
            dict(r)
            for r in self.conn.execute(
                "SELECT COALESCE(d.agent_type, '(unknown)') agent_type,"
                " COUNT(*) dispatches,"
                " MAX(d.subagent_tokens) peak_context_tokens,"
                " SUM(d.subagent_tokens IS NULL) dispatches_without_peak,"
                " SUM(m.tokens) measured_tokens,"
                " SUM(m.agent_id IS NULL) dispatches_without_spend"
                " FROM agent_dispatches d"
                " LEFT JOIN (SELECT agent_id,"
                f"     SUM({TOTAL_TOKENS_SQL}) tokens"
                "   FROM api_calls WHERE agent_id IS NOT NULL GROUP BY agent_id) m"
                "   ON m.agent_id = d.task_id"
                " WHERE d.session_id = ?"
                " GROUP BY d.agent_type"
                " ORDER BY measured_tokens IS NULL, measured_tokens DESC,"
                "  peak_context_tokens IS NULL, peak_context_tokens DESC,"
                "  agent_type",
                (session_id,),
            )
        ]
        outlier_turns = [
            dict(r)
            for r in self.conn.execute(
                "SELECT t.id turn_id, t.turn_type, t.preview, t.ts,"
                " COUNT(a.id) calls, SUM(a.cache_read) cache_read"
                " FROM turns t JOIN api_calls a ON a.turn_id = t.id"
                " WHERE t.session_id = ? GROUP BY t.id"
                " ORDER BY cache_read DESC LIMIT 10",
                (session_id,),
            )
        ]
        scopes = [
            {
                "scope": SCOPE_LABELS.get(r["source_kind"], r["source_kind"]),
                **{k: r[k] for k in r.keys() if k != "source_kind"},
            }
            for r in self.conn.execute(
                "SELECT source_kind, COUNT(*) calls, SUM(input_tokens) input,"
                " SUM(cache_read) cache_read, SUM(cache_write) cache_write,"
                " SUM(output_tokens) output, " + context_aggregate_sql() +
                " FROM api_calls WHERE session_id = ?"
                " GROUP BY source_kind ORDER BY source_kind",
                (session_id,),
            )
        ]
        return {
            "session": dict(session),
            "scopes": scopes,
            "turn_types": turn_types,
            "models": models,
            "agent_types": agent_types,
            "outlier_turns": outlier_turns,
        }

    def outliers(self, start: float, end: float, limit: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT a.session_id, a.ts, a.model, a.cache_read, a.cache_write,"
                " a.input_tokens, a.output_tokens, a.context_size, a.is_sidechain,"
                " t.turn_type, t.preview"
                " FROM api_calls a LEFT JOIN turns t ON a.turn_id = t.id"
                " WHERE a.ts >= ? AND a.ts < ? ORDER BY a.cache_read DESC LIMIT ?",
                (start, end, limit),
            )
        ]


def make_handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            # Reject any request whose Host header isn't loopback BEFORE
            # doing anything else: the bind address (127.0.0.1) blocks a
            # remote connection, but a page in the user's browser can still
            # reach this server via DNS rebinding and read turn previews
            # (CodeRabbit security finding).
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in LOOPBACK_HOSTS:
                self._respond(403, b"bad host", "text/plain")
                return
            parsed = urlparse(self.path)
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                if parsed.path == "/":
                    body = (HERE / "index.html").read_bytes()
                    self._respond(200, body, "text/html; charset=utf-8")
                    return
                if parsed.path.startswith("/vendor/"):
                    self._serve_vendor_asset(parsed.path[len("/vendor/"):])
                    return
                if not parsed.path.startswith("/api/"):
                    self._respond(404, b"not found", "text/plain")
                    return
                payload = self._route(parsed.path, params)
                self._respond(
                    200, json.dumps(payload).encode(), "application/json"
                )
            except ValueError as exc:
                self._respond(
                    400, json.dumps({"error": str(exc)}).encode(), "application/json"
                )
            except KeyError as exc:
                self._respond(
                    404,
                    json.dumps({"error": f"not found: {exc.args[0] if exc.args else ''}"}).encode(),
                    "application/json",
                )
            except Exception as exc:  # noqa: BLE001 -- local tool, never drop the response
                # An uncaught exception (e.g. sqlite3.OperationalError from a
                # stale schema) otherwise closes the connection with no
                # status line; the browser fetch rejects with no handler and
                # loadAll() silently stops updating (CodeRabbit finding).
                self._respond(
                    500,
                    json.dumps({"error": f"internal error: {type(exc).__name__}"}).encode(),
                    "application/json",
                )

        def _route(self, path: str, params: dict[str, str]) -> Any:
            start, end = day_bounds(params.get("from"), params.get("to"))
            if path == "/api/summary":
                return api.summary(start, end)
            if path == "/api/timeseries":
                by = params.get("by", "class")
                if by not in ("class", "turntype", "scope"):
                    raise ValueError("by must be class, turntype or scope")
                return api.timeseries(start, end, by)
            if path == "/api/sessions":
                return api.sessions(start, end)
            if path == "/api/session":
                if "id" not in params:
                    # A missing required parameter is a 400 (malformed
                    # request), never the 404 a KeyError would map to (that
                    # collides with "session id not found") (Sentry finding).
                    raise ValueError("missing required parameter: id")
                return api.session_detail(params["id"])
            if path == "/api/outliers":
                return api.outliers(start, end, clamp_limit(params.get("limit")))
            if path == "/api/agents":
                return api.agents(start, end, clamp_limit(params.get("limit")))
            raise KeyError(path)

        def _serve_vendor_asset(self, rel_path: str) -> None:
            """Serve a vendored static asset (e.g. Chart.js) from `vendor/`.

            Resolves symlinks/`..` and confirms the result stays inside
            VENDOR_DIR before reading, so a crafted `/vendor/../../etc/passwd`
            path cannot escape the vendor directory.
            """
            target = (VENDOR_DIR / rel_path).resolve()
            if target != VENDOR_DIR and VENDOR_DIR not in target.parents:
                self._respond(404, b"not found", "text/plain")
                return
            if not target.is_file():
                self._respond(404, b"not found", "text/plain")
                return
            content_type = (
                "application/javascript" if target.suffix == ".js" else "application/octet-stream"
            )
            self._respond(200, target.read_bytes(), content_type)

        def _respond(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            pass  # quiet by default; a local dev tool

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the usage-report UI")
    parser.add_argument("--db", type=Path, default=HERE / "db" / "usage.db")
    parser.add_argument("--port", type=int, default=8377)
    args = parser.parse_args()

    db_path = args.db.expanduser()
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path} -- run ingest.py first")
    server = HTTPServer(("127.0.0.1", args.port), make_handler(Api(db_path)))
    print(f"usage-report: http://127.0.0.1:{args.port}/ (db: {db_path})")
    server.serve_forever()


if __name__ == "__main__":
    main()
