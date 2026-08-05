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
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest import ingest  # noqa: E402
from test_ingest import build_corpus  # noqa: E402
from pricing import RATES_AS_OF  # noqa: E402
from serve import Api, clamp_limit, day_bounds, eastern_day  # noqa: E402

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


class ApiCostFieldContractTest(unittest.TestCase):
    """The API must expose an ESTIMATE, never something a consumer could
    mistake for a bill (#4948 follow-up, product-owner direction 2026-08-02).
    Every cost figure crossing the `Api` JSON boundary is named
    `cost_estimate_usd` -- never bare `cost_usd` -- and `summary()` carries a
    `cost_basis` naming the rate table + its `RATES_AS_OF` date.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-serve-test-"))
        projects_dir = self.tmp / "projects"
        projects_dir.mkdir()
        shutil.copy(FIXTURE, projects_dir / "session-fixture.jsonl")
        self.db_path = self.tmp / "usage.db"
        ingest(projects_dir, self.db_path)
        self.api = Api(self.db_path)
        self.start, self.end = day_bounds(None, None)

    def tearDown(self) -> None:
        self.api.conn.close()
        shutil.rmtree(self.tmp)

    def test_summary_exposes_cost_estimate_usd_not_cost_usd(self) -> None:
        s = self.api.summary(self.start, self.end)
        self.assertIn("cost_estimate_usd", s)
        self.assertNotIn("cost_usd", s)
        self.assertGreater(s["cost_estimate_usd"], 0.0)

    def test_summary_cost_basis_names_the_rate_table_and_its_date(self) -> None:
        s = self.api.summary(self.start, self.end)
        self.assertIn("cost_basis", s)
        self.assertEqual(s["cost_basis"]["rates_as_of"], RATES_AS_OF)
        # The source string must actually name the table + say "not a bill" --
        # a consumer reading only cost_basis (no other context) must still be
        # able to tell this figure is an estimate.
        self.assertIn("pricing.py", s["cost_basis"]["source"])
        self.assertIn("not a bill", s["cost_basis"]["source"])

    def test_sessions_expose_cost_estimate_usd_not_cost_usd(self) -> None:
        rows = self.api.sessions(self.start, self.end)
        self.assertEqual(len(rows), 1)
        self.assertIn("cost_estimate_usd", rows[0])
        self.assertNotIn("cost_usd", rows[0])
        self.assertGreater(rows[0]["cost_estimate_usd"], 0.0)

    def test_session_detail_turn_types_and_models_expose_cost_estimate_usd(self) -> None:
        d = self.api.session_detail("session-fixture")
        self.assertGreater(len(d["turn_types"]), 0)
        self.assertGreater(len(d["models"]), 0)
        for row in d["turn_types"] + d["models"]:
            self.assertIn("cost_estimate_usd", row)
            self.assertNotIn("cost_usd", row)


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

    def test_agents_endpoint_ranks_by_spend_and_names_the_agent(self) -> None:
        rows = self.api.agents(self.start, self.end, 20)
        by_id = {r["agent_id"]: r for r in rows}
        self.assertEqual(by_id["atest1"]["agent_type"], "laravel-expert")
        self.assertEqual(by_id["atest1"]["description"], "Fix widget")
        self.assertEqual(by_id["atest1"]["calls"], 2)
        self.assertEqual(by_id["atest1"]["cache_read"], 4090)
        self.assertEqual(by_id["atest2"]["agent_type"], "architect")
        self.assertEqual(by_id["atest2"]["session_id"], "other-session")
        # Ranked by cache-read descending: atest1 (4090) before atest2 (7).
        ingested = [r["agent_id"] for r in rows if r["status"] == "ingested"]
        self.assertEqual(ingested, ["atest1", "atest2"])

    def test_agents_endpoint_lists_unavailable_runs_with_null_not_zero(self) -> None:
        rows = self.api.agents(self.start, self.end, 20)
        gone = [r for r in rows if r["agent_id"] == "agone"]
        self.assertEqual(len(gone), 1)
        self.assertEqual(gone[0]["status"], "unavailable")
        # The whole point: an unmeasured agent must NOT report 0 spend.
        self.assertIsNone(gone[0]["calls"])
        self.assertIsNone(gone[0]["cache_read"])
        self.assertIsNone(gone[0]["cost_estimate_usd"])

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
        start = html.index("function renderSummary(")
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
            raise AssertionError("could not find the end of renderSummary()")
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        return re.sub(r"^\s*//.*$", "", body, flags=re.M)

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
        self.assertGreater(row["cost_estimate_usd"], 0.0)

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
        self.assertIsNone(row["cost_estimate_usd"])
        self.assertEqual(row["dispatches_without_spend"], 1)

    def test_rows_rank_by_measured_spend_with_unmeasured_last(self) -> None:
        # Ranking by the self-reported peak put `reviewer` (480,000 reported,
        # zero measured spend) at the top of a panel a reader consults to see
        # what delegation cost.
        detail = self.api.session_detail(PEAK_SESSION)
        order = [r["agent_type"] for r in detail["agent_types"]]
        self.assertEqual(order, ["backend-expert", "architect", "reviewer"])

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


if __name__ == "__main__":
    unittest.main()
