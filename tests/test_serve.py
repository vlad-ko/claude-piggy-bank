"""Tests for the usage-report serving layer's date/limit helpers (#4948).

CodeRabbit finding: serve.py had no test coverage at all, and `day_bounds`
is the input to every filtered query -- a DST-boundary error there would
shift every chart silently. These tests pin the [start, end) half-open
contract, including across a DST transition, plus the limit-clamping and
reversed-range validation added in this cycle's review pass.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest import INGEST_RUNS_TABLE, ingest, record_ingest_run  # noqa: E402
from test_ingest import build_corpus  # noqa: E402
from serve import (  # noqa: E402
    RANKED_BY,
    STALE_AFTER_SECONDS,
    STALE_UNKNOWN_NO_RUN_RECORDED,
    STALE_UNKNOWN_NO_RUN_TABLE,
    STALE_UNKNOWN_RUN_IN_FUTURE,
    STATUS_ARCHIVED,
    Api,
    clamp_limit,
    day_bounds,
    eastern_day,
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
        for name in ("ingest.py", "serve.py", "index.html"):
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


class BannerPrecedenceTest(unittest.TestCase):
    """Three writers, one banner element: the loudest true thing must win (#20).

    `renderSummary` rebuilt the banner's whole text on every render and
    `reportLoadFailure` overwrote it wholesale, so the later writer decided
    which of two true statements the reader got -- and the staleness warning
    added here would have been a third. A user shown an unparsed-records notice
    while a load failure or a stale database goes unmentioned has been handed
    the milder fact with no sign the harsher one exists, which is this
    repository's core rule failing inside the feature built to enforce it.

    These assertions are STRUCTURAL, and that is a real limit, not a stylistic
    choice: the project ships no JS runtime (stdlib-only, no Node), so the
    composition cannot be executed here. They pin the two properties that make
    the defect unreachable -- one writer, and a fixed order -- and a renamed
    binding still defeats them. `DataStalenessTest` covers the same
    two-things-true-at-once case at the layer that CAN be executed.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (Path(__file__).resolve().parent.parent / "index.html").read_text()

    def test_the_banner_element_has_exactly_one_writer(self) -> None:
        writers = self.html.count('getElementById("banner")')
        self.assertEqual(
            writers,
            1,
            "every write to the banner goes through renderBanner(); a second "
            "direct writer is how one warning silently replaces another",
        )
        self.assertIn('getElementById("banner")', js_function_body(self.html, "function renderBanner("))

    def test_the_precedence_order_is_failure_then_stale_then_notices(self) -> None:
        body = js_function_body(self.html, "function renderBanner(")
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
        self.assertEqual(self.html.count("bannerState.loadFailure = null"), 1)
        self.assertIn(
            "bannerState.loadFailure = null",
            js_function_body(self.html, "async function loadAll("),
        )

    def test_only_a_true_staleness_verdict_raises_the_banner(self) -> None:
        # `stale` is tri-state; the null case ("no ingest run was ever
        # recorded") is an UNKNOWN age, and a banner it can never clear would
        # train the reader to ignore the one that means something.
        body = js_function_body(self.html, "function renderSummary(")
        self.assertIn("s.ingest.stale === true", body)

    def test_the_data_age_line_distinguishes_never_recorded_from_zero(self) -> None:
        # The BRANCH, not a mention: `=== 0` reads a never-recorded run as an
        # ingest at the epoch, and matching the bare substring elsewhere in the
        # function let exactly that mutation survive.
        body = js_function_body(self.html, "function renderDataAge(")
        self.assertRegex(body, r"if \(ing\.last_run_at === null\) \{")
        self.assertRegex(body, r"if \(ing\.newest_call_ts === null\) \{")
        self.assertIn("not recorded", body)


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
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (Path(__file__).resolve().parent.parent / "index.html").read_text()

    def data_age(self) -> str:
        return js_function_body(self.html, "function renderDataAge(")

    def summary(self) -> str:
        return js_function_body(self.html, "function renderSummary(")

    def test_the_data_age_line_branches_on_the_reason_the_api_reports(self) -> None:
        body = self.data_age()
        self.assertIn("ing.stale_unknown_reason", body)
        for reason in (STALE_UNKNOWN_NO_RUN_TABLE, STALE_UNKNOWN_NO_RUN_RECORDED):
            with self.subTest(reason=reason):
                self.assertIn(f'"{reason}"', body)

    def test_the_predates_claim_is_reachable_only_under_the_missing_table(
        self,
    ) -> None:
        # THE defect: the claim itself is fine, unconditionally making it is
        # not. Asserted as "appears once, after the guard that establishes it",
        # because a second copy outside the branch is exactly the regression.
        body = self.data_age()
        self.assertEqual(body.count("predates"), 1)
        self.assertLess(
            body.index(f'"{STALE_UNKNOWN_NO_RUN_TABLE}"'),
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
        self.assertIn("s.ingest.stale === true", body)
        self.assertEqual(body.count("bannerState.stale ="), 1)


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
    """

    NOT_RENDERED = {
        # The window's token totals by class are plotted from /api/timeseries,
        # which carries them per-day; the flat summary totals would be a second
        # expression of the same fact and are deliberately not shown.
        "input": "plotted per-day via /api/timeseries",
        "cache_read": "plotted per-day via /api/timeseries",
        "cache_write": "plotted per-day via /api/timeseries",
        "output": "plotted per-day via /api/timeseries",
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
    def _render_summary_body(html: str) -> str:
        """`renderSummary`'s body, comments stripped.

        Qodo, on this PR: searching the WHOLE file for `s.<field>` is spoofable
        by a mention in a comment or an unrelated string, and can also match a
        different function's local `s`. Narrowing to the one function that
        receives the summary payload, and stripping comments first, removes
        both. It stays a heuristic -- a renamed binding or destructuring still
        defeats it -- which is why the PR names the structural fix (declarative
        binding, i.e. the Alpine rewrite) as a separate change rather than
        claiming this test is a guarantee.
        """
        return js_function_body(html, "function renderSummary(")

    def test_every_summary_field_is_rendered_or_declared_unrendered(self) -> None:
        payload = self.api.summary(*day_bounds(None, None))
        body = self._render_summary_body(self.html)
        unwired = [
            k
            for k in payload
            if k not in self.NOT_RENDERED and f"s.{k}" not in body
        ]
        self.assertEqual(
            unwired,
            [],
            f"/api/summary computes {unwired} and index.html never reads them. "
            "Render the field, or add it to NOT_RENDERED with the reason.",
        )

    def test_the_allowlist_cannot_hide_a_field_that_no_longer_exists(self) -> None:
        # A stale exemption is how an allowlist rots into a rubber stamp: the
        # field is renamed, the entry stays, and the NEW name is unguarded
        # while the list still looks deliberate.
        payload = self.api.summary(*day_bounds(None, None))
        stale = [k for k in self.NOT_RENDERED if k not in payload]
        self.assertEqual(stale, [], f"NOT_RENDERED names absent fields: {stale}")


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
        # The pinning test the issue asks for: if a future harness release
        # changes what <subagent_tokens> counts, this goes red instead of the
        # meaning silently re-diverging. Ratios come from the DB so the
        # relationship is pinned at the source, not at the API's rounding.
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


if __name__ == "__main__":
    unittest.main()
