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
                " SUM(output_tokens) output, AVG(context_size) avg_context"
                " FROM api_calls WHERE ts >= ? AND ts < ? GROUP BY source_kind",
                (start, end),
            )
        }
        empty = {
            "calls": 0, "input": 0, "cache_read": 0, "cache_write": 0,
            "output": 0, "avg_context": None,
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

        An `unavailable` run (its transcript reaped from /private/tmp) is
        listed with NULL figures, never zeros -- the operator must be able to
        see that an agent ran and that its usage is unknown. The FIRST sort key
        is the status, so unmeasured runs land at the end deliberately rather
        than wherever a NULL happens to collate; they read as an explicit gap
        rather than as the smallest dispatches.

        **Both sides of the join are restricted to the window.** Filtering
        only `api_calls` mixed two sets: a dispatch whose calls all fall
        outside `[from, to)` still came back, rendered as in-period activity
        under a panel whose empty state reads "No subagent dispatches in this
        period", and consumed a `LIMIT` slot that a real in-window dispatch
        needed. So an `ingested` run must have at least one in-window call,
        and an `unavailable` run must belong to a session that is itself in
        the window -- otherwise it is not this period's gap. Same defect class
        as #4955, one layer down.
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
            " WHERE r.status <> ?"
            "   OR (r.dispatched_at >= ? AND r.dispatched_at < ?)"
            " GROUP BY r.agent_id"
            " HAVING COUNT(a.id) > 0 OR r.status = ?"
            " ORDER BY r.status = ?, total_tokens DESC, r.agent_id"
            " LIMIT ?",
            (start, end, STATUS_UNAVAILABLE, start, end,
             STATUS_UNAVAILABLE, STATUS_UNAVAILABLE, limit),
        ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            unavailable = row.pop("status") == STATUS_UNAVAILABLE
            n_calls = row.pop("n_calls")
            n_models = row.pop("n_models")
            any_model = row.pop("any_model")
            row["status"] = STATUS_UNAVAILABLE if unavailable else STATUS_INGESTED
            # Rule #12: for an unmeasured run every figure stays NULL. A
            # LEFT JOIN naturally yields COUNT(*) == 0, which is exactly the
            # plausible-looking zero this issue exists to stop.
            row["calls"] = None if unavailable else n_calls
            row["model"] = (
                None if unavailable or n_models == 0
                else (any_model if n_models == 1 else f"{n_models} models")
            )
            if unavailable:
                # Including `total_tokens`, the key this panel RANKS on: a 0
                # there would file a reaped dispatch among the smallest ones
                # instead of showing it as unmeasured.
                for key in ("input", "cache_read", "cache_write", "output",
                            "total_tokens", "first_ts", "last_ts"):
                    row[key] = None
            out.append(row)
        return out

    def summary(self, start: float, end: float) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT COUNT(*) calls, COUNT(DISTINCT session_id) sessions,"
            " SUM(input_tokens) input, SUM(cache_read) cache_read,"
            " SUM(cache_write) cache_write, SUM(output_tokens) output,"
            " AVG(context_size) avg_context"
            " FROM api_calls WHERE ts >= ? AND ts < ?",
            (start, end),
        ).fetchone()
        return {
            **dict(row),
            "ingest": self._ingest_health(),
            "scope": self._scope(start, end),
            "durability": self._durability(start, end),
            "models": self.models(start, end),
        }

    def timeseries(self, start: float, end: float, by: str) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT a.ts, a.input_tokens, a.cache_read, a.cache_write,"
            " a.output_tokens, a.context_size, a.source_kind, t.turn_type"
            " FROM api_calls a LEFT JOIN turns t ON a.turn_id = t.id"
            " WHERE a.ts >= ? AND a.ts < ?",
            (start, end),
        ).fetchall()
        days: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        meta: dict[str, dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "context": 0, "main": 0, "subagent": 0}
        )
        for r in rows:
            day = eastern_day(r["ts"])
            scope = SCOPE_LABELS.get(r["source_kind"], r["source_kind"])
            meta[day]["calls"] += 1
            meta[day]["context"] += r["context_size"]
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
                (meta[d]["context"] / meta[d]["calls"]) if meta[d]["calls"] else None
                for d in ordered
            ],
        }

    def sessions(self, start: float, end: float) -> list[dict[str, Any]]:
        """The window's sessions, listed from `api_calls`.

        A session known ONLY through a reaped subagent run is not listed here:
        it has no `api_calls` row, so there are no token columns to show for
        it. Its gap is not lost -- `_scope()`'s coverage reports the reaped
        runs for this window (dated by `subagent_runs.dispatched_at`) plus any
        that carry no timestamp at all.

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
            " AVG(a.context_size) avg_context"
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
                " SUM(output_tokens) output, AVG(context_size) avg_context"
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
