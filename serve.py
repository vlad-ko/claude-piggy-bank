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
from typing import Any, Iterable, Optional
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
    SHAPE_TABLE,
    SOURCE_MAIN,
    SOURCE_SUBAGENT,
    STATUS_INGESTED,
    STATUS_UNAVAILABLE,
    SUBAGENTS_DIR,
    TASKS_DIR,
    census_coverage,
)
from recommendations import (
    METRIC_CACHE_READS_PER_WRITE,
    METRIC_CACHE_WRITE_ONLY_SHARE,
    METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
    METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW,
    METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,
    METRICS,
    RANKING_PROVENANCE,
    RECOMMENDATION_PROVENANCE,
    RECOMMENDATIONS_AS_OF,
    SAMPLE_FLOOR_AS_OF,
    SAMPLE_MEASURED,
    SAMPLE_STATES,
    SAMPLE_UNDER_SAMPLED,
    SAMPLE_UNMEASURED,
    SEVERITY_ACT,
    SEVERITY_OK,
    SEVERITY_RANK,
    SEVERITY_WATCH,
    UNDER_SAMPLED_NOTE,
    UNMEASURED_NOTE,
    WORSE_WHEN_HIGHER,
    Assessment,
    Assessments,
    Lever,
    Metric,
    Provenance,
    Reading,
    UnderSampled,
    assess_all,
    cache_write_repayment,
    depth_in_band,
)

# WHICH BUILD PRODUCED THESE NUMBERS (#92, closing #21's last criterion).
#
# The report renders figures a reader may file a bug about, and until now
# neither the page nor the payload could say which build computed them --
# `grep -c VERSION serve.py index.html` returned 0 and 0.
#
# It is IMPORTED, never restated. A second copy of a version string is the
# defect `DocsStateTheShippedVersionTest` already exists for, twice over: the
# docs drifted from `cpb.VERSION` at 1.0.0 -> 1.1.0 and again at 1.1.0 ->
# 1.2.0, and a copy in the payload would drift the same way while looking
# authoritative.
#
# **No import cycle, checked rather than assumed.** `cpb.py` imports only
# `importlib`, `sys` and `typing` at module level; it reaches `ingest` and
# `serve` through `importlib.import_module` inside `_dispatch()`, at call time.
# So `serve -> cpb` closes no loop. (Under `python3 cpb.py serve` the entry
# point is `__main__` and this import loads `cpb.py` a second time under its
# own name -- an extra module object holding one string literal, with no
# side effects at import.)
#
# The module is held rather than the constant, and read per request, so a test
# can patch `cpb.VERSION` and see the payload move. Binding the string here
# would make a restated literal indistinguishable from a read one.
try:
    import cpb as _cpb
except ImportError:  # pragma: no cover -- see `cpb_version()`
    _cpb = None  # type: ignore[assignment]


def cpb_version() -> Optional[str]:
    """The build that produced these figures, or None if it cannot be read.

    None is not a benign default and is not swallowing anything: it is the
    honest reading when `cpb.py` is not beside this file. `serve.py` is a
    supported entry point in its own right (`docs/versioning.md` clause 1) and
    ran without `cpb.py` before this constant existed, so a checkout that
    carries `serve.py`, `ingest.py`, `context_window.py` and
    `recommendations.py` still serves -- and says its build is UNKNOWN rather
    than naming a plausible one. A version invented for a bug report is worse
    than no version, because the reader cannot see the error.
    """
    return getattr(_cpb, "VERSION", None) if _cpb is not None else None


EASTERN = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
VENDOR_DIR = (HERE / "vendor").resolve()

# THE TIE BETWEEN TWO ENUMERATIONS OF ONE SET (#84). `recommendations.METRICS`
# DECLARES the metrics the table assesses; `_recommendations()` COMPUTES a value
# for each. Until this constant existed, nothing connected them -- and the cost
# of that gap is on the record rather than hypothetical: the branch that added
# the fifth metric ran a fully green suite, because at the commit it forked from
# `serve.py` contained no reference to `assess_all()` at all, and the
# incompleteness appeared only when a request was served over a merged tree.
#
# This is `RANKED_BY`'s discipline one level over. There a heading and an
# `ORDER BY` were free to disagree and did, for a whole release; here a table
# and its only caller were. So the set is named ONCE, checked against the table
# AT IMPORT by `_refuse_unwired_metrics()` below, and asserted equal in
# tests/test_serve.py. A metric added to the table and not wired here now fails
# the moment anything imports this module.
RECOMMENDED_METRICS = frozenset(
    {
        METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW,
        METRIC_CACHE_READS_PER_WRITE,
        METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
        METRIC_CACHE_WRITE_ONLY_SHARE,
        METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,
    }
)


def _refuse_unwired_metrics(declared: frozenset[str], table: frozenset[str]) -> None:
    """Refuse to import if the two enumerations of the metric set disagree.

    Both directions, and they are not the same mistake. A metric the table
    declares and this module never computes reaches `assess_all()` as a partial
    mapping, which it rightly refuses -- so the whole payload 500s, and every
    figure on the page goes with it. A key this module supplies that the table
    does not declare is the quieter one: arithmetic run, a query paid for, and
    a value nobody assesses. Neither is caught by `assess_all()` alone, which
    sees only the mapping it is handed and only when a request builds one.

    AT IMPORT rather than at request time, deliberately. `assess_all()`'s
    refusal cannot fire until a summary is served, so it is invisible to any
    suite that never serves one -- which is exactly how #84 came to be green in
    isolation and red on merge. Raising here fails `import serve`, and every
    test module that imports it, on the branch where the metric was added.

    `RuntimeError` rather than `SystemExit`: this is a wiring defect in the
    repository, not a refusal to answer a question about the user's data, and
    the traceback naming this function is the whole diagnosis.
    """
    unwired = sorted(table - declared)
    undeclared = sorted(declared - table)
    if unwired or undeclared:
        raise RuntimeError(
            "serve.RECOMMENDED_METRICS and recommendations.METRICS disagree: "
            f"declared by the table and computed nowhere here: {unwired}; "
            f"computed here and declared by no table entry: {undeclared}. "
            "One of the two is the set that ships; make them the same set."
        )


_refuse_unwired_metrics(RECOMMENDED_METRICS, frozenset(METRICS))

# --------------------------------------------------------------------------
# #89: the summary level -- the four-dot status strip and the knob rows.
#
# Three levels over ONE payload: the summary (what do I do?), the four question
# cards (why?), and the raw data (show me everything). Everything this block
# adds is a READING of something already computed for the other two levels --
# `health`, `context`, and the `recommendations` table -- never a second
# derivation of it. That is why `_status()` takes those three blocks as
# arguments, exactly as `_health()` and `_recommendations()` take `context`: a
# strip that ran its own queries would be a second opinion on the numbers it
# summarises, and a summary that disagreed with the level below it is the one
# defect a three-level page makes easy.
#
# NO THRESHOLD IS AUTHORED HERE OR IN THE PAGE. Every number a gauge draws --
# where an arc starts and stops, where a tick sits, what the reader should aim
# for -- is a boundary in `recommendations.METRICS`, carried across with the
# provenance that boundary already has. The one thing added is GEOMETRY: which
# fraction of a semicircular sweep a value sits at, which is arithmetic over
# the table's own ordered ranges.
# --------------------------------------------------------------------------

# Which metrics the summary's cache dot ranges over. A SECOND enumeration of a
# subset of one set, so it gets `RECOMMENDED_METRICS`' treatment rather than
# its own habits: checked against the wired set AT IMPORT, because a cache
# metric added to the table and not named here would leave the dot reporting
# "working" over a reading it never looked at -- the milder of two true
# statements, chosen by an omission.
#
# It carries NO number. Which metrics measure the cache is a statement about
# what was divided by what; where a cache reading stops being healthy is the
# table's, and this set never decides it.
CACHE_METRICS = frozenset(
    {
        METRIC_CACHE_READS_PER_WRITE,
        METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
        METRIC_CACHE_WRITE_ONLY_SHARE,
    }
)


def _refuse_ungrouped_cache_metrics(
    cache: frozenset[str], wired: frozenset[str]
) -> None:
    """Refuse to import if the cache group names a metric nothing computes."""
    stray = sorted(cache - wired)
    if stray:
        raise RuntimeError(
            f"serve.CACHE_METRICS names {stray}, which serve.RECOMMENDED_METRICS "
            "does not: a dot cannot report on a reading nothing measures."
        )


_refuse_ungrouped_cache_metrics(CACHE_METRICS, RECOMMENDED_METRICS)

# WHICH METRIC THE CONTEXT DOT ANSWERS TO (#93, second pass).
#
# The dot asks "Wasting context?" and that is a JUDGMENT, not an observation.
# The observation underneath it -- did any call in this period reach half its
# model's documented window -- is complete over whatever ran, however little
# ran, and `_context()` answers it honestly on any sample. Turning that into
# "No, you are not wasting context" is a different claim, and it is the same
# claim `main_thread_share_over_half_window` makes; so it is owed the same
# floor.
#
# THIS WAS SHIPPED WRONG AND CAUGHT IN REVIEW. The first pass exempted this dot
# on the grounds that its verdict is a complete statement over the period. That
# is true of the question the CARD asks and false of the question the DOT asks,
# and the two sat on one screen contradicting each other: a green "Wasting
# context? -- No" directly above a row reading `TOO FEW -
# main_thread_share_over_half_window - 5 of 11`. A reader cannot hold both.
#
# Named here rather than reached for inside `_status()`, and checked against
# the wired set at import, for `CACHE_METRICS`' reason: a dot that answers to a
# metric nothing computes would report a verdict it never looked at.
CONTEXT_DOT_METRIC = METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW


def _refuse_unbacked_context_dot(metric: str, wired: frozenset[str]) -> None:
    """Refuse to import if the context dot answers to nothing this file computes."""
    if metric not in wired:
        raise RuntimeError(
            f"serve.CONTEXT_DOT_METRIC is {metric!r}, which "
            "serve.RECOMMENDED_METRICS does not name: a dot cannot inherit the "
            "floor of a reading nothing measures."
        )


_refuse_unbacked_context_dot(CONTEXT_DOT_METRIC, RECOMMENDED_METRICS)

# WHAT KIND OF NUMBER EACH READING IS -- `30.3%` rather than `0.3034`, `3.20x`
# rather than `3.195` -- IS NOT DECLARED HERE ANY MORE. It shipped as a
# `METRIC_UNITS` mapping in this file, under an import-time totality guard,
# because the branch that added it could not edit `recommendations.py`. The
# unit is a property of the metric, so it now sits on `recommendations.Metric`
# beside `measurement`, and the guard's property survives in a stronger form: a
# metric with no unit, or one outside `METRIC_UNIT_KINDS`, cannot be
# CONSTRUCTED. Everything below reads `METRICS[key].unit`; a second mapping
# here would be one enumeration of the metric set too many, which is the defect
# `RECOMMENDED_METRICS` exists to prevent.


def _refuse_unhandled_states(
    what: str, handled: Iterable[str], declared: Iterable[str]
) -> None:
    """Refuse to import unless a state table covers its vocabulary exactly.

    Both directions. A state with no entry is the one a `.get()` default would
    render as whichever verdict was convenient; an entry for a state that no
    longer exists is a translation nothing can reach, and it makes the table
    look more complete than it is.
    """
    handled, declared = frozenset(handled), frozenset(declared)
    if handled != declared:
        raise RuntimeError(
            f"{what} does not cover its states exactly: unhandled "
            f"{sorted(declared - handled)}, unknown {sorted(handled - declared)}"
        )


# What the model-mix observation ranges over, named ONCE and carried in the
# payload -- `CONTEXT_SAMPLE`'s rule for a figure that is not a recommendation.
#
# IT IS AN OBSERVATION AND NOT ADVICE, and the distinction is an owner
# decision rather than a presentation choice: routing work to a weaker model to
# save tokens can cost more in rework than it saves, and CPB measures tokens,
# not rework. So this block carries no severity, no lever, no target and no
# direction -- there is nothing here that could be turned into a knob by a
# later change without somebody deciding to -- and it is deliberately NOT a
# member of `recommendations.METRICS`, whose entries all carry exactly those
# things.
MODEL_MIX_SAMPLE = (
    "API calls in this window, both scopes, grouped by the model each one "
    "names -- one row per model, not per model and scope"
)

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
# The order every per-scope list is emitted in, and -- more importantly -- the
# set that is emitted WHETHER OR NOT the window holds any such call. A scope
# that ran nothing must appear and say so; a missing key is an absence the
# reader cannot see. `_scope()` makes the same guarantee through its `bucket()`.
SCOPE_ORDER = (SOURCE_MAIN, SOURCE_SUBAGENT)

# #61: why a per-scope band tally has no sample, spelled ONCE and non-null
# exactly when that scope's `banded_calls` is 0. Three different states of
# knowledge read identically as four bands of `calls: 0`, and they have three
# different remedies -- so the payload names which one it is rather than
# leaving the page to infer it from a zero (the same tri-state-with-a-reason
# shape `stale_unknown_reason` uses).
UTIL_NO_SAMPLE_NO_CALLS = "this scope ran no calls in this window"
UTIL_NO_SAMPLE_NO_CONTEXT_MEASUREMENT = (
    "this scope ran calls, none of which carried a context measurement"
)
UTIL_NO_SAMPLE_NO_DOCUMENTED_WINDOW = (
    "every measured call in this scope ran on a model with no documented "
    "context window, so none of them has a utilisation"
)

# ---------------------------------------------------------------------------
# #64: the health VERDICT -- the reassurance the report never stated.
# ---------------------------------------------------------------------------
#
# The page reported 8,163 records parsed and 0 unparsed and then never said
# "nothing is broken". A reader scanning for alarm had to infer reassurance
# from the ABSENCE OF A WARNING -- which is exactly the inference this
# repository refuses to let a number make. The reassuring answer is a real
# measurement, so it is stated as one.
#
# THREE STATES, NOT TWO, and the third is the whole point. "Nothing is broken"
# and "we could not check" are different claims with different remedies, and a
# verdict that collapsed them would report health over a corpus nobody has
# looked at -- `source_shape`'s own rule (a source with no rows has not been
# CENSUSED rather than been found CLEAN) applied to the report as a whole.
HEALTH_OK = "ok"
HEALTH_UNCHECKED = "unchecked"
HEALTH_FAILED = "failed"
# WORST FIRST. The verdict is the worst state any check reached, so a genuine
# failure outranks the reassurance and is never averaged with it. Named as an
# ordered tuple rather than compared with `<`, because the ordering is a
# judgment about which claim matters more and not a property of the strings.
HEALTH_ORDER = (HEALTH_FAILED, HEALTH_UNCHECKED, HEALTH_OK)

