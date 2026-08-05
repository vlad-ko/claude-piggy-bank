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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cpb  # noqa: E402
from ingest import (  # noqa: E402
    CACHE_CREATION_KEY,
    CACHE_WRITE_1H_KEY,
    CACHE_WRITE_5M_KEY,
    INGEST_RUNS_TABLE,
    SOURCE_MAIN,
    SOURCE_SUBAGENT,
    ingest,
    ingest_transcript,
    record_ingest_run,
)
from test_ingest import build_corpus  # noqa: E402
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
    SEVERITY_OK,
    SEVERITY_RANK,
    UNMEASURED_NOTE,
    Lever,
)
from serve import (  # noqa: E402
    CONTEXT_SAMPLE,
    MEASURED_CONTEXT_MIN,
    OVER_HALF_WINDOW_BANDS,
    PERCENTILES,
    RANKED_BY,
    RECOMMENDED_METRICS,
    SCOPE_INCLUDES_BOTH,
    SCOPE_MAIN,
    SCOPE_ORDER,
    SCOPE_SUBAGENT,
    STALE_AFTER_SECONDS,
    STALE_UNKNOWN_NO_RUN_RECORDED,
    STALE_UNKNOWN_NO_RUN_TABLE,
    STALE_UNKNOWN_RUN_IN_FUTURE,
    STATUS_ARCHIVED,
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

    def _set_run_stamp(self, finished_at: float) -> None:
        """Stamp the run through the ingester's own writer, never a raw INSERT."""
        conn = sqlite3.connect(self.db_path)
        try:
            record_ingest_run(conn, finished_at)
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


def iteration_aliases(surface: str, exprs) -> frozenset:
    """The loop variables the page binds a list's ELEMENTS to."""
    return frozenset(
        var for var, iterated in X_FOR.findall(surface) if iterates(surface, iterated, exprs)
    )


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
SCOPE_CONTEXT_MEAN_HAS_NO_READER = (
    "#4966/#25: the per-scope context MEAN and its sample counts have no "
    "reader. The context surface is the median card and #61's per-scope "
    "utilisation bands, which range over the same calls and say more about "
    "them. Declared by #76, the first check able to SEE it -- putting it on "
    "the page is a change of its own, not a line in this dict."
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
        "scope.main_thread.avg_context": SCOPE_CONTEXT_MEAN_HAS_NO_READER,
        "scope.main_thread.context_calls": SCOPE_CONTEXT_MEAN_HAS_NO_READER,
        "scope.main_thread.unmeasured_calls": SCOPE_CONTEXT_MEAN_HAS_NO_READER,
        "scope.subagent.input": SCOPE_SPLIT_IS_PLOTTED,
        "scope.subagent.cache_read": SCOPE_SPLIT_IS_PLOTTED,
        "scope.subagent.cache_write": SCOPE_SPLIT_IS_PLOTTED,
        "scope.subagent.output": SCOPE_SPLIT_IS_PLOTTED,
        "scope.subagent.avg_context": SCOPE_CONTEXT_MEAN_HAS_NO_READER,
        "scope.subagent.context_calls": SCOPE_CONTEXT_MEAN_HAS_NO_READER,
        "scope.subagent.unmeasured_calls": SCOPE_CONTEXT_MEAN_HAS_NO_READER,
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

    def test_moving_a_field_between_views_does_not_widen_the_register(self) -> None:
        # #70 splits the page into an overview and a detail view. A field that
        # MOVES between them is still rendered, so the register must not grow
        # to cover one -- an entry added by a layout change would be a field
        # quietly dropped from the report while the allowlist made it look
        # decided. Twenty-one is the count #76 found and declared; the healthy
        # direction is down.
        self.assertLessEqual(
            len(self.NOT_RENDERED),
            21,
            "a field that stopped being rendered was exempted rather than "
            "re-homed. Moving a panel between views does not orphan a field.",
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
        loops = re.findall(r'<template x-for="([^"]+)"', self.band)
        self.assertEqual(
            [loop for loop in loops if "bands" in loop],
            ["b in s.bands", "b in summary.context.utilisation.bands"],
            "a band list is built some other way, or one of the two is gone",
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
        for field in ("scope", "banded_calls", "calls", "sample_calls"):
            with self.subTest(field=field):
                self.assertIn(f"s.{field}", self.scope_loop)

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
            self.scope_loop.index("b in s.bands"),
            "the bands are drawn outside the branch that establishes a sample",
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
        # CLAUDE.md constraint 4. This earned its space by DISPLACING the mean
        # in the card above it; it may not also append a surface. It reuses the
        # `note-band` component the scope note already uses, and adds no table.
        self.assertRegex(self.raw, r'<div class="note-band" id="context-note">')
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
REC_CALLS: list[tuple[str, str, str, int, int, int, int, Optional[dict]]] = [
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

# Hand-written from the table above, then checked against it by
# `test_the_fixture_holds_what_the_expectations_claim`. Derived-only
# expectations would agree with a fixture that had drifted.
REC_FULL_MAIN_CALLS = 5
REC_FULL_MAIN_TOTAL = 2_000_000
REC_FULL_MAIN_BANDED = 5
REC_FULL_MAIN_OVER_HALF = 2
REC_FULL_SUB_CALLS = 3
REC_FULL_SUB_TOTAL = 58_700
REC_FULL_CACHE_READS = 1_525_000
REC_FULL_CACHE_WRITES = 422_100
REC_FULL_WRITING_CALLS = 7
REC_FULL_WRITE_ONLY_CALLS = 2
# The per-TTL metric's set (#84): the three full-day calls carrying BOTH TTL
# keys, and the reads from those same three. Every figure here is hand-written
# from the table above and checked against it below.
REC_FULL_SPLIT_CALLS = 3
REC_FULL_SPLIT_READS = 541_000        # 470,000 + 64,000 + 7,000
REC_FULL_SPLIT_5M = 109_000           # 100,000 + 3,000 + 6,000
REC_FULL_SPLIT_1H = 40_000            # 20,000 + 20,000 + 0
# HAND-WRITTEN, not derived from the module's weights: 1 x 109,000 + 2 x 40,000.
# An expectation computed from `READ_TOKENS_TO_REPAY_A_*` would move with them,
# so a release that swapped the two multipliers would pass every assertion below
# -- the "fixture that makes the defect undetectable" failure, arriving through
# a constant instead of through a value. The weights are checked AGAINST this
# literal in `test_the_fixtures_split_holds_what_the_expectations_claim`.
REC_FULL_REQUIRED = 189_000
# 541,000 / 189,000 = 2.8624...
# Deliberately unlike its four neighbours, none of which may reproduce it:
#   whole-window reads over the same denominator  1,525,000/189,000 = 8.069
#   the weights swapped                             541,000/258,000 = 2.097
#   the half-split call counted as 5-minute only    541,000/195,100 = 2.773
#   the flat reads-per-write ratio                1,525,000/422,100 = 3.613
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
        self.assertEqual(len(half), 1)
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

    def test_every_metric_in_the_table_is_assessed_or_named_unmeasured(self) -> None:
        # `assess_all()` refuses a partial mapping, so a forgotten metric would
        # raise rather than pass -- but only if `_recommendations()` keeps
        # passing every key. This asserts the state that proves it did: the two
        # halves of the payload partition `METRICS` exactly, on every window.
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
                self.assertEqual(ranked | unmeasured, set(METRICS))
                self.assertEqual(ranked & unmeasured, set())
                # And against what `serve` DECLARES it computes (#84). The
                # import-time guard compares that declaration with the table;
                # this compares it with what a served payload actually holds,
                # so a declaration that had drifted from the mapping below it
                # cannot pass by agreeing with the table alone.
                self.assertEqual(ranked | unmeasured, set(RECOMMENDED_METRICS))

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
        lower = self.reading(REC_SOLO_DAY, METRIC_CACHE_WRITE_ONLY_SHARE)[
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
        # Constraint 4. `note-band` is this page's idiom for annotating the
        # figures above it -- `#scope-note` and `#context-note` are the two that
        # came first -- and a `<table>` here would be the appended surface the
        # constraint names.
        self.assertIn('class="note-band"', self.band)
        self.assertNotIn("<table", self.band)


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
    # The affordance itself: one click there, one click back, from either view.
    "view-tabs",
}
OVERVIEW_PANELS = {"overview-period", "cards", "scope-note", "context-note", "advice-note"}
DETAIL_PANELS = {
    "filters", "chart-panel", "models", "detail", "sessions", "agents", "outliers",
}

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


class ReportViewSplitTest(unittest.TestCase):
    """The overview is the front door; the detail view keeps every panel (#70).

    The owner's decision, taken before this was built: the overview is the
    landing view and the details view is one click away. It costs the heaviest
    reader -- the person who has been reading the tables all along -- one extra
    click, forever, which is why two of these tests are load-bearing rather
    than tidy. One click there and one click BACK, or the extra click compounds
    instead of being paid once. And the view is in the URL, or anyone who has
    bookmarked this report loses their bookmark silently.
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (cls.ROOT / "index.html").read_text()
        cls.html = strip_comments(cls.raw)
        cls.overview = view_section(cls.raw, "overview")
        cls.details = view_section(cls.raw, "details")

    def component(self, decl: str) -> str:
        return js_function_body(self.html, decl)

    # --- the landing view and the way back ---------------------------------

    def test_the_overview_is_the_landing_view(self) -> None:
        # The DEFAULT, not merely a reachable state: a page that opens on the
        # details view has not been split, it has been rearranged.
        component = self.component("function report(")
        self.assertRegex(component, re.compile(r'^\s*view: DEFAULT_VIEW,$', re.M))
        self.assertRegex(
            self.html, r'const DEFAULT_VIEW = "overview";'
        )
        self.assertIn("""x-show="view === 'overview'\"""", self.raw)
        self.assertIn("""x-show="view === 'details'\"""", self.raw)

    def test_one_click_reaches_details_and_one_click_returns(self) -> None:
        # Symmetry is the whole point. The tabs sit in NEITHER section, so both
        # are on screen whichever view is showing -- a "back" that is reachable
        # only from the top of a long scrolled page is not one click.
        tabs = html_element(self.raw, 'id="view-tabs"')
        for view in ("overview", "details"):
            with self.subTest(view=view):
                self.assertIn(f"setView('{view}')", tabs)
        self.assertNotIn('id="view-tabs"', self.overview)
        self.assertNotIn('id="view-tabs"', self.details)

    # --- nothing is removed ------------------------------------------------

    def test_every_panel_survives_in_a_named_view(self) -> None:
        # THE acceptance criterion: every panel on the report before the split
        # is still on it, in a named place. A dropped detail panel is red here
        # and nowhere else -- the wiring guard would not see it, because the
        # payload field it renders may well still be read somewhere.
        for panel in sorted(OVERVIEW_PANELS):
            with self.subTest(panel=panel, view="overview"):
                self.assertIn(f'id="{panel}"', self.overview)
                self.assertNotIn(f'id="{panel}"', self.details)
        for panel in sorted(DETAIL_PANELS):
            with self.subTest(panel=panel, view="details"):
                self.assertIn(f'id="{panel}"', self.details)
                self.assertNotIn(f'id="{panel}"', self.overview)

    def test_each_panel_exists_exactly_once_in_the_page(self) -> None:
        # A panel duplicated into both views is the disagreement this issue is
        # about, in its most literal form.
        for panel in sorted(CHROME_PANELS | OVERVIEW_PANELS | DETAIL_PANELS):
            with self.subTest(panel=panel):
                self.assertEqual(self.html.count(f'id="{panel}"'), 1)

    def test_the_shared_chrome_belongs_to_neither_view(self) -> None:
        # The strongest form of "cannot disagree": not two bindings kept equal,
        # but ONE element that both views are looking at.
        for panel in sorted(CHROME_PANELS):
            with self.subTest(panel=panel):
                self.assertIn(f'id="{panel}"', self.html)
                self.assertNotIn(f'id="{panel}"', self.overview)
                self.assertNotIn(f'id="{panel}"', self.details)

    # --- the two views cannot disagree -------------------------------------

    def test_no_summary_field_is_bound_in_both_views(self) -> None:
        # A field read in both views is two renderings of one figure with a
        # navigation step between them, and nothing keeps them equal. The
        # remedy is not "keep them in sync": it is one getter, named once, so
        # that there is one definition to be right or wrong.
        #
        # Over the view SURFACE, not the markup: the six summary cards are
        # composed in a getter, and a detail panel that re-rendered one of
        # their figures would be invisible to a check that read templates only.
        both = (
            summary_paths(view_surface(self.raw, self.overview))
            & summary_paths(view_surface(self.raw, self.details))
        )
        self.assertEqual(
            sorted(both),
            [],
            f"{sorted(both)} is bound in BOTH views. Two renderings of one "
            "figure can drift; route it through a single named getter instead.",
        )

    def test_the_period_is_named_by_one_binding_in_both_views(self) -> None:
        # The one figure both views genuinely need, and therefore the test case
        # for the rule above: the overview names the period it describes, the
        # detail view opens on the same one, and there is ONE definition.
        self.assertEqual(self.html.count("get periodLabel("), 1)
        for view, section in (("overview", self.overview), ("details", self.details)):
            with self.subTest(view=view):
                self.assertIn("periodLabel", section)

    def test_neither_view_composes_a_period_of_its_own(self) -> None:
        # How the rule above would be defeated: not by binding the same path
        # twice, but by rebuilding the same sentence out of the range state.
        # The picker owns `from`/`to`/`range`; everything else asks
        # `periodLabel`.
        for expr in BINDING_ATTR.findall(self.overview):
            with self.subTest(binding=expr):
                self.assertNotRegex(expr, r"\b(from|to|range)\b")
        picker = html_element(self.raw, 'id="filters"')
        for expr in BINDING_ATTR.findall(self.details.replace(picker, "")):
            with self.subTest(binding=expr):
                self.assertNotRegex(expr, r"\b(from|to)\b")

    def test_a_figure_unmeasured_in_one_view_is_unmeasured_in_the_other(self) -> None:
        # Absence must not become a value by changing level, so both views run
        # every figure through the SAME formatters -- the ones
        # `AbsenceIsNeverRenderedAsAValueTest` pins as answering absence before
        # they compute. A second copy of one would be a second rule.
        for formatter in ("fmtTok", "fmtCount", "fmtPct"):
            with self.subTest(formatter=formatter):
                self.assertEqual(self.html.count(f"function {formatter}("), 1)
        self.assertIn("fmtTok(", self.overview)
        self.assertIn("fmtTok(", self.details)

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

    def test_an_unknown_view_in_the_url_falls_back_to_the_overview(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
