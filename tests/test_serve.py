"""Tests for the usage-report serving layer's date/limit helpers (#4948).

CodeRabbit finding: serve.py had no test coverage at all, and `day_bounds`
is the input to every filtered query -- a DST-boundary error there would
shift every chart silently. These tests pin the [start, end) half-open
contract, including across a DST transition, plus the limit-clamping and
reversed-range validation added in this cycle's review pass.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Sequence
from datetime import date
from http.server import HTTPServer
from pathlib import Path
from typing import Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cpb  # noqa: E402
from ingest import (  # noqa: E402
    CACHE_CREATION_KEY,
    CACHE_WRITE_1H_KEY,
    CACHE_WRITE_5M_KEY,
    INGEST_RUNS_TABLE,
    RUN_SCOPE_CORPUS,
    RUN_SCOPE_FILE,
    SOURCE_MAIN,
    SOURCE_SUBAGENT,
    ingest,
    ingest_transcript,
    record_ingest_run,
)
from test_ingest import build_corpus, build_hook_only_database  # noqa: E402
from context_window import (  # noqa: E402
    BAND_25_TO_50,
    BAND_50_TO_90,
    BAND_AT_LEAST_90,
    BAND_UNDER_25,
    BANDS,
    BANDS_AS_OF,
    CONTEXT_WINDOWS,
    WINDOW_SOURCE,
    WINDOWS_AS_OF,
    band_for,
    longest_prefix_match,
    window_for_model,
)
from recommendations import (  # noqa: E402
    ACTION_REDUCE,
    ACTION_VERBS,
    DISCOUNTED_TOKEN_CLASSES,
    LEVER_TARGETS,
    METRIC_CACHE_READS_PER_WRITE,
    METRIC_CACHE_WRITE_ONLY_SHARE,
    METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
    METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW,
    METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,
    METRIC_UNIT_KINDS,
    METRICS,
    PROVENANCE_CITED,
    PROVENANCE_JUDGED,
    PROVENANCE_KINDS,
    PROVENANCE_STRUCTURAL,
    RANKING_PROVENANCE,
    READ_TOKENS_TO_REPAY_A_1H_WRITE_TOKEN,
    READ_TOKENS_TO_REPAY_A_5M_WRITE_TOKEN,
    RECOMMENDATION_PROVENANCE,
    RECOMMENDATIONS_AS_OF,
    SAMPLE_FLOOR_AS_OF,
    SEVERITY_ACT,
    SEVERITY_OK,
    SEVERITY_RANK,
    SEVERITY_WATCH,
    UNDER_SAMPLED_NOTE,
    UNMEASURED_NOTE,
    WORSE_WHEN_HIGHER,
    WORSE_WHEN_LOWER,
    Lever,
    Reading,
    assess_all,
)
import serve  # noqa: E402
from serve import (  # noqa: E402
    CHECK_CONTEXT_MEASURED,
    CHECK_FORMAT_CENSUS,
    CHECK_INGEST_AGE,
    CHECK_MODEL_WINDOW_KNOWN,
    CACHE_METRICS,
    CHECK_RECORDS_PARSED,
    CHECK_WITHIN_WINDOW,
    CONTEXT_ANSWER_INCONCLUSIVE,
    CONTEXT_ANSWER_NO,
    CONTEXT_ANSWER_NO_SAMPLE,
    CONTEXT_ANSWER_STATEMENTS,
    CONTEXT_ANSWER_STATES,
    CONTEXT_DOT_METRIC,
    CONTEXT_ANSWER_UNKNOWN,
    CONTEXT_ANSWER_YES,
    CONTEXT_SAMPLE,
    FULL_SCAN_UNKNOWN_NONE_RECORDED,
    FULL_SCAN_UNKNOWN_NO_RUN_TABLE,
    FULL_SCAN_UNKNOWN_NO_SCOPE_COLUMN,
    GROWTH_MATERIAL_CHANGE,
    GROWTH_MIN_CALLS,
    GROWTH_MIN_CALLS_PER_QUARTER,
    GROWTH_QUARTERS,
    GROWTH_QUARTER_NO_CALLS,
    GROWTH_REFUSED_NO_SPAN,
    GROWTH_REFUSED_TOO_FEW,
    GROWTH_SCOPE,
    GROWTH_SHAPES,
    GROWTH_SHAPE_FALLING,
    GROWTH_SHAPE_FLAT,
    GROWTH_SHAPE_MIXED,
    GROWTH_SHAPE_PROVENANCE,
    GROWTH_SHAPE_RISING,
    GROWTH_SHAPE_ROSE_THEN_FELL,
    GROWTH_SHAPE_STATEMENTS,
    GROWTH_SHAPE_UNMEASURABLE,
    HEALTH_CHECKS,
    HEALTH_FAILED,
    HEALTH_OK,
    HEALTH_ORDER,
    HEALTH_STATEMENTS,
    HEALTH_UNCHECKED,
    MEASURED_CONTEXT_MIN,
    MODEL_MIX_SAMPLE,
    OVER_HALF_WINDOW_BANDS,
    PERCENTILES,
    RANKED_BY,
    RECOMMENDED_METRICS,
    SAMPLE_MEASURED,
    SAMPLE_UNDER_SAMPLED,
    SAMPLE_UNMEASURED,
    SATURATION_RANKED_BY,
    SCOPE_INCLUDES_BOTH,
    SCOPE_LABELS,
    SCOPE_MAIN,
    SCOPE_ORDER,
    SCOPE_SUBAGENT,
    STALE_AFTER_SECONDS,
    STALE_UNKNOWN_NO_RUN_RECORDED,
    STALE_UNKNOWN_NO_RUN_TABLE,
    STALE_UNKNOWN_RUN_IN_FUTURE,
    STATUS_ARCHIVED,
    STRIP_NOT_MEASURED,
    STRIP_BAD,
    STRIP_DOTS,
    STRIP_DOT_BROKEN,
    STRIP_DOT_CACHE,
    STRIP_DOT_CONTEXT,
    STRIP_DOT_KNOBS,
    STRIP_FROM_CONTEXT,
    STRIP_FROM_HEALTH,
    STRIP_FROM_SEVERITY,
    STRIP_GOOD,
    STRIP_ORDER,
    STRIP_QUESTIONS,
    STRIP_UNDER_SAMPLED,
    STRIP_WATCH,
    STRIP_UNKNOWN,
    UTIL_NO_SAMPLE_NO_CALLS,
    UTIL_NO_SAMPLE_NO_CONTEXT_MEASUREMENT,
    UTIL_NO_SAMPLE_NO_DOCUMENTED_WINDOW,
    Api,
    _refuse_unwired_metrics,
    clamp_limit,
    day_bounds,
    eastern_day,
    has_context_measurement,
    make_handler,
    measured_context_sql,
    nearest_rank,
    project_of,
    staleness_verdict,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "session-fixture.jsonl"
DISPATCH_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "dispatch-period-fixture.jsonl"
)
SUBAGENT_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "subagent-transcript.jsonl"
)


class DayBoundsTest(unittest.TestCase):
    def test_no_range_is_effectively_unbounded(self) -> None:
        start, end = day_bounds(None, None)
        self.assertEqual(start, 0.0)
        self.assertGreater(end, 32000000000.0)  # year ~3000, "no upper bound"

    def test_single_day_is_24_hours_on_a_normal_day(self) -> None:
        start, end = day_bounds("2026-07-15", "2026-07-15")
        self.assertEqual(end - start, 24 * 3600)

    def test_half_open_interval_end_is_exclusive(self) -> None:
        # from=to=X means the WHOLE of day X: [midnight X, midnight X+1).
        start, end = day_bounds("2026-07-15", "2026-07-15")
        self.assertEqual(eastern_day(start), "2026-07-15")
        self.assertEqual(eastern_day(end - 1), "2026-07-15")  # last second is IN
        self.assertEqual(eastern_day(end), "2026-07-16")  # end itself is OUT

    def test_multi_day_range_spans_from_start_of_from_to_end_of_to(self) -> None:
        start, end = day_bounds("2026-07-01", "2026-07-03")
        self.assertEqual(end - start, 3 * 24 * 3600)

    def test_dst_spring_forward_day_is_23_hours(self) -> None:
        # 2026-03-08 -> 2026-03-09: America/New_York goes EST(-5) -> EDT(-4).
        # A calendar "day" that loses an hour must reflect actual elapsed
        # wall-clock seconds, not a fabricated flat 24h.
        start, end = day_bounds("2026-03-08", "2026-03-08")
        self.assertEqual(end - start, 23 * 3600)

    def test_dst_fall_back_day_is_25_hours(self) -> None:
        # 2026-11-01 -> 2026-11-02: EDT(-4) -> EST(-5), a gained hour.
        start, end = day_bounds("2026-11-01", "2026-11-01")
        self.assertEqual(end - start, 25 * 3600)

    def test_reversed_range_raises_value_error(self) -> None:
        # CodeRabbit finding: "from" after "to" must be REJECTED (400), never
        # silently read as zero matching rows ("no usage this period").
        with self.assertRaises(ValueError):
            day_bounds("2026-07-30", "2026-07-01")

    def test_from_equals_to_does_not_raise(self) -> None:
        # The boundary itself (a single day) must NOT be treated as reversed.
        start, end = day_bounds("2026-07-15", "2026-07-15")
        self.assertLess(start, end)


class EasternDayTest(unittest.TestCase):
    def test_epoch_zero_is_1969_in_eastern(self) -> None:
        # 1970-01-01T00:00:00Z is still Dec 31 1969 in America/New_York.
        self.assertEqual(eastern_day(0.0), "1969-12-31")

    def test_roundtrips_through_day_bounds_start(self) -> None:
        start, _ = day_bounds("2026-07-15", None)
        self.assertEqual(eastern_day(start), "2026-07-15")


class ClampLimitTest(unittest.TestCase):
    def test_default_when_absent(self) -> None:
        self.assertEqual(clamp_limit(None), 20)

    def test_ordinary_value_passes_through(self) -> None:
        self.assertEqual(clamp_limit("50"), 50)

    def test_negative_limit_is_clamped_to_minimum(self) -> None:
        # SQLite treats a negative LIMIT as "no limit" -- must never reach it.
        self.assertEqual(clamp_limit("-1"), 1)

    def test_huge_limit_is_clamped_to_maximum(self) -> None:
        self.assertEqual(clamp_limit("999999999"), 500)

    def test_zero_is_clamped_to_minimum_not_treated_as_no_rows(self) -> None:
        self.assertEqual(clamp_limit("0"), 1)

    def test_non_numeric_limit_raises(self) -> None:
        with self.assertRaises(ValueError):
            clamp_limit("not-a-number")


# Field names the API must no longer emit anywhere (#30). The estimate was
# list-rate arithmetic over a hand-maintained table that went stale twice and
# diverged from real spend by >2.5x, so it is removed rather than qualified: a
# precise-looking figure that is wrong by a factor of two is worse than no
# figure, because the reader has no way to see the error.
REMOVED_COST_FIELDS = frozenset({
    "cost_estimate_usd",
    "cost_usd",
    "cost_basis",
    "rates_as_of",
    "unpriced_calls",
    "unpriced_models",
})
# ...and the shape they would come back in. Matched on whole name COMPONENTS,
# so `stale_after_seconds` is not swept up while `cost_per_call` or
# `estimated_usd` would be. Named removals rot: a reintroduction under a new
# name would pass a list of six literals.
MONEY_NAME_RE = re.compile(
    r"(?:^|_)(?:cost|costs|usd|price|prices|pricing"
    r"|rate|rates|dollar|dollars)(?:_|$)"
)


def json_keys(payload) -> set[str]:
    """Every key name anywhere in a JSON-shaped payload, at any depth."""
    if isinstance(payload, dict):
        found = set(payload)
        for value in payload.values():
            found |= json_keys(value)
        return found
    if isinstance(payload, list):
        found: set[str] = set()
        for item in payload:
            found |= json_keys(item)
        return found
    return set()


class NoMoneyCrossesTheApiTest(unittest.TestCase):
    """No route emits a dollar figure, or the fields that framed one (#30).

    Asserted PER ROUTE rather than on `/api/summary` alone: the estimate was
    summed in seven different queries across five endpoints, and a check on the
    headline payload would have called the job done while
    `/api/session`'s three tables still carried it.

    The corpus is the full one (main thread + subagent transcripts + a reaped
    dispatch + task index), so every route returns rows -- a route that
    returned nothing would satisfy this vacuously.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-nomoney-test-"))
        projects_dir, tasks_dir = build_corpus(cls.tmp)
        cls.db_path = cls.tmp / "usage.db"
        ingest(projects_dir, cls.db_path, tasks_dir=tasks_dir)
        cls.api = Api(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def payloads(self) -> dict[str, object]:
        start, end = day_bounds(None, None)
        return {
            "/api/summary": self.api.summary(start, end),
            "/api/timeseries?by=class": self.api.timeseries(start, end, "class"),
            "/api/timeseries?by=turntype": self.api.timeseries(start, end, "turntype"),
            "/api/timeseries?by=scope": self.api.timeseries(start, end, "scope"),
            "/api/sessions": self.api.sessions(start, end),
            "/api/session": self.api.session_detail("session-fixture"),
            "/api/outliers": self.api.outliers(start, end, 20),
            "/api/agents": self.api.agents(start, end, 20),
        }

    def test_the_corpus_gives_every_route_something_to_return(self) -> None:
        # Without this the assertions below can pass on empty responses.
        for route, payload in self.payloads().items():
            with self.subTest(route=route):
                self.assertTrue(payload, f"{route} returned nothing to inspect")

    def test_no_route_returns_a_removed_cost_field(self) -> None:
        for route, payload in self.payloads().items():
            with self.subTest(route=route):
                leaked = sorted(json_keys(payload) & REMOVED_COST_FIELDS)
                self.assertEqual(leaked, [], f"{route} still emits {leaked}")

    def test_no_route_returns_any_money_shaped_field(self) -> None:
        for route, payload in self.payloads().items():
            with self.subTest(route=route):
                leaked = sorted(
                    k for k in json_keys(payload) if MONEY_NAME_RE.search(k)
                )
                self.assertEqual(
                    leaked,
                    [],
                    f"{route} emits money-shaped field(s) {leaked}; #30 removed "
                    "the estimate rather than renaming it",
                )

    def test_the_serving_layer_imports_no_pricing_module(self) -> None:
        self.assertNotIn("pricing", sys.modules)
        source = (Path(__file__).resolve().parent.parent / "serve.py").read_text()
        self.assertNotIn("pricing", source)


class NoDollarFigureIsRenderedTest(unittest.TestCase):
    """The removal reaches the PAGE, not just the payload (#30).

    An API that stopped emitting the estimate while `index.html` still had a
    "Cost estimate" column would render "—" in a money column forever -- the
    tool's own failure mode (a shape that looks like a measurement) applied to
    the very number this issue removed. So the rate table, the totals card, the
    columns, the basis banner and the formatter all go together.
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (cls.ROOT / "index.html").read_text()

    def test_the_rate_table_module_is_gone_from_the_repository(self) -> None:
        self.assertFalse(
            (self.ROOT / "pricing.py").exists(),
            "pricing.py is deleted, not merely unimported",
        )

    def test_no_shipped_module_mentions_pricing(self) -> None:
        # `context_window.py` is in the list because it is the one module that
        # would plausibly want to name the deleted table -- it is a
        # hand-maintained table of documented facts and it has to justify
        # existing where that one was removed. It makes the comparison in
        # prose, without reintroducing the name.
        for name in ("ingest.py", "serve.py", "context_window.py", "index.html"):
            with self.subTest(module=name):
                self.assertNotIn("pricing", (self.ROOT / name).read_text())

    def test_the_page_renders_no_money(self) -> None:
        for gone in (
            "fmtCost",           # the dollar formatter
            "cost_estimate_usd",  # the payload field
            "cost_basis",         # the rate-table banner's source
            "Cost estimate",      # the card and the column headings
            "unpriced",           # the "excluded from cost" notice
            "not a bill",         # the estimate's disclaimer, no longer needed
            "$0.01",              # the sub-cent floor in the old formatter
        ):
            with self.subTest(string=gone):
                self.assertNotIn(gone, self.html)


class SessionsDispatchPeriodFilterTest(unittest.TestCase):
    """#4955: `/api/sessions` dispatch counts must respect the `from`/`to`
    period filter like every other figure on the report -- previously they
    were session-LIFETIME totals, so a session could show dispatches that
    fell outside the selected window.

    The fixture pins one dispatch INSIDE a narrow window (2026-01-03) and one
    far OUTSIDE it (2026-06-01) at deliberately distant timestamps -- a
    filter that silently ignores the bound (the #4955 defect) would return 2
    for the narrow window too, so this cannot pass by accident.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-dispatch-period-test-"))
        projects_dir = self.tmp / "projects"
        projects_dir.mkdir()
        shutil.copy(DISPATCH_FIXTURE, projects_dir / "dispatch-period-fixture.jsonl")
        self.db_path = self.tmp / "usage.db"
        ingest(projects_dir, self.db_path)
        self.api = Api(self.db_path)

    def tearDown(self) -> None:
        self.api.conn.close()
        shutil.rmtree(self.tmp)

    def test_narrow_window_excludes_the_dispatch_outside_it(self) -> None:
        # Covers the api_call (2026-01-01) and the EARLY dispatch
        # (2026-01-03), but ends well before the LATE dispatch (2026-06-01).
        start, end = day_bounds("2026-01-01", "2026-01-05")
        rows = self.api.sessions(start, end)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dispatches"], 1)

    def test_wide_window_includes_the_dispatch_the_narrow_one_excluded(self) -> None:
        start, end = day_bounds(None, None)
        rows = self.api.sessions(start, end)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dispatches"], 2)

    def test_session_with_zero_dispatches_in_window_is_a_real_zero(self) -> None:
        # rule #12: a window with no matching dispatch rows must show a
        # genuine 0 (the session had none in-period), not be silently
        # dropped or coerced into looking like unmeasured data. Pick a
        # window that still includes the api_call (so the session row
        # exists) but excludes BOTH dispatches.
        start, end = day_bounds("2026-01-01", "2026-01-01")
        rows = self.api.sessions(start, end)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dispatches"], 0)


class ScopeLabellingTest(unittest.TestCase):
    """#4966: every aggregate must NAME the scope it ranges over.

    Before this, `summary()` returned main-thread-only figures under the bare
    names `calls` / `cost_estimate_usd` / `avg_context`, presenting them as
    session totals. The fixture pins a main transcript AND a subagent
    transcript with deliberately different magnitudes, so a response that
    quietly dropped either side cannot pass.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-scope-test-"))
        self.projects_dir = self.tmp / "projects"
        self.projects_dir.mkdir()
        shutil.copy(FIXTURE, self.projects_dir / "session-fixture.jsonl")
        subagents = self.projects_dir / "session-fixture" / "subagents"
        subagents.mkdir(parents=True)
        shutil.copy(SUBAGENT_FIXTURE, subagents / "agent-atest1.jsonl")
        self.db_path = self.tmp / "usage.db"
        ingest(self.projects_dir, self.db_path)
        self.api = Api(self.db_path)
        self.start, self.end = day_bounds(None, None)

    def tearDown(self) -> None:
        self.api.conn.close()
        shutil.rmtree(self.tmp)

    def test_summary_totals_span_both_scopes(self) -> None:
        s = self.api.summary(self.start, self.end)
        self.assertEqual(s["calls"], 7)  # 5 main + 2 subagent
        self.assertEqual(s["cache_read"], 456 + 4090)

    def test_summary_breaks_out_main_thread_and_subagent(self) -> None:
        s = self.api.summary(self.start, self.end)
        self.assertEqual(s["scope"]["main_thread"]["calls"], 5)
        self.assertEqual(s["scope"]["main_thread"]["cache_read"], 456)
        self.assertEqual(s["scope"]["subagent"]["calls"], 2)
        self.assertEqual(s["scope"]["subagent"]["cache_read"], 4090)
        # The label a consumer reads to know what the bare totals mean.
        self.assertEqual(s["scope"]["includes"], "main-thread + subagent")

    def test_summary_reports_subagent_transcript_coverage(self) -> None:
        # Rule #12: "0 subagent calls" and "no subagent transcripts on disk"
        # are different facts and must not render identically.
        s = self.api.summary(self.start, self.end)
        cov = s["scope"]["coverage"]
        self.assertEqual(cov["sessions"], 1)
        self.assertEqual(cov["sessions_with_subagent_transcripts"], 1)
        self.assertEqual(cov["subagent_files"], 1)

    def test_coverage_zero_when_no_subagent_transcripts_exist(self) -> None:
        bare = self.tmp / "bare"
        bare.mkdir()
        shutil.copy(FIXTURE, bare / "session-fixture.jsonl")
        db2 = self.tmp / "bare.db"
        ingest(bare, db2)
        api2 = Api(db2)
        try:
            s = api2.summary(*day_bounds(None, None))
            cov = s["scope"]["coverage"]
            self.assertEqual(cov["sessions"], 1)
            self.assertEqual(cov["sessions_with_subagent_transcripts"], 0)
            self.assertEqual(s["scope"]["subagent"]["calls"], 0)
            # ...and it says so, rather than implying a measured zero.
            self.assertEqual(s["scope"]["includes"], "main-thread only")
        finally:
            api2.conn.close()

    def test_timeseries_breaks_out_calls_by_scope_per_day(self) -> None:
        ts = self.api.timeseries(self.start, self.end, "class")
        self.assertEqual(ts["days"], ["2026-07-28"])
        self.assertEqual(ts["calls"], [7])
        self.assertEqual(ts["main_thread_calls"], [5])
        self.assertEqual(ts["subagent_calls"], [2])

    def test_timeseries_by_scope_series_splits_tokens(self) -> None:
        ts = self.api.timeseries(self.start, self.end, "scope")
        self.assertEqual(
            ts["series"]["main-thread"], [118 + 233 + 456 + 76]
        )
        self.assertEqual(
            ts["series"]["subagent"], [1030 + 2060 + 4090 + 620]
        )

    def test_sessions_row_breaks_out_subagent_calls(self) -> None:
        rows = self.api.sessions(self.start, self.end)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["calls"], 7)
        self.assertEqual(rows[0]["subagent_calls"], 2)
        self.assertEqual(rows[0]["main_thread_calls"], 5)

    def test_session_detail_breaks_out_scope(self) -> None:
        d = self.api.session_detail("session-fixture")
        by_scope = {r["scope"]: r for r in d["scopes"]}
        self.assertEqual(by_scope["main-thread"]["calls"], 5)
        self.assertEqual(by_scope["subagent"]["calls"], 2)
        self.assertEqual(by_scope["subagent"]["cache_read"], 4090)


class WindowScopedCoverageTest(unittest.TestCase):
    """Coverage and the buckets must range over the SAME set (rule #15).

    Corpus-wide coverage beside window-scoped buckets produced the exact
    sentence the scope block exists to prevent: `subagent.calls == 0` next to
    `includes == "main-thread + subagent"`, which asserts the subagents
    genuinely spent nothing in a window whose subagent transcripts were never
    established to exist.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-window-test-"))
        self.projects_dir, self.tasks_dir = build_corpus(self.tmp)
        self.db_path = self.tmp / "usage.db"
        ingest(self.projects_dir, self.db_path, tasks_dir=self.tasks_dir)
        self.api = Api(self.db_path)

    def tearDown(self) -> None:
        self.api.conn.close()
        shutil.rmtree(self.tmp)

    def test_whole_corpus_window_reports_both_scopes(self) -> None:
        scope = self.api.summary(*day_bounds(None, None))["scope"]
        self.assertEqual(scope["includes"], "main-thread + subagent")
        self.assertGreater(scope["coverage"]["sessions_with_subagent_transcripts"], 0)

    def test_window_with_no_sessions_does_not_claim_subagent_coverage(self) -> None:
        # A day the corpus has no calls on. The corpus DOES hold subagent
        # transcripts -- on another day -- so this is exactly the case that
        # used to report "main-thread + subagent" over an empty window.
        start, end = day_bounds("2020-01-01", "2020-01-01")
        scope = self.api.summary(start, end)["scope"]
        self.assertEqual(scope["subagent"]["calls"], 0)
        self.assertEqual(scope["coverage"]["sessions"], 0)
        self.assertEqual(scope["coverage"]["sessions_with_subagent_transcripts"], 0)
        self.assertEqual(scope["includes"], "main-thread only")

    def test_subagent_files_is_an_integer_not_null_on_an_empty_window(self) -> None:
        # SUM() over no rows is NULL; the UI calls toLocaleString() on this.
        start, end = day_bounds("2020-01-01", "2020-01-01")
        cov = self.api.summary(start, end)["scope"]["coverage"]
        self.assertEqual(cov["subagent_files"], 0)
        self.assertIsInstance(cov["subagent_files"], int)


# --- #44: the scope must name the PROJECT set it ranges over --------------
#
# The plugin (#33) resolves ONE database per INSTALL, not per project, so a
# user-scope install ingests every project the user opens into the same file.
# Every headline figure is then a sum across projects, rendered in the
# vocabulary of a single one.
#
# Fixture design (CLAUDE.md, "a fixture must not make the defect
# undetectable"):
#   * every token class of every call carries a DIFFERENT value and the two
#     projects' totals are deliberately unequal (12,130 against 100,010), so a
#     grouping that collapsed the two -- or swapped them -- cannot pass;
#   * one project owns a SUBAGENT transcript, which sits two directories deeper
#     than a main one, so a derivation that simply read the containing
#     directory would report the SESSION directory as a third project;
#   * the two projects sit on DIFFERENT DAYS, so a window can hold one and not
#     the other -- the measured-zero case;
#   * a THIRD project was ingested and produced no API call at all, so the
#     ledger (`ingest_state`) knows a project the measurements (`api_calls`)
#     never will. That is a measured zero in every window, and a project set
#     read off the calls alone would drop it in all of them.
#
# The names are synthetic and must stay so. A real project directory is the
# absolute working directory with each separator folded to `-`, so it carries
# the user's username and every repo name; none may enter this repository.
PROJECT_A = "-fixture-alpha"
PROJECT_B = "-fixture-beta"
PROJECT_C = "-fixture-gamma"
SESSION_A = "fixture-alpha-session"
SESSION_B = "fixture-beta-session"
SESSION_C = "fixture-gamma-session"
DAY_A = "2026-03-01"
DAY_B = "2026-03-02"
# (input, cache_write, cache_read, output) per call.
A_MAIN_CALLS = [(101, 202, 303, 404), (111, 222, 333, 444)]
A_SUBAGENT_CALLS = [(1001, 2002, 3003, 4004)]
B_MAIN_CALLS = [(10001, 20002, 30003, 40004)]
A_TOKENS = sum(sum(c) for c in A_MAIN_CALLS + A_SUBAGENT_CALLS)  # 12,130
B_TOKENS = sum(sum(c) for c in B_MAIN_CALLS)  # 100,010


def build_project(
    root: Path,
    name: str,
    session: str,
    day: str,
    main_calls: Sequence[tuple[int, int, int, int]],
    subagent_calls: Sequence[tuple[int, int, int, int]] = (),
) -> list[Path]:
    """One synthetic project directory. Returns its transcripts, in order."""

    def rec(obj: dict) -> str:
        return json.dumps(obj) + "\n"

    def call(
        session_id: str,
        n: int,
        usage: tuple[int, int, int, int],
        sidechain: bool,
        agent: str | None = None,
    ) -> str:
        inp, cw, cr, out = usage
        record = {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": f"{day}T15:0{n}:00.000Z",
            "isSidechain": sidechain,
            "message": {
                "id": f"msg-{agent or session_id}-{n}",
                "model": "claude-sonnet-5-20260115",
                "usage": {
                    "input_tokens": inp,
                    "cache_creation_input_tokens": cw,
                    "cache_read_input_tokens": cr,
                    "output_tokens": out,
                },
                "content": [{"type": "text", "text": f"{agent or session_id} {n}"}],
            },
        }
        if agent is not None:
            record["agentId"] = agent
        return rec(record)

    project = root / name
    project.mkdir(parents=True)
    transcript = project / f"{session}.jsonl"
    transcript.write_text(
        "".join(call(session, n, u, False) for n, u in enumerate(main_calls))
    )
    paths = [transcript]
    if subagent_calls:
        subagents = project / session / "subagents"
        subagents.mkdir(parents=True)
        agent = f"agent-{session[:5]}01"
        agent_file = subagents / f"{agent}.jsonl"
        agent_file.write_text(
            "".join(
                call(session, n, u, True, agent=agent)
                for n, u in enumerate(subagent_calls)
            )
        )
        paths.append(agent_file)
    return paths


def build_callless_project(root: Path, name: str, session: str, day: str) -> Path:
    """A project whose transcript holds a prompt and no assistant reply.

    Ingested exactly like any other -- it gets an `ingest_state` row -- and it
    contributes no `api_calls` row to any window. Reachable in a second: open a
    directory, type one message, close the session.
    """
    project = root / name
    project.mkdir(parents=True)
    transcript = project / f"{session}.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": session,
                "timestamp": f"{day}T15:00:00.000Z",
                "message": {"role": "user", "content": "a prompt and no reply"},
            }
        )
        + "\n"
    )
    return transcript


class ProjectOfPathTest(unittest.TestCase):
    """`project_of()` reads the layout, and REFUSES rather than guessing (#44).

    The three shapes are `ingest.discover_sources()`'s own: the two transcript
    globs plus the harness task directory it reads as an index. Anything else
    must return None -- bucketing an unreadable path into the directory that
    happens to sit above it would attribute one project's spend to another,
    which is the very defect this issue is about, one layer down.
    """

    def test_a_main_transcript_names_its_containing_directory(self) -> None:
        self.assertEqual(
            project_of(f"/x/projects/{PROJECT_A}/{SESSION_A}.jsonl", SOURCE_MAIN),
            PROJECT_A,
        )

    def test_a_subagent_transcript_names_the_project_not_the_session(self) -> None:
        self.assertEqual(
            project_of(
                f"/x/projects/{PROJECT_A}/{SESSION_A}/subagents/agent-a1.jsonl",
                SOURCE_SUBAGENT,
            ),
            PROJECT_A,
        )

    def test_a_task_index_entry_names_the_project(self) -> None:
        # The harness mirrors the project SLUG under the OS temp dir, so the
        # same name identifies the same project from a different root.
        self.assertEqual(
            project_of(
                f"/private/tmp/claude-0/{PROJECT_A}/{SESSION_A}/tasks/agent-a1.output",
                SOURCE_SUBAGENT,
            ),
            PROJECT_A,
        )

    def test_a_relative_source_path_still_resolves(self) -> None:
        # `--projects-dir ./x` is legal and stores relative paths.
        self.assertEqual(
            project_of(f"{PROJECT_A}/{SESSION_A}.jsonl", SOURCE_MAIN), PROJECT_A
        )

    def test_a_transcript_at_the_filesystem_root_has_no_project(self) -> None:
        self.assertIsNone(project_of("/orphan.jsonl", SOURCE_MAIN))

    def test_a_bare_filename_has_no_project(self) -> None:
        self.assertIsNone(project_of("orphan.jsonl", SOURCE_MAIN))

    def test_a_subagent_path_with_no_session_directory_has_no_project(self) -> None:
        self.assertIsNone(project_of("/subagents/agent-a1.jsonl", SOURCE_SUBAGENT))

    def test_an_unexpected_intermediate_directory_is_not_guessed(self) -> None:
        self.assertIsNone(
            project_of(
                f"/x/{PROJECT_A}/{SESSION_A}/notes/agent-a1.jsonl", SOURCE_SUBAGENT
            )
        )

    def test_an_unknown_source_kind_is_not_guessed(self) -> None:
        self.assertIsNone(
            project_of(f"/x/{PROJECT_A}/{SESSION_A}.jsonl", "some-future-kind")
        )


class ProjectScopeTest(unittest.TestCase):
    """A cross-project total must not be indistinguishable from one project's.

    Ingested ONE FILE AT A TIME, which is what the plugin's `Stop` hook does:
    directory mode would archive the other project's sources as missing on
    every alternate run, so single-file mode is both the production path for
    this defect and the only one that produces the state honestly.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-project-scope-test-"))
        cls.db_path = cls.tmp / "shared.db"
        no_tasks = cls.tmp / "no-task-index"
        for path in (
            *build_project(
                cls.tmp, PROJECT_A, SESSION_A, DAY_A, A_MAIN_CALLS, A_SUBAGENT_CALLS
            ),
            *build_project(cls.tmp, PROJECT_B, SESSION_B, DAY_B, B_MAIN_CALLS),
            build_callless_project(cls.tmp, PROJECT_C, SESSION_C, DAY_B),
        ):
            ingest_transcript(path, cls.db_path, tasks_dir=no_tasks)
        cls.api = Api(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def projects(self, frm: str | None = None, to: str | None = None) -> dict:
        return self.api.summary(*day_bounds(frm, to))["scope"]["projects"]

    def test_fixture_ingested_the_corpus_these_tests_assume(self) -> None:
        # If the corpus did not land, every assertion below is vacuous. Four
        # calls: two main-thread and one subagent in PROJECT_A, one in
        # PROJECT_B, and none at all in PROJECT_C.
        s = self.api.summary(*day_bounds(None, None))
        self.assertEqual(s["calls"], 4)
        self.assertEqual(
            s["input"] + s["cache_read"] + s["cache_write"] + s["output"],
            A_TOKENS + B_TOKENS,
        )
        self.assertNotEqual(A_TOKENS, B_TOKENS)

    def test_the_scope_counts_the_projects_the_window_ranges_over(self) -> None:
        # PROJECT_C is ingested and made no call, so it is NOT in this set --
        # and the two counts differ even over an unbounded window.
        p = self.projects()
        self.assertEqual(p["in_window"], 2)
        self.assertEqual(p["names_in_window"], [PROJECT_A, PROJECT_B])

    def test_the_scope_counts_the_projects_the_database_holds(self) -> None:
        # Read off the FILE LEDGER, not off the calls: a project that produced
        # no call is still a project this database mixes into its totals, and
        # calls-only would drop it from every window there is.
        p = self.projects()
        self.assertEqual(p["in_database"], 3)
        self.assertEqual(p["names_in_database"], [PROJECT_A, PROJECT_B, PROJECT_C])

    def test_a_subagent_transcript_counts_toward_its_project(self) -> None:
        # `<project>/<session>/subagents/agent-<id>.jsonl` is two directories
        # deeper than a main transcript: reading the containing directory would
        # name the SESSION and report a third project that does not exist.
        p = self.projects()
        self.assertNotIn(SESSION_A, p["names_in_database"])
        self.assertNotIn("subagents", p["names_in_database"])

    def test_a_project_with_no_calls_in_the_window_is_a_measured_zero(self) -> None:
        # Project B has calls on DAY_B only. In DAY_A's window it contributes
        # nothing -- but it has NOT gone away, and a scope line that named only
        # the window would under-report what this database mixes together.
        p = self.projects(DAY_A, DAY_A)
        self.assertEqual(p["in_window"], 1)
        self.assertEqual(p["names_in_window"], [PROJECT_A])
        self.assertEqual(p["in_database"], 3)
        self.assertIn(PROJECT_B, p["names_in_database"])

    def test_an_empty_window_still_names_the_database_set(self) -> None:
        p = self.projects("2020-01-01", "2020-01-01")
        self.assertEqual(p["in_window"], 0)
        self.assertEqual(p["names_in_window"], [])
        self.assertEqual(p["in_database"], 3)

    def test_no_source_path_in_this_corpus_is_unattributable(self) -> None:
        p = self.projects()
        self.assertEqual(p["unattributed_calls"], 0)
        self.assertEqual(p["unattributed_sources"], 0)


class SingleProjectScopeTest(unittest.TestCase):
    """The ordinary case must not start announcing a dimension it does not need.

    Almost every user has one project in their database. If naming the project
    set changes what they see, this fix has traded one wrong number for a
    permanent false alarm.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-one-project-test-"))
        cls.db_path = cls.tmp / "one.db"
        for path in build_project(
            cls.tmp, PROJECT_A, SESSION_A, DAY_A, A_MAIN_CALLS, A_SUBAGENT_CALLS
        ):
            ingest_transcript(path, cls.db_path, tasks_dir=cls.tmp / "no-task-index")
        cls.api = Api(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_one_project_is_reported_as_one_project(self) -> None:
        p = self.api.summary(*day_bounds(None, None))["scope"]["projects"]
        self.assertEqual(p["in_window"], 1)
        self.assertEqual(p["in_database"], 1)
        self.assertEqual(p["unattributed_calls"], 0)
        self.assertEqual(p["unattributed_sources"], 0)

    def test_the_scope_block_gains_exactly_one_key(self) -> None:
        # The main-vs-subagent axis is a DIFFERENT axis and must be untouched:
        # this change annotates the scope block, it does not restructure it.
        scope = self.api.summary(*day_bounds(None, None))["scope"]
        self.assertEqual(
            set(scope),
            {"includes", "main_thread", "subagent", "coverage", "projects"},
        )
        self.assertEqual(scope["includes"], "main-thread + subagent")


class UnattributableProjectPathTest(unittest.TestCase):
    """A path the layout cannot parse is COUNTED, never bucketed or dropped.

    Reachable in production: `ingest.py --transcript` (the hook's mode) accepts
    any `.jsonl` file, so a transcript at the filesystem ROOT stores a
    `source_path` with no project directory above it. That shape cannot be
    created under a temp directory on this host, so the rows are written
    directly -- the serve layer's contract is over what the database HOLDS, and
    the alternative would be to leave the case untested.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-unattributed-test-"))
        self.db_path = self.tmp / "one.db"
        for path in build_project(
            self.tmp, PROJECT_A, SESSION_A, DAY_A, A_MAIN_CALLS
        ):
            ingest_transcript(path, self.db_path, tasks_dir=self.tmp / "no-task-index")
        conn = sqlite3.connect(self.db_path)
        with conn:
            # TWO calls on ONE unplaceable file, deliberately unequal: a count
            # of sources reported as a count of calls would otherwise pass.
            conn.executemany(
                "INSERT INTO api_calls (session_id, source_path, source_kind, ts,"
                " model, input_tokens, cache_read, cache_write, output_tokens,"
                " context_size, is_sidechain, message_id)"
                " VALUES (?, '/orphan.jsonl', 'main', ?, 'claude-sonnet-5-20260115',"
                "  7, 7, 7, 7, 21, 0, ?)",
                [
                    (SESSION_A, day_bounds(DAY_A, DAY_A)[0] + 3600, "msg-orphan-1"),
                    (SESSION_A, day_bounds(DAY_A, DAY_A)[0] + 7200, "msg-orphan-2"),
                ],
            )
            conn.execute(
                "INSERT INTO ingest_state (path, session_id, source_kind, size,"
                " mtime, unparsed_records)"
                " VALUES ('/orphan.jsonl', ?, 'main', 1, 1, 0)",
                (SESSION_A,),
            )
        conn.close()
        self.api = Api(self.db_path)

    def tearDown(self) -> None:
        self.api.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_unattributable_call_is_counted_on_its_own(self) -> None:
        # CALLS and SOURCES are separate counts of separate things: two calls
        # in one file.
        p = self.api.summary(*day_bounds(None, None))["scope"]["projects"]
        self.assertEqual(p["unattributed_calls"], 2)
        self.assertEqual(p["unattributed_sources"], 1)

    def test_it_is_not_bucketed_into_the_project_beside_it(self) -> None:
        # The failure mode: `/orphan.jsonl` silently joining PROJECT_A, or
        # inventing a project named after the filesystem root.
        p = self.api.summary(*day_bounds(None, None))["scope"]["projects"]
        self.assertEqual(p["names_in_window"], [PROJECT_A])
        self.assertEqual(p["names_in_database"], [PROJECT_A])
        self.assertEqual(p["in_window"], 1)
        self.assertEqual(p["in_database"], 1)

    def test_the_call_is_still_in_the_totals(self) -> None:
        # Its project is unknown; its tokens are measured. Dropping it from the
        # aggregate would be absence rendered as a smaller number.
        s = self.api.summary(*day_bounds(None, None))
        self.assertEqual(s["calls"], len(A_MAIN_CALLS) + 2)


class ProjectSetSurvivesALedgerGapTest(unittest.TestCase):
    """The project set is read from BOTH tables, so a gap cannot shrink it.

    `ingest_state` is the file ledger and `api_calls` the measurements, and
    `ArchivedSourceDurabilityTest` pins that they agree today. Should they ever
    diverge -- a hand-pruned row, a partial restore, a future prune that
    forgets one table -- the set must OVER-report rather than quietly shrink:
    under-reporting the project set is the entirety of this defect.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-ledger-gap-test-"))
        self.db_path = self.tmp / "one.db"
        for path in build_project(self.tmp, PROJECT_A, SESSION_A, DAY_A, A_MAIN_CALLS):
            ingest_transcript(path, self.db_path, tasks_dir=self.tmp / "no-task-index")
        conn = sqlite3.connect(self.db_path)
        with conn:
            # A measurement whose ledger row is gone. The file need not exist:
            # `project_of()` is pure string work over the STORED path, which is
            # also what lets a transcript reaped months ago still name its
            # project.
            conn.execute(
                "INSERT INTO api_calls (session_id, source_path, source_kind, ts,"
                " model, input_tokens, cache_read, cache_write, output_tokens,"
                " context_size, is_sidechain, message_id)"
                " VALUES (?, ?, 'main', ?, 'claude-sonnet-5-20260115',"
                "  3, 5, 7, 11, 15, 0, 'msg-unledgered')",
                (
                    SESSION_B,
                    f"{self.tmp}/{PROJECT_B}/{SESSION_B}.jsonl",
                    day_bounds(DAY_A, DAY_A)[0] + 3600,
                ),
            )
        conn.close()
        self.api = Api(self.db_path)

    def tearDown(self) -> None:
        self.api.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_project_known_only_from_its_calls_is_still_in_the_set(self) -> None:
        p = self.api.summary(*day_bounds(None, None))["scope"]["projects"]
        self.assertEqual(p["names_in_database"], [PROJECT_A, PROJECT_B])
        self.assertEqual(p["in_database"], 2)
        self.assertEqual(p["unattributed_sources"], 0)


class ProjectScopeNoteTest(unittest.TestCase):
    """The page must SAY when its figures span projects (#44).

    Structural, for the reason `BannerPrecedenceTest` gives: the project ships
    no JS runtime, so the composition cannot be executed here. These pin the
    branch that exists to keep the single-project case silent, and the decision
    that the band states COUNTS rather than project names.

    Since #8 the band is a template, so `body` is the band's own markup plus
    the two getters it binds text from. Those two sentences stay in JS on
    purpose and the code says why: each continues the preceding `<strong>` with
    a comma and NO space, which HTML's whitespace collapsing would otherwise
    render as "projects , of which".
    """

    @classmethod
    def setUpClass(cls) -> None:
        html = (Path(__file__).resolve().parent.parent / "index.html").read_text()
        stripped = strip_comments(html)
        cls.body = "\n".join(
            [
                html_element(html, 'id="scope-note"'),
                js_function_body(stripped, "get crossProjectTail("),
                js_function_body(stripped, "get narrowProjectTail("),
            ]
        )

    def test_the_scope_note_reads_the_project_block(self) -> None:
        self.assertIn("scope.projects", self.body)

    def test_it_speaks_only_when_more_than_one_project_is_involved(self) -> None:
        # The BRANCH, not a mention: without the `> 1` guard every existing
        # single-project user gets a new sentence about a dimension their
        # database does not have.
        self.assertRegex(
            self.body, r'x-if="summary\.scope\.projects\.in_database > 1"'
        )

    def test_it_names_both_the_window_set_and_the_database_set(self) -> None:
        # A project with no calls in this window is a measured zero, and the
        # reader must be able to tell it apart from one that is not there.
        self.assertIn("in_window", self.body)
        self.assertIn("in_database", self.body)

    def test_it_surfaces_calls_whose_project_could_not_be_derived(self) -> None:
        # Again the BRANCH: the field name also appears inside the sentence, so
        # matching the bare substring let a disabled clause pass.
        self.assertRegex(
            self.body, r'x-if="summary\.scope\.projects\.unattributed_calls"'
        )

    def test_the_project_note_reaches_the_band_on_both_paths(self) -> None:
        # Composed and never appended is the "fully tested, never wired"
        # pattern SummaryPayloadIsWiredTest exists for. The band has TWO scope
        # branches -- main-thread-only and both-scopes -- and the project note
        # belongs on both: it does not depend on that axis.
        #
        # This used to be asserted as "the tail is concatenated onto both
        # exits" (`count("projects + undated") == 2`). The template cannot HAVE
        # two exits: there is ONE band, the project and undated blocks are
        # siblings of both scope branches, and appending to one alone is not
        # expressible. Asserted as exactly that: one occurrence of each block,
        # placed after both scope branches rather than inside either.
        scope_branches = [
            m.start()
            for m in re.finditer(
                r'x-if="!?summary\.scope\.coverage\.sessions_with_subagent_transcripts"',
                self.body,
            )
        ]
        self.assertEqual(len(scope_branches), 2, "the two scope branches are gone")
        for block in (
            r'x-if="summary\.scope\.projects\.in_database > 1"',
            r'x-if="summary\.scope\.projects\.unattributed_calls"',
            r'x-if="summary\.scope\.coverage\.runs_undated_unavailable"',
        ):
            with self.subTest(block=block):
                found = [m.start() for m in re.finditer(block, self.body)]
                self.assertEqual(
                    len(found), 1, "composed once, for both scope branches"
                )
                self.assertGreater(
                    found[0],
                    max(scope_branches),
                    "the note is nested inside one scope branch, so the other "
                    "branch renders without it",
                )

    def test_it_reports_counts_not_project_names(self) -> None:
        # DECISION, pinned so it cannot drift by accident. A project name is a
        # filesystem path with the username in it, and this band is one line:
        # eighteen slugs would swamp it and put the reader's directory tree
        # into every screenshot of the report. The names ride in the payload
        # instead, exactly as `durability.sources` does.
        self.assertNotIn("names_in_window", self.body)
        self.assertNotIn("names_in_database", self.body)


class ArchivedSourceDurabilityTest(unittest.TestCase):
    """A window whose measurements outlive their transcripts must SAY SO (#14).

    Once a transcript is reaped, its rows cannot be regenerated by
    re-ingesting -- the database is their only copy. A total computed over
    such a window is complete, but it is no longer *reproducible*, and a
    reader who does not know that will treat the database as derived data
    they can safely drop and rebuild. The durability block is what keeps
    "this number can be recomputed" distinguishable from "this number exists
    only here" (rule #12: the two facts must not render identically).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-durability-test-"))
        self.projects_dir, self.tasks_dir = build_corpus(self.tmp)
        self.db_path = self.tmp / "usage.db"
        ingest(self.projects_dir, self.db_path, tasks_dir=self.tasks_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _summary(self, frm=None, to=None):
        api = Api(self.db_path)
        try:
            return api.summary(*day_bounds(frm, to))
        finally:
            api.conn.close()

    def _reap_one_transcript(self) -> str:
        """Delete a main-thread transcript and re-ingest, as retention does."""
        victim = sorted(self.projects_dir.rglob("*.jsonl"))[0]
        path = str(victim)
        victim.unlink()
        ingest(self.projects_dir, self.db_path, tasks_dir=self.tasks_dir)
        return path

    def test_intact_corpus_reports_no_archived_sources(self) -> None:
        d = self._summary()["durability"]
        self.assertEqual(d["archived_sources"], 0)
        self.assertEqual(d["archived_calls"], 0)
        self.assertTrue(d["reproducible"])

    def test_window_over_a_reaped_transcript_is_flagged_irreproducible(self) -> None:
        reaped = self._reap_one_transcript()
        d = self._summary()["durability"]
        self.assertEqual(d["archived_sources"], 1)
        self.assertGreater(d["archived_calls"], 0)
        self.assertFalse(d["reproducible"])
        self.assertIn(reaped, d["sources"])

    def test_the_total_still_includes_the_archived_calls(self) -> None:
        # The fix RETAINS the rows -- the label is about reproducibility, not
        # truncation. A window that dropped them would report a smaller total.
        before = self._summary()["calls"]
        self._reap_one_transcript()
        after = self._summary()
        self.assertEqual(after["calls"], before)
        self.assertGreater(after["durability"]["archived_calls"], 0)

    def test_every_api_call_joins_to_a_tracked_source(self) -> None:
        # The durability block is a JOIN on api_calls.source_path =
        # ingest_state.path. If those two ever diverged the join would return
        # nothing and the banner would silently report "reproducible" over a
        # window that is not -- absence rendering as a healthy value. Both are
        # written from the same str(path), and this pins that.
        api = Api(self.db_path)
        try:
            total = api.conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
            joined = api.conn.execute(
                "SELECT COUNT(*) FROM api_calls a"
                " JOIN ingest_state i ON a.source_path = i.path"
            ).fetchone()[0]
        finally:
            api.conn.close()
        self.assertGreater(total, 0)
        self.assertEqual(joined, total, "orphaned api_calls break the durability join")

    def test_a_window_that_excludes_the_reaped_session_is_still_reproducible(
        self,
    ) -> None:
        # Scoping matters: the durability claim ranges over the SAME window as
        # the totals it qualifies, or it condemns windows it does not describe.
        self._reap_one_transcript()
        d = self._summary("2020-01-01", "2020-01-01")["durability"]
        self.assertEqual(d["archived_sources"], 0)
        self.assertTrue(d["reproducible"])


class AgentsPeriodFilterTest(unittest.TestCase):
    """`/api/agents` must range over the window on BOTH sides of the join.

    Filtering only `api_calls` returned dispatches whose calls all fall
    outside `[from, to)` as plausible rows with `calls=0`, under a panel whose
    empty state reads "No subagent dispatches in this period" -- and each one
    consumed a LIMIT slot a real in-window dispatch needed. Same defect class
    as #4955, one layer down.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-agentwin-test-"))
        self.projects_dir, self.tasks_dir = build_corpus(self.tmp)
        self.db_path = self.tmp / "usage.db"
        ingest(self.projects_dir, self.db_path, tasks_dir=self.tasks_dir)
        self.api = Api(self.db_path)

    def tearDown(self) -> None:
        self.api.conn.close()
        shutil.rmtree(self.tmp)

    def test_dispatches_are_listed_for_the_window_that_holds_their_calls(self) -> None:
        rows = self.api.agents(*day_bounds(None, None), 50)
        self.assertTrue(rows, "the corpus must hold at least one dispatch")
        self.assertTrue(
            any(r["status"] == "ingested" for r in rows),
            "the corpus must hold at least one measured dispatch",
        )

    def test_a_window_with_no_calls_lists_no_ingested_dispatch(self) -> None:
        rows = self.api.agents(*day_bounds("2020-01-01", "2020-01-01"), 50)
        self.assertEqual(
            [r for r in rows if r["status"] == "ingested"],
            [],
            "an ingested run with no in-window call is not this period's activity",
        )

    def test_an_unavailable_run_needs_an_in_window_session_too(self) -> None:
        rows = self.api.agents(*day_bounds("2020-01-01", "2020-01-01"), 50)
        self.assertEqual(rows, [], "no dispatch belongs to an empty window")


class AgentAttributionApiTest(unittest.TestCase):
    """#4966 elevated scope: "which agent cost me that" in one look, and a
    reaped transcript reported as UNAVAILABLE rather than as zero."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-agents-test-"))
        self.projects_dir, self.tasks_dir = build_corpus(self.tmp)
        self.db_path = self.tmp / "usage.db"
        ingest(self.projects_dir, self.db_path, tasks_dir=self.tasks_dir)
        self.api = Api(self.db_path)
        self.start, self.end = day_bounds(None, None)

    def tearDown(self) -> None:
        self.api.conn.close()
        shutil.rmtree(self.tmp)

    def test_agents_endpoint_ranks_by_total_tokens_and_names_the_agent(self) -> None:
        rows = self.api.agents(self.start, self.end, 20)
        by_id = {r["agent_id"]: r for r in rows}
        self.assertEqual(by_id["atest1"]["agent_type"], "laravel-expert")
        self.assertEqual(by_id["atest1"]["description"], "Fix widget")
        self.assertEqual(by_id["atest1"]["calls"], 2)
        self.assertEqual(by_id["atest1"]["cache_read"], 4090)
        self.assertEqual(by_id["atest2"]["agent_type"], "architect")
        self.assertEqual(by_id["atest2"]["session_id"], "other-session")
        # Ranked by TOTAL tokens descending: atest1 before atest2.
        ingested = [r["agent_id"] for r in rows if r["status"] == "ingested"]
        self.assertEqual(ingested, ["atest1", "atest2"])
        self.assertGreater(
            by_id["atest1"]["total_tokens"], by_id["atest2"]["total_tokens"]
        )

    def test_agents_endpoint_lists_unavailable_runs_with_null_not_zero(self) -> None:
        rows = self.api.agents(self.start, self.end, 20)
        gone = [r for r in rows if r["agent_id"] == "agone"]
        self.assertEqual(len(gone), 1)
        self.assertEqual(gone[0]["status"], "unavailable")
        # The whole point: an unmeasured agent must NOT report 0 spend. That
        # now includes the field the panel RANKS on -- a 0 there would sort a
        # reaped dispatch among the cheapest agents instead of showing a gap.
        self.assertIsNone(gone[0]["calls"])
        self.assertIsNone(gone[0]["cache_read"])
        self.assertIsNone(gone[0]["total_tokens"])

    def test_summary_scope_reports_unavailable_transcripts(self) -> None:
        cov = self.api.summary(self.start, self.end)["scope"]["coverage"]
        self.assertEqual(cov["subagent_transcripts_unavailable"], 1)
        self.assertEqual(cov["sessions_with_unavailable_subagents"], 1)

    def test_session_subagent_status_distinguishes_the_cases(self) -> None:
        rows = {r["id"]: r for r in self.api.sessions(self.start, self.end)}
        # session-fixture: has ingested subagent calls AND a reaped one ->
        # its total is incomplete, so it must not read as fully measured.
        self.assertEqual(rows["session-fixture"]["subagent_status"], "unavailable")
        self.assertEqual(rows["other-session"]["subagent_status"], "measured")

    def test_reaped_dispatch_does_not_mark_a_window_it_predates(self) -> None:
        """Sentry finding: the UNAVAILABLE flag was corpus-wide.

        A session appearing in several windows was marked `unavailable` in
        every one of them, including windows in which its reaped agent never
        ran. The flag is now scoped on the run's own `dispatched_at` -- the
        task-index entry's mtime, the only timestamp a reaped run has.
        """
        # Re-date the reaped run into a window we will NOT ask about. Nothing
        # else about the corpus changes, so any status change is attributable
        # to the date alone.
        # A BOUNDED window, not day_bounds(None, None): the unbounded range
        # contains every date, so it could not distinguish in-window from out.
        win_start, win_end = day_bounds("2026-07-28", "2026-07-28")
        far_past = day_bounds("2019-06-01", "2019-06-01")[0] + 3600
        self.api.conn.execute(
            "UPDATE subagent_runs SET dispatched_at = ? WHERE agent_id = 'agone'",
            (far_past,),
        )
        self.api.conn.commit()

        rows = {r["id"]: r for r in self.api.sessions(win_start, win_end)}
        self.assertEqual(
            rows["session-fixture"]["subagent_status"], "measured",
            "a reaped dispatch outside the window must not mark this window",
        )
        cov = self.api.summary(win_start, win_end)["scope"]["coverage"]
        self.assertEqual(cov["subagent_transcripts_unavailable"], 0)
        self.assertEqual(cov["sessions_with_unavailable_subagents"], 0)

        # ...and the window that DOES contain it still reports the gap, which
        # is what proves the row was re-scoped rather than dropped.
        start, end = day_bounds("2019-06-01", "2019-06-01")
        far_cov = self.api.summary(start, end)["scope"]["coverage"]
        self.assertEqual(far_cov["subagent_transcripts_unavailable"], 1)
        self.assertEqual(
            [r["agent_id"] for r in self.api.agents(start, end, 20)], ["agone"]
        )

    def test_a_reaped_run_with_no_timestamp_is_undated_not_measured(self) -> None:
        """Rule #12: an undatable gap is neither in nor out of a window.

        Rounding it toward `unavailable` re-creates the over-report above;
        rounding it toward `measured` claims a completeness nothing
        established. It gets its own status and its own corpus-wide count.
        """
        self.api.conn.execute(
            "UPDATE subagent_runs SET dispatched_at = NULL WHERE agent_id = 'agone'"
        )
        self.api.conn.commit()

        win_start, win_end = day_bounds("2026-07-28", "2026-07-28")
        rows = {r["id"]: r for r in self.api.sessions(win_start, win_end)}
        self.assertEqual(
            rows["session-fixture"]["subagent_status"], "unavailable-undated"
        )
        cov = self.api.summary(win_start, win_end)["scope"]["coverage"]
        # Not counted as an in-window gap...
        self.assertEqual(cov["subagent_transcripts_unavailable"], 0)
        # ...but never silently dropped either.
        self.assertEqual(cov["runs_undated_unavailable"], 1)

    def test_models_break_down_across_both_scopes(self) -> None:
        models = self.api.summary(self.start, self.end)["models"]
        key = {(m["model"], m["scope"]): m for m in models}
        # The subagent fixture's sonnet calls are a DIFFERENT row from the
        # main thread's sonnet calls -- same model, different scope.
        self.assertEqual(key[("claude-sonnet-5-20260115", "subagent")]["calls"], 2)
        self.assertEqual(key[("claude-sonnet-5-20260115", "main-thread")]["calls"], 2)
        self.assertEqual(key[("claude-opus-5-20260201", "subagent")]["calls"], 1)


class DataStalenessTest(unittest.TestCase):
    """How old the data is, as TWO facts that must never be conflated (#20).

    `serve.py` reads whatever the database holds and `ingest.py` never runs on
    its own, so a report left open serves older and older numbers with nothing
    saying so -- the reference install was 1.2 h behind with the page reading
    exactly as it does when current.

    Two timestamps, because either alone lies:

    * **last ingest run** -- when this tool last looked at the transcripts.
    * **newest measured call** -- the most recent thing it found, CORPUS-WIDE
      rather than window-scoped: it describes the database's freshness, not
      the period on screen, and windowing it would report a 2025 window as
      "months stale" when the ingest ran a minute ago.

    A fresh run over an idle machine is healthy; a fresh run that found
    nothing new is indistinguishable from no run unless both are shown.

    The fixture pins them DELIBERATELY UNEQUAL -- the run stamp is seconds old
    and the fixture's newest call is years older -- so a swapped mapping
    cannot pass.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-staleness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "session-fixture.jsonl"
        shutil.copy(FIXTURE, self.transcript)
        self.db_path = self.tmp / "usage.db"
        ingest(self.projects, self.db_path)
        self.newest_call = self._scalar("SELECT MAX(ts) FROM api_calls")
        self.assertIsNotNone(self.newest_call)

    def _scalar(self, sql: str):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql).fetchone()[0]
        finally:
            conn.close()

    def _write(self, sql: str, *params) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _set_run_stamp(
        self, finished_at: float, scope: str = RUN_SCOPE_CORPUS
    ) -> None:
        """Stamp the run through the ingester's own writer, never a raw INSERT.

        `scope` defaults to CORPUS because this fixture ingests with `ingest()`,
        so a corpus stamp is what the run it is standing in for produced. Tests
        about the single-file mode pass `RUN_SCOPE_FILE` explicitly -- the
        writer itself has no default (#105), for the reason its docstring gives.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            record_ingest_run(conn, finished_at, scope=scope)
            conn.commit()
        finally:
            conn.close()

    def _ingest_block(self, frm=None, to=None) -> dict:
        api = Api(self.db_path)
        try:
            return api.summary(*day_bounds(frm, to))["ingest"]
        finally:
            api.conn.close()

    def test_staleness_does_not_displace_the_other_warnings(self) -> None:
        # TWO alarming things are true at once. A payload that can only ever
        # express one at a time -- or a reader that shows the milder -- is the
        # same defect this feature exists to fix, one level up: the user sees
        # one true statement with no sign that the other exists.
        self.transcript.unlink()
        ingest(self.projects, self.db_path)  # archives it; rows retained
        self._set_run_stamp(time.time() - (STALE_AFTER_SECONDS + 3600))
        api = Api(self.db_path)
        try:
            payload = api.summary(*day_bounds(None, None))
        finally:
            api.conn.close()
        self.assertTrue(payload["ingest"]["stale"])
        self.assertFalse(payload["durability"]["reproducible"])
        self.assertGreater(payload["durability"]["archived_calls"], 0)
        self.assertGreater(payload["calls"], 0, "the totals are still reported")

    def test_both_timestamps_are_reported_as_separate_fields(self) -> None:
        run_at = time.time() - 30.0
        self._set_run_stamp(run_at)
        block = self._ingest_block()
        self.assertAlmostEqual(block["last_run_at"], run_at, places=3)
        self.assertAlmostEqual(block["newest_call_ts"], self.newest_call, places=3)
        self.assertNotAlmostEqual(
            block["last_run_at"],
            block["newest_call_ts"],
            msg="the two facts are one field, or the mapping is swapped",
        )

    def test_the_newest_call_ranges_over_the_WHOLE_corpus(self) -> None:
        # Freshness is a property of the database, not of the period on
        # screen. Windowed, this figure would call a deliberately historical
        # view stale and a narrow recent view fresh -- an aggregate answering
        # about a set nobody asked about.
        self._set_run_stamp(time.time())
        block = self._ingest_block("2020-01-01", "2020-01-02")
        self.assertAlmostEqual(block["newest_call_ts"], self.newest_call, places=3)

    def test_a_database_that_never_recorded_a_run_says_so(self) -> None:
        # Every database written before this landed is in this state, and it
        # is NOT "ingested at epoch 0" and NOT "0 seconds ago". No sample.
        self._write(f"DELETE FROM {INGEST_RUNS_TABLE}")
        block = self._ingest_block()
        self.assertIsNone(block["last_run_at"])
        self.assertIsNone(block["stale"], "unknown age is not a stale verdict")
        # The other fact is still measured and must still be reported.
        self.assertAlmostEqual(block["newest_call_ts"], self.newest_call, places=3)

    def test_a_v6_database_without_the_table_still_serves(self) -> None:
        # serve.py never migrates -- it opens the database as ingest left it.
        # A user who upgrades CPB and reads the report before re-running
        # ingest.py has NO `ingest_runs` table at all, and the page must
        # report an unknown age rather than 500 on every request.
        self._write(f"DROP TABLE {INGEST_RUNS_TABLE}")
        block = self._ingest_block()
        self.assertIsNone(block["last_run_at"])
        self.assertIsNone(block["stale"])
        self.assertEqual(block["files"], 1)

    def test_a_recent_run_is_not_stale(self) -> None:
        self._set_run_stamp(time.time() - (STALE_AFTER_SECONDS - 60))
        block = self._ingest_block()
        self.assertFalse(block["stale"])

    def test_a_run_older_than_the_threshold_is_stale(self) -> None:
        self._set_run_stamp(time.time() - (STALE_AFTER_SECONDS + 60))
        block = self._ingest_block()
        self.assertTrue(block["stale"])

    def test_a_run_stamped_in_the_FUTURE_is_not_a_freshness_verdict(self) -> None:
        # Reproduced 2026-08-05: a `finished_at` 30 days ahead of the server's
        # clock gives an age of -2,592,000 s, and `(as_of - last_run_at) >
        # STALE_AFTER_SECONDS` answered False -- "the data is fresh" -- while
        # the data-age line on the same render read "in the future (clock
        # skew)". One surface refused, the adjacent one answered.
        #
        # Three ways in, one of them explicitly in scope per CLAUDE.md's
        # Durability section: clock skew, a database copied between machines,
        # an NTP step during a run.
        self._set_run_stamp(time.time() + 30 * 86400)
        block = self._ingest_block()
        self.assertIsNone(block["stale"], "a negative age is not a fresh verdict")
        self.assertEqual(
            block["stale_unknown_reason"], STALE_UNKNOWN_RUN_IN_FUTURE
        )
        # ...and it is distinguishable from "no run was ever recorded": the
        # stamp is right there, it is the CLOCKS that disagree.
        self.assertIsNotNone(block["last_run_at"])

    def test_a_real_verdict_carries_no_unknown_reason(self) -> None:
        # The reason field qualifies an ABSENT verdict. Present beside a real
        # one, it would be a second, contradictory answer to the same question.
        self._set_run_stamp(time.time())
        block = self._ingest_block()
        self.assertIs(block["stale"], False)
        self.assertIsNone(block["stale_unknown_reason"])
        self._set_run_stamp(time.time() - (STALE_AFTER_SECONDS + 60))
        block = self._ingest_block()
        self.assertIs(block["stale"], True)
        self.assertIsNone(block["stale_unknown_reason"])

    def test_data_with_no_completed_run_is_NOT_a_database_that_predates_recording(
        self,
    ) -> None:
        # `ingest.py`'s schema comment promises that "a run that raised never
        # stamps, so a broken ingest ages visibly". The table is here -- this
        # database is at the current schema and holds real measurements -- and
        # no run has ever completed over it. That is a crashed or never-
        # finished ingest, and the page used to state, with confidence, the one
        # cause it CANNOT be.
        self._write(f"DELETE FROM {INGEST_RUNS_TABLE}")
        block = self._ingest_block()
        self.assertIsNone(block["stale"])
        self.assertEqual(
            block["stale_unknown_reason"],
            STALE_UNKNOWN_NO_RUN_RECORDED,
            "a schema that RECORDS run times cannot predate recording them",
        )
        # The facts that make it "crashed" rather than "empty and new".
        self.assertGreater(block["files"], 0)
        self.assertIsNotNone(block["newest_call_ts"])

    def test_only_a_MISSING_run_table_reads_as_predating_run_recording(self) -> None:
        # The one absence the database really can attribute: no table at all.
        # serve.py never migrates, so this is what a user who upgrades CPB and
        # opens the report before re-running ingest.py actually has.
        self._write(f"DROP TABLE {INGEST_RUNS_TABLE}")
        block = self._ingest_block()
        self.assertIsNone(block["stale"])
        self.assertEqual(
            block["stale_unknown_reason"], STALE_UNKNOWN_NO_RUN_TABLE
        )

    def test_the_threshold_and_the_clock_it_is_measured_against_are_published(
        self,
    ) -> None:
        # The reader is told the rule, not just the verdict, and the age is
        # computed against the SERVER's clock -- the machine that owns the
        # data -- rather than left to the browser's.
        self._set_run_stamp(time.time())
        block = self._ingest_block()
        self.assertEqual(block["stale_after_seconds"], STALE_AFTER_SECONDS)
        self.assertGreater(STALE_AFTER_SECONDS, 0)
        self.assertAlmostEqual(block["as_of"], time.time(), delta=5.0)

    def test_a_corpus_with_no_calls_has_no_newest_call_timestamp(self) -> None:
        empty_projects = self.tmp / "empty-projects"
        empty_projects.mkdir()
        empty_db = self.tmp / "empty.db"
        ingest(empty_projects, empty_db)
        api = Api(empty_db)
        try:
            block = api.summary(*day_bounds(None, None))["ingest"]
        finally:
            api.conn.close()
        self.assertIsNone(block["newest_call_ts"], "no calls is not a call at epoch 0")
        self.assertEqual(block["files"], 0)
        # The run itself DID happen, and is a separate fact from what it found.
        self.assertIsNotNone(block["last_run_at"])
        self.assertFalse(block["stale"])

    def test_a_full_scan_and_a_single_file_run_are_separate_fields(self) -> None:
        # #105. The pair `last_run_at` / `newest_call_ts` above is one axis;
        # this is the other, and it splits `last_run_at` rather than joining it.
        # Stamped DELIBERATELY APART so a payload that reported one field twice
        # cannot pass.
        scanned_at = time.time() - 600.0
        self._set_run_stamp(scanned_at, RUN_SCOPE_CORPUS)
        self._set_run_stamp(scanned_at + 300.0, RUN_SCOPE_FILE)
        block = self._ingest_block()
        self.assertAlmostEqual(block["last_full_scan_at"], scanned_at, places=3)
        self.assertAlmostEqual(block["last_run_at"], scanned_at + 300.0, places=3)
        self.assertIsNone(block["full_scan_unknown_reason"])

    def test_a_database_that_cannot_record_scope_says_unrecorded_not_none(
        self,
    ) -> None:
        # `serve.py` never migrates, so a pre-v13 database read by this build
        # has `ingest_runs` and no `corpus_finished_at`. Whether a full scan
        # ever ran is UNRECORDED. It is pointedly not answered "yes" either,
        # though every stamp such a build wrote did come from a corpus run --
        # `ingest.py` refuses that same deduction at the migration, and the
        # reader is owed the same refusal.
        self._write(
            f"ALTER TABLE {INGEST_RUNS_TABLE} DROP COLUMN corpus_finished_at"
        )
        block = self._ingest_block()
        self.assertIsNone(block["last_full_scan_at"])
        self.assertEqual(
            block["full_scan_unknown_reason"], FULL_SCAN_UNKNOWN_NO_SCOPE_COLUMN
        )
        # ...and it has NOT cost the freshness verdict, which reads the other
        # column and is unaffected.
        self.assertIs(block["stale"], False)

    def test_a_database_with_no_run_table_cannot_answer_either_question(
        self,
    ) -> None:
        self._write(f"DROP TABLE {INGEST_RUNS_TABLE}")
        block = self._ingest_block()
        self.assertIsNone(block["last_full_scan_at"])
        self.assertEqual(
            block["full_scan_unknown_reason"], FULL_SCAN_UNKNOWN_NO_RUN_TABLE
        )
        self.assertEqual(block["stale_unknown_reason"], STALE_UNKNOWN_NO_RUN_TABLE)

    def test_a_measured_full_scan_carries_no_unknown_reason(self) -> None:
        # Same rule as `stale_unknown_reason`: the reason field qualifies an
        # ABSENT answer, and beside a real one it is a second, contradictory
        # answer to the same question.
        self._set_run_stamp(time.time(), RUN_SCOPE_CORPUS)
        block = self._ingest_block()
        self.assertIsNotNone(block["last_full_scan_at"])
        self.assertIsNone(block["full_scan_unknown_reason"])


class HookOnlyDatabaseFreshnessTest(unittest.TestCase):
    """What the report says about a database only the plugin's hooks built.

    THE STATE EVERY PLUGIN USER IS IN, and no fixture built it before #105.
    `hooks/hooks.json` fires on `Stop`, `SubagentStop` and `SessionEnd`; each
    runs `hooks/cpb_ingest_hook.py`, which spawns `ingest.py --transcript` for
    exactly one file. `ingest()` is never called on such a machine, so single-
    file mode's silence about run stamps was the whole of what those installs
    reported: measured on `main` at 3.1.0, 2026-08-07, a hook run that ingested
    5 calls and exited 0 left `ingest_runs` empty, and the page then said

        INCONCLUSIVE: no ingest.py run has ever COMPLETED over this database
        ... a run that raises never stamps, so a failing ingest looks exactly
        like this. Re-run ingest.py and check it exits cleanly.

    permanently, on a working install, with advice that could never clear it --
    the mode they would re-run in was the mode that did not stamp.

    The two halves are asserted together on purpose. A hook-only database must
    read as CURRENT, and it must not thereby claim that anything ever scanned
    the corpus: the second is what a run over one file has no evidence for, and
    a fix that asserted it would be a different wrong number in the same field.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-hook-only-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path, self.transcripts = build_hook_only_database(self.tmp)

    def block(self) -> dict:
        api = Api(self.db_path)
        try:
            return api.summary(*day_bounds(None, None))["ingest"]
        finally:
            api.conn.close()

    def payload(self) -> dict:
        api = Api(self.db_path)
        try:
            return api.summary(*day_bounds(None, None))
        finally:
            api.conn.close()

    def test_the_fixture_really_is_hook_only(self) -> None:
        # Guarding the fixture, not the code. If this ever ingests through
        # `ingest()`, every assertion below stops describing a plugin install
        # and starts passing for the wrong reason.
        conn = sqlite3.connect(self.db_path)
        try:
            corpus = conn.execute(
                f"SELECT corpus_finished_at FROM {INGEST_RUNS_TABLE}"
            ).fetchone()
            calls = conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(corpus[0], "a directory run touched this fixture")
        self.assertGreater(calls, 0, "the fixture ingested nothing to report on")
        self.assertEqual(len(self.transcripts), 2)

    def test_a_working_install_is_not_told_its_ingest_may_have_failed(self) -> None:
        # THE defect, at the surface the user reads.
        block = self.block()
        self.assertIsNotNone(
            block["last_run_at"],
            "a hook-maintained database reports that no ingest ever completed",
        )
        self.assertIs(block["stale"], False)
        self.assertIsNone(
            block["stale_unknown_reason"],
            "the INCONCLUSIVE banner fires on a working plugin install",
        )

    def test_it_does_not_claim_a_corpus_scan_nobody_ran(self) -> None:
        block = self.block()
        self.assertIsNone(block["last_full_scan_at"])
        self.assertEqual(
            block["full_scan_unknown_reason"], FULL_SCAN_UNKNOWN_NONE_RECORDED
        )

    def test_the_health_band_agrees_with_the_staleness_verdict(self) -> None:
        # One of the three surfaces. It may not LOWER the verdict and it may not
        # raise one either: `ingest-age` maps the tri-state it was handed.
        payload = self.payload()
        check = next(
            c for c in payload["health"]["checks"] if c["check"] == CHECK_INGEST_AGE
        )
        self.assertEqual(check["state"], HEALTH_OK)

    def test_the_three_surfaces_read_one_source(self) -> None:
        # The requirement that outlives this fix: the staleness verdict, the
        # banner and the data-age line share `ingest.last_run_at` and must keep
        # sharing it. Asserted by RE-DERIVING the verdict from the payload's own
        # published fields -- if the block ever computed `stale` from something
        # the page cannot see, the page's rendering and the verdict would be
        # free to disagree, which is the defect `RANKED_BY` exists to stop one
        # layer up.
        block = self.block()
        stale, reason = staleness_verdict(
            block["last_run_at"], block["as_of"], True
        )
        self.assertIs(block["stale"], stale)
        self.assertEqual(block["stale_unknown_reason"], reason)

    def test_a_hook_run_that_raises_still_reads_as_unknown(self) -> None:
        # The true alarm the false one must not be fixed by deleting. A hook
        # whose spawned ingest died leaves no stamp, and a database in that
        # state is indistinguishable from one nothing has ever run over --
        # because it is.
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(f"DELETE FROM {INGEST_RUNS_TABLE}")
            conn.commit()
        finally:
            conn.close()
        block = self.block()
        self.assertIsNone(block["last_run_at"])
        self.assertIsNone(block["stale"])
        self.assertEqual(
            block["stale_unknown_reason"], STALE_UNKNOWN_NO_RUN_RECORDED
        )

    def test_a_second_hook_run_advances_the_age(self) -> None:
        # The property that makes the fix hold over time rather than once: the
        # `Stop` hook fires every turn, most of them changing nothing, so a
        # stamp written only when bytes moved would age past the threshold on a
        # database being kept current.
        first = self.block()["last_run_at"]
        time.sleep(0.01)
        ingest_transcript(self.transcripts[-1], self.db_path)
        self.assertGreater(self.block()["last_run_at"], first)


class StalenessThresholdTest(unittest.TestCase):
    """The threshold's VALUE, its boundary, and the sign of the age (#37).

    Every other staleness test stamps its fixture RELATIVE to the constant
    (`time.time() - (STALE_AFTER_SECONDS + 60)`), so the whole suite moves with
    it: on 2026-08-05 `STALE_AFTER_SECONDS` could be set to 25 hours -- past
    the 1.2 h incident that opened #20, i.e. past the only case the feature
    exists for -- and CI stayed green. The number with the most carefully
    documented provenance in the change was the one nothing tested.

    So this class asserts the constant against the two MEASUREMENTS that chose
    it rather than against itself, and asserts the verdict at the exact second
    on either side of it. `staleness_verdict` is a pure function of
    (`last_run_at`, `as_of`) precisely so that boundary can be stated without
    patching a clock, which would pin the test to a mocking seam instead.
    """

    # The unflagged staleness that opened #20: the reference install was this
    # far behind with the page reading exactly as it does when current. A
    # threshold at or above it would not have fired on its own motivating case.
    INCIDENT_SECONDS = 1.2 * 3600
    # What a refresh COSTS -- the floor, or the banner fires on people who are
    # re-ingesting. Measured 2026-08-04 on macOS 15 against that machine's
    # largest corpus (2,891 files, 1.9 GB): cold full ingest 39.9 s, all-skipped
    # incremental re-run 1.8 s. Both are quoted in serve.py beside the constant.
    COLD_INGEST_SECONDS = 39.9
    INCREMENTAL_INGEST_SECONDS = 1.8

    AS_OF = 1_800_000_000.0  # any fixed clock; the verdict is a function of AGE

    def verdict(self, age: float) -> tuple:
        return staleness_verdict(
            self.AS_OF - age, self.AS_OF, run_table_present=True
        )

    def test_the_threshold_is_fifteen_minutes(self) -> None:
        self.assertEqual(
            STALE_AFTER_SECONDS,
            900,
            "changing this number is a deliberate act: re-derive it from a "
            "fresh ingest measurement and update serve.py's provenance block",
        )

    def test_the_threshold_fires_on_the_incident_that_motivated_the_feature(
        self,
    ) -> None:
        # The ceiling, stated as behaviour rather than as a comparison of
        # constants: the case #20 was filed over must come out STALE.
        self.assertEqual(self.verdict(self.INCIDENT_SECONDS), (True, None))

    def test_the_threshold_clears_the_cost_of_re_ingesting(self) -> None:
        # The floor. A threshold near the cost of a refresh would fire on
        # people who ARE re-ingesting, which is how a banner gets ignored.
        self.assertGreater(STALE_AFTER_SECONDS, 10 * self.COLD_INGEST_SECONDS)
        self.assertEqual(self.verdict(self.COLD_INGEST_SECONDS), (False, None))
        self.assertEqual(
            self.verdict(self.INCREMENTAL_INGEST_SECONDS), (False, None)
        )

    def test_exactly_the_threshold_is_still_fresh(self) -> None:
        # `>`, not `>=`: the rule the UI publishes is "older than
        # `stale_after_seconds`", so the threshold second itself is the last
        # fresh one. Stated because it is the only place the two spellings
        # differ, and nothing else in the suite can tell them apart.
        self.assertEqual(self.verdict(STALE_AFTER_SECONDS), (False, None))

    def test_one_second_either_side_of_the_threshold_straddles_the_verdict(
        self,
    ) -> None:
        self.assertEqual(self.verdict(STALE_AFTER_SECONDS - 1), (False, None))
        self.assertEqual(self.verdict(STALE_AFTER_SECONDS + 1), (True, None))

    def test_a_run_that_just_finished_is_a_real_zero_not_an_unknown(self) -> None:
        # Age 0 is a MEASURED age, and the freshest one there is. It must not
        # be swept into the "cannot tell" branch along with the negative ages.
        self.assertEqual(self.verdict(0.0), (False, None))

    def test_a_negative_age_is_cannot_tell_and_never_a_verdict(self) -> None:
        # #34. `abs()` on the subtraction survives every other test in this
        # suite and turns "these two clocks disagree" into "this data is old";
        # the unmutated expression turned it into "this data is fresh". Both
        # answer a question the arithmetic cannot: with the stamp ahead of the
        # server's clock there is no age to compare with anything.
        for age in (-1.0, -(STALE_AFTER_SECONDS + 60), -30 * 86400.0):
            with self.subTest(age=age):
                self.assertEqual(
                    self.verdict(age), (None, STALE_UNKNOWN_RUN_IN_FUTURE)
                )

    def test_the_two_absent_stamps_are_told_apart_by_the_table(self) -> None:
        # No stamp has two causes and they are not the same claim: no table is
        # a database written before CPB recorded run times; an empty table is
        # a database that records them and over which no run has ever finished.
        self.assertEqual(
            staleness_verdict(None, self.AS_OF, run_table_present=False),
            (None, STALE_UNKNOWN_NO_RUN_TABLE),
        )
        self.assertEqual(
            staleness_verdict(None, self.AS_OF, run_table_present=True),
            (None, STALE_UNKNOWN_NO_RUN_RECORDED),
        )


def js_function_body(html: str, decl: str) -> str:
    """The body of a JS function in `index.html`, comments stripped.

    Brace-matched from the declaration rather than regexed, so a nested block
    cannot end the match early.
    """
    start = html.index(decl)
    depth, i = 0, html.index("{", start)
    for end in range(i, len(html)):
        if html[end] == "{":
            depth += 1
        elif html[end] == "}":
            depth -= 1
            if depth == 0:
                body = html[i : end + 1]
                break
    else:  # pragma: no cover - unbalanced braces means the parse is wrong
        raise AssertionError(f"could not find the end of {decl}")
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return re.sub(r"^\s*//.*$", "", body, flags=re.M)


def strip_comments(source: str) -> str:
    """`index.html` with HTML and whole-line JS comments removed.

    Every structural assertion below runs over this rather than the raw file:
    the prose in this repository's comments quotes the very strings the tests
    look for, so an un-stripped haystack lets a DELETED binding keep passing
    because its rationale is still described above it.
    """
    source = re.sub(r"<!--.*?-->", "", source, flags=re.S)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


def html_element(html: str, needle: str) -> str:
    """The source of the element whose opening tag contains `needle`.

    Tag-matched rather than regexed to the next `</div>`, so a nested element
    of the same name cannot end the match early. Comments are stripped, for
    the reason `strip_comments` gives.

    The Alpine rewrite moved most of the page's branching OUT of JS function
    bodies and INTO templates, so the checks that used to read a render
    function's source now read the element it renders into. That is the point:
    a binding cannot be present in the template and absent from the page.
    """
    at = html.index(needle)
    start = html.rindex("<", 0, at)
    tag = re.match(r"<([a-zA-Z][a-zA-Z0-9-]*)", html[start:])
    if tag is None:  # pragma: no cover - means the needle is not in a tag
        raise AssertionError(f"{needle!r} is not inside an opening tag")
    name = tag.group(1)
    token = re.compile(rf"<{name}\b|</{name}\s*>", re.I)
    depth = 0
    for m in token.finditer(html, start):
        depth += 1 if m.group(0).startswith("</") is False else -1
        if depth == 0:
            return strip_comments(html[start : m.end()])
    raise AssertionError(f"could not find the end of the <{name}> at {needle!r}")


class BannerPrecedenceTest(unittest.TestCase):
    """Three writers, one banner element: the loudest true thing must win (#20).

    `renderSummary` rebuilt the banner's whole text on every render and
    `reportLoadFailure` overwrote it wholesale, so the later writer decided
    which of two true statements the reader got -- and the staleness warning
    added here would have been a third. A user shown an unparsed-records notice
    while a load failure or a stale database goes unmentioned has been handed
    the milder fact with no sign the harsher one exists, which is this
    repository's core rule failing inside the feature built to enforce it.

    Since #8 the "one writer" half is not a convention any more. The element
    carries a single `x-text` binding over `bannerMessages` and NO code in the
    page reaches into the DOM at all, so a second writer is unreachable rather
    than merely discouraged -- that is asserted below as an absence of DOM
    access, not as a count of one call site.

    The rest stays STRUCTURAL, and that is a real limit, not a stylistic
    choice: the project ships no JS runtime (stdlib-only, no Node), so the
    composition cannot be executed here. `DataStalenessTest` covers the same
    two-things-true-at-once case at the layer that CAN be executed.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (Path(__file__).resolve().parent.parent / "index.html").read_text()
        cls.html = strip_comments(cls.raw)

    def test_the_banner_element_has_exactly_one_writer(self) -> None:
        # ONE binding, ON the banner element, and nothing else in the page can
        # write it -- the imperative DOM handle that made three writers
        # possible is gone from the whole file.
        banner = html_element(self.raw, 'id="banner"')
        self.assertIn("x-text=\"bannerMessages.join(' ')\"", banner)
        self.assertEqual(
            self.html.count('x-text="bannerMessages'),
            1,
            "a second binding over the banner text is how one warning "
            "silently replaces another",
        )
        for handle in ("getElementById", "querySelector", "innerHTML"):
            with self.subTest(dom_api=handle):
                self.assertNotIn(handle, self.html)

    def test_the_precedence_order_is_failure_then_stale_then_notices(self) -> None:
        body = js_function_body(self.html, "get bannerMessages(")
        order = [body.index(k) for k in ("loadFailure", "stale", "notices")]
        self.assertEqual(
            order,
            sorted(order),
            "banner precedence is failure > stale > notices, by how badly the "
            "figures on screen can mislead",
        )

    def test_a_load_failure_is_cleared_only_by_a_fully_successful_load(self) -> None:
        # A failure that any later render can clear is a failure that can be
        # hidden by a partial success.
        self.assertEqual(self.html.count("this.banner.loadFailure = null"), 1)
        self.assertIn(
            "this.banner.loadFailure = null",
            js_function_body(self.html, "async load("),
        )

    def test_only_a_true_staleness_verdict_raises_the_banner(self) -> None:
        # `stale` is tri-state; the null case ("no ingest run was ever
        # recorded") is an UNKNOWN age, and a banner it can never clear would
        # train the reader to ignore the one that means something.
        body = js_function_body(self.html, "applySummary(")
        self.assertIn("this.summary.ingest.stale === true", body)

    def test_the_data_age_line_distinguishes_never_recorded_from_zero(self) -> None:
        # The BRANCH, not a mention: `=== 0` reads a never-recorded run as an
        # ingest at the epoch, and matching the bare substring elsewhere in the
        # region let exactly that mutation survive.
        band = html_element(self.raw, 'id="data-age"')
        self.assertRegex(band, r'x-if="summary\.ingest\.last_run_at === null"')
        self.assertRegex(band, r'x-if="summary\.ingest\.newest_call_ts === null"')
        self.assertIn("not recorded", band)


class DataAgeCauseTest(unittest.TestCase):
    """The page may not assert a cause the payload cannot support (#34).

    `renderDataAge`'s null branch said "this database predates CPB recording
    ingest times" -- ONE named cause, stated with confidence, for an absence
    with three reachable ones. Two of the three (an ingest that raised after
    the schema was stamped, an upgrade whose run table is empty until a run
    completes) describe a database that is at the CURRENT schema and whose
    ingest has not finished, possibly for a month. Those readers were told
    their database was merely old, and the most stale state the tool can be in
    produced its weakest signal: a grey line, no banner, indefinitely.

    Structural assertions, with the same limit `BannerPrecedenceTest` records:
    the project ships no JS runtime (stdlib-only, no Node), so these pin the
    branch and the vocabulary rather than executing the render. They are
    written against the reason strings `serve.py` defines, so a rename on
    either side goes red instead of silently unwiring the branch.

    Since #8 the branch lives in the data-age element's template rather than in
    a `renderDataAge()` body, so that is what these read. Quoting is matched
    permissively because an Alpine expression sits inside a double-quoted
    attribute and therefore spells its own strings with single quotes -- the
    assertion is about the branch, not about which quote character carries it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (Path(__file__).resolve().parent.parent / "index.html").read_text()

    def data_age(self) -> str:
        return html_element(self.html, 'id="data-age"')

    def summary(self) -> str:
        return js_function_body(strip_comments(self.html), "applySummary(")

    def test_the_data_age_line_branches_on_the_reason_the_api_reports(self) -> None:
        body = self.data_age()
        self.assertIn("ingest.stale_unknown_reason", body)
        for reason in (STALE_UNKNOWN_NO_RUN_TABLE, STALE_UNKNOWN_NO_RUN_RECORDED):
            with self.subTest(reason=reason):
                self.assertRegex(body, r"""['"]%s['"]""" % re.escape(reason))

    def test_the_predates_claim_is_reachable_only_under_the_missing_table(
        self,
    ) -> None:
        # THE defect: the claim itself is fine, unconditionally making it is
        # not. Asserted as "appears once, after the guard that establishes it",
        # because a second copy outside the branch is exactly the regression.
        body = self.data_age()
        self.assertEqual(body.count("predates"), 1)
        guard = re.search(r"""['"]%s['"]""" % re.escape(STALE_UNKNOWN_NO_RUN_TABLE), body)
        self.assertIsNotNone(guard, "nothing tests for the missing run table")
        self.assertLess(
            guard.start(),
            body.index("predates"),
            "the 'predates CPB' claim is made before anything establishes it",
        )

    def test_a_crashed_ingest_is_named_as_a_possible_cause(self) -> None:
        body = self.data_age()
        self.assertIn("may have failed", body)

    def test_clock_skew_is_raised_as_an_operator_visible_notice(self) -> None:
        # A verdict of "cannot tell" belongs in the same register as the
        # archived-source and unparsed-record notices, not only in the grey
        # data-age line -- `fmtAge` already prints "in the future (clock skew)"
        # there while nothing above it says the freshness verdict is void.
        body = self.summary()
        self.assertRegex(
            body,
            r'stale_unknown_reason === "%s"\)\s*msgs\.push\('
            % re.escape(STALE_UNKNOWN_RUN_IN_FUTURE),
        )

    def test_a_database_with_no_completed_run_raises_a_notice_too(self) -> None:
        body = self.summary()
        self.assertRegex(
            body,
            r'stale_unknown_reason === "%s"\)\s*msgs\.push\('
            % re.escape(STALE_UNKNOWN_NO_RUN_RECORDED),
        )

    def test_neither_notice_becomes_a_staleness_verdict(self) -> None:
        # The banner's `stale` slot stays reserved for an actual `true`. An
        # unknown age that seized it would be a warning the reader cannot
        # clear by re-running ingest when the cause is a skewed clock.
        body = self.summary()
        self.assertIn("this.summary.ingest.stale === true", body)
        self.assertEqual(body.count("this.banner.stale ="), 1)

    def test_the_data_age_line_names_the_set_the_age_ranges_over(self) -> None:
        # #105. The age above is now true for a hook-maintained database, and
        # this clause is what stops it from also reading as "the corpus was
        # rescanned". Both absences the API can report are branched on by name,
        # never inferred from the timestamp being null.
        body = self.data_age()
        self.assertIn("ingest.last_full_scan_at", body)
        self.assertIn("ingest.full_scan_unknown_reason", body)
        for reason in (
            FULL_SCAN_UNKNOWN_NONE_RECORDED,
            FULL_SCAN_UNKNOWN_NO_SCOPE_COLUMN,
        ):
            with self.subTest(reason=reason):
                self.assertRegex(body, r"""['"]%s['"]""" % re.escape(reason))

    def test_no_full_corpus_scan_is_not_raised_as_a_failure(self) -> None:
        # It is the plugin's NORMAL state, not a fault: a hook-maintained
        # database is current for the sessions it was handed. Putting it in the
        # banner would restore, one field over, exactly the permanent false
        # alarm #105 removed -- and this one could not be cleared either,
        # because the hooks will never run a corpus scan.
        body = self.summary()
        self.assertNotIn("full_scan_unknown_reason", body)


# ---------------------------------------------------------------------------
# #76: the wiring guard walks the payload BY PATH, not by top-level key.
# ---------------------------------------------------------------------------
#
# `for k in payload` walked the top level and nothing else, so `context` --
# bound, and therefore passed -- hid everything inside it however deep. That is
# how PR #71 shipped `context.utilisation.by_scope` with no consumer at all and
# a green suite: the check silently ranged over a smaller set than its name
# claimed, which is this repository's own rule ("an aggregate must name the set
# it ranges over") failing inside its own safety net.
#
# The walk below is defined by three rules, and each one exists because the
# alternative would have let the shipped defect through:
#
#   * a field is named by its PATH (`context.utilisation.by_scope`), and every
#     path is checked, however deep;
#   * an ARRAY OF OBJECTS is walked through, not stepped over. `by_scope` is a
#     list, so a walk that descended only into `dict` values would leave exactly
#     the shape that caused this uncovered. Its elements are reached through the
#     `x-for` alias that iterates them, so a field rendered only inside a loop
#     over a nested list counts as bound -- which is the ordinary way this page
#     renders a row;
#   * a node the walk cannot adjudicate makes it REFUSE (`UnwalkablePayload`),
#     never skip. Skipping is the defect in miniature: a check that cannot see
#     its answer must not report a clean one.
#
# It is still a heuristic about a page no test here can execute (stdlib only, no
# JS runtime), and it is still defeated by destructuring a payload object into a
# local. It is defeated by a deliberate act rather than by depth.

# Everything a JSON payload can hold that is not a container. Anything else --
# a set, a datetime, an object -- means the payload grew a shape this walk was
# never designed for, and it says so instead of quietly ignoring it.
PAYLOAD_LEAVES = (str, int, float, bool, type(None))

# `x-for="s in summary.context.utilisation.by_scope"` and
# `x-for="(b, i) in s.bands"`: the loop VARIABLE, and what it iterates.
X_FOR = re.compile(r'x-for="\(?\s*([A-Za-z_$][\w$]*)[^"]*?\bin\b([^"]*)"')
# A mapping consumed WHOLE, e.g. `Object.entries(this.summary.context.percentiles)`.
# Every member of an iterated mapping reaches the reader by construction, which
# is a real structural guarantee and not an exemption: `contextSpread` renders
# one line per percentile without naming any of them.
BULK_READ = re.compile(r"Object\.(?:keys|values|entries)\(([^()]*)\)")
# `get shownModels() {`: a component getter, which is the one indirection this
# walk follows between an `x-for` and the payload it renders (#70).
GETTER_DECL = re.compile(r"\bget\s+([A-Za-z_$][\w$]*)\s*\(\s*\)\s*\{")

# Never a plausible substitute for a resolved node (rule #12 in the test layer).
UNRESOLVED = object()


class UnwalkablePayload(AssertionError):
    """The walk met something it cannot adjudicate, so it refuses.

    Raised rather than skipped, and an `AssertionError` so it fails the test
    that provoked it rather than erroring out of the suite unattributed. The
    three reachable causes are a value of an unforeseen type, a list that mixes
    objects with scalars, and a list the fixture leaves EMPTY -- an empty list
    cannot show what it holds, so a walk that stepped over one would report
    every field of its elements as fine without having looked at one.
    """


def binding_regex(expr: str) -> "re.Pattern[str]":
    """`summary.context.utilisation` -> the property access that reads it.

    Optional-chained at every hop (`summary?.context?.utilisation`), because
    both spellings are the same read.
    """
    return re.compile(
        r"\b" + r"\??\.".join(re.escape(part) for part in expr.split(".")) + r"\b"
    )


def reads_any(source: str, exprs) -> bool:
    """Does `source` read the node those expressions name?"""
    return any(binding_regex(expr).search(source) for expr in exprs)


def getter_bodies(surface: str) -> dict[str, str]:
    """Every `get name() { ... }` in the page, by name.

    Brace-matched through `js_function_body`, so a nested block cannot end a
    body early and a getter that merely MENTIONS a field in a comment cannot
    stand in for one that reads it.
    """
    return {
        name: js_function_body(surface, f"get {name}(")
        for name in GETTER_DECL.findall(surface)
    }


def iterates(surface: str, iterated: str, exprs) -> bool:
    """Does this `x-for`'s expression iterate the node `exprs` names?

    Containment rather than equality on the expression, because the page
    legitimately guards one: `x-for="(r, i) in (summary ? summary.models : [])"`
    iterates `summary.models` through a ternary, and a rule that demanded the
    bare expression would call that list uniterated.

    #70 added the second clause. A view that FILTERS a table iterates a getter
    -- `x-for="(r, i) in (shownModels ?? [])"` -- and the payload it renders is
    one hop away, inside that getter. A guard that stopped at the loop would
    report the whole of `summary.models` unwired the moment a deep-link filter
    was added to the panel that renders it, which is a check reporting an
    absence it can see is not there.

    EXACTLY ONE HOP, through a getter DEFINED IN THIS PAGE, and the hop must
    itself read the payload. A getter that stopped reading `summary.models`
    turns the guard red again, which is the property worth having: the
    indirection is followed, not excused.
    """
    if reads_any(iterated, exprs):
        return True
    return any(
        re.search(rf"\b{re.escape(name)}\b", iterated) and reads_any(body, exprs)
        for name, body in getter_bodies(surface).items()
    )


# #89: the OTHER way this page iterates a list, and it is not a preference.
# The gauge's arcs are drawn inside `<svg>`, where the HTML parser is in
# foreign content and `<template>` is an SVG-namespaced element with no
# `.content` -- so Alpine has nothing to clone and an `x-for` there is a loop
# that cannot run. The dial therefore hands its segments to a page function
# that maps them, and a walk that only knew `x-for` would report every field of
# every segment as unread while the page draws all of them.
#
# A JS array callback IS an iteration, so it binds an element alias exactly as
# `x-for` does. `iterates()`'s rule applies unchanged: EXACTLY ONE HOP, through
# code DEFINED IN THIS PAGE, and the hop must itself iterate what it was
# handed. A function that stopped reading its argument turns the guard red
# again, which is the property worth having.
ARRAY_CALLBACK = re.compile(
    r"([A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*)*)\s*\.\s*"
    r"(?:map|filter|forEach|find|some|every|flatMap)\s*\(\s*\(?\s*"
    r"([A-Za-z_$][\w$]*)"
)
PAGE_FUNCTION = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)")


def _callback_aliases_here(surface: str, exprs) -> frozenset:
    """Element aliases bound by an array callback ON these expressions."""
    return frozenset(
        param
        for receiver, param in ARRAY_CALLBACK.findall(surface)
        if reads_any(receiver, exprs)
    )


def callback_aliases(surface: str, exprs) -> frozenset:
    """Element aliases an array callback binds, directly or one hop away.

    The hop is a page-level `function` called with an expression that reads the
    list: its matching PARAMETER is that list inside the body, so a callback on
    the parameter is a callback on the list. Positional, so passing the list in
    the wrong slot does not count, and the body must really iterate it.
    """
    aliases = set(_callback_aliases_here(surface, exprs))
    for name, params in PAGE_FUNCTION.findall(surface):
        names = [part.strip() for part in params.split(",") if part.strip()]
        if not names:
            continue
        body = None
        for call in re.findall(rf"\b{re.escape(name)}\s*\(([^()]*)\)", surface):
            args = [arg.strip() for arg in call.split(",")]
            for index, arg in enumerate(args):
                if index >= len(names) or not reads_any(arg, exprs):
                    continue
                if body is None:
                    body = js_function_body(surface, f"function {name}(")
                aliases |= _callback_aliases_here(body, frozenset({names[index]}))
    return frozenset(aliases)


def iteration_aliases(surface: str, exprs) -> frozenset:
    """The variables the page binds a list's ELEMENTS to, however it loops."""
    return frozenset(
        var for var, iterated in X_FOR.findall(surface) if iterates(surface, iterated, exprs)
    ) | callback_aliases(surface, exprs)


def is_read_whole(surface: str, exprs) -> bool:
    """Is this mapping handed to `Object.entries`/`values`/`keys` entire?

    EQUALITY on the argument, not containment: `Object.entries(x.percentiles)`
    consumes `percentiles`, and it says nothing whatever about `x` -- which is
    the whole payload. Containment here would declare every field on the page
    reached, by one call.
    """
    consumed = {
        re.sub(r"^this\.", "", arg.strip()) for arg in BULK_READ.findall(surface)
    }
    return bool(consumed & set(exprs))


def merge_rows(rows: Sequence[dict]) -> dict:
    """One representative row, keeping the MOST informative value per key.

    A list's elements are one shape, and the fixture pins them deliberately
    unequal: `by_scope[0].unknown_models` names a model while `by_scope[1]`'s is
    empty, because one scope ran an unwindowed model and the other did not.
    Walking the empty one alone would refuse a list it could have read, so the
    key is taken from whichever row can answer for it.
    """
    merged: dict = {}
    for row in rows:
        for key, value in row.items():
            if key not in merged:
                merged[key] = value
            elif merged[key] is None and value is not None:
                merged[key] = value
            elif isinstance(merged[key], (dict, list)) and not merged[key] and value:
                merged[key] = value
    return merged


def walk_payload(
    node,
    surface: str,
    declared,
    path: str = "",
    exprs=frozenset({"summary"}),
) -> list:
    """Every path in `node` that nothing in `surface` reads, deepest first.

    `declared` is the allowlist: a declared path is neither reported nor
    descended into, which is what an exemption MEANS -- the fields under it are
    covered by the decision recorded there, not by this walk.
    """
    unwired: list = []
    if isinstance(node, dict):
        if is_read_whole(surface, exprs):
            for key, value in node.items():
                if not isinstance(value, PAYLOAD_LEAVES):
                    raise UnwalkablePayload(
                        f"{path}.{key}: a container inside a mapping the page "
                        "consumes whole. The iteration reaches the member; it "
                        "cannot say which of the member's own fields are read."
                    )
            return unwired
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if child in declared:
                continue
            child_exprs = frozenset(f"{expr}.{key}" for expr in exprs)
            if not reads_any(surface, child_exprs):
                unwired.append(child)
                continue
            unwired += walk_payload(value, surface, declared, child, child_exprs)
        return unwired
    if isinstance(node, list):
        return _walk_rows(node, surface, declared, path, exprs)
    if not isinstance(node, PAYLOAD_LEAVES):
        raise UnwalkablePayload(
            f"{path}: {type(node).__name__} is not a shape this walk can read."
        )
    return unwired


def _walk_rows(rows: Sequence, surface: str, declared, path: str, exprs) -> list:
    """A list, adjudicated by what it holds."""
    if not rows:
        raise UnwalkablePayload(
            f"{path}: empty in this fixture, so the walk cannot see what it "
            "holds. Give the fixture a row, or declare the path."
        )
    objects = [row for row in rows if isinstance(row, dict)]
    if objects and len(objects) != len(rows):
        raise UnwalkablePayload(
            f"{path}: mixes objects with scalars, so no one rule reads it."
        )
    if not objects:
        for row in rows:
            if not isinstance(row, PAYLOAD_LEAVES):
                raise UnwalkablePayload(
                    f"{path}: holds {type(row).__name__}, which the walk cannot read."
                )
        # A list of scalars carries no field of its own: the binding on the
        # list itself is the whole of what there is to check, and it passed.
        return []
    aliases = iteration_aliases(surface, exprs)
    if not aliases:
        return [f"{path}[]"]
    return walk_payload(merge_rows(objects), surface, declared, f"{path}[]", aliases)


def locate_path(payload: dict, surface: str, path: str):
    """The node an allowlist path names, and the expressions that reach it.

    `(UNRESOLVED, empty)` when it does not resolve -- a stale exemption must
    fail loudly rather than pass by naming nothing.
    """
    node, exprs = payload, frozenset({"summary"})
    for step in path.split("."):
        key, mark, _ = step.partition("[]")
        if not isinstance(node, dict) or key not in node:
            return UNRESOLVED, frozenset()
        node = node[key]
        exprs = frozenset(f"{expr}.{key}" for expr in exprs)
        if mark:
            if not isinstance(node, list) or not node:
                return UNRESOLVED, frozenset()
            if not all(isinstance(row, dict) for row in node):
                return UNRESOLVED, frozenset()
            node = merge_rows(node)
            exprs = iteration_aliases(surface, exprs)
    return node, exprs


# The reasons the register below records, spelled once each because several
# paths share one decision and a copied reason is a reason free to drift.
SCOPE_SPLIT_IS_PLOTTED = (
    "#4966: the per-scope TOKEN split is plotted per day by "
    "/api/timeseries?by=scope, which the chart already renders, and the "
    "API-calls card states the per-scope CALL counts beside it. Tabulating it "
    "here as well would be a second surface for one figure (constraint 4)."
)
REAPED_IN_WINDOW_HAS_NO_READER = (
    "#41: the scope band renders `runs_undated_unavailable` -- the residue "
    "that belongs to no window -- and the sessions table marks reaped "
    "dispatches per session through `subagent_status`. These two "
    "window-scoped counts have no reader of their own. Declared by #76, which "
    "is the first check able to see them; a banner line for them is its own "
    "change."
)
PROJECT_NAMES_ARE_COUNTS_ON_THE_PAGE = (
    "#44: COUNTS, not names, by decision. A project name is the working "
    "directory with its separators folded to `-`, so it carries the username "
    "and the repo path, and eighteen of them would swamp a one-line band. "
    "/api/summary carries them for anyone who needs them."
)


class SummaryPayloadIsWiredTest(unittest.TestCase):
    """Every field `/api/summary` computes must REACH a reader, or say why not.

    The defect this exists to catch, found by hand: `summary()` has always
    returned a global per-model breakdown, `models()` has always been tested
    (`test_models_break_down_across_both_scopes` above), and NOTHING in
    `index.html` ever read it. The `d.models` reference in the page belongs to
    the session-detail payload -- a different object -- so a grep for "models"
    looked wired while the window-level breakdown was dark.

    That is the "fully tested, never wired" pattern, and a passing API test is
    exactly what disguises it: the data layer is correct, so every test is
    green while no user can see the number.

    The allowlist is the point. A field may legitimately not be rendered, but
    that must be a DECLARED decision with a reason, not an accident nobody
    noticed -- so adding a field to the payload forces the author either to
    render it or to name it here.

    #8 DECISION: STRENGTHENED, NOT RETIRED.
    ---------------------------------------
    The Alpine rewrite makes `x-for` a real structural guarantee for the ROW
    payloads: `/api/sessions`, `/api/agents`, `/api/outliers` and
    `summary.models` are iterated by a template, and a column cannot exist
    without markup that names it. `/api/summary`'s TOP-LEVEL fields are not
    like that. They are scalars rendered by individually named bindings, so
    nothing structural forces any of them to have a consumer -- which is
    exactly where the original defect lived (`models`, a summary field). The
    guarantee therefore does not cover this payload and retiring the check here
    would lose coverage at the only place it ever caught anything.

    What changed is the haystack, which is the weakness Qodo named. It was one
    function's body, matched as `s.<field>`; that missed every field consumed
    by a template and could be defeated by a rename to any other local. It is
    now the WHOLE PAGE with comments stripped, matched as a property access on
    the one identifier the payload is ever bound through: the component's
    `summary` state. Every consumer, JS or template, spells it `summary.<field>`
    (or `summary?.<field>`), so this ranges over the render layer entirely
    rather than over one function of nine.

    It is still a heuristic and still not a proof -- destructuring `summary`
    into a local would defeat it. It is now defeated by a deliberate act rather
    than by ordinary refactoring.

    NOT_RENDERED is empty, and that is a fix, not a simplification: it listed
    the four token totals as "plotted per-day via /api/timeseries" while the
    summary cards had been rendering `s.input`, `s.cache_read`, `s.cache_write`
    and `s.output` all along. A stale exemption is how an allowlist rots into a
    rubber stamp, so a field that IS rendered may no longer be listed here --
    `test_the_allowlist_cannot_exempt_a_field_that_is_rendered` pins that.

    #31's `context` was the fifth entry, and it is the allowlist working as
    designed rather than rotting: its data layer landed first, on purpose,
    declaring the field as one OWED a consumer because wiring it into the
    string-concatenating `renderSummary` this change deletes would have been
    written to be thrown away. This change is the consumer it was waiting for,
    so the entry is gone and `ContextReferentIsBoundTest` says what was decided
    instead -- which is the stronger record. An allowlist entry can only say
    that somebody thought about it.

    #76: THE HAYSTACK WAS RIGHT AND THE NEEDLE SET WAS NOT.
    ------------------------------------------------------
    All of the above ranged over the TOP LEVEL of the payload. `context` is a
    top-level key, it is bound, and it passed -- so everything inside it was
    unchecked, which is how `context.utilisation.by_scope` shipped with no
    consumer at all and a green suite. The walk is now by PATH and goes all the
    way down, through arrays of objects, refusing rather than skipping anything
    it cannot adjudicate (see `walk_payload`).

    That turned the register from empty to twenty-one entries, and the entries
    are the FINDING, not a regression. Every one of them is a decision somebody
    made and nobody ever declared, because until this change nothing could see
    them to ask. An undeclared decision is invisible; a declared one can be
    argued with. Two things keep the list from rotting into the rubber stamp
    this class was built to prevent: an entry that names a field the page DOES
    render fails, and an entry that names a field the payload no longer has
    fails -- both by path, and both mutation-checked below.

    #61's own subtree needs no entry. Everything `context` computes, down to
    each band's boundaries and each scope's reason for having no sample, is
    rendered -- which is the point of doing the guard and the render together.
    """

    # Fields `/api/summary` computes that the page deliberately does not show,
    # BY PATH. An entry must name its reason and cite the issue that owns the
    # decision (`test_every_exemption_cites_the_decision_it_records`). Empty is
    # the healthy state and this is not it -- see the class docstring.
    NOT_RENDERED: dict[str, str] = {
        "scope.includes": (
            "#4966: the scope band composes its own sentence from `coverage` "
            "and SHOUTS 'MAIN-THREAD ONLY' where this string would merely say "
            "it. Two spellings of one fact is a smell; declared by #76 rather "
            "than fixed here, because rewording that band is neither #76's "
            "subject nor #61's."
        ),
        "scope.main_thread.input": SCOPE_SPLIT_IS_PLOTTED,
        "scope.main_thread.cache_read": SCOPE_SPLIT_IS_PLOTTED,
        "scope.main_thread.cache_write": SCOPE_SPLIT_IS_PLOTTED,
        "scope.main_thread.output": SCOPE_SPLIT_IS_PLOTTED,
        "scope.subagent.input": SCOPE_SPLIT_IS_PLOTTED,
        "scope.subagent.cache_read": SCOPE_SPLIT_IS_PLOTTED,
        "scope.subagent.cache_write": SCOPE_SPLIT_IS_PLOTTED,
        "scope.subagent.output": SCOPE_SPLIT_IS_PLOTTED,
        "scope.coverage.subagent_transcripts_unavailable": (
            REAPED_IN_WINDOW_HAS_NO_READER
        ),
        "scope.coverage.sessions_with_unavailable_subagents": (
            REAPED_IN_WINDOW_HAS_NO_READER
        ),
        "scope.projects.names_in_window": PROJECT_NAMES_ARE_COUNTS_ON_THE_PAGE,
        "scope.projects.names_in_database": PROJECT_NAMES_ARE_COUNTS_ON_THE_PAGE,
        "scope.projects.unattributed_sources": (
            "#44: `unattributed_calls` is what the band states, because the "
            "reader acts on calls. The source count is the same absence "
            "measured a second way -- another number, no further decision."
        ),
        "durability.sources": (
            "#14: the banner states the archived-source COUNT and the calls it "
            "qualifies; the paths are the user's own directory names, and "
            "/api/summary carries them for anyone who needs the list. #44's "
            "decision about project names, one axis over."
        ),
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-wiring-test-"))
        projects = cls.tmp / "projects"
        projects.mkdir()
        shutil.copy(FIXTURE, projects / "session-fixture.jsonl")
        subagents = projects / "session-fixture" / "subagents"
        subagents.mkdir(parents=True)
        shutil.copy(SUBAGENT_FIXTURE, subagents / "agent-atest1.jsonl")
        ingest(projects, cls.tmp / "usage.db")
        # #93: THE RECOMMENDATION CORPUS TOO, and the walk is why. This class
        # adjudicates the SHAPE of every payload field, and it can only see the
        # fields of a list it finds a row in -- `_walk_rows` refuses an empty
        # one rather than passing over it. The session fixture above holds a
        # handful of calls, which is now correctly BELOW every metric's sample
        # floor, so on its own it produces `ranked: []` and the whole
        # recommendation subtree becomes unwalkable. A second, larger source
        # gives the walk a banded row to read without touching the first, whose
        # scope, coverage and model figures the rest of this class reads.
        ingest(
            build_recommendation_corpus(cls.tmp / "rec"),
            cls.tmp / "usage.db",
            tasks_dir=cls.tmp / "no-task-index",
        )
        cls.api = Api(cls.tmp / "usage.db")
        cls.html = (Path(__file__).resolve().parent.parent / "index.html").read_text()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def _binding_surface(html: str) -> str:
        """Everything in `index.html` that can CONSUME the summary payload.

        The whole page, comments stripped -- a mention in a comment or in a
        prose string must not be able to stand in for a binding, and this
        repository's comments quote the field names at length.
        """
        return strip_comments(html)

    # `_is_bound(surface, field)` used to live here, matching `summary.<field>`
    # for one top-level name. `reads_any` replaces it: same haystack, same
    # optional-chaining, but a PATH rather than a name -- and it is module-level
    # because the walk, the resolver and both allowlist tests all ask the same
    # question, and two spellings of "is this read?" would be free to disagree.

    def payload(self) -> dict:
        return self.api.summary(*day_bounds(None, None))

    def surface(self) -> str:
        return self._binding_surface(self.html)

    def test_every_summary_field_is_rendered_or_declared_unrendered(self) -> None:
        unwired = walk_payload(self.payload(), self.surface(), self.NOT_RENDERED)
        self.assertEqual(
            unwired,
            [],
            f"/api/summary computes {unwired} and index.html never reads them. "
            "Render the field, or add the PATH to NOT_RENDERED with the reason.",
        )

    def test_the_walk_goes_below_the_top_level(self) -> None:
        # THE regression #76 exists for, pinned as a test rather than left to a
        # one-off mutation: `context.utilisation.by_scope` is nested three deep
        # under a top-level key that IS bound, which is exactly why the old
        # `for k in payload` reported a clean page while nothing rendered it.
        #
        # The page is mutated here, not the guard, so this stays red for the
        # real reason if the walk is ever narrowed back to the top level.
        surface = self.surface().replace("summary.context.utilisation.by_scope", "")
        self.assertIn(
            "context.utilisation.by_scope",
            walk_payload(self.payload(), surface, self.NOT_RENDERED),
            "a nested field with no consumer does not turn this guard red -- "
            "which is the state PR #71 shipped in",
        )

    def test_the_walk_reports_a_nested_field_by_its_whole_path(self) -> None:
        # The path is the name: two `calls` at two depths are two fields, and a
        # report that said only "calls" would send the reader to the wrong one.
        unwired = walk_payload(
            {"context": {"utilisation": {"calls": 1}}},
            "summary.context.utilisation",
            {},
        )
        self.assertEqual(unwired, ["context.utilisation.calls"])

    def test_an_array_of_objects_is_walked_through_not_stepped_over(self) -> None:
        # `by_scope` is a LIST. A walk that descended only into dicts would
        # leave the exact shape that caused #76 uncovered, so this pins the
        # element's own fields -- one rendered, one not, in one payload, so a
        # walk that reported everything or nothing cannot pass.
        surface = '<template x-for="s in summary.rows"><span x-text="s.shown">'
        self.assertEqual(
            walk_payload({"rows": [{"shown": 1, "dark": 2}]}, surface, {}),
            ["rows[].dark"],
        )

    def test_a_field_read_only_through_an_array_callback_counts(self) -> None:
        # #89's hop, with teeth. The dial cannot use `x-for`, so its segments
        # are read inside `.filter`/`.map` callbacks -- directly and one
        # function call away. Both spellings are iterations and both must
        # count, or the walk reports every drawn field as unread.
        direct = '<path :d="summary.rows.map(s => arc(s.start))">'
        self.assertEqual(walk_payload({"rows": [{"start": 1}]}, direct, {}), [])
        hop = (
            '<path :d="arcsFor(summary.rows, \'ok\')">'
            "function arcsFor(segments, tone) {"
            " return segments.filter(s => s.tone === tone)"
            ".map(s => arc(s.start)).join(' '); }"
        )
        self.assertEqual(
            walk_payload({"rows": [{"start": 1, "tone": "ok"}]}, hop, {}), []
        )

    def test_a_function_that_stops_iterating_its_argument_turns_red(self) -> None:
        # The mutation: the hop is FOLLOWED, not excused. A helper handed the
        # list that no longer loops over it draws nothing, and the walk must
        # say so rather than credit the call site.
        dead = (
            '<path :d="arcsFor(summary.rows, \'ok\')">'
            "function arcsFor(segments, tone) { return tone; }"
        )
        self.assertEqual(walk_payload({"rows": [{"start": 1}]}, dead, {}), ["rows[]"])

    def test_the_hop_is_positional_and_not_merely_a_mention(self) -> None:
        # A list passed in the WRONG slot is not the parameter the body loops
        # over, so it must not be credited: `arcsFor(tone, summary.rows)` reads
        # the second argument, and the body iterates the first.
        wrong = (
            '<path :d="arcsFor(\'ok\', summary.rows)">'
            "function arcsFor(segments, tone) {"
            " return segments.map(s => s.start).join(' '); }"
        )
        self.assertEqual(walk_payload({"rows": [{"start": 1}]}, wrong, {}), ["rows[]"])

    def test_a_field_bound_only_inside_a_loop_over_a_nested_list_counts(self) -> None:
        # The ordinary way this page renders a row: the field is never spelled
        # `summary.<path>` anywhere, only against the loop's alias.
        surface = (
            '<template x-for="s in summary.a.rows">'
            '<template x-for="b in s.bands"><span x-text="b.label">'
        )
        self.assertEqual(
            walk_payload({"a": {"rows": [{"bands": [{"label": "x"}]}]}}, surface, {}),
            [],
        )

    def test_a_list_of_objects_nothing_iterates_is_unwired(self) -> None:
        # Naming the list is not rendering it: a reference that never iterates
        # cannot put a single one of its rows on the page.
        self.assertEqual(
            walk_payload({"rows": [{"a": 1}]}, "summary.rows.length", {}),
            ["rows[]"],
        )

    def test_a_mapping_the_page_consumes_whole_reaches_every_member(self) -> None:
        # `contextSpread` renders one line per percentile without naming p10,
        # p25 or any other. The iteration IS the binding, and requiring a
        # per-key one would force the page to spell out a list it deliberately
        # does not know the length of.
        self.assertEqual(
            walk_payload(
                {"percentiles": {"p10": 1, "p99": 2}},
                "Object.entries(this.summary.percentiles)",
                {},
            ),
            [],
        )

    def test_consuming_a_mapping_whole_says_nothing_about_its_parent(self) -> None:
        # The sharpest way this walk could report a clean page: match the bulk
        # read by CONTAINMENT and `Object.entries(summary.percentiles)` marks
        # the whole payload consumed, and every field on it passes at once.
        self.assertEqual(
            walk_payload(
                {"percentiles": {"p10": 1}, "dark": 2},
                "Object.entries(summary.percentiles)",
                {},
            ),
            ["dark"],
        )

    def test_the_walk_refuses_a_shape_it_cannot_read(self) -> None:
        # A traversal that hit an unexpected type and SKIPPED it would be this
        # very defect in miniature -- a check reporting a clean answer over a
        # set it never looked at.
        for node, why in (
            ({"odd": {1, 2}}, "a value of an unforeseen type"),
            ({"mixed": [{"a": 1}, "scalar"]}, "objects mixed with scalars"),
            ({"empty": []}, "a list the fixture leaves empty"),
        ):
            with self.subTest(shape=why):
                with self.assertRaises(UnwalkablePayload):
                    walk_payload(node, "summary.odd summary.mixed summary.empty", {})

    def test_the_allowlist_cannot_exempt_a_field_that_is_rendered(self) -> None:
        # The other way an allowlist rots, and the one that had actually
        # happened: four fields were exempted as "plotted per-day via
        # /api/timeseries" while the summary cards rendered them. An entry that
        # describes the page wrongly is worse than no entry -- it asserts a
        # decision nobody made and hides the field from the check above.
        #
        # By PATH since #76, and it must resolve THROUGH lists: an exemption on
        # `x.rows[].field` is checked against the loop alias that renders it,
        # not against a name nothing on this page ever spells.
        surface = self.surface()
        payload = self.payload()
        wrong = sorted(
            path
            for path in self.NOT_RENDERED
            if reads_any(surface, locate_path(payload, surface, path)[1])
        )
        self.assertEqual(
            wrong,
            [],
            f"NOT_RENDERED claims {wrong} are not shown, but index.html binds "
            "them. Drop the entry.",
        )

    def test_the_allowlist_cannot_hide_a_field_that_no_longer_exists(self) -> None:
        # A stale exemption is how an allowlist rots into a rubber stamp: the
        # field is renamed, the entry stays, and the NEW name is unguarded
        # while the list still looks deliberate. A path makes that failure
        # sharper AND likelier -- `scope.main_thread.input` stops resolving if
        # any of three names changes -- so the resolution is checked at every
        # hop and an unresolved path is UNRESOLVED, never a plausible node.
        surface = self.surface()
        payload = self.payload()
        stale = sorted(
            path
            for path in self.NOT_RENDERED
            if locate_path(payload, surface, path)[0] is UNRESOLVED
        )
        self.assertEqual(stale, [], f"NOT_RENDERED names absent paths: {stale}")

    def test_a_stale_exemption_fails_rather_than_passing_quietly(self) -> None:
        # Teeth on the test above: every hop of the path is resolved, so a
        # renamed leaf, a renamed parent and a list that stopped being one all
        # fail. Each of these would otherwise be an entry that guards nothing
        # while the list still looks deliberate.
        payload = {"scope": {"main_thread": {"input": 1}}, "rows": [{"a": 1}]}
        for path in (
            "scope.main_thread.renamed",
            "scope.renamed.input",
            "renamed.main_thread.input",
            "scope[].main_thread",
        ):
            with self.subTest(path=path):
                self.assertIs(locate_path(payload, "", path)[0], UNRESOLVED)
        self.assertEqual(locate_path(payload, "", "scope.main_thread.input")[0], 1)

    def test_every_exemption_cites_the_decision_it_records(self) -> None:
        # "An entry must name the reason" was a comment; this is the check. A
        # bare "not needed" is how a register of decisions becomes a list of
        # shrugs, so every entry names the issue whose decision it records.
        for path, reason in sorted(self.NOT_RENDERED.items()):
            with self.subTest(path=path):
                self.assertRegex(
                    reason,
                    r"#\d+",
                    f"{path} is exempted without citing the decision it records",
                )
                self.assertGreater(len(reason), 60, f"{path}: not a reason")

    # How many entries the register is DECLARED to hold. Twenty-one was the
    # count #76 found; 15 since #65, which gave the six per-scope context
    # statistics (#82 group 1) a reader. They were consumed, not deleted -- the
    # scoped meters needed exactly those figures, which is why #82 sequenced
    # them here rather than removing a correct measurement.
    EXPECTED_NOT_RENDERED = 15

    def test_moving_a_field_between_views_does_not_widen_the_register(self) -> None:
        # #70 splits the page into an overview and a detail view. A field that
        # MOVES between them is still rendered, so the register must not grow
        # to cover one -- an entry added by a layout change would be a field
        # quietly dropped from the report while the allowlist made it look
        # decided. The healthy direction is down.
        self.assertLessEqual(
            len(self.NOT_RENDERED),
            self.EXPECTED_NOT_RENDERED,
            "a field that stopped being rendered was exempted rather than "
            "re-homed. Moving a panel between views does not orphan a field.",
        )

    def test_the_declared_register_size_is_the_register_size(self) -> None:
        # TEETH ON THE CEILING ITSELF. `assertLessEqual` against a literal
        # leaves that literal free to be raised back, and nothing would notice:
        # loosening it to 21 after #65 emptied six slots would re-open exactly
        # the six the last change closed, and the whole suite would stay green
        # because the register really is under 21.
        #
        # So the ceiling is compared to the truth as well as the truth to the
        # ceiling. Removing an entry legitimately costs one edit to a line that
        # states what the register holds, which is the deliberate act this
        # register is for -- an allowlist whose size nobody restates is how it
        # rots into a rubber stamp.
        self.assertEqual(
            len(self.NOT_RENDERED),
            self.EXPECTED_NOT_RENDERED,
            "the register's size and the size it declares disagree; the "
            "ceiling is not a budget to spend",
        )

    def test_the_context_block_owes_no_exemption_at_all(self) -> None:
        # #61 and #76 are one change on purpose: the guard that starts seeing
        # nested fields sees #61's unrendered ones first. Every field under
        # `context` -- including `by_scope`, every scope's `no_sample_reason`
        # and every band's boundaries -- is rendered, so none of them is
        # declared here. An entry appearing under this prefix means the render
        # half regressed and was papered over.
        under_context = sorted(
            path for path in self.NOT_RENDERED if path.startswith("context")
        )
        self.assertEqual(under_context, [])


# --- #10: peak context is not spend --------------------------------------
#
# Fixture design (CLAUDE.md, "a fixture must not make the defect
# undetectable"). Every token class of every call carries a DIFFERENT value,
# and the two `backend-expert` dispatches carry deliberately unequal peaks so
# that SUM (70,000) and MAX (40,000) cannot be confused. `pk1`'s peak call is
# its THIRD of four, so an implementation that reads "the last call's context"
# instead of the maximum is red too. Ordering has teeth as well: `reviewer`
# has the largest self-reported peak (480,000) and NO measured spend at all,
# so the old `ORDER BY SUM(subagent_tokens) DESC` ranked it first while spend
# ranks it last.
#
# The self-reported/peak ratios are pinned near the corpus median: pk1
# 40000/39840 = 1.0040, pk2 30000/29940 = 1.0020 (measured corpus median
# 1.004 -- see the comment in serve.py:session_detail).
PEAK_SESSION = "peak-fixture"
# agent id (== <task-id>) -> (input, cache_write, cache_read, output) per call.
PEAK_SUBAGENT_CALLS = {
    "pk1": [
        (1000, 2000, 3000, 400),      # ctx  6,000   spend  6,400
        (1100, 2100, 16000, 500),     # ctx 19,200   spend 19,700
        (1200, 2400, 36240, 600),     # ctx 39,840   spend 40,440  <- PEAK
        (1300, 2500, 30000, 700),     # ctx 33,800   spend 34,500
    ],
    "pk2": [
        (900, 1800, 2700, 300),       # ctx  5,400   spend  5,700
        (940, 1900, 27100, 350),      # ctx 29,940   spend 30,290  <- PEAK
    ],
    "pk3": [
        (510, 620, 730, 840),         # ctx  1,860   spend  2,700
    ],
}
PK1_PEAK_CONTEXT = 39840
PK1_SPEND = 101040
PK2_PEAK_CONTEXT = 29940
PK2_SPEND = 35990
PK3_SPEND = 2700
# task_id, tool_use_id, agent type, <subagent_tokens> value (None = no tag).
PEAK_DISPATCHES = [
    ("pk1", "toolu_A", "backend-expert", 40000),
    ("pk2", "toolu_B", "backend-expert", 30000),
    ("pk3", "toolu_C", "architect", None),        # no tag: NO SAMPLE, not 0
    ("pk4", "toolu_D", "reviewer", 480000),       # no transcript: spend UNMEASURED
]


def build_peak_context_corpus(root: Path) -> Path:
    """A session that dispatches four agents, three of which left transcripts."""
    projects = root / "projects"
    subagents = projects / PEAK_SESSION / "subagents"
    subagents.mkdir(parents=True)

    def rec(obj: dict) -> str:
        return json.dumps(obj) + "\n"

    lines = [
        rec({"type": "user", "sessionId": PEAK_SESSION,
             "timestamp": "2026-02-10T10:00:00.000Z",
             "message": {"role": "user", "content": "Delegate four tasks"}}),
        rec({"type": "assistant", "sessionId": PEAK_SESSION,
             "timestamp": "2026-02-10T10:00:05.000Z", "isSidechain": False,
             "message": {
                 "id": "msg_peak_main",
                 "model": "claude-sonnet-5-20260115",
                 "usage": {"input_tokens": 11, "cache_creation_input_tokens": 22,
                           "cache_read_input_tokens": 33, "output_tokens": 44},
                 "content": [
                     {"type": "tool_use", "id": tool_use_id, "name": "Agent",
                      "input": {"subagent_type": agent_type,
                                "description": f"work for {task_id}"}}
                     for task_id, tool_use_id, agent_type, _ in PEAK_DISPATCHES
                 ],
             }}),
    ]
    for i, (task_id, tool_use_id, _, reported) in enumerate(PEAK_DISPATCHES):
        usage = (
            f"\n<usage><subagent_tokens>{reported}</subagent_tokens>"
            "<tool_uses>3</tool_uses></usage>"
            if reported is not None else ""
        )
        lines.append(rec({
            "type": "user", "sessionId": PEAK_SESSION,
            "timestamp": f"2026-02-10T10:1{i}:00.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": (
                f"<task-notification>\n<task-id>{task_id}</task-id>\n"
                f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
                f"<status>completed</status>\n<result>Done.</result>{usage}\n"
                "</task-notification>"
            )}]},
        }))
    (projects / f"{PEAK_SESSION}.jsonl").write_text("".join(lines))

    for agent_id, calls in PEAK_SUBAGENT_CALLS.items():
        (subagents / f"agent-{agent_id}.jsonl").write_text("".join(
            rec({"type": "assistant", "sessionId": PEAK_SESSION,
                 "agentId": f"agent-{agent_id}",
                 "timestamp": f"2026-02-10T10:2{n}:00.000Z", "isSidechain": True,
                 "message": {
                     "id": f"msg_{agent_id}_{n}",
                     "model": "claude-sonnet-5-20260115",
                     "usage": {"input_tokens": inp,
                               "cache_creation_input_tokens": cw,
                               "cache_read_input_tokens": cr,
                               "output_tokens": out},
                     "content": [{"type": "text", "text": f"{agent_id} step {n}"}],
                 }})
            for n, (inp, cw, cr, out) in enumerate(calls)
        ))
    return projects


class AgentDispatchPeakContextTest(unittest.TestCase):
    """#10: `<subagent_tokens>` is PEAK CONTEXT, and was rendered as spend.

    Measured 2026-08-02 over 1,774 dispatches carrying both figures: the tag
    divided by `MAX(api_calls.context_size)` for the same agent has median
    1.004 (98.1% within +-5% of 1.0), while the same tag divided by
    transcript-measured tokens has median 0.011 -- measured spend is 51.6x
    larger. The session-detail panel SUMMED the tag and titled the column
    "Agent dispatch spend", understating dispatch spend by ~50x at the median.

    These tests pin all three halves of the fix: the aggregate (MAX, never
    SUM), the naming (`peak_context_tokens`), and the second, genuinely
    different quantity beside it (cumulative measured spend).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-peak-context-test-"))
        projects = build_peak_context_corpus(cls.tmp)
        ingest(projects, cls.tmp / "usage.db")
        cls.api = Api(cls.tmp / "usage.db")
        cls.html = (Path(__file__).resolve().parent.parent / "index.html").read_text()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def rows(self) -> dict[str, dict]:
        detail = self.api.session_detail(PEAK_SESSION)
        return {r["agent_type"]: r for r in detail["agent_types"]}

    def test_fixture_ingested_the_shape_these_tests_assume(self) -> None:
        # If the corpus did not land, every assertion below is vacuous.
        rows = self.rows()
        self.assertEqual(
            {k: v["dispatches"] for k, v in rows.items()},
            {"backend-expert": 2, "architect": 1, "reviewer": 1},
        )

    def test_peak_context_is_the_max_dispatch_not_the_sum(self) -> None:
        # THE defect. SUM over the two backend-expert dispatches is 70,000 --
        # a total of two high-water marks, which is not a quantity.
        row = self.rows()["backend-expert"]
        self.assertEqual(row["peak_context_tokens"], 40000)

    def test_api_names_the_field_peak_context_not_subagent_tokens(self) -> None:
        for row in self.rows().values():
            self.assertIn("peak_context_tokens", row)
            self.assertNotIn("subagent_tokens", row)

    def test_reported_peak_tracks_max_context_size_not_measured_spend(self) -> None:
        # What this pins: CPB's READING of the tag, against a fixture built to
        # the measured relationship. `<subagent_tokens>` tracks
        # MAX(context_size) and emphatically not the spend beside it, so an
        # implementation that swapped the two, summed instead of maxing, or
        # took the last call's context is red -- pk1's peak is its THIRD call
        # of four. Ratios come from the DB so the relationship is pinned at the
        # source, not at the API's rounding.
        #
        # What it CANNOT pin, despite the shape of a canary: a harness change.
        # Both sides of every ratio are written by THIS file -- the tag values
        # from PEAK_DISPATCHES, the contexts from PEAK_SUBAGENT_CALLS -- so a
        # Claude Code release that changed what the tag counts would move
        # neither, and the fixture and its expectations would go on agreeing.
        # Only re-measuring the tag against a real corpus catches that (the
        # 1,774-dispatch scan in this class's docstring is that measurement,
        # and it has a date for the same reason).
        rows = self.api.conn.execute(
            "SELECT d.task_id, d.subagent_tokens reported,"
            " (SELECT MAX(a.context_size) FROM api_calls a"
            "    WHERE a.agent_id = d.task_id) peak,"
            " (SELECT SUM(a.input_tokens + a.cache_read + a.cache_write"
            "             + a.output_tokens) FROM api_calls a"
            "    WHERE a.agent_id = d.task_id) spend"
            " FROM agent_dispatches d"
            " WHERE d.session_id = ? AND d.subagent_tokens IS NOT NULL",
            (PEAK_SESSION,),
        ).fetchall()
        pinned = {r["task_id"]: r for r in rows if r["peak"] is not None}
        self.assertEqual(set(pinned), {"pk1", "pk2"})
        self.assertEqual(pinned["pk1"]["peak"], PK1_PEAK_CONTEXT)
        self.assertEqual(pinned["pk2"]["peak"], PK2_PEAK_CONTEXT)
        for task_id, r in pinned.items():
            ratio = r["reported"] / r["peak"]
            self.assertAlmostEqual(
                ratio, 1.0, delta=0.05,
                msg=f"{task_id}: <subagent_tokens>/MAX(context_size) = {ratio}; "
                    "the tag is a peak-context high-water mark (98.1% of 1,774 "
                    "real dispatches land within +-5%, measured 2026-08-02)",
            )
            # ...and it is emphatically NOT the spend figure beside it.
            self.assertLess(r["reported"] / r["spend"], 0.9)

    def test_measured_spend_is_reported_beside_the_peak(self) -> None:
        row = self.rows()["backend-expert"]
        self.assertEqual(row["measured_tokens"], PK1_SPEND + PK2_SPEND)

    def test_a_dispatch_with_no_tag_has_no_peak_sample_not_a_zero(self) -> None:
        # architect's one dispatch emitted no <subagent_tokens>: absence must
        # stay absent (CLAUDE.md), and it must not drag the spend column --
        # which IS measured for that dispatch -- down with it.
        row = self.rows()["architect"]
        self.assertIsNone(row["peak_context_tokens"])
        self.assertEqual(row["dispatches_without_peak"], 1)
        self.assertEqual(row["measured_tokens"], PK3_SPEND)

    def test_a_dispatch_with_no_transcript_has_no_spend_sample(self) -> None:
        # reviewer reported a 480,000-token peak and left no transcript: its
        # spend is UNMEASURED, which must never render as 0 (a free dispatch).
        row = self.rows()["reviewer"]
        self.assertEqual(row["peak_context_tokens"], 480000)
        self.assertIsNone(row["measured_tokens"])
        self.assertEqual(row["dispatches_without_spend"], 1)

    def test_rows_rank_by_measured_spend_with_unmeasured_last(self) -> None:
        # Ranking by the self-reported peak put `reviewer` (480,000 reported,
        # zero measured spend) at the top of a panel a reader consults to see
        # what delegation cost.
        #
        # What this proves and what it does NOT (#37): the returned ORDER is
        # pinned, and the property that an unmeasured row never precedes a
        # measured one is stated rather than left to the reader of a literal
        # list. It cannot prove the `measured_tokens IS NULL` sort key is
        # present -- SQLite collates NULL last under DESC anyway, so deleting
        # that key changes no row of any fixture. The key is pinned on the
        # executed statement in the test below instead.
        detail = self.api.session_detail(PEAK_SESSION)
        rows = detail["agent_types"]
        self.assertEqual(
            [r["agent_type"] for r in rows],
            ["backend-expert", "architect", "reviewer"],
        )
        measured = [r["measured_tokens"] is not None for r in rows]
        self.assertEqual(
            measured,
            sorted(measured, reverse=True),
            "an unmeasured dispatch was interleaved with the measured ones",
        )
        spends = [r["measured_tokens"] for r in rows if r["measured_tokens"] is not None]
        self.assertEqual(spends, sorted(spends, reverse=True))

    def test_unmeasured_last_is_stated_in_the_query_not_left_to_the_engine(
        self,
    ) -> None:
        """The NULL-ordering key is asserted on the statement that ran (#37).

        This one is deliberately structural, and the reason is the finding: the
        behavioural test above passes with `measured_tokens IS NULL` DELETED,
        because SQLite already sorts NULL last under `DESC`. No fixture can
        make that mutation red -- the two orderings are identical for every
        possible input on this engine -- so a behavioural assertion here would
        be pinning an incidental property of the engine while reading as a
        guarantee about the panel. That is the failure this issue is about.

        What the explicit key buys is what the test therefore states: the rule
        "unmeasured ranks last" is expressed by the query and survives the
        column direction being flipped, the engine changing its NULL
        collation, or the ordering moving into Python. It is captured from the
        connection's trace callback rather than grepped out of `serve.py`, so
        an ORDER BY that is edited but never executed cannot satisfy it.
        """
        executed: list[str] = []
        self.api.conn.set_trace_callback(executed.append)
        try:
            self.api.session_detail(PEAK_SESSION)
        finally:
            self.api.conn.set_trace_callback(None)
        ranked = [s for s in executed if "FROM agent_dispatches d" in s]
        self.assertEqual(
            len(ranked), 1, "expected exactly one agent-type ranking query"
        )
        order_by = ranked[0].split("ORDER BY", 1)[1].strip()
        self.assertTrue(
            order_by.startswith("measured_tokens IS NULL"),
            "the FIRST sort key must be the presence of a measurement, not the "
            f"measurement itself; ORDER BY was: {order_by}",
        )

    def test_the_page_renders_peak_context_and_no_longer_calls_it_spend(self) -> None:
        # The API being right while the page still says "spend" is the whole
        # defect, so the label is asserted too (same posture as
        # SummaryPayloadIsWiredTest).
        self.assertIn("r.peak_context_tokens", self.html)
        self.assertIn("r.measured_tokens", self.html)
        # The page must no longer READ the old field. The literal string may
        # still appear -- the column tooltip names the `<subagent_tokens>` tag
        # it is explaining -- so this is asserted on the binding, not the text.
        self.assertNotIn("r.subagent_tokens", self.html)
        self.assertNotIn("Agent dispatch spend", self.html)
        self.assertIn("Peak context", self.html)


# --- #30: the panel ranks by the quantity its heading names ----------------
#
# Fixture design (CLAUDE.md, "a fixture must not make the defect
# undetectable"). THREE orderings are pinned mutually different, so no
# leftover sort can pass by looking right:
#
#   agent      model         input   cache_read  cache_write  output    total
#   rkout      haiku        11,000      400,000       13,000  600,000  1,024,000
#   rkcache    haiku         5,000      900,000        7,000    8,000    920,000
#   rkopus     opus-4        1,000        2,000        3,000    4,000     10,000
#   rkmixed    sonnet+opus     110          220          330      440      1,100
#
#   by total tokens (the new key):  rkout, rkcache, rkopus, rkmixed
#   by cache_read (the OLD query):  rkcache, rkout, rkopus, rkmixed
#   by list-rate cost (the old HEADING's claim, output- and tier-weighted):
#                                   rkout, rkopus, rkcache, rkmixed
#
# The top two swap between the token order and the cache-read order, and
# rkopus/rkcache swap between the token order and any tier-weighted price
# order. `rkcache` is deliberately the cache-read leader while `rkout` is the
# token leader, which is exactly the pair a cache-read sort gets wrong.
RANK_SESSION = "rank-fixture"
# The Eastern day every fixture call is timestamped into (09:1x UTC on
# 2026-03-04 -> 04:1x Eastern, same date). A reaped run's `dispatched_at`
# comes from the task index's mtime instead -- i.e. NOW -- so the two are
# deliberately different days, which is what makes the window tests bite.
RANK_CALL_DAY = "2026-03-04"
RANK_AGENTS: dict[str, list[tuple[str, int, int, int, int]]] = {
    # agent_id -> [(model, input, cache_read, cache_write, output), ...]
    "rkout": [("claude-haiku-4-5", 11_000, 400_000, 13_000, 600_000)],
    "rkcache": [
        ("claude-haiku-4-5", 2_000, 400_000, 3_000, 3_000),
        ("claude-haiku-4-5", 3_000, 500_000, 4_000, 5_000),
    ],
    "rkopus": [("claude-opus-4-8", 1_000, 2_000, 3_000, 4_000)],
    "rkmixed": [
        ("claude-sonnet-5-20260115", 100, 200, 300, 400),
        ("claude-opus-5-20260201", 10, 20, 30, 40),
    ],
}
RANK_TOTALS = {
    "rkout": 1_024_000,
    "rkcache": 920_000,
    "rkopus": 10_000,
    "rkmixed": 1_100,
}
RANK_BY_TOTAL_TOKENS = ["rkout", "rkcache", "rkopus", "rkmixed"]
RANK_BY_CACHE_READ = ["rkcache", "rkout", "rkopus", "rkmixed"]

# A FIFTH dispatch, and the one the panel's whole epistemics turn on: reaped
# AFTER it was ingested (#41).
#
# `rkgone` above is reaped-BEFORE-ingest -- no transcript ever landed, so its
# SQL `total_tokens` is already NULL, NULL collates last under DESC anyway,
# and the Python null-ing is a no-op on a value that is already None. Any
# assertion made only against `rkgone` therefore passes without the code doing
# anything, which is exactly the "a fixture must not make the defect
# undetectable" failure in CLAUDE.md.
#
# `rkarch` is the other, load-bearing half of the durability story: ingested
# on one run, its transcript reaped before the next. `store_subagent_runs()`
# rebuilds its row as `unavailable` from the task index, while the archive
# path KEEPS its `api_calls` rows (CLAUDE.md, "Durability -- the DB is not
# regenerable"). So its raw SQL sort key is 9,003,000, NOT NULL -- and one
# status was carrying two facts:
#
#   * `rkgone` is UNMEASURED: no rows exist, nothing can be shown, and a 0
#     would file it among the smallest dispatches;
#   * `rkarch` is MEASURED AND IRREPLACEABLE: the rows exist and this database
#     is now their only copy. Blanking them rendered a present measurement as
#     absence -- the project's own rule pointed the wrong way -- and put
#     9,003,000 tokens in the summary cards and seven dashes in the panel that
#     ranks by exactly that quantity.
#
# Its total is ~8.8x the measured leader's so the ordering property bites at
# the very top of the list, and its four token classes are pinned mutually
# different so a swapped column mapping cannot pass either.
RANK_REAPED = "rkarch"
RANK_REAPED_MODEL = "claude-opus-4-8"
RANK_REAPED_CALLS = [(RANK_REAPED_MODEL, 3_000_000, 4_000_000, 2_000_000, 3_000)]
RANK_REAPED_TOTAL = 9_003_000
# The panel's order once the reaped-but-measured run ranks on the total it
# still has: 9,003,000 outranks every measured row, and only the run with NO
# measurement at all is held back to the end.
RANK_BY_TOTAL_WITH_ARCHIVED = [RANK_REAPED] + RANK_BY_TOTAL_TOKENS + ["rkgone"]
# Fields of an `/api/agents` row that identify the dispatch rather than
# measure it. Spelled out HERE, independently of `serve.py`, so that a run we
# hold no calls for can be asserted null in EVERY other field -- including any
# field added later. A future measured column that forgets the blanking goes
# red here rather than shipping a fabricated figure.
AGENT_IDENTITY_KEYS = frozenset({
    "agent_id", "agent_type", "description", "spawn_depth",
    "dispatching_session_id", "storing_session_id", "session_id", "status",
})
# Every agent that gets a real transcript written, measured or later reaped.
RANK_TRANSCRIPTS = {**RANK_AGENTS, RANK_REAPED: RANK_REAPED_CALLS}


def build_ranking_corpus(root: Path) -> tuple[Path, Path]:
    """A session dispatching four measured agents and two reaped ones.

    Reaping is a two-step story, so this only lays down the corpus in its
    pre-reap state: the caller ingests, calls `reap_ingested_transcript()`,
    and ingests again.
    """
    projects = root / "projects"
    subagents = projects / RANK_SESSION / "subagents"
    subagents.mkdir(parents=True)

    def rec(obj: dict) -> str:
        return json.dumps(obj) + "\n"

    (projects / f"{RANK_SESSION}.jsonl").write_text(rec({
        "type": "user", "sessionId": RANK_SESSION,
        "timestamp": "2026-03-04T09:00:00.000Z",
        "message": {"role": "user", "content": "Delegate"},
    }))
    for agent_id, calls in RANK_TRANSCRIPTS.items():
        (subagents / f"agent-{agent_id}.jsonl").write_text("".join(
            rec({
                "type": "assistant", "sessionId": RANK_SESSION,
                "agentId": f"agent-{agent_id}",
                "timestamp": f"2026-03-04T09:1{n}:00.000Z", "isSidechain": True,
                "message": {
                    "id": f"msg_{agent_id}_{n}",
                    "model": model,
                    "usage": {"input_tokens": inp,
                              "cache_creation_input_tokens": cw,
                              "cache_read_input_tokens": cr,
                              "output_tokens": out},
                    "content": [{"type": "text", "text": f"{agent_id} step {n}"}],
                },
            })
            for n, (model, inp, cr, cw, out) in enumerate(calls)
        ))
        (subagents / f"agent-{agent_id}.meta.json").write_text(json.dumps({
            "agentType": f"type-{agent_id}", "description": f"work by {agent_id}",
        }))

    tasks = root / "tasks"
    index = tasks / RANK_SESSION / "tasks"
    index.mkdir(parents=True)
    for agent_id in RANK_TRANSCRIPTS:
        (index / f"{agent_id}.output").symlink_to(
            subagents / f"agent-{agent_id}.jsonl"
        )
    # A dispatch whose transcript is gone: its spend is UNMEASURED, and the
    # ranking must not read that as the cheapest agent.
    (index / "rkgone.output").symlink_to(subagents / "agent-rkgone.jsonl")
    return projects, tasks


def reap_ingested_transcript(projects: Path) -> None:
    """Delete `RANK_REAPED`'s transcript, leaving its task-index entry behind.

    What Claude Code's `cleanupPeriodDays` does between two ingests: the
    canonical transcript goes, the index symlink stays and dangles. Only the
    subagent's own files are touched -- the sidecar goes with it, because a
    reap takes the directory entry, not just the JSONL.
    """
    subagents = projects / RANK_SESSION / "subagents"
    (subagents / f"agent-{RANK_REAPED}.jsonl").unlink()
    (subagents / f"agent-{RANK_REAPED}.meta.json").unlink()


class AgentRankingByTotalTokensTest(unittest.TestCase):
    """The panel ranks by the quantity its heading names (#30).

    "Top subagent dispatches (by spend)" ordered by `cache_read DESC` for its
    whole life -- the heading claimed a ranking the query never computed.
    Measured over a local transcript corpus on 2026-08-05 (48 main-thread
    sessions, 2,891 transcripts), only 4 of the 10 dispatches shown were among
    the 10 most expensive; the row displayed seventh was the 343rd. Ranking by
    total tokens reproduces the displayed order almost exactly (one adjacent
    swap in the top 10, because cache reads are ~83% of cache volume), so this
    is a truth-in-labelling fix rather than a behaviour change.

    The durable half is that heading, payload field and `ORDER BY` are now ONE
    quantity, named once in `serve.RANKED_BY`. The defect was never a wrong
    sort -- it was a heading and a query free to disagree with nothing
    asserting they agreed.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-ranking-test-"))
        projects, tasks = build_ranking_corpus(cls.tmp)
        cls.db = cls.tmp / "usage.db"
        # TWO ingests, which is the whole point of `rkarch`: the first measures
        # it, the reap takes its transcript, the second archives the source
        # (rows KEPT) and rebuilds its run row as `unavailable`. One ingest
        # cannot produce a dispatch that is unmeasured and has measurements.
        ingest(projects, cls.db, tasks_dir=tasks)
        reap_ingested_transcript(projects)
        ingest(projects, cls.db, tasks_dir=tasks)
        cls.api = Api(cls.db)
        cls.html = (Path(__file__).resolve().parent.parent / "index.html").read_text()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def rows(self) -> list[dict]:
        return self.api.agents(*day_bounds(None, None), 20)

    def test_fixture_landed_with_the_totals_these_tests_assume(self) -> None:
        # If the corpus did not ingest as designed, every ordering assertion
        # below is vacuous.
        measured = {r["agent_id"]: r for r in self.rows() if r["status"] == "ingested"}
        self.assertEqual(
            {k: v["total_tokens"] for k, v in measured.items()}, RANK_TOTALS
        )

    def test_measured_dispatches_rank_by_total_tokens(self) -> None:
        order = [r["agent_id"] for r in self.rows() if r["status"] == "ingested"]
        self.assertEqual(order, RANK_BY_TOTAL_TOKENS)
        # ...and specifically NOT by the quantity the old query used.
        self.assertNotEqual(order, RANK_BY_CACHE_READ)

    def test_the_returned_order_matches_the_field_the_heading_names(self) -> None:
        # Self-consistency, so a future ORDER BY change on any other column is
        # red even if this fixture's rows happen to be re-arranged. Ranges over
        # every row that HAS a total, whatever its status: the ranking key is
        # the measurement, not the provenance of the transcript behind it.
        totals = [
            r["total_tokens"] for r in self.rows() if r["total_tokens"] is not None
        ]
        self.assertEqual(totals, sorted(totals, reverse=True))

    def test_total_tokens_is_the_four_token_classes_summed(self) -> None:
        for row in self.rows():
            if row["total_tokens"] is None:
                continue
            with self.subTest(agent=row["agent_id"]):
                self.assertEqual(
                    row["total_tokens"],
                    row["input"] + row["cache_read"]
                    + row["cache_write"] + row["output"],
                )

    def test_an_unmeasured_dispatch_ranks_last_with_no_total_at_all(self) -> None:
        # The trap in removing the money column: whatever replaces it as the
        # sort key must not read a reaped dispatch as a 0-token one. It sorts
        # last by the EXPLICIT status key, not by however NULL happens to
        # collate, and its total stays absent.
        rows = self.rows()
        self.assertEqual(rows[-1]["agent_id"], "rkgone")
        self.assertEqual(rows[-1]["status"], "unavailable")
        self.assertIsNone(rows[-1]["total_tokens"])

    # --- the reaped-AFTER-ingest dispatch: measurements outlive the source ---

    def test_fixture_landed_a_reaped_dispatch_that_still_has_its_call_rows(
        self,
    ) -> None:
        # THE guard. Both properties below are vacuous unless the raw sort key
        # `serve.agents()` reads for `rkarch` is a large NUMBER while its
        # status is `unavailable`. `rkgone` cannot establish that -- it has no
        # call rows at all, so its SQL total is NULL and every assertion about
        # NULL-handling passes without the code doing anything.
        status = self.api.conn.execute(
            "SELECT status FROM subagent_runs WHERE agent_id = ?", (RANK_REAPED,)
        ).fetchone()
        self.assertIsNotNone(status, f"{RANK_REAPED} has no subagent_runs row")
        self.assertEqual(status["status"], "unavailable")
        # Archived, not pruned: the rows the first ingest wrote are still here.
        calls, total = self.api.conn.execute(
            "SELECT COUNT(*), SUM(input_tokens + cache_read + cache_write"
            " + output_tokens) FROM api_calls WHERE agent_id = ?",
            (RANK_REAPED,),
        ).fetchone()
        self.assertEqual(calls, len(RANK_REAPED_CALLS))
        self.assertEqual(total, RANK_REAPED_TOTAL)
        # ...and it is bigger than the measured leader, so an ordering that
        # reads it as a number cannot come out looking right by accident.
        self.assertGreater(total, max(RANK_TOTALS.values()))
        # The source really is gone from disk and marked archived.
        archived = self.api.conn.execute(
            "SELECT archived_at FROM ingest_state WHERE path LIKE ?",
            (f"%agent-{RANK_REAPED}.jsonl",),
        ).fetchone()
        self.assertIsNotNone(archived, "the reaped source lost its ingest_state row")
        self.assertIsNotNone(archived["archived_at"])

    def test_a_reaped_dispatch_reports_the_measurements_that_outlived_it(
        self,
    ) -> None:
        # THE defect (#41). `rkarch`'s transcript is gone, but its `api_calls`
        # rows are still here and this database is now their only copy --
        # exactly what the durability design exists to keep. Rendering them as
        # absence discarded a present measurement, and put 9,003,000 tokens in
        # the summary cards and seven dashes in the panel at the same time.
        row = {r["agent_id"]: r for r in self.rows()}[RANK_REAPED]
        model, inp, cache_read, cache_write, output = RANK_REAPED_CALLS[0]
        self.assertEqual(row["total_tokens"], RANK_REAPED_TOTAL)
        # Every class separately, and the fixture pins them mutually different,
        # so a swapped column mapping cannot pass by summing to the same total.
        self.assertEqual(row["input"], inp)
        self.assertEqual(row["cache_read"], cache_read)
        self.assertEqual(row["cache_write"], cache_write)
        self.assertEqual(row["output"], output)
        self.assertEqual(row["calls"], len(RANK_REAPED_CALLS))
        self.assertEqual(row["model"], model)
        self.assertIsNotNone(row["first_ts"])
        self.assertIsNotNone(row["last_ts"])

    def test_the_reaped_states_are_two_statuses_not_one(self) -> None:
        # The distinction itself. "We never measured this" and "we measured it
        # and the source is now gone" are different claims about the same
        # missing file, and one status value cannot carry both. Derived from
        # the evidence and nothing else: `rkarch` has surviving call rows,
        # `rkgone` has none.
        by_id = {r["agent_id"]: r for r in self.rows()}
        self.assertEqual(by_id[RANK_REAPED]["status"], STATUS_ARCHIVED)
        self.assertEqual(by_id["rkgone"]["status"], "unavailable")
        self.assertNotEqual(STATUS_ARCHIVED, "unavailable")
        self.assertNotEqual(STATUS_ARCHIVED, "ingested")

    def test_a_never_measured_dispatch_is_absent_in_every_measured_field(
        self,
    ) -> None:
        # The other direction, which the fix must not break: `rkgone`'s
        # transcript was reaped before CPB ever read it, so there is nothing to
        # show and a 0 would file it among the smallest dispatches. Asserted
        # over EVERY non-identifying key rather than a hand-kept list, so a
        # measured column added later cannot quietly escape the blanking.
        row = {r["agent_id"]: r for r in self.rows()}["rkgone"]
        measured = sorted(set(row) - AGENT_IDENTITY_KEYS)
        self.assertIn("total_tokens", measured, "the ranking key must be covered")
        for key in measured:
            with self.subTest(field=key):
                self.assertIsNone(row[key])

    def test_a_reaped_dispatch_ranks_on_the_total_that_survived_it(self) -> None:
        # Behaviour change, stated deliberately. The status used to be the
        # FIRST sort key, so `rkarch` sorted last however large its surviving
        # total. That was right while the total was blanked and wrong once it
        # is shown: a panel ranked by total tokens that files its largest
        # measured dispatch at the bottom is not ranked by total tokens.
        #
        # What still holds is the property the status key was protecting: a run
        # with NO measurement never sorts among the measured ones, because it
        # has no key to sort by. That is now keyed on the evidence -- rows or
        # no rows -- rather than on a status that meant both.
        rows = self.rows()
        self.assertEqual(
            [r["agent_id"] for r in rows], RANK_BY_TOTAL_WITH_ARCHIVED
        )
        self.assertEqual(rows[0]["agent_id"], RANK_REAPED)
        has_total = [r["total_tokens"] is not None for r in rows]
        self.assertEqual(
            has_total, sorted(has_total, reverse=True),
            "a dispatch with no measurement was interleaved with the measured ones",
        )

    def test_the_panel_and_the_summary_cards_agree_over_the_same_window(
        self,
    ) -> None:
        # The reader-visible symptom, asserted end to end: every subagent token
        # the cards count is attributed to a dispatch in the panel. It diverged
        # by exactly `rkarch`'s 9,003,000 -- one database, one window, two
        # answers -- and the panel was the one under-reporting.
        #
        # Asserted over BOTH windows because the divergence had two independent
        # mechanisms of the same size: the blanking (corpus-wide) and the
        # dispatch-date filter (the day the calls actually fall in).
        for label, rng in (("corpus-wide", (None, None)),
                           ("the call day", (RANK_CALL_DAY, RANK_CALL_DAY))):
            with self.subTest(window=label):
                start, end = day_bounds(*rng)
                rows = self.api.agents(start, end, 20)
                bucket = self.api.summary(start, end)["scope"]["subagent"]
                card_total = sum(
                    bucket[k]
                    for k in ("input", "cache_read", "cache_write", "output")
                )
                panel_total = sum(
                    r["total_tokens"] for r in rows if r["total_tokens"] is not None
                )
                self.assertEqual(panel_total, card_total)
                self.assertEqual(
                    sum(r["calls"] for r in rows if r["calls"] is not None),
                    bucket["calls"],
                )

    def test_a_window_that_holds_the_measurements_lists_the_dispatch(self) -> None:
        # The second mechanism, worth the same 9,003,000. A reaped run's only
        # timestamp is `dispatched_at` -- the mtime of its task-index entry,
        # which is when that entry was last touched, not when the agent ran.
        # Gating the ROW on it dropped `rkarch` out of the very window its
        # measurements fall in (panel 1,955,100 vs cards 10,958,100 for
        # 2026-03-04, measured 2026-08-05) while a corpus-wide view agreed.
        #
        # A dispatch belongs in this window if we hold its measurements here OR
        # its dispatch is dated here. The first is the fact the panel exists to
        # report and cannot be conditioned on the second.
        start, end = day_bounds(RANK_CALL_DAY, RANK_CALL_DAY)
        by_id = {r["agent_id"]: r for r in self.api.agents(start, end, 20)}
        self.assertIn(RANK_REAPED, by_id)
        self.assertEqual(by_id[RANK_REAPED]["total_tokens"], RANK_REAPED_TOTAL)
        self.assertEqual(by_id[RANK_REAPED]["status"], STATUS_ARCHIVED)
        # And the gate still holds in the other direction: a run with NO
        # measurement here and no dispatch dated here is not this window's gap,
        # so it is absent from the list rather than present as a row of dashes.
        self.assertNotIn("rkgone", by_id)

    def test_the_model_is_carried_beside_the_ranking(self) -> None:
        by_id = {r["agent_id"]: r for r in self.rows()}
        self.assertEqual(by_id["rkout"]["model"], "claude-haiku-4-5")
        self.assertEqual(by_id["rkopus"]["model"], "claude-opus-4-8")
        # A run spanning two models must not be attributed to ONE of them --
        # and an unmeasured one to none at all (the page renders null as "—").
        self.assertEqual(by_id["rkmixed"]["model"], "2 models")
        self.assertIsNone(by_id["rkgone"]["model"])

    def test_the_heading_names_the_ordering_key(self) -> None:
        self.assertIn(f"Top subagent dispatches (by {RANKED_BY})", self.html)
        self.assertNotIn("(by spend)", self.html)

    def test_the_page_shows_the_ranked_quantity_it_sorted_on(self) -> None:
        # A ranking whose key is not on screen cannot be checked by the reader.
        self.assertIn("r.total_tokens", self.html)

    def test_the_page_names_the_archived_state_and_flags_it_irreplaceable(
        self,
    ) -> None:
        # CLAUDE.md constraint 4: the new state ANNOTATES this panel -- no new
        # table and no column added just for it. The page reads the third
        # status value, and where it shows those figures it says why they are
        # not routine: no re-ingest can reproduce them.
        self.assertIn(f'"{STATUS_ARCHIVED}"', self.html)
        self.assertIn("only copy", self.html)
        # The count of them is stated on the panel -- the established way this
        # codebase names a partial figure (cf. `dispatches_without_peak`).
        self.assertIn('id="agents-archived"', self.html)

    def test_the_page_keeps_the_unmeasured_treatment_for_the_unmeasured_row(
        self,
    ) -> None:
        # The absence half must survive the fix: the muted, dashed rendering
        # keys on `unavailable` ALONE, so a row that has real figures is not
        # styled as a gap and a row that has none is not styled as data.
        self.assertIn('r.status === "unavailable"', self.html)


# --- #31: context-window utilisation --------------------------------------
#
# The headline `Avg context/call` has no referent: nothing on the page says
# whether 266.6k is a lot. These tests pin the two halves of the referent --
# a DOCUMENTED denominator (the model's context window) and a DATED JUDGEMENT
# (where the bands sit) -- and keep the two provenances apart, because
# borrowing Anthropic's authority for a boundary Anthropic never published is
# the failure this repository exists to avoid.


class ContextWindowTableTest(unittest.TestCase):
    """The window table is a documented fact, matched the way rates were.

    The expectations are spelled out here rather than read from
    `CONTEXT_WINDOWS`: a test that asserts the table equals itself passes over
    any edit, including the one that matters (a model quietly given the wrong
    window).
    """

    # Documented context windows, checked 2026-08-05 against Anthropic's model
    # reference. Haiku is the reason this is a per-model table at all.
    DOCUMENTED = {
        "claude-opus-5": 1_000_000,
        "claude-opus-4-8": 1_000_000,
        "claude-opus-4-7": 1_000_000,
        "claude-opus-4-6": 1_000_000,
        "claude-sonnet-5": 1_000_000,
        "claude-sonnet-4-6": 1_000_000,
        "claude-fable-5": 1_000_000,
        "claude-haiku-4-5": 200_000,
    }

    def test_every_documented_model_carries_its_documented_window(self) -> None:
        self.assertEqual(CONTEXT_WINDOWS, self.DOCUMENTED)

    def test_a_dated_model_id_resolves_to_its_family(self) -> None:
        # Every id in a transcript carries a date suffix; the table does not.
        self.assertEqual(window_for_model("claude-opus-5-20260101"), 1_000_000)
        self.assertEqual(window_for_model("claude-haiku-4-5-20251001"), 200_000)

    def test_haiku_is_a_fifth_of_the_window_the_others_have(self) -> None:
        # The whole justification for a per-model table. Measured 2026-08-05 on
        # the reference corpus: Haiku's largest call carries 111,700 tokens --
        # 55.9% of its 200K window, but 11.2% against a wrongly assumed 1M.
        self.assertEqual(window_for_model("claude-haiku-4-5-20251001"), 200_000)
        self.assertNotEqual(window_for_model("claude-haiku-4-5-20251001"), 1_000_000)

    def test_an_unknown_model_has_no_window_at_all(self) -> None:
        # Rule #12, and the central rule of this feature: no default, and
        # emphatically not 1M. A window we cannot source produces no
        # utilisation, so the call is counted as INCONCLUSIVE instead.
        for model in (
            "claude-nosuchtier-9-20260101",
            "<synthetic>",  # Claude Code's local error placeholder
            "<unknown>",    # ingest.py's own fallback for a model-less record
            "",
        ):
            with self.subTest(model=model):
                self.assertIsNone(window_for_model(model))

    def test_a_prefix_matches_only_at_a_boundary(self) -> None:
        # `claude-opus-5` must not match a hypothetical `claude-opus-5x`: the
        # rule the deleted rate table used, kept because a family boundary is
        # exactly where a window can change.
        self.assertIsNone(window_for_model("claude-opus-5x"))
        self.assertIsNone(window_for_model("claude-opus"))
        self.assertEqual(window_for_model("claude-opus-5"), 1_000_000)

    def test_the_longest_matching_prefix_wins(self) -> None:
        # No pair in today's table nests, so the rule is pinned against a
        # table that does -- otherwise the property is untestable until the
        # day a nested entry is added, which is the day it must not break.
        nested = {"claude-opus-4": 111, "claude-opus-4-8": 222}
        self.assertEqual(longest_prefix_match("claude-opus-4-8-20260101", nested), 222)
        self.assertEqual(longest_prefix_match("claude-opus-4-7-20260101", nested), 111)
        # Insertion order must not decide it.
        reversed_table = dict(reversed(list(nested.items())))
        self.assertEqual(
            longest_prefix_match("claude-opus-4-8-20260101", reversed_table), 222
        )

    def test_the_table_is_dated_so_a_reader_can_judge_its_currency(self) -> None:
        self.assertEqual(WINDOWS_AS_OF, "2026-08-05")
        date.fromisoformat(WINDOWS_AS_OF)  # raises if it is not a real date
        self.assertTrue(WINDOW_SOURCE.startswith("https://"))

    def test_every_window_is_one_of_the_two_documented_sizes(self) -> None:
        # A typo'd window is the one failure this feature cannot see for
        # itself below 100%; the sizes are documented and few, so pin them.
        self.assertEqual(set(CONTEXT_WINDOWS.values()), {200_000, 1_000_000})


class UtilisationBandTest(unittest.TestCase):
    """Where the boundaries sit, and that the LOWER edge is the inclusive one.

    The `>=` vs `>` question is real: a call at exactly 90% of its window is
    either "probably wrong" or not, and the two readings differ on real data.
    Every band is half-open, lower-inclusive -- the same convention
    `day_bounds()` uses for a day.
    """

    def test_each_boundary_belongs_to_the_band_above_it(self) -> None:
        self.assertEqual(band_for(0.90), BAND_AT_LEAST_90)
        self.assertEqual(band_for(0.50), BAND_50_TO_90)
        self.assertEqual(band_for(0.25), BAND_25_TO_50)
        self.assertEqual(band_for(0.0), BAND_UNDER_25)

    def test_one_token_below_a_boundary_falls_in_the_band_below(self) -> None:
        # Expressed as real token counts against a real window rather than as
        # bare floats: 0.8999999 is a number, 899,999 tokens is a call.
        self.assertEqual(band_for(899_999 / 1_000_000), BAND_50_TO_90)
        self.assertEqual(band_for(499_999 / 1_000_000), BAND_25_TO_50)
        self.assertEqual(band_for(249_999 / 1_000_000), BAND_UNDER_25)

    def test_a_call_over_its_window_stays_in_the_top_band(self) -> None:
        # >100% is absurd on its face -- a stale table, a beta window, a
        # corrupt row -- and must not fall off the end of the banding.
        self.assertEqual(band_for(1.25), BAND_AT_LEAST_90)

    def test_a_negative_fraction_is_refused_rather_than_banded(self) -> None:
        with self.assertRaises(ValueError):
            band_for(-0.01)

    def test_the_bands_are_contiguous_and_cover_everything_from_zero(self) -> None:
        lowers = [lower for _, _, lower, _ in BANDS]
        uppers = [upper for _, _, _, upper in BANDS]
        self.assertEqual(lowers, sorted(lowers, reverse=True), "bands run high to low")
        self.assertIsNone(uppers[0], "the top band is unbounded above")
        self.assertEqual(lowers[-1], 0.0, "the bottom band starts at zero")
        for i in range(1, len(BANDS)):
            with self.subTest(band=BANDS[i][0]):
                self.assertEqual(uppers[i], lowers[i - 1], "a gap between bands")

    def test_only_the_two_upper_bands_carry_a_judgement(self) -> None:
        # The boundaries are a product-owner judgement; the WORDS are the
        # judgement made visible, and no judgement was made about a call under
        # half its window. A label there would invent one.
        labels = {key: label for key, label, _, _ in BANDS}
        self.assertIn("probably wrong", labels[BAND_AT_LEAST_90])
        self.assertIn("likely wasteful", labels[BAND_50_TO_90])
        for key in (BAND_25_TO_50, BAND_UNDER_25):
            with self.subTest(band=key):
                for verdict in ("wrong", "wasteful", "bloat", "healthy", "good"):
                    self.assertNotIn(verdict, labels[key])

    def test_the_judgement_is_dated_separately_from_the_window_table(self) -> None:
        # Two facts with two different owners and two different staleness
        # stories: re-checking Anthropic's published window does not re-decide
        # where 50% sits, and one date for both would imply it had.
        self.assertEqual(BANDS_AS_OF, "2026-08-05")
        date.fromisoformat(BANDS_AS_OF)

    def test_the_model_window_moves_a_call_across_a_band_boundary(self) -> None:
        # The 5x misread, as a band change rather than as a ratio: Haiku's
        # largest measured call (111,700 tokens, reference corpus 2026-08-05)
        # is over half its window, and would read as a frugal call against the
        # 1M window every other current model has.
        against_own = band_for(111_700 / window_for_model("claude-haiku-4-5"))
        against_wrong = band_for(111_700 / 1_000_000)
        self.assertEqual(against_own, BAND_50_TO_90)
        self.assertEqual(against_wrong, BAND_UNDER_25)


class NearestRankTest(unittest.TestCase):
    """Every published percentile is a value some call actually carried."""

    def test_an_empty_sample_has_no_percentile(self) -> None:
        # Not 0: an empty window is not a window of tiny calls.
        for p in PERCENTILES:
            with self.subTest(percentile=p):
                self.assertIsNone(nearest_rank([], p))

    def test_a_single_call_is_every_percentile_of_itself(self) -> None:
        for p in PERCENTILES:
            with self.subTest(percentile=p):
                self.assertEqual(nearest_rank([77], p), 77)

    def test_the_median_of_an_even_sample_is_an_observed_value(self) -> None:
        # The interpolating definition returns 30 here -- a context no call
        # had, and one that can sit on the far side of a band boundary from
        # both of its neighbours.
        self.assertEqual(nearest_rank([10, 20, 40, 80], 50), 20)

    def test_the_top_percentile_is_the_largest_call_not_past_it(self) -> None:
        sample = list(range(1, 101))  # 1..100
        self.assertEqual(nearest_rank(sample, 99), 99)
        self.assertEqual(nearest_rank(sample, 10), 10)
        self.assertEqual(nearest_rank(sample, 100), 100)


# The utilisation fixture. Every call is pinned deliberately, and the numbers
# are chosen so that a plausible wrong implementation goes red:
#
#   * the four bands hold 3/4/5/1 calls -- all different, so a band mapping
#     that swaps two of them cannot pass;
#   * the SAME context size (250,000) appears on both a 1M model and a 200K
#     model, landing in DIFFERENT bands. One shared denominator collapses them;
#   * three calls sit exactly ON a band boundary and three sit one token below
#     it; one more sits exactly ON its window, which is the limit and not over
#     it, so `over_window_calls` cannot pass with a `>=`;
#   * every call carries output tokens, so an implementation that measures
#     total tokens instead of context pushes each just-below call over its
#     boundary;
#   * one call has an unknown model, one call has no context measurement at
#     all, and both are counted rather than banded or dropped;
#   * the mean lands ABOVE the median (the corpus's skew, in miniature), and
#     the pre-#25 `avg_context` -- the same tokens divided by one more row --
#     lands BETWEEN the two, so no pair of them can be confused and a
#     regression to that divisor is a different number from either.
CONTEXT_SESSION = "context-fixture"
CONTEXT_DAY = "2026-04-07"
CONTEXT_EMPTY_DAY = "2026-04-06"
OPUS_1M = "claude-opus-5-20260101"
HAIKU_200K = "claude-haiku-4-5-20251001"
UNKNOWN_MODEL = "claude-nosuchtier-9-20260101"
# (model, context_size, output_tokens, expected band -- None = no window)
CONTEXT_CALLS = [
    (OPUS_1M, 900_000, 3, BAND_AT_LEAST_90),      # exactly 90.0%
    (OPUS_1M, 899_999, 5, BAND_50_TO_90),         # one token below it
    (OPUS_1M, 600_000, 7, BAND_50_TO_90),
    (OPUS_1M, 500_000, 11, BAND_50_TO_90),        # exactly 50.0%
    (OPUS_1M, 499_999, 13, BAND_25_TO_50),        # one token below it
    (OPUS_1M, 350_000, 17, BAND_25_TO_50),
    (OPUS_1M, 300_000, 19, BAND_25_TO_50),
    (OPUS_1M, 250_000, 23, BAND_25_TO_50),        # exactly 25.0%
    (OPUS_1M, 249_999, 29, BAND_UNDER_25),        # one token below it
    (HAIKU_200K, 250_000, 31, BAND_AT_LEAST_90),  # 125% of a 200K window
    (HAIKU_200K, 200_000, 37, BAND_AT_LEAST_90),  # exactly ITS window: not over
    (HAIKU_200K, 111_700, 41, BAND_50_TO_90),     # 55.9%; 11.2% against 1M
    (HAIKU_200K, 50_000, 43, BAND_25_TO_50),      # exactly 25%; 5% against 1M
    (UNKNOWN_MODEL, 777_777, 47, None),           # no window: INCONCLUSIVE
]
# A record whose `usage` carries output tokens and no prompt accounting at
# all -- the population #25 is about. Its context is 0, which is not a small
# context: it is no measurement of one.
CONTEXT_ZERO_OUTPUT = 53
CONTEXT_TOTAL_CALLS = len(CONTEXT_CALLS) + 1                       # 15
CONTEXT_SAMPLE_CALLS = len(CONTEXT_CALLS)                          # 14
CONTEXT_BANDED_CALLS = sum(1 for *_, band in CONTEXT_CALLS if band is not None)
# Hand-written, then checked against the table above: a count derived only
# from the fixture would agree with a fixture that had drifted.
EXPECTED_BAND_CALLS = {
    BAND_AT_LEAST_90: 3,
    BAND_50_TO_90: 4,
    BAND_25_TO_50: 5,
    BAND_UNDER_25: 1,
}
# Nearest-rank over the 14 measured contexts, sorted:
#    50,000  111,700  200,000  249,999  250,000  250,000  300,000
#   350,000  499,999  500,000  600,000  777,777  899,999  900,000
# All six percentiles land on DIFFERENT values, so a swapped mapping is red.
EXPECTED_PERCENTILES = {
    "p10": 111_700,
    "p25": 249_999,
    "p50": 300_000,
    "p75": 600_000,
    "p90": 899_999,
    "p99": 900_000,
}
EXPECTED_MEDIAN = 300_000
EXPECTED_SAMPLE_TOTAL = 5_939_474
EXPECTED_MEAN = EXPECTED_SAMPLE_TOTAL / CONTEXT_SAMPLE_CALLS  # 424,248.1
EXPECTED_CALLS_ABOVE_MEAN = 6
# What `AVG(context_size)` reported BEFORE #25: the same tokens over one more
# row. Kept, and asserted AGAINST, so a regression to it is red rather than
# silent.
EXPECTED_LEGACY_AVG = EXPECTED_SAMPLE_TOTAL / CONTEXT_TOTAL_CALLS  # 395,964.9


def build_context_corpus(root: Path) -> Path:
    """One session whose calls span both window sizes, plus the two absences.

    Returns the PROJECT directory, which is what `ingest()` scans -- one
    project per directory, as `default_projects_dir()` resolves it.
    """
    project = root / "projects" / "-fixture-context"
    project.mkdir(parents=True)

    def call(n: int, model: str, usage: dict[str, int]) -> str:
        return json.dumps(
            {
                "type": "assistant",
                "sessionId": CONTEXT_SESSION,
                "timestamp": f"{CONTEXT_DAY}T15:{n:02d}:00.000Z",
                "isSidechain": False,
                "message": {
                    "id": f"msg-context-{n}",
                    "model": model,
                    "usage": usage,
                    "content": [{"type": "text", "text": f"call {n}"}],
                },
            }
        ) + "\n"

    lines = []
    for n, (model, context, output, _band) in enumerate(CONTEXT_CALLS):
        # Three deliberately unequal classes summing to the target context, so
        # a swapped column mapping cannot reproduce it.
        lines.append(
            call(
                n,
                model,
                {
                    "input_tokens": 1_000,
                    "cache_creation_input_tokens": 2_000,
                    "cache_read_input_tokens": context - 3_000,
                    "output_tokens": output,
                },
            )
        )
    lines.append(
        call(
            len(CONTEXT_CALLS),
            OPUS_1M,
            {"output_tokens": CONTEXT_ZERO_OUTPUT},
        )
    )
    (project / f"{CONTEXT_SESSION}.jsonl").write_text("".join(lines))
    return project


class ContextUtilisationApiTest(unittest.TestCase):
    """`/api/summary`'s `context` block: the median, the spread and the bands.

    Asserted through the real ingest path, so the block is measured over rows
    written the way production writes them.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-context-test-"))
        projects = build_context_corpus(cls.tmp)
        db_path = cls.tmp / "usage.db"
        ingest(projects, db_path, tasks_dir=cls.tmp / "no-task-index")
        cls.api = Api(db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def block(self, day: str | None = None) -> dict:
        return self.api.summary(*day_bounds(day, day))["context"]

    def test_the_fixture_holds_what_it_claims_to(self) -> None:
        # A fixture that drifted from the expectations above would make every
        # assertion below a statement about a different corpus.
        summary = self.api.summary(*day_bounds(None, None))
        self.assertEqual(summary["calls"], CONTEXT_TOTAL_CALLS)
        counted: dict[str, int] = {}
        for *_, band in CONTEXT_CALLS:
            if band is not None:
                counted[band] = counted.get(band, 0) + 1
        self.assertEqual(counted, EXPECTED_BAND_CALLS)
        self.assertEqual(
            sum(context for _, context, _, _ in CONTEXT_CALLS),
            EXPECTED_SAMPLE_TOTAL,
        )

    def test_the_headline_is_a_median_of_the_measured_calls(self) -> None:
        block = self.block()
        self.assertEqual(block["median"], EXPECTED_MEDIAN)
        self.assertEqual(block["sample_calls"], CONTEXT_SAMPLE_CALLS)
        self.assertEqual(block["sample_is"], CONTEXT_SAMPLE)

    def test_the_spread_reports_every_percentile_it_publishes(self) -> None:
        block = self.block()
        self.assertEqual(block["percentiles"], EXPECTED_PERCENTILES)
        self.assertEqual(
            sorted(block["percentiles"]), sorted(f"p{p}" for p in PERCENTILES)
        )

    def test_the_median_is_the_p50_it_publishes(self) -> None:
        # One quantity, one spelling: a headline that disagreed with the spread
        # beside it would be two answers to the same question.
        block = self.block()
        self.assertEqual(block["median"], block["percentiles"]["p50"])

    def test_the_mean_is_kept_beside_the_median_and_named_as_the_skew(
        self,
    ) -> None:
        # The mean survives only as EVIDENCE about the distribution -- it is
        # 1.45x the median here and only 5 of 11 calls exceed it. On the
        # reference corpus (2026-08-05) it is 1.53x with 28.7% above.
        block = self.block()
        self.assertAlmostEqual(block["mean"], EXPECTED_MEAN)
        self.assertGreater(block["mean"], block["median"])
        self.assertAlmostEqual(
            block["mean_over_median"], EXPECTED_MEAN / EXPECTED_MEDIAN
        )
        self.assertEqual(block["calls_above_mean"], EXPECTED_CALLS_ABOVE_MEAN)
        self.assertAlmostEqual(
            block["share_above_mean"],
            EXPECTED_CALLS_ABOVE_MEAN / CONTEXT_SAMPLE_CALLS,
        )

    def test_a_call_with_no_context_measurement_is_counted_not_sampled(self) -> None:
        # #25's population, in one row. Banded, it would file as the most
        # frugal call on the report; sampled, it divides the same tokens by one
        # more row.
        #
        # This assertion was INVERTED by #25, deliberately and not quietly.
        # When #31 landed, `avg_context` still divided by one more row, and
        # the gap between it and `mean` WAS that defect made visible -- so
        # this test pinned the two unequal on purpose. #25 closes the gap by
        # giving both figures the same definition of "measured", so the same
        # pin now reads the other way: they must be EQUAL, and neither may be
        # the legacy figure below. Both directions have teeth -- regress the
        # guard and `avg_context` returns to `EXPECTED_LEGACY_AVG`, which the
        # last assertion refuses.
        block = self.block()
        summary = self.api.summary(*day_bounds(None, None))
        self.assertEqual(block["unmeasured_calls"], 1)
        self.assertEqual(summary["unmeasured_calls"], block["unmeasured_calls"])
        self.assertEqual(summary["context_calls"], block["sample_calls"])
        self.assertAlmostEqual(summary["avg_context"], EXPECTED_MEAN)
        self.assertAlmostEqual(summary["avg_context"], block["mean"])
        self.assertNotAlmostEqual(summary["avg_context"], EXPECTED_LEGACY_AVG)

    def test_every_call_is_banded_or_counted_as_a_named_absence(self) -> None:
        # The invariant that makes the bands readable: nothing is silently
        # dropped, so band counts + INCONCLUSIVE + unmeasured == the window's
        # calls, the same number the summary card shows.
        block = self.block()
        util = block["utilisation"]
        self.assertEqual(
            sum(b["calls"] for b in util["bands"])
            + util["unknown_model_calls"]
            + block["unmeasured_calls"],
            self.api.summary(*day_bounds(None, None))["calls"],
        )
        self.assertEqual(util["banded_calls"], CONTEXT_BANDED_CALLS)

    def test_each_call_is_banded_against_its_own_model_window(self) -> None:
        util = self.block()["utilisation"]
        self.assertEqual(
            {b["band"]: b["calls"] for b in util["bands"]}, EXPECTED_BAND_CALLS
        )

    def test_the_bands_are_reported_high_to_low_with_their_boundaries(self) -> None:
        # A declarative template renders this list as-is: the label, the
        # boundaries and the share are all in the payload, so the view does no
        # arithmetic and cannot invent a boundary of its own.
        util = self.block()["utilisation"]
        self.assertEqual(
            [b["band"] for b in util["bands"]],
            [BAND_AT_LEAST_90, BAND_50_TO_90, BAND_25_TO_50, BAND_UNDER_25],
        )
        top = util["bands"][0]
        self.assertEqual(top["lower"], 0.90)
        self.assertIsNone(top["upper"])
        self.assertAlmostEqual(
            top["share"], EXPECTED_BAND_CALLS[BAND_AT_LEAST_90] / CONTEXT_BANDED_CALLS
        )
        self.assertAlmostEqual(sum(b["share"] for b in util["bands"]), 1.0)

    def test_an_unknown_model_is_named_rather_than_given_a_window(self) -> None:
        # THE rule of this feature. The call keeps its place in the context
        # distribution -- its size was measured -- and has no utilisation at
        # all, so it is counted and named instead of banded.
        util = self.block()["utilisation"]
        self.assertEqual(util["unknown_model_calls"], 1)
        self.assertEqual(util["unknown_models"], [UNKNOWN_MODEL])
        # Still in the distribution: the sample is the banded calls PLUS it,
        # so dropping it would move the median and the mean as well as this
        # count.
        self.assertEqual(
            util["banded_calls"] + util["unknown_model_calls"],
            self.block()["sample_calls"],
        )
        self.assertEqual(util["banded_calls"], CONTEXT_SAMPLE_CALLS - 1)

    def test_a_call_over_its_window_is_counted_where_a_reader_can_see_it(
        self,
    ) -> None:
        # What makes a stale window table safe in a way a stale rate table was
        # not: a wrong denominator produces utilisation over 100%, which is
        # absurd on its face -- but only if someone counts it.
        #
        # A call that exactly FILLS its window is not one of them: the window
        # is a limit, and reaching it is the limit working. The fixture holds
        # one of each, so `>` cannot be relaxed to `>=` without going red.
        util = self.block()["utilisation"]
        self.assertEqual(util["over_window_calls"], 1)
        self.assertEqual(
            {b["band"]: b["calls"] for b in util["bands"]}[BAND_AT_LEAST_90],
            EXPECTED_BAND_CALLS[BAND_AT_LEAST_90],
            "the call at exactly 100% is still in the top band",
        )

    def test_the_window_and_the_bands_carry_different_provenances(self) -> None:
        # Getting this wrong would lend Anthropic's authority to a boundary
        # this project invented.
        util = self.block()["utilisation"]
        self.assertEqual(util["windows_as_of"], WINDOWS_AS_OF)
        self.assertEqual(util["bands_as_of"], BANDS_AS_OF)
        self.assertIn(WINDOW_SOURCE, util["window_provenance"])
        self.assertIn("documented", util["window_provenance"])
        self.assertIn("product-owner judgment", util["band_provenance"])
        self.assertIn("not an Anthropic recommendation", util["band_provenance"])
        self.assertNotIn("Anthropic recommend", util["window_provenance"])

    def test_a_window_with_no_calls_reports_no_sample_rather_than_zero(self) -> None:
        # An empty period is not a period of tiny, healthy calls (rule #12).
        block = self.block(CONTEXT_EMPTY_DAY)
        self.assertEqual(block["sample_calls"], 0)
        self.assertEqual(block["unmeasured_calls"], 0)
        for field in (
            "median", "mean", "mean_over_median", "calls_above_mean",
            "share_above_mean",
        ):
            with self.subTest(field=field):
                self.assertIsNone(block[field])
        for name, value in block["percentiles"].items():
            with self.subTest(percentile=name):
                self.assertIsNone(value)
        util = block["utilisation"]
        self.assertEqual(util["banded_calls"], 0)
        for band in util["bands"]:
            with self.subTest(band=band["band"]):
                self.assertEqual(band["calls"], 0)
                self.assertIsNone(band["share"], "a share of an empty set is not 0%")
        self.assertEqual(util["unknown_models"], [])

    def test_the_block_is_window_scoped_like_every_other_figure(self) -> None:
        self.assertEqual(self.block(CONTEXT_DAY), self.block())
        self.assertNotEqual(self.block(CONTEXT_EMPTY_DAY), self.block())


# ---------------------------------------------------------------------------
# #61: the utilisation bands must name the set they range over.
# ---------------------------------------------------------------------------
#
# The defect, measured 2026-08-05 over this project's own transcripts (44
# sources, 8,163 records, 0 unparsed, 2,722 banded calls):
#
#     scope        calls   median ctx    peak     >=90%          50-90%
#     main-thread    390      427,037   996,190   52 (13.3%)   100 (25.6%)
#     subagent     2,332      104,727   374,816    0  (0.0%)     0  (0.0%)
#     pooled       2,722            --        --   52  (1.9%)   100  (3.7%)
#
# Every red-band call was main-thread. The pooled figure is arithmetically
# correct and moves the WRONG WAY under the condition it exists to detect: a
# saturated orchestrator dispatches more subagents, whose healthy calls push
# the pooled share down.
#
# The fixture is built so no dropped or swapped scope filter can pass:
#
#   * on the busy day the two scopes land in DIFFERENT bands, and their four
#     band counts differ at EVERY index -- main [3, 1, 1, 0], subagent
#     [0, 0, 4, 11] -- so pooling, swapping or dropping either filter changes
#     a number this file names;
#   * each scope has a band it has a REAL zero in (main has no under-25 call;
#     the subagent has none in either of the two top bands), beside a scope
#     that has NO SAMPLE at all -- so `share == 0.0` and `share is None` are
#     both present and must stay distinguishable;
#   * the subagent outnumbers the main thread 15:6, the dilution the issue is
#     about in miniature: the top band is 60.0% of main-thread banded calls
#     and 15.0% pooled, a 4x understatement;
#   * one main-thread call sits at EXACTLY 90.0% of its window, so relaxing
#     `>= 0.9` to `> 0.9` moves it and goes red;
#   * `over_window_calls` is scoped too, pinned from BOTH sides: on the busy
#     day the main thread has one and the subagent none, and a day of its own
#     gives the main thread one and the subagent TWO -- so redirecting every
#     over-window call into one scope changes a count either way. (One side
#     alone did not: the mutation that pooled them into the main thread was
#     GREEN against a fixture where every over-window call was already
#     main-thread.);
#   * the unknown-model call is main-thread ONLY, so a scope that borrowed the
#     pooled `unknown_models` list would name a model its own calls never used;
#   * three separate days each strand one scope in a DIFFERENT absence -- no
#     calls, calls that measured nothing, and measured calls whose model has no
#     documented window -- because all three render as four bands of zero.
SU_SESSION = "scoped-utilisation-fixture"
SU_AGENT = "agent-su61a"
SU_EMPTY_DAY = "2026-06-09"        # nothing at all
SU_BUSY_DAY = "2026-06-10"         # both scopes, deliberately different bands
SU_MAIN_ONLY_DAY = "2026-06-11"    # the subagent ran no call
SU_BLIND_DAY = "2026-06-12"        # the subagent ran calls that measured nothing
SU_UNWINDOWED_DAY = "2026-06-13"   # the subagent's calls have no documented window
SU_OVER_WINDOW_DAY = "2026-06-14"  # both scopes exceed their window, unequally

SU_OPUS_1M = "claude-opus-5-20260101"
SU_HAIKU_200K = "claude-haiku-4-5-20251001"
SU_UNKNOWN_MODEL = "claude-nosuchtier-9-20260101"

# (day, kind, model, context_size, expected band -- None = not banded)
SU_MAIN = SOURCE_MAIN
SU_SUB = SOURCE_SUBAGENT
SU_CALLS: list[tuple[str, str, str, int, str | None]] = [
    # --- the busy day, main thread: heavy, and nothing under 25% ---
    (SU_BUSY_DAY, SU_MAIN, SU_OPUS_1M, 1_100_000, BAND_AT_LEAST_90),  # 110%: over
    (SU_BUSY_DAY, SU_MAIN, SU_OPUS_1M, 950_000, BAND_AT_LEAST_90),    # 95%
    (SU_BUSY_DAY, SU_MAIN, SU_OPUS_1M, 900_000, BAND_AT_LEAST_90),    # exactly 90.0%
    (SU_BUSY_DAY, SU_MAIN, SU_OPUS_1M, 600_000, BAND_50_TO_90),       # 60%
    (SU_BUSY_DAY, SU_MAIN, SU_OPUS_1M, 300_000, BAND_25_TO_50),       # 30%
    (SU_BUSY_DAY, SU_MAIN, SU_UNKNOWN_MODEL, 500_000, None),          # no window
    # --- the busy day, subagents: many, and none above half a window ---
    *[
        (SU_BUSY_DAY, SU_SUB, SU_HAIKU_200K, 60_000 + n, BAND_25_TO_50)  # 30%ish
        for n in range(4)
    ],
    *[
        (SU_BUSY_DAY, SU_SUB, SU_HAIKU_200K, 40_000 + n, BAND_UNDER_25)  # 20%ish
        for n in range(11)
    ],
    # --- a day the subagent sat out entirely ---
    (SU_MAIN_ONLY_DAY, SU_MAIN, SU_OPUS_1M, 950_000, BAND_AT_LEAST_90),
    # --- a day the subagent ran calls that carried no prompt accounting ---
    (SU_BLIND_DAY, SU_MAIN, SU_OPUS_1M, 300_000, BAND_25_TO_50),
    (SU_BLIND_DAY, SU_SUB, SU_HAIKU_200K, 0, None),
    (SU_BLIND_DAY, SU_SUB, SU_HAIKU_200K, 0, None),
    # --- a day the subagent's measured calls have no documented window ---
    (SU_UNWINDOWED_DAY, SU_MAIN, SU_OPUS_1M, 300_000, BAND_25_TO_50),
    (SU_UNWINDOWED_DAY, SU_SUB, SU_UNKNOWN_MODEL, 111_111, None),
    (SU_UNWINDOWED_DAY, SU_SUB, SU_UNKNOWN_MODEL, 222_222, None),
    # --- a day both scopes run past their window, by DIFFERENT counts ---
    (SU_OVER_WINDOW_DAY, SU_MAIN, SU_HAIKU_200K, 300_000, BAND_AT_LEAST_90),   # 150%
    (SU_OVER_WINDOW_DAY, SU_SUB, SU_HAIKU_200K, 250_000, BAND_AT_LEAST_90),    # 125%
    (SU_OVER_WINDOW_DAY, SU_SUB, SU_HAIKU_200K, 400_000, BAND_AT_LEAST_90),    # 200%
]

# Hand-written, then checked against the table above -- a count derived only
# from the fixture would agree with a fixture that had drifted.
SU_BUSY_MAIN_BANDS = {
    BAND_AT_LEAST_90: 3,
    BAND_50_TO_90: 1,
    BAND_25_TO_50: 1,
    BAND_UNDER_25: 0,
}
SU_BUSY_SUB_BANDS = {
    BAND_AT_LEAST_90: 0,
    BAND_50_TO_90: 0,
    BAND_25_TO_50: 4,
    BAND_UNDER_25: 11,
}
SU_BUSY_MAIN_CALLS = 6
SU_BUSY_MAIN_BANDED = 5
SU_BUSY_SUB_CALLS = 15
SU_BUSY_SUB_BANDED = 15
SU_BUSY_POOLED_BANDED = SU_BUSY_MAIN_BANDED + SU_BUSY_SUB_BANDED  # 20
# The issue, as two numbers: 3/5 against 3/20.
SU_BUSY_MAIN_TOP_SHARE = 3 / 5     # 0.60
SU_BUSY_POOLED_TOP_SHARE = 3 / 20  # 0.15


def build_scoped_utilisation_corpus(root: Path) -> Path:
    """One session across five days, with subagent calls banded away from the
    main thread's.

    Returns the PROJECT directory, which is what `ingest()` scans.
    """
    project = root / "projects" / "-fixture-scoped-utilisation"
    project.mkdir(parents=True)
    subagents = project / SU_SESSION / "subagents"
    subagents.mkdir(parents=True)

    def record(n: int, day: str, kind: str, model: str, context: int) -> str:
        if context:
            # Three deliberately unequal classes summing to the target context,
            # so a swapped column mapping cannot reproduce it.
            usage = {
                "input_tokens": 1_000,
                "cache_creation_input_tokens": 2_000,
                "cache_read_input_tokens": context - 3_000,
                # Distinct per call and never zero, so the dedupe's
                # greatest-output rule has an unambiguous survivor and an
                # output-token mix-up cannot look like a context.
                "output_tokens": n + 1,
            }
        else:
            # The #25 population: the four keys PRESENT and valued zero, which
            # is what the records on disk actually look like.
            usage = {
                "input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 0,
            }
        payload: dict[str, object] = {
            "type": "assistant",
            "sessionId": SU_SESSION,
            "timestamp": f"{day}T15:{n // 60:02d}:{n % 60:02d}.000Z",
            "isSidechain": kind == SOURCE_SUBAGENT,
            "message": {
                "id": f"msg-su61-{n}",
                "model": model,
                "usage": usage,
                "content": [{"type": "text", "text": f"su61 call {n}"}],
            },
        }
        if kind == SOURCE_SUBAGENT:
            payload["agentId"] = SU_AGENT
        return json.dumps(payload) + "\n"

    main_lines: list[str] = []
    sub_lines: list[str] = []
    for n, (day, kind, model, context, _band) in enumerate(SU_CALLS):
        line = record(n, day, kind, model, context)
        (sub_lines if kind == SOURCE_SUBAGENT else main_lines).append(line)
    (project / f"{SU_SESSION}.jsonl").write_text("".join(main_lines))
    (subagents / f"{SU_AGENT}.jsonl").write_text("".join(sub_lines))
    return project


class ScopedUtilisationBandTest(unittest.TestCase):
    """#61: the bands are tallied per scope, and every tally names its set.

    Asserted through the real ingest path, so the split is measured over rows
    written the way production writes them -- including the `source_kind` the
    split turns on, which the ingester derives from the source's own path.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-scoped-util-test-"))
        projects = build_scoped_utilisation_corpus(cls.tmp)
        db_path = cls.tmp / "usage.db"
        ingest(projects, db_path, tasks_dir=cls.tmp / "no-task-index")
        cls.api = Api(db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def util(self, day: str | None = None) -> dict:
        return self.api.summary(*day_bounds(day, day))["context"]["utilisation"]

    def scope(self, name: str, day: str | None = None) -> dict:
        found = [s for s in self.util(day)["by_scope"] if s["scope"] == name]
        self.assertEqual(len(found), 1, f"{name} is not in by_scope exactly once")
        return found[0]

    @staticmethod
    def counts(tally: dict) -> dict:
        return {b["band"]: b["calls"] for b in tally["bands"]}

    @staticmethod
    def shares(tally: dict) -> dict:
        return {b["band"]: b["share"] for b in tally["bands"]}

    # --- the fixture holds what it claims to ---

    def test_the_fixture_pins_the_two_scopes_into_different_bands(self) -> None:
        # A fixture whose two scopes agreed anywhere would let a dropped filter
        # through at that index.
        counted: dict[str, dict[str, int]] = {SU_MAIN: {}, SU_SUB: {}}
        for day, kind, _model, _context, band in SU_CALLS:
            if day == SU_BUSY_DAY and band is not None:
                counted[kind][band] = counted[kind].get(band, 0) + 1
        for kind, expected in (
            (SU_MAIN, SU_BUSY_MAIN_BANDS),
            (SU_SUB, SU_BUSY_SUB_BANDS),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(
                    {k: counted[kind].get(k, 0) for k in expected}, expected
                )
        for band in SU_BUSY_MAIN_BANDS:
            with self.subTest(band=band):
                self.assertNotEqual(
                    SU_BUSY_MAIN_BANDS[band],
                    SU_BUSY_SUB_BANDS[band],
                    "the two scopes agree in this band, so a dropped scope "
                    "filter would pass here",
                )

    # --- the split itself ---

    def test_the_bands_are_tallied_per_scope(self) -> None:
        # THE test. Pooled, both scopes would read [3, 1, 5, 11].
        self.assertEqual(
            self.counts(self.scope(SCOPE_MAIN, SU_BUSY_DAY)), SU_BUSY_MAIN_BANDS
        )
        self.assertEqual(
            self.counts(self.scope(SCOPE_SUBAGENT, SU_BUSY_DAY)), SU_BUSY_SUB_BANDS
        )

    def test_every_scope_is_emitted_in_the_api_vocabulary_and_in_one_order(
        self,
    ) -> None:
        # `source_kind` is the ingester's word; these are the labels that cross
        # the API, and they are the SAME ones every other scoped figure uses.
        util = self.util(SU_BUSY_DAY)
        labels = [s["scope"] for s in util["by_scope"]]
        self.assertEqual(labels, [SCOPE_MAIN, SCOPE_SUBAGENT])
        self.assertEqual(list(SCOPE_ORDER), [SOURCE_MAIN, SOURCE_SUBAGENT])
        # The SAME words every other scoped figure on this API uses -- not a
        # second vocabulary for the same distinction.
        self.assertEqual(
            set(labels),
            {
                r["scope"]
                for r in self.api.models(*day_bounds(SU_BUSY_DAY, SU_BUSY_DAY))
            },
        )
        # `SOURCE_MAIN` is 'main' and its label is 'main-thread', so this one
        # catches a raw `source_kind` crossing the boundary. (`SOURCE_SUBAGENT`
        # and `SCOPE_SUBAGENT` are the same word, so it cannot be tested there
        # -- which is why the label mapping is asserted above rather than by
        # spot-checking strings.)
        self.assertNotIn(
            SOURCE_MAIN, labels, "the stored source_kind leaked across the API"
        )

    def test_the_pooled_tally_names_the_set_it_spans(self) -> None:
        # Kept, because it is still the true answer to "of every call in this
        # window, how many were red" -- but it may not sit there unnamed.
        util = self.util(SU_BUSY_DAY)
        self.assertEqual(util["includes"], SCOPE_INCLUDES_BOTH)
        self.assertIn(SCOPE_MAIN, util["includes"])
        self.assertIn(SCOPE_SUBAGENT, util["includes"])
        self.assertNotIn(
            "scope",
            util,
            "the pooled tally names one set twice, in two vocabularies",
        )

    def test_the_pooled_tally_is_the_sum_of_the_scoped_ones(self) -> None:
        # Summed from the scopes rather than counted again, so the two can
        # never disagree -- band by band, not just in total.
        util = self.util(SU_BUSY_DAY)
        scopes = util["by_scope"]
        for band in SU_BUSY_MAIN_BANDS:
            with self.subTest(band=band):
                self.assertEqual(
                    self.counts(util)[band],
                    sum(self.counts(s)[band] for s in scopes),
                )
        for field in (
            "calls", "sample_calls", "unmeasured_calls", "banded_calls",
            "unknown_model_calls", "over_window_calls",
        ):
            with self.subTest(field=field):
                self.assertEqual(util[field], sum(s[field] for s in scopes))
        self.assertEqual(util["banded_calls"], SU_BUSY_POOLED_BANDED)

    def test_the_pooled_share_dilutes_the_scope_the_reader_can_act_on(self) -> None:
        # The issue in one assertion. On the reference corpus the same
        # arithmetic turned 13.3% of main-thread calls into 1.9% pooled; here
        # it turns 60.0% into 15.0%.
        util = self.util(SU_BUSY_DAY)
        main = self.scope(SCOPE_MAIN, SU_BUSY_DAY)
        self.assertAlmostEqual(
            self.shares(main)[BAND_AT_LEAST_90], SU_BUSY_MAIN_TOP_SHARE
        )
        self.assertAlmostEqual(
            self.shares(util)[BAND_AT_LEAST_90], SU_BUSY_POOLED_TOP_SHARE
        )
        self.assertLess(
            self.shares(util)[BAND_AT_LEAST_90],
            self.shares(main)[BAND_AT_LEAST_90],
            "the fixture no longer reproduces the dilution this exists to fix",
        )
        # And the direction the issue is really about: the subagent scope, which
        # supplies the dilution, has no red call at all.
        self.assertEqual(
            self.counts(self.scope(SCOPE_SUBAGENT, SU_BUSY_DAY))[BAND_AT_LEAST_90], 0
        )

    def test_each_scope_partitions_its_own_calls(self) -> None:
        # Nothing is silently dropped from a scope's denominator: bands +
        # unknown + unmeasured is that scope's whole call count, and the scopes
        # together are the window's.
        util = self.util(SU_BUSY_DAY)
        for tally in util["by_scope"]:
            with self.subTest(scope=tally["scope"]):
                self.assertEqual(
                    sum(b["calls"] for b in tally["bands"])
                    + tally["unknown_model_calls"]
                    + tally["unmeasured_calls"],
                    tally["calls"],
                )
        self.assertEqual(
            sum(s["calls"] for s in util["by_scope"]),
            self.api.summary(*day_bounds(SU_BUSY_DAY, SU_BUSY_DAY))["calls"],
        )
        self.assertEqual(
            self.scope(SCOPE_MAIN, SU_BUSY_DAY)["calls"], SU_BUSY_MAIN_CALLS
        )
        self.assertEqual(
            self.scope(SCOPE_SUBAGENT, SU_BUSY_DAY)["calls"], SU_BUSY_SUB_CALLS
        )

    def test_a_band_boundary_is_lower_inclusive_inside_a_scope_too(self) -> None:
        # One main-thread call sits at exactly 90.0% of its window. Relaxing
        # `>= 0.9` to `> 0.9` moves it into the band below and turns the two
        # counts here into 2 and 2.
        main = self.scope(SCOPE_MAIN, SU_BUSY_DAY)
        self.assertEqual(self.counts(main)[BAND_AT_LEAST_90], 3)
        self.assertEqual(self.counts(main)[BAND_50_TO_90], 1)

    def test_a_call_over_its_window_is_counted_inside_its_own_scope(self) -> None:
        # Pinned from both sides. The busy day gives the main thread one and
        # the subagent none; a day of its own gives the main thread one and the
        # subagent TWO, so a mutation that funnels every over-window call into
        # one scope changes a count whichever scope it picks. Mutation-checked:
        # with the busy day alone, redirecting them all to the main thread was
        # GREEN, because every over-window call in the fixture was already
        # main-thread.
        self.assertEqual(self.scope(SCOPE_MAIN, SU_BUSY_DAY)["over_window_calls"], 1)
        self.assertEqual(
            self.scope(SCOPE_SUBAGENT, SU_BUSY_DAY)["over_window_calls"], 0
        )
        self.assertEqual(
            self.scope(SCOPE_MAIN, SU_OVER_WINDOW_DAY)["over_window_calls"], 1
        )
        self.assertEqual(
            self.scope(SCOPE_SUBAGENT, SU_OVER_WINDOW_DAY)["over_window_calls"], 2
        )
        self.assertEqual(self.util(SU_OVER_WINDOW_DAY)["over_window_calls"], 3)

    def test_an_unknown_model_stays_inside_the_scope_that_called_it(self) -> None:
        # A scope that borrowed the pooled list would name a model its own
        # calls never used -- an absence attributed to the wrong reader.
        main = self.scope(SCOPE_MAIN, SU_BUSY_DAY)
        sub = self.scope(SCOPE_SUBAGENT, SU_BUSY_DAY)
        self.assertEqual(main["unknown_model_calls"], 1)
        self.assertEqual(main["unknown_models"], [SU_UNKNOWN_MODEL])
        self.assertEqual(sub["unknown_model_calls"], 0)
        self.assertEqual(sub["unknown_models"], [])
        # Still in that scope's sample: its size was measured, so dropping it
        # from the denominator would be the silent removal this refuses.
        self.assertEqual(main["sample_calls"], SU_BUSY_MAIN_CALLS)
        self.assertEqual(
            main["banded_calls"] + main["unknown_model_calls"], main["sample_calls"]
        )

    # --- the absences ---

    def test_a_real_zero_share_and_an_absent_one_are_different_values(self) -> None:
        # THE central rule of this repo, on this payload. A scope that ran
        # calls and had none in a band measured a real 0; a scope with no
        # banded sample measured nothing, and its share is null.
        main = self.scope(SCOPE_MAIN, SU_BUSY_DAY)
        sub = self.scope(SCOPE_SUBAGENT, SU_BUSY_DAY)
        self.assertEqual(self.shares(main)[BAND_UNDER_25], 0.0)
        self.assertEqual(self.shares(sub)[BAND_AT_LEAST_90], 0.0)
        self.assertIsNone(main["no_sample_reason"])
        self.assertIsNone(sub["no_sample_reason"])
        absent = self.scope(SCOPE_SUBAGENT, SU_MAIN_ONLY_DAY)
        for band, share in self.shares(absent).items():
            with self.subTest(band=band):
                self.assertIsNone(share, "a share of an empty set is not 0%")
        self.assertIsNotNone(absent["no_sample_reason"])

    def test_a_scope_that_ran_no_call_says_so_rather_than_banding_zero(self) -> None:
        absent = self.scope(SCOPE_SUBAGENT, SU_MAIN_ONLY_DAY)
        self.assertEqual(absent["calls"], 0)
        self.assertEqual(absent["sample_calls"], 0)
        self.assertEqual(absent["banded_calls"], 0)
        self.assertEqual(absent["no_sample_reason"], UTIL_NO_SAMPLE_NO_CALLS)
        # And the main thread that day is untouched by its neighbour's absence.
        self.assertEqual(
            self.counts(self.scope(SCOPE_MAIN, SU_MAIN_ONLY_DAY))[BAND_AT_LEAST_90], 1
        )

    def test_a_scope_whose_calls_measured_nothing_is_told_apart_from_one_that_ran_none(
        self,
    ) -> None:
        # Both render as four bands of zero and have different remedies: one
        # dispatched nothing, the other dispatched work whose records carry no
        # prompt accounting.
        blind = self.scope(SCOPE_SUBAGENT, SU_BLIND_DAY)
        self.assertEqual(blind["calls"], 2)
        self.assertEqual(blind["sample_calls"], 0)
        self.assertEqual(blind["unmeasured_calls"], 2)
        self.assertEqual(blind["banded_calls"], 0)
        self.assertEqual(
            blind["no_sample_reason"], UTIL_NO_SAMPLE_NO_CONTEXT_MEASUREMENT
        )
        self.assertNotEqual(blind["no_sample_reason"], UTIL_NO_SAMPLE_NO_CALLS)

    def test_a_scope_with_no_documented_window_keeps_its_calls_in_view(self) -> None:
        # The third way four zero bands can be true: the calls happened and
        # were measured, and this tool has no denominator for their model.
        # They must not vanish from the scope's own counts.
        unwindowed = self.scope(SCOPE_SUBAGENT, SU_UNWINDOWED_DAY)
        self.assertEqual(unwindowed["calls"], 2)
        self.assertEqual(unwindowed["sample_calls"], 2)
        self.assertEqual(unwindowed["unmeasured_calls"], 0)
        self.assertEqual(unwindowed["banded_calls"], 0)
        self.assertEqual(unwindowed["unknown_model_calls"], 2)
        self.assertEqual(unwindowed["unknown_models"], [SU_UNKNOWN_MODEL])
        self.assertEqual(
            unwindowed["no_sample_reason"], UTIL_NO_SAMPLE_NO_DOCUMENTED_WINDOW
        )

    def test_an_empty_window_reports_both_scopes_as_absent(self) -> None:
        util = self.util(SU_EMPTY_DAY)
        self.assertEqual(
            [s["scope"] for s in util["by_scope"]], [SCOPE_MAIN, SCOPE_SUBAGENT]
        )
        for tally in util["by_scope"]:
            with self.subTest(scope=tally["scope"]):
                self.assertEqual(tally["calls"], 0)
                self.assertEqual(tally["no_sample_reason"], UTIL_NO_SAMPLE_NO_CALLS)
                for band in tally["bands"]:
                    self.assertEqual(band["calls"], 0)
                    self.assertIsNone(band["share"])

    def test_the_reason_is_given_exactly_when_there_is_no_banded_sample(self) -> None:
        # Non-null exactly when `banded_calls` is 0 -- in both directions, over
        # every day the fixture holds, so neither a missing reason nor a
        # spurious one can pass.
        days = [
            SU_EMPTY_DAY, SU_BUSY_DAY, SU_MAIN_ONLY_DAY, SU_BLIND_DAY,
            SU_UNWINDOWED_DAY, SU_OVER_WINDOW_DAY, None,
        ]
        seen = set()
        for day in days:
            util = self.util(day)
            for tally in util["by_scope"] + [util]:
                with self.subTest(day=day, scope=tally.get("scope", "pooled")):
                    self.assertEqual(
                        tally["no_sample_reason"] is None,
                        bool(tally["banded_calls"]),
                    )
                    seen.add(tally["no_sample_reason"])
        # All three absences are exercised, so no branch is asserted only in
        # the abstract.
        self.assertEqual(
            seen - {None},
            {
                UTIL_NO_SAMPLE_NO_CALLS,
                UTIL_NO_SAMPLE_NO_CONTEXT_MEASUREMENT,
                UTIL_NO_SAMPLE_NO_DOCUMENTED_WINDOW,
            },
        )

    # --- what survived the restructuring ---

    def test_both_provenances_survive_the_split_intact(self) -> None:
        util = self.util(SU_BUSY_DAY)
        self.assertEqual(util["windows_as_of"], WINDOWS_AS_OF)
        self.assertEqual(util["bands_as_of"], BANDS_AS_OF)
        self.assertIn(WINDOW_SOURCE, util["window_provenance"])
        self.assertIn("documented", util["window_provenance"])
        self.assertIn("product-owner judgment", util["band_provenance"])
        self.assertIn("not an Anthropic recommendation", util["band_provenance"])

    def test_the_provenances_are_stated_once_and_not_per_scope(self) -> None:
        # Which window a model has, and where the boundaries sit, are facts
        # about `context_window.py` -- not about a scope. A per-scope copy
        # would assert they could differ by scope, and would be a second place
        # for one of the two dates to go stale in.
        for tally in self.util(SU_BUSY_DAY)["by_scope"]:
            for field in (
                "windows_as_of", "window_provenance", "bands_as_of",
                "band_provenance",
            ):
                with self.subTest(scope=tally["scope"], field=field):
                    self.assertNotIn(field, tally)

    def test_every_scope_carries_the_whole_band_table_labels_and_all(self) -> None:
        # A declarative template renders these lists as-is, so a scoped band
        # that lost its label or its boundaries would render a row the page
        # would have to reconstruct -- and reconstructing it is exactly the
        # copy of the judgment `context_window.py` owns.
        for tally in self.util(SU_BUSY_DAY)["by_scope"]:
            with self.subTest(scope=tally["scope"]):
                self.assertEqual(
                    [
                        (b["band"], b["label"], b["lower"], b["upper"])
                        for b in tally["bands"]
                    ],
                    [(key, label, lower, upper) for key, label, lower, upper in BANDS],
                )

    def test_the_split_is_window_scoped_like_every_other_figure(self) -> None:
        self.assertNotEqual(self.util(SU_BUSY_DAY), self.util(SU_MAIN_ONLY_DAY))
        self.assertNotEqual(self.util(SU_EMPTY_DAY), self.util(SU_BUSY_DAY))


class VendoredAssetTest(unittest.TestCase):
    """Every library the page runs is on this disk, pinned, and inert (#8).

    CLAUDE.md constraint 2 is not an availability preference. `index.html`
    renders the user's own prompts, file paths and source code, so a script
    fetched from a CDN at runtime is a privacy and supply-chain surface -- it
    sees the transcripts, and whoever controls it decides what it does with
    them. Chart.js was vendored for that reason and Alpine gets identical
    treatment.

    Four separable claims, one test each, because they fail independently:
    the bytes are the reviewed bytes, their provenance is written down, they
    contain no way to reach the network, and the page loads them from nowhere
    else.
    """

    ROOT = Path(__file__).resolve().parent.parent
    VENDOR = ROOT / "vendor"

    # Recorded on 2026-08-05 from the committed files; provenance for each is
    # in vendor/README.md. A bundle upgrade changes these deliberately.
    PINNED = {
        "alpine.min.js": (
            46346,
            "57b37d7cae9a27d965fdae4adcc844245dfdc407e655aee85dcfff3a08036a3f",
        ),
        "chart.umd.min.js": (
            205399,
            "d2af8974e95271638772e9e9524db5b9a6f58d6ec2d5d781400447b4a31c681e",
        ),
    }

    # Everything a script could use to originate a request. A vendored bundle
    # that contains none of these cannot phone home whatever it is asked to do.
    NETWORK_APIS = (
        "fetch(",
        "XMLHttpRequest",
        "importScripts",
        "navigator.sendBeacon",
        "WebSocket",
        "EventSource",
        "import(",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (cls.ROOT / "index.html").read_text()

    def bundles(self) -> list[Path]:
        return sorted(self.VENDOR.glob("*.js"))

    def test_the_bundles_on_disk_are_the_ones_that_were_reviewed(self) -> None:
        # A vendored file is code that runs in the user's browser over the
        # user's transcripts, and nothing else in this repository would notice
        # it being edited.
        found = {p.name for p in self.bundles()}
        self.assertEqual(found, set(self.PINNED), "vendor/ gained or lost a bundle")
        for name, (size, digest) in self.PINNED.items():
            with self.subTest(bundle=name):
                raw = (self.VENDOR / name).read_bytes()
                self.assertEqual(len(raw), size)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)

    def test_every_bundle_has_its_provenance_written_down(self) -> None:
        # "Where did this 46KB of minified code come from" must be answerable
        # from the tree, not from someone's memory (CLAUDE.md: if you add a
        # number, say where it came from and when it was checked).
        readme = (self.VENDOR / "README.md").read_text()
        for name, (_, digest) in self.PINNED.items():
            with self.subTest(bundle=name):
                self.assertIn(name, readme)
                self.assertIn(digest, readme)

    def test_no_vendored_bundle_can_reach_the_network(self) -> None:
        for path in self.bundles():
            source = path.read_text(errors="replace")
            for api in self.NETWORK_APIS:
                with self.subTest(bundle=path.name, api=api):
                    self.assertNotIn(api, source)

    def test_the_page_loads_its_scripts_only_from_vendor(self) -> None:
        srcs = re.findall(r"""<script[^>]*\ssrc=["']([^"']+)["']""", self.html)
        self.assertEqual(
            sorted(srcs),
            ["/vendor/alpine.min.js", "/vendor/chart.umd.min.js"],
            "a script in the shipped page is loaded from somewhere other than "
            "vendor/",
        )

    def test_no_remote_reference_survives_anywhere_in_the_page(self) -> None:
        # Wider than the <script> tags above: a stylesheet, a font, a
        # preconnect hint or a protocol-relative URL leaks just as much.
        for remote in ("http://", "https://", 'src="//', "href=\"//", "cdn",
                       "unpkg", "jsdelivr", "integrity=", "crossorigin"):
            with self.subTest(token=remote):
                self.assertNotIn(remote, self.html)

    def test_vendoring_added_no_build_step(self) -> None:
        # A package manifest anywhere would also fail the stdlib-only CI job,
        # which is the point: Alpine is one file, not a dependency tree.
        for manifest in ("package.json", "package-lock.json", "pyproject.toml",
                         "requirements.txt", "Pipfile", "setup.py"):
            with self.subTest(manifest=manifest):
                self.assertFalse((self.ROOT / manifest).exists())
                self.assertFalse((self.VENDOR / manifest).exists())
        self.assertFalse((self.ROOT / "node_modules").exists())


class VendorRouteTest(unittest.TestCase):
    """`/vendor/*` serves the new bundle through the existing, checked handler.

    #8 needed NO change to `serve.py`: `_serve_vendor_asset()` was already
    generic over the directory. These assertions are what makes that claim
    checkable rather than asserted -- Alpine really is reachable over the
    route, and the path-escape check that guards it still refuses to leave the
    directory now that there is a second file to ask for.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-vendor-route-test-"))
        projects = cls.tmp / "projects"
        projects.mkdir()
        shutil.copy(FIXTURE, projects / "session-fixture.jsonl")
        ingest(projects, cls.tmp / "usage.db")
        cls.api = Api(cls.tmp / "usage.db")
        cls.server = HTTPServer(("127.0.0.1", 0), make_handler(cls.api))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path: str) -> tuple[int, str, bytes]:
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            conn.request("GET", path)
            r = conn.getresponse()
            return r.status, r.getheader("Content-Type") or "", r.read()
        finally:
            conn.close()

    def test_alpine_is_served_byte_for_byte_as_javascript(self) -> None:
        status, ctype, body = self.get("/vendor/alpine.min.js")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "application/javascript")
        expected = (Path(__file__).resolve().parent.parent
                    / "vendor" / "alpine.min.js").read_bytes()
        self.assertEqual(body, expected)

    def test_the_page_itself_still_asks_for_exactly_that_path(self) -> None:
        # The route and the reference are two halves of one fact; a test that
        # fetches a path the page never requests proves nothing.
        _, _, page = self.get("/")
        self.assertIn(b'src="/vendor/alpine.min.js"', page)

    def test_the_escape_check_still_refuses_to_leave_the_vendor_directory(
        self,
    ) -> None:
        for escape in ("/vendor/../serve.py", "/vendor/../../etc/passwd",
                       "/vendor/nope.js"):
            with self.subTest(path=escape):
                status, _, _ = self.get(escape)
                self.assertEqual(status, 404)


class DeclarativeRenderLayerTest(unittest.TestCase):
    """The render layer is bindings, not string-built markup (#8).

    `index.html` used to assemble the DOM with 12 `innerHTML` assignments and a
    hand-written `esc()` on 29 interpolations. That is the shape that made the
    "computed but never rendered" defect possible -- a payload field could
    simply have no consumer -- and every one of those interpolations was one
    forgotten `esc()` away from executing a session transcript's contents as
    markup in the page that displays them.

    `x-text` sets textContent, so it escapes by construction: there is nothing
    to forget. These tests keep the property rather than the diff -- the ways
    back to string-built markup are named individually, because each of them
    reintroduces the same surface on its own.
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (cls.ROOT / "index.html").read_text()
        cls.html = strip_comments(cls.raw)

    def test_no_markup_is_built_from_strings_any_more(self) -> None:
        for api in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                    "document.write", "createElement", "createContextualFragment"):
            with self.subTest(api=api):
                self.assertNotIn(api, self.html)

    def test_the_hand_written_escaper_is_gone_and_not_needed(self) -> None:
        # `esc()` existed only because values were concatenated into markup.
        # Keeping it would be an invitation to concatenate again.
        self.assertNotIn("esc(", self.html)
        self.assertNotIn("function esc", self.html)

    def test_no_binding_interprets_a_payload_value_as_markup(self) -> None:
        # THE regression to fear: `x-html` is `innerHTML` with a shorter name,
        # and a turn preview is arbitrary text from the user's own transcript.
        self.assertNotIn("x-html", self.html)

    def test_the_page_reaches_the_dom_through_bindings_alone(self) -> None:
        # An imperative handle is how a second writer gets added later (see
        # BannerPrecedenceTest). There is exactly one component and no lookups.
        self.assertEqual(self.html.count("x-data="), 1)
        for lookup in ("getElementById", "querySelector", "addEventListener"):
            with self.subTest(dom_api=lookup):
                self.assertNotIn(lookup, self.html)

    # table id -> the state the rows must be iterated FROM.
    ROW_SOURCES = {
        "models": "shownModels",
        "sessions": "sessions",
        "agents": "agents",
        "outliers": "shownOutliers",
    }
    # #70: two of those are DERIVED -- a deep link filters them -- so the
    # source above is a getter rather than the payload itself. That indirection
    # is exactly how "iterating a literal []" would come back wearing a
    # respectable name, so each derived source is pinned to the state it must
    # read. Getter -> the payload it derives from.
    DERIVED_ROW_SOURCES = {
        "shownModels": "this.summary.models",
        "shownOutliers": "this.outliers",
    }

    def test_every_table_body_is_produced_by_iterating_the_payload(self) -> None:
        # This is the structural half of the "computed but never rendered"
        # fix, and the reason SummaryPayloadIsWiredTest could be narrowed to
        # the summary scalars: a row payload is iterated, so a column cannot
        # exist without markup naming the field it shows.
        #
        # The SOURCE is asserted, not merely the presence of an `x-for`.
        # Iterating a literal `[]` is a table that renders nothing while every
        # field name it mentions still looks wired -- which is the exact defect
        # (`summary.models`) this whole guard was built for.
        for table_id, source in self.ROW_SOURCES.items():
            with self.subTest(table=table_id):
                table = html_element(self.raw, f'id="{table_id}"')
                loop = re.search(r'<template x-for="([^"]+)"', table)
                self.assertIsNotNone(loop, f"#{table_id} builds its rows some other way")
                self.assertRegex(
                    loop.group(1),
                    rf"\bin\s+.*\b{re.escape(source)}\b",
                    f"#{table_id} does not iterate {source}",
                )

    def test_a_derived_row_source_still_reads_the_payload(self) -> None:
        # The hop, checked. A filtered table iterates a getter, and a getter
        # that stopped reading the payload would render nothing while every
        # field it mentions still looked wired -- which is the original defect
        # (`summary.models`) with one more step in front of it.
        for getter, source in self.DERIVED_ROW_SOURCES.items():
            with self.subTest(getter=getter):
                self.assertIn(source, js_function_body(self.html, f"get {getter}("))

    def test_a_not_yet_loaded_table_is_not_an_empty_one(self) -> None:
        # Three states, not two. "No sessions in this period" is a claim about
        # the window and must not be made before the window has been fetched,
        # so each list starts as null rather than [].
        component = js_function_body(self.html, "function report(")
        for field in ("sessions", "agents", "outliers", "summary", "detail"):
            with self.subTest(field=field):
                self.assertRegex(
                    component, re.compile(rf"^\s*{field}: null,$", re.M)
                )


class TurnTypeChartColourTest(unittest.TestCase):
    """The stack needs more colours than the chart can produce series.

    `TT_COLORS` is cycled with `i % TT_COLORS.length`, so a list shorter than
    the key count gives two series the same colour -- and a stacked bar chart
    with two identically coloured bands does not look broken, it looks like one
    band. The by-turn-type breakdown can emit 11 keys: `turns.turn_type` has 9
    values, plus "(no turn)" for calls with no main-thread turn, plus the
    "subagent" scope bucket those calls get instead.

    Pinned here because the count is a REQUIREMENT of the chart code and was
    only ever recorded in a comment beside it, which is how it came to be too
    short once already.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = strip_comments(
            (Path(__file__).resolve().parent.parent / "index.html").read_text()
        )

    def colours(self) -> list[str]:
        block = re.search(r"const TT_COLORS = \[(.*?)\];", self.html, re.S)
        self.assertIsNotNone(block, "TT_COLORS is gone")
        return re.findall(r"#[0-9a-fA-F]{6}", block.group(1))

    def test_there_are_at_least_as_many_colours_as_reachable_series(self) -> None:
        self.assertGreaterEqual(len(self.colours()), 11)

    def test_no_two_series_can_be_handed_the_same_colour(self) -> None:
        colours = self.colours()
        self.assertEqual(
            len(set(colours)), len(colours), "a duplicate colour in the cycle"
        )


class AbsenceIsNeverRenderedAsAValueTest(unittest.TestCase):
    """`null` renders as "—", never "0" -- the repository's central rule.

    A real 0 is a healthy sample; no sample at all is not. The page is where
    the two become indistinguishable if a formatter is careless, because both
    end up as characters in the same column. Every formatter must therefore
    answer the absence BEFORE it does any arithmetic -- `String(null)` and
    `(null).toLocaleString()` are the two ways a missing measurement acquires a
    plausible value.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = strip_comments(
            (Path(__file__).resolve().parent.parent / "index.html").read_text()
        )

    # formatter -> the first token of the arithmetic the guard must precede.
    FORMATTERS = {
        "function fmtTok(": "1e9",
        "function fmtCount(": "toLocaleString",
        "function fmtTs(": "new Date",
        "function fmtAge(": "fmtSpan",
        # #31's three. `fmtPct` is the one the band shares run through, and
        # `share` is null -- never 0 -- for a band over an empty set: "0.0% of
        # calls are in this band" is a measurement nobody made.
        "function fmtPct(": "* 100",
        "function fmtRatio(": "toFixed",
        # And the sharpest of them: `Math.round(null)` is 0, so rounding before
        # checking turns a window that measured nothing into a measured zero.
        "function fmtTokRounded(": "Math.round",
    }

    def test_each_formatter_answers_absence_before_it_computes(self) -> None:
        for decl, arithmetic in self.FORMATTERS.items():
            with self.subTest(formatter=decl):
                body = js_function_body(self.html, decl)
                guard = re.search(
                    r"if \((\w+) === null \|\| \1 === undefined\) return \"—\";",
                    body,
                )
                self.assertIsNotNone(
                    guard,
                    f"{decl} does not refuse a missing measurement outright",
                )
                self.assertLess(
                    guard.start(),
                    body.index(arithmetic),
                    f"{decl} computes over the value before checking it is one",
                )

    def test_the_dash_is_never_a_zero(self) -> None:
        # The mutation this exists to catch, stated as an absence: no formatter
        # may return a numeric string for a missing value.
        for decl in self.FORMATTERS:
            with self.subTest(formatter=decl):
                body = js_function_body(self.html, decl)
                self.assertNotRegex(body, r"=== null \|\| \w+ === undefined\) return \"0")

    def test_an_absent_label_falls_back_to_the_dash_too(self) -> None:
        # `orDash` is the non-numeric half: an unmeasured dispatch has no model
        # name, and an empty cell reads as "no model" rather than "not known".
        self.assertIn('return v ?? "—";', js_function_body(self.html, "function orDash("))


# The loop #61's render half turns on, spelled once: the tests below assert
# both that it EXISTS and that certain things sit outside it, and two spellings
# of it would let one of those pass against a loop the other never saw.
SCOPE_LOOP_EXPR = "s in summary.context.utilisation.by_scope"
SCOPE_LOOP = f'x-for="{SCOPE_LOOP_EXPR}"'


class ContextReferentIsBoundTest(unittest.TestCase):
    """#31's `context` block reaches the reader, and keeps its two voices (#8).

    The data layer landed first, deliberately, with `context` declared in
    `SummaryPayloadIsWiredTest.NOT_RENDERED` as a field OWED a consumer. This
    is the change that owes it, so the entry is gone and these tests are what
    replace it -- an allowlist entry says "someone decided"; these say what was
    decided.

    The half that is not about wiring at all is the PROVENANCE. The denominator
    is Anthropic's documented context window. Where the band boundaries sit is
    a dated product-owner judgment about which Anthropic publishes nothing. The
    API carries them as two fields with two dates precisely so the page cannot
    present the second in the first's voice, and a page that merged them would
    borrow an authority this project has not earned -- undoing the care the
    data layer took, invisibly, because the merged sentence still reads true.
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (cls.ROOT / "index.html").read_text()
        cls.html = strip_comments(cls.raw)
        cls.band = html_element(cls.raw, 'id="context-note"')
        cls.cards = js_function_body(cls.html, "get cards(")
        cls.sample_line = js_function_body(cls.html, "get contextSampleLine(")
        # The per-scope block ALONE (#61), so a test about what belongs inside
        # the loop cannot be satisfied by something that sits beside it.
        cls.scope_loop = html_element(cls.raw, SCOPE_LOOP)

    # --- the median displaced the mean, rather than joining it ---

    def test_the_card_leads_with_the_median(self) -> None:
        self.assertIn("summary.context.median", self.cards)
        self.assertIn("Median context/call", self.cards)

    def test_the_mean_is_no_longer_a_headline_card(self) -> None:
        # "Displaced", not "added beside". A mean 1.5x its median, which 71% of
        # calls fall below, is not a card -- it is evidence of skew, and it
        # belongs where the skew is being argued.
        self.assertNotIn("Avg context/call", self.cards)
        self.assertNotIn("context.mean", self.cards)
        self.assertIn("summary.context.mean", self.band)

    def test_the_window_average_is_shown_as_the_same_figure_not_a_rival(
        self,
    ) -> None:
        # INVERTED BY #25, deliberately. This used to assert the page called
        # `avg_context` "a different figure over a different set" -- true while
        # it divided the same tokens across every row including those that
        # reported no prompt accounting, and the gap between it and
        # `context.mean` WAS that defect made visible.
        #
        # The defect is fixed: both run through `has_context_measurement()`, so
        # they are the same number over the same sample and `serve.py` asserts
        # it. A page still claiming they differ would be asserting a
        # discrepancy that no longer exists -- which is the same failure as
        # rendering a figure nobody measured, pointed the other way.
        self.assertIn("summary.avg_context", self.band)
        self.assertNotIn("summary.avg_context", self.cards)
        self.assertIn("the same", self.band)
        for stale in ("different set", "divides the same tokens across EVERY row"):
            with self.subTest(claim=stale):
                self.assertNotIn(stale, self.band)

    def test_the_legacy_average_cannot_round_an_absence_into_a_zero(self) -> None:
        # `Math.round(null)` is 0. The card used to be guarded by a `noData`
        # flag computed elsewhere; the formatter now refuses on its own, so the
        # guard cannot be left behind when the binding moves.
        self.assertRegex(self.band, r"fmtTokRounded\(summary\.avg_context\)")
        self.assertNotRegex(self.band, r"Math\.round\(summary\.avg_context\)")

    # --- the two provenances ---

    def test_both_provenances_are_rendered(self) -> None:
        for field in ("window_provenance", "band_provenance"):
            with self.subTest(field=field):
                self.assertIn(f"utilisation.{field}", self.band)

    def test_no_single_binding_carries_both_provenances(self) -> None:
        # The failure mode is one sentence built from the two, which reads as
        # though Anthropic published the boundaries.
        for expr in re.findall(r'x-text="([^"]+)"', self.band):
            with self.subTest(expr=expr):
                self.assertFalse(
                    "window_provenance" in expr and "band_provenance" in expr,
                    "one binding renders both provenances as a single claim",
                )

    def test_the_two_provenances_sit_in_separately_marked_elements(self) -> None:
        # Rendered apart is not enough -- they must LOOK like different kinds
        # of claim. The judged one carries a marker class the documented one
        # does not, and that class has a rule of its own in the stylesheet.
        judged = re.search(
            r'<span class="([^"]*context-judged[^"]*)"[^>]*>\s*Boundaries:', self.band
        )
        self.assertIsNotNone(
            judged, "the judged boundaries are not visually distinguished"
        )
        documented = re.search(r'<span class="([^"]*)"[^>]*>\s*Denominator:', self.band)
        self.assertIsNotNone(documented, "the documented denominator is not led as one")
        self.assertNotIn(
            "context-judged",
            documented.group(1),
            "the documented window is marked as a judgment",
        )
        self.assertRegex(self.html, r"\.context-judged\s*\{[^}]+\}")

    def test_each_provenance_is_led_by_what_kind_of_claim_it_is(self) -> None:
        # A reader skimming must be able to tell which is which without
        # parsing either sentence.
        denominator = self.band.index("Denominator:")
        boundaries = self.band.index("Boundaries:")
        self.assertLess(
            denominator,
            self.band.index("window_provenance"),
            "the documented window's lead does not precede it",
        )
        self.assertLess(
            boundaries,
            self.band.index("band_provenance"),
            "the judged boundaries' lead does not precede them",
        )

    # --- the bands ---

    def test_the_bands_are_iterated_from_the_payload(self) -> None:
        # Structural, like every other row payload on this page: the page
        # cannot show a band the API did not send, and cannot omit one it did.
        # TWO band loops since #61 -- one per scope and one pooled -- and both
        # iterate a list the API sent rather than a list the page assembled.
        #
        # FOUR loops since #88, not two: each of the two band lists is iterated
        # once for its METER and once for its LEGEND, and the pair is the point
        # rather than a duplication. A segment is drawn only for a band with a
        # nonzero share, so a meter alone could not state a band that measured
        # zero -- and "0 calls in this band" is a measurement, not an absence.
        # The legend carries it, muted, beside the picture that omits it.
        loops = re.findall(r'<template x-for="([^"]+)"', self.band)
        self.assertEqual(
            [loop for loop in loops if "bands" in loop],
            [
                "(b, i) in s.bands",
                "(b, i) in s.bands",
                "(b, i) in summary.context.utilisation.bands",
                "(b, i) in summary.context.utilisation.bands",
            ],
            "a band list is built some other way, or one of the four is gone",
        )

    def test_the_bands_lead_with_the_scope_split(self) -> None:
        # #61: the FIRST thing under this heading is the scoped tally. On this
        # project's own transcripts the pooled banner read "52 (1.9%) at 90%+",
        # a non-event; the same calls scoped read main-thread 52 of 390 (13.3%)
        # and subagent 0 of 2,332. Every red-band call was the orchestrating
        # thread. Order is the whole difference between a figure that prompts
        # an action and one that prompts none, so it is pinned.
        loops = re.findall(r'<template x-for="([^"]+)"', self.band)
        self.assertEqual(loops[0], SCOPE_LOOP_EXPR, "the pooled tally leads")
        self.assertLess(
            self.band.index(SCOPE_LOOP_EXPR),
            self.band.index("summary.context.utilisation.includes"),
            "the pooled figure is not demoted below the split it hides",
        )

    def test_the_pooled_tally_names_the_set_it_spans(self) -> None:
        # Kept, not deleted -- it is the honest answer to "of every call in
        # this window" and the denominator the partition is checked against.
        # What was wrong with it was that it did not NAME its set: 2,332
        # subagent calls dilute 390 main-thread ones 6:1 in it.
        self.assertIn('x-text="summary.context.utilisation.includes"', self.band)

    def test_each_scope_states_its_own_name_and_its_own_denominator(self) -> None:
        # An aggregate must name the set it ranges over, and a share of the
        # BANDED calls is neither a share of the sample nor of the window's
        # calls. Both come from the scope's own row, never from the pooled one.
        for field in ("banded_calls", "calls", "sample_calls"):
            with self.subTest(field=field):
                self.assertIn(f"s.{field}", self.scope_loop)

    def test_the_scope_name_leads_the_line_rather_than_only_appearing_on_it(
        self,
    ) -> None:
        # TIGHTENED, because the assertion above did not pin what its name
        # claimed. `s.scope` was asserted to appear ANYWHERE in the loop, and
        # the #70 drill-through button at the foot of that same loop renders it
        # too ("See the models <s.scope> calls ran on ->") -- so deleting the
        # LEAD left the suite green. Nothing was wrong on the page; the guard
        # was.
        #
        # Two renderings serve two purposes and only one of them names the
        # figure's set: a scope named only inside a link to another view is a
        # tally whose denominator the reader must click away to learn. So the
        # lead is pinned as ITSELF -- a marked element, asserted to come before
        # any band, count or drill-down on the line -- rather than by counting
        # occurrences, which would be the same weakness with a bigger number.
        lead = re.search(
            r'<strong class="scope-lead" x-text="s\.scope"></strong>',
            self.scope_loop,
        )
        self.assertIsNotNone(
            lead,
            "the per-scope line does not lead with the scope's own name; a "
            "figure whose set is named only in a link to another view has not "
            "named its set",
        )
        for later in ("s.banded_calls", "s.no_sample_reason", "showPanel("):
            with self.subTest(after=later):
                self.assertLess(
                    lead.start(),
                    self.scope_loop.index(later),
                    "the scope name does not come first on its own line",
                )

    def test_a_scope_with_no_banded_sample_says_why_instead_of_drawing_zeroes(
        self,
    ) -> None:
        # THE rule this block turns on: a real 0 and "no sample" must not
        # render alike. A scope that ran calls with none in a band shows 0; a
        # scope with no banded sample shows the API's reason. Its `share` is
        # null, not 0.0, and four bands of "0.0%" would state a measurement
        # nobody made -- so the chips live under the NEGATIVE branch, which is
        # asserted by position rather than by their mere presence.
        self.assertIn('x-if="s.no_sample_reason"', self.scope_loop)
        self.assertIn('x-if="!s.no_sample_reason"', self.scope_loop)
        self.assertLess(
            self.scope_loop.index('x-if="!s.no_sample_reason"'),
            self.scope_loop.index("(b, i) in s.bands"),
            "the meter is drawn outside the branch that establishes a sample",
        )
        self.assertIn('x-text="s.no_sample_reason"', self.scope_loop)

    def test_every_caveat_is_counted_per_scope_and_not_only_pooled(self) -> None:
        # The three ways a call leaves the bands are scope facts too: a pooled
        # count of them cannot say which scope to look at, which is the same
        # defect as the pooled share one line up.
        for field in (
            "unmeasured_calls", "unknown_model_calls", "unknown_models",
            "over_window_calls",
        ):
            with self.subTest(field=field):
                self.assertIn(f"s.{field}", self.scope_loop)

    def test_the_provenances_stay_outside_the_scope_loop(self) -> None:
        # Which window a model has, and where the boundaries sit, are
        # properties of `context_window.py` -- not of a scope. Rendered inside
        # this loop they would be stated once per scope, implying they could
        # differ by one, and would put two copies of each date on the page.
        # The API refuses to carry them per scope
        # (`test_the_provenances_are_stated_once_and_not_per_scope`); this is
        # the same refusal one layer up.
        for field in ("window_provenance", "band_provenance"):
            with self.subTest(field=field):
                self.assertNotIn(field, self.scope_loop)
                self.assertEqual(self.band.count(f'utilisation.{field}"'), 1)

    def test_the_judged_boundaries_are_shown_and_not_just_their_verdict(self) -> None:
        # Where a band is CUT is the judged half of the two provenances, and
        # the page rendered only the verdict it produces. The chip now carries
        # the boundary itself, read off the payload rather than copied here.
        self.assertIn(":title=\"bandRange(b)\"", self.band)
        body = js_function_body(self.html, "function bandRange(")
        for field in ("b.lower", "b.upper"):
            with self.subTest(field=field):
                self.assertIn(field, body)

    def test_the_open_top_band_is_not_rendered_as_an_absence(self) -> None:
        # `upper` is null on the top band because it has NO upper bound, which
        # is a fact about the band -- not a measurement nobody made. Run
        # through `fmtPct` it would print the em-dash this page reserves for
        # the other kind of absence, so the two must not print alike.
        body = js_function_body(self.html, "function bandRange(")
        self.assertLess(
            body.index("b.upper === null"),
            body.index("fmtPct(b.upper)"),
            "a null upper bound is formatted before it is recognised",
        )
        self.assertIn("and above", body)

    def test_both_dates_ride_with_the_figures(self) -> None:
        # The two sentences stay at the foot of the band in their two voices;
        # the two DATES sit with the numbers they qualify, so a reader weighing
        # a band never has to hunt for how old the window table is or when the
        # boundaries were last judged.
        for field in ("windows_as_of", "bands_as_of"):
            with self.subTest(field=field):
                self.assertIn(f'x-text="summary.context.utilisation.{field}"', self.band)

    def test_the_page_invents_no_band_of_its_own(self) -> None:
        # Boundaries, labels and verdicts all belong to `context_window.py`,
        # which is where the judgment is dated. A copy here would be a second
        # place to change and a second thing to forget -- and only one of the
        # two would carry `BANDS_AS_OF`.
        for key, label, lower, _upper in BANDS:
            with self.subTest(band=key):
                self.assertNotIn(key, self.html)
                self.assertNotIn(label, self.html)
                self.assertNotIn(f"{lower}", self.band)

    def test_a_band_share_of_nothing_is_not_a_share_of_zero(self) -> None:
        # `share` is null for every band when nothing was banded. Rendered as
        # "0.0%" that reads as a measured absence of calls in each band, which
        # is the exact substitution this repository refuses.
        self.assertIn("fmtPct(b.share)", self.band)
        self.assertNotRegex(self.band, r"b\.share\s*\*")

    def test_the_shares_name_the_set_they_are_shares_of(self) -> None:
        # An aggregate must name the set it ranges over: bands are shares of
        # the BANDED calls, which is neither the sample nor the window's calls.
        self.assertIn("utilisation.banded_calls", self.band)

    # --- the absences the block is careful about ---

    def test_an_unwindowed_model_is_named_not_merely_counted(self) -> None:
        # "3 calls could not be banded" is unactionable; naming the model tells
        # the reader whether the table is stale or the id is exotic.
        self.assertIn("utilisation.unknown_model_calls", self.band)
        self.assertIn("utilisation.unknown_models", self.band)
        self.assertIn("UNKNOWN, not low", self.band)

    def test_calls_over_their_own_window_are_surfaced_as_inconclusive(self) -> None:
        # The loud half of the hand-maintained table's safety story: a stale
        # window shows up as utilisation above 100%, and it is only a safety
        # story if the page says so.
        self.assertIn("utilisation.over_window_calls", self.band)
        self.assertIn("INCONCLUSIVE", self.band)

    def test_a_call_with_no_context_accounting_is_counted_not_banded(self) -> None:
        self.assertIn("context.unmeasured_calls", self.band)

    def test_an_empty_sample_says_so_instead_of_printing_dashes(self) -> None:
        # A window with no measured context renders one sentence, not a median
        # of "—" beside four bands of "—": a screen of dashes reads as breakage
        # rather than as the honest absence it is.
        self.assertRegex(self.band, r'x-if="summary && !summary\.context\.sample_calls"')
        self.assertRegex(self.band, r'x-if="summary && summary\.context\.sample_calls"')

    def test_the_sample_names_itself_from_the_api(self) -> None:
        # `sample_is` exists so the page does not invent its own words for what
        # was counted. An aggregate must name the set it ranges over, and the
        # set is named once, server-side.
        #
        # Read from the sample line rather than the card body: #25 gave the
        # line two counts to state as well as a name, so it moved into a getter
        # of its own. The property being asserted is that the WORDS come from
        # the API, which is about where they originate, not where they are
        # assembled.
        self.assertIn("context.sample_is", self.sample_line)

    def test_the_sample_line_states_the_measured_count_and_the_excluded_one(
        self,
    ) -> None:
        # #25: `avg_context` and the median both range over a strictly smaller
        # set than `calls`, and the card must say by how much. Bound to the
        # TOP-LEVEL pair, which is the one partitioned against the card's own
        # call count -- `context_calls + unmeasured_calls == calls`.
        # The RENDERED form, not the bare property: `unmeasured_calls` also
        # appears as the condition deciding whether to mention it at all, so
        # matching the property name alone passed a mutation that kept the test
        # and dropped the words.
        for field in ("context_calls", "unmeasured_calls"):
            with self.subTest(field=field):
                self.assertIn(
                    f"fmtCount(this.summary.{field})",
                    self.sample_line,
                    f"{field} is consulted but never stated",
                )

    def test_the_sample_line_actually_reaches_the_card(self) -> None:
        # `SummaryPayloadIsWiredTest` greps the whole page for a reference, so a
        # getter that nothing renders satisfies it -- the "computed but never
        # rendered" shape, one level up from the one that test was built for.
        # Mutation-checked: deleting the card's `note` leaves that test green.
        self.assertIn("this.contextSampleLine", self.cards)

    def test_the_excluded_count_is_stated_only_when_there_is_one(self) -> None:
        # A window where every call was measured must not grow a permanent
        # "0 measured nothing" clause: a notice that never varies is a notice
        # nobody reads, and this one would be announcing a healthy state.
        self.assertRegex(
            self.sample_line, r"summary\.unmeasured_calls\s*\n?\s*\?"
        )

    def test_the_annotation_adds_no_panel(self) -> None:
        # CLAUDE.md constraint 4. #88 turned this from a note-band annotating a
        # card deck into question card TWO, which is a change of LEVEL and not
        # an appended surface: the deck it used to annotate moved to the detail
        # view in the same change, and `ReportViewSplitTest` pins that the net
        # count did not rise. What must still hold is that the overview's idiom
        # is not the detail view's -- `.panel` is what the tables and the chart
        # wear, and its count is unchanged -- and that this card is still not a
        # table.
        self.assertRegex(self.raw, r'<section class="q" id="context-note">')
        self.assertNotIn("<table", self.band)
        self.assertEqual(self.html.count('class="panel"'), 6, "a panel was added")


# ---------------------------------------------------------------------------
# #25: a call carrying no measurement is not a call that measured zero.
# ---------------------------------------------------------------------------
#
# The fixture is built so that a wrong guard cannot pass, in BOTH directions:
#
#   * three rows carry NO measurement -- every token class 0. Two of them are
#     written with the four keys PRESENT and valued 0, which is what the
#     records on disk actually look like (measured 2026-08-05: all 82 such
#     rows in a local corpus carry a complete `usage` block whose token keys
#     are present and zero), and one with an empty `usage` object, so the two
#     shapes cannot be handled differently;
#   * one row is a GENUINE MEASURED ZERO -- `output_tokens: 0` beside a real
#     60,000-token context -- and it sits ALONE in its own day, so a guard
#     that suppressed every zero would turn that day INCONCLUSIVE and go red;
#   * one day's ONLY call is a no-measurement row (the narrow window the issue
#     observed in the chart: `calls: 1, avg_context: 0`, read by a reader as a
#     context collapse);
#   * every mean the five call sites produce is a DIFFERENT number
#     (52,250 / 36,333 / 100,000 / 66,667 / 50,000 / 9,000 / 24,500 / 60,000),
#     so a site reading another site's set cannot pass;
#   * the unmeasured rows carry BOTH a real model id and `<synthetic>`, so a
#     guard that keyed on the model rather than on the measurement is red.
NM_BUSY = "m25-busy"
NM_LONELY = "m25-lonely"
NM_MIXED_DAY = "2026-05-11"       # measured + unmeasured, main + subagent
NM_REAL_ZERO_DAY = "2026-05-12"   # one call: a genuine measured zero output
NM_BLIND_DAY = "2026-05-13"       # one call: no measurement at all
NM_MEASURED_DAY = "2026-05-14"    # one call, fully measured
NM_MODEL = "claude-opus-5-20260101"
NM_SUB_MODEL = "claude-haiku-4-5-20251001"
NM_SYNTHETIC_MODEL = "<synthetic>"

# The three token classes are deliberately unequal within every call, so a
# swapped column mapping cannot reproduce the context.
NM_A_CONTEXT = 40_000    # 7,000 + 11,000 + 22,000
NM_C_CONTEXT = 60_000    # 5,000 + 13,000 + 42,000, output_tokens 0
NM_E_CONTEXT = 9_000     # 1,000 + 2,000 + 6,000   (subagent)
NM_G_CONTEXT = 100_000   # 3,000 + 17,000 + 80,000

NM_TOTAL_CALLS = 7
NM_MEASURED_CALLS = 4
NM_UNMEASURED_CALLS = 3
# Hand-written, then checked against the parts above -- a figure derived only
# from the fixture would agree with a fixture that had drifted.
NM_CORPUS_AVG = 209_000 / 4                              # 52,250.0
NM_CORPUS_AVG_IF_UNGUARDED = 209_000 / 7                 # 29,857.1
NM_BUSY_AVG = (NM_A_CONTEXT + NM_C_CONTEXT + NM_E_CONTEXT) / 3   # 36,333.3
NM_LONELY_AVG = float(NM_G_CONTEXT)                      # 100,000.0
NM_MAIN_AVG = (NM_A_CONTEXT + NM_C_CONTEXT + NM_G_CONTEXT) / 3   # 66,666.7
NM_SUBAGENT_AVG = float(NM_E_CONTEXT)                    # 9,000.0
NM_BUSY_MAIN_AVG = (NM_A_CONTEXT + NM_C_CONTEXT) / 2     # 50,000.0
NM_MIXED_DAY_AVG = (NM_A_CONTEXT + NM_E_CONTEXT) / 2     # 24,500.0


def build_no_measurement_corpus(root: Path) -> Path:
    """Two sessions across four days, one of which measures nothing at all."""
    project = root / "projects" / "-fixture-no-measurement"
    project.mkdir(parents=True)

    def record(
        session: str,
        day: str,
        minute: int,
        model: str,
        usage: dict[str, int],
        *,
        agent_id: str | None = None,
    ) -> str:
        message: dict[str, object] = {
            "id": f"msg-{session}-{day}-{minute}",
            "model": model,
            "usage": usage,
            "content": [{"type": "text", "text": f"{session} {day} {minute}"}],
        }
        payload: dict[str, object] = {
            "type": "assistant",
            "sessionId": session,
            "timestamp": f"{day}T15:{minute:02d}:00.000Z",
            "isSidechain": agent_id is not None,
            "message": message,
        }
        if agent_id is not None:
            payload["agentId"] = agent_id
        return json.dumps(payload) + "\n"

    def measured(input_t: int, write: int, read: int, output: int) -> dict[str, int]:
        return {
            "input_tokens": input_t,
            "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read,
            "output_tokens": output,
        }

    # What the records on disk carry: the keys are PRESENT and valued 0.
    zeros = measured(0, 0, 0, 0)

    busy = [
        # A -- measured, on the mixed day.
        record(NM_BUSY, NM_MIXED_DAY, 1, NM_MODEL, measured(7_000, 11_000, 22_000, 13)),
        # B -- no measurement, on the mixed day, under a REAL model id.
        record(NM_BUSY, NM_MIXED_DAY, 2, NM_MODEL, zeros),
        # C -- a GENUINE measured zero: no output tokens, a real 60k context,
        # alone in its day.
        record(
            NM_BUSY, NM_REAL_ZERO_DAY, 3, NM_MODEL, measured(5_000, 13_000, 42_000, 0)
        ),
    ]
    (project / f"{NM_BUSY}.jsonl").write_text("".join(busy))

    subagents = project / NM_BUSY / "subagents"
    subagents.mkdir(parents=True)
    sub = [
        # E -- measured subagent call, on the mixed day.
        record(
            NM_BUSY, NM_MIXED_DAY, 4, NM_SUB_MODEL,
            measured(1_000, 2_000, 6_000, 3), agent_id="agent-nm25a",
        ),
        # F -- the OTHER absence shape: an empty `usage` object. It must be
        # read exactly as B and D are.
        record(NM_BUSY, NM_MIXED_DAY, 5, NM_SUB_MODEL, {}, agent_id="agent-nm25a"),
    ]
    (subagents / "agent-nm25a.jsonl").write_text("".join(sub))

    lonely = [
        # D -- the narrow window: this day's ONLY call, measuring nothing,
        # under the model id 76 of the 82 corpus rows carry.
        record(NM_LONELY, NM_BLIND_DAY, 6, NM_SYNTHETIC_MODEL, zeros),
        # G -- measured, alone in its day.
        record(
            NM_LONELY, NM_MEASURED_DAY, 7, NM_MODEL,
            measured(3_000, 17_000, 80_000, 5),
        ),
    ]
    (project / f"{NM_LONELY}.jsonl").write_text("".join(lonely))
    return project


class NoMeasurementIsNotAMeasuredZeroTest(unittest.TestCase):
    """#25: every context mean ranges over the calls that carry a measurement.

    A call with no usage measurement is still COUNTED as a call -- it happened
    -- and is kept out of the mean, with the two sample counts published beside
    the aggregate so a reader can see what it ranged over. A window whose only
    calls carry no measurement reports no average at all rather than 0.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-nomeasure-test-"))
        projects = build_no_measurement_corpus(cls.tmp)
        db_path = cls.tmp / "usage.db"
        ingest(projects, db_path, tasks_dir=cls.tmp / "no-task-index")
        cls.api = Api(db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def all_time(self) -> tuple[float, float]:
        return day_bounds(None, None)

    def day(self, day: str) -> tuple[float, float]:
        return day_bounds(day, day)

    def test_the_parser_stored_the_rows_faithfully(self) -> None:
        # The blocking question this issue carried for a year, pinned as a
        # test: ingest is NOT losing a `usage` block. It stores what is on
        # disk -- seven calls, three of which report every token class as 0 --
        # and the defect is downstream, in what the aggregates do with them.
        rows = self.api.conn.execute(
            "SELECT input_tokens, cache_read, cache_write, output_tokens,"
            " context_size FROM api_calls ORDER BY ts, id"
        ).fetchall()
        self.assertEqual(len(rows), NM_TOTAL_CALLS)
        blank = [r for r in rows if r["context_size"] == 0]
        self.assertEqual(len(blank), NM_UNMEASURED_CALLS)
        for r in blank:
            with self.subTest(row=dict(r)):
                self.assertEqual(
                    (r["input_tokens"], r["cache_read"], r["cache_write"],
                     r["output_tokens"]),
                    (0, 0, 0, 0),
                )
        # And the genuine measured zero is stored as a zero OUTPUT beside a
        # real context -- not as an absence.
        real_zero = [
            r for r in rows
            if r["context_size"] == NM_C_CONTEXT and r["output_tokens"] == 0
        ]
        self.assertEqual(len(real_zero), 1)

    def test_the_two_definitions_of_a_measurement_cannot_drift_apart(self) -> None:
        # SQL and Python each need their own spelling of the predicate; two
        # spellings free to disagree is how this class of defect recurs. They
        # are derived from one threshold, and this asserts they select the
        # same rows over the same table.
        by_sql = {
            r["id"]
            for r in self.api.conn.execute(
                f"SELECT id FROM api_calls WHERE {measured_context_sql()}"
            )
        }
        by_python = {
            r["id"]
            for r in self.api.conn.execute("SELECT id, context_size FROM api_calls")
            if has_context_measurement(r["context_size"])
        }
        self.assertEqual(by_sql, by_python)
        self.assertEqual(len(by_sql), NM_MEASURED_CALLS)
        # Aliased for a joined query, the predicate names the same rows.
        aliased = {
            r["id"]
            for r in self.api.conn.execute(
                "SELECT a.id FROM api_calls a"
                f" WHERE {measured_context_sql('a.context_size')}"
            )
        }
        self.assertEqual(aliased, by_sql)

    def test_a_genuine_measured_zero_is_below_the_threshold_of_nothing(self) -> None:
        # The over-correction guard, at the predicate itself. A context of 0
        # is no prompt accounting at all; ONE token is the smallest thing that
        # can be measured, and it is a measurement.
        self.assertEqual(MEASURED_CONTEXT_MIN, 1)
        self.assertTrue(has_context_measurement(MEASURED_CONTEXT_MIN))
        self.assertFalse(has_context_measurement(0))
        # A negative context cannot come from summed token counts, so a row
        # carrying one is broken -- and the one thing it must not do is join
        # the sample as the most frugal call on the report.
        self.assertFalse(has_context_measurement(-1))

    def test_summary_averages_the_measured_calls_and_counts_the_rest(self) -> None:
        s = self.api.summary(*self.all_time())
        self.assertEqual(s["calls"], NM_TOTAL_CALLS)
        self.assertAlmostEqual(s["avg_context"], NM_CORPUS_AVG)
        self.assertEqual(s["context_calls"], NM_MEASURED_CALLS)
        self.assertEqual(s["unmeasured_calls"], NM_UNMEASURED_CALLS)
        # The defect, named: the same tokens over every row rather than over
        # the rows that carry one.
        self.assertNotAlmostEqual(s["avg_context"], NM_CORPUS_AVG_IF_UNGUARDED)

    def test_summary_agrees_with_the_context_block_it_carries(self) -> None:
        # One definition of "measured", so the card's mean and the block's
        # mean are the same number over the same set. They were deliberately
        # unequal before this fix, and that inequality WAS the defect.
        s = self.api.summary(*self.all_time())
        block = s["context"]
        self.assertAlmostEqual(s["avg_context"], block["mean"])
        self.assertEqual(s["context_calls"], block["sample_calls"])
        self.assertEqual(s["unmeasured_calls"], block["unmeasured_calls"])

    def test_a_window_whose_only_call_measured_nothing_reports_no_average(
        self,
    ) -> None:
        # The observed symptom: on the reference corpus the chart's
        # `avg context/call` plunged to zero on two days whose single call was
        # one of these rows, reading as a context collapse.
        s = self.api.summary(*self.day(NM_BLIND_DAY))
        self.assertEqual(s["calls"], 1)
        self.assertIsNone(s["avg_context"], "no sample is not an average of 0")
        self.assertEqual(s["context_calls"], 0)
        self.assertEqual(s["unmeasured_calls"], 1)

    def test_a_window_whose_only_call_measured_a_real_zero_still_averages(
        self,
    ) -> None:
        # The mirror-image defect, which is the easy way to get this wrong: a
        # call that genuinely measured 0 OUTPUT tokens is a healthy sample,
        # its context was measured, and it must survive the guard.
        start, end = self.day(NM_REAL_ZERO_DAY)
        s = self.api.summary(start, end)
        self.assertEqual(s["calls"], 1)
        self.assertAlmostEqual(s["avg_context"], float(NM_C_CONTEXT))
        self.assertEqual(s["context_calls"], 1)
        self.assertEqual(s["unmeasured_calls"], 0)
        # And its zero output is summed as the real zero it is, not dropped.
        self.assertEqual(s["output"], 0)

    def test_an_empty_window_has_no_average_and_no_calls_to_count(self) -> None:
        s = self.api.summary(*self.day("2026-05-10"))
        self.assertEqual(s["calls"], 0)
        self.assertIsNone(s["avg_context"])
        # A count over an empty set IS a real zero: nothing happened, and that
        # is corroborated by `calls`.
        self.assertEqual(s["context_calls"], 0)
        self.assertEqual(s["unmeasured_calls"], 0)

    def test_the_daily_series_leaves_a_gap_rather_than_plotting_zero(self) -> None:
        ts = self.api.timeseries(*self.all_time(), by="tokens")
        by_day = dict(zip(ts["days"], ts["avg_context"]))
        calls = dict(zip(ts["days"], ts["calls"]))
        measured = dict(zip(ts["days"], ts["context_calls"]))
        blank = dict(zip(ts["days"], ts["unmeasured_calls"]))
        self.assertEqual(
            ts["days"],
            [NM_MIXED_DAY, NM_REAL_ZERO_DAY, NM_BLIND_DAY, NM_MEASURED_DAY],
        )
        self.assertAlmostEqual(by_day[NM_MIXED_DAY], NM_MIXED_DAY_AVG)
        self.assertAlmostEqual(by_day[NM_REAL_ZERO_DAY], float(NM_C_CONTEXT))
        self.assertIsNone(by_day[NM_BLIND_DAY])
        self.assertAlmostEqual(by_day[NM_MEASURED_DAY], float(NM_G_CONTEXT))
        # The call still happened on the blind day, and still counts.
        self.assertEqual(calls, {
            NM_MIXED_DAY: 4, NM_REAL_ZERO_DAY: 1,
            NM_BLIND_DAY: 1, NM_MEASURED_DAY: 1,
        })
        self.assertEqual(measured, {
            NM_MIXED_DAY: 2, NM_REAL_ZERO_DAY: 1,
            NM_BLIND_DAY: 0, NM_MEASURED_DAY: 1,
        })
        self.assertEqual(blank, {
            NM_MIXED_DAY: 2, NM_REAL_ZERO_DAY: 0,
            NM_BLIND_DAY: 1, NM_MEASURED_DAY: 0,
        })

    def test_the_daily_series_publishes_a_sample_count_per_point(self) -> None:
        # An aggregate must name the set it ranges over, and for a series that
        # means per point -- one number per day, the same length as `days`.
        ts = self.api.timeseries(*self.all_time(), by="tokens")
        for key in ("avg_context", "context_calls", "unmeasured_calls", "calls"):
            with self.subTest(series=key):
                self.assertEqual(len(ts[key]), len(ts["days"]))
        for i, day in enumerate(ts["days"]):
            with self.subTest(day=day):
                self.assertEqual(
                    ts["context_calls"][i] + ts["unmeasured_calls"][i],
                    ts["calls"][i],
                )

    def test_the_session_list_averages_only_its_measured_calls(self) -> None:
        rows = {r["id"]: r for r in self.api.sessions(*self.all_time())}
        self.assertEqual(set(rows), {NM_BUSY, NM_LONELY})
        busy, lonely = rows[NM_BUSY], rows[NM_LONELY]
        self.assertEqual(busy["calls"], 5)
        self.assertAlmostEqual(busy["avg_context"], NM_BUSY_AVG)
        self.assertEqual(busy["context_calls"], 3)
        self.assertEqual(busy["unmeasured_calls"], 2)
        self.assertEqual(lonely["calls"], 2)
        self.assertAlmostEqual(lonely["avg_context"], NM_LONELY_AVG)
        self.assertEqual(lonely["context_calls"], 1)
        self.assertEqual(lonely["unmeasured_calls"], 1)

    def test_a_session_whose_window_holds_only_a_blind_call_is_inconclusive(
        self,
    ) -> None:
        rows = self.api.sessions(*self.day(NM_BLIND_DAY))
        self.assertEqual([r["id"] for r in rows], [NM_LONELY])
        self.assertEqual(rows[0]["calls"], 1)
        self.assertIsNone(rows[0]["avg_context"])
        self.assertEqual(rows[0]["context_calls"], 0)
        self.assertEqual(rows[0]["unmeasured_calls"], 1)

    def test_each_scope_bucket_averages_only_its_own_measured_calls(self) -> None:
        scope = self.api.summary(*self.all_time())["scope"]
        main, sub = scope["main_thread"], scope["subagent"]
        self.assertEqual(main["calls"], 5)
        self.assertAlmostEqual(main["avg_context"], NM_MAIN_AVG)
        self.assertEqual(main["context_calls"], 3)
        self.assertEqual(main["unmeasured_calls"], 2)
        self.assertEqual(sub["calls"], 2)
        self.assertAlmostEqual(sub["avg_context"], NM_SUBAGENT_AVG)
        self.assertEqual(sub["context_calls"], 1)
        self.assertEqual(sub["unmeasured_calls"], 1)
        # Two scopes, two different means: a bucket reading the other's set --
        # or the corpus's -- cannot pass.
        self.assertNotAlmostEqual(main["avg_context"], sub["avg_context"])

    def test_a_scope_with_no_calls_at_all_reports_no_average(self) -> None:
        scope = self.api.summary(*self.day(NM_BLIND_DAY))["scope"]
        sub = scope["subagent"]
        self.assertEqual(sub["calls"], 0)
        self.assertIsNone(sub["avg_context"])
        self.assertEqual(sub["context_calls"], 0)
        self.assertEqual(sub["unmeasured_calls"], 0)

    def test_a_scope_whose_only_call_measured_nothing_reports_no_average(
        self,
    ) -> None:
        scope = self.api.summary(*self.day(NM_BLIND_DAY))["scope"]
        main = scope["main_thread"]
        self.assertEqual(main["calls"], 1)
        self.assertIsNone(main["avg_context"])
        self.assertEqual(main["context_calls"], 0)
        self.assertEqual(main["unmeasured_calls"], 1)

    def test_session_detail_scopes_average_only_their_measured_calls(self) -> None:
        scopes = {
            s["scope"]: s for s in self.api.session_detail(NM_BUSY)["scopes"]
        }
        self.assertEqual(set(scopes), {"main-thread", "subagent"})
        main, sub = scopes["main-thread"], scopes["subagent"]
        self.assertEqual(main["calls"], 3)
        self.assertAlmostEqual(main["avg_context"], NM_BUSY_MAIN_AVG)
        self.assertEqual(main["context_calls"], 2)
        self.assertEqual(main["unmeasured_calls"], 1)
        self.assertEqual(sub["calls"], 2)
        self.assertAlmostEqual(sub["avg_context"], NM_SUBAGENT_AVG)
        self.assertEqual(sub["context_calls"], 1)
        self.assertEqual(sub["unmeasured_calls"], 1)

    def test_every_call_is_in_the_mean_or_counted_beside_it(self) -> None:
        # The invariant that makes the pair readable at all five sites:
        # nothing is silently dropped, so sample + unmeasured == calls.
        start, end = self.all_time()
        buckets: list[tuple[str, dict]] = [("summary", self.api.summary(start, end))]
        buckets += [
            (f"scope:{name}", buckets[0][1]["scope"][name])
            for name in ("main_thread", "subagent")
        ]
        buckets += [(f"session:{r['id']}", r) for r in self.api.sessions(start, end)]
        buckets += [
            (f"detail:{session}:{s['scope']}", s)
            for session in (NM_BUSY, NM_LONELY)
            for s in self.api.session_detail(session)["scopes"]
        ]
        for name, row in buckets:
            with self.subTest(site=name):
                self.assertEqual(
                    row["context_calls"] + row["unmeasured_calls"], row["calls"]
                )
                # And the pair is honest about the aggregate beside it.
                if row["context_calls"] == 0:
                    self.assertIsNone(row["avg_context"])
                else:
                    self.assertIsNotNone(row["avg_context"])


class NoMeasurementIsNotAZeroInTheViewTest(unittest.TestCase):
    """#25's five means reach the reader as means over a NAMED sample.

    The API stopped publishing 0 for a mean over nothing; the page had not
    caught up. `Math.round(null)` is 0, so a binding that rounds before it
    checks re-creates the defect one layer down -- the API says INCONCLUSIVE
    and the cell still draws a measured zero, which is worse than the original
    because the number now looks freshly corrected.

    Every context mean on this page therefore has to answer two questions, and
    these tests are one per question: what does it read when nothing was
    measured, and over how many of the calls beside it was it taken.
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (cls.ROOT / "index.html").read_text()
        cls.html = strip_comments(cls.raw)
        cls.sessions = html_element(cls.raw, 'id="sessions"')

    def test_no_context_mean_is_rounded_before_it_is_checked(self) -> None:
        # THE defect, as an absence: `Math.round(null)` is 0, so every
        # `avg_context` binding must go through the formatter that refuses
        # first. Asserted over the whole page rather than the known sites, so a
        # sixth mean added later cannot quietly reintroduce it.
        for hit in re.findall(r"Math\.round\([^)]*\)", self.html):
            with self.subTest(expr=hit):
                self.assertNotIn("avg_context", hit)
        self.assertNotIn("Math.round(r.avg_context)", self.html)

    def test_every_rendered_context_mean_uses_the_refusing_formatter(self) -> None:
        bindings = re.findall(r"\w+\(\s*\w+\.avg_context\s*\)", self.html)
        self.assertTrue(bindings, "no context mean is rendered at all")
        for binding in bindings:
            with self.subTest(binding=binding):
                self.assertTrue(
                    binding.startswith("fmtTokRounded("),
                    f"{binding} does not refuse a missing measurement first",
                )

    # --- the sessions table ---

    def test_the_sessions_mean_carries_its_own_sample(self) -> None:
        # An aggregate must name the set it ranges over, and this one no longer
        # ranges over the call count in the same row.
        self.assertIn("contextSample(r)", self.sessions)
        body = js_function_body(self.html, "contextSample(r) {")
        self.assertIn("r.context_calls", body)
        self.assertIn("r.unmeasured_calls", body)
        self.assertIn("r.calls", body)

    def test_the_sessions_mean_distinguishes_none_measured_from_some(self) -> None:
        # Three states, not two: every call measured, some measured, none
        # measured. The third is the one that must never read as a small mean.
        body = js_function_body(self.html, "contextSample(r) {")
        self.assertRegex(body, r"if \(!r\.context_calls\) \{")
        self.assertRegex(body, r"if \(r\.unmeasured_calls\) \{")
        self.assertIn("UNMEASURED, never 0", body)

    def test_the_partial_sample_is_marked_and_not_only_tooltipped(self) -> None:
        # A caveat only in a `title` is a caveat nobody sees. This mirrors the
        # subagent cell in the same table: value, mark, tooltip.
        self.assertRegex(self.sessions, r'title="contextSample\(r\)\[1\]"')
        self.assertRegex(self.sessions, r'x-text="contextSample\(r\)\[0\]"')

    def test_the_column_heading_says_what_the_mean_ranges_over(self) -> None:
        heading = re.search(r'<th title="([^"]*)">Avg ctx/call</th>', self.sessions)
        self.assertIsNotNone(heading, "the column no longer names its sample")
        for claim in ("context measurement", "never averaged in as a zero"):
            with self.subTest(claim=claim):
                self.assertIn(claim, heading.group(1))

    # --- the timeseries, which is where the defect was SEEN ---

    def test_the_chart_point_names_the_sample_behind_it(self) -> None:
        # The fifth site, and the only one whose failure was visible: the line
        # plunged to zero on days nobody measured. A gap is the right drawing,
        # but a gap alone does not say whether the day had no calls or no
        # measurements -- these two arrays do, per point.
        chart = js_function_body(self.html, "drawChart(ts)")
        self.assertIn("ts.context_calls", chart)
        self.assertIn("ts.unmeasured_calls", chart)
        label = js_function_body(self.html, "function chartTooltipLabel(")
        self.assertIn("dataset.contextCalls", label)
        self.assertIn("dataset.unmeasuredCalls", label)

    def test_a_point_with_no_measured_call_says_so_rather_than_nothing(self) -> None:
        label = js_function_body(self.html, "function chartTooltipLabel(")
        self.assertRegex(label, r"if \(!n\) \{")
        self.assertIn("no call this day carried a context measurement", label)

    def test_the_token_series_claim_no_sample_they_do_not_have(self) -> None:
        # A stacked token series is a SUM over every call that day, so it has
        # no sample to name; only the mean does. Attaching the counts to every
        # dataset would put a sample size on a figure that has none.
        label = js_function_body(self.html, "function chartTooltipLabel(")
        self.assertRegex(
            label, r"const measured = c\.dataset\.contextCalls;\s*\n\s*if \(!measured\) return base;"
        )

    def test_the_page_derives_no_total_the_api_already_publishes(self) -> None:
        # `context_calls + unmeasured_calls == calls` is the API's invariant,
        # published as `calls`. Re-adding the two here would be a second
        # arithmetic free to disagree with it.
        label = js_function_body(self.html, "function chartTooltipLabel(")
        self.assertIn("dataset.totalCalls", label)
        self.assertNotRegex(label, r"unmeasured\s*\+\s*n|n\s*\+\s*unmeasured")


# The tiny JS dialect the mirrored staleness rule is allowed to be written in,
# and its Python spelling. Deliberately minimal: `js_guarded_returns` raises on
# anything it cannot translate, so a rewrite of the mirror in a richer style is
# a LOUD failure rather than a silently skipped comparison.
JS_TO_PY = (
    ("===", " == "),
    ("!==", " != "),
    (r"\bnull\b", "None"),
    (r"\btrue\b", "True"),
    (r"\bfalse\b", "False"),
)
# What a translated fragment may contain. The source is this repository's own
# file, but the check is cheap and it doubles as the "did the translation
# actually cover this statement" test.
JS_EXPR_SAFE = re.compile(r"^[\w\s.<>=!()+\-*/]+$")


def js_guarded_returns(body: str) -> list[tuple[str | None, str]]:
    """`(condition, expression)` pairs from a JS body of guarded returns.

    The project ships NO JS runtime (stdlib-only, no Node), so a rule that must
    hold identically on both sides of the wire cannot simply be executed here.
    The two available options were a hand-written Python twin of the client
    rule -- which is a second definition free to drift, i.e. exactly the defect
    the comparison exists to catch -- or reading the SHIPPED source and
    translating it. This does the second.

    It accepts only `if (cond) return expr;` lines followed by one bare
    `return expr;`, which is the whole grammar the mirror is written in, and
    raises on anything else rather than skipping it.
    """
    inner = body.strip()
    if not (inner.startswith("{") and inner.endswith("}")):  # pragma: no cover
        raise AssertionError("expected a braced function body")
    pairs: list[tuple[str | None, str]] = []
    for raw in inner[1:-1].split(";"):
        statement = " ".join(raw.split())
        if not statement:
            continue
        guarded = re.fullmatch(r"if \((?P<cond>.+?)\) return (?P<expr>.+)", statement)
        plain = re.fullmatch(r"return (?P<expr>.+)", statement)
        if guarded is not None:
            pairs.append(
                (_js_to_py(guarded["cond"]), _js_to_py(guarded["expr"]))
            )
        elif plain is not None:
            pairs.append((None, _js_to_py(plain["expr"])))
        else:
            raise AssertionError(
                f"cannot translate {statement!r}: the mirrored rule must stay a "
                "list of guarded returns, or this comparison silently stops "
                "covering it"
            )
    if not pairs:  # pragma: no cover - an empty mirror is not a mirror
        raise AssertionError("no statements found in the mirrored rule")
    return pairs


def _js_to_py(fragment: str) -> str:
    for js, py in JS_TO_PY:
        fragment = re.sub(js, py, fragment)
    fragment = " ".join(fragment.split())
    if not JS_EXPR_SAFE.fullmatch(fragment):
        raise AssertionError(f"untranslatable fragment: {fragment!r}")
    return fragment


def eval_js_guarded_returns(pairs: Sequence[tuple[str | None, str]], **env):
    """Run a `js_guarded_returns` table under Python's own semantics."""
    for cond, expr in pairs:
        if cond is None or eval(cond, {"__builtins__": {}}, dict(env)):
            return eval(expr, {"__builtins__": {}}, dict(env))
    raise AssertionError("the mirrored rule fell off the end without returning")


class ClientSideAgeRecomputeTest(unittest.TestCase):
    """The staleness indicator must not itself go stale (#24).

    #20 Part 1 computes the age and the verdict SERVER-SIDE, once, against an
    `as_of` stamped at response time, and the page then froze both in the DOM.
    Measured on merged `main` 2026-08-05: a tab that loaded at age 120 s still
    read "Last ingest: 2 minutes ago, not stale" two hours later, when the
    truthful verdict was STALE (`STALE_AFTER_SECONDS` = 900). So the banner
    could never fire on a tab that stayed open -- it could only appear on a
    page that was ALREADY stale when it loaded, which is the case least in need
    of a warning, because the user is right there having just loaded it. #20
    exists because "a page left open for a week looked identical to one opened
    a second ago"; the indicator built to expose that had inherited it.

    The fix recomputes the AGE, and nothing else. It issues no request, so it
    is evidence that time has passed and never evidence that anything was
    re-read -- #24's open question A (polling vs SSE vs long-poll for the DATA)
    is untouched, and `serve.py` is still single-threaded. These tests pin the
    three things that make that non-trivial:

    * a locally recomputed verdict may turn the banner ON and never OFF, since
      only a real re-fetch is evidence that staleness ended;
    * an UNKNOWN age must not decay into a verdict as seconds accumulate;
    * the client rule must agree with `staleness_verdict()` EXACTLY, including
      the strict `>` boundary and the negative-age branch. Two definitions of
      "stale" free to drift apart is the defect class this repository keeps
      finding, so the last one is asserted by translating the shipped client
      rule and running it against the server's over a table of ages, rather
      than by reading both and hoping.
    """

    ROOT = Path(__file__).resolve().parent.parent
    AS_OF = 1_800_000_000.0  # any fixed clock; both rules are functions of AGE

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (cls.ROOT / "index.html").read_text()
        cls.html = strip_comments(cls.raw)

    # --- the timer -------------------------------------------------------

    def test_the_rendered_age_is_recomputed_on_a_timer(self) -> None:
        # Without this the page has no mechanism to notice time passing at
        # all, which is the whole defect.
        self.assertIn("setInterval(", self.html)
        self.assertRegex(
            js_function_body(self.html, "init() {"),
            r"setInterval\(\s*\(\)\s*=>\s*this\.tickAge\(\),\s*AGE_TICK_MS\s*\)",
        )

    def test_the_tick_is_far_finer_than_the_threshold_it_must_cross(self) -> None:
        # The interval bounds how LATE the banner can be. Tied to the server's
        # constant rather than asserted as a bare number, so raising one and
        # not the other cannot pass: a tick near the threshold would let a tab
        # sit stale for a large fraction of the window the banner exists for.
        found = re.search(r"const AGE_TICK_MS = (\d+);", self.html)
        self.assertIsNotNone(found, "the tick interval is no longer named")
        seconds = int(found.group(1)) / 1000
        self.assertLessEqual(seconds, STALE_AFTER_SECONDS / 20)
        # ...and not so fine that it redraws an unchanged string: `fmtSpan`
        # renders whole minutes over the band a left-open tab lives in.
        self.assertGreaterEqual(seconds, 10)

    def test_the_recompute_issues_no_request(self) -> None:
        # #24's question A stays open. A tick that re-fetched would be the
        # polling decision, taken by implementation rather than by the owner.
        tick = js_function_body(self.html, "tickAge() {")
        for call in ("fetch(", "getJSON(", "this.load(", "EventSource"):
            with self.subTest(call=call):
                self.assertNotIn(call, tick)

    def test_a_hidden_tab_is_not_kept_redrawing(self) -> None:
        # Nothing to redraw for a tab nobody is looking at; the visibility
        # binding brings it back up to date before it is seen again.
        self.assertRegex(
            js_function_body(self.html, "tickAge() {"),
            r"if \(document\.hidden\) return",
        )
        self.assertRegex(self.html, r'x-on:visibilitychange[.\w]*="tickAge\(\)"')

    # --- what the recompute is allowed to change --------------------------

    def test_the_rendered_ages_are_measured_from_the_carried_clock(self) -> None:
        # The defect, as an absence: a binding straight off `ingest.as_of` is
        # the frozen number. Both ages on the line move, not just the one the
        # verdict reads.
        band = html_element(self.raw, 'id="data-age"')
        self.assertIn("asOfNow - summary.ingest.last_run_at", band)
        self.assertIn("asOfNow - summary.ingest.newest_call_ts", band)
        self.assertNotIn("summary.ingest.as_of -", band)

    def test_elapsed_time_is_measured_locally_and_never_runs_backwards(self) -> None:
        # The carried-forward part is the time THIS PAGE measured, added to the
        # server's own clock -- so no browser/server skew is introduced by the
        # fix, and a backwards clock step freezes the age instead of making the
        # data look fresher than the last thing anyone measured.
        body = js_function_body(self.html, "get elapsedSinceLoad(")
        self.assertRegex(body, r"Math\.max\(\s*0\s*,")
        self.assertIn("this.loadedAt", body)
        self.assertIn("this.now", body)
        self.assertIn(
            "ingest.as_of + this.elapsedSinceLoad",
            js_function_body(self.html, "get asOfNow("),
        )

    def test_only_a_successful_load_restarts_the_page_clock(self) -> None:
        # "Last updated" means last SUCCESSFUL update (#24 AC). `applySummary`
        # runs only after every endpoint returned, so the origin the elapsed
        # time counts from is set there and nowhere else -- a failed refresh
        # that reset it would make the page claim it had just re-read.
        self.assertEqual(self.html.count("this.loadedAt = "), 1)
        self.assertIn(
            "this.loadedAt = ", js_function_body(self.html, "applySummary(")
        )

    def test_a_recomputed_verdict_can_only_turn_the_banner_on(self) -> None:
        # THE asymmetry. Elapsed time is evidence that the data got older; it
        # is no evidence at all that anything was re-read, so it may raise the
        # warning and must never clear one. Structurally: the server's verdict
        # is consulted first and the local one only where the server's is
        # absent, and no code outside `applySummary` writes the slot.
        self.assertRegex(
            js_function_body(self.html, "get bannerMessages("),
            r"this\.banner\.stale \?\? this\.localStaleMessage",
        )
        self.assertEqual(
            self.html.count("this.banner.stale = "),
            1,
            "a second writer for the staleness slot is how a recompute comes "
            "to clear a warning it has no grounds to clear",
        )
        self.assertIn(
            "this.banner.stale = ", js_function_body(self.html, "applySummary(")
        )

    def test_the_recompute_never_turns_an_unknown_age_into_a_verdict(self) -> None:
        # `stale` is tri-state and `stale_unknown_reason` is non-null exactly
        # when it is null. An age nobody could measure does not become
        # measurable because seconds accumulated: a run stamped in the FUTURE
        # would otherwise tick up through zero and out the far side into a
        # confident "stale", which is a verdict on a comparison that never
        # happened. Guarded twice -- the clock is not carried forward at all,
        # and the age stays null when there is no stamp to subtract from.
        self.assertIn(
            "ingest.stale_unknown_reason !== null",
            js_function_body(self.html, "get asOfNow("),
        )
        self.assertIn(
            "ingest.last_run_at === null",
            js_function_body(self.html, "get ingestAge("),
        )

    # --- the two rules must not drift -------------------------------------

    def mirror(self) -> list[tuple[str | None, str]]:
        return js_guarded_returns(
            js_function_body(self.html, "function staleFromAge(")
        )

    def client_verdict(self, age: float | None):
        return eval_js_guarded_returns(
            self.mirror(), ageSeconds=age, thresholdSeconds=STALE_AFTER_SECONDS
        )

    def server_verdict(self, age: float | None):
        last_run_at = None if age is None else self.AS_OF - age
        return staleness_verdict(last_run_at, self.AS_OF, run_table_present=True)[0]

    # Both boundary seconds, a real zero, a negative, and the two measured
    # ingest costs `STALE_AFTER_SECONDS` was chosen against.
    AGES = (
        None,
        -30 * 86400.0,
        -1.0,
        0.0,
        1.8,
        39.9,
        STALE_AFTER_SECONDS - 1,
        STALE_AFTER_SECONDS,
        STALE_AFTER_SECONDS + 1,
        1.2 * 3600,
        30 * 86400.0,
    )

    def test_the_client_rule_answers_exactly_as_the_server_rule_does(self) -> None:
        for age in self.AGES:
            with self.subTest(age=age):
                self.assertEqual(
                    self.client_verdict(age),
                    self.server_verdict(age),
                    "the page and serve.py disagree about what STALE means",
                )

    def test_the_table_covers_the_cases_that_tell_the_spellings_apart(self) -> None:
        # A table that never crosses the boundary agrees with anything, so the
        # answers themselves are pinned: without a True and a False one second
        # apart, and a None, the comparison above is vacuous.
        answers = {self.server_verdict(age) for age in self.AGES}
        self.assertEqual(answers, {True, False, None})
        self.assertIs(self.client_verdict(STALE_AFTER_SECONDS), False)
        self.assertIs(self.client_verdict(STALE_AFTER_SECONDS + 1), True)
        self.assertIsNone(self.client_verdict(-1.0))

    def test_the_threshold_is_read_from_the_payload_not_spelled_in_the_page(
        self,
    ) -> None:
        # The cheapest way for the two definitions to drift is a second copy of
        # the number. The page never names one: it compares against the
        # threshold the response carried.
        self.assertRegex(
            self.html,
            r"staleFromAge\([^)]*ingest\.stale_after_seconds\s*\)",
        )
        self.assertNotIn(str(STALE_AFTER_SECONDS), self.html)

    # --- wording -----------------------------------------------------------

    def test_the_page_says_time_passed_and_never_that_data_is_fresh(self) -> None:
        # The fix's own failure mode, wearing the opposite mask: after a
        # recompute the page knows time has passed, NOT that anything was
        # re-read. A line that implies the second is the original defect with
        # the sign flipped.
        note = js_function_body(self.html, "get elapsedNote(")
        for claim in ("NOT re-read", "only TIME has passed", "Reload to re-measure"):
            with self.subTest(claim=claim):
                self.assertIn(claim, note)
        message = js_function_body(self.html, "get localStaleMessage(")
        self.assertIn("has NOT re-read the database", message)
        self.assertIn("reload to re-measure", message)

    def test_the_frozen_server_message_is_dated_to_the_load_it_came_from(
        self,
    ) -> None:
        # Found while wiring this: the SERVER's stale message is composed once,
        # in `applySummary`, and quotes the age it was composed with. Now that
        # the line above it counts forward, an undated "ingest.py last ran 20
        # minutes ago" would sit under a data-age line reading 2.2 hours -- two
        # ages of the same fact disagreeing on one screen, which is the drift
        # this whole change exists to prevent. It stays frozen (only a re-fetch
        # could move it honestly) and says WHEN it was measured instead.
        message = js_function_body(self.html, "applySummary(")
        self.assertIn("when this page loaded", message)

    def test_the_elapsed_note_is_rendered_where_the_ages_are(self) -> None:
        # Constraint 4: it ANNOTATES the data-age line the recompute moves,
        # rather than adding a surface of its own.
        self.assertIn('x-text="elapsedNote"', html_element(self.raw, 'id="data-age"'))


# --- #78: the recommendation table's consumer ------------------------------
#
# FIXTURE DESIGN. Five days, each isolating one thing the block has to get
# right, and every token class of every call deliberately unequal so a swapped
# column mapping cannot reproduce a figure.
#
#   * REC_FULL_DAY runs both scopes and pins all five metrics at once. Its
#     values are chosen to land in DIFFERENT places in the table -- an `act`
#     above an open-ended range, a `watch` inside a range whose lower edge is
#     CITED and whose upper edge is JUDGED, and so on -- so one fixture
#     exercises every provenance kind the payload can carry.
#   * REC_SOLO_DAY dispatches no subagent. `main_vs_subagent_tokens_per_reply`
#     is then UNMEASURED, which is the zero-denominator case this whole block
#     turns on: no subagent reply is not a ratio of zero.
#   * REC_NO_CACHE_DAY runs calls that wrote no cache at all, so BOTH flat cache
#     metrics lose their denominators -- one a token sum, one a call count,
#     which vanish together here and are checked separately in the code.
#   * REC_UNWINDOWED_DAY runs the main thread on a model with no documented
#     window, so `main_thread_share_over_half_window` has no denominator while
#     the others still do.
#   * REC_EMPTY_DAY holds nothing, so all five are unmeasured and `ranked` is
#     empty -- the state in which a page that rendered absence as health would
#     tell the reader everything is fine.
#
# THE PER-TTL SPLIT IS THE EIGHTH FIELD (#84), and it is the transcript's own
# `usage.cache_creation` object rather than a pair of numbers, because the three
# states that matter are states of the RECORD and not of the arithmetic:
#
#   * `None` -- no `cache_creation` key at all, which is what every Claude Code
#     record CPB ingested before #84 looks like. Both columns land NULL.
#   * both TTL keys -- a measured split. Only these calls are in either side of
#     `cache_write_repayment_at_own_ttl`.
#   * ONE TTL key -- a half-measurement. It is excluded from BOTH sides, and the
#     fixture carries one on purpose: reading the absent half as 0 is the
#     mistake that would report a 1-hour write repaid at its first read.
#
# The splits below RECONCILE with the flat `cache_write` beside them (they are
# the same write, counted twice), and the two TTLs are deliberately unequal on
# every call that has both, so a denominator that swapped the 1x and 2x weights
# cannot reproduce the expected figure.
REC_SESSION = "recommendation-fixture"
REC_AGENT = "agent-rec78a"
REC_FULL_DAY = "2026-07-01"
REC_SOLO_DAY = "2026-07-02"
REC_NO_CACHE_DAY = "2026-07-03"
REC_UNWINDOWED_DAY = "2026-07-04"
REC_EMPTY_DAY = "2026-07-05"

REC_OPUS_1M = "claude-opus-5-20260101"          # 1,000,000-token window
REC_HAIKU_200K = "claude-haiku-4-5-20251001"    # 200,000-token window
REC_UNKNOWN_MODEL = "claude-nosuchtier-9-20260101"

# The `usage.cache_creation` object a record carries, spelled with ingest's own
# key names so a renamed key fails here too.
def _split(five_m: Optional[int] = None, one_h: Optional[int] = None) -> dict:
    out: dict[str, int] = {}
    if five_m is not None:
        out[CACHE_WRITE_5M_KEY] = five_m
    if one_h is not None:
        out[CACHE_WRITE_1H_KEY] = one_h
    return out


# (day, kind, model, input, cache_write, cache_read, output, cache_creation).
# `context_size` is input + cache_write + cache_read, which is how `ingest.py`
# derives it, so the main-thread contexts below are engineered to sit in known
# bands against a 1M window: 600k (50-90), 950k (>=90), 300k, 100k, 38k.
REC_BASE_CALLS: list[tuple[str, str, str, int, int, int, int, Optional[dict]]] = [
    # --- the full day, main thread: two of five calls over half the window ---
    # SPLIT MEASURED, both TTLs, unequal: 100,000 + 20,000 = the flat 120,000.
    (
        REC_FULL_DAY, SOURCE_MAIN, REC_OPUS_1M, 10_000, 120_000, 470_000, 2_000,
        _split(100_000, 20_000),
    ),
    # NO SPLIT, and the two largest read totals of the day are here on purpose:
    # 984,000 read tokens whose writes cannot enter the denominator. A numerator
    # taken over the whole window rather than over the split-measured calls
    # inflates the metric by exactly these.
    (
        REC_FULL_DAY, SOURCE_MAIN, REC_OPUS_1M, 11_000, 121_000, 818_000, 2_200,
        None,
    ),
    (
        REC_FULL_DAY, SOURCE_MAIN, REC_OPUS_1M, 12_000, 122_000, 166_000, 2_400,
        None,
    ),
    # SPLIT MEASURED, and mostly 1-hour: 3,000 + 20,000 = the flat 23,000. The
    # 1-hour tokens outweigh the 5-minute ones across the day's measured set, so
    # swapping the two weights moves the figure.
    (
        REC_FULL_DAY, SOURCE_MAIN, REC_OPUS_1M, 13_000, 23_000, 64_000, 2_600,
        _split(3_000, 20_000),
    ),
    # Wrote cache and read NOTHING back -- one half of `cache_write_only_share`.
    (
        REC_FULL_DAY, SOURCE_MAIN, REC_OPUS_1M, 14_000, 24_000, 0, 2_800,
        None,
    ),
    # --- the full day, subagents ---
    # SPLIT MEASURED, all five-minute. Both scopes are in the metric, as they
    # are in the flat ratio beside it.
    (
        REC_FULL_DAY, SOURCE_SUBAGENT, REC_HAIKU_200K, 5_000, 6_000, 7_000, 8_000,
        _split(6_000, 0),
    ),
    # The second write-only call, in the OTHER scope: the share ranges over
    # both, so a query that filtered to one would come out 1/5 or 1/2 here
    # rather than 2/7. HALF A SPLIT -- the 1-hour key is absent, not zero -- so
    # its 6,100 write tokens belong in no denominator at all.
    (
        REC_FULL_DAY, SOURCE_SUBAGENT, REC_HAIKU_200K, 5_100, 6_100, 0, 8_100,
        _split(6_100),
    ),
    # Wrote no cache at all, so it is in NEITHER the numerator nor the
    # denominator of the write-only share -- a call that never stored a prefix
    # cannot have failed to read one back.
    (
        REC_FULL_DAY, SOURCE_SUBAGENT, REC_HAIKU_200K, 5_200, 0, 0, 8_200,
        None,
    ),
    # --- a day the main thread ran alone, and NOTHING carries a split: the
    # shape of every database not re-ingested since #84, where the flat ratio is
    # measured and the per-TTL one is not ---
    (REC_SOLO_DAY, SOURCE_MAIN, REC_OPUS_1M, 1_000, 2_000, 3_000, 4_000, None),
    (REC_SOLO_DAY, SOURCE_MAIN, REC_OPUS_1M, 1_100, 2_100, 3_100, 4_100, None),
    # --- a day nothing wrote cache. The split IS measured here and is 0 at both
    # TTLs, which is a real reading and still not a ratio: nothing was required,
    # so there is nothing to have repaid ---
    (
        REC_NO_CACHE_DAY, SOURCE_MAIN, REC_OPUS_1M, 5_000, 0, 0, 6_000,
        _split(0, 0),
    ),
    (
        REC_NO_CACHE_DAY, SOURCE_SUBAGENT, REC_HAIKU_200K, 7_000, 0, 0, 8_000,
        _split(0, 0),
    ),
    # --- a day the main thread ran on a model with no documented window ---
    (
        REC_UNWINDOWED_DAY, SOURCE_MAIN, REC_UNKNOWN_MODEL,
        1_000, 2_000, 3_000, 4_000, None,
    ),
    (
        REC_UNWINDOWED_DAY, SOURCE_SUBAGENT, REC_HAIKU_200K,
        1_100, 2_100, 3_100, 4_100, None,
    ),
]

# #93: HOW MANY TIMES THE PATTERN ABOVE IS RUN, and why it is a replication
# rather than more hand-written rows.
#
# Every metric now refuses to band a reading whose sample is below its floor,
# and the largest of those floors is 51 -- `cache_write_only_share`'s, derived
# from its own narrowest band. The pattern above holds 7 cache-writing calls on
# its full day, so as written it is a corpus that CANNOT support the verdicts
# this class asserts, and asserting them over it was testing the defect #93
# names: a band placement earned by a sample too thin to place anything.
#
# REPLICATING THE PATTERN IS THE ONE WAY TO GROW IT THAT MOVES NO FIGURE. Every
# count multiplies by `REC_REPLICAS` and every token sum multiplies with it, so
# every SHARE and every RATIO -- which are the readings under test -- comes out
# bit-identical to the eight-call original. Hand-writing forty more rows would
# have moved each of the five readings by whatever those rows happened to
# contain, and every expectation below with them.
#
# EIGHT, and it is the smallest factor that clears every floor rather than a
# comfortable round number: 7 x 7 = 49 cache-writing calls, one short of 51.
# `test_the_fixture_clears_every_floor_it_is_asserted_against` pins that the
# corpus is above the floors and `test_seven_replicas_would_not_have_cleared_
# them` pins that the margin is real, so a floor raised later fails here loudly
# instead of silently turning these assertions into assertions about absence.
REC_REPLICAS = 8
REC_CALLS: list[tuple[str, str, str, int, int, int, int, Optional[dict]]] = [
    call for _ in range(REC_REPLICAS) for call in REC_BASE_CALLS
]

# Hand-written from the table above, then checked against it by
# `test_the_fixture_holds_what_the_expectations_claim`. Derived-only
# expectations would agree with a fixture that had drifted -- so these are the
# eight-replica totals written out, NOT `8 * <base>` expressions, which would
# recompute themselves around a changed `REC_REPLICAS` and assert nothing.
REC_FULL_MAIN_CALLS = 40
REC_FULL_MAIN_TOTAL = 16_000_000
REC_FULL_MAIN_BANDED = 40
REC_FULL_MAIN_OVER_HALF = 16
REC_FULL_SUB_CALLS = 24
REC_FULL_SUB_TOTAL = 469_600
REC_FULL_CACHE_READS = 12_200_000
REC_FULL_CACHE_WRITES = 3_376_800
REC_FULL_WRITING_CALLS = 56
REC_FULL_WRITE_ONLY_CALLS = 16
# The per-TTL metric's set (#84): the full-day calls carrying BOTH TTL keys,
# and the reads from those same calls. Every figure here is hand-written from
# the table above and checked against it below.
REC_FULL_SPLIT_CALLS = 24
REC_FULL_SPLIT_READS = 4_328_000      # 8 x (470,000 + 64,000 + 7,000)
REC_FULL_SPLIT_5M = 872_000           # 8 x (100,000 + 3,000 + 6,000)
REC_FULL_SPLIT_1H = 320_000           # 8 x (20,000 + 20,000 + 0)
# HAND-WRITTEN, not derived from the module's weights: 1 x 872,000 + 2 x 320,000.
# An expectation computed from `READ_TOKENS_TO_REPAY_A_*` would move with them,
# so a release that swapped the two multipliers would pass every assertion below
# -- the "fixture that makes the defect undetectable" failure, arriving through
# a constant instead of through a value. The weights are checked AGAINST this
# literal in `test_the_fixtures_split_holds_what_the_expectations_claim`.
REC_FULL_REQUIRED = 1_512_000
# 4,328,000 / 1,512,000 = 2.8624..., which is the base pattern's own
# 541,000/189,000 unchanged -- replication moves no ratio, which is why it was
# chosen over more hand-written rows (see `REC_REPLICAS`).
# Deliberately unlike its four neighbours, none of which may reproduce it:
#   whole-window reads over the same denominator 12,200,000/1,512,000 = 8.069
#   the weights swapped                           4,328,000/2,064,000 = 2.097
#   the half-split call counted as 5-minute only  4,328,000/1,560,800 = 2.773
#   the flat reads-per-write ratio               12,200,000/3,376,800 = 3.613
REC_FULL_REPAYMENT = REC_FULL_SPLIT_READS / REC_FULL_REQUIRED


def build_recommendation_corpus(root: Path) -> Path:
    """One session over five days, laid out as `discover_sources()` expects."""
    project = root / "projects" / "-fixture-recommendations"
    project.mkdir(parents=True)
    subagents = project / REC_SESSION / "subagents"
    subagents.mkdir(parents=True)

    def record(n: int, call: tuple) -> str:
        day, kind, model, inp, write, read, out, split = call
        usage: dict[str, object] = {
            "input_tokens": inp,
            "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read,
            "output_tokens": out,
        }
        # ABSENT, not `{}`: a record from before #84 has no such key, and an
        # empty object is a different observation (offered and empty) that
        # `_cache_write_split()` reads differently.
        if split is not None:
            usage[CACHE_CREATION_KEY] = split
        payload: dict[str, object] = {
            "type": "assistant",
            "sessionId": REC_SESSION,
            "timestamp": f"{day}T15:{n // 60:02d}:{n % 60:02d}.000Z",
            "isSidechain": kind == SOURCE_SUBAGENT,
            "message": {
                # Unique per call: `_dedupe_calls()` keys on this, and two calls
                # sharing an id would collapse into one and quietly change every
                # denominator below.
                "id": f"msg-rec78-{n}",
                "model": model,
                "usage": usage,
                "content": [{"type": "text", "text": f"rec78 call {n}"}],
            },
        }
        if kind == SOURCE_SUBAGENT:
            payload["agentId"] = REC_AGENT
        return json.dumps(payload) + "\n"

    main_lines: list[str] = []
    sub_lines: list[str] = []
    for n, call in enumerate(REC_CALLS):
        line = record(n, call)
        (sub_lines if call[1] == SOURCE_SUBAGENT else main_lines).append(line)
    (project / f"{REC_SESSION}.jsonl").write_text("".join(main_lines))
    (subagents / f"{REC_AGENT}.jsonl").write_text("".join(sub_lines))
    return project


class RecommendationApiTest(unittest.TestCase):
    """#78: `/api/summary` evaluates the table, and says what it could not.

    Asserted through the real ingest path, so every figure is measured over
    rows written the way production writes them -- including `source_kind`,
    which two of the five metrics divide by and which the ingester derives from
    the source's own path, and the per-TTL cache-write columns, which only the
    ingester can decide are NULL rather than 0.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-rec78-test-"))
        projects = build_recommendation_corpus(cls.tmp)
        db_path = cls.tmp / "usage.db"
        ingest(projects, db_path, tasks_dir=cls.tmp / "no-task-index")
        cls.api = Api(db_path)
        cls.html = (Path(__file__).resolve().parent.parent / "index.html").read_text()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def block(self, day: str | None = None) -> dict:
        return self.api.summary(*day_bounds(day, day))["recommendations"]

    def summary(self, day: str | None = None) -> dict:
        return self.api.summary(*day_bounds(day, day))

    def reading(self, day: str, metric: str) -> dict:
        found = [a for a in self.block(day)["ranked"] if a["metric"] == metric]
        self.assertEqual(len(found), 1, f"{metric} is not ranked exactly once on {day}")
        return found[0]

    # --- the fixture holds what it claims to ---

    def test_the_fixture_holds_what_the_expectations_claim(self) -> None:
        # The expectations above are hand-written; this is where they meet the
        # table. A fixture edited without them would otherwise move every
        # figure below and every assertion with it.
        full = [c for c in REC_CALLS if c[0] == REC_FULL_DAY]
        main = [c for c in full if c[1] == SOURCE_MAIN]
        sub = [c for c in full if c[1] == SOURCE_SUBAGENT]
        self.assertEqual(len(main), REC_FULL_MAIN_CALLS)
        self.assertEqual(len(sub), REC_FULL_SUB_CALLS)
        self.assertEqual(sum(sum(c[3:7]) for c in main), REC_FULL_MAIN_TOTAL)
        self.assertEqual(sum(sum(c[3:7]) for c in sub), REC_FULL_SUB_TOTAL)
        self.assertEqual(sum(c[5] for c in full), REC_FULL_CACHE_READS)
        self.assertEqual(sum(c[4] for c in full), REC_FULL_CACHE_WRITES)
        self.assertEqual(len([c for c in full if c[4] > 0]), REC_FULL_WRITING_CALLS)
        self.assertEqual(
            len([c for c in full if c[4] > 0 and c[5] == 0]),
            REC_FULL_WRITE_ONLY_CALLS,
        )

    def test_the_fixtures_split_holds_what_the_expectations_claim(self) -> None:
        # #84's half of the check above. A call is in the per-TTL metric's set
        # only if its record carried BOTH TTL keys, so the membership test is
        # spelled out here rather than taken from the code under test -- and
        # the flat totals are asserted to DIFFER from the split ones, because a
        # fixture where they agreed could not tell the two sets apart.
        full = [c for c in REC_CALLS if c[0] == REC_FULL_DAY]
        measured = [
            c
            for c in full
            if c[7] is not None
            and CACHE_WRITE_5M_KEY in c[7]
            and CACHE_WRITE_1H_KEY in c[7]
        ]
        self.assertEqual(len(measured), REC_FULL_SPLIT_CALLS)
        self.assertEqual(sum(c[5] for c in measured), REC_FULL_SPLIT_READS)
        self.assertEqual(
            sum(c[7][CACHE_WRITE_5M_KEY] for c in measured), REC_FULL_SPLIT_5M
        )
        self.assertEqual(
            sum(c[7][CACHE_WRITE_1H_KEY] for c in measured), REC_FULL_SPLIT_1H
        )
        # Each measured split reconciles with the flat write beside it: they are
        # one write counted twice, and a fixture where they disagreed would let
        # a query that read `cache_write` pass as one that read the split.
        for call in measured:
            with self.subTest(call=call[3]):
                self.assertEqual(
                    call[7][CACHE_WRITE_5M_KEY] + call[7][CACHE_WRITE_1H_KEY], call[4]
                )
        # The two sets are different sets, in both quantities.
        self.assertNotEqual(REC_FULL_SPLIT_READS, REC_FULL_CACHE_READS)
        self.assertNotEqual(
            REC_FULL_SPLIT_5M + REC_FULL_SPLIT_1H, REC_FULL_CACHE_WRITES
        )
        # Where the hand-written denominator meets the module's weights. This is
        # the ONE place they are compared, and it is an equality against a
        # literal: swapping the two multipliers reads 258,000 here and fails,
        # where an expectation derived from them would have moved with them.
        self.assertEqual(
            READ_TOKENS_TO_REPAY_A_5M_WRITE_TOKEN * REC_FULL_SPLIT_5M
            + READ_TOKENS_TO_REPAY_A_1H_WRITE_TOKEN * REC_FULL_SPLIT_1H,
            REC_FULL_REQUIRED,
            "the read tokens this fixture's writes require is not what the "
            "module's per-TTL break-evens say it is",
        )
        # Exactly one half-measured call, and it is not in the set above.
        half = [
            c
            for c in full
            if c[7] is not None
            and (CACHE_WRITE_5M_KEY in c[7]) != (CACHE_WRITE_1H_KEY in c[7])
        ]
        # One per replica of the pattern, and the pattern carries exactly one.
        self.assertEqual(len(half), REC_REPLICAS)
        self.assertGreater(
            half[0][4],
            0,
            "a half-split call that wrote nothing could not show the "
            "exclusion it exists to show",
        )

    def test_the_fixture_reaches_every_provenance_kind(self) -> None:
        # A fixture whose readings all landed on judged boundaries could not
        # tell a cited edge from a judged one however carefully the payload
        # carried the difference, so the tests below would pass over a page
        # that had merged them.
        kinds = set()
        for day in (REC_FULL_DAY, REC_SOLO_DAY):
            for reading in self.block(day)["ranked"]:
                for edge in ("lower_provenance", "upper_provenance"):
                    if reading[edge]:
                        kinds.add(reading[edge]["kind"])
        self.assertEqual(kinds, set(PROVENANCE_KINDS))

    # --- every metric is computed, and computed from its `measurement` ---

    def test_every_metric_in_the_table_lands_in_exactly_one_state(self) -> None:
        # `assess_all()` refuses a partial mapping, so a forgotten metric would
        # raise rather than pass -- but only if `_recommendations()` keeps
        # passing every key. This asserts the state that proves it did: the
        # THREE parts of the payload partition `METRICS` exactly, on every
        # window.
        #
        # #93 made it three rather than two, and the third is not decoration:
        # every window below except the full day has at least one metric in it,
        # because a fixture small enough to be readable is a fixture small
        # enough to be under-sampled. Before the third part existed those
        # metrics were `ranked`, with severities.
        for day in (
            REC_FULL_DAY,
            REC_SOLO_DAY,
            REC_NO_CACHE_DAY,
            REC_UNWINDOWED_DAY,
            REC_EMPTY_DAY,
            None,
        ):
            with self.subTest(day=day):
                block = self.block(day)
                ranked = {a["metric"] for a in block["ranked"]}
                unmeasured = set(block["unmeasured"])
                under = set(block["under_sampled"])
                self.assertEqual(ranked | unmeasured | under, set(METRICS))
                # Pairwise disjoint, all three ways: a metric in two states is
                # the collapse this change exists to prevent, and "the union is
                # everything" alone would not catch it.
                self.assertEqual(ranked & unmeasured, set())
                self.assertEqual(ranked & under, set())
                self.assertEqual(unmeasured & under, set())
                # And against what `serve` DECLARES it computes (#84). The
                # import-time guard compares that declaration with the table;
                # this compares it with what a served payload actually holds,
                # so a declaration that had drifted from the mapping below it
                # cannot pass by agreeing with the table alone.
                self.assertEqual(
                    ranked | unmeasured | under, set(RECOMMENDED_METRICS)
                )

    def test_cache_reads_per_write_divides_the_tokens_its_measurement_names(
        self,
    ) -> None:
        # TOKENS over TOKENS, both scopes -- not calls, and not the main thread
        # alone. The fixture's subagent cache traffic is a small fraction of the
        # main thread's, so a query that dropped it still returns a plausible
        # ratio; it just returns the wrong one.
        self.assertAlmostEqual(
            self.reading(REC_FULL_DAY, METRIC_CACHE_READS_PER_WRITE)["value"],
            REC_FULL_CACHE_READS / REC_FULL_CACHE_WRITES,
        )

    def test_cache_write_only_share_counts_calls_not_tokens(self) -> None:
        # CALLS over CALLS, over the calls that wrote cache -- 2 of 7 here. The
        # same day's write-only TOKEN share is a completely different number, so
        # a denominator taken from the token sums cannot pass.
        self.assertAlmostEqual(
            self.reading(REC_FULL_DAY, METRIC_CACHE_WRITE_ONLY_SHARE)["value"],
            REC_FULL_WRITE_ONLY_CALLS / REC_FULL_WRITING_CALLS,
        )

    def test_a_call_that_wrote_no_cache_is_outside_the_write_only_share(self) -> None:
        # Both ends of it: the fixture's third subagent call stored nothing, so
        # it is in neither the numerator nor the denominator. Counting it in the
        # denominator would read 2/8, and in both 3/8 -- both plausible.
        value = self.reading(REC_FULL_DAY, METRIC_CACHE_WRITE_ONLY_SHARE)["value"]
        full = [c for c in REC_CALLS if c[0] == REC_FULL_DAY]
        self.assertNotAlmostEqual(value, REC_FULL_WRITE_ONLY_CALLS / len(full))
        self.assertNotAlmostEqual(value, (REC_FULL_WRITE_ONLY_CALLS + 1) / len(full))

    # --- #84: the per-TTL repayment, over the calls whose split was measured ---

    def test_the_per_ttl_repayment_weights_each_ttl_at_its_own_break_even(
        self,
    ) -> None:
        # The metric's whole reason for existing: 1 read token per 5-minute
        # write token, 2 per 1-hour write token, one per TTL rather than
        # averaged. The fixture's measured set is mostly 1-hour by weight, so
        # the two multipliers swapped is a DIFFERENT number (2.097 against
        # 2.862) and cannot pass -- which a fixture whose writes were all one
        # TTL would not have caught.
        self.assertAlmostEqual(
            self.reading(REC_FULL_DAY, METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL)[
                "value"
            ],
            REC_FULL_REPAYMENT,
        )
        # 2 x 109,000 + 1 x 40,000 = 258,000, the denominator a swap produces.
        self.assertNotAlmostEqual(
            REC_FULL_REPAYMENT, REC_FULL_SPLIT_READS / 258_000
        )

    def test_the_reads_come_from_the_same_calls_as_the_writes(self) -> None:
        # THE defect this metric is most likely to ship with, and it inflates:
        # the day's split-measured calls read 541,000 tokens while the window
        # reads 1,525,000, so a numerator taken over the whole window against a
        # denominator taken over the measured set reports 8.07 -- a project
        # comfortably repaying its writes -- where the measured set reports
        # 2.86. Both are plausible readings in the same band's neighbourhood,
        # which is why only the number can tell them apart.
        value = self.reading(REC_FULL_DAY, METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL)[
            "value"
        ]
        # Asserted as the PROPERTY, not against one wrong figure: recover the
        # numerator the served value implies and check WHICH SET it came from.
        # A `assertNotAlmostEqual` against a single hand-computed alternative
        # passes for every other way of widening the numerator, and the first
        # mutation tried -- dropping the `WHERE` from the whole query, so both
        # sides widen but by different amounts -- was exactly that.
        implied_reads = value * REC_FULL_REQUIRED
        self.assertAlmostEqual(implied_reads, REC_FULL_SPLIT_READS, places=6)
        self.assertNotAlmostEqual(implied_reads, REC_FULL_CACHE_READS, places=6)
        # And the direction, stated rather than left to be inferred: reads that
        # do not belong to the measured set can only INFLATE the ratio, so the
        # honest reading is the smaller one.
        self.assertLess(value, REC_FULL_CACHE_READS / REC_FULL_REQUIRED)

    def test_a_call_with_only_one_ttl_read_is_in_neither_side(self) -> None:
        # A partial split is a half-measurement, not a 5-minute write. The
        # fixture's half-split call wrote 6,100 tokens and read none back, so
        # admitting it to the denominator alone reads 2.773 against 2.862 --
        # close enough to look like rounding and wrong for a reason that would
        # never be found from the number.
        self.assertNotAlmostEqual(
            self.reading(REC_FULL_DAY, METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL)[
                "value"
            ],
            REC_FULL_SPLIT_READS / (REC_FULL_REQUIRED + 6_100),
        )

    def test_the_per_ttl_metric_is_not_the_flat_ratio_under_another_name(
        self,
    ) -> None:
        # Two metrics, two sets, two numbers -- and both are ranked on this
        # day, so a wiring that computed the flat ratio twice would show a
        # reader one figure claiming to answer two questions.
        flat = self.reading(REC_FULL_DAY, METRIC_CACHE_READS_PER_WRITE)["value"]
        per_ttl = self.reading(REC_FULL_DAY, METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL)[
            "value"
        ]
        self.assertNotAlmostEqual(flat, per_ttl)
        self.assertAlmostEqual(flat, REC_FULL_CACHE_READS / REC_FULL_CACHE_WRITES)

    def test_a_window_whose_calls_predate_the_split_has_no_repayment(self) -> None:
        # THE state of every database today: #84 reads the split going forward
        # and no past call can be re-ingested to acquire one, so on a corpus
        # that has not been re-ingested the metric is UNMEASURED. Not 0, which
        # would band as the worst reading in the table and tell the reader
        # their cache writes are never repaid -- over calls nobody measured.
        block = self.block(REC_SOLO_DAY)
        self.assertIn(METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, block["unmeasured"])
        self.assertNotIn(
            METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
            [a["metric"] for a in block["ranked"]],
        )
        # The flat ratio IS measured on that same day, which is the point of
        # keeping both: a blanket "this day has no cache figure" would pass the
        # assertion above while saying something false.
        self.assertIn(
            METRIC_CACHE_READS_PER_WRITE, [a["metric"] for a in block["ranked"]]
        )

    def test_a_measured_split_of_zero_requires_nothing_and_is_unmeasured(self) -> None:
        # The other absence, and a different one: the split here WAS read and
        # both TTLs are 0, so the denominator is a measured zero. Nothing was
        # required, so nothing can have repaid it -- `inf` is what the
        # arithmetic offers and `assess()` refuses it, so the module returns
        # None one step earlier.
        self.assertIn(
            METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
            self.block(REC_NO_CACHE_DAY)["unmeasured"],
        )

    def test_the_reply_ratio_is_per_api_call_as_its_measurement_says(self) -> None:
        # The metric's KEY says "per reply"; its `measurement` says "per API
        # call", and the measurement is the field that names what is divided by
        # what. The fixture's two scopes run different numbers of calls (5 and
        # 3), so a ratio of the raw token SUMS -- the reading the key invites --
        # is a different number, and is pinned here as one this must not be.
        value = self.reading(REC_FULL_DAY, METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY)
        self.assertAlmostEqual(
            value["value"],
            (REC_FULL_MAIN_TOTAL / REC_FULL_MAIN_CALLS)
            / (REC_FULL_SUB_TOTAL / REC_FULL_SUB_CALLS),
        )
        self.assertNotAlmostEqual(
            value["value"], REC_FULL_MAIN_TOTAL / REC_FULL_SUB_TOTAL
        )

    def test_the_saturation_share_is_the_bands_the_page_already_renders(self) -> None:
        # ONE definition of "over half the window", not two. The value is
        # checked against the per-scope band tally in the SAME payload, so a
        # second query here could not drift from the figures beside it -- and
        # against the hand-written count, so the two agreeing wrongly is not
        # enough to pass.
        summary = self.summary(REC_FULL_DAY)
        main = [
            s
            for s in summary["context"]["utilisation"]["by_scope"]
            if s["scope"] == SCOPE_MAIN
        ][0]
        over_half = sum(
            b["calls"] for b in main["bands"] if b["band"] in OVER_HALF_WINDOW_BANDS
        )
        self.assertEqual(over_half, REC_FULL_MAIN_OVER_HALF)
        self.assertEqual(main["banded_calls"], REC_FULL_MAIN_BANDED)
        served = [
            a
            for a in summary["recommendations"]["ranked"]
            if a["metric"] == METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW
        ][0]
        self.assertAlmostEqual(served["value"], over_half / main["banded_calls"])
        self.assertAlmostEqual(
            served["value"], REC_FULL_MAIN_OVER_HALF / REC_FULL_MAIN_BANDED
        )

    def test_the_saturation_share_excludes_the_subagent_scope(self) -> None:
        # Its denominator is MAIN-THREAD calls with a known window. The
        # fixture's subagent calls all sit under a quarter of their window, so
        # pooling the two scopes leaves a plausible smaller share -- 2/8 rather
        # than 2/5 -- which is the dilution #61 exists to refuse, arriving here
        # through a metric instead of through a band.
        pooled = self.summary(REC_FULL_DAY)["context"]["utilisation"]["banded_calls"]
        self.assertGreater(pooled, REC_FULL_MAIN_BANDED)
        self.assertNotAlmostEqual(
            self.reading(REC_FULL_DAY, METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW)[
                "value"
            ],
            REC_FULL_MAIN_OVER_HALF / pooled,
        )

    # --- a zero denominator is None, and is NAMED ---

    def test_a_project_that_dispatched_no_subagent_has_no_ratio(self) -> None:
        # THE case. Zero subagent replies is not a ratio of zero -- it is no
        # ratio -- and a `0.0` here would band as the healthiest possible
        # reading and tell the reader their work is already landing where it is
        # cheapest to run.
        block = self.block(REC_SOLO_DAY)
        self.assertIn(METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY, block["unmeasured"])
        self.assertNotIn(
            METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,
            [a["metric"] for a in block["ranked"]],
        )

    def test_no_call_wrote_cache_so_both_cache_metrics_are_unmeasured(self) -> None:
        # Two denominators that vanish together here and are different
        # quantities -- a token sum and a call count. A ratio of 0/0 is not 0,
        # and `0.0` reads per write would fire the table's worst entry at a
        # project whose cache nobody used.
        block = self.block(REC_NO_CACHE_DAY)
        for metric in (METRIC_CACHE_READS_PER_WRITE, METRIC_CACHE_WRITE_ONLY_SHARE):
            with self.subTest(metric=metric):
                self.assertIn(metric, block["unmeasured"])

    def test_an_unwindowed_main_thread_has_no_saturation_share(self) -> None:
        # A share of an empty set is not 0%. Every main-thread call this day ran
        # on a model with no documented window, so nothing was banded and there
        # is no denominator -- while other metrics still have one, which is
        # what stops a blanket "this day is unmeasured" from passing.
        block = self.block(REC_UNWINDOWED_DAY)
        self.assertIn(METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW, block["unmeasured"])
        self.assertNotEqual(block["ranked"], [])

    def test_an_empty_window_is_every_metric_unmeasured_and_no_advice(self) -> None:
        # The state a page that rendered absence as health would describe as a
        # clean bill: nothing measured, so nothing to advise -- said out loud
        # rather than by an empty block.
        block = self.block(REC_EMPTY_DAY)
        self.assertEqual(block["ranked"], [])
        self.assertEqual(set(block["unmeasured"]), set(METRICS))

    def test_an_unmeasured_metric_is_named_with_what_would_have_been_measured(
        self,
    ) -> None:
        # Not merely absent from `ranked`: present, named, and carrying the
        # measurement it could not take, so the reader can see WHICH figure is
        # missing rather than inferring it from a gap.
        block = self.block(REC_EMPTY_DAY)
        for metric, measurement in block["unmeasured"].items():
            with self.subTest(metric=metric):
                self.assertEqual(measurement, METRICS[metric].measurement)
        self.assertEqual(block["unmeasured_note"], UNMEASURED_NOTE)

    def test_no_metric_is_ever_reported_as_a_measured_zero(self) -> None:
        # The rule in one assertion, over every window: a metric is either in
        # `ranked` with a real reading or in `unmeasured` with none. There is no
        # third state, and in particular no entry whose value is a 0 that
        # arithmetic produced from an empty denominator.
        for day in (REC_NO_CACHE_DAY, REC_UNWINDOWED_DAY, REC_EMPTY_DAY, REC_SOLO_DAY):
            for reading in self.block(day)["ranked"]:
                with self.subTest(day=day, metric=reading["metric"]):
                    self.assertIsNotNone(reading["value"])

    # --- provenance, per boundary, with its kind ---

    def test_a_cited_boundary_and_a_judged_one_are_distinguishable(self) -> None:
        # THE requirement of #78, at the payload boundary. The fixture's
        # reads-per-write reading sits in a range whose LOWER edge is a
        # documented fact and whose UPPER edge is a product-owner judgment --
        # one range, two kinds -- so a payload that carried one provenance for
        # the pair could not pass.
        reading = self.reading(REC_FULL_DAY, METRIC_CACHE_READS_PER_WRITE)
        self.assertEqual(reading["lower_provenance"]["kind"], PROVENANCE_CITED)
        self.assertEqual(reading["upper_provenance"]["kind"], PROVENANCE_JUDGED)
        self.assertNotEqual(
            reading["lower_provenance"]["statement"],
            reading["upper_provenance"]["statement"],
        )

    def test_a_cited_boundary_carries_its_source_check_date_and_coverage(self) -> None:
        # A citation whose coverage is unstated is the one that gets applied to
        # the case it was never checked against -- which is exactly what
        # happened to the `1.0` boundary before #83 split it in two.
        cited = self.reading(REC_FULL_DAY, METRIC_CACHE_READS_PER_WRITE)[
            "lower_provenance"
        ]
        self.assertTrue(cited["source"])
        self.assertRegex(cited["checked"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(cited["covers"])

    def test_a_judged_boundary_crosses_the_api_with_no_source_at_all(self) -> None:
        # NULL, not "" and not "n/a": the page has to be able to tell that a
        # judgment HAS no source from the payload alone. The module refuses to
        # construct one with a source, and this is the other end of that -- a
        # serialiser that substituted a plausible string would undo it.
        for day, metric in (
            (REC_FULL_DAY, METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW),
            (REC_FULL_DAY, METRIC_CACHE_WRITE_ONLY_SHARE),
        ):
            with self.subTest(metric=metric):
                lower = self.reading(day, metric)["lower_provenance"]
                self.assertEqual(lower["kind"], PROVENANCE_JUDGED)
                self.assertIsNone(lower["source"])
                self.assertIsNone(lower["covers"])

    def test_a_structural_boundary_carries_neither_source_nor_check_date(self) -> None:
        # There is nothing to re-check about "a share cannot be negative", and a
        # date beside it would claim a currency it does not have.
        #
        # #93 moved this off `cache_write_only_share` on the solo day, whose 16
        # cache-writing calls are below that metric's derived floor of 51 -- so
        # it has no banded reading there any more, which is the correct answer
        # and not one this test can read a boundary off. The saturation share on
        # the same day sits in the same FIRST range, whose lower edge is the
        # same kind of domain floor, over 16 banded calls against a floor of 11.
        lower = self.reading(REC_SOLO_DAY, METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW)[
            "lower_provenance"
        ]
        self.assertEqual(lower["kind"], PROVENANCE_STRUCTURAL)
        self.assertIsNone(lower["source"])
        self.assertIsNone(lower["checked"])

    def test_an_open_ended_range_has_no_upper_provenance(self) -> None:
        # None, because there is no boundary there -- not a provenance for a
        # ceiling nobody set.
        reading = self.reading(REC_FULL_DAY, METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY)
        self.assertIsNone(reading["range_upper"])
        self.assertIsNone(reading["upper_provenance"])

    def test_both_table_level_provenances_cross_the_api(self) -> None:
        block = self.block(REC_FULL_DAY)
        self.assertEqual(block["provenance"], RECOMMENDATION_PROVENANCE)
        self.assertEqual(block["ranking_provenance"], RANKING_PROVENANCE)
        self.assertEqual(block["as_of"], RECOMMENDATIONS_AS_OF)
        # Two SEPARATE statements, as the window's and the bands' are: the
        # entries and the order they are shown in are different judgments, and
        # one sentence covering both would let a reader attach it to whichever
        # they were reading.
        self.assertNotEqual(block["provenance"], block["ranking_provenance"])

    # --- the ranking is derived, never authored ---

    def test_the_ranking_is_severity_then_depth_then_key(self) -> None:
        # Recomputed from the payload's OWN fields, so the order and the key it
        # claims to use cannot disagree -- `RANKED_BY`'s rule where the key is a
        # derived depth rather than a column.
        ranked = self.block(REC_FULL_DAY)["ranked"]
        self.assertEqual(
            [a["metric"] for a in ranked],
            [
                a["metric"]
                for a in sorted(
                    ranked,
                    key=lambda a: (
                        -SEVERITY_RANK[a["severity"]],
                        -a["depth_in_severity"],
                        a["metric"],
                    ),
                )
            ],
        )

    def test_the_ranking_is_not_the_tables_authoring_order(self) -> None:
        # Teeth on the test above, which a table whose authoring order happened
        # to be the ranked order would pass without saying anything. The fixture
        # is built so the two visibly differ.
        ranked = [a["metric"] for a in self.block(REC_FULL_DAY)["ranked"]]
        self.assertNotEqual(ranked, [k for k in METRICS if k in ranked])

    def test_the_worst_severity_is_ranked_first(self) -> None:
        severities = [
            SEVERITY_RANK[a["severity"]] for a in self.block(REC_FULL_DAY)["ranked"]
        ]
        self.assertEqual(severities, sorted(severities, reverse=True))
        # And the fixture actually holds more than one severity, so a sort that
        # never ran would not pass here.
        self.assertGreater(len(set(severities)), 1)

    # --- an `ok` is a positive statement ---

    def test_a_healthy_reading_is_ranked_rather_than_dropped(self) -> None:
        # "Nothing to change here" is a FINDING. Represented by absence, it
        # would be indistinguishable from a metric nobody measured -- which is
        # the rule this whole repository is built on, and the reason the healthy
        # range is an entry in the table rather than the gap between two.
        healthy = [
            a
            for a in self.block(REC_SOLO_DAY)["ranked"]
            if a["severity"] == SEVERITY_OK
        ]
        self.assertNotEqual(healthy, [])
        for reading in healthy:
            with self.subTest(metric=reading["metric"]):
                self.assertTrue(reading["recommendation"].strip())
                # A healthy entry carries no lever by construction: "nothing to
                # change" and "change this" cannot both be true.
                self.assertIsNone(reading["lever"])

    def test_a_firing_reading_always_names_the_lever_to_pull(self) -> None:
        for reading in self.block(REC_FULL_DAY)["ranked"]:
            if reading["severity"] == SEVERITY_OK:
                continue
            with self.subTest(metric=reading["metric"]):
                self.assertIsNotNone(reading["lever"])
                self.assertIn(reading["lever"]["action"], ACTION_VERBS)
                self.assertIn(reading["lever"]["target"], LEVER_TARGETS)

    # --- the discounted class stays unreducible ---

    def test_no_served_advice_asks_for_less_of_a_discounted_class(self) -> None:
        # Cache read is the 0.1x class. Advice to shrink it would be advice to
        # re-send the prefix uncached at ten times the tokens, and the report
        # would be confidently recommending the more expensive of two options.
        for day in (REC_FULL_DAY, REC_SOLO_DAY, REC_NO_CACHE_DAY, REC_UNWINDOWED_DAY):
            for reading in self.block(day)["ranked"]:
                lever = reading["lever"]
                if lever is None:
                    continue
                with self.subTest(day=day, metric=reading["metric"]):
                    self.assertFalse(
                        lever["action"] == ACTION_REDUCE
                        and lever["target"] in DISCOUNTED_TOKEN_CLASSES
                    )

    def test_the_served_directive_is_the_modules_own_words(self) -> None:
        # `lever()` refuses to build a reduce-directive over a discounted class,
        # and `Lever.directive` composes from a closed registry. A directive
        # assembled in `serve.py` from `action` and `target` would route around
        # both guards while looking identical on this fixture, so the served
        # string is pinned to the one the module composes.
        for reading in self.block(REC_FULL_DAY)["ranked"]:
            lever = reading["lever"]
            if lever is None:
                continue
            with self.subTest(metric=reading["metric"]):
                self.assertEqual(
                    lever["directive"],
                    Lever(action=lever["action"], target=lever["target"]).directive,
                )

    def test_the_reduce_lever_the_fixture_fires_is_a_full_price_class(self) -> None:
        # Teeth: a corpus that never fired a `reduce` at all would pass the two
        # tests above without exercising them. The write-only share fires one
        # here, on `cache_write` -- the 1.25x class, which IS reducible.
        levers = [
            a["lever"]
            for a in self.block(REC_FULL_DAY)["ranked"]
            if a["lever"] and a["lever"]["action"] == ACTION_REDUCE
        ]
        self.assertNotEqual(levers, [])
        for lever in levers:
            self.assertNotIn(lever["target"], DISCOUNTED_TOKEN_CLASSES)

    # --- no money, still ---

    def test_the_block_carries_no_money_shaped_field(self) -> None:
        # #30 reaches new payloads too. The table is about token multipliers;
        # the currency figure they would imply is exactly the arithmetic this
        # project deleted rather than qualified.
        blob = json.dumps(self.block(REC_FULL_DAY)).lower()
        for token in ("cost", "usd", "dollar", "$", "price", "spend"):
            with self.subTest(token=token):
                self.assertNotIn(token, blob)


# ---------------------------------------------------------------------------
# #93: what a fresh install actually renders.
# ---------------------------------------------------------------------------
#
# THE ONE STATE NOBODY TESTED. Every issue in this project was measured against
# a corpus with thousands of calls; the first thirty seconds of the beta are
# spent here. Built through the real ingest path rather than by handing values
# to `assess_all()`, because the defect was never in the table -- every figure
# it produced was individually correct -- and lived entirely in what the layers
# above did with a sample too small to carry them.
FIRST_RUN_SESSION = "first-run-fixture"
FIRST_RUN_MODEL = REC_OPUS_1M
# Three assistant replies. Deliberately shaped to reproduce the issue's own
# reading: 7,200 read tokens over 300 written is 24.0 reads per write, which is
# what the report told a three-reply project not to change.
FIRST_RUN_REPLIES = 3
FIRST_RUN_READ = 2_400
FIRST_RUN_WRITE = 100
FIRST_RUN_READS_PER_WRITE = 24.0


def build_first_run_corpus(
    root: Path, replies: int = FIRST_RUN_REPLIES, read: int = FIRST_RUN_READ
) -> Path:
    """One session, `replies` assistant replies, NO subagents.

    The zero-subagent half matters as much as the small count: it is what makes
    `main_vs_subagent_tokens_per_reply` UNMEASURED rather than under-sampled,
    so one corpus strands the table in both absences at once and a page that
    rendered them alike cannot pass.
    """
    project = root / "projects" / "-fixture-first-run"
    project.mkdir(parents=True)
    lines = [json.dumps({"type": "mode", "mode": "normal",
                         "sessionId": FIRST_RUN_SESSION})]
    for n in range(replies):
        ts = f"2026-08-06T10:{n // 60:02d}:{n % 60:02d}.000Z"
        lines.append(json.dumps({
            "type": "user", "sessionId": FIRST_RUN_SESSION, "timestamp": ts,
            "message": {"role": "user", "content": "do a thing"},
        }))
        lines.append(json.dumps({
            "type": "assistant",
            "sessionId": FIRST_RUN_SESSION,
            "timestamp": ts,
            "isSidechain": False,
            "message": {
                "id": f"msg-first-run-{n}",
                "model": FIRST_RUN_MODEL,
                "usage": {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": FIRST_RUN_WRITE,
                    "cache_read_input_tokens": read,
                    "output_tokens": 50,
                },
                "content": [{"type": "text", "text": f"reply {n}"}],
            },
        }))
    (project / f"{FIRST_RUN_SESSION}.jsonl").write_text("\n".join(lines) + "\n")
    return project


class FirstRunRendersNoVerdictTest(unittest.TestCase):
    """#93: five green verdicts over three calls, and what replaced them.

    Reproduced from the issue: one session, three replies, no subagents. The
    report said health `ok`, four green dots, four knobs reading "Nothing to
    turn here" and one "No reading" -- and `cache_reads_per_write` did not
    merely show green, it said "Do not change this." An instruction, in the
    product owner's voice, derived from three calls.

    Every figure was correct. The COMPOSITION asserted a clean bill of health
    on evidence that could not support one, which is this repository's central
    rule failing one level above the level it had been applied to.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-93-test-"))
        db_path = cls.tmp / "usage.db"
        ingest(
            build_first_run_corpus(cls.tmp),
            db_path,
            tasks_dir=cls.tmp / "no-task-index",
        )
        cls.api = Api(db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def summary(self) -> dict:
        return self.api.summary(*day_bounds(None, None))

    def block(self) -> dict:
        return self.summary()["recommendations"]

    def dot(self, key: str) -> dict:
        return next(d for d in self.summary()["status"]["dots"] if d["key"] == key)

    # --- the corpus is the one the issue described -------------------------

    def test_the_fixture_is_a_first_run_and_reproduces_the_issues_reading(self):
        # If this corpus quietly grew, every assertion below would be about
        # some other state. The reading is pinned too: 24.0 is the figure the
        # report told a three-reply project not to change.
        payload = self.summary()
        self.assertEqual(payload["calls"], FIRST_RUN_REPLIES)
        self.assertEqual(payload["sessions"], 1)
        knob = next(
            k
            for k in payload["recommendations"]["knobs"]
            if k["metric"] == METRIC_CACHE_READS_PER_WRITE
        )
        self.assertEqual(knob["value"], FIRST_RUN_READS_PER_WRITE)

    # --- no verdict is reached over it -------------------------------------

    def test_not_one_knob_is_banded(self):
        block = self.block()
        self.assertEqual(block["ranked"], [])
        self.assertEqual(
            [
                k["metric"]
                for k in block["knobs"]
                if k["sample"]["state"] == SAMPLE_MEASURED
            ],
            [],
        )

    def test_no_knob_carries_a_severity_or_a_directive(self):
        # "ok" is the one that matters. A green severity over three calls is
        # the defect; a `watch` would have been wrong in the same way and is
        # refused by the same code, so this asserts the absence of ALL of them.
        for knob in self.block()["knobs"]:
            with self.subTest(metric=knob["metric"]):
                self.assertIsNone(knob["severity"])
                self.assertIsNone(knob["directive"])

    def test_no_dial_carries_a_needle(self):
        # The verdict in its second notation. A needle under the green arc says
        # "do not change this" in a picture after the words have stopped.
        for knob in self.block()["knobs"]:
            with self.subTest(metric=knob["metric"]):
                self.assertIsNone(knob["gauge"]["needle"])

    def test_the_true_readings_survive_the_withheld_verdicts(self):
        # The numbers were measured and are true, so they are shown. What is
        # withheld is the claim ABOUT them. A change that hid the figures too
        # would be answering a composition defect by deleting measurements.
        under = self.block()["under_sampled"]
        values = {
            k["metric"]: k["value"]
            for k in self.block()["knobs"]
            if k["metric"] in under
        }
        self.assertEqual(values[METRIC_CACHE_READS_PER_WRITE], 24.0)
        self.assertEqual(values[METRIC_CACHE_WRITE_ONLY_SHARE], 0.0)

    # --- the three states are three ----------------------------------------

    def test_the_two_absences_are_told_apart(self):
        block = self.block()
        # Under-sampled: a reading exists and the sample cannot carry it.
        self.assertEqual(
            sorted(block["under_sampled"]),
            [
                METRIC_CACHE_READS_PER_WRITE,
                METRIC_CACHE_WRITE_ONLY_SHARE,
                METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW,
            ],
        )
        # Unmeasured: no reading at all, and no number of further sessions
        # changes that for a project that never dispatches a subagent.
        self.assertEqual(
            sorted(block["unmeasured"]),
            [
                METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
                METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,
            ],
        )

    def test_each_under_sampled_row_says_how_short_it_is_and_of_what(self):
        block = self.block()
        for metric, sentence in block["under_sampled"].items():
            with self.subTest(metric=metric):
                floor = METRICS[metric].sample_floor
                # The count it has, the count it needs, and the NOUN -- "51" is
                # a different fact against cache-writing calls than against
                # calls, and a sentence with only the number says neither.
                self.assertIn(str(FIRST_RUN_REPLIES), sentence)
                self.assertIn(str(floor.minimum), sentence)
                self.assertIn(floor.counts, sentence)
                self.assertIn(str(floor.minimum - FIRST_RUN_REPLIES), sentence)

    def test_the_note_tells_a_new_user_what_to_do(self):
        # The one genuinely useful thing the report can say to somebody who has
        # just installed it.
        self.assertEqual(self.block()["under_sampled_note"], UNDER_SAMPLED_NOTE)
        self.assertIn("come back", self.block()["under_sampled_note"])

    def test_the_two_notes_are_not_one_note(self):
        block = self.block()
        self.assertNotEqual(block["under_sampled_note"], block["unmeasured_note"])

    # --- the strip cannot go green over it ---------------------------------

    def test_the_knob_dot_is_not_green_and_does_not_count_to_five(self):
        dot = self.dot(STRIP_DOT_KNOBS)
        self.assertEqual(dot["state"], STRIP_UNKNOWN)
        # "0 of 5" is arithmetic over an empty set that reads exactly like five
        # checks passing. That sentence is what a fresh install saw.
        self.assertNotIn("of 5", dot["answer"])
        self.assertEqual(dot["answer"], STRIP_UNDER_SAMPLED)

    def test_the_cache_dot_is_not_green_and_names_the_right_absence(self):
        dot = self.dot(STRIP_DOT_CACHE)
        self.assertEqual(dot["state"], STRIP_UNKNOWN)
        # Under-sampled, not unmeasured: two of the three cache metrics have
        # readings here. A dot that said "Not measured" would send the reader
        # to the wrong remedy.
        self.assertEqual(dot["answer"], STRIP_UNDER_SAMPLED)
        self.assertNotEqual(dot["answer"], STRIP_NOT_MEASURED)

    def test_the_context_dot_is_not_green_either(self):
        # #93, second pass, and the defect this class first shipped WITH. The
        # page rendered a green "Wasting context? -- No" directly above a row
        # reading `TOO FEW - main_thread_share_over_half_window - 5 of 11`, and
        # a reader cannot hold both. "Am I wasting context?" is a judgment; it
        # is the judgment that metric makes, and it is owed that metric's floor.
        dot = self.dot(STRIP_DOT_CONTEXT)
        self.assertEqual(dot["state"], STRIP_UNKNOWN)
        self.assertEqual(dot["answer"], STRIP_UNDER_SAMPLED)
        # The card underneath still answers its own, narrower question -- the
        # observation is complete over these three calls and is not withdrawn.
        # Only the judgment built on top of it is.
        self.assertEqual(
            self.summary()["context"]["utilisation"]["answer"]["verdict"],
            CONTEXT_ANSWER_NO,
        )

    def test_no_dot_backed_by_the_table_is_good(self):
        # GREEN MEANS MEASURED AND HEALTHY. Every dot whose question is
        # answered by a metric in the table has no basis here and may not wear
        # the colour of one that has.
        #
        # "Anything broken?" is deliberately excluded and deliberately still
        # green-capable: nothing was unparsed, nothing skipped and no model
        # unknown, which is a COMPLETE statement over whatever was ingested
        # rather than a rate estimated from a sample. It answers to no floor
        # because there is no floor a count of parse failures could need.
        for key in (STRIP_DOT_CONTEXT, STRIP_DOT_KNOBS, STRIP_DOT_CACHE):
            with self.subTest(dot=key):
                self.assertNotEqual(self.dot(key)["state"], STRIP_GOOD)

    # --- growing out of it -------------------------------------------------

    def test_enough_sessions_earn_the_verdicts_back(self):
        # The floor is a floor and not a wall: the same corpus, longer, bands
        # what it could not band at three. Without this the change would be
        # indistinguishable from one that simply stopped assessing.
        tmp = Path(tempfile.mkdtemp(prefix="usage-report-93-grown-"))
        try:
            db_path = tmp / "usage.db"
            ingest(
                build_first_run_corpus(tmp, replies=60),
                db_path,
                tasks_dir=tmp / "no-task-index",
            )
            api = Api(db_path)
            try:
                block = api.summary(*day_bounds(None, None))["recommendations"]
            finally:
                api.conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        banded = {a["metric"] for a in block["ranked"]}
        self.assertIn(METRIC_CACHE_READS_PER_WRITE, banded)
        self.assertIn(METRIC_CACHE_WRITE_ONLY_SHARE, banded)
        self.assertIn(METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW, banded)
        self.assertEqual(block["under_sampled"], {})
        # The reading is the SAME reading -- 24.0 at three replies and at 60 --
        # so what changed is only whether it may be banded, which is the whole
        # claim this change makes.
        reads = next(
            a for a in block["ranked"] if a["metric"] == METRIC_CACHE_READS_PER_WRITE
        )
        self.assertEqual(reads["value"], FIRST_RUN_READS_PER_WRITE)


class AProvenYesSurvivesTheFloorTest(unittest.TestCase):
    """#93, second pass: the floor weakens a clean answer, never a bad one.

    The direction is `CONTEXT_ANSWER_STATES`' own, written down long before
    this change: "an unknown may weaken a `no` and never a `yes` -- a proven
    saturation is not softened by the calls that could not be measured beside
    it." A call observed at or above half its window HAPPENED; the remedy is
    real, and a thin sample is no reason to hide it. Holding the dot to a floor
    in both directions would have answered one over-claim with the opposite
    error, and it is the error this repository never makes.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-93-yes-"))
        db_path = cls.tmp / "usage.db"
        # Three replies, each carrying 70% of a 1,000,000-token window: too few
        # to band the share, and every one of them proven over half.
        ingest(
            build_first_run_corpus(cls.tmp, read=700_000),
            db_path,
            tasks_dir=cls.tmp / "no-task-index",
        )
        cls.api = Api(db_path)
        cls.payload = cls.api.summary(*day_bounds(None, None))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_fixture_is_the_case_it_claims_to_be(self):
        backing = next(
            k
            for k in self.payload["recommendations"]["knobs"]
            if k["metric"] == CONTEXT_DOT_METRIC
        )
        self.assertEqual(backing["sample"]["state"], SAMPLE_UNDER_SAMPLED)
        self.assertEqual(
            self.payload["context"]["utilisation"]["answer"]["verdict"],
            CONTEXT_ANSWER_YES,
        )

    def test_the_dot_stays_bad_over_an_under_sampled_metric(self):
        dot = next(
            d for d in self.payload["status"]["dots"] if d["key"] == STRIP_DOT_CONTEXT
        )
        self.assertEqual(dot["state"], STRIP_BAD)
        self.assertNotEqual(dot["answer"], STRIP_UNDER_SAMPLED)

    def test_the_proven_answer_keeps_its_named_scope(self):
        # The scope is the ranking's own winner and is the actionable half of a
        # proven yes. A floor that swallowed it would leave the reader a red
        # dot with nothing to act on.
        dot = next(
            d for d in self.payload["status"]["dots"] if d["key"] == STRIP_DOT_CONTEXT
        )
        self.assertIn(str(self.payload["context"]["utilisation"]["worst_scope"]),
                      dot["answer"])

    def test_the_floor_helper_never_improves_a_state(self):
        # Directly, over every state the strip has, so the one-way property is
        # pinned independently of whatever a corpus happens to reach.
        under = {"sample": {"state": SAMPLE_UNDER_SAMPLED}}
        banded = {"sample": {"state": SAMPLE_MEASURED}}
        for state in STRIP_ORDER:
            with self.subTest(state=state):
                floored, _ = Api._floored(state, "answer", under)
                # `STRIP_ORDER` is WORST FIRST, so a lower index is a worse
                # state and "never improves" is the floored index being at most
                # the original's. Spelled through the published order rather
                # than as a comparison of the four names, so a reordering there
                # cannot silently invert this.
                self.assertLessEqual(
                    STRIP_ORDER.index(floored), STRIP_ORDER.index(state)
                )
                self.assertEqual(
                    Api._floored(state, "answer", banded), (state, "answer")
                )
        # ...and specifically: good is weakened, bad is not.
        self.assertEqual(
            Api._floored(STRIP_GOOD, "No", under), (STRIP_UNKNOWN, STRIP_UNDER_SAMPLED)
        )
        self.assertEqual(Api._floored(STRIP_BAD, "Yes", under), (STRIP_BAD, "Yes"))
        # An already-unknown dot keeps its own, more specific words: "no
        # sample" and "unknown" say different things and neither is improved by
        # being told to come back later.
        self.assertEqual(
            Api._floored(STRIP_UNKNOWN, "No sample", under),
            (STRIP_UNKNOWN, "No sample"),
        )


def build_subagent_only_corpus(root: Path) -> Path:
    """A window whose only calls are a subagent's.

    The main-thread share is then UNMEASURED -- no such call ran -- while the
    context card still answers cleanly, because every call in the period was
    measured, banded and inside its window.
    """
    project = root / "projects" / "-fixture-subagent-only"
    subagents = project / "sub-only" / "subagents"
    subagents.mkdir(parents=True)
    (project / "sub-only.jsonl").write_text(
        json.dumps({"type": "mode", "mode": "normal", "sessionId": "sub-only"}) + "\n"
    )
    lines = []
    for n in range(3):
        lines.append(json.dumps({
            "type": "assistant", "sessionId": "sub-only", "agentId": "agent-solo",
            "isSidechain": True,
            "timestamp": f"2026-08-06T10:0{n}:00.000Z",
            "message": {
                "id": f"msg-sub-only-{n}", "model": REC_OPUS_1M,
                "usage": {
                    "input_tokens": 100, "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 1_000, "output_tokens": 50,
                },
                "content": [{"type": "text", "text": f"sub {n}"}],
            },
        }))
    (subagents / "agent-solo.jsonl").write_text("\n".join(lines) + "\n")
    return project


class AnAbsentQuestionIsNotAnUnansweredOneTest(unittest.TestCase):
    """#93, second pass: the floor binds on UNDER-SAMPLED, never on UNMEASURED.

    A window holding only subagent calls has no main-thread share at all. The
    metric is unmeasured because no such call ran -- not because too few did --
    and the context card answers cleanly over what did run: every call
    measured, banded and inside its window.

    Telling that reader "come back after a few more sessions" would be a
    promise with no arithmetic behind it, and it is the mirror of the defect
    this change fixes rather than more of the same medicine. The distinction
    exists precisely because this repository keeps the two absences apart
    everywhere else.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-93-absent-"))
        db_path = cls.tmp / "usage.db"
        ingest(
            build_subagent_only_corpus(cls.tmp),
            db_path,
            tasks_dir=cls.tmp / "no-task-index",
        )
        cls.api = Api(db_path)
        cls.payload = cls.api.summary(*day_bounds(None, None))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_fixture_is_the_case_it_claims_to_be(self):
        backing = next(
            k
            for k in self.payload["recommendations"]["knobs"]
            if k["metric"] == CONTEXT_DOT_METRIC
        )
        self.assertEqual(backing["sample"]["state"], SAMPLE_UNMEASURED)
        self.assertEqual(
            self.payload["context"]["utilisation"]["answer"]["verdict"],
            CONTEXT_ANSWER_NO,
        )

    def test_the_dot_keeps_its_clean_answer(self):
        # Nothing here is waiting for data, so nothing here is told to wait.
        dot = next(
            d for d in self.payload["status"]["dots"] if d["key"] == STRIP_DOT_CONTEXT
        )
        self.assertEqual(dot["state"], STRIP_GOOD)
        self.assertNotEqual(dot["answer"], STRIP_UNDER_SAMPLED)

    def test_the_floor_helper_ignores_an_unmeasured_backing_reading(self):
        # Directly, so the decision is pinned independently of what any corpus
        # happens to reach.
        unmeasured = {"sample": {"state": SAMPLE_UNMEASURED}}
        self.assertEqual(
            Api._floored(STRIP_GOOD, "No", unmeasured), (STRIP_GOOD, "No")
        )


class SampleFloorIsPerPeriodTest(unittest.TestCase):
    """#93: the floor binds on the WINDOW, never on the database.

    Every figure on this report ranges over a period, so the sample a reading
    rests on is the period's and not the corpus's. A floor applied per database
    would wave a seven-day window through on the strength of history it does
    not describe -- the same wrong-set defect `SCOPE_*` labelling exists for,
    on the time axis.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-93-period-"))
        db_path = cls.tmp / "usage.db"
        ingest(
            build_recommendation_corpus(cls.tmp),
            db_path,
            tasks_dir=cls.tmp / "no-task-index",
        )
        cls.api = Api(db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def block(self, day):
        return self.api.summary(*day_bounds(day, day))["recommendations"]

    def sample(self, day, metric):
        return next(
            k for k in self.block(day)["knobs"] if k["metric"] == metric
        )["sample"]

    def test_a_metric_banded_over_the_database_is_under_sampled_in_one_day(self):
        # THE mutation: a floor compared against a count taken over the whole
        # `api_calls` table. `cache_write_only_share` clears 51 across this
        # corpus and does not clear it on the solo day, so a per-database floor
        # would band the solo day's reading -- a verdict about two calls'
        # worth of history, drawn from history outside the window.
        whole = self.sample(None, METRIC_CACHE_WRITE_ONLY_SHARE)
        solo = self.sample(REC_SOLO_DAY, METRIC_CACHE_WRITE_ONLY_SHARE)
        self.assertEqual(whole["state"], SAMPLE_MEASURED)
        self.assertEqual(solo["state"], SAMPLE_UNDER_SAMPLED)
        self.assertGreater(whole["size"], solo["size"])
        self.assertGreaterEqual(whole["size"], whole["minimum"])
        self.assertLess(solo["size"], solo["minimum"])
        self.assertIn(
            METRIC_CACHE_WRITE_ONLY_SHARE, self.block(REC_SOLO_DAY)["under_sampled"]
        )
        self.assertNotIn(
            METRIC_CACHE_WRITE_ONLY_SHARE, self.block(None)["under_sampled"]
        )

    def test_the_same_metric_is_still_banded_on_the_day_that_has_the_sample(self):
        # The other direction, so the test above cannot pass by the floor
        # simply refusing everything narrow. The full day holds 56 of them.
        full = self.sample(REC_FULL_DAY, METRIC_CACHE_WRITE_ONLY_SHARE)
        self.assertEqual(full["state"], SAMPLE_MEASURED)

    def test_a_windows_count_is_the_windows_own(self):
        # Every published size is strictly a slice of the whole, and the days
        # do not all agree -- a fixture where they did could not tell a
        # per-window count from a per-database one.
        sizes = {
            day: self.sample(day, METRIC_CACHE_WRITE_ONLY_SHARE)["size"]
            for day in (None, REC_FULL_DAY, REC_SOLO_DAY, REC_NO_CACHE_DAY)
        }
        self.assertEqual(len(set(sizes.values())), len(sizes))
        for day, size in sizes.items():
            if day is not None:
                with self.subTest(day=day):
                    self.assertLess(size, sizes[None])

    def test_the_published_floor_is_the_tables_and_not_a_copy(self):
        # A minimum spelled in `serve.py` would be a second enumeration of a
        # number the table derives, free to drift the moment a band moves --
        # which is `RECOMMENDED_METRICS`' whole subject, at the grain of a
        # count.
        for metric in METRICS:
            with self.subTest(metric=metric):
                sample = self.sample(None, metric)
                floor = METRICS[metric].sample_floor
                self.assertEqual(sample["minimum"], floor.minimum)
                self.assertEqual(sample["counts"], floor.counts)
                self.assertEqual(sample["rule"], floor.rule)
                self.assertEqual(sample["provenance_kind"], floor.provenance.kind)
                self.assertEqual(
                    sample["provenance_statement"], floor.provenance.statement
                )

    def test_a_judged_floor_reaches_the_page_saying_it_is_judged(self):
        # Both provenances stay separate and dated. A judged floor rendered in
        # the derived one's voice is the borrowed authority `band_provenance`
        # was introduced to refuse (#31), one field over.
        judged_floor = self.sample(None, METRIC_CACHE_READS_PER_WRITE)
        derived_floor = self.sample(None, METRIC_CACHE_WRITE_ONLY_SHARE)
        self.assertEqual(judged_floor["provenance_kind"], PROVENANCE_JUDGED)
        self.assertEqual(derived_floor["provenance_kind"], PROVENANCE_STRUCTURAL)
        self.assertNotEqual(
            judged_floor["provenance_statement"],
            derived_floor["provenance_statement"],
        )
        self.assertEqual(self.block(None)["sample_floor_as_of"], SAMPLE_FLOOR_AS_OF)
        # ...and that date is NOT the boundaries' date, which crosses the API
        # beside it.
        self.assertNotEqual(
            self.block(None)["sample_floor_as_of"], self.block(None)["as_of"]
        )


class GreenMeansMeasuredAndHealthyTest(unittest.TestCase):
    """#93: a strip dot may not be green over a reading nobody could take.

    The corpus is a first run grown to twelve replies -- deliberately chosen
    over three. At twelve, two of the five metrics HAVE cleared their floors
    and both read `ok`, so the old code's "worst of the severities that exist"
    produced a green dot with three severity-less knobs standing beside it.
    That is the harder half of the defect: at three calls every knob was
    absent and the strip at least had nothing to be green about.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-93-green-"))
        db_path = cls.tmp / "usage.db"
        ingest(
            build_first_run_corpus(cls.tmp, replies=12),
            db_path,
            tasks_dir=cls.tmp / "no-task-index",
        )
        cls.api = Api(db_path)
        cls.payload = cls.api.summary(*day_bounds(None, None))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def dot(self, key):
        return next(d for d in self.payload["status"]["dots"] if d["key"] == key)

    def test_the_fixture_is_the_awkward_case_it_claims_to_be(self):
        # Without this the class could pass over a corpus where something read
        # `watch`, and the green dot would never have been reachable.
        knobs = self.payload["recommendations"]["knobs"]
        severities = [k["severity"] for k in knobs if k["severity"] is not None]
        self.assertTrue(severities, "no knob is banded; this is not the case")
        self.assertEqual(set(severities), {SEVERITY_OK})
        self.assertTrue(
            [k for k in knobs if k["severity"] is None],
            "every knob is banded; there is nothing for the dot to be green over",
        )

    def test_the_knob_dot_is_unknown_rather_than_green(self):
        # An unknown may WEAKEN a clean answer and may never soften a bad one
        # -- `STRIP_ORDER`'s own rule, which the strip published and did not
        # apply, because every caller filtered the unknowns out before
        # comparing.
        self.assertEqual(self.dot(STRIP_DOT_KNOBS)["state"], STRIP_UNKNOWN)

    def test_the_cache_dot_is_unknown_rather_than_green(self):
        self.assertEqual(self.dot(STRIP_DOT_CACHE)["state"], STRIP_UNKNOWN)

    def test_the_count_still_ranges_over_every_knob_and_names_the_rest(self):
        knobs = self.payload["recommendations"]["knobs"]
        answer = self.dot(STRIP_DOT_KNOBS)["answer"]
        # The denominator is not quietly narrowed to the measured ones: a
        # smaller true statement told in place of the first is its own defect.
        self.assertTrue(answer.startswith(f"0 of {len(knobs)}"))
        self.assertIn(str(len([k for k in knobs if k["severity"] is None])), answer)

    def test_an_unknown_does_not_soften_a_bad_reading(self):
        # The other direction of `STRIP_ORDER`, checked directly so the change
        # cannot have turned every dot into an unknown.
        self.assertEqual(
            Api._worst_strip_state([SEVERITY_ACT, None, SEVERITY_OK]), STRIP_BAD
        )
        self.assertEqual(
            Api._worst_strip_state([SEVERITY_WATCH, None]), STRIP_WATCH
        )
        # ...and a run with nothing but verdicts is still green.
        self.assertEqual(
            Api._worst_strip_state([SEVERITY_OK, SEVERITY_OK]), STRIP_GOOD
        )
        # ...while one unknown among them is enough to withhold it.
        self.assertEqual(
            Api._worst_strip_state([SEVERITY_OK, None]), STRIP_UNKNOWN
        )
        self.assertEqual(Api._worst_strip_state([]), STRIP_UNKNOWN)


class ThreeLevelPayloadTest(unittest.TestCase):
    """#89: three levels, one payload, and no way for them to disagree.

    The structural tests over `index.html` can say which binding sits at which
    level. They cannot execute anything, so the property that actually matters
    -- that the gauge, the diagnosis and the strip are all reading ONE
    derivation -- is asserted here, against a real payload built through the
    real ingest path.

    The corpus is `build_recommendation_corpus`'s, chosen because it already
    strands the table in every state this level has to render: a day where all
    five metrics are measured and land in different ranges, a day where one is
    unmeasured, a day where three are, and a day that holds nothing at all.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-89-test-"))
        projects = build_recommendation_corpus(cls.tmp)
        db_path = cls.tmp / "usage.db"
        ingest(projects, db_path, tasks_dir=cls.tmp / "no-task-index")
        cls.api = Api(db_path)
        cls.raw = (Path(__file__).resolve().parent.parent / "index.html").read_text()
        cls.html = strip_comments(cls.raw)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def summary(self, day: str | None) -> dict:
        return self.api.summary(*day_bounds(day, day))

    def block(self, day: str | None) -> dict:
        return self.summary(day)["recommendations"]

    ALL_DAYS = (
        REC_FULL_DAY,
        REC_SOLO_DAY,
        REC_NO_CACHE_DAY,
        REC_UNWINDOWED_DAY,
        REC_EMPTY_DAY,
        None,
    )

    # --- the two levels are one derivation ---------------------------------

    def test_every_knob_repeats_its_ranked_reading_exactly(self) -> None:
        # THE test the whole three-level structure rests on. `knobs` is what
        # the summary draws and `ranked` is what the diagnosis card states; a
        # summary that rounded, re-queried or re-ranked would be a second
        # opinion on the numbers it summarises, and the reader would have no
        # way to tell which of the two levels was describing their data.
        #
        # Field by field, and IDENTITY on the float rather than a tolerance: a
        # difference too small to see is still two derivations.
        for day in self.ALL_DAYS:
            block = self.block(day)
            ranked = {a["metric"]: a for a in block["ranked"]}
            knobs = {k["metric"]: k for k in block["knobs"]}
            for metric, reading in ranked.items():
                with self.subTest(day=day, metric=metric):
                    knob = knobs[metric]
                    self.assertEqual(knob["value"], reading["value"])
                    self.assertEqual(knob["severity"], reading["severity"])
                    self.assertEqual(knob["measurement"], reading["measurement"])
                    # The UNIT too (#89 review): one figure printed one way at
                    # both levels. `30.3%` on the gauge and `0.3034` on the
                    # diagnosis is the same number in two voices, which a
                    # levelled page makes easy even though the value cannot
                    # move.
                    self.assertEqual(knob["unit"], reading["unit"])
                    lever = reading["lever"]
                    self.assertEqual(
                        knob["directive"], None if lever is None else lever["directive"]
                    )

    def test_the_knobs_are_in_the_tables_own_ranked_order(self) -> None:
        # The summary shows the biggest lever first, and it does NOT decide
        # which that is: the order is `recommendations.rank()`'s, published
        # under `ranking_provenance`, and this asserts the summary did not
        # re-sort it. A second ordering here would be an undated judgment about
        # which lever matters most.
        for day in self.ALL_DAYS:
            with self.subTest(day=day):
                block = self.block(day)
                # BY THE SAMPLE'S OWN STATE, not by "has a value" (#93). An
                # under-sampled knob HAS a value -- the reading is true and is
                # shown -- and has no severity, so it is not in `ranked` and
                # must not be expected there. Selecting on the value would put
                # it in this list and turn a correct refusal into a failure.
                measured = [
                    k["metric"]
                    for k in block["knobs"]
                    if k["sample"]["state"] == SAMPLE_MEASURED
                ]
                self.assertEqual(measured, [a["metric"] for a in block["ranked"]])

    def test_every_metric_is_a_knob_measured_or_not(self) -> None:
        # A knob that does not apply is DIMMED, NOT HIDDEN -- and a metric with
        # no sample is not hidden either. Seeing that five knobs exist and
        # three are already fine is the finding; a list that dropped the
        # healthy or the unmeasured ones would make "fine" and "never measured"
        # look identical, which is the defect the table's explicit healthy
        # entry exists to prevent.
        for day in self.ALL_DAYS:
            with self.subTest(day=day):
                block = self.block(day)
                self.assertEqual(
                    sorted(k["metric"] for k in block["knobs"]), sorted(METRICS)
                )
                unmeasured = {k["metric"] for k in block["knobs"] if k["value"] is None}
                self.assertEqual(unmeasured, set(block["unmeasured"]))

    def test_a_knob_with_no_sample_carries_three_nulls_and_never_a_zero(self) -> None:
        # The empty day: nothing measured, and nothing may render as a reading
        # of zero -- which for four of the five metrics is the WORST reading
        # there is.
        block = self.block(REC_EMPTY_DAY)
        self.assertEqual(block["ranked"], [])
        self.assertEqual(len(block["knobs"]), len(METRICS))
        for knob in block["knobs"]:
            with self.subTest(metric=knob["metric"]):
                self.assertIsNone(knob["value"])
                self.assertIsNone(knob["severity"])
                self.assertIsNone(knob["directive"])
                self.assertIsNone(knob["gauge"]["needle"])

    # --- the gauge is a drawing of the table -------------------------------

    def test_every_gauge_segment_is_a_range_of_the_table(self) -> None:
        # The acceptance criterion in its literal form: no arc exists that the
        # table did not put there, in the table's own order, with the table's
        # own severity.
        for knob in self.block(REC_FULL_DAY)["knobs"]:
            metric = METRICS[knob["metric"]]
            with self.subTest(metric=knob["metric"]):
                self.assertEqual(
                    [s["severity"] for s in knob["gauge"]["segments"]],
                    [r.recommendation.severity for r in metric.ranges],
                )
                self.assertEqual(knob["gauge"]["worse_when"], metric.worse_when)

    def test_every_gauge_boundary_is_a_boundary_of_the_table(self) -> None:
        # Value AND provenance, per boundary. The gauge inherits the table's
        # per-boundary provenance rather than restating it, so a judged cut
        # point cannot be drawn in a cited one's voice -- #31's
        # `band_provenance` failure mode, in the layer that draws.
        for knob in self.block(REC_FULL_DAY)["knobs"]:
            metric = METRICS[knob["metric"]]
            expected = [
                (r.lower.value, r.lower.provenance.kind, r.lower.provenance.statement)
                for r in metric.ranges[1:]
            ]
            with self.subTest(metric=knob["metric"]):
                self.assertEqual(
                    [
                        (b["value"], b["kind"], b["statement"])
                        for b in knob["gauge"]["boundaries"]
                    ],
                    expected,
                )

    def test_the_target_is_the_edge_of_the_tables_healthy_range(self) -> None:
        # "Aim under X" is not a new number: it is where the table stops
        # calling a reading healthy, on the side the harm is. Hand-written per
        # metric, so a derivation that agreed with a drifted table cannot pass.
        expected = {
            METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW: 0.10,
            METRIC_CACHE_WRITE_ONLY_SHARE: 0.02,
            METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY: 1.5,
            METRIC_CACHE_READS_PER_WRITE: 10.0,
            METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL: 10.0,
        }
        self.assertEqual(sorted(expected), sorted(METRICS))
        for knob in self.block(REC_FULL_DAY)["knobs"]:
            with self.subTest(metric=knob["metric"]):
                targets = [
                    b for b in knob["gauge"]["boundaries"] if b["is_target"]
                ]
                self.assertEqual(len(targets), 1, "a gauge names one target")
                self.assertEqual(targets[0]["value"], expected[knob["metric"]])

    def test_a_target_is_never_taken_from_the_wrong_end(self) -> None:
        # Teeth on the derivation: two of the five metrics are worse when
        # LOWER, so an implementation that always took the healthy run's upper
        # edge would name a target on the wrong side for exactly those two, and
        # the page would point its arrow away from the advice.
        for knob in self.block(REC_FULL_DAY)["knobs"]:
            metric = METRICS[knob["metric"]]
            target = next(b for b in knob["gauge"]["boundaries"] if b["is_target"])
            healthy = [
                r for r in metric.ranges if r.recommendation.severity == SEVERITY_OK
            ]
            with self.subTest(metric=knob["metric"]):
                self.assertEqual(len(healthy), 1, "this fixture assumes one ok range")
                if metric.worse_when == WORSE_WHEN_HIGHER:
                    self.assertEqual(target["value"], healthy[0].upper.value)
                else:
                    self.assertEqual(target["value"], healthy[0].lower.value)

    def test_the_needle_lands_under_the_arc_the_table_would_have_chosen(self) -> None:
        # The drawing and the advice cannot disagree, because the needle's
        # position is derived through `range_for()` -- the same lookup the
        # severity came from. A needle under a green arc beside a DO NOW tag is
        # exactly the contradiction a gauge makes easy.
        for day in self.ALL_DAYS:
            for knob in self.block(day)["knobs"]:
                needle = knob["gauge"]["needle"]
                if needle is None:
                    continue
                with self.subTest(day=day, metric=knob["metric"]):
                    self.assertGreaterEqual(needle, 0.0)
                    self.assertLessEqual(needle, 1.0)
                    under = [
                        s
                        for s in knob["gauge"]["segments"]
                        if s["start"] <= needle <= s["end"]
                    ]
                    self.assertIn(knob["severity"], [s["severity"] for s in under])

    def test_the_needle_moves_with_the_reading_and_not_with_the_range(self) -> None:
        # Teeth: a position of `index / n` -- the range's own start -- would
        # satisfy every containment check above while showing every reading in
        # a range at the same place. Two values inside ONE range must draw
        # differently, and a value deeper into harm must draw further along.
        metric = METRICS[METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW]
        low = Api._gauge(metric, 0.30)["needle"]
        high = Api._gauge(metric, 0.60)["needle"]
        self.assertLess(low, high)
        self.assertNotEqual(low, 2 / 3)

    def test_an_unbounded_range_draws_without_inventing_a_ceiling(self) -> None:
        # The open-ended top range has no width to normalise against, and the
        # answer is `depth_in_band`'s reciprocal rather than a maximum somebody
        # picked: a value ten times past the boundary is further along than one
        # just past it, and neither ever reaches the end of the dial.
        metric = METRICS[METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY]
        just_past = Api._gauge(metric, 3.01)["needle"]
        far_past = Api._gauge(metric, 300.0)["needle"]
        self.assertLess(just_past, far_past)
        self.assertLess(far_past, 1.0)

    # --- the strip reads, and never re-derives -----------------------------

    def test_every_dot_answers_its_own_question_in_the_apis_words(self) -> None:
        for day in self.ALL_DAYS:
            with self.subTest(day=day):
                dots = self.summary(day)["status"]["dots"]
                self.assertEqual([d["key"] for d in dots], list(STRIP_DOTS))
                for dot in dots:
                    self.assertEqual(dot["question"], STRIP_QUESTIONS[dot["key"]])
                    self.assertIn(dot["state"], STRIP_ORDER)
                    self.assertTrue(dot["answer"].strip())

    def test_the_broken_dot_is_its_cards_own_verdict(self) -> None:
        # A dot that disagreed with the card it summarises is the three-level
        # page's own defect in its most literal form. "Anything broken?" is a
        # complete statement over whatever was ingested -- nothing unparsed,
        # nothing skipped -- rather than a rate estimated from a sample, so it
        # answers to no floor and is its card's verdict unconditionally.
        for day in self.ALL_DAYS:
            with self.subTest(day=day):
                payload = self.summary(day)
                dots = {d["key"]: d for d in payload["status"]["dots"]}
                self.assertEqual(
                    dots[STRIP_DOT_BROKEN]["state"],
                    STRIP_FROM_HEALTH[payload["health"]["verdict"]][0],
                )

    def test_the_context_dot_is_its_cards_verdict_held_to_its_metrics_floor(self):
        # #93, second pass. The dot asks "Wasting context?", which is a
        # JUDGMENT; the card underneath answers "did any call reach half its
        # window", which is an observation complete over any sample. The two
        # are not the same claim, and on a five-call corpus the page rendered a
        # green "No" directly above a row reading `TOO FEW -
        # main_thread_share_over_half_window - 5 of 11`. A reader cannot hold
        # both.
        #
        # So the dot is the card's verdict EXCEPT where the metric that answers
        # its question has no basis, and the exception runs one way only.
        for day in self.ALL_DAYS:
            with self.subTest(day=day):
                payload = self.summary(day)
                dot = next(
                    d
                    for d in payload["status"]["dots"]
                    if d["key"] == STRIP_DOT_CONTEXT
                )
                verdict = payload["context"]["utilisation"]["answer"]["verdict"]
                card = STRIP_FROM_CONTEXT[verdict][0]
                backing = next(
                    k
                    for k in payload["recommendations"]["knobs"]
                    if k["metric"] == CONTEXT_DOT_METRIC
                )
                if backing["sample"]["state"] != SAMPLE_UNDER_SAMPLED:
                    self.assertEqual(dot["state"], card)
                    continue
                # Under-sampled: the floor contributes an unknown and
                # `STRIP_ORDER` decides, so the dot is never BETTER than the
                # card and never better than unknown.
                self.assertEqual(
                    dot["state"], min((card, STRIP_UNKNOWN), key=STRIP_ORDER.index)
                )
                self.assertNotEqual(dot["state"], STRIP_GOOD)

    def test_the_corpus_reaches_a_context_dot_held_to_the_floor(self) -> None:
        # Without this the test above could pass vacuously over a corpus where
        # the backing metric is always banded.
        floored = [
            day
            for day in self.ALL_DAYS
            if next(
                k
                for k in self.summary(day)["recommendations"]["knobs"]
                if k["metric"] == CONTEXT_DOT_METRIC
            )["sample"]["state"]
            == SAMPLE_UNDER_SAMPLED
        ]
        self.assertTrue(
            floored, "no window in this corpus under-samples the context metric"
        )

    def test_the_context_dot_names_the_scope_only_on_a_proven_yes(self) -> None:
        # A named scope is the ranking's own winner and only exists where the
        # answer is proven. On any other state there is no winner to name, and
        # a dot that named one anyway would assert a finding the card below it
        # does not make.
        for day in self.ALL_DAYS:
            payload = self.summary(day)
            dot = next(
                d
                for d in payload["status"]["dots"]
                if d["key"] == STRIP_DOT_CONTEXT
            )
            utilisation = payload["context"]["utilisation"]
            with self.subTest(day=day, verdict=utilisation["answer"]["verdict"]):
                if utilisation["answer"]["verdict"] == CONTEXT_ANSWER_YES:
                    self.assertIn(str(utilisation["worst_scope"]), dot["answer"])
                else:
                    for scope in SCOPE_LABELS.values():
                        self.assertNotIn(scope, dot["answer"])

    def test_the_knob_dot_counts_the_levers_against_every_knob(self) -> None:
        # "2 of 5", never "2": the second half is what stops a page of two rows
        # reading as a page of two problems, and it is what makes a dimmed knob
        # information rather than clutter.
        #
        # #93 added the other half of the sentence. Where some knob has no
        # basis the count still ranges over ALL of them -- narrowing the
        # denominator to the measured ones would be a second, smaller truth
        # told in place of the first -- and the shortfall is named beside it.
        # Where NONE has a basis there is no count worth printing at all: "0 of
        # 5" is arithmetic over an empty set that reads exactly like five
        # checks passing, which is the sentence a fresh install saw.
        for day in self.ALL_DAYS:
            with self.subTest(day=day):
                payload = self.summary(day)
                knobs = payload["recommendations"]["knobs"]
                dot = next(
                    d for d in payload["status"]["dots"] if d["key"] == STRIP_DOT_KNOBS
                )
                turnable = len([k for k in knobs if k["directive"]])
                without_basis = [k for k in knobs if k["severity"] is None]
                if len(without_basis) == len(knobs):
                    # WHICH absence, not merely that there is one. An empty
                    # window is waiting for nothing and must not be told to
                    # come back later; a fresh install is, and must.
                    under = [
                        k
                        for k in knobs
                        if k["sample"]["state"] == SAMPLE_UNDER_SAMPLED
                    ]
                    self.assertEqual(
                        dot["answer"],
                        STRIP_UNDER_SAMPLED if under else STRIP_NOT_MEASURED,
                    )
                    continue
                self.assertTrue(
                    dot["answer"].startswith(f"{turnable} of {len(knobs)}"),
                    f"{dot['answer']!r} does not count against every knob",
                )
                if without_basis:
                    self.assertIn(str(len(without_basis)), dot["answer"])
                else:
                    self.assertEqual(dot["answer"], f"{turnable} of {len(knobs)}")

    def test_the_cache_dot_is_unknown_rather_than_healthy_with_no_sample(self) -> None:
        # The one substitution this repository refuses, in a new place: a dot
        # that went green because nothing was measured. `REC_NO_CACHE_DAY`
        # writes no cache at all, so every cache metric loses its denominator.
        payload = self.summary(REC_NO_CACHE_DAY)
        dot = next(d for d in payload["status"]["dots"] if d["key"] == STRIP_DOT_CACHE)
        self.assertEqual(dot["state"], STRIP_UNKNOWN)
        self.assertEqual(dot["answer"], STRIP_NOT_MEASURED)
        self.assertNotEqual(dot["state"], STRIP_GOOD)
        # And the day where they ARE measured is not unknown, so the assertion
        # above is not passing on a dot that is always grey.
        full = next(
            d
            for d in self.summary(REC_FULL_DAY)["status"]["dots"]
            if d["key"] == STRIP_DOT_CACHE
        )
        self.assertNotEqual(full["state"], STRIP_UNKNOWN)

    def test_the_cache_dot_takes_the_worst_of_its_own_metrics(self) -> None:
        payload = self.summary(REC_FULL_DAY)
        severities = [
            k["severity"]
            for k in payload["recommendations"]["knobs"]
            if k["metric"] in CACHE_METRICS and k["severity"] is not None
        ]
        worst = max(severities, key=lambda s: SEVERITY_RANK[s])
        dot = next(d for d in payload["status"]["dots"] if d["key"] == STRIP_DOT_CACHE)
        self.assertEqual(dot["state"], STRIP_FROM_SEVERITY[worst][0])

    # --- the model mix is an observation ------------------------------------

    def test_the_model_mix_names_the_busiest_model_and_its_sample(self) -> None:
        payload = self.summary(REC_FULL_DAY)
        mix = payload["model_mix"]
        self.assertEqual(mix["sample_calls"], payload["calls"])
        self.assertEqual(mix["sample_is"], MODEL_MIX_SAMPLE)
        self.assertEqual(mix["busiest"]["model"], REC_OPUS_1M)
        self.assertEqual(mix["busiest"]["calls"], REC_FULL_MAIN_CALLS)
        self.assertEqual(mix["models"], 2)

    def test_a_window_with_no_call_has_no_busiest_model(self) -> None:
        # Never a row of zeroes, and never the name of a model this window
        # never ran: an absence, said as one.
        mix = self.summary(REC_EMPTY_DAY)["model_mix"]
        self.assertIsNone(mix["busiest"])
        self.assertEqual(mix["sample_calls"], 0)
        self.assertEqual(mix["models"], 0)

    def test_the_model_mix_carries_no_severity_lever_or_target(self) -> None:
        # THE owner decision, as a structural guarantee rather than a note.
        # Model tier is a measurement and NOT advice -- CPB counts tokens and
        # cannot see whether a cheaper model would have needed more attempts --
        # so nothing in this block may look like a knob. A later change that
        # gave it one has to add a field here and turn this red first.
        mix = self.summary(REC_FULL_DAY)["model_mix"]
        for field in ("severity", "lever", "directive", "target", "worse_when",
                      "gauge", "recommendation"):
            with self.subTest(field=field):
                self.assertNotIn(field, mix)
        self.assertNotIn("model_mix", str(sorted(METRICS)))
        self.assertNotIn(
            "model", " ".join(m.key for m in METRICS.values()),
            "the model mix must not be a member of the advice table",
        )

    # --- the shared strings really are strings ------------------------------

    def test_every_shared_reading_is_a_server_composed_string(self) -> None:
        # The rule `SHARED_READINGS` rests on: what two levels may share is a
        # SENTENCE or a DATE, composed once in `serve.py`, because there is
        # nothing about it that two renderings could compute differently. A
        # FIGURE shared this way could be rounded at one level and not at the
        # other, which is the drift the register exists to refuse.
        payload = self.summary(REC_FULL_DAY)
        for path in sorted(SHARED_READINGS):
            with self.subTest(path=path):
                node = payload
                for hop in path.split("."):
                    self.assertIn(hop, node, f"{path} no longer resolves")
                    node = node[hop]
                self.assertIsInstance(
                    node,
                    str,
                    f"{path} is shared between levels and is not a string",
                )

    def test_every_reading_declares_what_kind_of_number_it_is(self) -> None:
        # #89 review: the page printed `0.3034` where a reader holds `30.3%`,
        # because nothing on the payload said which readings are shares and
        # which are multiples. Every reading now carries it, measured or not --
        # what kind of number a metric produces does not depend on whether this
        # window measured one, and the boundaries drawn on an empty dial are in
        # that unit too.
        for day in self.ALL_DAYS:
            block = self.block(day)
            for knob in block["knobs"]:
                with self.subTest(day=day, metric=knob["metric"]):
                    self.assertEqual(knob["unit"], METRICS[knob["metric"]].unit)
                    self.assertIn(knob["unit"], METRIC_UNIT_KINDS)

    def test_every_reading_says_what_it_means_to_the_reader(self) -> None:
        # #89: the summary row's why-line. It is the TABLE's sentence on every
        # knob, measured or not -- what a metric means does not depend on
        # whether this window sampled it, and a row that lost its sentence with
        # its sample would say less about the absence than about the reading.
        for day in self.ALL_DAYS:
            block = self.block(day)
            self.assertEqual(len(block["knobs"]), len(METRICS))
            for knob in block["knobs"]:
                with self.subTest(day=day, metric=knob["metric"]):
                    self.assertEqual(knob["means"], METRICS[knob["metric"]].means)
                    # And BESIDE the measurement, never instead of it: the
                    # specification still crosses on the same row, for the
                    # disclosure that states it in full.
                    self.assertEqual(
                        knob["measurement"], METRICS[knob["metric"]].measurement
                    )
                    self.assertNotEqual(knob["means"], knob["measurement"])

    def test_the_unit_is_the_metrics_own_and_serve_holds_no_table_of_them(
        self,
    ) -> None:
        # The migration, pinned in both directions (#89). The unit is a
        # property of the metric and now lives on it; a second mapping in
        # `serve.py` would be one enumeration of the metric set too many --
        # `RECOMMENDED_METRICS`' own defect -- and it is what this branch
        # removed rather than left beside the table.
        #
        # The property the deleted import guards were buying is what matters,
        # and it is now unconditional: a metric with no unit cannot be
        # CONSTRUCTED, so the failure no longer waits for something to import
        # the serving layer.
        for name in ("METRIC_UNITS", "METRIC_UNIT_KINDS", "_metric_unit"):
            with self.subTest(attribute=name):
                self.assertFalse(
                    hasattr(serve, name),
                    f"serve.{name} is back: the unit belongs to the metric",
                )
        for key, metric in METRICS.items():
            with self.subTest(metric=key):
                self.assertIn(metric.unit, METRIC_UNIT_KINDS)

    def test_the_new_blocks_carry_no_money_shaped_field(self) -> None:
        # #30 reaches new payloads too, and a summary level is exactly where a
        # money-shaped figure would be reintroduced: "what do I do?" invites
        # one. Rank by tokens and show the model; the reader weighs the tiers.
        payload = self.summary(REC_FULL_DAY)
        blob = json.dumps(
            {key: payload[key] for key in ("status", "model_mix", "recommendations")}
        ).lower()
        for token in ("cost", "usd", "dollar", "$", "price", "spend"):
            with self.subTest(token=token):
                self.assertNotIn(token, blob)

    # --- no threshold in the page ------------------------------------------

    def test_no_boundary_of_the_table_appears_in_the_page(self) -> None:
        # THE acceptance criterion, and the mutation this class was written
        # for: a gauge threshold authored in `index.html`. Every boundary in
        # the table is searched for as a decimal LITERAL with word boundaries,
        # so `10.0` does not match a `10` in a viewBox and `1.5` does not match
        # a `1.5rem` -- and any of them appearing turns this red.
        values = {
            entry.lower.value
            for metric in METRICS.values()
            for entry in metric.ranges
        } | {
            entry.upper.value
            for metric in METRICS.values()
            for entry in metric.ranges
            if entry.upper is not None
        }
        for value in sorted(values):
            literal = repr(float(value))
            with self.subTest(value=literal):
                self.assertNotRegex(
                    self.html,
                    r"(?<![\w.])" + re.escape(literal) + r"(?![\d\w])",
                    f"{literal} is a boundary of the table and is written into "
                    "index.html. Every number a gauge draws comes from "
                    "recommendations.py through the payload.",
                )


class ThreeStatesGetThreeRenderingsTest(unittest.TestCase):
    """#93 (page side): a real 0, an unmeasured metric and an under-sampled one.

    THE LIMIT, STATED PLAINLY, as everywhere else on this page: nothing here
    executes Alpine or measures a pixel. These assert which binding sits where
    and that no floor is spelled in this file. Whether the compact rows READ as
    "come back later" rather than as "broken", and what the page actually
    measures on a screen, is the human-eye half and is not claimed.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (Path(__file__).resolve().parent.parent / "index.html").read_text()
        cls.html = strip_comments(cls.raw)
        cls.knobs = html_element(cls.raw, 'id="knobs-note"')
        cls.rows = cls.knobs[: cls.knobs.index('id="knobs-provenance"')]
        cls.full_rows = cls.rows[: cls.rows.index('x-if="unbandedKnobs.length"')]
        cls.thin = cls.rows[cls.rows.index('x-if="unbandedKnobs.length"'):]
        cls.disclosure = html_element(cls.raw, 'id="knobs-provenance"')

    def test_no_sample_floor_is_written_into_the_page(self):
        # `test_no_boundary_of_the_table_appears_in_the_page`'s rule, for the
        # other kind of number the table now owns. A floor typed here would be
        # a threshold with no date, no provenance and nothing to redline --
        # and, worse than a boundary, one that could not move when the band it
        # is derived from moved.
        floors = {metric.sample_floor.minimum for metric in METRICS.values()}
        self.assertTrue(floors)
        for floor in sorted(floors):
            with self.subTest(floor=floor):
                self.assertNotRegex(
                    self.rows,
                    r"(?<![\w.-])" + str(floor) + r"(?![\d\w])",
                    f"{floor} is a sample floor and is written into index.html",
                )

    def test_the_page_names_one_sample_state_and_derives_the_rest(self):
        # A page-side copy of `SAMPLE_STATES` would be a second enumeration of
        # a vocabulary in the one file no import guard reaches. It needs the
        # name of the state that CAN be banded and nothing else.
        self.assertIn('const SAMPLE_MEASURED = "measured";', self.html)
        for never in (SAMPLE_UNDER_SAMPLED, SAMPLE_UNMEASURED):
            with self.subTest(state=never):
                self.assertNotIn(f'"{never}"', self.html)

    def test_an_unrecognised_state_falls_to_the_form_that_withholds_a_verdict(self):
        # `!== SAMPLE_MEASURED` rather than `=== 'unmeasured'`: a fourth state
        # added upstream renders COMPACT, with no dial and no severity, instead
        # of falling through to the full row and being drawn as a verdict. Same
        # direction as every other fallback on this page.
        self.assertIn(
            "!== SAMPLE_MEASURED", js_function_body(self.raw, "get unbandedKnobs(")
        )

    def test_the_full_row_is_reached_only_by_a_banded_knob(self):
        # The needle, the target and the severity tag all live in the full row,
        # so a row with no basis reaching it would be drawn as a verdict
        # whatever its fields said.
        self.assertIn('x-for="k in bandedKnobs"', self.full_rows)
        self.assertIn('class="needle"', self.full_rows)
        self.assertNotIn('x-for="k in unbandedKnobs"', self.full_rows)

    def test_the_compact_block_carries_no_dial(self):
        # The 60x40 instrument is what the row with no reading was paying for.
        for drawn in ("<svg", "class=\"needle\"", "arcPathsFor", "gauge"):
            with self.subTest(part=drawn):
                self.assertNotIn(drawn, self.thin)

    def test_the_compact_block_tells_the_two_absences_apart(self):
        # Two tags and two footers, on two conditions. One tag over both, or
        # one footer, would be the collapse this whole change undoes.
        self.assertIn("'TOO FEW'", self.thin)
        self.assertIn("'NO READING'", self.thin)
        self.assertIn("tag-thin", self.thin)
        self.assertIn("tag-none", self.thin)
        self.assertIn("under_sampled_note", self.thin)
        self.assertIn("unmeasured_note", self.thin)
        self.assertIn('x-if="underSampledKnobs.length"', self.thin)
        self.assertIn('x-if="unmeasuredKnobs.length"', self.thin)

    def test_the_two_tags_are_styled_apart_and_neither_is_the_healthy_one(self):
        # Three states may not share a look, and neither absence may wear the
        # colour of a clean reading.
        self.assertRegex(self.html, r"\.tag-thin\s*\{[^}]*background")
        self.assertRegex(self.html, r"\.tag-none\s*\{[^}]*background")
        thin = re.search(r"\.tag-thin\s*\{([^}]*)\}", self.html).group(1)
        none = re.search(r"\.tag-none\s*\{([^}]*)\}", self.html).group(1)
        fine = re.search(r"\.tag-fine\s*\{([^}]*)\}", self.html).group(1)
        self.assertNotEqual(thin.strip(), none.strip())
        self.assertNotEqual(thin.strip(), fine.strip())
        self.assertNotEqual(none.strip(), fine.strip())

    def test_every_compact_row_states_its_own_shortfall(self):
        # The count it has and the count it needs, with the noun behind them:
        # "3 of 51" says nothing until something says 51 of WHAT.
        self.assertIn('x-text="k.sample.size"', self.thin)
        self.assertIn('x-text="k.sample.minimum"', self.thin)
        self.assertIn("k.sample.counts", self.thin)

    def test_a_banded_row_shows_the_sample_that_earned_its_verdict(self):
        # "over 51 of 51 needed" is the evidence a green row earned its green.
        self.assertIn('x-text="k.sample.size"', self.full_rows)
        self.assertIn('x-text="k.sample.minimum"', self.full_rows)

    def test_the_floor_states_its_own_voice_in_the_disclosure(self):
        # A judged floor and a derived one wear the same voice classes the cut
        # points do, through the page's existing `provenanceVoice` -- which
        # means the page classifies neither and reads the kind the API sent.
        self.assertIn("k.sample.provenance_kind", self.disclosure)
        self.assertIn('x-text="k.sample.provenance_statement"', self.disclosure)
        self.assertIn('x-text="k.sample.rule"', self.disclosure)
        self.assertIn(
            'provenanceVoice({ kind: k.sample.provenance_kind })', self.disclosure
        )
        # And the floors' own date, which is NOT the boundaries'.
        self.assertIn("recommendations.sample_floor_as_of", self.disclosure)

    def test_the_disclosure_still_states_every_metric_whatever_its_state(self):
        # The boundaries and the floor of a metric with no reading are as true
        # as any other's, and a reader auditing an absence needs them most.
        self.assertIn(
            'x-for="k in (summary ? summary.recommendations.knobs : [])"',
            self.disclosure,
        )


class AFirstRunIsShorterThanABusyOneTest(unittest.TestCase):
    """#93: a first run must not be TALLER than a corpus with something to say.

    Measured as ROWS OF EACH KIND, not in pixels. Nothing in this suite renders
    a page, so a pixel figure here would be a number this project cannot check
    -- the exact thing it refuses elsewhere. What is checkable is the count of
    full-height instrument rows a payload asks the page to draw, which is what
    the height is made of: the issue measured 976px against 924px, and the
    difference was five 61px rows of dial with no needle in them.

    THE PIXEL CLAIM IS NOT MADE HERE and needs a browser to confirm.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-93-height-"))
        first = cls.tmp / "first"
        ingest(
            build_first_run_corpus(first),
            first / "usage.db",
            tasks_dir=cls.tmp / "no-task-index",
        )
        cls.first_api = Api(first / "usage.db")
        busy = cls.tmp / "busy"
        busy.mkdir()
        ingest(
            build_recommendation_corpus(busy),
            busy / "usage.db",
            tasks_dir=cls.tmp / "no-task-index",
        )
        cls.busy_api = Api(busy / "usage.db")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.first_api.conn.close()
        cls.busy_api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def rows(api):
        knobs = api.summary(*day_bounds(None, None))["recommendations"]["knobs"]
        full = [k for k in knobs if k["sample"]["state"] == SAMPLE_MEASURED]
        return len(full), len(knobs) - len(full)

    def test_a_first_run_asks_the_page_to_draw_no_full_rows(self):
        full, compact = self.rows(self.first_api)
        self.assertEqual(full, 0)
        self.assertEqual(compact, len(METRICS))

    def test_a_busy_corpus_still_gets_every_full_row(self):
        # The other half, so the change cannot be "draw fewer rows everywhere".
        full, compact = self.rows(self.busy_api)
        self.assertEqual(full, len(METRICS))
        self.assertEqual(compact, 0)

    def test_the_first_run_draws_strictly_fewer_instruments(self):
        self.assertLess(self.rows(self.first_api)[0], self.rows(self.busy_api)[0])

    def test_no_knob_is_lost_to_the_shortening(self):
        # Shorter by rendering each row smaller, never by dropping one: every
        # metric still reaches the screen at both extremes. "Dimmed, not
        # hidden" was never a claim about the height of the row.
        for name, api in (("first-run", self.first_api), ("busy", self.busy_api)):
            with self.subTest(corpus=name):
                self.assertEqual(sum(self.rows(api)), len(METRICS))


class SummaryLevelRenderTest(unittest.TestCase):
    """#89: the summary draws the table and decides nothing (page side).

    `ThreeLevelPayloadTest` proves the payload is one derivation. These are the
    properties of the DRAWING: that no knob is filtered off the screen, that a
    dial with no reading has no needle, and that every vocabulary the API sends
    has a complete treatment here rather than a default that would render a
    state nobody has seen as a reassuring one.

    THE LIMIT, STATED PLAINLY. Nothing below can see whether the summary fits
    on a screen, whether a needle is legible at 60 by 40 pixels, whether the
    dimmed rows read as "already fine" rather than as "broken", or whether the
    observation panel is far enough from the knobs to stop a reader taking it
    for advice. Only a person looking at the page can judge those, and three
    defects on this page have shipped green for exactly that reason.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (Path(__file__).resolve().parent.parent / "index.html").read_text()
        cls.html = strip_comments(cls.raw)
        cls.knobs = html_element(cls.raw, 'id="knobs-note"')
        # The knob ROWS alone -- everything above the provenance disclosure.
        # Several assertions below are about what a reader sees at rest, and
        # the disclosure states many of the same fields one click down; a
        # containment check over the whole panel would be satisfied by
        # evidence from a place the reader is not looking.
        cls.rows = cls.knobs[: cls.knobs.index('id="knobs-provenance"')]
        # #93: the FULL rows alone -- the banded template, stopping where the
        # compact block for the knobs with no basis begins. Several assertions
        # below are about what a row of ADVICE may carry, and a compact line
        # that carries no advice is held to different rules: it has no
        # directive to identify itself by, so the metric key is the only handle
        # it has.
        cls.full_rows = cls.rows[: cls.rows.index('x-if="unbandedKnobs.length"')]
        cls.strip = html_element(cls.raw, 'id="status-strip"')
        cls.observations = html_element(cls.raw, 'id="observations-note"')

    def table(self, decl: str) -> dict:
        return dict(
            re.findall(r"(\w+):\s*\"([^\"]+)\"", js_function_body(self.raw, decl))
        )

    # --- nothing is filtered off the screen --------------------------------

    def test_the_knob_list_is_iterated_whole(self) -> None:
        # A KNOB THAT DOES NOT APPLY IS DIMMED, NOT HIDDEN. Dropping one is the
        # mutation: the page would then show only what is wrong, "already fine"
        # and "never measured" would both be absence, and a reader could not
        # tell a short list from a healthy project.
        #
        # #93 SPLIT THE ROWS AND DID NOT NARROW THEM. The panel renders two
        # loops where it rendered one -- the banded knobs as full rows, the
        # rest as one line each -- so "iterates the API's whole list" is now a
        # claim about the two together. It is checked as a PARTITION, which is
        # stronger than the old containment check: the two getters must select
        # on the same field, with complementary comparisons, so no knob can
        # fall into neither and none can appear in both.
        loops = re.findall(r'x-for="[^"]*\bin\s*([^"]+)"', self.knobs)
        iterated = {expr.strip() for expr in loops}
        self.assertIn("bandedKnobs", iterated)
        self.assertIn("unbandedKnobs", iterated)
        # The disclosure still iterates the whole list directly, so every
        # metric's boundaries and floor are stated whatever its state.
        self.assertIn("(summary ? summary.recommendations.knobs : [])", iterated)
        banded = js_function_body(self.raw, "get bandedKnobs(")
        unbanded = js_function_body(self.raw, "get unbandedKnobs(")
        for name, body in (("bandedKnobs", banded), ("unbandedKnobs", unbanded)):
            with self.subTest(getter=name):
                self.assertIn("summary.recommendations.knobs", body)
                # On the STATE the API decided, never on a value or a key: a
                # split on `k.value !== null` would put an under-sampled knob
                # -- which has a value -- into the full rows and hand it a dial.
                self.assertIn("k.sample.state", body)
                self.assertNotIn("k.value", body)
                self.assertNotIn(".sort(", body)
        self.assertIn("=== SAMPLE_MEASURED", banded)
        self.assertIn("!== SAMPLE_MEASURED", unbanded)

    def test_a_knob_with_nothing_to_turn_is_dimmed_and_not_dropped(self) -> None:
        # Dimming is a CLASS on a row that is still rendered. The mutation this
        # catches is the cheap one: `x-if` instead of `:class`, which passes
        # every wiring check because the binding is still in the file.
        self.assertIn('class="knob" :class="{ off: !k.directive }"', self.knobs)
        self.assertRegex(self.html, r"\.knob\.off\s*\{[^}]*opacity")
        # And it says WHY it has nothing to turn, rather than leaving a gap
        # where the other rows carry a verb.
        self.assertIn('x-if="!k.directive && k.severity"', self.knobs)
        self.assertIn('x-if="!k.severity"', self.knobs)

    def test_an_unmeasured_knob_draws_no_needle_and_says_so(self) -> None:
        # THE central rule in a new visual form. `needlePath` answers absence
        # BEFORE it computes -- a needle at zero would be a reading of zero,
        # which for four of the five metrics is the worst there is -- and the
        # row renders the API's own note where the figure would be.
        body = js_function_body(self.raw, "function needlePath(")
        self.assertRegex(
            body,
            r"if \(t === null \|\| t === undefined\) return \"\";",
            "needlePath computes before it answers absence",
        )
        self.assertLess(body.index("return \"\""), body.index("gaugePoint"))
        # And the row with no reading says so in the API's own words. #93 moved
        # where that happens: a knob with no reading no longer reaches the full
        # template at all -- it has no needle BECAUSE it has no row with a dial
        # on it -- so the note is rendered on the compact block those rows now
        # form. The old `x-if="k.value === null"` inside the full row is gone
        # rather than merely unused, because a branch that cannot fire is a
        # claim about the payload that has stopped being true.
        self.assertNotIn('x-if="k.value === null"', self.knobs)
        self.assertIn("summary.recommendations.unmeasured_note", self.knobs)
        self.assertIn("summary.recommendations.under_sampled_note", self.knobs)

    # --- the page holds no threshold, no order and no verdict ---------------

    def test_the_summary_ranks_nothing_of_its_own(self) -> None:
        # The order is `recommendations.rank()`'s, published with its own
        # provenance. A sort here would be a second, undated judgment about
        # which lever matters most.
        for banned in (".sort(", ".reverse("):
            with self.subTest(call=banned):
                self.assertNotIn(banned, self.knobs)

    def test_the_gauge_reads_its_positions_and_computes_none(self) -> None:
        # Every `d` on the dial is composed from a position the API sent. A
        # boundary VALUE turned into a position here would be the page holding
        # a threshold -- and `test_no_boundary_of_the_table_appears_in_the_page`
        # is the other half of that.
        for binding in re.findall(r':d="([^"]+)"', self.knobs):
            with self.subTest(binding=binding):
                self.assertRegex(
                    binding,
                    r"^(arcPath|arcPathsFor|tickPathsFor|needlePath)\(",
                    "the dial composes a path some other way",
                )
        self.assertIn("k.gauge.needle", self.knobs)
        self.assertIn("k.gauge.segments", self.knobs)
        self.assertIn("k.gauge.boundaries", self.knobs)
        self.assertIn("k.gauge.worse_when", self.knobs)

    # --- a reading prints as the kind of number it is ----------------------

    def test_the_page_dispatches_on_the_unit_and_never_on_a_metric_key(self) -> None:
        # A share reads as a percentage and a ratio as a multiple, and WHICH it
        # is comes from the payload. A metric key spelled in this file would be
        # a third enumeration of the metric set, in the one place no import
        # guard can reach -- `RECOMMENDED_METRICS`' defect, one layer out.
        for key in METRICS:
            with self.subTest(metric=key):
                self.assertNotIn(key, self.html)
        self.assertIn("fmtUnit(k.value, k.unit)", self.knobs)
        self.assertIn(
            "return (UNIT_FORMAT[unit] ?? fmtMetric)(value);",
            js_function_body(self.raw, "function fmtUnit("),
        )

    def test_the_unit_table_covers_every_unit_the_api_can_send(self) -> None:
        # A unit with no formatter falls back to the unitless one and prints a
        # share as `0.3034` -- the defect this whole table was added to fix,
        # arriving later through an unhandled member.
        units = re.findall(
            r"(\w+):\s*(\w+),", re.search(
                r"const UNIT_FORMAT = \{([^}]*)\}", self.html
            ).group(1)
        )
        self.assertEqual(sorted(k for k, _ in units), sorted(METRIC_UNIT_KINDS))
        # And every formatter behind it answers absence before it computes, so
        # a null reading is the em-dash in EVERY unit.
        for _unit, formatter in units:
            with self.subTest(formatter=formatter):
                body = js_function_body(self.raw, f"function {formatter}(")
                self.assertRegex(body, r"=== null|== null|\?\?")

    def test_the_target_is_printed_in_the_metrics_own_unit(self) -> None:
        # "aim under 0.1" is the same defect as the headline figure, one line
        # down: the reader is being asked to move a number they hold as 10%.
        #
        # Scoped to the ROWS, not the whole panel. The disclosure prints the
        # same boundaries and would satisfy a containment check over the panel
        # on its own -- which is a test passing on evidence from somewhere the
        # reader is not looking (`scope-lead`'s lesson, one panel over).
        self.assertIn("fmtUnit(b.value, k.unit)", self.rows)
        self.assertNotIn("fmtMetric(", self.rows)

    # --- the row is advice, not a dump of the table ------------------------

    def test_the_metric_key_is_not_a_caption_on_the_summary_row(self) -> None:
        # #89 review: `main_thread_share_over_half_window` in monospace is a
        # debugging aid on a row of advice. It is genuinely useful for
        # traceability, so it MOVED rather than went -- to the panel's
        # provenance disclosure, and to the details level, where a reader has
        # already asked why.
        # THE FULL ROWS, which are the rows of advice this is about. #93's
        # compact lines carry no directive and no dial, so the key is the only
        # handle they have -- exactly as it is for the unmeasured rows at the
        # details level, which have always shown it.
        self.assertNotIn('x-text="k.metric"', self.full_rows)
        disclosure = html_element(self.raw, 'id="knobs-provenance"')
        self.assertIn('x-text="k.metric"', disclosure)
        self.assertIn('x-text="a.metric"', html_element(self.raw, 'id="advice-note"'))

    def test_the_row_says_what_the_number_means_and_not_what_defines_it(
        self,
    ) -> None:
        # #89 review, and the last of the page's density: the row's why-line
        # printed `measurement`, which is a SPECIFICATION -- right under
        # "Measures:" one level down, two lines of jargon on a row of advice.
        # It now prints `means`, the table's own reader sentence.
        self.assertIn('x-text="k.means"', self.rows)
        self.assertNotIn('x-text="k.measurement"', self.rows)

    def test_the_measurement_moved_into_the_disclosure_and_was_not_dropped(
        self,
    ) -> None:
        # THE mutation the rule "nothing is deleted" is for, and the cheapest
        # way to satisfy the test above: drop `measurement` from the summary
        # instead of re-homing it. Moving a figure behind a disclosure is not
        # exempting it -- so the specification is still one click away, in the
        # panel's own disclosure, beside the metric key that identifies the
        # dial it belongs to.
        disclosure = html_element(self.raw, 'id="knobs-provenance"')
        self.assertIn('x-text="k.measurement"', disclosure)
        self.assertIn('x-text="k.metric"', disclosure)
        # And it is still stated in full at the level whose job is "why".
        self.assertIn(
            'x-text="a.measurement"', html_element(self.raw, 'id="advice-note"')
        )

    def test_no_reader_sentence_is_authored_in_this_file(self) -> None:
        # The same rule that holds for the advice, over the field that is
        # written in plain English and is therefore the tempting one to type
        # here. A sentence in this page would be a claim with no date, no owner
        # and nothing to check it against -- and the page and the table would
        # become two enumerations free to disagree.
        for key, metric in METRICS.items():
            with self.subTest(metric=key):
                self.assertNotIn(metric.means, self.html)
                # Nor a fragment of one: a "shortened for the row" copy is the
                # same defect with a diff nobody would notice.
                self.assertNotIn(metric.means.split(".")[0], self.html)

    def test_the_boundaries_are_on_the_dial_and_in_the_disclosure(self) -> None:
        # #89 review: "at 0.1 judged   at 0.25 judged" is a dump of the table,
        # not a caption. The provenance discipline survives and MOVES: the
        # values and kinds ride on the dial's own `<title>`, and every boundary
        # is stated in full, in its own voice, one click down.
        self.assertNotIn("kmark", self.rows)
        self.assertNotIn('x-text="b.kind"', self.rows)
        for tone in ("tick-cited", "tick-judged", "tick-structural"):
            with self.subTest(tone=tone):
                self.assertIn(
                    f"tickTitleFor(k.gauge.boundaries, '{tone}', k.unit)", self.knobs
                )
        disclosure = html_element(self.raw, 'id="knobs-provenance"')
        for field in ("b.value", "b.kind", "b.statement"):
            with self.subTest(field=field):
                self.assertIn(field, disclosure)
        self.assertIn("provenanceVoice({ kind: b.kind })", disclosure)

    def test_a_judged_target_stays_reachable_from_its_conclusion(self) -> None:
        # The rule the move must not cost: the target IS the visible
        # conclusion, so the judgment behind it cannot be a page away. It wears
        # that boundary's voice class, carries its kind and statement in its
        # own tooltip, and the disclosure that states it in full is in the same
        # panel.
        self.assertIn('class="aim" :class="tickVoice(b.kind)"', self.knobs)
        self.assertIn(":title=\"b.kind + ': ' + b.statement\"", self.knobs)
        self.assertIn('id="knobs-provenance"', self.knobs)

    def test_the_target_is_the_apis_and_the_direction_is_the_metrics(self) -> None:
        # "Aim under X" names a boundary the API marked, and which way to move
        # is the metric's own `worse_when`: two of the five are worse when
        # LOWER, so a page that assumed one direction would point its arrow
        # away from the advice.
        self.assertIn("targetsOf(k.gauge)", self.knobs)
        self.assertIn("aimWord(k.gauge.worse_when)", self.knobs)
        self.assertIn(
            "return gauge.boundaries.filter(b => b.is_target);",
            js_function_body(self.raw, "function targetsOf("),
        )

    def test_every_tone_table_covers_the_apis_vocabulary_exactly(self) -> None:
        # Four tables, four vocabularies, and each must be complete: a state
        # with no entry falls to a default, and a default that rendered a new
        # failure state as a healthy arc is the substitution this repository
        # refuses. Tables rather than chains of ifs precisely so this can be
        # asserted over every member rather than the ones somebody remembered.
        for decl, expected in (
            ("const SEVERITY_ARC", set(SEVERITY_RANK)),
            ("const URGENCY_TAG =", set(SEVERITY_RANK)),
            ("const URGENCY_CLASS =", set(SEVERITY_RANK)),
            ("const TICK_VOICE", set(PROVENANCE_KINDS)),
            ("const STRIP_DOT_TONE", set(STRIP_ORDER)),
            ("const AIM_WORDS", {WORSE_WHEN_HIGHER, WORSE_WHEN_LOWER}),
        ):
            with self.subTest(table=decl):
                self.assertEqual(set(self.table(decl)), expected)

    def test_every_tone_table_keeps_its_states_visibly_apart(self) -> None:
        # A table whose two states resolve to one class is a verdict the reader
        # cannot see -- `PROVENANCE_VOICE`'s rule, over the four new tables.
        for decl in (
            "const SEVERITY_ARC",
            "const URGENCY_TAG =",
            "const URGENCY_CLASS =",
            "const TICK_VOICE",
            "const STRIP_DOT_TONE",
            "const AIM_WORDS",
        ):
            with self.subTest(table=decl):
                values = list(self.table(decl).values())
                self.assertEqual(len(values), len(set(values)))

    def test_an_unmeasured_knob_is_neither_an_urgency_nor_an_all_clear(self) -> None:
        # `null` is not an unrecognised severity: it is the API saying there is
        # no reading. It gets its own label and its own neutral tone, so a knob
        # with no sample can never wear FINE.
        for decl in ("function urgencyTag(", "function urgencyClass("):
            with self.subTest(decl=decl):
                body = js_function_body(self.raw, decl)
                self.assertRegex(body, r"severity === null")
                self.assertLess(body.index("null"), body.index("??"))
        self.assertNotEqual(
            self.table("const URGENCY_TAG =")[SEVERITY_OK],
            re.search(
                r'const URGENCY_TAG_UNMEASURED = "([^"]+)"', self.html
            ).group(1),
        )

    def test_an_unrecognised_state_takes_the_loudest_treatment(self) -> None:
        # Every fallback on this level runs the same direction as the rest of
        # the page: a value this build has never seen must not read as healthy.
        self.assertRegex(
            self.html, r'const SEVERITY_ARC_UNRECOGNISED = "arc-extra";'
        )
        self.assertRegex(self.html, r'const URGENCY_TAG_UNRECOGNISED = "DO NOW";')
        self.assertRegex(self.html, r'const STRIP_DOT_TONE_UNRECOGNISED = "dot-bad";')
        self.assertIn(
            "return TICK_VOICE[kind] ?? TICK_VOICE.judged;",
            js_function_body(self.raw, "function tickVoice("),
        )

    # --- the strip is the API's, dot for dot -------------------------------

    def test_the_strip_iterates_the_apis_own_dots(self) -> None:
        # Four questions today, and a fifth added server-side reaches the strip
        # by construction. Nothing here decides a state or writes a question.
        self.assertIn("d.question", self.strip)
        self.assertIn("d.answer", self.strip)
        self.assertIn("dotTone(d.state)", self.strip)
        for question in STRIP_QUESTIONS.values():
            with self.subTest(question=question):
                self.assertNotIn(question, self.html)

    # --- the observation is not a knob -------------------------------------

    def test_the_observation_panel_carries_no_knob_furniture(self) -> None:
        # THE owner decision as a page-side guarantee. Model tier is a
        # measurement and NOT advice, so the reader must not be able to mistake
        # it for a row they are being told to change: no urgency tag, no gauge,
        # no target, no direction.
        for furniture in (
            "urgencyTag", "urgencyClass", "aimWord", "targetsOf", "arcPath",
            "needlePath", "gauge", "DO NOW",
        ):
            with self.subTest(furniture=furniture):
                self.assertNotIn(furniture, self.observations)
        self.assertIn("not something to change", self.observations)
        self.assertNotIn('id="observations-note"', self.knobs)

    def test_the_observation_says_what_it_ranges_over_and_what_it_is_not(self) -> None:
        self.assertIn("summary.model_mix.sample_is", self.observations)
        self.assertIn("summary.model_mix.sample_calls", self.observations)
        self.assertIn("summary.model_mix.busiest.model", self.observations)
        # A window with no call says so rather than showing a model it never
        # ran, and `orDash` keeps an unrecorded model name from printing as a
        # measurement.
        self.assertIn('x-if="summary && !summary.model_mix.busiest"', self.observations)
        self.assertIn("orDash(summary.model_mix.busiest.model)", self.observations)

    # --- the finding stays; the explanation moves --------------------------

    def observation_resting(self) -> str:
        """The panel with its explanation disclosure removed."""
        return self.observations.replace(
            html_element(self.raw, 'id="observation-detail"'), ""
        )

    def observed_branch(self) -> str:
        """The branch that renders when a model WAS named, at rest.

        Scoped to that branch on purpose: the unmeasured branch beside it is
        required to state its sample in the open (an absence says itself), so a
        check over the whole panel would be satisfied by the wrong half.
        """
        resting = self.observation_resting()
        start = resting.index('x-if="summary && summary.model_mix.busiest"')
        end = resting.index('x-if="summary && !summary.model_mix.busiest"')
        return resting[start:end]

    def test_the_observation_itself_is_what_stays_on_screen(self) -> None:
        # #89's height budget, and the level-2 rule applied one level up: the
        # FINDING stays at rest, the explanation moves. What a reader must be
        # able to see without asking is which model ran this window's work --
        # and that the panel is not advice, which is its HEADING.
        resting = self.observation_resting()
        self.assertIn("summary.model_mix.busiest.model", resting)
        self.assertIn("summary.model_mix.busiest.calls", resting)
        self.assertIn("summary.model_mix.models", resting)
        self.assertIn("not something to change", resting)
        # AND NOT BEHIND A SECOND ONE. Removing the explanation disclosure and
        # then looking for the headline's bindings is satisfied by a headline
        # nested inside a disclosure of its own -- a mutation this test
        # survived when it was written, and the reason the check is on the
        # ABSENCE of any remaining disclosure rather than on the presence of
        # some strings.
        self.assertNotIn("<details", self.observed_branch())

    def test_the_explanation_moved_rather_than_being_dropped(self) -> None:
        # The mutation: satisfy the test above by deleting the sample and the
        # reasoning instead of collapsing them. Both must be INSIDE the
        # disclosure -- an aggregate that stopped naming the set it ranges over
        # would be a figure with no sample, and the owner decision that this is
        # not advice would be a heading with no argument behind it.
        detail = html_element(self.raw, 'id="observation-detail"')
        self.assertIn("summary.model_mix.sample_is", detail)
        self.assertIn("Stated, not recommended", detail)
        self.assertRegex(detail, r"^<details\b")
        self.assertIn("<summary>", detail)
        self.assertNotIn("summary.model_mix.sample_is", self.observed_branch())
        self.assertNotIn("Stated, not recommended", self.observed_branch())

    def test_the_unmeasured_branch_states_its_absence_at_rest(self) -> None:
        # The one state that may NOT collapse: a window that named no model is
        # an absence, and an absence says itself in the open. It carries no
        # disclosure of its own, so "unmeasured, not zero" and the sample it
        # ranges over are both on screen.
        at = self.observations.index("!summary.model_mix.busiest")
        empty = self.observations[at:]
        self.assertIn("unmeasured, not zero", empty)
        self.assertNotIn("<details", empty)


class RecommendationRenderTest(unittest.TestCase):
    """#78: the page renders what it is handed, and holds no table of its own.

    The wiring guard already proves every field reaches a binding. These are
    the properties a binding alone does not give: that the page cannot decide
    anything, that a healthy entry is not filtered out on the way to the
    screen, and that the two provenance voices stay apart.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (Path(__file__).resolve().parent.parent / "index.html").read_text()
        cls.html = strip_comments(cls.raw)
        cls.band = html_element(cls.raw, 'id="advice-note"')

    def test_the_page_holds_none_of_the_tables_prose(self) -> None:
        # THE property #78 asks for: "the page performs no lookup and holds no
        # threshold". Advice hard-coded here would be advice with no date, no
        # provenance and nothing to redline -- which is the alternative the
        # module was written to replace.
        for metric in METRICS.values():
            for entry in metric.ranges:
                with self.subTest(metric=metric.key):
                    self.assertNotIn(entry.recommendation.detail.strip(), self.html)

    def test_the_page_holds_no_lever_directive_of_its_own(self) -> None:
        for target, phrase in LEVER_TARGETS.items():
            with self.subTest(target=target):
                self.assertNotIn(phrase, self.html)

    def test_the_unmeasured_note_comes_from_the_api_not_the_page(self) -> None:
        # A copy here would drift from the module's, and the page would tell
        # two different stories about the same absence on two different days.
        self.assertNotIn(UNMEASURED_NOTE, self.html)
        self.assertIn("summary.recommendations.unmeasured_note", self.html)

    def test_neither_table_level_provenance_is_copied_into_the_page(self) -> None:
        self.assertNotIn(RECOMMENDATION_PROVENANCE, self.html)
        self.assertNotIn(RANKING_PROVENANCE, self.html)

    def test_the_page_iterates_every_ranked_reading(self) -> None:
        # An `ok` rendered as absence is the mutation this catches: a filter in
        # the loop would hide healthy readings, and healthy and unmeasured would
        # then look identical on screen -- which is the defect the table's
        # explicit healthy entry exists to prevent.
        loops = re.findall(r'x-for="[^"]*\bin\s*([^"]+)"', self.band)
        ranked = [
            iterated.strip()
            for iterated in loops
            if "recommendations.ranked" in iterated
        ]
        self.assertEqual(ranked, ["summary.recommendations.ranked"])

    def test_the_healthy_case_renders_a_statement_rather_than_a_blank(self) -> None:
        # The other half: a reading with no lever must SAY it has none, or the
        # reader sees a row where the others have a directive and cannot tell
        # "nothing to change" from "we forgot".
        self.assertIn('x-if="!a.lever"', self.band)

    def test_the_two_provenance_voices_are_distinct_classes(self) -> None:
        # A judged boundary rendered in a cited one's voice is the mutation
        # here. The mapping is a table in the page precisely so this can assert
        # over every kind rather than over the two somebody remembered.
        voices = dict(
            re.findall(
                r"(\w+):\s*\"(advice-\w+)\"",
                js_function_body(self.raw, "const PROVENANCE_VOICE"),
            )
        )
        self.assertEqual(set(voices), set(PROVENANCE_KINDS))
        self.assertEqual(
            len(set(voices.values())),
            len(PROVENANCE_KINDS),
            f"two provenance kinds render in one voice: {voices}",
        )

    def test_every_provenance_voice_has_a_style_that_marks_it(self) -> None:
        # A class with no rule is a distinction that exists only in the DOM.
        for kind in PROVENANCE_KINDS:
            with self.subTest(kind=kind):
                self.assertRegex(self.raw, rf"\.advice-{kind}\s*\{{[^}}]+\}}")

    def test_both_edges_take_their_voice_from_the_payloads_own_kind(self) -> None:
        # Not from where the boundary sits and not from the metric: a hard-coded
        # class on either edge would survive every test above.
        for edge in ("lower_provenance", "upper_provenance"):
            with self.subTest(edge=edge):
                self.assertIn(f':class="provenanceVoice(a.{edge})"', self.band)

    def test_the_unmeasured_mapping_is_consumed_whole(self) -> None:
        # The wiring guard CANNOT check this one, and the gap is worth stating.
        # Its fixture measured all four metrics when this was written, so
        # `unmeasured` was `{}` there; an empty mapping has no members for the
        # walk to adjudicate, so naming the path was enough to satisfy it
        # (mutation-checked: replacing the `Object.entries` consumer with a
        # bare `summary.recommendations.unmeasured.rows` leaves the guard
        # GREEN). #84 made that fixture's mapping non-empty by adding a metric
        # it cannot measure, which is luck rather than coverage -- the next
        # fixture may measure everything again.
        #
        # What makes every member reachable is therefore not the fixture but
        # the shape: the keys are metric names the SERVER decides, so the page
        # cannot name them in advance, and consuming the mapping whole is the
        # structural guarantee that covers all of them however many there are.
        # `context.percentiles` is the same pattern, and `walk_payload` blesses
        # it for the same reason. This asserts it directly rather than leaving
        # it to a corpus that happens to be complete.
        # `is_read_whole` alone is too weak to assert here: the x-if guard
        # spells `Object.keys(...).length`, which satisfies it while rendering
        # not one member. What has to hold is that the mapping is ITERATED, so
        # the assertion is on the loop.
        self.assertNotEqual(
            iteration_aliases(self.html, {"summary.recommendations.unmeasured"}),
            frozenset(),
            "nothing iterates the unmeasured mapping, so the metrics it names "
            "reach no reader -- and the wiring guard cannot be relied on to "
            "say so, because a fixture that measures every metric leaves this "
            "mapping empty",
        )
        self.assertTrue(
            is_read_whole(self.html, {"summary.recommendations.unmeasured"}),
            "the unmeasured metrics are keyed on names the SERVER chooses, so "
            "the page cannot name them in advance: consuming the mapping whole "
            "is what makes every member reachable however many there are",
        )

    def test_the_band_annotates_rather_than_adding_a_table(self) -> None:
        # Constraint 4. #88 promoted this band to question card THREE, which is
        # the level change that issue authorises rather than an appended
        # surface -- the net count across both views is pinned unchanged in
        # `ReportViewSplitTest`. The rendering itself is still ranked ROWS
        # rather than a `<table>`: the detail view is where tables live.
        self.assertIn('class="q"', self.band)
        self.assertNotIn("<table", self.band)

    def test_the_readings_render_as_ranked_rows_rather_than_paragraphs(self) -> None:
        # #88's complaint in its most literal form: each reading was four
        # stacked `advice-line` paragraphs, which is why eight of them read as
        # a wall. A row carries its RANK, what it is, and its figure at the
        # right -- and the rank is the loop index rather than a number the page
        # invents, so it cannot disagree with the order the API published.
        self.assertIn('<div class="rank" x-text="i + 1"></div>', self.band)
        self.assertIn('<div class="n" x-text="fmtUnit(a.value, a.unit)"></div>', self.band)
        self.assertRegex(self.raw, r"\.opp\s*\{[^}]+\}")
        self.assertRegex(self.raw, r"\.opp \.fig\s*\{[^}]*text-align:right")


class RecommendationWiringIsCompleteTest(unittest.TestCase):
    """#84: the two enumerations of the metric set cannot drift apart.

    THE FAILURE THIS EXISTS FOR, because it happened. `recommendations.METRICS`
    declares the metrics; `serve._recommendations()` computes a value for each;
    nothing connected the two. A branch added a fifth metric to the table, ran
    the whole suite green, and broke on merge -- `assess_all()` refusing a
    four-key mapping against a five-metric table, on every Python version, in
    every test that served a summary.

    The refusal was right and is not relaxed. What was missing is that it can
    only fire when a request builds a mapping: at the commit that branch forked
    from, `serve.py` did not mention `assess_all` at all, so a suite that never
    served a summary from that tree had nothing to notice. The coupling was
    predicted in the module's own docstring and left untied.

    So the tie is `serve.RECOMMENDED_METRICS`, checked at IMPORT -- which is
    what makes the failure arrive on the branch that causes it rather than on
    the merge that reveals it. These tests pin the tie itself, and the payload
    test above pins that the declaration still describes what is computed.
    """

    def test_serve_declares_exactly_the_metrics_the_table_declares(self) -> None:
        # Set equality, not containment, and the message names both directions:
        # a metric declared and not computed 500s the whole payload, and one
        # computed but not declared is a query paid for and never read.
        self.assertEqual(
            set(RECOMMENDED_METRICS),
            set(METRICS),
            "serve and recommendations enumerate different metric sets",
        )

    def test_a_metric_the_table_declares_and_serve_does_not_is_refused(self) -> None:
        # The #84 shape exactly: the table grows by one, the caller does not.
        with self.assertRaises(RuntimeError) as caught:
            _refuse_unwired_metrics(
                frozenset(METRICS), frozenset(METRICS) | {"a_sixth_metric"}
            )
        self.assertIn("a_sixth_metric", str(caught.exception))

    def test_a_metric_serve_computes_and_no_table_declares_is_refused(self) -> None:
        # The quieter direction, and the reason this is not a restatement of
        # `assess_all()`'s own refusal: that one raises on a MISSING key and
        # says nothing about a surplus one, which `assess_all` would report as
        # an unknown metric only once a request reached it.
        with self.assertRaises(RuntimeError) as caught:
            _refuse_unwired_metrics(
                frozenset(METRICS) | {"a_metric_nobody_assesses"}, frozenset(METRICS)
            )
        self.assertIn("a_metric_nobody_assesses", str(caught.exception))

    def test_agreeing_sets_pass_whatever_they_contain(self) -> None:
        # Teeth on the two above: a guard that raised unconditionally would
        # pass both of them and refuse every import.
        self.assertIsNone(_refuse_unwired_metrics(frozenset(), frozenset()))
        self.assertIsNone(_refuse_unwired_metrics(frozenset({"x"}), frozenset({"x"})))

    def test_the_guard_runs_at_import_not_only_when_a_summary_is_served(
        self,
    ) -> None:
        # WHERE the check sits is the whole fix, so it is asserted rather than
        # left to the reader: the call is at module scope in serve.py, outside
        # every class and function, so `import serve` performs it. A check
        # moved inside `_recommendations()` would be green on a branch that
        # serves no summary -- which is the state #84 shipped in.
        source = (Path(__file__).resolve().parent.parent / "serve.py").read_text()
        calls = [
            line
            for line in source.splitlines()
            if line.startswith("_refuse_unwired_metrics(")
        ]
        self.assertEqual(
            len(calls),
            1,
            "the completeness check is not called at module scope in serve.py, "
            "so importing the module no longer performs it",
        )


class RecommendationVersionTest(unittest.TestCase):
    """A new payload field is a MINOR release, and the manifest carries it.

    `docs/versioning.md`: the HTTP API's payload fields are a governed surface,
    and "a new payload field" is listed there as minor. The manifest matters on
    its own -- Claude Code's plugin loader reads that JSON without running any
    Python and uses the version as its update cache key, so an unbumped one
    means an installed user is never offered the change. `tests/test_cpb.py`
    already pins the manifest equal to `cpb.VERSION`; this pins the floor that
    shipping `recommendations` puts under both.
    """

    RECOMMENDATIONS_MINOR = (1, 2, 0)
    # #84: a FIFTH metric is a new member of `recommendations.unmeasured` and a
    # new possible member of `ranked` -- payload fields both, so minor again.
    # The floor is per change rather than one constant re-pointed, because what
    # each release owed is a fact about that release: overwriting this would
    # make the previous claim unverifiable.
    PER_TTL_REPAYMENT_MINOR = (1, 3, 0)
    # #89: `recommendations.knobs[].means` is a new payload field -- the
    # reader-facing sentence the summary rows print instead of `measurement`.
    # Minor again, and recorded per change rather than by re-pointing one
    # constant: what each release owed is a fact about that release, and
    # overwriting it would make the previous claim unverifiable.
    READER_SENTENCE_MINOR = (1, 6, 0)

    def test_serving_the_recommendation_block_owes_a_minor_bump(self) -> None:
        self.assertIn("recommendations", Api.summary.__doc__ or "")
        parsed = tuple(int(p) for p in cpb.VERSION.split("."))
        self.assertGreaterEqual(
            parsed,
            self.RECOMMENDATIONS_MINOR,
            "/api/summary carries a `recommendations` block, which is a new "
            "payload field and so a MINOR release (docs/versioning.md). The "
            "plugin manifest is Claude Code's update cache key: left unbumped, "
            "installed users receive nothing.",
        )

    def test_serving_the_per_ttl_repayment_owes_a_further_minor_bump(self) -> None:
        # The metric has to be IN the payload for the floor to be owed, so both
        # are asserted here: a version bumped without the field, or a field
        # shipped without the bump, each fails.
        self.assertIn(METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, RECOMMENDED_METRICS)
        parsed = tuple(int(p) for p in cpb.VERSION.split("."))
        self.assertGreaterEqual(
            parsed,
            self.PER_TTL_REPAYMENT_MINOR,
            "`cache_write_repayment_at_own_ttl` is a new member of the "
            "`recommendations` payload, which docs/versioning.md makes a MINOR "
            "release. Bump cpb.VERSION and .claude-plugin/plugin.json together "
            "-- the manifest is the plugin loader's update cache key.",
        )

    def test_the_reader_sentence_owes_a_further_minor_bump(self) -> None:
        # Same shape as the two above, and asserted against a SERVED payload
        # rather than against the table: the field has to be in
        # `/api/summary`'s `knobs` for the floor to be owed, so a version
        # bumped without the field and a field shipped without the bump each
        # fail here.
        knobs = Api._knobs(
            assess_all({key: Reading(None, 0) for key in METRICS})
        )
        self.assertEqual(len(knobs), len(METRICS))
        for knob in knobs:
            with self.subTest(metric=knob["metric"]):
                self.assertEqual(knob["means"], METRICS[knob["metric"]].means)
        parsed = tuple(int(p) for p in cpb.VERSION.split("."))
        self.assertGreaterEqual(
            parsed,
            self.READER_SENTENCE_MINOR,
            "`knobs[].means` is a new field on the `recommendations` payload, "
            "which docs/versioning.md makes a MINOR release. Bump cpb.VERSION "
            "and .claude-plugin/plugin.json together -- the manifest is the "
            "plugin loader's update cache key.",
        )


# ---------------------------------------------------------------------------
# #70: two views of one payload.
# ---------------------------------------------------------------------------
#
# The report used to be one page serving two readers. It is now an OVERVIEW
# (the landing view) and a DETAILS & RAW DATA view one click away, and the
# whole risk of that split is stated in one sentence: two renderings of one
# figure that can drift is "one sample, one definition" with a navigation step
# hidden in the middle.
#
# Three mechanisms answer it, and each has its own test below:
#
#   * what BOTH views need is rendered ONCE, above them, in neither section --
#     the banner and the data-age line are single elements, so they cannot
#     disagree because there is no second rendering to disagree with;
#   * no `summary.<path>` may be bound in both sections. A figure needed by
#     both goes through ONE named getter, which is the only shape in which
#     "there is a single definition" is a checkable statement;
#   * the period every figure covers is `periodLabel`, defined once and bound
#     in both, and neither view may compose one from `from`/`to` itself.
#
# These are structural assertions with the limit the rest of this file records:
# the project ships no JS runtime (stdlib only, no Node), so they pin the
# bindings rather than executing the render. What they CANNOT catch is two
# DIFFERENT fields presented as one figure -- no static check can -- which is
# exactly what the single-getter rule exists to make unnecessary.

# The panels that make up the report, and which of the three regions each one
# lives in. This dict IS the acceptance criterion "every panel on the report
# today survives": a panel dropped from the detail view leaves its id in no
# section and turns `test_every_panel_survives_in_a_named_view` red.
CHROME_PANELS = {
    # Rendered ONCE for both views. A warning is the worst possible thing to
    # render twice: two copies free to disagree is a reader shown the milder
    # of two true statements with no sign the other exists (BannerPrecedence-
    # Test's defect, one level up), and a warning visible on only one view is
    # a warning the reader can navigate away from without resolving.
    "banner",
    "data-age",
    # #64's verdict, and it is chrome for the same reason the banner is: it
    # qualifies EVERY figure in either view, so a rendering the reader could
    # navigate away from would be a verdict they could leave behind unresolved.
    # One element, no second copy to drift from. #88 made it question card ONE
    # -- #62's first question is this verdict -- without moving it into a view:
    # what changed is that it now looks like what it always was.
    "health-note",
    # The affordance itself: one click there, one click back, from either view.
    "view-tabs",
}
# #88: #62's four questions, as four cards. Question 1 is `health-note` above,
# in the chrome; 2, 3 and 4 are here.
#
# `overview-period` LEFT THE REGISTER AND DID NOT LEAVE THE PAGE. It is a
# caption naming the window the figures cover, and its twin `#details-period`
# was never registered as a panel either -- so #88 removed the asymmetry rather
# than exploiting it. `test_the_period_caption_is_not_counted_as_a_panel` pins
# that it is still there and still bound.
OVERVIEW_PANELS = {"context-note", "advice-note", "next-note"}
# #89's LEVEL ONE, and the only surfaces that were ADDED rather than moved.
#
# Constraint 4 ("a new detector earns space by displacing or annotating") is
# about adding to a screen, and this is a new SCREEN -- #70's own argument, one
# level up. The summary displaces nothing because nothing was there: it is the
# first thing a reader meets and the two levels below it kept every panel they
# had. What the constraint still buys is the count below, restated
# deliberately rather than raised by habit.
#
# `observations-note` is a SEPARATE surface from `knobs-note` on purpose and by
# owner decision. Model tier is a real measurement and is NOT advice -- CPB
# counts tokens and cannot see whether a cheaper model would have needed more
# attempts -- so it carries no urgency tag, no gauge, no target and no
# direction, and it sits under a heading that says so. A dimmed row among the
# knobs would have read as a knob somebody had decided was fine.
SUMMARY_PANELS = {"status-strip", "knobs-note", "observations-note"}
# The raw token deck and the scope band MOVED DOWN, they were not dropped: they
# are inputs to the four answers rather than answers, and "Input tokens 32.1k /
# Cache-read 443.47M" with no verdict is the reaction #62 was filed over. The
# scope band travels with the deck because it is the deck's own caveat -- "every
# figure below is a main-thread figure, not a session total" names the scope of
# exactly those cards, and split from them it would warn about numbers on
# another page.
DETAIL_PANELS = {
    "filters", "cards", "scope-note", "chart-panel", "models", "detail",
    "sessions", "agents", "outliers",
}
# What "net panel count must not rise" (#88) means as a number, restated for
# the third level (#89). Sixteen through #88 -- 4 chrome + 3 overview + 9
# detail -- and nineteen now: the summary level added three surfaces and moved
# nothing off the two levels below it, which both keep every panel they had.
#
# Asserted as EQUALITY, and per level as well as in total, for
# `EXPECTED_NOT_RENDERED`'s reason: a ceiling nobody restates is a budget to
# spend, and raising this literal has to be a line somebody argues for.
EXPECTED_SURFACES = 19

# Every JS member name that can sit on the tail of a payload read. Stripped
# before two views' bindings are compared, so `summary.calls` in one view and
# `summary.calls.toLocaleString()` in the other are recognised as the same
# field read twice rather than as two different paths.
JS_MEMBERS = frozenset({
    "length", "join", "includes", "toLocaleString", "toFixed", "toPrecision",
    "slice", "filter", "map", "split", "entries", "keys", "values", "some",
    "every", "find", "indexOf", "sort", "reverse", "concat", "push",
})
SUMMARY_PATH = re.compile(
    r"\bsummary(?:\??\.[A-Za-z_$][\w$]*)+"
)
# The attributes through which a value reaches the page. A binding is one of
# these; a mention in prose is not.
BINDING_ATTR = re.compile(r'(?:x-text|x-model|x-if|x-show|x-for|:value|:class|:title)="([^"]*)"')


def view_section(html: str, view: str) -> str:
    """The markup of one of #70's two views, comments stripped."""
    return html_element(html, f'id="view-{view}"')


def view_surface(html: str, section: str) -> str:
    """Everything one view can read the payload THROUGH.

    Not the section markup alone. A view renders `x-for="(c, i) in cards"`, and
    the figures that loop puts on screen are spelled in the `cards` GETTER --
    so a check that compared the two sections' markup would call the six
    summary cards unbound and, worse, would let the detail view re-render the
    median card's own figure without noticing. The surface of a view is its
    markup plus the body of every getter it names, transitively.

    Transitively because `cards` reads `contextSampleLine`, which is where that
    card's sample is composed: one hop would stop exactly where the fields are.
    """
    bodies = getter_bodies(strip_comments(html))
    surface, seen = section, set()
    while True:
        named = {
            name for name in bodies
            if name not in seen and re.search(rf"\b{re.escape(name)}\b", surface)
        }
        if not named:
            return surface
        seen |= named
        surface += "\n" + "\n".join(bodies[name] for name in sorted(named))


def summary_paths(source: str) -> set[str]:
    """Every `/api/summary` path `source` reads, normalised.

    Normalised by dropping JS member names from the tail, so the comparison is
    between FIELDS rather than between spellings of a read.
    """
    paths = set()
    for match in SUMMARY_PATH.findall(source):
        parts = match.replace("?.", ".").split(".")[1:]
        while parts and parts[-1] in JS_MEMBERS:
            parts.pop()
        if parts:
            paths.add(".".join(parts))
    return paths


# #89: the payload paths more than one LEVEL reads, declared with the reason.
#
# The rule the three-level page needs, and it is narrower than "no field twice".
# Two renderings of a FIGURE can drift -- one rounds, one does not; one is
# recomputed in a getter, one is not -- and that is what `test_no_summary_
# figure_is_rendered_at_two_levels` still forbids outright. A server-composed
# SENTENCE or DATE cannot: there is one string, written once in `serve.py`, and
# both levels render exactly it. So a shared path must be declared here AND
# must resolve to a string in the payload, which `ThreeLevelPayloadTest`
# checks against a real one.
#
# The figures the summary and the four cards genuinely share -- every reading
# in the table -- are not shared as paths at all. `recommendations.knobs` is
# built server-side from the SAME `Assessment` objects `recommendations.ranked`
# is built from, and `ThreeLevelPayloadTest` asserts the two agree field by
# field. One derivation, published twice, tied in Python where a test can
# execute it -- `RANKED_BY`'s discipline, at the level of a page split.
SHARED_READINGS = {
    "recommendations.as_of": (
        "#89: the date the judged boundaries were decided. The summary's "
        "gauges and the diagnosis card below both draw those boundaries, and a "
        "judged table whose date appears on only one level is a judgment "
        "presented as a fact on the other. One string, two renderings, nothing "
        "between them to drift."
    ),
    "recommendations.provenance": (
        "#89: the table's own provenance, for the same reason as `as_of` -- "
        "the summary draws the boundaries and must say in whose voice they "
        "were decided, and it says it in the module's own words rather than "
        "in a paraphrase this page would own."
    ),
    "recommendations.unmeasured_note": (
        "#89: what an unmeasured reading means, said once by "
        "`recommendations.py`. The summary renders it where a gauge has no "
        "needle and the diagnosis card renders it against the metric; a second "
        "copy would tell two stories about one absence."
    ),
    "recommendations.under_sampled_note": (
        "#93: what a reading measured over too little means, and what to do "
        "about it -- the one genuinely useful thing the report can tell a new "
        "user, so both the level they land on and the level they drill into "
        "have to be able to say it. Shared for exactly `unmeasured_note`'s "
        "reason and held to the same rule: it is the table's sentence, said "
        "once, and a copy on either level would let 'come back after a few "
        "more sessions' drift into two different promises."
    ),
}


class ReportViewSplitTest(unittest.TestCase):
    """Three levels over one payload (#70, extended by #89).

    The owner's decision, taken before either was built: the landing view is
    the one that answers the question most readers arrive with, and everything
    else is one click away. #70 made that two levels; #89 makes it three --
    what do I do, why, and show me everything -- and the same two properties
    are load-bearing rather than tidy at every one of them. One click there and
    one click BACK, or the extra click compounds instead of being paid once.
    And the view is in the URL, or anyone who has bookmarked this report loses
    their bookmark silently.

    THE LIMIT, STATED PLAINLY. These pin which binding sits at which level and
    what decides a gauge's arc. They cannot see whether the summary fits on a
    screen, whether a needle is where a reader would look for it, or whether
    the dimmed rows read as "already fine" rather than as "broken". Three
    defects on this page have shipped green for exactly that reason.
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (cls.ROOT / "index.html").read_text()
        cls.html = strip_comments(cls.raw)
        cls.summary_view = view_section(cls.raw, "summary")
        cls.overview = view_section(cls.raw, "overview")
        cls.details = view_section(cls.raw, "details")

    def component(self, decl: str) -> str:
        return js_function_body(self.html, decl)

    def levels(self) -> dict:
        return {
            "summary": self.summary_view,
            "overview": self.overview,
            "details": self.details,
        }

    # --- the landing view and the way back ---------------------------------

    def test_the_summary_is_the_landing_view(self) -> None:
        # The DEFAULT, not merely a reachable state (#89). A page that opens on
        # the four question cards has not gained a level, it has gained a tab
        # nobody will find -- and the summary is the one level whose whole
        # claim is that it is the first thing read.
        component = self.component("function report(")
        self.assertRegex(component, re.compile(r'^\s*view: DEFAULT_VIEW,$', re.M))
        self.assertRegex(self.html, r'const DEFAULT_VIEW = "summary";')
        for view in ("summary", "overview", "details"):
            with self.subTest(view=view):
                self.assertIn(f"""x-show="view === '{view}'\"""", self.raw)

    def test_every_level_has_a_tab_and_the_tabs_belong_to_none_of_them(self) -> None:
        # Symmetry is the whole point, and with three levels it is what stops
        # the middle one becoming unreachable from the other two. The tabs are
        # ITERATED over `VIEWS`, so a level added without a way in is not a
        # thing that can be built -- and they sit in NO section, so the way
        # back is on screen wherever the reader has scrolled to.
        tabs = html_element(self.raw, 'id="view-tabs"')
        self.assertIn('x-for="v in views"', tabs)
        self.assertIn("setView(v)", tabs)
        self.assertIn("viewLabel(v)", tabs)
        self.assertIn(
            'const VIEWS = ["summary", "overview", "details"];',
            self.html,
        )
        for section in self.levels().values():
            self.assertNotIn('id="view-tabs"', section)

    def test_every_view_has_a_label_and_no_label_names_a_missing_view(self) -> None:
        # The key and the label deliberately differ for two of the three: the
        # URL keeps #70's names so a bookmark still resolves to the level it
        # named, and the tabs say what the levels are. That is only safe while
        # the two sets correspond exactly -- a label with no view is a tab that
        # cannot exist, and a view with no label would render its raw key.
        labels = dict(
            re.findall(
                r"(\w+):\s*\"([^\"]+)\"",
                re.search(r"const VIEW_LABELS = \{([^}]*)\}", self.html).group(1),
            )
        )
        self.assertEqual(
            sorted(labels),
            ["details", "overview", "summary"],
            "VIEW_LABELS and VIEWS name different sets of levels",
        )
        self.assertEqual(
            labels,
            {"summary": "Summary", "overview": "Details", "details": "Raw data"},
        )

    # --- nothing is removed ------------------------------------------------

    def test_every_panel_survives_in_a_named_view(self) -> None:
        # THE acceptance criterion, now over three levels: every panel the
        # report had is still on it, in a named place, and in exactly one. A
        # dropped panel is red here and nowhere else -- the wiring guard would
        # not see it, because the payload field it renders may well still be
        # read somewhere.
        owned = {
            "summary": SUMMARY_PANELS,
            "overview": OVERVIEW_PANELS,
            "details": DETAIL_PANELS,
        }
        sections = self.levels()
        for view, panels in owned.items():
            for panel in sorted(panels):
                for other, section in sections.items():
                    with self.subTest(panel=panel, view=view, section=other):
                        if other == view:
                            self.assertIn(f'id="{panel}"', section)
                        else:
                            self.assertNotIn(f'id="{panel}"', section)

    def test_the_third_level_did_not_cost_the_other_two_a_panel(self) -> None:
        # CLAUDE.md constraint 4 as #88 and #89 state it: a level may be
        # restructured, and the count is restated deliberately rather than
        # raised by habit. Asserted per level as well as in total, so a change
        # that moved one panel down and added two up cannot pass on the sum.
        surfaces = CHROME_PANELS | SUMMARY_PANELS | OVERVIEW_PANELS | DETAIL_PANELS
        self.assertEqual(len(surfaces), EXPECTED_SURFACES)
        self.assertEqual(len(CHROME_PANELS), 4)
        self.assertEqual(len(SUMMARY_PANELS), 3)
        self.assertEqual(len(OVERVIEW_PANELS), 3)
        self.assertEqual(len(DETAIL_PANELS), 9)

    def test_the_four_questions_are_the_overviews_structure(self) -> None:
        # #62's information architecture, as MARKUP rather than as prose: four
        # cards, each numbered, each headed by the question it answers. The
        # first is chrome (see CHROME_PANELS); the other three are the whole of
        # the overview.
        questions = [
            ("health-note", "1", "Is anything blowing up?"),
            ("context-note", "2", "Am I wasting context?"),
            ("advice-note", "3", "Where can I optimize?"),
            ("next-note", "4", "What do I do next?"),
        ]
        for panel, number, question in questions:
            with self.subTest(question=question):
                card = html_element(self.raw, f'id="{panel}"')
                self.assertRegex(
                    card,
                    rf'<h2><span class="num">{number}</span>{re.escape(question)}</h2>',
                    f"{panel} is not headed by question {number}",
                )
        # THE CARD IDIOM IS NOT THE DETAIL VIEW'S. `.panel` is what the tables
        # and the chart wear, and the count of it is pinned elsewhere; a
        # question card that reused it would erase the level distinction this
        # whole split is built on.
        for panel, _number, _question in questions:
            with self.subTest(panel=panel, css="q"):
                self.assertRegex(
                    self.raw, rf'<section class="q" id="{panel}"'
                )

    def test_the_period_caption_is_not_counted_as_a_panel(self) -> None:
        # It left the register and did not leave the page. All three levels
        # name the window their figures cover, through the one getter that
        # composes it; none of the three namings is a panel.
        for view, element in (
            ("summary", "summary-period"),
            ("overview", "overview-period"),
            ("details", "details-period"),
        ):
            with self.subTest(view=view):
                caption = html_element(self.raw, f'id="{element}"')
                self.assertIn('class="period"', caption)
                self.assertIn("periodLabel", caption)
                self.assertNotIn(
                    element, SUMMARY_PANELS | OVERVIEW_PANELS | DETAIL_PANELS
                )

    def test_each_panel_exists_exactly_once_in_the_page(self) -> None:
        # A panel duplicated into both views is the disagreement this issue is
        # about, in its most literal form.
        for panel in sorted(
            CHROME_PANELS | SUMMARY_PANELS | OVERVIEW_PANELS | DETAIL_PANELS
        ):
            with self.subTest(panel=panel):
                self.assertEqual(self.html.count(f'id="{panel}"'), 1)

    def test_the_shared_chrome_belongs_to_neither_view(self) -> None:
        # The strongest form of "cannot disagree": not two bindings kept equal,
        # but ONE element that both views are looking at.
        for panel in sorted(CHROME_PANELS):
            with self.subTest(panel=panel):
                self.assertIn(f'id="{panel}"', self.html)
                for section in self.levels().values():
                    self.assertNotIn(f'id="{panel}"', section)

    # --- the three levels cannot disagree ----------------------------------

    def test_no_summary_path_is_shared_between_levels_undeclared(self) -> None:
        # A path read at two levels is two renderings of one thing with a
        # navigation step between them. Where that thing is a FIGURE they can
        # drift, and the test below forbids it outright; where it is a sentence
        # the server composed they cannot, and #89 needs a few of those -- the
        # summary draws the table's boundaries and has to say in whose voice
        # and on what date, in the same words the level below uses.
        #
        # So sharing is DECLARED rather than banned, and an undeclared share is
        # red. Over the view SURFACE, not the markup: the summary cards are
        # composed in a getter, and a level that re-rendered one of their
        # figures through one would be invisible to a check that read templates
        # only.
        surfaces = {
            name: summary_paths(view_surface(self.raw, section))
            for name, section in self.levels().items()
        }
        shared = set()
        names = sorted(surfaces)
        for i, one in enumerate(names):
            for other in names[i + 1:]:
                shared |= surfaces[one] & surfaces[other]
        undeclared = sorted(shared - set(SHARED_READINGS))
        self.assertEqual(
            undeclared,
            [],
            f"{undeclared} is read at more than one level and not declared in "
            "SHARED_READINGS. Two renderings of one figure can drift; publish "
            "it once server-side, or declare the share and say why.",
        )

    def test_the_shared_register_names_nothing_that_is_not_shared(self) -> None:
        # The other way a register rots. An entry for a path only one level
        # reads asserts a decision nobody made, and it exempts that path from
        # the check above the moment a second level does start reading it.
        surfaces = {
            name: summary_paths(view_surface(self.raw, section))
            for name, section in self.levels().items()
        }
        stale = sorted(
            path
            for path in SHARED_READINGS
            if sum(path in paths for paths in surfaces.values()) < 2
        )
        self.assertEqual(stale, [], f"SHARED_READINGS names unshared paths: {stale}")

    def test_every_shared_reading_cites_the_decision_it_records(self) -> None:
        for path, reason in sorted(SHARED_READINGS.items()):
            with self.subTest(path=path):
                self.assertRegex(reason, r"#\d+")
                self.assertGreater(len(reason), 60, f"{path}: not a reason")

    def test_the_readings_reach_the_summary_by_one_payload_path(self) -> None:
        # The strict half, and where the three levels are ACTUALLY kept honest.
        # Every reading the summary draws arrives as `recommendations.knobs`,
        # which `serve.Api._knobs()` builds from the same `Assessment` objects
        # `recommendations.ranked` is built from -- so the figure on the gauge
        # and the figure on the diagnosis card are one derivation published
        # twice, and `ThreeLevelPayloadTest` asserts they agree field by field.
        #
        # A summary that read `ranked` directly would pass every check above
        # and be a second rendering of a figure with a navigation step between
        # them, which is the whole thing this split must not become.
        self.assertIn("summary.recommendations.knobs", self.summary_view)
        for path in ("ranked", "unmeasured"):
            with self.subTest(path=path):
                self.assertNotRegex(
                    self.summary_view,
                    rf"summary\.recommendations\.{path}(?!\w)",
                )
        self.assertNotIn("recommendations.knobs", self.overview)
        self.assertNotIn("recommendations.knobs", self.details)

    def test_the_period_is_named_by_one_binding_at_every_level(self) -> None:
        # The one figure all three levels genuinely need, and therefore the
        # test case for the rule above: each names the period it describes and
        # there is ONE definition.
        self.assertEqual(self.html.count("get periodLabel("), 1)
        for view, section in self.levels().items():
            with self.subTest(view=view):
                self.assertIn("periodLabel", section)

    def test_no_level_composes_a_period_of_its_own(self) -> None:
        # How the rule above would be defeated: not by binding the same path
        # twice, but by rebuilding the same sentence out of the range state.
        # The picker owns `from`/`to`/`range`; everything else asks
        # `periodLabel`.
        for view in ("summary", "overview"):
            for expr in BINDING_ATTR.findall(self.levels()[view]):
                with self.subTest(view=view, binding=expr):
                    self.assertNotRegex(expr, r"\b(from|to|range)\b")
        picker = html_element(self.raw, 'id="filters"')
        for expr in BINDING_ATTR.findall(self.details.replace(picker, "")):
            with self.subTest(binding=expr):
                self.assertNotRegex(expr, r"\b(from|to)\b")

    def test_a_figure_unmeasured_at_one_level_is_unmeasured_at_the_others(self) -> None:
        # Absence must not become a value by changing level, so every level
        # runs its figures through the SAME formatters -- the ones
        # `AbsenceIsNeverRenderedAsAValueTest` pins as answering absence before
        # they compute. A second copy of one would be a second rule.
        for formatter in ("fmtTok", "fmtCount", "fmtPct", "fmtMetric", "fmtUnit"):
            with self.subTest(formatter=formatter):
                self.assertEqual(self.html.count(f"function {formatter}("), 1)
        self.assertIn("fmtTok(", self.overview)
        self.assertIn("fmtTok(", self.details)
        # A reading is printed in the metric's own unit, and BOTH levels that
        # show one go through the same dispatcher: the summary would otherwise
        # print `30.3%` where the diagnosis printed `0.3034`, which is one
        # figure in two voices -- the drift a levelled page makes easy even
        # when the value itself cannot move.
        self.assertIn("fmtUnit(", self.summary_view)
        self.assertIn("fmtUnit(", self.overview)

    # --- the view is in the URL --------------------------------------------

    def test_the_view_is_written_to_the_url(self) -> None:
        # A details view that cannot be linked or reloaded is a silent
        # regression for everyone who has bookmarked this report.
        body = self.component("writeUrl() {")
        self.assertIn('p.set("view", this.view)', body)
        self.assertIn("history.replaceState", body)
        self.assertIn("this.writeUrl()", self.component("setView(view) {"))

    def test_the_url_is_read_back_on_load_and_on_navigation(self) -> None:
        body = self.component("applyUrl() {")
        self.assertIn("location.hash", body)
        self.assertIn("this.applyUrl()", self.component("init() {"))
        # Alpine's own event modifier, not `addEventListener` (#8): the page
        # reaches no DOM API. A bookmark opened in an already-loaded tab, or a
        # back button, must land on the view the URL names.
        self.assertIn('x-on:hashchange.window="applyUrl()"', self.raw)

    def test_an_unknown_view_in_the_url_falls_back_to_the_landing_view(self) -> None:
        # A hash is not a route -- there is no request to answer with a 404 --
        # so the page states the view it CAN show rather than a view it cannot.
        body = self.component("applyUrl() {")
        self.assertRegex(body, r"VIEWS\.includes\(\w+\)\s*\?\s*\w+\s*:\s*DEFAULT_VIEW")

    # --- deep links carry their filter -------------------------------------

    # An overview figure, the detail panel it drills into, and the filter the
    # link carries. A bare "Details" link makes the reader do the join
    # themselves, which is the thing this issue names.
    DEEP_LINKS = (
        # #61's per-scope saturation band -> the models table, filtered to that
        # scope. The filter value is the payload's OWN scope label on both
        # ends (`by_scope[].scope` and `models[].scope` are both
        # `serve.SCOPE_LABELS`), so the page invents no equivalence.
        ("context-note", "showPanel('models', { scope: s.scope })"),
        # The unknown-window models -> the heaviest calls that ran on them.
        # Again both ends are server strings: `unknown_models` and
        # `outliers[].model`.
        (
            "context-note",
            "showPanel('outliers', { models: summary.context.utilisation.unknown_models })",
        ),
        # The scope band already told the reader that subagent calls appear
        # under their own bucket in the by-turn-type chart. Now it takes them
        # there, with that grouping selected.
        ("scope-note", "showPanel('chart', { by: 'turntype' })"),
    )

    def test_every_deep_link_names_a_panel_and_carries_its_filter(self) -> None:
        for element, call in self.DEEP_LINKS:
            with self.subTest(link=call):
                self.assertIn(call, html_element(self.raw, f'id="{element}"'))

    def test_a_deep_link_lands_on_the_detail_view(self) -> None:
        body = self.component("showPanel(panel, opts) {")
        self.assertIn('this.view = "details"', body)
        self.assertIn("this.writeUrl()", body)
        # And says where it landed: a filter nobody can see is a table that
        # silently disagrees with the figure that linked to it.
        self.assertIn("scrollIntoView", body)

    def test_the_filter_is_carried_in_the_url_too(self) -> None:
        # Otherwise the deep link survives the click and not the reload, which
        # is the same regression as losing the view.
        body = self.component("writeUrl() {")
        for param in ("panel", "scope", "models", "by"):
            with self.subTest(param=param):
                self.assertIn(f'p.set("{param}"', body)

    def test_a_filtered_table_says_what_it_is_hiding(self) -> None:
        # An unexplained filter is a table that disagrees with the figure that
        # linked to it. Every filtered panel states the filter, the two counts
        # and the way out of it.
        for table in ("models", "outliers"):
            with self.subTest(table=table):
                panel = html_element(self.raw, f'id="{table}-panel"')
                self.assertIn("clearFilters()", panel)
                self.assertIn("filterNote", panel)
                self.assertIn("Showing", panel)

    def test_the_page_spells_no_scope_label_of_its_own(self) -> None:
        # The filter compares one payload string against another. A page-side
        # map from `is_sidechain` to a scope label would be the page inventing
        # an equivalence the API never stated -- `is_sidechain` is the record's
        # own flag and `scope` is derived from `source_kind`, two different
        # measurements -- and that is this issue's defect one level down.
        for decl in ("get shownModels(", "get shownOutliers(", "showPanel(panel, opts) {"):
            with self.subTest(decl=decl):
                body = self.component(decl)
                for label in (SCOPE_MAIN, SCOPE_SUBAGENT):
                    self.assertNotIn(f'"{label}"', body)
                    self.assertNotIn(f"'{label}'", body)

    def test_a_filter_never_turns_an_absence_into_an_empty_window(self) -> None:
        # "Not fetched yet" and "the window holds none" are different facts and
        # only the second may say "No calls in this period" -- so a filtered
        # view of a null payload is null, never []. And a filter that excludes
        # every row says THAT, rather than reporting an empty window.
        self.assertIn("if (this.outliers === null) return null;", self.component("get shownOutliers("))
        self.assertIn("if (!this.summary) return null;", self.component("get shownModels("))
        for table in ("models", "outliers"):
            with self.subTest(table=table):
                self.assertIn("No rows match the filter", html_element(self.raw, f'id="{table}"'))

    # --- the detail view keeps its behaviour -------------------------------

    def test_the_time_picker_still_filters(self) -> None:
        # EVERY control, individually: a picker with one dead button is a
        # picker that silently shows the wrong window for one of its four
        # presets, which is worse than one that visibly does nothing.
        picker = html_element(self.raw, 'id="filters"')
        clicks = re.findall(r'@click="([^"]+)"', picker)
        self.assertEqual(
            clicks,
            ["setRange(1)", "setRange(7)", "setRange(30)", "setRange('all')",
             "applyCustomRange()"],
            "a range control is bound to something other than the picker",
        )
        # The picker is inert unless it re-reads, and inert unless what it
        # re-reads carries the window: a range that changes state and fetches
        # nothing, or fetches without its dates, is a filter that does not
        # filter.
        for decl in ("setRange(days) {", "applyCustomRange() {"):
            with self.subTest(decl=decl):
                self.assertIn("this.load()", self.component(decl))
        qs = self.component("rangeQS() {")
        self.assertIn('p.set("from", this.from)', qs)
        self.assertIn('p.set("to", this.to)', qs)
        self.assertIn("this.rangeQS()", self.component("async load("))

    def test_the_chart_still_switches_groupings(self) -> None:
        # EVERY radio, not "the panel mentions a load()". One grouping left
        # bound to the model and not to the fetch is a control that changes the
        # legend and not the data -- the chart would then be labelled by one
        # grouping and drawn by another, which is a wrong number wearing a
        # right heading.
        panel = html_element(self.raw, 'id="chart-panel"')
        radios = re.findall(r"<input[^>]*type=\"radio\"[^>]*>", panel)
        self.assertEqual(
            sorted(re.search(r'value="([^"]+)"', r).group(1) for r in radios),
            ["class", "scope", "turntype"],
        )
        for radio in radios:
            with self.subTest(radio=radio):
                self.assertIn('x-model="by"', radio)
                self.assertIn('@change="load()"', radio)
        self.assertIn("this.by", self.component("async load("))

    def test_the_chart_is_resized_when_its_view_becomes_visible(self) -> None:
        # Chart.js sizes to its container, and `x-show` gives a hidden one no
        # size at all -- so a chart drawn while the overview was showing comes
        # up 0px high unless it is told to re-measure.
        self.assertIn("this.chart.resize()", self.component("setView(view) {"))

    def test_the_split_introduces_no_new_fetch(self) -> None:
        # The page already retrieves everything both views need. A split that
        # multiplied requests would pay for the structure with the one budget
        # this tool does not have -- `serve.py` is a single-threaded
        # HTTPServer.
        self.assertEqual(self.html.count("await fetch("), 1)
        self.assertEqual(
            self.html.count("getJSON("),
            7,
            "one declaration, five in load(), one in showDetail() -- any other "
            "count is a request the split added",
        )


# ---------------------------------------------------------------------------
# #64 / #65: the health verdict and the growth curve.
# ---------------------------------------------------------------------------
#
# ONE corpus, built so that no test below can pass by accident:
#
#   * the main thread's context RISES across the clean day and the subagents'
#     FALLS across the same minutes, so a curve computed over the wrong scope,
#     or pooled across both, produces different numbers in the opposite
#     direction. A fixture whose two scopes agreed would let a dropped filter
#     through;
#   * every quarter of the clean day holds FOUR calls, which is one above
#     `GROWTH_MIN_CALLS_PER_QUARTER`, and each quarter's four are ordered in
#     TIME as 1st-4th-2nd-3rd, so a "first call", "last call" or "mean"
#     standing in for the median reports a different figure;
#   * each day strands the health verdict in a DIFFERENT state -- a call with
#     no prompt accounting, a call on a model with no documented window, a call
#     measuring past 100% of its own window -- because all three otherwise sit
#     under one reassuring "nothing is broken";
#   * one day makes the SUBAGENTS the worst-saturated scope, so a `worst_scope`
#     hard-coded to the main thread is red;
#   * one day gives every main-thread call the SAME timestamp, so a period with
#     no span cannot be quartered and must say so rather than divide by zero.
HG_SESSION = "health-growth-fixture"
HG_AGENT = "agent-hg64a"
HG_OPUS_1M = "claude-opus-5-20260101"
HG_HAIKU_200K = "claude-haiku-4-5-20251001"
HG_UNKNOWN = "claude-nosuchtier-9-20260101"

HG_CLEAN_DAY = "2026-07-01"       # every check passes; the growth curve's day
HG_SUB_HEAVY_DAY = "2026-07-02"   # the SUBAGENTS are the saturated scope
HG_BLIND_DAY = "2026-07-03"       # a call carrying no prompt accounting
HG_UNKNOWN_DAY = "2026-07-04"     # a call on a model with no documented window
HG_OVER_DAY = "2026-07-05"        # a call measuring past its own window
HG_INSTANT_DAY = "2026-07-06"     # every main-thread call at one instant
HG_FALLING_DAY = "2026-07-07"     # the typical reply SHRANK across the day
HG_SAWTOOTH_DAY = "2026-07-08"    # it climbed, then dropped by two thirds
HG_LATE_DAY = "2026-07-20"        # far enough out to leave a quarter empty

HG_MAIN = SOURCE_MAIN
HG_SUB = SOURCE_SUBAGENT

# The clean day's main thread, in TIME order: four quarters of four, rising.
# Within each quarter the order is smallest, largest, then the two middles, so
# the median is neither the first nor the last call of its quarter.
HG_RISING = [
    100_000, 130_000, 110_000, 120_000,
    300_000, 330_000, 310_000, 320_000,
    500_000, 530_000, 510_000, 520_000,
    900_000, 930_000, 910_000, 920_000,
]
# Hand-written, then checked against the list above: `nearest_rank` at p50 over
# four values takes index 1, i.e. the SECOND SMALLEST.
HG_QUARTER_MEDIANS = [110_000, 310_000, 510_000, 910_000]
HG_QUARTER_UTILISATIONS = [0.11, 0.31, 0.51, 0.91]
# The subagents over the same minutes, FALLING, and never above a quarter of
# the window -- so a pooled or mis-scoped curve is red in shape as well as in
# value.
HG_FALLING = [240_000 - 10_000 * n for n in range(16)]
# A main thread whose typical reply SHRANK, so a shape verdict hard-coded to
# "rising" -- the sentence this panel shipped with in review -- is red.
HG_SHRINKING = [900_000 - 50_000 * n for n in range(16)]
# THE SHAPE THE AUTHORED HEADING GOT WRONG, in the proportions the coordinator
# measured on the real database (277,945 -> 837,645 -> 297,343 -> 430,832): it
# climbs, drops by roughly two thirds, and rises again. A session that
# compacted. Four per quarter, the quarter's own four ordered so the median is
# neither its first nor its last call.
HG_SAWTOOTH = [
    270_000, 300_000, 280_000, 290_000,
    800_000, 860_000, 830_000, 840_000,
    290_000, 320_000, 300_000, 310_000,
    420_000, 450_000, 430_000, 440_000,
]

# (day, minute, second, kind, model, context)
HG_CALLS: list[tuple[str, int, int, str, str, int]] = [
    *[
        (HG_CLEAN_DAY, n, 0, HG_MAIN, HG_OPUS_1M, size)
        for n, size in enumerate(HG_RISING)
    ],
    *[
        (HG_CLEAN_DAY, n, 30, HG_SUB, HG_OPUS_1M, size)
        for n, size in enumerate(HG_FALLING)
    ],
    # The subagents saturated and the main thread idle: the ranking must follow
    # the measurement, not the scope's name.
    *[(HG_SUB_HEAVY_DAY, n, 0, HG_MAIN, HG_OPUS_1M, 100_000) for n in range(4)],
    *[(HG_SUB_HEAVY_DAY, n, 30, HG_SUB, HG_OPUS_1M, 950_000) for n in range(4)],
    # One measured call and one carrying no prompt accounting at all.
    (HG_BLIND_DAY, 0, 0, HG_MAIN, HG_OPUS_1M, 300_000),
    (HG_BLIND_DAY, 1, 0, HG_MAIN, HG_OPUS_1M, 0),
    # A model this build has no documented window for.
    (HG_UNKNOWN_DAY, 0, 0, HG_MAIN, HG_UNKNOWN, 400_000),
    # 150% of a 200k window: impossible, therefore a stale window table.
    (HG_OVER_DAY, 0, 0, HG_MAIN, HG_HAIKU_200K, 300_000),
    # Twelve calls -- past the floor -- sharing one instant, so the period they
    # span is a point.
    *[(HG_INSTANT_DAY, 0, 0, HG_MAIN, HG_OPUS_1M, 200_000 + n) for n in range(12)],
    # A day whose typical reply shrank, and a day it climbed then dropped.
    *[
        (HG_FALLING_DAY, n, 0, HG_MAIN, HG_OPUS_1M, size)
        for n, size in enumerate(HG_SHRINKING)
    ],
    *[
        (HG_SAWTOOTH_DAY, n, 0, HG_MAIN, HG_OPUS_1M, size)
        for n, size in enumerate(HG_SAWTOOTH)
    ],
    # The far end of the wide window: eight banded calls and four whose model
    # has no window, so the quarter's two medians range over two sets.
    *[(HG_LATE_DAY, n, 0, HG_MAIN, HG_OPUS_1M, 600_000 + 1_000 * n) for n in range(8)],
    *[(HG_LATE_DAY, 8 + n, 0, HG_MAIN, HG_UNKNOWN, 700_000) for n in range(4)],
]


def build_health_growth_corpus(root: Path) -> Path:
    """The corpus above, written the way Claude Code writes transcripts."""
    project = root / "projects" / "-fixture-health-growth"
    project.mkdir(parents=True)
    subagents = project / HG_SESSION / "subagents"
    subagents.mkdir(parents=True)

    def record(
        n: int, day: str, minute: int, second: int, kind: str, model: str, context: int
    ) -> str:
        if context:
            # Three deliberately unequal classes summing to the target context,
            # so a swapped column mapping cannot reproduce it.
            usage = {
                "input_tokens": 1_000,
                "cache_creation_input_tokens": 2_000,
                "cache_read_input_tokens": context - 3_000,
                "output_tokens": n + 1,
            }
        else:
            # The #25 population: the four keys PRESENT and valued zero.
            usage = {
                "input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 0,
            }
        payload: dict[str, object] = {
            "type": "assistant",
            "sessionId": HG_SESSION,
            "timestamp": f"{day}T15:{minute:02d}:{second:02d}.000Z",
            "isSidechain": kind == SOURCE_SUBAGENT,
            "message": {
                "id": f"msg-hg64-{n}",
                "model": model,
                "usage": usage,
                "content": [{"type": "text", "text": f"hg64 call {n}"}],
            },
        }
        if kind == SOURCE_SUBAGENT:
            payload["agentId"] = HG_AGENT
        return json.dumps(payload) + "\n"

    main_lines: list[str] = []
    sub_lines: list[str] = []
    for n, (day, minute, second, kind, model, context) in enumerate(HG_CALLS):
        line = record(n, day, minute, second, kind, model, context)
        (sub_lines if kind == SOURCE_SUBAGENT else main_lines).append(line)
    (project / f"{HG_SESSION}.jsonl").write_text("".join(main_lines))
    (subagents / f"{HG_AGENT}.jsonl").write_text("".join(sub_lines))
    return project


class HealthGrowthCorpusTest(unittest.TestCase):
    """Shared fixture for the two blocks #64 and #65 added.

    Every variant state is produced by COPYING the ingested database and
    mutating the copy, so one ingest serves them all and no test can leave a
    state behind for the next one.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="usage-report-health-growth-test-"))
        projects = build_health_growth_corpus(cls.tmp)
        cls.db = cls.tmp / "usage.db"
        ingest(projects, cls.db, tasks_dir=cls.tmp / "no-task-index")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def api(self, mutate=None) -> Api:
        """An `Api` over a private COPY of the corpus, optionally mutated."""
        copy = self.tmp / f"copy-{self.id().rsplit('.', 1)[-1]}-{id(mutate)}.db"
        shutil.copy(self.db, copy)
        if mutate is not None:
            conn = sqlite3.connect(copy)
            mutate(conn)
            conn.commit()
            conn.close()
        api = Api(copy)
        self.addCleanup(api.conn.close)
        return api

    def summary(self, day: str | None = None, to: str | None = None, mutate=None):
        return self.api(mutate).summary(*day_bounds(day, to if to else day))

    def health(self, day: str | None = None, mutate=None) -> dict:
        return self.summary(day, mutate=mutate)["health"]

    @staticmethod
    def check(health: dict, name: str) -> dict:
        found = [c for c in health["checks"] if c["check"] == name]
        assert len(found) == 1, f"{name} is not reported exactly once"
        return found[0]


class HealthVerdictTest(HealthGrowthCorpusTest):
    """#64: the report states whether anything is broken, and CAN say it is not.

    The page reported "8,163 records parsed, 0 unparsed" and then stopped. A
    reader scanning for alarm had to infer reassurance from the ABSENCE OF A
    WARNING -- the exact inference this repository refuses to let a number make.

    The half that makes the issue worth doing is the THIRD state. "Nothing is
    broken" and "we could not check" have different remedies, and a corpus with
    no `source_shape` rows has not been censused rather than been found clean.
    A two-state verdict would report health over a corpus nobody has looked at.
    """

    # --- the reassurance is a real measurement ---

    def test_a_clean_window_states_that_nothing_is_broken(self) -> None:
        health = self.health(HG_CLEAN_DAY)
        self.assertEqual(health["verdict"], HEALTH_OK)
        self.assertEqual(health["statement"], HEALTH_STATEMENTS[HEALTH_OK])
        self.assertEqual(
            [c["state"] for c in health["checks"]],
            [HEALTH_OK] * len(HEALTH_CHECKS),
            "the clean day is not clean, so nothing below distinguishes a "
            "passing check from a missing one",
        )

    def test_every_declared_check_is_computed_and_reported_in_order(self) -> None:
        # `HEALTH_CHECKS` and `_health()` are two enumerations of one set, and
        # nothing but this ties them: a check declared and computed nowhere
        # would simply never be asked, and one computed and undeclared would
        # appear in a list nothing describes.
        health = self.health(HG_CLEAN_DAY)
        self.assertEqual([c["check"] for c in health["checks"]], list(HEALTH_CHECKS))

    def test_each_check_carries_every_field(self) -> None:
        for check in self.health(HG_CLEAN_DAY)["checks"]:
            with self.subTest(check=check["check"]):
                self.assertEqual(
                    set(check), {"check", "state", "statement", "count", "of"}
                )
                self.assertIn(check["state"], HEALTH_ORDER)
                self.assertGreater(len(check["statement"]), 40)

    # --- "nothing is broken" is distinguishable from "we could not check" ---

    def test_an_uncensused_corpus_is_not_reported_clean(self) -> None:
        # THE test this issue exists for. Every other check passes over this
        # database; the one thing that would notice Claude Code renaming a
        # token key has never run over it. A row in `source_shape` is a
        # POSITIVE observation, so no rows means uncensused -- and a verdict
        # that read that as health would be `source_shape`'s own rule failing
        # at the last inch.
        health = self.health(
            HG_CLEAN_DAY, mutate=lambda c: c.execute("DELETE FROM source_shape")
        )
        self.assertEqual(health["verdict"], HEALTH_UNCHECKED)
        self.assertNotEqual(health["verdict"], HEALTH_OK)
        census = self.check(health, CHECK_FORMAT_CENSUS)
        self.assertEqual(census["state"], HEALTH_UNCHECKED)
        self.assertEqual(census["count"], 0)
        self.assertGreater(census["of"], 0)

    def test_a_partly_censused_corpus_is_unchecked_too(self) -> None:
        # The v8-upgrade state: some sources were censused and the unchanged
        # ones will be when they next change. "Most of it was looked at" is not
        # "it was looked at".
        health = self.health(
            HG_CLEAN_DAY,
            mutate=lambda c: c.execute(
                "DELETE FROM source_shape WHERE path = "
                "(SELECT MIN(path) FROM source_shape)"
            ),
        )
        census = self.check(health, CHECK_FORMAT_CENSUS)
        self.assertEqual(census["state"], HEALTH_UNCHECKED)
        self.assertLess(census["count"], census["of"])
        self.assertEqual(health["verdict"], HEALTH_UNCHECKED)

    def test_a_database_that_cannot_census_at_all_says_so(self) -> None:
        # `serve.py` never migrates, so a pre-v9 database simply has no table.
        # "This build cannot ask the question" must not read as an answer.
        health = self.health(
            HG_CLEAN_DAY, mutate=lambda c: c.execute("DROP TABLE source_shape")
        )
        census = self.check(health, CHECK_FORMAT_CENSUS)
        self.assertEqual(census["state"], HEALTH_UNCHECKED)
        self.assertIsNone(census["count"])
        self.assertIsNone(census["of"])
        self.assertEqual(health["verdict"], HEALTH_UNCHECKED)

    def test_the_two_decided_verdicts_lead_with_their_answer(self) -> None:
        # #88: the card's heading is the question "is anything blowing up?",
        # and a first line that opens with the evidence has made the reader
        # derive the answer from it. `ok` has said "No." since #64; the failed
        # one led with the evidence, which matters more now that `failed` is
        # the ONE verdict that expands its own six lines. `unchecked` is not
        # here because its answer is neither: it leads by saying so.
        self.assertTrue(HEALTH_STATEMENTS[HEALTH_OK].startswith("No."))
        self.assertTrue(HEALTH_STATEMENTS[HEALTH_FAILED].startswith("Yes."))
        self.assertNotIn(
            HEALTH_STATEMENTS[HEALTH_UNCHECKED].split()[0],
            ("Yes.", "No."),
            "the unchecked verdict answers a question it cannot answer",
        )

    def test_the_three_verdict_statements_are_three_different_claims(self) -> None:
        # Rendered identically, the states would be a distinction the payload
        # made and the reader could not see.
        self.assertEqual(len(set(HEALTH_STATEMENTS.values())), 3)
        self.assertEqual(set(HEALTH_STATEMENTS), set(HEALTH_ORDER))
        self.assertNotIn(
            HEALTH_STATEMENTS[HEALTH_OK],
            HEALTH_STATEMENTS[HEALTH_UNCHECKED],
            "the unchecked verdict quotes the clean one",
        )

    # --- a failure outranks the reassurance ---

    def test_an_unparsed_record_fails_the_verdict(self) -> None:
        health = self.health(
            HG_CLEAN_DAY,
            mutate=lambda c: c.execute(
                "UPDATE ingest_state SET unparsed_records = 3"
                " WHERE path = (SELECT MIN(path) FROM ingest_state)"
            ),
        )
        self.assertEqual(health["verdict"], HEALTH_FAILED)
        parsed = self.check(health, CHECK_RECORDS_PARSED)
        self.assertEqual(parsed["state"], HEALTH_FAILED)
        self.assertEqual(parsed["count"], 3)

    def test_a_failure_is_never_softened_by_the_checks_that_pass(self) -> None:
        # Five checks pass and one fails: the verdict is the failure, and the
        # passing five are still SHOWN -- an `ok` is a positive statement, and
        # hiding them would make "healthy" and "not asked" render alike.
        health = self.health(
            HG_CLEAN_DAY,
            mutate=lambda c: c.execute(
                "UPDATE ingest_state SET unparsed_records = 1"
                " WHERE path = (SELECT MIN(path) FROM ingest_state)"
            ),
        )
        self.assertEqual(health["verdict"], HEALTH_FAILED)
        self.assertEqual(
            sum(1 for c in health["checks"] if c["state"] == HEALTH_OK),
            len(HEALTH_CHECKS) - 1,
        )

    def test_a_failure_outranks_an_unchecked_as_well_as_an_ok(self) -> None:
        # Both other states present at once. `unchecked` is the milder true
        # statement, and a reader shown it while a failure went unmentioned
        # would have been handed the milder fact -- `BannerPrecedenceTest`'s
        # defect, one layer up.
        def mutate(conn):
            conn.execute("DELETE FROM source_shape")
            conn.execute(
                "UPDATE ingest_state SET unparsed_records = 2"
                " WHERE path = (SELECT MIN(path) FROM ingest_state)"
            )

        health = self.health(HG_CLEAN_DAY, mutate=mutate)
        states = {c["state"] for c in health["checks"]}
        self.assertEqual(states, {HEALTH_OK, HEALTH_UNCHECKED, HEALTH_FAILED})
        self.assertEqual(health["verdict"], HEALTH_FAILED)

    def test_the_precedence_is_failed_then_unchecked_then_ok(self) -> None:
        self.assertEqual(HEALTH_ORDER, (HEALTH_FAILED, HEALTH_UNCHECKED, HEALTH_OK))

    def test_a_reply_past_its_own_window_is_a_failure_not_a_caveat(self) -> None:
        # A call cannot exceed the window it had, so a measurement that says one
        # did means this build's window table has gone stale. That is the loud
        # half of `context_window.py`'s safety story, and it is only a safety
        # story if a verdict states it.
        health = self.health(HG_OVER_DAY)
        within = self.check(health, CHECK_WITHIN_WINDOW)
        self.assertEqual(within["state"], HEALTH_FAILED)
        self.assertEqual(within["count"], 1)
        self.assertEqual(health["verdict"], HEALTH_FAILED)

    # --- the unchecked states, each on its own day ---

    def test_an_unknown_model_leaves_the_window_check_unmade(self) -> None:
        health = self.health(HG_UNKNOWN_DAY)
        known = self.check(health, CHECK_MODEL_WINDOW_KNOWN)
        self.assertEqual(known["state"], HEALTH_UNCHECKED)
        self.assertEqual(known["count"], 1)
        self.assertEqual(health["verdict"], HEALTH_UNCHECKED)

    def test_a_call_with_no_prompt_accounting_leaves_context_unchecked(self) -> None:
        health = self.health(HG_BLIND_DAY)
        measured = self.check(health, CHECK_CONTEXT_MEASURED)
        self.assertEqual(measured["state"], HEALTH_UNCHECKED)
        self.assertEqual(measured["count"], 1)
        self.assertEqual(measured["of"], 2)
        self.assertEqual(health["verdict"], HEALTH_UNCHECKED)

    def test_an_empty_window_is_unchecked_rather_than_clean(self) -> None:
        # A window holding no call has nothing wrong with it and nothing right
        # either. Reported `ok`, it would tell a reader who narrowed the dates
        # past every call that their corpus is healthy.
        health = self.health("2026-07-10")
        for name in (
            CHECK_MODEL_WINDOW_KNOWN, CHECK_WITHIN_WINDOW, CHECK_CONTEXT_MEASURED,
        ):
            with self.subTest(check=name):
                self.assertEqual(self.check(health, name)["state"], HEALTH_UNCHECKED)
        self.assertEqual(health["verdict"], HEALTH_UNCHECKED)

    # --- staleness cannot be lowered by anything here (PR #60) ---

    def test_a_stale_database_fails_and_the_verdict_cannot_lower_it(self) -> None:
        health = self.health(
            HG_CLEAN_DAY,
            mutate=lambda c: c.execute(
                f"UPDATE {INGEST_RUNS_TABLE} SET finished_at = ?",
                (time.time() - STALE_AFTER_SECONDS * 10,),
            ),
        )
        age = self.check(health, CHECK_INGEST_AGE)
        self.assertEqual(age["state"], HEALTH_FAILED)
        self.assertEqual(health["verdict"], HEALTH_FAILED)

    def test_an_unknown_age_is_unchecked_and_never_fresh(self) -> None:
        health = self.health(
            HG_CLEAN_DAY,
            mutate=lambda c: c.execute(f"DELETE FROM {INGEST_RUNS_TABLE}"),
        )
        age = self.check(health, CHECK_INGEST_AGE)
        self.assertEqual(age["state"], HEALTH_UNCHECKED)
        self.assertIn(STALE_UNKNOWN_NO_RUN_RECORDED, age["statement"])
        self.assertEqual(health["verdict"], HEALTH_UNCHECKED)

    def test_the_age_check_reads_the_tri_state_rather_than_the_clock(self) -> None:
        # It maps `ingest.stale` and does no comparison of its own, which is
        # what makes "this verdict cannot lower a staleness warning" a
        # structural property rather than a promise: there is no arithmetic
        # here to get the direction wrong.
        for stale, expected in (
            (True, HEALTH_FAILED), (False, HEALTH_OK), (None, HEALTH_UNCHECKED),
        ):
            with self.subTest(stale=stale):
                self.assertEqual(
                    Api._check_ingest_age(
                        {"stale": stale, "stale_unknown_reason": "why"}
                    )["state"],
                    expected,
                )

    # --- the verdict and the figures it qualifies are ONE reading ---

    def test_every_count_is_the_figure_the_rest_of_the_payload_carries(self) -> None:
        # `_health()` takes `ingest` and `context` as ARGUMENTS rather than
        # re-querying, so the verdict at the top of the page and the numbers
        # below it cannot disagree. This is the tie that says so -- the same
        # discipline `RANKED_BY` is for a heading and its ORDER BY.
        payload = self.summary(HG_BLIND_DAY)
        health, context = payload["health"], payload["context"]
        util = context["utilisation"]
        for name, count, of in (
            (CHECK_RECORDS_PARSED, payload["ingest"]["unparsed_records"], None),
            (CHECK_MODEL_WINDOW_KNOWN, util["unknown_model_calls"],
             context["sample_calls"]),
            (CHECK_WITHIN_WINDOW, util["over_window_calls"], util["banded_calls"]),
            (CHECK_CONTEXT_MEASURED, context["unmeasured_calls"], util["calls"]),
        ):
            with self.subTest(check=name):
                check = self.check(health, name)
                self.assertEqual(check["count"], count)
                self.assertEqual(check["of"], of)

    def test_a_total_the_database_does_not_hold_is_null_and_not_zero(self) -> None:
        # `ingest_state` records how many records FAILED to parse and not how
        # many were read, so `records-parsed` has no denominator. Reported as 0
        # it would say every record failed; invented, it would be exactly the
        # defect this whole block exists to report.
        parsed = self.check(self.health(HG_CLEAN_DAY), CHECK_RECORDS_PARSED)
        self.assertIsNone(parsed["of"])
        self.assertEqual(parsed["count"], 0)

    def test_an_empty_ledger_is_unchecked_rather_than_a_clean_zero(self) -> None:
        health = self.health(
            HG_CLEAN_DAY, mutate=lambda c: c.execute("DELETE FROM ingest_state")
        )
        parsed = self.check(health, CHECK_RECORDS_PARSED)
        self.assertEqual(parsed["state"], HEALTH_UNCHECKED)
        self.assertNotEqual(parsed["state"], HEALTH_OK)


class SaturationRankingTest(HealthGrowthCorpusTest):
    """#65(a): which scope is the problem, named -- and the key it is named by.

    A ranking must name the key it orders by and the name must BE the key
    (`RANKED_BY`'s rule). The share is published per scope, the ranking
    maximises that same field, and the phrase crosses the API beside the winner.
    """

    def scope(self, day: str, name: str) -> dict:
        util = self.summary(day)["context"]["utilisation"]
        found = [s for s in util["by_scope"] if s["scope"] == name]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_each_scope_publishes_the_share_the_ranking_orders_by(self) -> None:
        # 8 of 16 main-thread calls at or above half the window; 0 of 16
        # subagent ones. Deliberately unequal, so a scope reading the other's
        # tally is red.
        self.assertEqual(
            self.scope(HG_CLEAN_DAY, SCOPE_MAIN)["over_half_window_calls"], 8
        )
        self.assertEqual(
            self.scope(HG_CLEAN_DAY, SCOPE_MAIN)["over_half_window_share"], 0.5
        )
        self.assertEqual(
            self.scope(HG_CLEAN_DAY, SCOPE_SUBAGENT)["over_half_window_calls"], 0
        )
        self.assertEqual(
            self.scope(HG_CLEAN_DAY, SCOPE_SUBAGENT)["over_half_window_share"], 0.0
        )

    def test_a_scope_with_no_banded_sample_has_no_share_rather_than_zero(self) -> None:
        # A share of an empty set is not 0%, and 0.0 here would rank a scope
        # that measured nothing as the most frugal one.
        sub = self.scope(HG_UNKNOWN_DAY, SCOPE_SUBAGENT)
        self.assertEqual(sub["banded_calls"], 0)
        self.assertIsNone(sub["over_half_window_share"])
        self.assertEqual(sub["over_half_window_calls"], 0)

    def test_the_worst_scope_is_the_one_the_measurement_names(self) -> None:
        util = self.summary(HG_CLEAN_DAY)["context"]["utilisation"]
        self.assertEqual(util["worst_scope"], SCOPE_MAIN)
        self.assertEqual(util["worst_scope_ranked_by"], SATURATION_RANKED_BY)

    def test_the_ranking_follows_the_figure_and_not_the_scope_name(self) -> None:
        # THE teeth: on this day every saturated call is a SUBAGENT's. A
        # `worst_scope` hard-coded to the main thread, or one ranked on call
        # counts rather than on the published share, is red here and nowhere
        # else.
        util = self.summary(HG_SUB_HEAVY_DAY)["context"]["utilisation"]
        self.assertEqual(util["worst_scope"], SCOPE_SUBAGENT)
        self.assertEqual(
            self.scope(HG_SUB_HEAVY_DAY, SCOPE_SUBAGENT)["over_half_window_share"], 1.0
        )
        self.assertEqual(
            self.scope(HG_SUB_HEAVY_DAY, SCOPE_MAIN)["over_half_window_share"], 0.0
        )

    def test_a_window_no_scope_banded_names_no_worst_scope(self) -> None:
        # None rather than a scope that measured nothing: "the worst scope is
        # the one we never looked at" is not a ranking.
        util = self.summary("2026-07-10")["context"]["utilisation"]
        self.assertIsNone(util["worst_scope"])

    def test_a_measured_zero_still_ranks(self) -> None:
        # 0.0 is a real reading and must not be treated as an absence: "the
        # pressure is in your main session, and it is currently none" is a true
        # and useful sentence.
        util = self.summary(HG_BLIND_DAY)["context"]["utilisation"]
        self.assertEqual(util["worst_scope"], SCOPE_MAIN)

    def test_the_recommendation_reads_the_published_share(self) -> None:
        # One definition of "over half the window", not two. The metric that
        # drives the advice band and the share the ranking uses are now the
        # same field; a second summation in `_recommendations()` would be free
        # to drift from the number the reader is looking at.
        payload = self.summary(HG_CLEAN_DAY)
        ranked = payload["recommendations"]["ranked"]
        found = [
            a for a in ranked
            if a["metric"] == METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW
        ]
        self.assertEqual(len(found), 1)
        self.assertEqual(
            found[0]["value"],
            self.scope(HG_CLEAN_DAY, SCOPE_MAIN)["over_half_window_share"],
        )


class ContextAnswerTest(HealthGrowthCorpusTest):
    """#88: question 2 answers its own heading, and the answer is a measurement.

    The card asked "am I wasting context?" and replied "Most of it, of the
    scopes measured, is in your main-thread" -- a LOCATION. A reader wanting
    yes or no got neither, and "most of it" had no antecedent on the resting
    page, because the meters that would give "it" a referent are one expansion
    down.

    The wording before that one ("the pressure is in your X") was worse: it
    asserts that there IS pressure, which is false of a healthy corpus and
    would be the page inventing a finding. The fix for both is the same -- the
    sentence is CONDITIONAL ON THE MEASUREMENT, and it is decided here, beside
    the tallies it is decided from, never written into a template. This branch
    already shipped one authored heading ("And it only ever grows.") that the
    same database contradicted three hours later.
    """

    def answer(self, day: str | None = None, to: str | None = None) -> dict:
        return self.summary(day, to)["context"]["utilisation"]["answer"]

    def util(self, day: str | None = None) -> dict:
        return self.summary(day)["context"]["utilisation"]

    # --- the two answers the question actually has ---

    def test_a_saturated_period_answers_yes(self) -> None:
        # 8 of 16 main-thread calls at or above half a 1M window. "Yes" is the
        # answer; the scope is where to go and look.
        answer = self.answer(HG_CLEAN_DAY)
        self.assertEqual(answer["verdict"], CONTEXT_ANSWER_YES)
        self.assertEqual(
            answer["statement"], CONTEXT_ANSWER_STATEMENTS[CONTEXT_ANSWER_YES]
        )
        self.assertEqual(self.util(HG_CLEAN_DAY)["worst_scope"], SCOPE_MAIN)

    def test_a_quiet_period_answers_no_as_a_finding(self) -> None:
        # THE half a softer wording cannot state. Twelve measured main-thread
        # calls, every one banded, none at or above the boundary, nothing
        # unmeasured, unwindowed or over its window: that is a complete clean
        # sample, and a clean corpus deserves an explicit no exactly as an `ok`
        # health verdict is a positive statement rather than an absence.
        answer = self.answer(HG_INSTANT_DAY)
        self.assertEqual(answer["verdict"], CONTEXT_ANSWER_NO)
        self.assertEqual(
            answer["statement"], CONTEXT_ANSWER_STATEMENTS[CONTEXT_ANSWER_NO]
        )
        util = self.util(HG_INSTANT_DAY)
        self.assertEqual(util["over_half_window_calls"], 0)
        self.assertEqual(util["unmeasured_calls"], 0)
        self.assertEqual(util["unknown_model_calls"], 0)
        self.assertEqual(util["over_window_calls"], 0)
        self.assertGreater(util["banded_calls"], 0)

    def test_the_answer_follows_the_measurement_and_not_the_scope_name(self) -> None:
        # THE teeth on the answer itself: on this day every saturated call is a
        # SUBAGENT's. An answer hard-coded to the main thread, or one that
        # reported the pooled share as the finding, is red here and nowhere
        # else -- the pooled share is where #61's 6:1 dilution lives.
        self.assertEqual(self.answer(HG_SUB_HEAVY_DAY)["verdict"], CONTEXT_ANSWER_YES)
        self.assertEqual(self.util(HG_SUB_HEAVY_DAY)["worst_scope"], SCOPE_SUBAGENT)

    # --- the three states that are neither yes nor no ---

    def test_a_period_that_could_not_be_fully_read_is_not_a_clean_no(self) -> None:
        # One measured call at 30% of its window and one carrying no prompt
        # accounting at all. Nothing reached the boundary, so the tempting
        # answer is "no" -- and it would be the milder of two true statements,
        # which is the defect this repository keeps finding. The unmeasured
        # call is UNKNOWN, not low.
        answer = self.answer(HG_BLIND_DAY)
        self.assertEqual(answer["verdict"], CONTEXT_ANSWER_INCONCLUSIVE)
        self.assertNotEqual(answer["verdict"], CONTEXT_ANSWER_NO)
        self.assertEqual(self.util(HG_BLIND_DAY)["unmeasured_calls"], 1)

    def test_a_measured_period_with_nothing_bandable_is_unknown(self) -> None:
        # A call on a model this build has no documented window for. Its
        # context WAS measured, so this is not "no sample"; there is simply
        # nothing to compare it with -- unknown, not none, and not a quiet no.
        answer = self.answer(HG_UNKNOWN_DAY)
        self.assertEqual(answer["verdict"], CONTEXT_ANSWER_UNKNOWN)
        util = self.util(HG_UNKNOWN_DAY)
        self.assertIsNone(util["worst_scope"])
        self.assertEqual(util["unknown_model_calls"], 1)
        self.assertGreater(self.summary(HG_UNKNOWN_DAY)["context"]["sample_calls"], 0)

    def test_a_period_with_no_measured_context_says_no_sample(self) -> None:
        # Distinct from `unknown`, and the distinction is the remedy: there is
        # nothing to band because there is nothing measured, not because
        # nothing could be compared.
        answer = self.answer("2026-07-10")
        self.assertEqual(answer["verdict"], CONTEXT_ANSWER_NO_SAMPLE)
        self.assertEqual(self.summary("2026-07-10")["context"]["sample_calls"], 0)

    # --- an unknown may weaken a `no`, and never a `yes` ---

    def test_an_unwindowed_call_does_not_soften_a_proven_yes(self) -> None:
        # Eight banded main-thread calls at 1.0 of the boundary, beside four on
        # a model with no documented window. The four are a real unknown and
        # they are counted -- but a proven saturation is not downgraded to
        # "inconclusive" by the calls that could not be measured next to it.
        answer = self.answer(HG_LATE_DAY)
        self.assertEqual(answer["verdict"], CONTEXT_ANSWER_YES)
        self.assertEqual(self.util(HG_LATE_DAY)["unknown_model_calls"], 4)

    def test_a_call_past_its_own_window_still_answers_yes(self) -> None:
        # 150% of a 200k window: the call IS at or above the boundary, so the
        # answer to this question is yes. That the window table is stale is a
        # different claim with a different remedy, and question 1 states it as
        # the FAILURE it is (`test_a_reply_past_its_own_window_is_a_failure`).
        self.assertEqual(self.answer(HG_OVER_DAY)["verdict"], CONTEXT_ANSWER_YES)
        self.assertEqual(self.util(HG_OVER_DAY)["over_window_calls"], 1)

    # --- the answer, the ranking and the meters are one reading ---

    def test_the_answer_states_the_winners_figure_and_not_the_pooled_one(self) -> None:
        # THE teeth on the two figures beside the answer, and they need a
        # window where the two scopes DISAGREE: over 1 and 2 July the main
        # thread is 8 of 20 banded (0.4) and the subagents 4 of 20 (0.2), so
        # the pooled tally is 12 of 40 (0.3) and every one of the three numbers
        # is different. A card that reached for the pooled count -- the easy
        # field, sitting one line up in the same payload -- would state 12 and
        # 30% under a sentence naming the main thread, which is #61's dilution
        # defect wearing the answer's clothes.
        util = self.summary(HG_CLEAN_DAY, to=HG_SUB_HEAVY_DAY)["context"][
            "utilisation"
        ]
        self.assertEqual(util["worst_scope"], SCOPE_MAIN)
        self.assertEqual(util["worst_scope_over_half_window_calls"], 8)
        self.assertEqual(util["worst_scope_over_half_window_share"], 0.4)
        self.assertEqual(util["over_half_window_calls"], 12)
        self.assertNotEqual(
            util["worst_scope_over_half_window_calls"], util["over_half_window_calls"]
        )
        self.assertNotEqual(
            util["worst_scope_over_half_window_share"], util["over_half_window_share"]
        )

    def test_the_winners_figures_are_the_winning_rows_own(self) -> None:
        # `worst_scope_over_half_window_*` is what the resting card states, and
        # `by_scope` is what the meter one click down draws. Derived twice they
        # would be two figures free to disagree, with the reader unable to see
        # it because only one of them is on screen.
        for day in (HG_CLEAN_DAY, HG_SUB_HEAVY_DAY, HG_INSTANT_DAY):
            with self.subTest(day=day):
                util = self.util(day)
                winner = [
                    s for s in util["by_scope"] if s["scope"] == util["worst_scope"]
                ]
                self.assertEqual(len(winner), 1)
                self.assertEqual(
                    util["worst_scope_over_half_window_calls"],
                    winner[0]["over_half_window_calls"],
                )
                self.assertEqual(
                    util["worst_scope_over_half_window_share"],
                    winner[0]["over_half_window_share"],
                )

    def test_a_yes_always_names_a_scope_with_a_count_behind_it(self) -> None:
        # The property the card's sentence depends on: "yes, and it is your X"
        # must never name a scope whose own count is zero or absent.
        for day in (HG_CLEAN_DAY, HG_SUB_HEAVY_DAY, HG_OVER_DAY, HG_LATE_DAY):
            with self.subTest(day=day):
                util = self.util(day)
                self.assertEqual(util["answer"]["verdict"], CONTEXT_ANSWER_YES)
                self.assertIsNotNone(util["worst_scope"])
                self.assertGreater(util["worst_scope_over_half_window_calls"], 0)

    def test_a_scope_that_ranked_nothing_has_no_figures_rather_than_zeroes(
        self,
    ) -> None:
        # A share of an empty set is not 0%, and a count of an absent winner is
        # not 0 either: rendered as zeroes they would report the most frugal
        # possible reading over a period nobody could measure.
        for day in (HG_UNKNOWN_DAY, "2026-07-10"):
            with self.subTest(day=day):
                util = self.util(day)
                self.assertIsNone(util["worst_scope"])
                self.assertIsNone(util["worst_scope_over_half_window_calls"])
                self.assertIsNone(util["worst_scope_over_half_window_share"])

    # --- the states are enumerated, and each is a different claim ---

    def test_every_state_is_declared_and_carries_its_own_sentence(self) -> None:
        # `CONTEXT_ANSWER_STATES` and `CONTEXT_ANSWER_STATEMENTS` are two
        # enumerations of one set, and nothing but this ties them: a state with
        # no sentence would raise on the day it first occurred, and a sentence
        # for a state nothing produces is prose no reader will ever see.
        self.assertEqual(set(CONTEXT_ANSWER_STATES), set(CONTEXT_ANSWER_STATEMENTS))
        self.assertEqual(
            len(set(CONTEXT_ANSWER_STATEMENTS.values())),
            len(CONTEXT_ANSWER_STATES),
            "two states are stated in the same words, so the payload makes a "
            "distinction the reader cannot see",
        )
        for state, statement in CONTEXT_ANSWER_STATEMENTS.items():
            with self.subTest(state=state):
                self.assertGreater(len(statement), 40)

    def test_each_sentence_leads_with_the_answer_to_the_question(self) -> None:
        # The heading is a yes/no question, so the first WORD of the reply is
        # the reply. A sentence that opens with its evidence has made the
        # reader derive the answer from it, which is what the location wording
        # did for the whole of this card's life.
        for state, lead in (
            (CONTEXT_ANSWER_YES, "Yes."),
            (CONTEXT_ANSWER_NO, "No."),
            (CONTEXT_ANSWER_INCONCLUSIVE, "Not established."),
            (CONTEXT_ANSWER_UNKNOWN, "Unknown."),
            (CONTEXT_ANSWER_NO_SAMPLE, "No sample."),
        ):
            with self.subTest(state=state):
                self.assertTrue(
                    CONTEXT_ANSWER_STATEMENTS[state].startswith(lead),
                    f"the {state} answer does not lead with its answer",
                )

    def test_every_state_the_corpus_can_reach_is_reached_by_a_test_above(
        self,
    ) -> None:
        # The enumeration is only a claim about completeness if something
        # produces every member of it. Each of the five is asserted on a day of
        # its own above; this is the tie that says the five are all of them.
        seen = {
            self.answer(day)["verdict"]
            for day in (
                HG_CLEAN_DAY, HG_INSTANT_DAY, HG_BLIND_DAY, HG_UNKNOWN_DAY,
                "2026-07-10",
            )
        }
        self.assertEqual(seen, set(CONTEXT_ANSWER_STATES))


class GrowthCurveTest(HealthGrowthCorpusTest):
    """#65(b): the main session's context across four quarters of its own life.

    Measured 2026-08-05 over this project's own transcripts: 97,436 -> 333,610
    -> 514,413 -> 906,301, the last of them 90.6% of the window. "Your context
    is large" and "your context only ever grows" are different findings, and
    the report showed only the first.
    """

    def growth(self, day: str | None = None, to: str | None = None) -> dict:
        return self.summary(day, to)["context"]["growth"]

    # --- the curve names its set ---

    def test_the_curve_names_which_scope_how_many_replies_and_what_period(
        self,
    ) -> None:
        growth = self.growth(HG_CLEAN_DAY)
        self.assertEqual(growth["scope"], SCOPE_MAIN)
        self.assertEqual(growth["calls"], len(HG_RISING))
        self.assertIn("main-thread", growth["sample_is"])
        self.assertIsNotNone(growth["first_ts"])
        self.assertIsNotNone(growth["last_ts"])
        self.assertLess(growth["first_ts"], growth["last_ts"])

    def test_the_scope_the_curve_names_is_the_scope_it_measures(self) -> None:
        # The label is derived from `GROWTH_SCOPE` through `SCOPE_LABELS`, so a
        # curve that changed scope without changing its label is impossible
        # rather than merely unlikely.
        self.assertEqual(GROWTH_SCOPE, SOURCE_MAIN)
        self.assertEqual(self.growth(HG_CLEAN_DAY)["scope"], SCOPE_MAIN)

    # --- the finding itself ---

    def test_the_typical_context_rises_across_the_four_quarters(self) -> None:
        quarters = self.growth(HG_CLEAN_DAY)["quarters"]
        self.assertEqual([q["quarter"] for q in quarters], [1, 2, 3, 4])
        self.assertEqual(
            [q["median_context"] for q in quarters], HG_QUARTER_MEDIANS
        )
        self.assertEqual([q["calls"] for q in quarters], [4, 4, 4, 4])

    def test_the_utilisation_median_is_published_beside_the_context_one(self) -> None:
        # The persuasive half: by the last quarter the TYPICAL reply sits at
        # 91% of the window it had.
        quarters = self.growth(HG_CLEAN_DAY)["quarters"]
        for q, expected in zip(quarters, HG_QUARTER_UTILISATIONS):
            with self.subTest(quarter=q["quarter"]):
                self.assertAlmostEqual(q["median_utilisation"], expected, places=9)

    def test_the_curve_is_computed_over_the_main_thread_alone(self) -> None:
        # THE mis-scoping test. Over the same minutes the subagents' context
        # FALLS from 240k to 90k, so a curve computed over the wrong scope, or
        # pooled across both, is red in value AND in direction.
        quarters = self.growth(HG_CLEAN_DAY)["quarters"]
        medians = [q["median_context"] for q in quarters]
        self.assertEqual(medians, sorted(medians), "the curve does not rise")
        for wrong in (
            sorted(HG_FALLING, reverse=True)[:4],          # a subagent-only curve
            [(a + b) // 2 for a, b in zip(HG_RISING, HG_FALLING)],  # pooled
        ):
            with self.subTest(wrong=wrong):
                self.assertNotEqual(medians, wrong)
        self.assertEqual(
            sum(q["calls"] for q in quarters),
            len(HG_RISING),
            "the curve counted calls from a scope it does not name",
        )

    def test_the_median_is_neither_the_first_nor_the_last_call_of_its_quarter(
        self,
    ) -> None:
        # The fixture orders each quarter smallest, largest, then the middles,
        # so "the first call", "the last call" and the mean each report a
        # different figure from the median.
        quarters = self.growth(HG_CLEAN_DAY)["quarters"]
        for i, q in enumerate(quarters):
            block = HG_RISING[i * 4:(i + 1) * 4]
            with self.subTest(quarter=q["quarter"]):
                self.assertNotEqual(q["median_context"], block[0])
                self.assertNotEqual(q["median_context"], block[-1])
                self.assertNotEqual(q["median_context"], sum(block) // len(block))

    # --- absence is never a plotted zero ---

    def test_a_quarter_with_no_measured_call_is_a_named_absence(self) -> None:
        # The window spans the clean day and a day nineteen days later, so the
        # two middle quarters hold nothing at all. Plotted as 0 they would draw
        # the context collapsing in a period that was merely idle -- the defect
        # `timeseries()`'s null `avg_context` fixed for the daily chart.
        quarters = self.growth(HG_CLEAN_DAY, HG_LATE_DAY)["quarters"]
        empty = [q for q in quarters if q["calls"] == 0]
        self.assertTrue(empty, "the fixture no longer leaves a quarter empty")
        for q in empty:
            with self.subTest(quarter=q["quarter"]):
                self.assertEqual(q["no_sample_reason"], GROWTH_QUARTER_NO_CALLS)
                self.assertIsNone(q["median_context"])
                self.assertNotEqual(q["median_context"], 0)
                self.assertIsNone(q["median_utilisation"])
                self.assertNotEqual(q["median_utilisation"], 0)
                self.assertEqual(q["banded_calls"], 0)
        for q in quarters:
            if q["calls"]:
                with self.subTest(quarter=q["quarter"]):
                    self.assertIsNone(q["no_sample_reason"])
                    self.assertIsNotNone(q["median_context"])

    def test_a_quarters_two_medians_range_over_two_different_samples(self) -> None:
        # The last quarter of the wide window holds twelve calls, four of them
        # on a model with no documented window. The context median ranges over
        # all twelve; the utilisation median over the eight that could be
        # banded, and `banded_calls` beside it is what keeps the two from being
        # read as one figure.
        last = self.growth(HG_CLEAN_DAY, HG_LATE_DAY)["quarters"][-1]
        self.assertEqual(last["calls"], 12)
        self.assertEqual(last["banded_calls"], 8)
        self.assertIsNotNone(last["median_context"])
        self.assertIsNotNone(last["median_utilisation"])

    # --- a corpus too small to quarter says so ---

    def test_a_period_with_too_few_replies_refuses_to_draw_a_curve(self) -> None:
        growth = self.growth(HG_UNKNOWN_DAY)
        self.assertEqual(growth["refused_reason"], GROWTH_REFUSED_TOO_FEW)
        self.assertEqual(growth["minimum_calls"], GROWTH_MIN_CALLS)
        self.assertLess(growth["calls"], GROWTH_MIN_CALLS)
        # The counts are still published: they are true, and withholding them
        # would answer an over-claim with an absence nobody asked for.
        self.assertEqual(len(growth["quarters"]), GROWTH_QUARTERS)

    def test_a_period_with_no_span_refuses_for_its_own_reason(self) -> None:
        # Twelve calls -- past the floor -- all at one instant. "Too few" and
        # "no span" are different absences with different remedies, and the
        # wider one is named first, exactly as `_no_band_sample_reason` orders
        # its three.
        growth = self.growth(HG_INSTANT_DAY)
        self.assertGreaterEqual(growth["calls"], GROWTH_MIN_CALLS)
        self.assertEqual(growth["refused_reason"], GROWTH_REFUSED_NO_SPAN)
        self.assertEqual(len(growth["quarters"]), GROWTH_QUARTERS)

    def test_a_curve_it_can_draw_carries_no_refusal(self) -> None:
        # Non-null EXACTLY when the curve is refused, in both directions.
        self.assertIsNone(self.growth(HG_CLEAN_DAY)["refused_reason"])

    def test_the_floor_is_derived_from_the_median_it_protects(self) -> None:
        # The floor is not taste. `nearest_rank` at p50 takes index
        # `ceil(n/2) - 1`, which is 0 -- the quarter's SMALLEST call -- for
        # n <= 2, and 1 for n = 3. Three is therefore the smallest sample whose
        # median is strictly interior, and a "typical" context that is in fact
        # the quarter's minimum is not a typical anything.
        self.assertEqual(GROWTH_MIN_CALLS_PER_QUARTER, 3)
        self.assertEqual(GROWTH_MIN_CALLS, GROWTH_QUARTERS * 3)
        sample = [10, 20, 30]
        self.assertEqual(nearest_rank(sample[:2], 50), sample[0])
        self.assertEqual(nearest_rank(sample, 50), sample[1])
        self.assertNotEqual(nearest_rank(sample, 50), min(sample))
        self.assertNotEqual(nearest_rank(sample, 50), max(sample))

    # --- the sentence over the curve is DERIVED, not authored ---

    def test_a_rising_corpus_is_reported_as_rising(self) -> None:
        growth = self.growth(HG_CLEAN_DAY)
        self.assertEqual(growth["shape"], GROWTH_SHAPE_RISING)
        self.assertEqual(
            growth["shape_statement"], GROWTH_SHAPE_STATEMENTS[GROWTH_SHAPE_RISING]
        )
        self.assertEqual(growth["peak_quarter"], GROWTH_QUARTERS)

    def test_a_falling_corpus_is_never_reported_as_rising(self) -> None:
        # THE defect this block exists for. The panel shipped in review with
        # "And it only ever grows." written into the markup, which was true of
        # the corpus it was written against and false of the same database
        # three hours later.
        growth = self.growth(HG_FALLING_DAY)
        self.assertEqual(growth["shape"], GROWTH_SHAPE_FALLING)
        self.assertNotEqual(growth["shape"], GROWTH_SHAPE_RISING)
        self.assertNotIn("GREW", growth["shape_statement"])
        self.assertEqual(growth["peak_quarter"], 1)

    def test_a_sawtooth_corpus_reports_that_it_climbed_and_then_dropped(self) -> None:
        # The interesting case, in the proportions measured on the real
        # database: 277,945 -> 837,645 -> 297,343 -> 430,832. A large drop is
        # very likely the user FIXING the problem this panel is about, and
        # reporting it as growth would tell them their successful intervention
        # was a failure.
        growth = self.growth(HG_SAWTOOTH_DAY)
        self.assertEqual(growth["shape"], GROWTH_SHAPE_ROSE_THEN_FELL)
        self.assertEqual(growth["peak_quarter"], 2)
        medians = [q["median_context"] for q in growth["quarters"]]
        self.assertGreater(medians[1], medians[0])
        self.assertLess(medians[2], medians[1])
        self.assertLess(medians[-1], medians[1], "the fixture is no longer a sawtooth")

    def test_a_drop_is_never_asserted_to_be_a_compaction(self) -> None:
        # CPB cannot see a compaction event; it sees a drop. Naming the likely
        # cause is useful and asserting it would be a figure nobody measured --
        # so the sentence names it as one possibility among several and says in
        # as many words that the cause is not something these figures carry.
        statement = GROWTH_SHAPE_STATEMENTS[GROWTH_SHAPE_ROSE_THEN_FELL]
        self.assertIn("compaction", statement)
        self.assertIn("measures the drop and never its cause", statement)
        for asserted in ("was compacted", "you compacted", "a compaction happened"):
            with self.subTest(claim=asserted):
                self.assertNotIn(asserted, statement)

    def test_a_refused_curve_claims_no_shape_at_all(self) -> None:
        # Claiming a trend from a sample that cannot support one is exactly the
        # over-claim `refused_reason` exists to prevent.
        for day in (HG_UNKNOWN_DAY, HG_INSTANT_DAY):
            with self.subTest(day=day):
                growth = self.growth(day)
                self.assertIsNotNone(growth["refused_reason"])
                self.assertEqual(growth["shape"], GROWTH_SHAPE_UNMEASURABLE)

    def test_every_shape_has_its_own_sentence_and_no_two_share_one(self) -> None:
        self.assertEqual(set(GROWTH_SHAPE_STATEMENTS), set(GROWTH_SHAPES))
        self.assertEqual(len(set(GROWTH_SHAPE_STATEMENTS.values())), len(GROWTH_SHAPES))
        for shape, statement in GROWTH_SHAPE_STATEMENTS.items():
            with self.subTest(shape=shape):
                self.assertGreater(len(statement), 60)

    def test_the_threshold_is_carried_as_a_judgment_with_its_own_date(self) -> None:
        # The one judged number in the block. The medians beside it are
        # measurements, so they cross the API as separate fields -- the shape
        # `band_provenance` established, one panel over.
        growth = self.growth(HG_CLEAN_DAY)
        self.assertEqual(growth["shape_provenance"], GROWTH_SHAPE_PROVENANCE)
        self.assertRegex(growth["shape_as_of"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("judgment", GROWTH_SHAPE_PROVENANCE)
        # The sentence quotes the threshold, so it is BUILT from the constant
        # rather than repeating it -- two spellings of one boundary is how a
        # provenance comes to describe a threshold nobody uses.
        self.assertIn(f"{GROWTH_MATERIAL_CHANGE:.0%}", GROWTH_SHAPE_PROVENANCE)

    # --- the shape rules themselves, exhaustively ---

    # Every branch of `_growth_shape`, as medians -> shape. Named here rather
    # than driven from corpora because the taxonomy has six outcomes and
    # building six transcript corpora would test the ingester, not the rule.
    # `HG_SAWTOOTH_DAY` and `HG_FALLING_DAY` carry the two that matter most
    # through the real ingest path as well.
    SHAPES = (
        ([100, 200, 300, 400], GROWTH_SHAPE_RISING),
        ([100, 100, 100, 100], GROWTH_SHAPE_FLAT),
        # Within the threshold in both directions: movement, but not a change.
        ([100, 110, 95, 105], GROWTH_SHAPE_FLAT),
        ([400, 300, 200, 100], GROWTH_SHAPE_FALLING),
        ([100, 800, 300, 400], GROWTH_SHAPE_ROSE_THEN_FELL),
        # A dip too small to count does not demote a clear climb.
        ([100, 200, 195, 400], GROWTH_SHAPE_RISING),
        # Sagged and recovered: the peak is at the END, so this is not a fall
        # and it is not a rise either. Refusal, not the nearest finding.
        ([400, 100, 200, 900], GROWTH_SHAPE_MIXED),
        # Fell then rose back to where it started: no trend, and emphatically
        # not "rose then fell" read backwards.
        ([400, 100, 150, 400], GROWTH_SHAPE_MIXED),
        ([100], GROWTH_SHAPE_UNMEASURABLE),
        ([], GROWTH_SHAPE_UNMEASURABLE),
    )

    @staticmethod
    def quarters_of(medians) -> list[dict]:
        """`medians` as quarter rows; `None` is a quarter with no sample."""
        return [
            {
                "quarter": i + 1,
                "calls": 0 if median is None else 4,
                "median_context": median,
            }
            for i, median in enumerate(medians)
        ]

    def test_each_named_shape_is_derived_from_the_sequence(self) -> None:
        for medians, expected in self.SHAPES:
            with self.subTest(medians=medians):
                shape, _peak = Api._growth_shape(self.quarters_of(medians), None)
                self.assertEqual(shape, expected)

    def test_a_quarter_with_no_sample_is_skipped_rather_than_counted_as_zero(
        self,
    ) -> None:
        # THE rule this panel turns on, applied to the trend rather than to the
        # bar. Carried in as a 0 the empty quarter makes every idle fortnight a
        # collapse, and a rising curve with a gap in it reports "rose then
        # fell" -- a finding manufactured entirely out of an absence.
        with_gap = [100, None, 300, 400]
        self.assertEqual(
            Api._growth_shape(self.quarters_of(with_gap), None)[0],
            GROWTH_SHAPE_RISING,
        )
        as_zero = [100, 0, 300, 400]
        self.assertNotEqual(
            Api._growth_shape(self.quarters_of(as_zero), None)[0],
            GROWTH_SHAPE_RISING,
            "the fixture no longer distinguishes a skipped quarter from a zero",
        )

    def test_the_peak_names_a_quarter_that_has_a_median(self) -> None:
        shape, peak = Api._growth_shape(self.quarters_of([100, None, 800, 400]), None)
        self.assertEqual(peak, 3)
        self.assertEqual(shape, GROWTH_SHAPE_ROSE_THEN_FELL)
        self.assertIsNone(Api._growth_shape(self.quarters_of([None, None]), None)[1])

    def test_a_refusal_overrides_every_shape_the_sequence_would_have(self) -> None:
        for medians, _expected in self.SHAPES:
            with self.subTest(medians=medians):
                self.assertEqual(
                    Api._growth_shape(self.quarters_of(medians), "a reason")[0],
                    GROWTH_SHAPE_UNMEASURABLE,
                )

    def test_an_empty_scope_has_no_period_rather_than_a_zero_one(self) -> None:
        growth = self.growth("2026-07-10")
        self.assertEqual(growth["calls"], 0)
        self.assertIsNone(growth["first_ts"])
        self.assertIsNone(growth["last_ts"])
        self.assertEqual(growth["refused_reason"], GROWTH_REFUSED_TOO_FEW)
        for q in growth["quarters"]:
            with self.subTest(quarter=q["quarter"]):
                self.assertEqual(q["calls"], 0)
                self.assertIsNone(q["median_context"])
                self.assertEqual(q["no_sample_reason"], GROWTH_QUARTER_NO_CALLS)


class HealthBandIsBoundTest(unittest.TestCase):
    """#64's verdict reaches the reader, in the chrome, in three visible tones.

    Structural, with the limit the rest of this file records: the project ships
    no JS runtime (stdlib only, no Node), so these pin the bindings rather than
    executing the render.
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (cls.ROOT / "index.html").read_text()
        cls.html = strip_comments(cls.raw)
        cls.band = html_element(cls.raw, 'id="health-note"')

    def test_the_verdict_and_its_statement_come_from_the_api(self) -> None:
        # The page performs no lookup and holds no threshold: which state the
        # corpus is in, and the sentence that says so, are both measurements.
        for field in ("verdict", "statement"):
            with self.subTest(field=field):
                self.assertIn(f"summary.health.{field}", self.band)

    def test_the_checks_are_iterated_rather_than_spelled_out(self) -> None:
        # A band that named its six checks would render five and look complete
        # the day a seventh was added -- the shape #84 caught one module over.
        loop = re.search(r'<template x-for="([^"]+)"', self.band)
        self.assertIsNotNone(loop, "the checks are built some other way")
        self.assertIn("summary.health.checks", loop.group(1))
        for field in ("c.check", "c.state", "c.statement", "c.count", "c.of"):
            with self.subTest(field=field):
                self.assertIn(field, self.band)

    def test_a_count_the_database_does_not_hold_prints_as_an_absence(self) -> None:
        # `of` is null on `records-parsed` for ever. Run through anything but
        # `fmtCount` it would print as 0 and state that every record failed.
        for field in ("c.count", "c.of"):
            with self.subTest(field=field):
                self.assertIn(f"fmtCount({field})", self.band)

    def test_the_three_states_are_three_visibly_different_tones(self) -> None:
        # An unchecked corpus that LOOKED like a clean one would be this whole
        # feature failing at the last inch, so the tone is asserted as a table
        # of three distinct classes, each with a rule of its own.
        table = re.search(r"const HEALTH_TONE = \{(.*?)\};", self.html, re.S)
        self.assertIsNotNone(table, "HEALTH_TONE is gone")
        classes = re.findall(r':\s*"([^"]+)"', table.group(1))
        self.assertEqual(len(classes), 3)
        self.assertEqual(len(set(classes)), 3, "two states share a tone")
        for state in (HEALTH_OK, HEALTH_UNCHECKED, HEALTH_FAILED):
            with self.subTest(state=state):
                self.assertRegex(table.group(1), rf"\b{re.escape(state)}\s*:")
        for cls_name in classes:
            with self.subTest(css=cls_name):
                self.assertRegex(self.html, rf"\.{re.escape(cls_name)}\s*\{{[^}}]+\}}")

    def test_an_unrecognised_verdict_falls_back_to_the_loudest_tone(self) -> None:
        # A state added server-side that this page has never seen is exactly
        # the one that must not render as a clean bill of health.
        self.assertIn(
            "const HEALTH_TONE_UNRECOGNISED = HEALTH_TONE.failed;", self.html
        )
        self.assertIn("?? HEALTH_TONE_UNRECOGNISED", self.html)

    def test_a_failed_load_outranks_the_servers_verdict(self) -> None:
        # A response that never arrived cannot be reassured about. The band
        # renders its own branch rather than comparing two verdicts, so this
        # page holds no severity ordering that could drift from the API's.
        self.assertIn('x-if="summary && banner.loadFailure"', self.band)
        self.assertIn('x-if="summary && !banner.loadFailure"', self.band)
        self.assertLess(
            self.band.index("banner.loadFailure"),
            self.band.index("summary.health.verdict"),
            "the server's verdict is rendered before the load failure is ruled out",
        )
        self.assertIn(
            "this.banner.loadFailure", js_function_body(self.html, "get healthTone(")
        )

    def test_the_verdict_is_rendered_once_and_in_neither_view(self) -> None:
        # Chrome, like the banner and the data-age line: a verdict over every
        # figure in either view must not be one the reader can navigate away
        # from, and one element cannot disagree with itself.
        self.assertEqual(self.html.count('id="health-note"'), 1)
        for view in ("overview", "details"):
            with self.subTest(view=view):
                self.assertNotIn(
                    'id="health-note"', view_section(self.raw, view)
                )

    def test_the_verdict_adds_no_panel(self) -> None:
        # CLAUDE.md constraint 4. It wears the overview's own question-card
        # idiom rather than the detail view's `.panel`, and it DISPLACES a
        # banner message: the "No files ingested yet" notice is gone, subsumed
        # by the `records-parsed` check, which says the same thing and says
        # which of "clean" and "unchecked" it is. The net surface count across
        # both views is pinned in `ReportViewSplitTest`.
        self.assertRegex(self.raw, r'<section class="q" id="health-note"')
        self.assertNotIn("<table", self.band)
        self.assertNotIn("No files ingested yet", self.html)
        self.assertEqual(self.html.count('class="panel"'), 6, "a panel was added")

    def test_the_checks_are_one_expansion_down_and_the_verdict_is_not(self) -> None:
        # #88: six lines of `OK -- check-name: full sentence` was the density
        # problem in miniature. The DETAIL moved; the verdict did not, and the
        # order is asserted rather than assumed -- a verdict rendered below the
        # evidence it summarises is the defect this whole issue is about.
        detail = html_element(self.raw, 'id="health-detail"')
        resting = self.band.replace(detail, "")
        self.assertIn("summary.health.verdict", resting)
        self.assertIn("summary.health.statement", resting)
        self.assertIn("summary.health.checks", detail)
        self.assertNotIn("summary.health.checks", resting)
        self.assertLess(
            self.band.index("summary.health.verdict"),
            self.band.index('id="health-detail"'),
            "the verdict is rendered after the checks it summarises",
        )
        # Native `<details>`, not an `x-show` toggle: it needs no state and no
        # binding, it is keyboard-reachable for free, and it still opens if the
        # vendored Alpine bundle ever fails to load.
        self.assertRegex(detail, r"^<details\b")

    def test_the_verdict_is_one_binding_in_every_form_of_the_card(self) -> None:
        # #89 renders the card as ONE LINE on the summary level. The tag and
        # the statement are not part of that: they are rendered by a single
        # binding each, outside every conditional the shortening uses, so no
        # form of this card can exist without the verdict on it. A second
        # rendering "for the short form" is exactly how two levels come to
        # disagree about a verdict.
        for field in ("verdict", "statement"):
            with self.subTest(field=field):
                self.assertEqual(self.band.count(f"summary.health.{field}"), 1)

    def test_the_unparsed_record_warning_is_not_removed_from_the_banner(self) -> None:
        # The health band is a SECOND rendering of `ingest.unparsed_records`,
        # not a replacement for it. Moved out of the banner it would become a
        # warning about every total on the page that a reader could navigate
        # past -- and the banner is chrome precisely so that cannot happen.
        self.assertIn(
            "this.summary.ingest.unparsed_records > 0",
            js_function_body(self.html, "applySummary("),
        )


class SummaryLevelHealthCardTest(unittest.TestCase):
    """#89: the verdict costs one line on the summary, and never less than that.

    The health card is CHROME -- one element above all three levels, because a
    verdict over every figure in the report must not be one the reader can
    navigate away from. It is also, at ~90px of heading, answer and disclosure
    control, one of the three things standing between the summary level and its
    stated budget of one screen.

    So on the summary it renders as ONE LINE: the verdict tag and its
    statement, in the card's own tone, with the numbered question heading and
    the (closed) checks control dropped. The checks themselves are one click
    away, at the two levels below, and the tabs that reach them are on screen
    throughout.

    THE HARD CONSTRAINT, AND HOW IT IS MET. A `failed` verdict must be
    unmissable at every level. It is not enough to remember that: the condition
    for shortening is `healthOpen` ITSELF, so anything that expands its own
    evidence -- `failed`, a state this build has never seen, a load failure --
    keeps the whole card at every level, by composition. There is no second
    table to keep in step with `HEALTH_OPEN`, and a change that made `failed`
    collapse would have to turn `test_a_failed_verdict_cannot_be_collapsed`
    red first.

    THE LIMIT, STATED PLAINLY. Nothing here can see whether the one-line form
    is still loud enough to stop a reader, whether the tone reads as a tone at
    9px of padding, or whether the summary now fits a screen. Only a person
    looking at the page can judge that, and this page has shipped three defects
    green for exactly that reason.
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (cls.ROOT / "index.html").read_text()
        cls.html = strip_comments(cls.raw)
        cls.band = html_element(cls.raw, 'id="health-note"')

    def test_the_short_form_is_decided_by_a_table_over_the_levels(self) -> None:
        # One entry per view, and a table for the reason `HEALTH_OPEN` is one:
        # a chain of ifs could quietly return the same default for two levels,
        # and a test can assert this covers the levels exactly.
        table = re.search(r"const HEALTH_COMPACT = \{(.*?)\};", self.html, re.S)
        self.assertIsNotNone(table, "HEALTH_COMPACT is gone")
        defaults = dict(re.findall(r"(\w+):\s*(true|false)", table.group(1)))
        self.assertEqual(
            defaults,
            {"summary": "true", "overview": "false", "details": "false"},
            "a level shortens or keeps the verdict against the rule",
        )
        self.assertEqual(
            sorted(defaults),
            sorted(re.search(r"const VIEWS = \[([^\]]*)\]", self.html)
                   .group(1).replace('"', "").replace(" ", "").split(",")),
            "the page has a rule for a level that does not exist, or none for "
            "one that does",
        )

    def test_a_level_this_build_does_not_know_gets_the_whole_card(self) -> None:
        # Same direction as every other fallback on this page: the unfamiliar
        # case takes the loudest treatment, never the shortened one.
        self.assertIn("const HEALTH_COMPACT_UNRECOGNISED = false;", self.html)
        self.assertIn(
            "?? HEALTH_COMPACT_UNRECOGNISED",
            js_function_body(self.html, "get healthOneLine("),
        )

    def test_a_verdict_that_opens_itself_is_never_shortened(self) -> None:
        # THE hard constraint, as the structure that enforces it: `healthOpen`
        # is consulted FIRST and returns before the level is even looked at, so
        # `failed` -- and an unrecognised verdict, and a load failure, both of
        # which `HEALTH_OPEN` sends the same way -- keeps its heading, its tone
        # and its six open lines on the summary too.
        body = js_function_body(self.html, "get healthOneLine(")
        self.assertRegex(body, r"if \(this\.healthOpen\) return false;")
        self.assertLess(
            body.index("healthOpen"),
            body.index("HEALTH_COMPACT"),
            "the level is consulted before the verdict, so a proven failure "
            "can be the thing that gets shortened",
        )

    def test_the_tag_and_the_statement_survive_the_short_form(self) -> None:
        # What one-lining may cost is furniture, and this is the list: the
        # numbered heading and the checks CONTROL. The verdict tag and its
        # statement are rendered outside both, so no form of this card exists
        # without them.
        heading = '<template x-if="!healthOneLine"><h2>'
        self.assertIn(heading, self.band)
        answer = self.band[self.band.index('class="answer"'):]
        self.assertNotIn("healthOneLine", answer[: answer.index("</div>")])
        self.assertIn("summary.health.verdict", self.band)
        self.assertIn("summary.health.statement", self.band)

    def test_the_checks_are_hidden_at_one_level_and_deleted_at_none(self) -> None:
        # The mutation "a disclosure used to drop a figure rather than move
        # it", in its cheapest form: delete the checks instead of hiding the
        # control. The element is still in the page, still iterating the API's
        # own list, still opened by `healthOpen` -- it is one `x-if` that
        # decides whether this LEVEL renders it, and the other two do.
        detail = html_element(self.raw, 'id="health-detail"')
        self.assertIn("summary.health.checks", detail)
        self.assertEqual(self.html.count('id="health-detail"'), 1)
        before = self.band[: self.band.index('id="health-detail"')]
        self.assertIn('x-if="!healthOneLine"', before)

    def test_the_short_form_is_the_same_surface_with_less_padding(self) -> None:
        # Not a second card, not a second tone table, not a second verdict: one
        # element, one class, and a padding rule. A short form built as its own
        # markup would be a second rendering of the verdict, free to drift from
        # the first.
        self.assertIn(
            ":class=\"healthTone + (healthOneLine ? ' compact' : '')\"", self.band
        )
        self.assertRegex(self.html, r"\.q\.compact\s*\{[^}]*padding")
        self.assertEqual(self.html.count('id="health-note"'), 1)


class GrowthCurveIsBoundTest(unittest.TestCase):
    """#65's curve reaches the reader, and an absence never gets a bar."""

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (cls.ROOT / "index.html").read_text()
        cls.html = strip_comments(cls.raw)
        cls.band = html_element(cls.raw, 'id="context-note"')

    def test_the_curve_states_the_set_it_ranges_over(self) -> None:
        for field in ("scope", "calls", "first_ts", "last_ts", "sample_is"):
            with self.subTest(field=field):
                self.assertIn(f"summary.context.growth.{field}", self.band)

    def test_the_quarters_are_iterated_from_the_payload(self) -> None:
        # TIGHTENED AFTER A SURVIVING MUTATION. `assertIn` over the whole card
        # passed while the BAR loop iterated a literal `[]`, because the LABEL
        # loop beside it still named the payload -- the curve rendered nothing
        # and every field it mentions still looked wired. That is
        # `DeclarativeRenderLayerTest`'s "iterating a literal []" defect, which
        # is pinned per table there and was unpinned here.
        #
        # So both loops are asserted, and each is asserted INSIDE the element
        # it fills: the bars and their labels are two renderings of one list
        # and neither may be fed from somewhere else.
        loop = 'x-for="q in summary.context.growth.quarters"'
        self.assertEqual(
            self.band.count(loop),
            2,
            "the quarters are built some other way, or one of the two loops "
            "was pointed at something that is not the payload",
        )
        for element in ('class="growth"', 'class="glabels"'):
            with self.subTest(element=element):
                self.assertIn(loop, html_element(self.raw, element))

    def test_a_quarter_with_no_sample_gets_the_apis_reason_and_no_bar(self) -> None:
        # The central rule of this panel, in pixels: an absence has no height.
        # A bar of zero height is indistinguishable from a bar the page decided
        # not to draw, and a bar drawn AT zero would show the context
        # collapsing in a quarter that was merely idle.
        #
        # #88 turned the four proportional ROWS into four labelled BARS, so the
        # gate moved from "inside the sampled branch" onto the bar element
        # itself -- which is the stronger statement of the two, because it is
        # the drawing that must not exist rather than a branch that happens to
        # contain it. A quarter that ran calls but banded none has a null
        # `median_utilisation` and a null `no_sample_reason`, and it must get no
        # bar either; only the element's own guard covers that case.
        self.assertRegex(
            self.band,
            r'x-if="barWidth\(q\.median_utilisation\) !== null">\s*'
            r'<div class="gbar"',
            "the bar is drawn outside the guard that establishes a reading",
        )
        self.assertEqual(
            self.band.count('class="gbar"'), 1, "a second bar escapes the guard"
        )
        self.assertIn('x-if="q.no_sample_reason"', self.band)
        self.assertIn('x-if="!q.no_sample_reason"', self.band)
        self.assertIn('x-text="q.no_sample_reason"', self.band)

    def test_a_nonzero_reading_keeps_a_visible_minimum_width(self) -> None:
        # "Rare" and "never" must never render as the same picture, which is
        # the acceptance criterion this issue states in pixels -- and #88's
        # central rule for the meters as well, which is why ONE function serves
        # both. Two copies of this rule would be free to drift.
        body = js_function_body(self.html, "function barWidth(")
        self.assertIn("Math.max(1,", body)
        self.assertLess(
            body.index("=== null"),
            body.index("Math.max"),
            "the width is computed before the absence is recognised",
        )
        self.assertRegex(body, r"if \(x <= 0\) return null;")
        # The percentage floor alone is not enough: 1% of a 200px meter is 2px.
        # The pixel floor lives in the stylesheet, and both are load-bearing.
        self.assertRegex(self.html, r"\.seg\s*\{[^}]*min-width:\s*\d+px")

    def test_the_curve_refuses_rather_than_drawing_four_bars_from_three_points(
        self,
    ) -> None:
        for field in ("refused_reason", "minimum_calls"):
            with self.subTest(field=field):
                self.assertIn(f"summary.context.growth.{field}", self.band)

    def test_the_sentence_over_the_curve_is_derived_and_not_written_here(self) -> None:
        # THE regression this test exists for, and it shipped once: the band
        # was headed "And it only ever grows." -- a trend claim written into
        # the markup, true of the corpus it was written against and false of
        # the SAME DATABASE three hours later, because the session compacted.
        #
        # A heading that states a trend the numbers do not have is a wrong
        # figure made of words, and no formatter guards prose. So the verdict
        # and its sentence both come off the payload, and the phrase that was
        # wrong may not reappear anywhere in the render layer.
        for field in ("shape", "shape_statement", "peak_quarter"):
            with self.subTest(field=field):
                self.assertIn(f"summary.context.growth.{field}", self.band)
        for authored in ("only ever grows", "it only ever", "never sheds"):
            with self.subTest(claim=authored):
                self.assertNotIn(authored, self.html)

    def test_the_page_names_no_shape_of_its_own(self) -> None:
        # Six shapes, six sentences, one owner. A page-side map from `shape` to
        # prose would be a seventh reading with no date and no provenance --
        # `bandRange`'s rule ("the page invents no band of its own") applied to
        # a verdict rather than to a boundary.
        for shape in GROWTH_SHAPES:
            with self.subTest(shape=shape):
                self.assertNotIn(f'"{shape}"', self.html)
                self.assertNotIn(f"'{shape}'", self.html)
        for statement in GROWTH_SHAPE_STATEMENTS.values():
            with self.subTest(statement=statement[:32]):
                self.assertNotIn(statement, self.html)

    def test_the_judged_threshold_keeps_its_own_voice_and_date(self) -> None:
        # The medians are measurements and the threshold is not, so the page
        # must not present the second in the first's voice -- `band_provenance`'s
        # rule, one panel over.
        judged = re.search(
            r'<span class="([^"]*context-judged[^"]*)"[^>]*>\s*Shape threshold',
            self.band,
        )
        self.assertIsNotNone(
            judged, "the shape threshold is not marked as a judgment"
        )
        self.assertIn("summary.context.growth.shape_provenance", self.band)
        self.assertIn("summary.context.growth.shape_as_of", self.band)

    def test_the_shape_claim_and_its_judgment_are_never_separated(self) -> None:
        # THE RULE #88 MADE BITE HARDER. It used to read "the threshold sits
        # WITH the verdict rather than behind the disclosure", which was the
        # right rule while the verdict was on the resting card. #88 collapsed
        # this whole block, so the rule has to be stated as what it always
        # meant: a judgment may not be further from the reader than the claim
        # it produced. Both are now inside `#context-detail`, and the failure
        # to catch is one of them moving without the other -- a card that
        # asserted a SHAPE at rest while the judgment behind it stayed one
        # click down would be a product-owner judgment presented as a
        # measurement, which is exactly what `band_provenance` exists to stop.
        detail = html_element(self.raw, 'id="context-detail"')
        resting = self.band.replace(detail, "")
        for field in ("shape", "shape_statement", "shape_as_of", "shape_provenance"):
            path = f"summary.context.growth.{field}"
            with self.subTest(field=field):
                self.assertIn(path, detail, f"{field} left the region")
                self.assertNotIn(
                    path,
                    resting,
                    f"{field} is asserted at rest while its companions are not",
                )

    def test_the_answer_sentence_precedes_the_meters_it_is_evidenced_by(self) -> None:
        # #65 asks for the ANSWER above the meters. Order is the whole
        # difference between a figure that prompts an action and one that
        # prompts none -- the same reason #61 pinned the scoped tally first.
        self.assertLess(
            self.band.index("summary.context.utilisation.worst_scope"),
            self.band.index(SCOPE_LOOP_EXPR),
            "the meters are rendered before the answer they evidence",
        )
        self.assertIn(
            'x-text="summary.context.utilisation.worst_scope_ranked_by"', self.band
        )

    def test_the_page_ranks_nothing_of_its_own(self) -> None:
        # The winner and the key are both read off the payload. A page-side
        # comparison would be a second ranking, free to disagree with the one
        # the recommendation table is driven by.
        for forbidden in ("Math.max(...", ".sort(", "over_half_window_share >"):
            with self.subTest(expr=forbidden):
                self.assertNotIn(forbidden, self.band)

    def test_the_method_and_both_provenances_move_behind_a_disclosure(self) -> None:
        # Not deleted -- the judged boundaries must keep their date and their
        # own voice, which is why `band_provenance` is a separate field at all.
        # What was wrong was the WEIGHT: at the same size as the finding they
        # buried it.
        disclosure = html_element(self.raw, 'id="context-detail"')
        for field in ("window_provenance", "band_provenance"):
            with self.subTest(field=field):
                self.assertIn(field, disclosure)
        self.assertIn("Median, not mean", disclosure)
        self.assertIn("One sample, one definition", disclosure)
        self.assertIn("contextSpread", disclosure)
        self.assertRegex(self.html, r"\.disclosure\b[^{]*\{[^}]+\}")

    def test_the_per_scope_means_reached_a_reader(self) -> None:
        # #82 group 1, consumed rather than deleted. Each scope names ITSELF
        # from the payload, so the page invents no mapping between the bucket
        # key and the label the meters are keyed on.
        for kind in ("main_thread", "subagent"):
            for field in ("scope", "avg_context", "context_calls", "unmeasured_calls"):
                with self.subTest(kind=kind, field=field):
                    self.assertIn(f"summary.scope.{kind}.{field}", self.band)
        # `Math.round(null)` is 0, so the mean goes through the formatter that
        # refuses an absence before it rounds.
        for kind in ("main_thread", "subagent"):
            with self.subTest(kind=kind):
                self.assertIn(
                    f"fmtTokRounded(summary.scope.{kind}.avg_context)", self.band
                )


class UtilisationMeterTest(unittest.TestCase):
    """#88: the bands are a picture, and a zero is not a thin sliver.

    THE CENTRAL RULE OF THIS CHANGE, IN PIXELS. Four comma-separated shares
    told the reader nothing at a glance; a proportional meter does. What a
    meter can lose that a sentence cannot is the difference between RARE and
    NEVER -- a 0.04% band and a 0% band are one pixel apart on any real width,
    and this repository's whole posture is that a real 0 and no sample must not
    render alike. So the rule is enforced in two directions at once: a nonzero
    share keeps a visible minimum, and a TRUE ZERO GETS NO SEGMENT AT ALL,
    while the legend beside it still states the zero as the measurement it is.

    The limit these share with every other check in this file: the project
    ships no JS runtime, so they pin the bindings and the stylesheet rather
    than executing the render. What a person still has to look at is whether
    the minimum width is wide enough to SEE -- a rule that is satisfied at one
    pixel is satisfied and invisible.
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (cls.ROOT / "index.html").read_text()
        cls.html = strip_comments(cls.raw)
        cls.band = html_element(cls.raw, 'id="context-note"')

    def test_a_true_zero_gets_no_segment_at_all(self) -> None:
        # Not a segment of zero width -- NO SEGMENT. A zero-width flex item
        # with a `min-width` is a segment of the minimum width, which is
        # exactly the picture "rare" produces, so the guard has to remove the
        # element rather than size it.
        segments = re.findall(
            r'x-if="barWidth\(b\.share\) !== null">\s*<span class="seg"', self.band
        )
        self.assertEqual(
            len(segments), 2, "a meter draws a segment outside the zero guard"
        )
        self.assertEqual(
            self.band.count('<span class="seg"'),
            2,
            "there is a segment element the guard does not cover",
        )
        body = js_function_body(self.html, "function barWidth(")
        self.assertRegex(body, r"if \(x <= 0\) return null;")

    def test_a_zero_band_is_still_stated_beside_the_picture_that_omits_it(
        self,
    ) -> None:
        # The other half, and the one that keeps the omission honest: a band
        # with no calls is dropped from the METER and kept in the LEGEND, with
        # its count of 0 rendered through `fmtCount` and marked as the real
        # zero it is. Dropped from both, it would be indistinguishable from a
        # band the API never sent.
        self.assertEqual(
            self.band.count("""<span :class="b.calls ? '' : 'zero'\""""),
            2,
            "a legend does not mark its zero bands, or one legend is gone",
        )
        self.assertEqual(self.band.count("fmtCount(b.calls)"), 2)
        self.assertRegex(self.html, r"\.legend \.zero[^{]*\{[^}]+\}")

    def test_a_share_of_an_empty_set_is_not_drawn_as_a_zero(self) -> None:
        # `share` is null -- never 0.0 -- for a band over an empty set, and
        # `barWidth` refuses a null before it computes. A meter drawn from
        # nulls would show four missing segments, which is the same picture as
        # four measured zeroes.
        body = js_function_body(self.html, "function barWidth(")
        self.assertLess(
            body.index("=== null"),
            body.index("Math.max"),
            "the width is computed before the absence is recognised",
        )

    def test_every_band_the_api_can_send_has_a_tone_of_its_own(self) -> None:
        # `TurnTypeChartColourTest`'s rule, one widget over: a palette shorter
        # than the series count gives two bands one colour, and a meter with
        # two identically coloured segments does not look broken -- it looks
        # like one segment. The tone is keyed on POSITION because the page may
        # not spell a band's name (`test_the_page_invents_no_band_of_its_own`).
        table = re.search(r"const BAND_TONES = \[(.*?)\];", self.html, re.S)
        self.assertIsNotNone(table, "BAND_TONES is gone")
        tones = re.findall(r'"([^"]+)"', table.group(1))
        self.assertGreaterEqual(
            len(tones), len(BANDS), "there are more bands than tones for them"
        )
        self.assertEqual(len(set(tones)), len(tones), "two bands share a tone")
        for tone in tones:
            with self.subTest(tone=tone):
                self.assertRegex(self.html, rf"\.{re.escape(tone)}\s*\{{[^}}]+\}}")

    def test_a_band_this_build_has_no_tone_for_is_still_visible(self) -> None:
        # The fallback is the load-bearing half. An uncoloured segment inherits
        # the meter TRACK's own background, so a band added upstream would
        # render as the absence of a band -- the one substitution this
        # repository refuses. Same direction as `HEALTH_TONE_UNRECOGNISED`.
        self.assertIn('const BAND_TONE_UNRECOGNISED = "seg-extra";', self.html)
        self.assertIn(
            "return BAND_TONES[i] ?? BAND_TONE_UNRECOGNISED;",
            js_function_body(self.html, "function bandTone("),
        )
        fallback = re.search(r"\.seg-extra\s*\{([^}]+)\}", self.html)
        self.assertIsNotNone(fallback, "the fallback tone has no rule")
        track = re.search(r"\.meter\s*\{([^}]+)\}", self.html)
        self.assertNotEqual(
            re.search(r"background:\s*([^;]+)", fallback.group(1)).group(1).strip(),
            re.search(r"background:\s*([^;]+)", track.group(1)).group(1).strip(),
            "an unrecognised band is drawn in the track's own colour, which is "
            "a measurement rendered as an absence",
        )

    def test_the_pooled_tally_is_drawn_the_same_way_it_is_demoted(self) -> None:
        # Kept, demoted, and drawn -- the dilution #61 is about is a SHAPE, and
        # stating it in words under two meters that show the opposite would
        # leave the reader to do the comparison themselves.
        self.assertLess(
            self.band.index(SCOPE_LOOP_EXPR),
            self.band.index("summary.context.utilisation.includes"),
            "the pooled meter is not demoted below the split it hides",
        )
        self.assertEqual(self.band.count('<div class="meter">'), 2)


class OverviewRestingStateTest(unittest.TestCase):
    """#88: what the overview says before anyone touches it.

    THE OWNER'S SECOND CORRECTION, and it is about the DEFAULT STATE rather
    than the layout: "let's start with the basics, and then expand, but not
    with a wall of text." Four cards that each state an answer and stop, with
    every figure behind them still rendered and one expansion away.

    ONE RULE GOVERNS THE WHOLE THING: DENSITY SCALES WITH HOW MUCH IS WRONG. A
    card opens ITSELF when the API has PROVEN something to act on -- a failed
    check, a scope at or above the judged boundary, a lever -- because a
    problem behind a click the reader has to know to make is a problem this
    page failed to report. Everything else collapses to a line, because a
    healthy report should be short and quiet and that brevity is itself the
    finding.

    THE CORRECTION, 2026-08-05, FOUND BY LOOKING AT THE PAGE. This class first
    shipped saying "anything that is not healthy -- including anything
    INCONCLUSIVE -- opens itself", and on a freshly re-ingested clean corpus
    the health card still opened all six of its lines: the verdict was
    `unchecked`, because `transcript-format-census` read 58 of 59 sources
    uncensused. Those sources were ingested before the census existed and are
    unchanged, so they are censused only when they next change -- which for
    most of them is never. The state is PERMANENT AND BENIGN, so the card was
    open on every load for ever, and the density rule collapsed nothing on the
    one page state every reader sees every time.

    So the rule is now: a PROVEN problem expands; an unknown and an all-clear
    both collapse. Nothing is hidden by it -- each state states itself at rest,
    in its own tone, with the counts that qualify it, and only the EVIDENCE
    moves. The same correction runs through questions 2, 3 and 4, because
    "could not be measured" is permanent there too: a project that dispatches
    no subagent has no main-to-subagent ratio and never will.

    THE LIMIT, STATED PLAINLY. These pin which bindings sit inside which
    region and what decides the `open` attribute. They cannot see whether the
    resting page fits on a screen, whether a segment is wide enough to notice,
    or whether the answer reads as an answer. That is the defect #88 was filed
    over and the reason it shipped green: only a person looking at the page can
    judge it.
    """

    ROOT = Path(__file__).resolve().parent.parent

    # card -> the ONE disclosure that holds its evidence.
    CARDS = (
        ("health-note", "health-detail"),
        ("context-note", "context-detail"),
        ("advice-note", "optimize-detail"),
        ("next-note", "next-detail"),
    )

    # What each card must ANSWER with, at rest, before any evidence. Every one
    # of these is the API's own word or figure: the page states no verdict of
    # its own anywhere on this level.
    RESTING = {
        "health-note": ("summary.health.verdict", "summary.health.statement"),
        "context-note": (
            # THE ANSWER TO THE HEADING, and both halves of it: the card asks a
            # yes/no question, so the verdict AND the sentence that states it
            # are the first thing on the card. Both are the API's -- a sentence
            # composed here would be a claim nothing can check.
            "summary.context.utilisation.answer.verdict",
            "summary.context.utilisation.answer.statement",
            # #89 MOVED `worst_scope` AND ITS TWO FIGURES DOWN, and this is the
            # trim the issue names in as many words: the card spent three lines
            # of prose plus a caption saying "yes, your main thread". The
            # ANSWER is now one sentence, and the scope that makes it so is one
            # expansion below with the meters that evidence it -- where a
            # proven YES opens it by default, so on the state where it matters
            # it is on screen without a click. It is also the one thing the
            # SUMMARY level says at a glance ("Yes -- main-thread"), off the
            # same server-side tally, so the reader who wants that word does
            # not have to open anything at all.
            # A ranking must name the key it orders by, and a key one click
            # away has not been named -- `serve.RANKED_BY`'s rule survives the
            # collapse.
            "summary.context.utilisation.worst_scope_ranked_by",
            # The two DATES ride with the answer for the same reason: a judged
            # cut point whose date is hidden is a judgment presented as a fact.
            "summary.context.utilisation.windows_as_of",
            "summary.context.utilisation.bands_as_of",
        ),
        "advice-note": (
            "topOpportunity.metric",
            "topOpportunity.value",
            "topOpportunity.severity",
            "summary.recommendations.as_of",
        ),
        "next-note": ("topOpportunity.lever.directive",),
    }

    # What each card must NOT assert at rest -- the evidence, which is one
    # expansion down. A path here that crept back up is a card rebuilding the
    # wall this issue tore down.
    COLLAPSED = {
        "health-note": ("summary.health.checks",),
        "context-note": (
            # #89: the ranking's winner and both of its figures. Moved, never
            # deleted -- `test_no_figure_was_deleted_rather_than_collapsed`
            # requires each of these to be inside the disclosure, which is what
            # makes "trim" different from "drop".
            "summary.context.utilisation.worst_scope",
            "summary.context.utilisation.worst_scope_over_half_window_calls",
            "summary.context.utilisation.worst_scope_over_half_window_share",
            "summary.context.utilisation.by_scope",
            "summary.context.utilisation.bands",
            "summary.context.growth",
            "summary.context.utilisation.band_provenance",
            "summary.context.utilisation.window_provenance",
            "summary.context.mean",
            "summary.scope.main_thread.avg_context",
            "contextSpread",
        ),
        "advice-note": (
            "summary.recommendations.ranked",
            "summary.recommendations.provenance",
            "summary.recommendations.ranking_provenance",
            "summary.recommendations.unmeasured_note",
        ),
        "next-note": ("summary.recommendations.ranked",),
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (cls.ROOT / "index.html").read_text()
        cls.html = strip_comments(cls.raw)

    def card(self, card_id: str) -> str:
        return html_element(self.raw, f'id="{card_id}"')

    def resting(self, card_id: str, detail_id: str) -> str:
        """The card with its evidence disclosure removed."""
        card = self.card(card_id)
        return card.replace(html_element(self.raw, f'id="{detail_id}"'), "")

    def test_every_card_answers_at_rest(self) -> None:
        for card_id, detail_id in self.CARDS:
            resting = self.resting(card_id, detail_id)
            for path in self.RESTING[card_id]:
                with self.subTest(card=card_id, path=path):
                    self.assertIn(
                        path, resting, f"{card_id} does not state {path} at rest"
                    )

    def test_no_card_states_its_evidence_at_rest(self) -> None:
        for card_id, detail_id in self.CARDS:
            resting = self.resting(card_id, detail_id)
            for path in self.COLLAPSED[card_id]:
                with self.subTest(card=card_id, path=path):
                    # Matched to a path BOUNDARY, not as a substring:
                    # `utilisation.bands_as_of` is a date the answer is
                    # required to carry and `utilisation.bands` is evidence it
                    # is required not to, and one is a prefix of the other.
                    self.assertNotRegex(
                        resting,
                        re.escape(path) + r"(?!\w)",
                        f"{card_id} still asserts {path} before anyone asks",
                    )

    def test_no_figure_was_deleted_rather_than_collapsed(self) -> None:
        # THE mutation this class exists to catch, and the cheapest way to
        # satisfy every test above: delete the evidence instead of collapsing
        # it. Collapsed is still RENDERED, so every path the resting card
        # refuses must be present in the disclosure below it. The wiring guard
        # would see a field that lost its LAST reader; it cannot see one that
        # merely lost the reader it was supposed to keep.
        for card_id, detail_id in self.CARDS:
            detail = html_element(self.raw, f'id="{detail_id}"')
            for path in self.COLLAPSED[card_id]:
                with self.subTest(card=card_id, path=path):
                    self.assertIn(
                        path,
                        detail,
                        f"{path} was dropped from {card_id} rather than collapsed",
                    )

    def test_every_answer_precedes_the_evidence_it_rests_on(self) -> None:
        # Order, as structure. #88's first complaint was that every verdict was
        # buried mid-paragraph; a card whose disclosure came first would satisfy
        # every containment check above and still bury it.
        for card_id, detail_id in self.CARDS:
            with self.subTest(card=card_id):
                card = self.card(card_id)
                self.assertLess(
                    card.index('class="answer"'),
                    card.index(f'id="{detail_id}"'),
                    f"{card_id} renders its evidence above its answer",
                )

    def test_each_card_holds_its_evidence_in_one_native_disclosure(self) -> None:
        # ONE, so there is a single thing to open and a single default to
        # decide. NATIVE, so it needs no state, no binding and no JavaScript,
        # stays keyboard-reachable, and still works on a page whose vendored
        # Alpine bundle failed to load -- the `:open` binding decides only the
        # DEFAULT, never whether the control exists.
        for card_id, detail_id in self.CARDS:
            with self.subTest(card=card_id):
                card = self.card(card_id)
                self.assertEqual(
                    card.count(f'id="{detail_id}"'), 1, "the card's disclosure moved"
                )
                detail = html_element(self.raw, f'id="{detail_id}"')
                self.assertRegex(detail, r"^<details\b")
                self.assertIn("<summary>", detail)
                self.assertNotIn("x-show", detail[: detail.index(">")])

    def test_only_a_failed_verdict_expands_its_own_evidence(self) -> None:
        # THE asymmetry, as a table for the reason `HEALTH_TONE` is one: a
        # chain of ifs could quietly return the same default for two states,
        # and a test can assert this.
        table = re.search(r"const HEALTH_OPEN = \{(.*?)\};", self.html, re.S)
        self.assertIsNotNone(table, "HEALTH_OPEN is gone")
        defaults = dict(re.findall(r"(\w+):\s*(true|false)", table.group(1)))
        self.assertEqual(
            defaults,
            {HEALTH_OK: "false", HEALTH_UNCHECKED: "false", HEALTH_FAILED: "true"},
            "a verdict collapses or expands against the rule",
        )

    def test_a_failed_verdict_cannot_be_collapsed(self) -> None:
        # One half of the rule on its own, so a table edited wholesale cannot
        # quietly take this with it: a PROVEN failure is the one thing that
        # must never sit behind a click the reader has to know to make.
        table = re.search(r"const HEALTH_OPEN = \{(.*?)\};", self.html, re.S)
        self.assertRegex(
            table.group(1),
            rf"\b{re.escape(HEALTH_FAILED)}:\s*true",
            "a failing verdict hides its own checks",
        )

    def test_an_unchecked_verdict_is_not_expanded_by_default(self) -> None:
        # THE CONVERSE, and the defect the owner found by loading the page: on
        # a clean, freshly re-ingested corpus the verdict is `unchecked` --
        # 58 of 59 sources uncensused, because they were ingested before the
        # census existed and are censused only when they next change -- and the
        # card opened all six lines on every load for ever. A state the reader
        # cannot clear and need not act on may not be what holds a card open,
        # or nothing on the page is ever collapsed.
        #
        # It is not hidden by this: `RESTING` above pins that the verdict and
        # its statement stay on the resting card, and `HEALTH_TONE` gives
        # `unchecked` a tone of its own, so the reader is told and can open it.
        table = re.search(r"const HEALTH_OPEN = \{(.*?)\};", self.html, re.S)
        self.assertRegex(
            table.group(1),
            rf"\b{re.escape(HEALTH_UNCHECKED)}:\s*false",
            "an unchecked verdict forces six lines open on every load",
        )

    def test_a_verdict_this_build_does_not_know_still_opens(self) -> None:
        # The fallback runs the other way from the rule above, and must: a
        # state added server-side that this page has never seen might be a new
        # FAILURE, and the one thing it must not do is render as a quiet page.
        self.assertIn("const HEALTH_OPEN_UNRECOGNISED = true;", self.html)
        self.assertIn(
            "?? HEALTH_OPEN_UNRECOGNISED", js_function_body(self.html, "get healthOpen(")
        )

    def test_a_failed_load_opens_the_verdict_it_cannot_reassure_about(self) -> None:
        # Same asymmetry as `healthTone` and `bannerMessages`: elapsed evidence
        # that a response never arrived may only ever RAISE the alarm.
        body = js_function_body(self.html, "get healthOpen(")
        self.assertRegex(
            body,
            r"if \(!this\.summary \|\| this\.banner\.loadFailure\) \{\s*"
            r"return HEALTH_OPEN_UNRECOGNISED;",
        )

    def test_only_a_proven_yes_opens_the_context_card(self) -> None:
        # The same rule as `HEALTH_OPEN`, over the API's five answers, and a
        # table for the same reason. `yes` is the one state the API has PROVEN
        # -- calls measured at or above the judged boundary -- so it is the one
        # that opens the meters. An `inconclusive` or `unknown` reading is an
        # absence, and the absences here are permanent in exactly the way the
        # census one is: a corpus that always carries a few calls with no
        # context accounting, or one model this build has no window for, would
        # hold this card open on every load for ever.
        table = re.search(r"const CONTEXT_OPEN = \{(.*?)\};", self.html, re.S)
        self.assertIsNotNone(table, "CONTEXT_OPEN is gone")
        defaults = dict(re.findall(r'"([^"]+)":\s*(true|false)', table.group(1)))
        self.assertEqual(
            defaults,
            {
                CONTEXT_ANSWER_YES: "true",
                CONTEXT_ANSWER_NO: "false",
                CONTEXT_ANSWER_INCONCLUSIVE: "false",
                CONTEXT_ANSWER_UNKNOWN: "false",
                CONTEXT_ANSWER_NO_SAMPLE: "false",
            },
            "question 2 collapses or expands against the rule",
        )
        self.assertEqual(
            set(defaults),
            set(CONTEXT_ANSWER_STATES),
            "the page has a default for a state the API does not send, or "
            "none for one it does",
        )
        self.assertIn("const CONTEXT_OPEN_UNRECOGNISED = true;", self.html)
        self.assertIn(
            "?? CONTEXT_OPEN_UNRECOGNISED",
            js_function_body(self.html, "get contextOpen("),
        )

    def test_what_could_not_be_banded_is_stated_at_rest_not_collapsed(self) -> None:
        # THE PRICE OF COLLAPSING ON AN UNKNOWN, and it is paid on the resting
        # card. An `inconclusive` answer no longer opens the meters, so the
        # three counts that make it inconclusive must be on the card itself --
        # counted three ways, because three different absences have three
        # different remedies and a single total would name none of them.
        resting = self.resting("context-note", "context-detail")
        for field in ("unmeasured_calls", "unknown_model_calls", "over_window_calls"):
            with self.subTest(field=field):
                self.assertIn(
                    f"fmtCount(summary.context.utilisation.{field})",
                    resting,
                    f"{field} is consulted but never stated at rest",
                )

    def test_an_unmeasured_metric_is_stated_at_rest_rather_than_forced_open(
        self,
    ) -> None:
        # "No sample" and "nothing to change" must not render alike, which is
        # the rule the table's explicit healthy entry exists for. It used to be
        # kept by opening both cards, and that is the `unchecked` defect again:
        # a project that dispatches no subagent has no main-to-subagent ratio
        # and never will, so both cards stood open for ever over a reading
        # nobody can act on. The COUNT moved onto the resting card instead; the
        # names of the metrics are evidence, and evidence is what collapses.
        body = js_function_body(self.html, "get opportunitiesOpen(")
        self.assertIn("return this.topOpportunity !== null;", body)
        self.assertNotIn("unmeasured", body, "an absence still forces the card open")
        for card_id, detail_id in (
            ("advice-note", "optimize-detail"),
            ("next-note", "next-detail"),
        ):
            with self.subTest(card=card_id):
                resting = self.resting(card_id, detail_id)
                self.assertIn(
                    "Object.keys(summary.recommendations.unmeasured).length",
                    resting,
                    f"{card_id} collapses on an unmeasured reading without "
                    "saying there is one",
                )

    def test_no_expansion_getter_holds_a_threshold_of_its_own(self) -> None:
        # What separates "counting" from "judging". Every boundary these
        # getters consult was drawn, dated and published by the API; a numeric
        # literal here would be a second cut point with no date and no
        # provenance -- `band_provenance`'s failure mode, in the layout layer.
        for decl in ("get contextOpen(", "get opportunitiesOpen("):
            with self.subTest(decl=decl):
                self.assertNotRegex(
                    js_function_body(self.html, decl),
                    r"\d",
                    "a cut point is spelled in the page rather than read off "
                    "the payload",
                )

    def test_no_card_writes_an_answer_of_its_own(self) -> None:
        # THE class of defect this card has now shipped twice: prose typed into
        # a template is a claim nothing checks. The heading "And it only ever
        # grows." was true of the database it was written against and false of
        # the same database three hours later; "Most of it is in your X"
        # answered a question nobody asked. Both survived a green suite.
        #
        # So the sentences live in `serve.CONTEXT_ANSWER_STATEMENTS`, and two
        # things are pinned here: the page holds no copy of one (a copy is a
        # second place to change and only one of them is dated), and no card
        # opens an answer with a yes or a no of its own.
        for statement in CONTEXT_ANSWER_STATEMENTS.values():
            with self.subTest(statement=statement[:40]):
                self.assertNotIn(
                    statement[:40],
                    self.html,
                    "the page spells an answer the API already states",
                )
        self.assertNotIn("Most of it", self.html)
        for card_id, detail_id in self.CARDS:
            with self.subTest(card=card_id):
                self.assertNotRegex(
                    self.resting(card_id, detail_id),
                    r">\s*(Yes|No)[.,]",
                    f"{card_id} authors its own answer instead of rendering "
                    "the one the API decided",
                )

    def test_the_context_answer_wears_a_tone_for_every_state(self) -> None:
        # The word is what carries the verdict, and the tone is what a reader
        # sees before reading it -- a YES that looks exactly like a NO is the
        # health band's three-tone rule failing one card over. A table, for the
        # reason `HEALTH_TONE` is one, and the fallback is the loud tone.
        table = re.search(r"const CONTEXT_TONE = \{(.*?)\};", self.html, re.S)
        self.assertIsNotNone(table, "CONTEXT_TONE is gone")
        tones = dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', table.group(1)))
        self.assertEqual(set(tones), set(CONTEXT_ANSWER_STATES))
        self.assertNotEqual(
            tones[CONTEXT_ANSWER_YES],
            tones[CONTEXT_ANSWER_NO],
            "a proven yes and a measured no render in the same tone",
        )
        for state, css in tones.items():
            with self.subTest(state=state):
                self.assertRegex(self.html, rf"\.{re.escape(css)}\s*\{{[^}}]+\}}")
        self.assertIn('const CONTEXT_TONE_UNRECOGNISED = "tag-alarm";', self.html)
        self.assertIn(
            "?? CONTEXT_TONE_UNRECOGNISED",
            js_function_body(self.html, "get contextTone("),
        )

    def test_the_top_opportunity_reads_the_apis_order_rather_than_ranking(
        self,
    ) -> None:
        # `recommendations.py` refuses to build an `ok` reading WITH a lever or
        # a non-`ok` reading WITHOUT one, so "is there anything to do, and what
        # is it first" is the module's own answer. A comparison here would be a
        # second ranking, free to disagree with the one the card below renders.
        body = js_function_body(self.html, "get topOpportunity(")
        self.assertIn(
            "return this.summary.recommendations.ranked.find(a => a.lever) ?? null;",
            body,
        )
        for forbidden in (".sort(", "Math.max(", "SEVERITY", "severity >"):
            with self.subTest(expr=forbidden):
                self.assertNotIn(forbidden, body)

    def test_an_empty_list_of_moves_is_a_named_absence(self) -> None:
        # A healthy table produces no moves, and an empty `<ol>` says nothing
        # about why. Both cards state it in words, in the branch that would
        # otherwise carry the first move.
        for card_id in ("advice-note", "next-note"):
            with self.subTest(card=card_id):
                card = self.card(card_id)
                self.assertIn('x-if="topOpportunity === null"', card)
                self.assertIn('x-if="topOpportunity !== null"', card)

    def test_the_moves_are_a_numbered_list_naming_their_figures(self) -> None:
        # #88 asks for next steps as a numbered list, each naming the figure
        # that justifies it -- so the move and its evidence cannot be read
        # apart, and neither is prose this page composed.
        detail = html_element(self.raw, 'id="next-detail"')
        self.assertIn('<ol class="next">', detail)
        self.assertIn('x-text="a.lever.directive"', detail)
        for evidence in (
            'x-text="a.metric"', "fmtUnit(a.value, a.unit)", 'x-text="a.severity"'
        ):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, detail)
        self.assertRegex(self.html, r"ol\.next\s*\{[^}]+\}")

    def test_the_raw_token_deck_answers_nothing_and_sits_one_level_down(
        self,
    ) -> None:
        # "Input tokens 32.1k / Cache-read 443.47M / Cache-write 8.07M -- what
        # do I do with this?" is the reaction #62 was filed over. The deck is
        # an INPUT to the four answers, so it moved to where raw data lives.
        # Moved, not deleted: `SummaryPayloadIsWiredTest` still sees every one
        # of its figures, and `ReportViewSplitTest` sees the panel.
        details = view_section(self.raw, "details")
        overview = view_section(self.raw, "overview")
        self.assertIn('id="cards"', details)
        self.assertNotIn('id="cards"', overview)
        deck = js_function_body(self.html, "get cards(")
        for figure in ("input", "cache_read", "cache_write", "output", "sessions"):
            with self.subTest(figure=figure):
                self.assertIn(f"this.summary.{figure}", deck)


class ReportNamesTheBuildTest(unittest.TestCase):
    """A figure on this page can be traced to the build that computed it (#92).

    #21's last open acceptance criterion, and the cheapest thing in that issue:
    `grep -c VERSION serve.py index.html` returned **0 and 0**, so the report
    could not say which build produced a number and neither could a bug filed
    against it. Every corrected figure in this project's record -- the 2.36x
    dedupe fix, the mean displaced by the median -- is a number that changed
    between builds; a screenshot with no build on it is a number nobody can
    place.

    The rule that shapes the implementation is the SECOND copy, not the first.
    `cpb.VERSION` is the authority and the docs have drifted from it twice
    (1.0.0 -> 1.1.0, then 1.1.0 -> 1.2.0), which is why
    `DocsStateTheShippedVersionTest` exists. A literal in `serve.py` would
    drift the same way while looking authoritative in a payload, so the payload
    READS the constant per request and the tests below can move it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="cpb-build-version-test-"))
        projects = cls.tmp / "projects"
        projects.mkdir()
        shutil.copy(FIXTURE, projects / "session-fixture.jsonl")
        ingest(projects, cls.tmp / "usage.db")
        cls.api = Api(cls.tmp / "usage.db")
        cls.html = (Path(__file__).resolve().parent.parent / "index.html").read_text()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def payload(self) -> dict:
        return self.api.summary(*day_bounds(None, None))

    def test_the_summary_carries_the_shipped_version(self) -> None:
        self.assertEqual(self.payload()["build"]["version"], cpb.VERSION)

    def test_the_version_is_READ_from_cpb_and_not_restated(self) -> None:
        # THE test with teeth, and the mutation it survives is the obvious
        # implementation: `"build": {"version": "1.6.0"}`. Moving `cpb.VERSION`
        # must move the payload, so a literal anywhere in the chain fails here
        # rather than at the next release.
        with mock.patch.object(cpb, "VERSION", "99.98.97-test"):
            self.assertEqual(self.payload()["build"]["version"], "99.98.97-test")
        self.assertEqual(self.payload()["build"]["version"], cpb.VERSION)

    def test_a_build_that_cannot_be_read_is_unknown_not_a_guess(self) -> None:
        # `serve.py` is a supported entry point in its own right and needs no
        # `cpb.py` to run (`docs/versioning.md` clause 1). Where the constant
        # cannot be read the field is null -- absence, not a plausible string
        # that would send a bug report at the wrong build.
        with mock.patch.object(serve, "_cpb", None):
            self.assertIsNone(self.payload()["build"]["version"])
        self.assertEqual(self.payload()["build"]["version"], cpb.VERSION)

    def test_a_cpb_without_the_constant_is_also_unknown(self) -> None:
        # The same absence by a different route: an import that succeeded over
        # a module that carries no VERSION. `getattr` with a default must not
        # be the only thing standing between that and an AttributeError 500.
        class Nameless:
            pass

        with mock.patch.object(serve, "_cpb", Nameless()):
            self.assertIsNone(serve.cpb_version())

    def test_serve_restates_no_version_literal(self) -> None:
        # The static half. A copy of the shipped string in either file is the
        # drift this whole arrangement exists to prevent, and it would pass
        # every payload test above on the day it was written.
        for path in ("serve.py", "index.html"):
            with self.subTest(file=path):
                source = (Path(__file__).resolve().parent.parent / path).read_text()
                self.assertNotIn(
                    f'"{cpb.VERSION}"', source,
                    f"{path} restates the version instead of reading it",
                )

    def test_the_page_renders_the_build(self) -> None:
        page = strip_comments(self.html)
        self.assertIn("summary.build.version", page)
        self.assertIn("Build:", page)

    def test_the_page_states_UNKNOWN_for_a_build_it_cannot_read(self) -> None:
        # Absence is never rendered as a value, in the one field whose whole
        # job is to make a report checkable. A null that rendered as a blank
        # would read as "no build", which is a claim; UNKNOWN is the truth.
        page = strip_comments(self.html)
        self.assertIn("summary.build.version === null", page)
        self.assertIn("UNKNOWN", page)

    def test_the_build_is_chrome_and_is_rendered_once(self) -> None:
        # It qualifies every figure in every view, so it is rendered where the
        # banner and the data-age line are: outside all three levels, one
        # element, nothing to drift from.
        page = strip_comments(self.html)
        self.assertEqual(page.count("summary.build.version"), 3)
        for view in ("summary", "overview", "details"):
            with self.subTest(view=view):
                self.assertNotIn(
                    "summary.build.version", view_section(self.html, view)
                )


if __name__ == "__main__":
    unittest.main()