# The verdict's own sentence, one per state, spelled ONCE. The page renders
# these rather than composing its own, for the reason `sample_is` exists: the
# words that say what was and was not established belong with the code that
# established it.
#
# EACH LEADS WITH ITS ANSWER (#88). The card's heading is the question "is
# anything blowing up?", and a first line that opens with the evidence has made
# the reader derive the answer from it. `ok` said "No." from the start; the
# failed one led with the evidence until #88, which matters more now that
# `failed` is the ONE verdict that expands its own detail.
HEALTH_STATEMENTS = {
    HEALTH_FAILED: (
        "Yes. At least one check FAILED, and that is not softened by the "
        "checks that passed -- read the failing line first, and treat every "
        "figure it qualifies as suspect until it is fixed."
    ),
    HEALTH_UNCHECKED: (
        "Nothing is proven broken, and at least one check COULD NOT BE MADE. "
        "This is not a clean bill of health, it is an incomplete one: the "
        "unchecked lines below name what was not established, and each of them "
        "is unknown rather than fine."
    ),
    HEALTH_OK: (
        "No. Every check this build can make passed: nothing unreadable, "
        "nothing skipped, nothing guessed at, and nothing measured against a "
        "limit this build does not know."
    ),
}

# The checks, named ONCE each. A check is a QUESTION the database can answer,
# so the set is enumerated here rather than inferred from whichever fields
# happened to be non-null -- an enumeration is the only shape in which "every
# state is handled" is a checkable statement.
CHECK_RECORDS_PARSED = "records-parsed"
CHECK_FORMAT_CENSUS = "transcript-format-census"
CHECK_MODEL_WINDOW_KNOWN = "model-window-known"
CHECK_WITHIN_WINDOW = "within-window"
CHECK_CONTEXT_MEASURED = "context-measured"
CHECK_INGEST_AGE = "ingest-age"
# The order they are reported in: the corpus first (can these files be read at
# all), then what was read (are the figures measurable), then how old the
# reading is. Not a severity ranking -- `verdict` is the severity, and a list
# re-sorted by state would move a check under the reader every time its answer
# changed.
HEALTH_CHECKS = (
    CHECK_RECORDS_PARSED,
    CHECK_FORMAT_CENSUS,
    CHECK_MODEL_WINDOW_KNOWN,
    CHECK_WITHIN_WINDOW,
    CHECK_CONTEXT_MEASURED,
    CHECK_INGEST_AGE,
)

# #61 gave the per-scope band tallies a name; #65 asks which of them is the
# PROBLEM, which is a ranking -- so it names the key it orders by, in the one
# place the ranking is computed, exactly as `RANKED_BY` does for the dispatch
# panel. The phrase, the `max()` key and the sentence on the page are one
# quantity in three places, asserted equal in tests/test_serve.py.
SATURATION_RANKED_BY = (
    "share of a scope's banded calls at or above half the model's documented "
    "context window"
)

# #88: THE ANSWER TO "AM I WASTING CONTEXT?", and it is a measurement.
#
# The card shipped answering with a LOCATION -- "Most of it, of the scopes
# measured, is in your main-thread" -- which names where the context is and
# never says whether any of it is a problem. A reader wanting yes or no got
# neither, and "most of it" had no antecedent on the resting page, because the
# meters that would give "it" a referent are one expansion down.
#
# The previous wording was a correction of a worse one: "the pressure is in
# your X" asserts that there IS pressure, which is false of a healthy corpus
# and would be the page inventing a finding. That instinct was right. The fix
# is not to soften further but to make the sentence CONDITIONAL ON THE
# MEASUREMENT -- so it is decided here, beside the tallies it is decided from,
# and the page renders the verdict and its sentence exactly as it renders the
# health verdict's.
#
# FIVE STATES, BECAUSE THE QUESTION HAS FIVE TRUE ANSWERS over this data, and
# collapsing any pair of them would be the milder-of-two-true-statements defect
# `HEALTH_UNCHECKED` exists to prevent one card up:
#
#   * `yes`         -- calls sit at or above the judged boundary. Proven, and
#                      the scope doing most of it is named beside the verdict.
#   * `no`          -- a complete sample, banded, none of it at the boundary. A
#                      POSITIVE finding, stated as one, exactly as an `ok`
#                      health verdict is.
#   * `inconclusive`-- nothing reached the boundary, but part of the period
#                      could not be read against a window at all. Not a clean
#                      no; the counts that say how much sit with the verdict.
#   * `unknown`     -- calls were measured and NONE of them could be banded, so
#                      there is nothing to compare against a window.
#   * `no sample`   -- no call in the period carried a measured context size.
#
# AN UNKNOWN MAY WEAKEN A `no` AND NEVER A `yes`: a proven saturation is not
# softened by the calls that could not be measured beside it. Same direction as
# every other fallback in this repository.
CONTEXT_ANSWER_YES = "yes"
CONTEXT_ANSWER_NO = "no"
CONTEXT_ANSWER_INCONCLUSIVE = "inconclusive"
CONTEXT_ANSWER_UNKNOWN = "unknown"
CONTEXT_ANSWER_NO_SAMPLE = "no sample"
# Enumerated, in the order "worst known" to "least established", so "every
# state is handled" is a checkable statement rather than a habit.
CONTEXT_ANSWER_STATES = (
    CONTEXT_ANSWER_YES,
    CONTEXT_ANSWER_NO,
    CONTEXT_ANSWER_INCONCLUSIVE,
    CONTEXT_ANSWER_UNKNOWN,
    CONTEXT_ANSWER_NO_SAMPLE,
)
# One sentence per state, spelled ONCE and here rather than in the page, for
# the reason `HEALTH_STATEMENTS` and `shape_statement` are: this branch shipped
# a heading ("And it only ever grows.") that the same database contradicted
# three hours later, because nothing guards prose written in a template. Each
# of these LEADS WITH THE ANSWER WORD -- the question is a yes/no question, and
# a first line that opens with a caveat has not answered it.
CONTEXT_ANSWER_STATEMENTS = {
    CONTEXT_ANSWER_YES: (
        "Yes. Calls are running at or above half the context window their "
        "model documents -- the judged boundary dated below -- and the scope "
        "doing most of it is named beside this line."
    ),
    CONTEXT_ANSWER_NO: (
        "No. Every scope's banded calls sat below half the context window "
        "their models document, and every call in this period was measured, "
        "banded and inside its window. That is a measured no over a complete "
        "sample, not an absence of evidence."
    ),
    CONTEXT_ANSWER_INCONCLUSIVE: (
        "Not established. No scope reached half its models' documented "
        "window, but part of this period could not be read against one, so "
        "this is not a clean no. The counts beside this line say how much, "
        "and each of them is UNKNOWN rather than low."
    ),
    CONTEXT_ANSWER_UNKNOWN: (
        "Unknown. Contexts were measured in this period and not one of them "
        "could be compared with a documented window, so there is no "
        "utilisation to answer with -- unknown, not none."
    ),
    CONTEXT_ANSWER_NO_SAMPLE: (
        "No sample. No call in this period carried a measured context size, "
        "so there is no median and no utilisation to report. None of that is "
        "a zero."
    ),
}


# The strip's own four states. THREE vocabularies reach this line -- the health
# verdict's, the context answer's and the table's severities -- and each is
# translated into these rather than rendered raw, so the four dots can be read
# in one glance without the reader learning three sets of words.
#
# `STRIP_UNKNOWN` is the load-bearing member and is NOT a fourth shade of
# `STRIP_WATCH`: "we could not check" and "we checked and it is middling" are
# different claims, and collapsing them is the substitution this repository
# refuses everywhere else. A dot with no measurement behind it must never wear
# the colour of one that has.
STRIP_GOOD = "good"
STRIP_WATCH = "watch"
STRIP_BAD = "bad"
STRIP_UNKNOWN = "unknown"
# Worst first, so "the worst state any reading reached" is a lookup rather than
# a comparison somebody writes out. `STRIP_UNKNOWN` sits between `watch` and
# `good` for the same reason `HEALTH_ORDER` puts `unchecked` there: an
# unestablished answer may weaken a clean one and may never soften a bad one.
STRIP_ORDER = (STRIP_BAD, STRIP_WATCH, STRIP_UNKNOWN, STRIP_GOOD)

# One entry per state of each vocabulary, checked EXHAUSTIVE at import. A state
# added upstream with no entry here would otherwise reach `KeyError` at request
# time -- a 500 over the whole payload -- or, worse, a `.get(..., default)`
# that quietly rendered a new failure state as a clean dot.
STRIP_FROM_HEALTH: dict[str, tuple[str, str]] = {
    HEALTH_OK: (STRIP_GOOD, "Nothing broken"),
    HEALTH_UNCHECKED: (STRIP_UNKNOWN, "Not fully checked"),
    HEALTH_FAILED: (STRIP_BAD, "Something is broken"),
}
STRIP_FROM_CONTEXT: dict[str, tuple[str, str]] = {
    CONTEXT_ANSWER_YES: (STRIP_BAD, "Yes"),
    CONTEXT_ANSWER_NO: (STRIP_GOOD, "No"),
    CONTEXT_ANSWER_INCONCLUSIVE: (STRIP_UNKNOWN, "Not established"),
    CONTEXT_ANSWER_UNKNOWN: (STRIP_UNKNOWN, "Unknown"),
    CONTEXT_ANSWER_NO_SAMPLE: (STRIP_UNKNOWN, "No sample"),
}
STRIP_FROM_SEVERITY: dict[str, tuple[str, str]] = {
    SEVERITY_OK: (STRIP_GOOD, "Repaying"),
    SEVERITY_WATCH: (STRIP_WATCH, "Watch"),
    SEVERITY_ACT: (STRIP_BAD, "Not repaying"),
}
# WHAT A DOT SAYS WHEN IT HAS NO VERDICT, and there are TWO such sentences
# because there are two absences. Spelled once each and shared by every dot
# that can reach them: three dots arrive here by three different routes, and a
# reader meets one phrase per state rather than one phrase per dot.
#
# "Nobody measured this" and "this was measured over too little" send a reader
# to two different remedies, and only one of them is "come back later". A
# project that never dispatches a subagent is not waiting for more sessions.
STRIP_NOT_MEASURED = "Not measured"
STRIP_UNDER_SAMPLED = "Not enough data yet"
# The suffix the knobs dot adds where SOME of its knobs have a basis. Where
# some do, the count still ranges over all of them -- narrowing the denominator
# to the measured ones would be a second, smaller truth told in place of the
# first -- and this names the rest. Where NONE does, "0 of 5" is arithmetic
# over an empty set wearing the look of five checks passed, which is the
# composition defect #93 is about in four characters, and the dot says which
# absence it is instead.
STRIP_KNOBS_SHORT_SUFFIX = "{short} not yet measurable"

# The four questions, in the order they are read. Chosen so the strip runs from
# "is it broken" to "is the discount working": a reader who stops after one dot
# has stopped on the one that would invalidate the rest.
STRIP_DOT_BROKEN = "broken"
STRIP_DOT_CONTEXT = "context"
STRIP_DOT_KNOBS = "knobs"
STRIP_DOT_CACHE = "cache"
STRIP_QUESTIONS: dict[str, str] = {
    STRIP_DOT_BROKEN: "Anything broken?",
    STRIP_DOT_CONTEXT: "Wasting context?",
    STRIP_DOT_KNOBS: "Knobs worth turning",
    STRIP_DOT_CACHE: "Cache health",
}
STRIP_DOTS = (
    STRIP_DOT_BROKEN,
    STRIP_DOT_CONTEXT,
    STRIP_DOT_KNOBS,
    STRIP_DOT_CACHE,
)
_refuse_unhandled_states("STRIP_FROM_HEALTH", STRIP_FROM_HEALTH, HEALTH_ORDER)
_refuse_unhandled_states(
    "STRIP_FROM_CONTEXT", STRIP_FROM_CONTEXT, CONTEXT_ANSWER_STATES
)
_refuse_unhandled_states("STRIP_FROM_SEVERITY", STRIP_FROM_SEVERITY, SEVERITY_RANK)
_refuse_unhandled_states("STRIP_QUESTIONS", STRIP_QUESTIONS, STRIP_DOTS)
_refuse_unhandled_states(
    "STRIP_ORDER",
    STRIP_ORDER,
    {STRIP_GOOD, STRIP_WATCH, STRIP_BAD, STRIP_UNKNOWN},
)

# #65: the growth curve. THE finding that makes the context figures actionable
# -- typical main-session context across the four quarters of its own life,
# measured 2026-08-05 over this project's own transcripts:
#
#     97,436 -> 333,610 -> 514,413 -> 906,301
#
# By the last quarter the TYPICAL reply sat at 90.6% of the window. "Your
# context is large" and "your context only ever grows" are different findings
# with different remedies, and nothing in the report showed the second.
GROWTH_QUARTERS = 4
# WHICH SCOPE. Main-thread only, and it says so in the payload: the subagents
# are short-lived by construction and their contexts do not accumulate, so a
# curve pooled across both would average the mechanism away -- #61's dilution
# defect on a second axis.
GROWTH_SCOPE = SOURCE_MAIN
GROWTH_SAMPLE = (
    "main-thread calls with a measured context size, split into four equal "
    "spans of the period between the first and the last of them"
)
# THE FLOOR, and it is DERIVED rather than judged. Each quarter's figure is a
# nearest-rank median, and the smallest sample on which that median is
# STRICTLY INTERIOR -- neither the smallest nor the largest call in the quarter
# -- is 3: `nearest_rank` takes index `ceil(p*n/100) - 1`, which for p=50 is
# index 0 (the minimum) at n<=2 and index 1 at n=3. A "typical" context that is
# in fact the quarter's smallest call is not a typical anything, so a curve
# drawn from such quarters would be four bars from three points.
GROWTH_MIN_CALLS_PER_QUARTER = 3
GROWTH_MIN_CALLS = GROWTH_QUARTERS * GROWTH_MIN_CALLS_PER_QUARTER  # 12
# Why the curve is refused, non-null exactly when it is -- the same
# tri-state-with-a-reason shape as `no_sample_reason` and
# `stale_unknown_reason`. A refused curve still publishes its quarters' COUNTS,
# which are true; what it withholds is the claim that they describe a trend.
GROWTH_REFUSED_TOO_FEW = (
    "too few measured main-thread calls in this period to quarter meaningfully "
    "-- a quarter of fewer than three calls has no median that is not simply "
    "its smallest or largest call"
)
GROWTH_REFUSED_NO_SPAN = (
    "every measured main-thread call in this period carries the same "
    "timestamp, so the period has no span to divide into quarters"
)
# A quarter nobody measured. NOT a zero: no call fell in it, so it has no
# median, and a plotted 0 would draw the context COLLAPSING in a quarter that
# was simply idle.
GROWTH_QUARTER_NO_CALLS = (
    "no measured main-thread call fell in this quarter of the period"
)

# ---------------------------------------------------------------------------
# THE SHAPE OF THE CURVE, DERIVED -- because the sentence over it was AUTHORED.
# ---------------------------------------------------------------------------
#
# The panel shipped in review headed "And it only ever grows." That sentence was
# true of the corpus it was written against and false three hours later, and the
# defect is worth stating plainly because it is this repository's own rule
# arriving by a route none of its guards watch:
#
#   * measured 2026-08-05 in the morning, main thread: 97,436 -> 333,610 ->
#     514,413 -> 906,301, monotonic;
#   * measured 2026-08-05 in the afternoon over the same database, 559 calls:
#     277,945 -> 837,645 -> 297,343 -> 430,832. It climbs, drops by two thirds,
#     and rises again. The session compacted in between.
#
# A HEADING THAT STATES A TREND THE NUMBERS DO NOT HAVE IS A WRONG FIGURE MADE
# OF WORDS, and it is worse than a wrong number: `Optional[int]` guards a
# number, and nothing whatever guards prose. So the sentence is derived from the
# quarters, every time, like every other figure here.
#
# The taxonomy is EXHAUSTIVE over the sequence, and its default is refusal:
# where the movement fits none of the named shapes the panel says so rather than
# picking the reading that sounds most like a finding.
GROWTH_SHAPE_UNMEASURABLE = "unmeasurable"
GROWTH_SHAPE_FLAT = "flat"
GROWTH_SHAPE_RISING = "rising"
GROWTH_SHAPE_FALLING = "falling"
GROWTH_SHAPE_ROSE_THEN_FELL = "rose-then-fell"
GROWTH_SHAPE_MIXED = "no-discernible-trend"
GROWTH_SHAPES = (
    GROWTH_SHAPE_UNMEASURABLE,
    GROWTH_SHAPE_FLAT,
    GROWTH_SHAPE_RISING,
    GROWTH_SHAPE_FALLING,
    GROWTH_SHAPE_ROSE_THEN_FELL,
    GROWTH_SHAPE_MIXED,
)

# What counts as a CHANGE, as a fraction of the earlier quarter. This is the
# one JUDGED number in the block and it carries its own provenance below --
# every other figure here is a median of measured calls.
GROWTH_MATERIAL_CHANGE = 0.25
GROWTH_SHAPE_AS_OF = "2026-08-05"
GROWTH_SHAPE_PROVENANCE = (
    "Product-owner judgment: a quarter counts as having moved only if the "
    "typical reply changed by at least "
    f"{GROWTH_MATERIAL_CHANGE:.0%} of the previous quarter's. Anthropic "
    "publishes nothing about this and it is derived from no measurement -- it "
    "is where this project judged a change worth a sentence. Set higher, every "
    "period reads as flat; set lower, ordinary variation reads as a trend. It "
    "is stated with its date so the verdict can be weighed against it rather "
    "than taken on trust."
)

# One sentence per shape, spelled ONCE and carried in the payload -- so the
# claim and the arithmetic that produced it live in the same file, and the page
# cannot author a seventh reading. No sentence here interpolates a figure: the
# four quarters are rendered directly under it, and a sentence quoting numbers
# would be a second copy of them.
GROWTH_SHAPE_STATEMENTS = {
    GROWTH_SHAPE_UNMEASURABLE: (
        "No shape is claimed. This period does not carry two quarters that can "
        "be compared, so whether the typical reply grew, shrank or held is "
        "UNKNOWN -- not flat, and not none."
    ),
    GROWTH_SHAPE_FLAT: (
        "The typical reply HELD STEADY across this period: no quarter differs "
        "from the one before it by enough to count as a change. Nothing here "
        "is accumulating, and nothing here needs acting on."
    ),
    GROWTH_SHAPE_RISING: (
        "The typical reply GREW across this period: every change large enough "
        "to count was upward. This is context accumulating -- the session is "
        "re-reading more of its own history each time and never shedding it."
    ),
    GROWTH_SHAPE_FALLING: (
        "The typical reply SHRANK across this period: every change large "
        "enough to count was downward. There is nothing to act on here -- "
        "whatever was keeping the context down was working."
    ),
    GROWTH_SHAPE_ROSE_THEN_FELL: (
        "The typical reply CLIMBED AND THEN DROPPED: it peaked in the quarter "
        "named below, and the last measured quarter sits well under that peak. "
        "A fall this size is what a context RESET looks like -- a compaction, "
        "a new session, or a change of subject -- but this report measures the "
        "drop and never its cause, so which of those it was is not something "
        "these figures can say."
    ),
    GROWTH_SHAPE_MIXED: (
        "NO TREND IS CLAIMED: the typical reply moved materially in BOTH "
        "directions across this period and settles into none of the shapes "
        "this build can name. Read the four quarters themselves rather than a "
        "summary of them."
    ),
}

# #78: half the window, as a FRACTION of it -- the point
# `METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW` counts from. Named rather than
# written `0.5` inline in the comprehension below, because it is the same
# judgment `BANDS` already made and the two must be able to be read together.
HALF_WINDOW = 0.5
# The bands whose lower edge is at or above half the window, DERIVED from
# `BANDS` rather than listed. The metric's `measurement` says "reaches at least
# half the model's documented window", and names `50-to-90` and `at-least-90`
# as what that is TODAY; a band table that later gained a cut at 0.6 would be
# part of "at least half" by that definition, and a hand-written pair of keys
# would silently drop it out of the numerator. Currently exactly those two.
OVER_HALF_WINDOW_BANDS = frozenset(
    key for key, _label, lower, _upper in BANDS if lower >= HALF_WINDOW
)

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

# Why `last_full_scan_at` is null, when it is (#105) -- the same three-value
# shape as the reasons above, and for the same reason: the page may state which
# absence it has, never guess one.
#
# `finished_at` answers "how old is this database" and `corpus_finished_at`
# answers "has anything ever scanned the whole corpus". They used to be one
# column answering both, which is why single-file mode -- the plugin's only mode
# -- could not stamp without asserting a corpus scan, and so stamped nothing.
#
# No `ingest_runs` table: this database predates run stamping entirely, so it
# cannot have recorded a scan of any scope. The SAME value the staleness reason
# uses, deliberately -- one absence, one name.
FULL_SCAN_UNKNOWN_NO_RUN_TABLE = STALE_UNKNOWN_NO_RUN_TABLE
# The table is here without the column: written by a build that stamped runs
# and did not record what they ranged over. Whether a full scan ever completed
# is UNRECORDED, not answered `no` -- and pointedly not answered `yes` either,
# though every stamp such a build wrote did come from a corpus run. `ingest.py`
# refuses that same deduction at the migration (IN_PLACE_ADDABLE_COLUMNS); the
# reader is owed the same refusal.
FULL_SCAN_UNKNOWN_NO_SCOPE_COLUMN = "no-scope-column"
# The column is here and NULL: this database records run scope and no
# full-corpus scan has ever stamped itself over it. THE PLUGIN'S NORMAL STATE,
# and a true statement rather than a fault -- a hook-maintained database holds
# the sessions its hooks were handed and has never been swept for anything else.
FULL_SCAN_UNKNOWN_NONE_RECORDED = "no-full-scan-recorded"

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

    def _has_run_scope_column(self) -> bool:
        """Does this database record WHAT a completed run looked at? (#105)

        A third question in the same family as `_has_ingest_runs_table()`, asked
        the same way and for the same reason: `serve.py` never migrates, so a
        database written by a pre-v13 build has `ingest_runs` and no
        `corpus_finished_at`, and "this build cannot ask" must not read as "the
        answer is no". `PRAGMA table_info` rather than a `SELECT` that catches
        `OperationalError`, which would swallow a corrupt or locked database as
        a merely old schema.
        """
        return "corpus_finished_at" in {
            row[1]
            for row in self.conn.execute(
                f"PRAGMA table_info({INGEST_RUNS_TABLE})"
            ).fetchall()
        }

    def _last_full_scan(self) -> tuple[Optional[float], Optional[str]]:
        """`(when a FULL-CORPUS scan last completed, why not)` (#105).

        The narrower of the two run facts, split out of `finished_at` when
        single-file runs started stamping. Both are needed and neither
        substitutes: `_last_ingest_run()` says how old this database's reading
        of the transcripts is, and this says whether anything has ever looked
        at the transcripts it was not handed.

        The reason is non-null exactly when the timestamp is null, and names
        which of THREE absences it is, because they carry different remedies:
        a schema too old to record runs at all, one that recorded runs without
        their scope, and one that records scope and has seen no corpus scan.
        Only the third is a statement about what has happened; the first two are
        statements about what could have been written down. Collapsing them
        would let a pre-v13 database read as a plugin install, which is the
        `no-run-table`/`no-run-recorded` conflation of #34 one column over.
        """
        if not self._has_ingest_runs_table():
            return None, FULL_SCAN_UNKNOWN_NO_RUN_TABLE
        if not self._has_run_scope_column():
            return None, FULL_SCAN_UNKNOWN_NO_SCOPE_COLUMN
        row = self.conn.execute(
            f"SELECT corpus_finished_at FROM {INGEST_RUNS_TABLE}"
            " WHERE corpus_finished_at IS NOT NULL"
            " ORDER BY corpus_finished_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None, FULL_SCAN_UNKNOWN_NONE_RECORDED
        return row["corpus_finished_at"], None

    def _last_ingest_run(self) -> Optional[float]:
        """When `ingest.py` last COMPLETED, or None if that was never recorded.

        **Of EITHER mode (#105).** That is what this has always claimed and
        since #105 it is also what it does: `ingest.py --transcript` is
        `ingest.py` completing, over one file rather than the corpus, and it
        used not to stamp -- so a plugin install, whose hooks use that mode and
        no other, reported "no run has ever completed" however much it ingested.
        What a single-file run is no evidence of is a corpus scan, and that
        narrower fact is `_last_full_scan()`, beside this rather than inside it.

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

        **A run that RAISES still reaches the second one, and must.** #105
        widened which runs stamp; it did not weaken when a stamp is written.
        Both modes stamp last and only on the success path, so a run that died
        before finishing leaves exactly what no run leaves, because that is what
        the database knows about it.
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

        A THIRD since #105, and it splits the first rather than joining it:
        `last_full_scan_at` is when a run last looked at the whole corpus
        instead of at one file. `last_run_at` is what every freshness surface
        reads -- the verdict, the banner and the data-age line, one source
        between them, unchanged -- and `last_full_scan_at` qualifies the SET
        that reading ranges over. A hook-maintained database is current and has
        never been swept; those are two facts and they now have two fields
        rather than one field that could only express the first by denying
        both.

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
        # NOT fed into `staleness_verdict` (#105). The staleness verdict is
        # about the age of this database's reading, and a corpus scan that never
        # happened is not an age -- folding it in would let "nobody has swept
        # the whole corpus" render as "your data may be stale", which is the
        # false alarm this issue exists to remove, restored one field over.
        last_full_scan_at, full_scan_unknown_reason = self._last_full_scan()
        return {
            "files": row["files"],
            "unparsed_records": row["unparsed"],
            "last_run_at": last_run_at,
            "last_full_scan_at": last_full_scan_at,
            "full_scan_unknown_reason": full_scan_unknown_reason,
            "newest_call_ts": newest_call_ts,
            "as_of": as_of,
            "stale_after_seconds": STALE_AFTER_SECONDS,
            "stale": stale,
            "stale_unknown_reason": stale_unknown_reason,
        }

    def _has_source_shape_table(self) -> bool:
        """Can this database record a transcript-format census AT ALL? (#15)

        Asked for the same reason `_has_ingest_runs_table()` is: `serve.py`
        never migrates, it reads the database as the ingester left it, so a
        pre-v9 database simply has no `source_shape`. "This build cannot ask
        the question" and "the question was asked and nothing was censused" are
        different states of knowledge, and a bare `OperationalError` handler
        would report the second for the first -- and would swallow a corrupt or
        locked database as a merely old schema besides.
        """
        return (
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (SHAPE_TABLE,),
            ).fetchone()
            is not None
        )

    def _health(
        self, ingest: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        """Is anything broken? -- the verdict the report never gave (#64).

        **DERIVED, never hardcoded optimistic.** Every check below reads a
        figure this response already computed: `unparsed_records` and the
        staleness tri-state off `_ingest_health()`, the census off
        `source_shape`, and the three utilisation absences off the `context`
        block that is PASSED IN rather than recomputed. A second query here
        would be a second definition of what was measured, free to disagree
        with the bands the reader is looking at -- the reason
        `_recommendations()` takes the same argument.

        **THE THIRD STATE IS THE POINT.** `HEALTH_OK` says the checks passed;
        `HEALTH_UNCHECKED` says at least one could not be made. Collapsing them
        would let a corpus nobody has censused report health -- and
        `source_shape`'s whole design is that a source with NO rows has not been
        censused rather than been found clean, which is this rule stated one
        layer down. An uncensused corpus is the case that forced the three
        states: every other check can pass over it while the one thing that
        would notice a transcript-format change has never run.

        **A FAILURE OUTRANKS THE REASSURANCE.** `verdict` is the worst state any
        check reached, by `HEALTH_ORDER`, so no number of passing checks can
        soften a failing one. And it cannot LOWER a staleness verdict: the
        `ingest-age` check reads `ingest.stale` and maps true to `failed`, null
        to `unchecked` and false to `ok`, which is the tri-state it was handed
        -- the banner's own warning is untouched by anything here (PR #60).

        Every check publishes `count` and `of` in ONE unit, or null where the
        database does not hold that quantity. `records-parsed` has a null `of`
        on purpose: `ingest_state` records how many records FAILED to parse and
        not how many were read, so there is no total to state and inventing one
        would be the defect this whole block exists to report.
        """
        util = context["utilisation"]
        checks = [
            self._check_records_parsed(ingest),
            self._check_format_census(),
            self._check_model_window_known(context, util),
            self._check_within_window(util),
            self._check_context_measured(context, util),
            self._check_ingest_age(ingest),
        ]
        # Enumerated, and asserted to BE the enumeration: a check added to
        # `HEALTH_CHECKS` and computed nowhere, or computed here and declared
        # nowhere, is the wiring gap `_refuse_unwired_metrics()` exists for one
        # module over. Here the set is small enough to state inline, and
        # tests/test_serve.py pins the two equal.
        states = {c["state"] for c in checks}
        verdict = next(state for state in HEALTH_ORDER if state in states)
        return {
            "verdict": verdict,
            "statement": HEALTH_STATEMENTS[verdict],
            "checks": checks,
        }

    @staticmethod
    def _health_check(
        check: str,
        state: str,
        statement: str,
        count: Optional[int] = None,
        of: Optional[int] = None,
    ) -> dict[str, Any]:
        """One check's row, spelled once so every check carries every field."""
        return {
            "check": check,
            "state": state,
            "statement": statement,
            "count": count,
            "of": of,
        }

    @classmethod
    def _check_records_parsed(cls, ingest: dict[str, Any]) -> dict[str, Any]:
        """Did every record in every ingested transcript parse? (#64)"""
        files = ingest["files"]
        unparsed = ingest["unparsed_records"]
        if not files:
            return cls._health_check(
                CHECK_RECORDS_PARSED,
                HEALTH_UNCHECKED,
                "No transcript has been ingested, so nothing has been read and "
                "nothing can be said about it. Run ingest.py.",
                count=0,
                of=None,
            )
        if unparsed is None:
            # SUM over a non-empty table cannot be NULL today; if it ever is,
            # "the ledger holds files and reports no parse count" is an unknown
            # and must not read as a clean zero.
            return cls._health_check(
                CHECK_RECORDS_PARSED,
                HEALTH_UNCHECKED,
                "The ingest ledger holds files but reports no parse count for "
                "them, so whether anything failed to parse is UNKNOWN, not no.",
                count=None,
                of=None,
            )
        if unparsed:
            return cls._health_check(
                CHECK_RECORDS_PARSED,
                HEALTH_FAILED,
                "Record(s) in the ingested transcripts could not be parsed, so "
                "every total in this report undercounts by an unknown amount. "
                "This is a real gap, not a rounding one.",
                count=unparsed,
                of=None,
            )
        return cls._health_check(
            CHECK_RECORDS_PARSED,
            HEALTH_OK,
            "Every record in every ingested transcript parsed cleanly -- "
            "nothing unreadable, nothing skipped, nothing guessed at.",
            count=0,
            of=None,
        )

    def _check_format_census(self) -> dict[str, Any]:
        """Has the transcript format been censused at all? (#15/#64)

        THE check that makes "nothing is broken" different from "we could not
        check". The census is what would notice Claude Code renaming a token
        key or emitting a record type CPB has never seen -- and a source with
        no `source_shape` row has not been censused rather than been found
        clean. A corpus that has never been censused therefore CANNOT report
        health, however clean every other check is.
        """
        if not self._has_source_shape_table():
            return self._health_check(
                CHECK_FORMAT_CENSUS,
                HEALTH_UNCHECKED,
                "This database predates the transcript-format census, so the "
                "shape of the records behind these figures has never been "
                "checked -- UNCENSUSED, not clean. Re-run ingest.py.",
                count=None,
                of=None,
            )
        censused, tracked = census_coverage(self.conn)
        if not tracked:
            return self._health_check(
                CHECK_FORMAT_CENSUS,
                HEALTH_UNCHECKED,
                "No transcript is tracked, so there is no format to census. "
                "Nothing here has been found clean; nothing has been looked at.",
                count=censused,
                of=tracked,
            )
        if censused < tracked:
            return self._health_check(
                CHECK_FORMAT_CENSUS,
                HEALTH_UNCHECKED,
                "Some tracked transcripts carry no format census: they were "
                "ingested before the census existed and are unchanged, so they "
                "will be censused when they next change. They are UNCENSUSED, "
                "not clean -- a format change in them would not have been seen.",
                count=censused,
                of=tracked,
            )
        return self._health_check(
            CHECK_FORMAT_CENSUS,
            HEALTH_OK,
            "Every tracked transcript has been censused for its record shape, "
            "so a Claude Code release that renamed a token key or emitted an "
            "unknown record type would have been counted rather than absorbed.",
            count=censused,
            of=tracked,
        )

    @classmethod
    def _check_model_window_known(
        cls, context: dict[str, Any], util: dict[str, Any]
    ) -> dict[str, Any]:
        """Did every measured call run on a model this build has a window for?"""
        sample_calls = context["sample_calls"]
        unknown = util["unknown_model_calls"]
        if not sample_calls:
            return cls._health_check(
                CHECK_MODEL_WINDOW_KNOWN,
                HEALTH_UNCHECKED,
                "No call in this period carried a context measurement, so "
                "there is no call whose model window could be looked up.",
                count=unknown,
                of=sample_calls,
            )
        if unknown:
            return cls._health_check(
                CHECK_MODEL_WINDOW_KNOWN,
                HEALTH_UNCHECKED,
                "Call(s) ran on a model this build has no documented context "
                "window for, so their utilisation is UNKNOWN, not low. The "
                "models are named beside the bands below.",
                count=unknown,
                of=sample_calls,
            )
        return cls._health_check(
            CHECK_MODEL_WINDOW_KNOWN,
            HEALTH_OK,
            "Every measured call ran on a model whose context window this "
            "build has documented, so none of them was banded against a guess.",
            count=0,
            of=sample_calls,
        )

    @classmethod
    def _check_within_window(cls, util: dict[str, Any]) -> dict[str, Any]:
        """Did any reply measure ABOVE 100% of the window it had? (#31)

        A FAILURE rather than a caveat. A call cannot exceed its own context
        window, so a measurement that says one did means this build's window
        table has gone stale -- the loud half of `context_window.py`'s safety
        story, and it is only a safety story if something states the verdict.
        """
        banded = util["banded_calls"]
        over = util["over_window_calls"]
        if not banded:
            return cls._health_check(
                CHECK_WITHIN_WINDOW,
                HEALTH_UNCHECKED,
                "No call in this period was banded against a documented "
                "window, so no call could be compared to one.",
                count=over,
                of=banded,
            )
        if over:
            return cls._health_check(
                CHECK_WITHIN_WINDOW,
                HEALTH_FAILED,
                "Call(s) measure ABOVE 100% of their model's documented "
                "window. That is impossible unless this build's window table "
                "has gone stale, so treat the bands as suspect rather than the "
                "calls as extraordinary.",
                count=over,
                of=banded,
            )
        return cls._health_check(
            CHECK_WITHIN_WINDOW,
            HEALTH_OK,
            "No reply exceeded the context window it had, so this build's "
            "window table is not contradicted by anything in this period.",
            count=0,
            of=banded,
        )

    @classmethod
    def _check_context_measured(
        cls, context: dict[str, Any], util: dict[str, Any]
    ) -> dict[str, Any]:
        """Did every call in the window carry prompt accounting? (#25)"""
        calls = util["calls"]
        unmeasured = context["unmeasured_calls"]
        if not calls:
            return cls._health_check(
                CHECK_CONTEXT_MEASURED,
                HEALTH_UNCHECKED,
                "No API call falls in this period, so there is nothing here "
                "whose context could have been measured.",
                count=unmeasured,
                of=calls,
            )
        if unmeasured:
            return cls._health_check(
                CHECK_CONTEXT_MEASURED,
                HEALTH_UNCHECKED,
                "Call(s) carry no prompt accounting at all -- every token "
                "class reported zero -- so their context is UNMEASURED, not "
                "small. They are counted and kept out of every context figure.",
                count=unmeasured,
                of=calls,
            )
        return cls._health_check(
            CHECK_CONTEXT_MEASURED,
            HEALTH_OK,
            "Every call in this period carried a context measurement, so no "
            "context figure below ranges over a smaller set than the calls "
            "beside it.",
            count=0,
            of=calls,
        )

    @classmethod
    def _check_ingest_age(cls, ingest: dict[str, Any]) -> dict[str, Any]:
        """How current the database is -- read, never re-derived (#20/#34).

        This check may not LOWER the staleness verdict, and structurally
        cannot: it maps the tri-state it was handed rather than comparing
        timestamps itself. The banner's own warning is composed from the same
        field and is untouched.
        """
        stale = ingest["stale"]
        if stale is None:
            return cls._health_check(
                CHECK_INGEST_AGE,
                HEALTH_UNCHECKED,
                "The age of this database cannot be measured "
                f"({ingest['stale_unknown_reason']}), so nothing below is "
                "qualified as fresh or stale. Unknown, not fresh.",
            )
        if stale:
            return cls._health_check(
                CHECK_INGEST_AGE,
                HEALTH_FAILED,
                "The last ingest run is older than this build's staleness "
                "threshold, so every figure here describes the transcripts as "
                "of THEN, not as of now. Re-run ingest.py.",
            )
        return cls._health_check(
            CHECK_INGEST_AGE,
            HEALTH_OK,
            "ingest.py has run within this build's staleness threshold, so "
            "these figures describe the transcripts as they are now.",
        )

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
            row = dict(empty) if row is None else row
            row.pop("source_kind", None)
            # #82: the bucket NAMES ITSELF, in `SCOPE_LABELS`' vocabulary. The
            # key it sits under (`main_thread`) is the payload's spelling of
            # the ingester's `source_kind`; the label is the one every other
            # scoped figure in this API crosses the boundary as, and the page
            # renders these means beside `context.utilisation.by_scope`, which
            # is keyed on the label. Without this the page would have to map
            # one to the other itself -- an equivalence the API never stated,
            # which is the class of invention `SCOPE_LABELS` exists to prevent.
            row["scope"] = SCOPE_LABELS.get(kind, kind)
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
        summary card. That partition holds PER SCOPE as well as pooled.

        **The bands are tallied per scope (#61).** Pooling main-thread and
        subagent calls into one band tally is arithmetically correct and tells
        the reader the wrong thing. Measured 2026-08-05 over this project's own
        transcripts (44 sources, 8,163 records, 0 unparsed), 2,722 banded calls:

            scope        calls   median ctx    peak     >=90%          50-90%
            main-thread    390      427,037   996,190   52 (13.3%)   100 (25.6%)
            subagent     2,332      104,727   374,816    0  (0.0%)     0  (0.0%)
            pooled       2,722            --        --   52  (1.9%)   100  (3.7%)

        Every red-band call was main-thread; not one was a subagent. 2,332
        healthy subagent calls dilute 390 main-thread ones 6:1, so the pooled
        share erases the one scope the reader can act on -- and it moves the
        WRONG WAY under the condition it exists to detect, because a saturated
        orchestrator dispatches more subagents, which pushes the pooled share
        down. "13.3% of your main-thread calls ran at 90%+ of the window"
        prompts an action; "1.9%" prompts none. This is CLAUDE.md's "an
        aggregate must name the set it ranges over", which `SCOPE_*` already
        applied to every token total and had never been extended to here.

        `by_scope` therefore carries one entry per scope, in `SCOPE_ORDER`, in
        the `SCOPE_LABELS` vocabulary the rest of the API uses -- and a scope
        with no calls gets an entry all the same, because a missing key is an
        absence nobody can see.

        **The pooled tally is KEPT and LABELLED rather than deleted.** It is
        still the honest answer to "of every call in this window, how many were
        red" -- the denominator the `bands + unknown + unmeasured == calls`
        partition is checked against -- so removing it would delete a true
        figure to prevent a misreading. What was actually wrong was that it did
        not name its set, so it now carries `includes`, and the page must lead
        with the scoped tallies and render the pooled one as what `includes`
        says it is. Provenance stays at the `utilisation` level and is NOT
        repeated per scope: which window a model has, and where the band
        boundaries sit, are properties of `context_window.py`. Copying them
        into each scope would imply they could differ by scope, which is a
        distinction nobody made.

        **A scope with no banded calls is a named absence.** Four bands of
        `calls: 0` is a true statement three different ways -- the scope ran
        nothing; it ran calls that carried no context measurement; it ran
        measured calls whose models have no documented window -- with three
        different remedies. `no_sample_reason` says which, non-null exactly
        when `banded_calls` is 0, and every `share` beside it is null rather
        than 0.0 (a share of an empty set is not 0%).

        One pass over the window's `(source_kind, model, context_size)` triples,
        banded in Python rather than in SQL, because the window lookup is a
        longest-prefix match no `GROUP BY` can express. Measured 2026-08-05 on a
        synthetic 600k-call / 3,000-source database of the shape `_projects()`
        cites (macOS, warm cache, best of 5): 470 ms, inside a `summary()` of
        2.8 s. The two shortcuts below are worth 480 ms of that -- the same loop
        costs 950 ms with a `sqlite3.Row` built per row and an unmemoised lookup
        -- and neither changes an answer. Splitting by scope adds one column and
        one dict lookup per row, not a second pass.
        """
        sizes: list[int] = []
        # Tallies keyed on the STORED `source_kind`, never on the outward
        # label: the label is this module's vocabulary and the key is the
        # ingester's, and one is derived from the other exactly once, below.
        scopes: dict[str, dict[str, Any]] = {}

        def tally(kind: str) -> dict[str, Any]:
            found = scopes.get(kind)
            if found is None:
                found = scopes[kind] = {
                    "calls": 0,
                    "sample_calls": 0,
                    "banded": {key: 0 for key, *_ in BANDS},
                    "unknown_model_calls": 0,
                    "unknown_models": set(),
                    "over_window_calls": 0,
                }
            return found

        # Windows per model id, resolved once. A window holds a handful of
        # distinct ids and hundreds of thousands of calls, and the lookup walks
        # the whole table per call otherwise. Misses are cached too -- an
        # unknown model is the case that would otherwise pay full price on
        # every row.
        windows: dict[str, Optional[int]] = {}
        # A cursor with the connection's `sqlite3.Row` factory turned OFF: this
        # loop reads three columns positionally, and building a mapping per row
        # is the single largest cost in the block at corpus scale.
        cursor = self.conn.cursor()
        cursor.row_factory = None
        # #65: the growth curve's sample, collected in THIS pass rather than by
        # a second query. A second read of the same rows would be a second
        # definition of "a measured main-thread call", free to drift from the
        # one the bands above are drawn from -- the defect `_recommendations()`
        # takes `context` as an argument to avoid, one block down.
        growth_points: list[tuple[float, int, Optional[float]]] = []
        for kind, model, size, ts in cursor.execute(
            "SELECT source_kind, model, context_size, ts FROM api_calls"
            " WHERE ts >= ? AND ts < ?",
            (start, end),
        ):
            scope = tally(kind)
            scope["calls"] += 1
            if not has_context_measurement(size):
                # The SAME predicate the four SQL means use (#25), not a
                # second spelling of it: this block and `summary()`'s
                # `avg_context` describe one sample, and two definitions free
                # to drift apart is how this defect recurs. It excludes a
                # negative as well as a zero -- a negative context is not
                # reachable from summed token counts, so a row carrying one is
                # broken, and the one thing it must not do is band as the most
                # frugal call on the report.
                continue
            scope["sample_calls"] += 1
            sizes.append(size)
            if model not in windows:
                windows[model] = window_for_model(model)
            window = windows[model]
            if window is None:
                scope["unknown_model_calls"] += 1
                scope["unknown_models"].add(model)
                # The size WAS measured, so this call is part of the growth
                # curve's context median; its utilisation is not known, so it
                # is not part of that quarter's utilisation median. Two
                # samples, counted separately (rule #12), never one figure
                # standing in for the other.
                if kind == GROWTH_SCOPE:
                    growth_points.append((ts, size, None))
                continue
            fraction = size / window
            if kind == GROWTH_SCOPE:
                growth_points.append((ts, size, fraction))
            if fraction > 1.0:
                # The loud half of this feature's safety story: a window this
                # table has let go stale shows up as calls over 100% of it,
                # which is absurd on its face -- but only if someone counts it.
                scope["over_window_calls"] += 1
            scope["banded"][band_for(fraction)] += 1

        # The pooled tally is SUMMED FROM the scoped ones rather than counted a
        # second time. Two loops over the same rows would be two figures free
        # to disagree, which is the defect this repo files as #4648; summing
        # makes "pooled == main + subagent" true by construction, and
        # `tests/test_serve.py` asserts it band by band anyway.
        pooled = {
            "calls": sum(s["calls"] for s in scopes.values()),
            "sample_calls": sum(s["sample_calls"] for s in scopes.values()),
            "banded": {
                key: sum(s["banded"][key] for s in scopes.values())
                for key, *_ in BANDS
            },
            "unknown_model_calls": sum(
                s["unknown_model_calls"] for s in scopes.values()
            ),
            "unknown_models": set().union(
                *(s["unknown_models"] for s in scopes.values())
            ),
            "over_window_calls": sum(s["over_window_calls"] for s in scopes.values()),
        }
        unmeasured_calls = pooled["calls"] - pooled["sample_calls"]
        # Every kind the window actually holds, in a fixed order, with the two
        # KNOWN kinds always present even when they ran nothing. An unforeseen
        # kind is appended rather than dropped: dropping it would silently
        # break `pooled == sum(by_scope)` and lose calls out of a denominator,
        # which is the exact failure this block exists to prevent.
        kinds = list(SCOPE_ORDER) + sorted(k for k in scopes if k not in SCOPE_ORDER)
        by_scope = [self._scoped_utilisation(k, scopes.get(k)) for k in kinds]
        pooled_scoped = self._scoped_utilisation(None, pooled)
        # ONE row wins the ranking, and the winner's own figures travel with
        # its name: `worst_scope` alone names a scope without saying what makes
        # it the worst, and the reader would have to open the meters to find
        # out. Read off the winning row rather than re-derived, so the answer
        # and the meter behind it cannot disagree.
        worst = self._worst_saturated_scope(by_scope)
        sizes.sort()
        growth = self._growth_curve(growth_points)
        percentiles = {f"p{p}": nearest_rank(sizes, p) for p in PERCENTILES}
        median = percentiles[f"p{MEDIAN_PERCENTILE}"]
        mean = (sum(sizes) / len(sizes)) if sizes else None
        # Strictly greater: a call AT the mean has not exceeded it.
        calls_above_mean = (
            len(sizes) - bisect.bisect_right(sizes, mean) if mean is not None else None
        )
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
                # ONE provenance pair for the whole block, never repeated per
                # scope: the documented window and the judged boundaries are
                # facts about `context_window.py`, and a per-scope copy would
                # assert they could differ by scope.
                "windows_as_of": WINDOWS_AS_OF,
                "window_provenance": WINDOW_PROVENANCE,
                "bands_as_of": BANDS_AS_OF,
                "band_provenance": BAND_PROVENANCE,
                # The POOLED tally, and the name of the set it ranges over
                # (#61). `includes` is not decoration: without it this is a
                # share of a set the reader cannot see, and the reader's own
                # scope is diluted in it 6:1.
                "includes": SCOPE_INCLUDES_BOTH,
                **pooled_scoped,
                # The SCOPED tallies -- one entry per scope, always both known
                # kinds, in `SCOPE_ORDER`.
                "by_scope": by_scope,
                # #65: WHICH SCOPE IS THE PROBLEM, answered here rather than
                # left to the page to work out from the tallies. A ranking must
                # name the key it orders by and the name must BE the key
                # (`RANKED_BY`'s rule), so the phrase below is the one
                # `_worst_saturated_scope()` maximises and the one the page
                # puts in the sentence.
                "worst_scope": worst["scope"] if worst else None,
                "worst_scope_ranked_by": SATURATION_RANKED_BY,
                # #88: the VALUE of the key the ranking ordered by, published
                # beside the winner it produced. Null when no scope ranked --
                # never 0, which would report the scope that measured nothing
                # as the most frugal one.
                "worst_scope_over_half_window_calls": (
                    worst["over_half_window_calls"] if worst else None
                ),
                "worst_scope_over_half_window_share": (
                    worst["over_half_window_share"] if worst else None
                ),
                # #88: and whether any of that is a problem, which is the
                # question the card's heading asks and the one nothing answered.
                "answer": self._context_answer(worst, pooled_scoped, len(sizes)),
            },
            # #65: the mechanism behind the bands. Its own block rather than a
            # field on `utilisation`, because it ranges over ONE scope and over
            # four spans of time, which is neither of the two sets `utilisation`
            # describes.
            "growth": growth,
        }

    @staticmethod
    def _worst_saturated_scope(
        by_scope: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """The scope ROW with the largest `over_half_window_share` (#65).

        None when NO scope has a share at all -- every one of them has an empty
        banded sample, so there is no ranking rather than a ranking whose
        winner is a scope that measured nothing. A share of 0.0 is a real
        reading and DOES rank: "the pressure is in your main session, and it is
        currently none" is a true and useful sentence, while "the worst scope
        is the one we never measured" is not.

        Ties go to the FIRST scope in `SCOPE_ORDER`, which `max()` gives
        without a tiebreaker because it returns the first maximal element. That
        is the main thread, which is the scope a reader can act on.

        **The whole ROW, not the name** (#88). The card states the share and
        the count that make this scope the winner, and taking them off the row
        the ranking returned is what stops the answer and the meter below it
        from being two derivations of one figure.
        """
        ranked = [s for s in by_scope if s["over_half_window_share"] is not None]
        if not ranked:
            return None
        return max(ranked, key=lambda s: s["over_half_window_share"])

    @staticmethod
    def _context_answer(
        worst: Optional[dict[str, Any]],
        pooled: dict[str, Any],
        sample_calls: int,
    ) -> dict[str, Any]:
        """Whether this period is wasting context, as one of five states (#88).

        The card's heading is a question, so its first line must answer that
        question -- and the answer has to be DERIVED, because a sentence
        written into the template is a claim nothing can check. This branch
        already shipped one of those ("And it only ever grows.") and the same
        database contradicted it three hours later.

        **The order of the branches is the judgment.** A proven reading is
        reported before an absent one: `yes` is decided from the POOLED count
        at or above the judged boundary, so it holds however the unmeasured,
        unwindowed and over-window calls fall around it. Only where nothing
        reached the boundary can an unknown weaken the answer -- and there it
        must, because "no scope reached it, and a third of the period could not
        be looked at" is not the same claim as "no scope reached it".

        `no` is therefore the ONE state that asserts a complete clean sample,
        which is what makes it safe to say plainly. Every other state names
        what it could not establish.
        """
        if not sample_calls:
            verdict = CONTEXT_ANSWER_NO_SAMPLE
        elif worst is None:
            verdict = CONTEXT_ANSWER_UNKNOWN
        elif pooled["over_half_window_calls"]:
            verdict = CONTEXT_ANSWER_YES
        elif (
            pooled["unmeasured_calls"]
            or pooled["unknown_model_calls"]
            or pooled["over_window_calls"]
        ):
            verdict = CONTEXT_ANSWER_INCONCLUSIVE
        else:
            verdict = CONTEXT_ANSWER_NO
        return {
            "verdict": verdict,
            "statement": CONTEXT_ANSWER_STATEMENTS[verdict],
        }

    @classmethod
    def _growth_curve(
        cls, points: list[tuple[float, int, Optional[float]]]
    ) -> dict[str, Any]:
        """Typical main-thread context across four spans of the period (#65).

        `points` is `(ts, context_size, utilisation or None)` for every
        main-thread call in the window whose context was MEASURED -- the same
        predicate the bands are drawn through, collected in the same pass.

        **The quarters are equal spans of TIME, not equal counts of calls.**
        Equal counts can never leave a quarter empty, which would make "a
        quarter with no measured call is a named absence" unreachable code and
        would quietly redefine the finding: the claim is about a session's
        LIFE, and a session that ran 900 calls in one hour and 30 over the next
        week did not spend half its life on either.

        **A quarter with no call has no median.** It is emitted with `calls: 0`
        and a `no_sample_reason`, and every median beside it is null -- a
        plotted 0 would draw the context collapsing in a quarter that was
        merely idle, which is the same defect `timeseries()`'s null
        `avg_context` fixed for the daily chart.

        **Two medians, two samples, two counts.** `median_context` ranges over
        the quarter's measured calls; `median_utilisation` ranges over the
        subset of those whose model has a documented window, which is a
        strictly smaller set whenever an unknown model ran. `banded_calls`
        beside it is what keeps them from being read as one figure.

        **The curve can be REFUSED, and says so.** `refused_reason` is non-null
        exactly when the sample cannot support a trend -- see
        `GROWTH_MIN_CALLS`, whose floor is derived from `nearest_rank`'s own
        arithmetic rather than judged. The quarters are still published when it
        is refused: their counts are true, and withholding them would replace
        one over-claim with an absence nobody asked for. What the page must not
        do is draw a trend through them.
        """
        points = sorted(points, key=lambda p: p[0])
        calls = len(points)
        first_ts = points[0][0] if points else None
        last_ts = points[-1][0] if points else None
        span = (last_ts - first_ts) if points else None
        refused: Optional[str] = None
        if calls < GROWTH_MIN_CALLS:
            refused = GROWTH_REFUSED_TOO_FEW
        elif not span:
            refused = GROWTH_REFUSED_NO_SPAN
        buckets: list[list[tuple[int, Optional[float]]]] = [
            [] for _ in range(GROWTH_QUARTERS)
        ]
        for ts, size, fraction in points:
            # The LAST quarter is closed at the top, so the final call -- which
            # sits exactly on `last_ts` -- lands in quarter 4 rather than in a
            # fifth bucket that does not exist. With no span at all every call
            # shares one instant and they all land there, which is true: the
            # period is a point and its end is that point.
            index = GROWTH_QUARTERS - 1
            if span:
                index = min(
                    GROWTH_QUARTERS - 1,
                    int((ts - first_ts) / span * GROWTH_QUARTERS),
                )
            buckets[index].append((size, fraction))
        quarters = []
        for i, bucket in enumerate(buckets):
            sizes = sorted(size for size, _ in bucket)
            fractions = sorted(f for _, f in bucket if f is not None)
            quarters.append({
                "quarter": i + 1,
                # Null rather than the window's own edges when there is no
                # sample to derive them from: an empty scope has no period.
                "from_ts": (first_ts + span * i / GROWTH_QUARTERS)
                if span else first_ts,
                "to_ts": (first_ts + span * (i + 1) / GROWTH_QUARTERS)
                if span else last_ts,
                "calls": len(bucket),
                "median_context": nearest_rank(sizes, MEDIAN_PERCENTILE),
                "banded_calls": len(fractions),
                # `nearest_rank` returns an element of the list it is given, so
                # it reports a utilisation some call actually carried -- the
                # same reason the context percentiles use it.
                "median_utilisation": nearest_rank(fractions, MEDIAN_PERCENTILE),
                "no_sample_reason": None if bucket else GROWTH_QUARTER_NO_CALLS,
            })
        shape, peak_quarter = cls._growth_shape(quarters, refused)
        return {
            # WHICH SCOPE, HOW MANY REPLIES, OVER WHAT PERIOD -- the three
            # things an aggregate owes the reader about the set it ranges over.
            "scope": SCOPE_LABELS.get(GROWTH_SCOPE, GROWTH_SCOPE),
            "sample_is": GROWTH_SAMPLE,
            "calls": calls,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "minimum_calls": GROWTH_MIN_CALLS,
            "refused_reason": refused,
            # The SENTENCE over the curve, derived from the curve. See
            # `_growth_shape()` for why this is a field rather than a heading.
            "shape": shape,
            "shape_statement": GROWTH_SHAPE_STATEMENTS[shape],
            # The judged half, with its own date, in the shape `band_provenance`
            # established: the medians are measurements and this threshold is
            # not, so they cross the API as separate fields and the page marks
            # them as separate voices.
            "shape_as_of": GROWTH_SHAPE_AS_OF,
            "shape_provenance": GROWTH_SHAPE_PROVENANCE,
            "peak_quarter": peak_quarter,
            "quarters": quarters,
        }

    @staticmethod
    def _growth_shape(
        quarters: list[dict[str, Any]], refused: Optional[str]
    ) -> tuple[str, Optional[int]]:
        """Which of `GROWTH_SHAPES` this curve is, and where its peak sits.

        **A QUARTER WITH NO SAMPLE DOES NOT PARTICIPATE.** The sequence is the
        quarters that have a median, in order, and an empty one is SKIPPED
        rather than carried in as a 0 -- which would turn every idle fortnight
        into a collapse and then into a "rose then fell". This is the same rule
        the null median beside it already follows, one level up: absence is not
        a value, including when it is a value in a trend.

        **A REFUSED CURVE HAS NO SHAPE.** If the sample cannot support a trend
        (`refused_reason`), claiming one from it is exactly the over-claim the
        refusal exists to prevent, so it reports `unmeasurable` and says so.

        **THE DEFAULT IS REFUSAL.** Movement that fits none of the named shapes
        is `no-discernible-trend`, never the nearest-sounding finding: this
        block's whole reason for existing is that "it only ever grows" was
        picked because it read well and stopped being true the same afternoon.

        The branches are exhaustive over `(any material rise, any material
        fall)`:

          * neither -- `flat`;
          * rises only -- `rising`. Not "monotonic": a dip too small to count
            is allowed, which is why the sentence says "every change large
            enough to count" rather than "every change";
          * falls only -- `falling`;
          * both -- the interesting one. It is `rose-then-fell` only if the
            peak is genuinely between the ends, i.e. the climb from the first
            sampled quarter to the peak AND the drop from the peak to the last
            are both material. That test excludes a peak at either end by
            construction (the change to itself is 0), so a curve that sagged in
            the middle and recovered is `no-discernible-trend`, not a fall.

        `peak_quarter` is the quarter NUMBER holding the largest median, or
        None where no quarter has one. It is published for every shape because
        it is true for every shape, and it is the evidence a reader checks
        `rose-then-fell` against.
        """
        sampled = [
            (q["quarter"], q["median_context"])
            for q in quarters
            if q["median_context"] is not None
        ]
        peak_quarter = (
            max(sampled, key=lambda pair: pair[1])[0] if sampled else None
        )
        if refused is not None or len(sampled) < 2:
            return GROWTH_SHAPE_UNMEASURABLE, peak_quarter
        medians = [median for _quarter, median in sampled]
        # `median_context` is at least `MEASURED_CONTEXT_MIN`, so the
        # denominator cannot be zero; the guard states that rather than relying
        # on it, because a future change to what counts as measured would
        # otherwise turn this into a ZeroDivisionError inside a summary.
        steps = [
            (later - earlier) / earlier if earlier else 0.0
            for earlier, later in zip(medians, medians[1:])
        ]
        rose = any(step >= GROWTH_MATERIAL_CHANGE for step in steps)
        fell = any(step <= -GROWTH_MATERIAL_CHANGE for step in steps)
        if not rose and not fell:
            return GROWTH_SHAPE_FLAT, peak_quarter
        if rose and not fell:
            return GROWTH_SHAPE_RISING, peak_quarter
        if fell and not rose:
            return GROWTH_SHAPE_FALLING, peak_quarter
        peak = max(medians)
        climb = (peak - medians[0]) / medians[0] if medians[0] else 0.0
        drop = (medians[-1] - peak) / peak if peak else 0.0
        if climb >= GROWTH_MATERIAL_CHANGE and drop <= -GROWTH_MATERIAL_CHANGE:
            return GROWTH_SHAPE_ROSE_THEN_FELL, peak_quarter
        return GROWTH_SHAPE_MIXED, peak_quarter

    @staticmethod
    def _utilisation_bands(
        banded: dict[str, int], banded_calls: int
    ) -> list[dict[str, Any]]:
        """`BANDS` with one set of calls counted into them, high to low.

        One spelling, used by the pooled tally and by every scoped one, so a
        band that gained a field in one place cannot be missing it in another.
        """
        return [
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
        ]

    @classmethod
    def _scoped_utilisation(
        cls, kind: Optional[str], tally: Optional[dict[str, Any]]
    ) -> dict[str, Any]:
        """One band tally, named (#61).

        `kind` is the STORED `source_kind` for a scoped tally, or None for the
        pooled one -- which carries `includes` instead and so must not also
        carry a `scope`, or the payload would name one set twice in two
        vocabularies.

        `tally` is None for a scope the window holds no row for at all. That is
        a real absence and it is emitted, not omitted: `calls: 0` corroborated
        by `no_sample_reason`, with every `share` null rather than 0.0.
        """
        if tally is None:
            tally = {
                "calls": 0,
                "sample_calls": 0,
                "banded": {key: 0 for key, *_ in BANDS},
                "unknown_model_calls": 0,
                "unknown_models": set(),
                "over_window_calls": 0,
            }
        calls = tally["calls"]
        sample_calls = tally["sample_calls"]
        banded_calls = sum(tally["banded"].values())
        named = {"scope": SCOPE_LABELS.get(kind, kind)} if kind is not None else {}
        # #65: the saturation reading, DERIVED from this tally's own bands
        # rather than counted a second time -- and derived from
        # `OVER_HALF_WINDOW_BANDS`, which is itself derived from `BANDS`, so a
        # band table that grew a cut at 0.6 joins the numerator instead of
        # being silently dropped out of it. Null share on an empty banded
        # sample: a share of an empty set is not 0%.
        over_half = sum(
            count
            for key, count in tally["banded"].items()
            if key in OVER_HALF_WINDOW_BANDS
        )
        return {
            **named,
            "calls": calls,
            "sample_calls": sample_calls,
            "over_half_window_calls": over_half,
            "over_half_window_share": (
                (over_half / banded_calls) if banded_calls else None
            ),
            # The REMAINDER, as in `context_aggregate_sql()`: the two counts
            # partition `calls` by construction rather than by a second
            # predicate free to drift from the first.
            "unmeasured_calls": calls - sample_calls,
            "banded_calls": banded_calls,
            "bands": cls._utilisation_bands(tally["banded"], banded_calls),
            "unknown_model_calls": tally["unknown_model_calls"],
            "unknown_models": sorted(tally["unknown_models"]),
            "over_window_calls": tally["over_window_calls"],
            "no_sample_reason": cls._no_band_sample_reason(
                calls, sample_calls, banded_calls
            ),
        }

    # ----------------------------------------------------------------------
    # #78: the recommendation table, evaluated here and only here
    # ----------------------------------------------------------------------

    def _recommendations(
        self, start: float, end: float, context: dict[str, Any]
    ) -> dict[str, Any]:
        """What the window's own figures fall in `recommendations.METRICS`.

        The consumer `recommendations.py` shipped without (#78). The lookup is
        arithmetic and the advice is data, so the same corpus always yields the
        same advice and a fixed problem makes its recommendation STOP firing --
        which is what makes the report dynamic without making it generative
        (constraint 3: no model produces a figure).

        **The five values are computed from each metric's `measurement` string,
        not from its key.** That field names exactly what is divided by what,
        and it is the thing a reader checks the advice against; a query that
        agreed with the key and disagreed with the measurement would be a wrong
        number that reads right. Three of the five say so in as many words --
        `main_vs_subagent_tokens_per_reply` is per API CALL, not per assistant
        turn, whatever "reply" suggests, and
        `cache_write_repayment_at_own_ttl` names the set it ranges over as the
        calls whose per-TTL split was measured, which is not the set beside it.

        **A zero denominator yields None, never 0.** `assess()` refuses a
        non-finite value precisely so this cannot arrive as `inf`, and a share
        of an empty set is not 0% (the rule `_utilisation_bands()` already
        applies one block up). A project that never dispatched a subagent has
        NO ratio of main to subagent tokens -- not a ratio of zero, and not a
        healthy one -- so it is passed as None and comes back named in
        `unmeasured`.

        **The mapping handed to `assess_all()` is COMPLETE**, every metric
        present with None where there is no sample. The module refuses a
        partial one, and that refusal is right: a caller that forgot a metric
        and a caller that measured nothing would otherwise produce the same
        page, and only one of them is telling the truth. What that refusal
        cannot do is fire before a request exists, so completeness is stated
        separately as `RECOMMENDED_METRICS` and checked at import (#84).

        `context` is passed in rather than recomputed, so the main-thread
        saturation share is read off the SAME per-scope band tally the page
        renders. A second query would be a second definition of "over half the
        window", free to drift from the one the reader is looking at -- the
        defect `avg_context` and `context.mean` were joined at the hip to stop
        (#25).
        """
        readings: dict[str, Reading] = {
            METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW: (
                self._main_thread_over_half_window_share(context)
            ),
            **self._cache_reuse_metrics(start, end),
            METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL: (
                self._cache_write_repayment_at_own_ttl(start, end)
            ),
            METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY: (
                self._main_vs_subagent_tokens_per_call(start, end)
            ),
        }
        assessed = assess_all(readings)
        return {
            # Two fields, as `context.utilisation` carries `bands_as_of` beside
            # `band_provenance`: the date the judgments were last decided is a
            # fact a reader weighs on its own, and burying it inside a sentence
            # makes it something they have to parse out.
            "as_of": RECOMMENDATIONS_AS_OF,
            "provenance": RECOMMENDATION_PROVENANCE,
            # The ORDER is itself a judgment and carries its own provenance --
            # `RANKED_BY`'s discipline (a ranking must name the key it orders
            # by) at the point where the key is a derived depth rather than a
            # column.
            "ranking_provenance": RANKING_PROVENANCE,
            # #93's third state, said once. A DIFFERENT DATE from `as_of`
            # above, because the ratios' floor is a judgment taken on its own
            # day and the boundaries' date must not be made to cover it -- the
            # `bands_as_of` / `band_provenance` separation (#31), one axis over.
            # The shares' floors carry no date at all: they are arithmetic over
            # boundaries this table already dates, and they move when those do.
            "sample_floor_as_of": SAMPLE_FLOOR_AS_OF,
            "unmeasured_note": UNMEASURED_NOTE,
            "under_sampled_note": UNDER_SAMPLED_NOTE,
            "ranked": [self._assessment_payload(a) for a in assessed.ranked],
            # #89's summary level, in the SAME order and off the SAME
            # `Assessment` objects the diagnosis one level down reads. One row
            # per metric, measured or not, so "four knobs exist and two are
            # already fine" is a thing the page can show rather than infer from
            # a list that dropped the healthy ones.
            "knobs": self._knobs(assessed),
            # A MAPPING, metric -> what would have been measured, not a list of
            # rows: an unmeasured metric has no reading, no severity and no
            # advice, so there are no columns to tabulate. `UNMEASURED_NOTE`
            # above is the one thing there is to say about every member, said
            # once. The page consumes it whole, exactly as it consumes
            # `context.percentiles`, so every member reaches a reader by
            # construction however many there are.
            "unmeasured": {
                key: METRICS[key].measurement for key in assessed.unmeasured
            },
            # THE THIRD STATE, AND IT IS NOT A SECOND `unmeasured`. Same shape
            # -- a mapping the page consumes whole, so every member reaches a
            # reader however many there are -- and a different sentence,
            # because these metrics HAVE a reading. What each says is how much
            # of its own denominator this period holds against how much the
            # bands it would be compared with require, which is the only thing
            # that makes "51" mean anything.
            "under_sampled": {
                u.metric: self._shortfall_sentence(u) for u in assessed.under_sampled
            },
        }

    @staticmethod
    def _shortfall_sentence(under: UnderSampled) -> str:
        """How short one metric's sample is, in the floor's own words.

        COMPOSED HERE AND NOT IN THE PAGE, for the reason no advice on that
        page is authored there: the sentence names a number, and a number
        rendered beside a noun the page chose would be a second statement of
        what the floor counts. `SampleFloor.counts` is the table's own phrase
        and this only puts it in a sentence.
        """
        return (
            f"{under.sample_size} of the {under.floor.minimum} "
            f"{under.floor.counts} this reading needs before its bands mean "
            f"anything -- {under.shortfall} short"
        )

    @staticmethod
    def _main_thread_over_half_window_share(
        context: dict[str, Any],
    ) -> Reading:
        """`METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW`, off the rendered tally.

        Numerator: main-thread banded calls in `OVER_HALF_WINDOW_BANDS`.
        Denominator: main-thread calls with a known window, which is exactly
        `banded_calls` -- a call is banded if and only if its context was
        measured AND its model has a documented window.

        None when that denominator is 0, which is three different absences
        (`no_sample_reason` says which) and no share at all.

        **The share is READ, not recomputed.** #65 made it a published field of
        the scope's own tally, because the page states which scope is worst by
        ranking on it; a second summation here would be a second definition of
        "over half the window", free to drift from the one the reader is
        looking at and from the one the ranking used. That is the same reason
        `context` is passed into `_recommendations()` rather than rebuilt.

        **The sample size is that same denominator** (#93), read off the same
        row: `banded_calls`, not the period's call count and not this scope's.
        A window of a thousand main-thread calls of which nine were banded is a
        share over nine, and the floor is owed the nine.
        """
        for scope in context["utilisation"]["by_scope"]:
            if scope["scope"] != SCOPE_MAIN:
                continue
            return Reading(
                scope["over_half_window_share"], scope["banded_calls"]
            )
        # Unreachable while `_context()` emits every scope in `SCOPE_ORDER`,
        # and None rather than 0.0 if that ever changes: a main-thread share
        # this function could not find is not a main-thread share of nothing.
        return Reading(None, 0)

    def _cache_reuse_metrics(self, start: float, end: float) -> dict[str, Reading]:
        """`METRIC_CACHE_READS_PER_WRITE` and `METRIC_CACHE_WRITE_ONLY_SHARE`.

        One pass, because both range over the same rows and a second query
        would be a second sample. Both scopes, which is what the first
        metric's `measurement` says -- a prefix stored by a subagent and read
        back by it is the same arithmetic as the main thread's.

        Both are undefined when no call wrote cache, and the two denominators
        are different quantities that happen to vanish together: `SUM(cache_
        write)` is tokens and `writing_calls` is calls. Each is checked on its
        own rather than one standing in for the other.

        `SUM` over no rows is SQL NULL, which is the same answer as 0 here --
        no call wrote cache -- and both take the None branch.

        Every call in the window, unfiltered, which is what separates this from
        `_cache_write_repayment_at_own_ttl()` one method down: that one ranges
        over the calls whose per-TTL split was measured and must not borrow
        this denominator, nor lend it these reads (#84).

        **ONE SAMPLE COUNT SERVES BOTH, AND IT IS `writing_calls`** (#93). It is
        `cache_write_only_share`'s literal denominator; for
        `cache_reads_per_write` the denominator is TOKENS, and the calls that
        contributed them are these same rows -- a period where one call wrote
        cache has one call setting the whole ratio, however many tokens it
        wrote. The two floors over that one count are still different numbers
        (51 derived against 10 judged), because how many members each needs is
        the table's question and not this query's.
        """
        row = self.conn.execute(
            "SELECT SUM(cache_read) reads, SUM(cache_write) writes,"
            " SUM(CASE WHEN cache_write > 0 THEN 1 ELSE 0 END) writing_calls,"
            " SUM(CASE WHEN cache_write > 0 AND cache_read = 0 THEN 1 ELSE 0 END)"
            "   write_only_calls"
            " FROM api_calls WHERE ts >= ? AND ts < ?",
            (start, end),
        ).fetchone()
        writes = row["writes"]
        # `SUM` over no rows is SQL NULL; the count of a set with no members is
        # a real 0, and the two must not both arrive as None.
        writing_calls = row["writing_calls"] or 0
        return {
            METRIC_CACHE_READS_PER_WRITE: Reading(
                (row["reads"] / writes) if writes else None, writing_calls
            ),
            METRIC_CACHE_WRITE_ONLY_SHARE: Reading(
                (row["write_only_calls"] / writing_calls) if writing_calls else None,
                writing_calls,
            ),
        }

    def _cache_write_repayment_at_own_ttl(self, start: float, end: float) -> Reading:
        """`METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL`, over ONE set of calls.

        A THIRD query rather than two more columns on `_cache_reuse_metrics()`'s
        one, and the extra query IS the reason: that one ranges over every call
        in the window, this one over the calls whose per-TTL cache-write split
        was measured (#84). Two different sets, so a single `SELECT` could only
        answer both by handing one of them the other's denominator.

        **NUMERATOR AND DENOMINATOR COME FROM THE SAME ROWS.** The `WHERE`
        picks the set and the reads are summed INSIDE it, never over the whole
        window. A call ingested before the split was read, or one whose record
        carried only one of the two TTL keys, contributes no write token to the
        denominator -- so its read tokens must contribute nothing to the
        numerator either. Mixing them would report reads repaying writes CPB
        never saw, and the error runs one way only: the ratio inflates by
        exactly the traffic it cannot account for. On a database not
        re-ingested since #84 -- every database today -- that is every read in
        the window over a denominator of nothing.

        **Both TTLs are required**, because `cache_write_5m IS NULL` means
        UNMEASURED and never 0. A 1-hour write whose column was read as absent
        would be counted at the 5-minute break-even and reported repaid a read
        early; `ingest.Call.cache_write_split_total` refuses a partial split on
        the write path for the same reason, and this is the read path's half.

        `SUM` over no matching row is SQL NULL, which is the state of every
        database that predates the split, and `cache_write_repayment()` turns
        it into None -- so the metric is NAMED in `unmeasured` rather than
        banded as the worst reading a project could have. The weighting stays
        in that function and is deliberately not written into this query:
        which multiplier goes with which TTL is the CITED boundary, and a
        `2 *` here would be free to drift from the citation justifying it.

        **The sample is counted inside the same `WHERE`** (#93), and counts the
        calls that actually WROTE at one of the two TTLs -- not every call whose
        split was readable. A call with a measured split of 0 and 0 contributes
        nothing to the denominator, so counting it would report a sample that
        is not holding the ratio up. Borrowing `_cache_reuse_metrics()`'
        `writing_calls` would be worse still: those are a different set of rows,
        which is the whole reason this is a third query.
        """
        row = self.conn.execute(
            "SELECT SUM(cache_read) reads, SUM(cache_write_5m) write_5m,"
            " SUM(cache_write_1h) write_1h,"
            " SUM(CASE WHEN cache_write_5m + cache_write_1h > 0 THEN 1 ELSE 0 END)"
            "   writing_calls"
            " FROM api_calls"
            " WHERE ts >= ? AND ts < ?"
            " AND cache_write_5m IS NOT NULL AND cache_write_1h IS NOT NULL",
            (start, end),
        ).fetchone()
        return Reading(
            cache_write_repayment(row["reads"], row["write_5m"], row["write_1h"]),
            row["writing_calls"] or 0,
        )

    def _main_vs_subagent_tokens_per_call(self, start: float, end: float) -> Reading:
        """`METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY`, per the measurement.

        Mean total tokens per main-thread API CALL over mean total tokens per
        subagent API call. The metric's key says "per reply" and its
        `measurement` says "per API call"; the measurement is the one that
        names what is divided by what, so it is the one implemented.

        None unless BOTH scopes ran at least one call. A project that never
        dispatched a subagent has no ratio -- the module's own words -- and an
        empty subagent scope divided into a main-thread mean is exactly the
        `inf` `assess()` refuses. None too if the subagent mean is 0, which is
        a real reading (calls that reported no tokens at all) but still a zero
        denominator, and so still not a ratio.

        **THE SAMPLE IS THE SMALLER SCOPE, NEVER THE POOLED COUNT** (#93). Both
        means are means over their own scope, so the reading is only as sampled
        as its thinner side: a window with 4,000 main-thread calls and one
        subagent reply has a DEFINED ratio whose whole subagent term is that one
        reply, and a pooled 4,001 would wave it through with room to spare. The
        refusal already in this method is a different absence -- a scope with no
        call has no ratio rather than an under-sampled one -- and neither
        substitutes for the other.
        """
        means: dict[str, float] = {}
        calls: dict[str, int] = {}
        for row in self.conn.execute(
            f"SELECT source_kind, COUNT(*) calls, SUM({TOTAL_TOKENS_SQL}) total"
            " FROM api_calls WHERE ts >= ? AND ts < ? GROUP BY source_kind",
            (start, end),
        ):
            if row["calls"]:
                means[row["source_kind"]] = (row["total"] or 0) / row["calls"]
                calls[row["source_kind"]] = row["calls"]
        sample = min(calls.get(SOURCE_MAIN, 0), calls.get(SOURCE_SUBAGENT, 0))
        main = means.get(SOURCE_MAIN)
        subagent = means.get(SOURCE_SUBAGENT)
        if main is None or not subagent:
            return Reading(None, sample)
        return Reading(main / subagent, sample)

    # ----------------------------------------------------------------------
    # #89: the same table, drawn -- the summary level's knobs and gauges
    # ----------------------------------------------------------------------

    @classmethod
    def _sample_block(
        cls, metric_key: str, state: str, size: int, note: Optional[str]
    ) -> dict[str, Any]:
        """What this window holds of one metric's denominator, and what it needs.

        THE FLOOR AND ITS VOICE TRAVEL TOGETHER. `rule`, `provenance_kind` and
        `provenance_statement` are the floor's own, so a derived floor and a
        judged one are distinguishable on the page without a reader leaving it
        -- `band_provenance` beside `window_provenance` (#31), and the
        per-boundary `Provenance` (#78), at the grain of the sample size. A
        judged floor that reached the page in the derived one's voice would be
        exactly the borrowed authority both of those exist to refuse.

        `note` is non-null EXACTLY when the state is under-sampled -- the
        tri-state-with-a-reason shape `no_sample_reason` and
        `stale_unknown_reason` already use. A measured metric has no shortfall
        to explain and an unmeasured one has no reading to explain it about.
        """
        floor = METRICS[metric_key].sample_floor
        return {
            "state": state,
            # HOW MANY, and OF WHAT. The count alone is meaningless: "3" is a
            # different fact against 51 cache-writing calls than against 51
            # calls, and `counts` is the table's own phrase for the set.
            "size": size,
            "minimum": floor.minimum,
            "counts": floor.counts,
            "rule": floor.rule,
            "provenance_kind": floor.provenance.kind,
            "provenance_statement": floor.provenance.statement,
            "note": note,
        }

    @classmethod
    def _knobs(cls, assessed: Assessments) -> list[dict[str, Any]]:
        """One row per metric, worst first, then the ones with no sample.

        THE ORDER IS THE TABLE'S. `assessed.ranked` is already sorted by
        `recommendations.rank()` -- severity, then depth into the severity
        band, then key -- and nothing here re-sorts it. A second ordering in
        this file would be a second judgment about which lever matters most,
        undated and with no provenance, which is `RANKING_PROVENANCE`'s whole
        subject.

        THE UNMEASURED ONES ARE ROWS, not an omission. A knob whose reading has
        no sample still exists, and the summary shows it dimmed with an empty
        gauge: seeing that four knobs exist and two are already fine is what
        stops the page becoming a list of complaints, and a metric that simply
        vanished would be indistinguishable from a healthy one -- the defect
        the table's explicit healthy entry exists to prevent, at the level
        where it is hardest to see.

        THREE STATES, THREE ROWS, AND EVERY ROW SAYS WHICH (#93). Between the
        two above sits the under-sampled one: it HAS a reading, so it is not
        unmeasured, and its sample cannot carry a verdict, so it has no
        severity and no directive. It keeps its `value` -- the number was
        measured and is true, and withholding it would be its own small
        dishonesty -- and it gets NO NEEDLE. A needle sitting under the green
        arc IS the verdict, drawn; `cache_reads_per_write` reading 24.0 over
        three calls would otherwise say "do not change this" in a picture after
        the words had stopped saying it.

        Every row carries `sample`, measured ones included. "51 of 51" is the
        evidence that a green row earned its green, and a page that showed the
        count only where it was short would make the floor look like an error
        message rather than a standing condition.

        EVERY FIGURE IS READ OFF THE SAME `Assessment` THE LEVEL BELOW READS.
        Not recomputed, not re-derived: `_assessment_payload()` and this method
        are two renderings of one object, so the summary and the diagnosis
        cannot disagree about a value, a severity or a directive. That is the
        `RANKED_BY` discipline applied to a page split into levels, and
        tests/test_serve.py asserts the two agree field by field.
        """
        rows = [
            {
                "metric": a.metric,
                "measurement": a.measurement,
                # WHAT THE NUMBER MEANS TO THE READER, beside the
                # specification and never instead of it (#89 review). The
                # summary row printed `measurement` because nothing else
                # existed, and `measurement` is a definition -- exactly right
                # under "Measures:" one level down, two lines of jargon on a
                # row of advice. Both are the table's own words: a sentence
                # composed here would be a claim with no date and no owner,
                # which is what `RECOMMENDATION_PROVENANCE` is about.
                "means": METRICS[a.metric].means,
                "value": a.value,
                "unit": METRICS[a.metric].unit,
                "severity": a.severity,
                # The module's own action-oriented phrase, composed from a
                # closed registry inside `Lever`. Never assembled here: a
                # directive built in this file from `action` and `target` would
                # route around the guard that makes "reduce your cache reads"
                # unrepresentable rather than merely absent.
                "directive": None if a.lever is None else a.lever.directive,
                "gauge": cls._gauge(METRICS[a.metric], a.value),
                "sample": cls._sample_block(
                    a.metric,
                    SAMPLE_MEASURED,
                    assessed.sample_sizes[a.metric],
                    None,
                ),
            }
            for a in assessed.ranked
        ]
        rows.extend(
            {
                "metric": u.metric,
                "measurement": u.measurement,
                "means": METRICS[u.metric].means,
                "unit": METRICS[u.metric].unit,
                # THE READING SURVIVES; THE VERDICT DOES NOT. `value` is the
                # number this window actually measured and `severity` is None
                # beside it, which is the whole of the third state: a figure
                # the reader may look at, and no claim about whether it is
                # good. `directive` is None for the same reason -- an
                # instruction is the strongest claim on this page and the last
                # thing a short sample may buy.
                "value": u.value,
                "severity": None,
                "directive": None,
                # No needle. See the class docstring: the needle's position
                # under a coloured arc is the verdict in a second notation.
                "gauge": cls._gauge(METRICS[u.metric], None),
                "sample": cls._sample_block(
                    u.metric,
                    SAMPLE_UNDER_SAMPLED,
                    u.sample_size,
                    cls._shortfall_sentence(u),
                ),
            }
            for u in assessed.under_sampled
        )
        rows.extend(
            {
                "metric": key,
                "measurement": METRICS[key].measurement,
                # A metric with no sample still MEANS what it means and still
                # HAS a unit: neither depends on whether this window measured
                # one, and the boundaries drawn on an empty dial are in that
                # unit whether or not a needle joins them. A row that lost its
                # sentence when the sample went missing would say less about
                # the absence than about the reading.
                "means": METRICS[key].means,
                "unit": METRICS[key].unit,
                # THREE NULLS, and none of them a zero. No reading, no
                # severity, nothing to do -- the gauge below carries no needle
                # for the same reason, and `unmeasured_note` beside it says so
                # in words.
                "value": None,
                "severity": None,
                "directive": None,
                "gauge": cls._gauge(METRICS[key], None),
                # An unmeasured metric still HAS a floor, and its sample size is
                # a real count rather than an absence -- usually 0, and not
                # always: a period can hold twenty calls whose per-TTL split was
                # read and whose writes were all zero, which is a denominator of
                # nothing over a sample of nothing. Published either way, so the
                # reader is never left inferring the count from the state.
                "sample": cls._sample_block(
                    key, SAMPLE_UNMEASURED, assessed.sample_sizes[key], None
                ),
            }
            for key in assessed.unmeasured
        )
        return rows

    @classmethod
    def _gauge(cls, metric: Metric, value: Optional[float]) -> dict[str, Any]:
        """A metric's ranges as a drawable sweep. NO NUMBER ORIGINATES HERE.

        The gauge is a VISUALISATION OF THE TABLE and needs no new judgment:
        the coloured arcs are the metric's own ranges with their own
        severities, the ticks are its own boundaries carrying their own
        per-boundary provenance, the target is the edge of its own healthy
        range, and the needle is this window's measured value. A boundary
        authored in this method, or in `index.html`, would be a threshold with
        no date, no provenance and nothing to redline -- the alternative
        `recommendations.py` was written to replace, reappearing in the layer
        that draws it.

        WHAT IS ADDED IS GEOMETRY, WHICH IS NOT A JUDGMENT. Each range gets an
        equal share of the sweep -- `1 / len(ranges)` -- and a value sits at
        the fraction of the way through its own range. Equal shares rather than
        a linear value axis because a linear one would need a maximum, and no
        metric here has one: the top range of every metric is unbounded on
        purpose, and inventing a ceiling to draw against is exactly the
        `depth_in_band()` refusal one module over.

        POSITION IS ALONG THE VALUE AXIS, NEVER ALONG HARM. `depth_in_band()`
        is called with `WORSE_WHEN_HIGHER` for every metric, which here means
        "further right", not "worse" -- it is the function's own left-to-right
        reading, and it carries the rule that an unbounded band is measured by
        the reciprocal of its entry boundary rather than against a made-up
        ceiling. Which end is the good one is a separate published field,
        `worse_when`, read straight off the metric.

        `needle` is None -- never 0.0 -- for a metric with no sample. A needle
        resting at the left of the dial is a reading of zero, which for four of
        the five metrics here is the WORST possible one; drawing absence there
        would be this repository's central rule failing in a new visual form.
        """
        ranges = metric.ranges
        span = len(ranges)
        target = cls._healthy_edge_index(metric)
        return {
            # Which direction the reader should want to move, from the table.
            "worse_when": metric.worse_when,
            "segments": [
                {
                    "severity": entry.recommendation.severity,
                    "start": i / span,
                    "end": (i + 1) / span,
                }
                for i, entry in enumerate(ranges)
            ],
            # The CUT POINTS, one per internal boundary. The first range's
            # lower edge is not one of them: it is the domain floor -- a share
            # cannot be negative -- so it is where the dial starts rather than
            # a line anybody drew. Each carries the KIND and the STATEMENT of
            # its own provenance, so a judged cut point and a documented one
            # are distinguishable on the dial itself and not only in a table
            # one level down (#31's `band_provenance` rule, at #78's grain).
            "boundaries": [
                {
                    "value": entry.lower.value,
                    "position": i / span,
                    "kind": entry.lower.provenance.kind,
                    "statement": entry.lower.provenance.statement,
                    # The one the reader is aiming for: the edge of the healthy
                    # range, which already exists and is already provenanced.
                    "is_target": i == target,
                }
                for i, entry in enumerate(ranges)
                if i > 0
            ],
            "needle": None if value is None else cls._gauge_position(metric, value),
        }

    @staticmethod
    def _healthy_edge_index(metric: Metric) -> Optional[int]:
        """Which boundary the healthy range ends at, by index into `ranges`.

        Derived from the table, twice over: which ranges are healthy is the
        entries' own severity, and which of the healthy run's two edges faces
        the harm is the metric's own `worse_when`. Nothing here decides where
        the number is.

        None when no range is healthy at all. No metric is shaped that way
        today, and the honest answer if one ever is would be that there is
        nothing to aim for -- not a target picked from whichever end came
        first.
        """
        healthy = [
            i
            for i, entry in enumerate(metric.ranges)
            if entry.recommendation.severity == SEVERITY_OK
        ]
        if not healthy:
            return None
        # `ranges` is ordered low to high, so the boundary between the healthy
        # run and its neighbour is above the run where higher is worse and
        # below it where lower is. Boundary `i` IS `ranges[i].lower`, and
        # adjacent ranges share one `Boundary` object, so either spelling names
        # the same number with the same provenance.
        if metric.worse_when == WORSE_WHEN_HIGHER:
            return healthy[-1] + 1
        return healthy[0]

    @staticmethod
    def _gauge_position(metric: Metric, value: float) -> float:
        """Where `value` sits along the sweep, in [0, 1].

        `range_for()` decides which range, so the drawing and the advice agree
        by construction: the needle cannot land under an arc the table would
        not have put it under.
        """
        span = len(metric.ranges)
        entry = metric.range_for(value)
        index = next(i for i, r in enumerate(metric.ranges) if r is entry)
        within = depth_in_band(
            value,
            entry.lower.value,
            None if entry.upper is None else entry.upper.value,
            WORSE_WHEN_HIGHER,
        )
        return (index + within) / span

    # ----------------------------------------------------------------------
    # #89: the four-dot status strip
    # ----------------------------------------------------------------------

    @classmethod
    def _status(
        cls,
        health: dict[str, Any],
        context: dict[str, Any],
        recommendations: dict[str, Any],
    ) -> dict[str, Any]:
        """The summary's one-line strip: four questions, four states.

        STATUS, NOT CONTENT. Each dot says which of four states its question is
        in and answers it in two or three words; everything that makes the
        answer true is one level down, on the card that already states it.

        EVERY ANSWER IS READ, NEVER RE-DERIVED. The three blocks arrive as
        arguments -- the same objects the rest of the payload carries -- so a
        dot cannot disagree with the card it summarises. A strip that ran its
        own queries would be a fifth opinion in a payload that has spent this
        much effort having one.
        """
        health_state, health_answer = STRIP_FROM_HEALTH[health["verdict"]]
        utilisation = context["utilisation"]
        context_verdict = utilisation["answer"]["verdict"]
        context_state, context_answer = STRIP_FROM_CONTEXT[context_verdict]
        # WHICH SCOPE, where there is a proven one and only there. The worst
        # scope is the ranking's own winner, off the same tally question 2
        # ranks on; where the answer is not a proven yes there is no winner to
        # name and the verdict already says so.
        if context_verdict == CONTEXT_ANSWER_YES and utilisation["worst_scope"]:
            context_answer = f"{context_answer} — {utilisation['worst_scope']}"
        knobs = recommendations["knobs"]
        # THE CONTEXT DOT INHERITS ITS BACKING METRIC'S FLOOR (#93). "Wasting
        # context?" is answered by `main_thread_share_over_half_window`, and a
        # dot may not state a verdict its own metric has just refused to state.
        #
        # THROUGH `STRIP_ORDER`, so no new precedence rule is invented here:
        # the floor contributes an UNKNOWN and the worst of the two wins. That
        # gives the direction this repository takes everywhere -- an unknown
        # WEAKENS a clean answer and NEVER softens a bad one, which
        # `CONTEXT_ANSWER_STATES` already spells out for the card. A proven
        # `yes` is a call observed at or above half its window; it happened,
        # the remedy is real, and a thin sample is no reason to hide it. A `no`
        # over five banded calls is the over-claim.
        context_state, context_answer = cls._floored(
            context_state,
            context_answer,
            next(k for k in knobs if k["metric"] == CONTEXT_DOT_METRIC),
        )
        turnable = [k for k in knobs if k["directive"]]
        # EVERY KNOB, INCLUDING THE ONES WITH NO SEVERITY (#93). This used to
        # filter the severity-less rows out and take the worst of what was
        # left, which is how a dot went green over readings nobody could take:
        # on a fresh install all five were severity-less and the strip reported
        # four good dots. A missing severity is now `STRIP_UNKNOWN` and joins
        # the comparison, where `STRIP_ORDER` already says what it does --
        # weaken a clean answer, never soften a bad one.
        knob_state = cls._worst_strip_state([k["severity"] for k in knobs])
        cache = [k for k in knobs if k["metric"] in CACHE_METRICS]
        cache_state = cls._worst_strip_state([k["severity"] for k in cache])
        answers = {
            STRIP_DOT_BROKEN: (health_state, health_answer),
            STRIP_DOT_CONTEXT: (context_state, context_answer),
            # A COUNT, not a verdict in words: "2 of 5" says both how many
            # knobs are worth turning and how many exist, and the second half
            # is what stops a page of two rows reading as a page of two
            # problems.
            STRIP_DOT_KNOBS: (knob_state, cls._knobs_answer(knobs, turnable)),
            STRIP_DOT_CACHE: (cache_state, cls._severity_answer(cache, cache_state)),
        }
        return {
            "dots": [
                {
                    "key": key,
                    "question": STRIP_QUESTIONS[key],
                    "state": answers[key][0],
                    "answer": answers[key][1],
                }
                for key in STRIP_DOTS
            ]
        }

    @staticmethod
    def _worst_strip_state(severities: list[Optional[str]]) -> str:
        """The worst of a run of table severities, as a strip state.

        NO SEVERITY ORDERING IS SPELLED HERE. `SEVERITY_RANK` is the module's
        own explicit ordering -- not alphabetical and not declaration order --
        and this reads it; the comparison across the FOUR strip states is
        `STRIP_ORDER`'s, for the same reason.

        A `None` SEVERITY IS `STRIP_UNKNOWN` AND IS COMPARED, NOT DROPPED
        (#93). It used to be filtered out by every caller, so a dot took the
        worst of whatever happened to be measured and went green over a set
        whose other members had no basis at all -- at the limit, over a set
        where none of them did. `STRIP_ORDER` already fixes what an unknown
        does when it meets a verdict: it sits between `watch` and `good`, so it
        weakens a clean answer and never softens a bad one. This function now
        applies that rule instead of leaving the caller to discard the case.

        An EMPTY run is `STRIP_UNKNOWN`, never `STRIP_GOOD`. No reading is not
        a clean reading, and a dot that went green because nothing was measured
        is the exact failure this project is arranged against.

        Returns the STATE ALONE. It used to return the state and an answer, and
        the answer for the empty case was a cache-specific string -- one dot's
        wording living inside the helper every dot shares. Each caller composes
        its own words now, off the state this returns.
        """
        states = [
            STRIP_UNKNOWN
            if severity is None
            else STRIP_FROM_SEVERITY[severity][0]
            for severity in severities
        ]
        if not states:
            return STRIP_UNKNOWN
        return min(states, key=STRIP_ORDER.index)

    @staticmethod
    def _no_basis_answer(rows: list[dict[str, Any]]) -> str:
        """WHICH absence a dot with no verdict is reporting (#93).

        Under-sampled beats unmeasured where both are present, and the
        direction is the useful one rather than the cautious one: if ANY of
        these readings is only waiting for more data, "come back after a few
        more sessions" is true and actionable for this dot. If none is, nothing
        here is waiting for anything, and saying so would be a promise the
        arithmetic will not keep.

        SHARED BY EVERY DOT THAT CAN HAVE NO VERDICT, so the two absences
        cannot be told apart on one dot and collapsed on another -- which is
        precisely what shipped in this change's first pass, where the knobs dot
        said "Not enough data yet" over an empty corpus that was waiting for
        nothing.
        """
        if any(row["sample"]["state"] == SAMPLE_UNDER_SAMPLED for row in rows):
            return STRIP_UNDER_SAMPLED
        return STRIP_NOT_MEASURED

    @classmethod
    def _floored(
        cls, state: str, answer: str, backing: dict[str, Any]
    ) -> tuple[str, str]:
        """One dot's state and words, held to its backing reading's floor.

        The floor contributes an UNKNOWN and `STRIP_ORDER` decides, so this
        adds no precedence rule of its own: a clean verdict is weakened, a bad
        one is not softened, and a dot already unknown keeps whatever more
        specific words it had (`no sample` and `unknown` say different things,
        and neither is improved by this sentence).

        UNDER-SAMPLED ONLY, NEVER UNMEASURED, and the difference is reachable
        rather than theoretical. A window holding only subagent calls has no
        main-thread share at all -- the metric is unmeasured because no such
        call ran, not because too few did -- while the context card still
        answers cleanly, every call in the period having been measured, banded
        and inside its window. There is nothing missing there and nothing to
        wait for, so "come back after a few more sessions" would be a promise
        with no arithmetic behind it. An absent question is not an unanswered
        one.
        """
        if backing["sample"]["state"] != SAMPLE_UNDER_SAMPLED:
            return state, answer
        weakened = min((state, STRIP_UNKNOWN), key=STRIP_ORDER.index)
        if weakened == state:
            return state, answer
        return weakened, STRIP_UNDER_SAMPLED

    @staticmethod
    def _knobs_answer(
        knobs: list[dict[str, Any]], turnable: list[dict[str, Any]]
    ) -> str:
        """The knobs dot's words: how many are worth turning, of how many.

        THE DENOMINATOR IS NEVER QUIETLY NARROWED. Where every knob has a
        basis this is the count it always was. Where some do not, the count
        stays over ALL of them and the shortfall is named beside it -- reporting
        "0 of 1" on a page showing five rows would be a second, smaller truth
        told in place of the first.

        Where NONE has a basis there is no count worth printing: "0 of 5" is
        arithmetic over an empty set, and it reads exactly like five checks
        that passed. That is the sentence a fresh install saw, and it is
        replaced by one that says what is actually true.
        """
        without_basis = [k for k in knobs if k["severity"] is None]
        if knobs and len(without_basis) == len(knobs):
            # WHICH absence, not merely that there is one. An empty corpus and
            # a fresh install both leave every knob without a verdict, and only
            # one of them is waiting for more sessions.
            return Api._no_basis_answer(knobs)
        answer = f"{len(turnable)} of {len(knobs)}"
        if without_basis:
            suffix = STRIP_KNOBS_SHORT_SUFFIX.format(short=len(without_basis))
            return f"{answer} — {suffix}"
        return answer

    @staticmethod
    def _severity_answer(rows: list[dict[str, Any]], state: str) -> str:
        """A dot's words where its readings carry severities (#93).

        The worst severity's own phrase where there is one. Where there is not,
        WHICH absence it is: a metric with a reading too thin to band sends the
        reader to "come back later", and one with no reading at all does not.
        Both were one string before, which made a first run and a project that
        never cached look identical.
        """
        if state != STRIP_UNKNOWN:
            worst = max(
                (row["severity"] for row in rows if row["severity"] is not None),
                key=lambda s: SEVERITY_RANK[s],
            )
            return STRIP_FROM_SEVERITY[worst][1]
        return Api._no_basis_answer(rows)

    # ----------------------------------------------------------------------
    # #89: the model mix -- an observation, and deliberately not advice
    # ----------------------------------------------------------------------

    @staticmethod
    def _model_mix(models: list[dict[str, Any]]) -> dict[str, Any]:
        """Which model this window's calls actually ran on.

        WHY THIS IS NOT A RECOMMENDATION, and why it is not in the table. It is
        a real measurement and plausibly the largest single lever on the page:
        measured 2026-08-05 on this project's own corpus, 3,489 subagent
        replies ran on Opus against 5 on Haiku. It is nonetheless NOT advice,
        by owner decision -- routing work to a weaker model to save tokens can
        cost more in rework than it saves, and CPB measures tokens and cannot
        see rework. There is therefore no severity to give it, no lever to pull
        and no direction to move, and `recommendations.assess_all()` would
        rightly refuse a metric with none of those. So it is stated, never
        prescribed, and it is read off `models` -- the breakdown the payload
        already carries -- rather than measured again.

        It names no tier and ranks no model against another. "The top tier" is
        a claim about Anthropic's line-up that this project has not checked and
        would have to date; the busiest model's own NAME is a measurement, and
        the reader knows what they asked for.

        `busiest` is None -- never a row of zeroes -- when the window holds no
        call. `model` inside it may itself be None, which is a call whose model
        the transcript never recorded: unmeasured, and rendered as such.
        """
        by_model: dict[Optional[str], int] = defaultdict(int)
        for row in models:
            by_model[row["model"]] += row["calls"]
        busiest = None
        if by_model:
            # Deterministic beyond the count, so two models tied on calls do
            # not swap places between requests over one unchanged database.
            model, calls = sorted(
                by_model.items(), key=lambda kv: (-kv[1], kv[0] or "")
            )[0]
            busiest = {"model": model, "calls": calls}
        return {
            "sample_is": MODEL_MIX_SAMPLE,
            "sample_calls": sum(by_model.values()),
            "models": len(by_model),
            "busiest": busiest,
        }

    @classmethod
    def _assessment_payload(cls, assessment: Assessment) -> dict[str, Any]:
        """One `Assessment`, flattened for JSON with its provenances intact.

        Both range edges cross with their OWN provenance and each provenance
        keeps its `kind`, so a cited boundary and a judged one are
        distinguishable in the payload rather than only in a comment (#78).
        That is the whole point: flattened to one table-level provenance line,
        the judged `0.25` would borrow the cited `1.0`'s authority, which is
        `band_provenance`'s failure mode one level down (#31).
        """
        return {
            "metric": assessment.metric,
            "measurement": assessment.measurement,
            "value": assessment.value,
            # WHAT KIND OF NUMBER THIS IS (#89 review). Carried on BOTH
            # renderings of a reading -- here and on the knob -- so the summary
            # and the diagnosis show one figure one way. Two levels formatting
            # `0.3034` as `30.3%` and as `0.3034` would be the same number in
            # two voices, which is the drift a levelled page makes easy even
            # when the value itself cannot move.
            "unit": METRICS[assessment.metric].unit,
            "severity": assessment.severity,
            "recommendation": assessment.recommendation,
            "lever": cls._lever_payload(assessment.lever),
            # The key the ranking orders by, published beside the order it
            # produced -- `RANKED_BY`'s rule, and the reason
            # `ranking_provenance` is a field rather than a comment.
            "depth_in_severity": assessment.depth_in_severity,
            "range_lower": assessment.range_lower,
            "range_upper": assessment.range_upper,
            "lower_provenance": cls._provenance_payload(
                assessment.lower_provenance
            ),
            "upper_provenance": cls._provenance_payload(
                assessment.upper_provenance
            ),
        }

    @staticmethod
    def _lever_payload(lever: Optional[Lever]) -> Optional[dict[str, Any]]:
        """The machine-readable half of a recommendation, or None.

        None for a healthy entry, which the module guarantees carries no lever:
        "nothing to change" and "change this" cannot both be true.

        `directive` is READ off the module, never composed here. `lever()`
        refuses to build a reduce-directive over a discounted token class, and
        a directive assembled in this file from `action` and `target` would
        route around that guard -- "Reduce cache-read tokens" is wrong at every
        scale, and the point of the registry is that it cannot be said.
        """
        if lever is None:
            return None
        return {
            "action": lever.action,
            "target": lever.target,
            "directive": lever.directive,
        }

    @staticmethod
    def _provenance_payload(
        provenance: Optional[Provenance],
    ) -> Optional[dict[str, Any]]:
        """One boundary's provenance, `kind` included, or None for an open end.

        `source`, `checked` and `covers` stay NULL where the kind has none --
        never "", never "n/a". A judged boundary structurally cannot carry a
        source (the module raises), and null is how the page can tell that it
        does not have one rather than that nobody filled it in.
        """
        if provenance is None:
            return None
        return {
            "kind": provenance.kind,
            "statement": provenance.statement,
            "checked": provenance.checked,
            "source": provenance.source,
            "covers": provenance.covers,
        }

    @staticmethod
    def _no_band_sample_reason(
        calls: int, sample_calls: int, banded_calls: int
    ) -> Optional[str]:
        """Why this tally has no banded sample, or None if it has one (#61).

        Non-null EXACTLY when `banded_calls` is 0, and the branches are ordered
        from the widest absence inward, so the reason names the first thing
        that was missing rather than the last: a scope that ran nothing is not
        told it lacks a documented window.
        """
        if banded_calls:
            return None
        if not calls:
            return UTIL_NO_SAMPLE_NO_CALLS
        if not sample_calls:
            return UTIL_NO_SAMPLE_NO_CONTEXT_MEASUREMENT
        return UTIL_NO_SAMPLE_NO_DOCUMENTED_WINDOW

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

        `ingest` and `context` are each computed ONCE and handed to `_health()`
        as well (#64), so the verdict at the top of the page and the figures it
        qualifies are one reading rather than two.

        `context` is computed ONCE and handed to `_recommendations()`, which
        reads the main-thread saturation share off the same per-scope tally the
        page renders. Recomputing it there would put two definitions of "over
        half the window" in one payload, free to disagree with each other and
        with the bands the reader is looking at.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) calls, COUNT(DISTINCT session_id) sessions,"
            " SUM(input_tokens) input, SUM(cache_read) cache_read,"
            " SUM(cache_write) cache_write, SUM(output_tokens) output,"
            " " + context_aggregate_sql() +
            " FROM api_calls WHERE ts >= ? AND ts < ?",
            (start, end),
        ).fetchone()
        context = self._context(start, end)
        ingest = self._ingest_health()
        health = self._health(ingest, context)
        models = self.models(start, end)
        recommendations = self._recommendations(start, end, context)
        return {
            **dict(row),
            # #92: which build computed everything below it. Read from
            # `cpb.VERSION` per request, never restated here -- and `null`
            # where it cannot be read, which the page states as UNKNOWN.
            "build": {"version": cpb_version()},
            "ingest": ingest,
            # #64: the VERDICT over the figures below, derived from the two
            # blocks either side of it and from the census -- never computed a
            # second time. It is built from `ingest` and `context` as ARGUMENTS
            # for the same reason `_recommendations()` takes `context`: a
            # verdict that ran its own queries would be a second opinion on the
            # very numbers it qualifies.
            "health": health,
            "context": context,
            "scope": self._scope(start, end),
            "durability": self._durability(start, end),
            "models": models,
            "recommendations": recommendations,
            # #89's summary level. Both blocks are READINGS of what is already
            # in this dict -- the strip of `health`, `context` and the table's
            # own knobs; the mix of `models` -- so the level that says what to
            # do and the levels that say why cannot report different numbers.
            # `models` and `recommendations` are computed into locals above for
            # exactly that reason: calling them twice would be two samples.
            "status": self._status(health, context, recommendations),
            "model_mix": self._model_mix(models),
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
