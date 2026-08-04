"""Tests for the usage-report ingest pipeline and pricing table (#4948).

Fixture design note (CLAUDE.md "fixture must not make the defect undetectable"):
every token class in the fixture carries a DELIBERATELY UNEQUAL value, so a
swapped column mapping (e.g. cache_read <-> cache_write) cannot pass.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ingest as ingest_mod  # noqa: E402
from ingest import (  # noqa: E402
    available_projects,
    default_projects_dir,
    ingest,
    parse_file,
    transcript_slug,
)
from pricing import RATES_AS_OF, cost_usd, rates_for_model  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE = FIXTURES / "session-fixture.jsonl"
SUBAGENT_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "subagent-transcript.jsonl"
)


class IngestTest(unittest.TestCase):
    """End-to-end ingest of the hand-built fixture transcript."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-test-"))
        self.projects_dir = self.tmp / "projects"
        self.projects_dir.mkdir()
        shutil.copy(FIXTURE, self.projects_dir / "session-fixture.jsonl")
        self.db_path = self.tmp / "usage.db"
        self.summary = ingest(self.projects_dir, self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp)

    def q1(self, sql: str, *params) -> sqlite3.Row:
        row = self.conn.execute(sql, params).fetchone()
        self.assertIsNotNone(row, f"query returned no row: {sql}")
        return row

    def test_per_class_token_totals_are_exact(self) -> None:
        row = self.q1(
            "SELECT SUM(input_tokens) i, SUM(cache_read) cr, "
            "SUM(cache_write) cw, SUM(output_tokens) o, COUNT(*) n FROM api_calls"
        )
        self.assertEqual(row["n"], 5)  # 4 real + 1 synthetic
        self.assertEqual(row["i"], 118)   # 100+10+1+0+7
        self.assertEqual(row["cw"], 233)  # 200+20+2+0+11
        self.assertEqual(row["cr"], 456)  # 400+40+3+0+13
        self.assertEqual(row["o"], 76)    # 50+5+4+0+17

    def test_context_size_is_input_plus_cache_write_plus_cache_read(self) -> None:
        row = self.q1(
            "SELECT context_size FROM api_calls WHERE model = 'claude-sonnet-5-20260115' "
            "ORDER BY ts LIMIT 1"
        )
        self.assertEqual(row["context_size"], 700)  # 100 + 200 + 400

    def test_turn_counts_by_type(self) -> None:
        rows = self.conn.execute(
            "SELECT turn_type, COUNT(*) n FROM turns GROUP BY turn_type"
        ).fetchall()
        by_type = {r["turn_type"]: r["n"] for r in rows}
        self.assertEqual(
            by_type, {"human": 1, "task-notification": 1, "system-reminder": 1}
        )

    def test_tool_result_only_user_record_is_not_a_turn(self) -> None:
        # 3 turns total: the tool_result round-trip must not have created one.
        self.assertEqual(
            self.q1("SELECT COUNT(*) n FROM turns")["n"], 3
        )

    def test_api_calls_attributed_to_most_recent_turn(self) -> None:
        human_turn = self.q1("SELECT id FROM turns WHERE turn_type = 'human'")
        n = self.q1(
            "SELECT COUNT(*) n FROM api_calls WHERE turn_id = ?", human_turn["id"]
        )["n"]
        # sonnet call, Agent tool_use call, sidechain fable call
        self.assertEqual(n, 3)

    def test_sidechain_flag_stored(self) -> None:
        row = self.q1("SELECT is_sidechain FROM api_calls WHERE model = 'claude-fable-5'")
        self.assertEqual(row["is_sidechain"], 1)

    def test_priced_call_cost(self) -> None:
        row = self.q1(
            "SELECT cost_usd FROM api_calls WHERE model = 'claude-sonnet-5-20260115' "
            "ORDER BY ts LIMIT 1"
        )
        # 100*2.00 + 400*0.20 + 200*2.50 + 50*10.00, all per MTok (introductory rates)
        self.assertAlmostEqual(row["cost_usd"], 0.00128, places=9)

    def test_unknown_model_cost_is_null_not_zero(self) -> None:
        row = self.q1("SELECT cost_usd FROM api_calls WHERE model = 'mystery-model-9'")
        self.assertIsNone(row["cost_usd"])

    def test_synthetic_record_stored_with_zero_usage(self) -> None:
        row = self.q1("SELECT context_size FROM api_calls WHERE model = '<synthetic>'")
        self.assertEqual(row["context_size"], 0)

    def test_agent_dispatch_joined_via_tool_use_id(self) -> None:
        row = self.q1("SELECT * FROM agent_dispatches")
        self.assertEqual(row["task_id"], "task123")
        self.assertEqual(row["agent_type"], "laravel-expert")
        self.assertEqual(row["description"], "Fix widget")
        self.assertEqual(row["subagent_tokens"], 7500)
        self.assertEqual(
            self.q1("SELECT COUNT(*) n FROM agent_dispatches")["n"], 1
        )

    def test_unparsed_records_counted_not_swallowed(self) -> None:
        row = self.q1("SELECT unparsed_records FROM ingest_state")
        self.assertEqual(row["unparsed_records"], 1)
        self.assertEqual(self.summary["unparsed_records"], 1)

    def test_unparsed_record_carries_diagnostic_detail(self) -> None:
        # Qodo finding: unparsed_records must be actionable, not just a count.
        parsed = parse_file(self.projects_dir / "session-fixture.jsonl")
        self.assertEqual(len(parsed.unparsed_details), 1)
        detail = parsed.unparsed_details[0]
        self.assertIn("session-fixture.jsonl:11", detail)
        self.assertIn("not json at all", detail)

    def test_ingest_summary_carries_unparsed_details(self) -> None:
        # CodeRabbit finding: ingest() must not print diagnostics as a side
        # effect (a library function should not do I/O) -- it accumulates
        # them into the returned summary instead, for main() to print.
        self.assertEqual(len(self.summary["unparsed_details"]), 1)
        detail = self.summary["unparsed_details"][0]
        self.assertIn("session-fixture.jsonl:11", detail)
        self.assertIn("not json at all", detail)

    def test_missing_projects_dir_raises_instead_of_silent_empty_run(self) -> None:
        # CodeRabbit finding: Path.glob on a nonexistent dir yields nothing
        # and must not be misread as "files scanned: 0" with no cause.
        missing = self.tmp / "does-not-exist"
        with self.assertRaises(SystemExit):
            ingest(missing, self.db_path)

    def test_malformed_present_token_value_is_unparsed_not_zero(self) -> None:
        # Rule #12: a PRESENT non-numeric usage value must never coerce to 0
        # (that would make a shape failure indistinguishable from a real
        # measured-zero token count).
        custom = self.projects_dir / "malformed-usage.jsonl"
        custom.write_text(
            '{"type":"assistant","sessionId":"malformed-usage",'
            '"timestamp":"2026-07-28T15:00:00.000Z","message":'
            '{"model":"claude-sonnet-5-20260115",'
            '"usage":{"input_tokens":"oops","output_tokens":5}}}\n'
        )
        parsed = parse_file(custom)
        self.assertEqual(len(parsed.calls), 0)  # never stored with a fabricated 0
        self.assertEqual(parsed.unparsed_records, 1)
        self.assertEqual(parsed.records_parsed, 0)

    def test_boolean_usage_value_is_unparsed_not_coerced(self) -> None:
        # bool is a subclass of int in Python: int(True) == 1 would silently
        # coerce a malformed boolean into a plausible-looking token count
        # (a str like "oops" is caught by int() alone; a bool is not --
        # this pins the explicit isinstance(value, bool) guard specifically).
        custom = self.projects_dir / "boolean-usage.jsonl"
        custom.write_text(
            '{"type":"assistant","sessionId":"boolean-usage",'
            '"timestamp":"2026-07-28T15:00:00.000Z","message":'
            '{"model":"claude-sonnet-5-20260115",'
            '"usage":{"input_tokens":true,"output_tokens":5}}}\n'
        )
        parsed = parse_file(custom)
        self.assertEqual(len(parsed.calls), 0)
        self.assertEqual(parsed.unparsed_records, 1)

    def test_missing_token_key_is_a_genuine_zero(self) -> None:
        # A key ABSENT from usage (no cache write happened) is a real 0,
        # distinct from a present-but-malformed value.
        custom = self.projects_dir / "no-cache-write.jsonl"
        custom.write_text(
            '{"type":"assistant","sessionId":"no-cache-write",'
            '"timestamp":"2026-07-28T15:00:00.000Z","message":'
            '{"model":"claude-sonnet-5-20260115",'
            '"usage":{"input_tokens":100,"output_tokens":5}}}\n'
        )
        parsed = parse_file(custom)
        self.assertEqual(len(parsed.calls), 1)
        self.assertEqual(parsed.calls[0].cache_write, 0)
        self.assertEqual(parsed.unparsed_records, 0)

    def test_malformed_tool_use_input_does_not_double_count(self) -> None:
        # CodeRabbit finding: a non-dict tool_use "input" must not raise
        # AFTER the ApiCall is already appended (that would both store the
        # call as parsed AND count it as unparsed).
        custom = self.projects_dir / "malformed-tool-use.jsonl"
        custom.write_text(
            '{"type":"assistant","sessionId":"malformed-tool-use",'
            '"timestamp":"2026-07-28T15:00:00.000Z","message":'
            '{"model":"claude-sonnet-5-20260115",'
            '"usage":{"input_tokens":100,"output_tokens":5},'
            '"content":[{"type":"tool_use","id":"toolu_bad","name":"Agent",'
            '"input":"not-a-dict"}]}}\n'
        )
        parsed = parse_file(custom)
        self.assertEqual(len(parsed.calls), 1)
        self.assertEqual(parsed.unparsed_records, 0)
        self.assertEqual(parsed.records_parsed, 1)

    def test_deleted_transcript_is_pruned_only_when_pruning_is_ASKED_FOR(self) -> None:
        # Originally this asserted that a vanished transcript is pruned on the
        # NEXT ingest, unconditionally -- the Qodo finding that stale rows must
        # not linger in the UI. That premise was right and its default was
        # wrong: it could not tell a deliberate delete from Claude Code reaping
        # the file on its 30-day schedule, so every run destroyed measurements
        # whose source no longer existed to re-read.
        #
        # The intent is preserved and moved behind the explicit flag: pruning
        # still removes every row for that source, per-file, exactly as before.
        # What changed is that you have to ask.
        path = self.projects_dir / "session-fixture.jsonl"
        path.unlink()
        summary2 = ingest(self.projects_dir, self.db_path, prune_missing=True)
        self.assertEqual(summary2["files_pruned"], 1)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM sessions")["n"], 0)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM api_calls")["n"], 0)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM turns")["n"], 0)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM agent_dispatches")["n"], 0)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM ingest_state")["n"], 0)

    def test_session_row(self) -> None:
        row = self.q1("SELECT * FROM sessions WHERE id = 'session-fixture'")
        self.assertGreater(row["last_ts"], row["first_ts"])

    def test_ingest_is_idempotent(self) -> None:
        before = self.q1(
            "SELECT (SELECT COUNT(*) FROM api_calls) a, (SELECT COUNT(*) FROM turns) t, "
            "(SELECT COUNT(*) FROM agent_dispatches) d"
        )
        summary2 = ingest(self.projects_dir, self.db_path)
        after = self.q1(
            "SELECT (SELECT COUNT(*) FROM api_calls) a, (SELECT COUNT(*) FROM turns) t, "
            "(SELECT COUNT(*) FROM agent_dispatches) d"
        )
        self.assertEqual(tuple(before), tuple(after))
        # Unchanged file must be SKIPPED, not re-parsed.
        self.assertEqual(summary2["files_skipped"], 1)
        self.assertEqual(summary2["files_ingested"], 0)
        self.assertEqual(summary2["records_parsed"], 0)

    def test_changed_file_is_reparsed_without_duplication(self) -> None:
        path = self.projects_dir / "session-fixture.jsonl"
        with open(path, "a") as fh:
            fh.write(
                '{"type":"user","sessionId":"session-fixture",'
                '"timestamp":"2026-07-28T16:00:00.000Z",'
                '"message":{"role":"user","content":"another human turn"}}\n'
            )
        summary2 = ingest(self.projects_dir, self.db_path)
        self.assertEqual(summary2["files_ingested"], 1)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM turns")["n"], 4)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM api_calls")["n"], 5)


class SubagentTranscriptIngestTest(unittest.TestCase):
    """#4966: subagent (sidechain) API calls live in a SEPARATE transcript,
    `<projects-dir>/<session-id>/subagents/agent-<id>.jsonl` -- NOT in the
    main-thread `<session-id>.jsonl`. The ingester globbed only `*.jsonl` at
    the top level, so it never opened them and `is_sidechain` was 0 on every
    row while the tool presented main-thread figures as session TOTALS.

    The pre-existing `test_sidechain_flag_stored` could not catch this: its
    fixture put an `isSidechain:true` record INSIDE the main transcript, a
    shape that does not occur on disk (CLAUDE.md "a fixture must not make the
    defect undetectable"). This class reproduces the REAL directory layout.

    Token values are deliberately unequal per class AND deliberately different
    from the main-thread fixture's, so main/subagent figures cannot be
    confused for one another and a swapped column mapping cannot pass.
    """

    # main fixture: input 118, cache_write 233, cache_read 456, output 76 (5 calls)
    MAIN = {"i": 118, "cw": 233, "cr": 456, "o": 76, "n": 5}
    # subagent fixture: 1000+30, 2000+60, 4000+90, 500+120 (2 calls)
    SUB = {"i": 1030, "cw": 2060, "cr": 4090, "o": 620, "n": 2}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-subagent-test-"))
        self.projects_dir = self.tmp / "projects"
        self.projects_dir.mkdir()
        shutil.copy(FIXTURE, self.projects_dir / "session-fixture.jsonl")
        self.subagents_dir = self.projects_dir / "session-fixture" / "subagents"
        self.subagents_dir.mkdir(parents=True)
        self.subagent_path = self.subagents_dir / "agent-atest1.jsonl"
        shutil.copy(SUBAGENT_FIXTURE, self.subagent_path)
        self.db_path = self.tmp / "usage.db"
        self.summary = ingest(self.projects_dir, self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp)

    def q1(self, sql: str, *params) -> sqlite3.Row:
        row = self.conn.execute(sql, params).fetchone()
        self.assertIsNotNone(row, f"query returned no row: {sql}")
        return row

    def test_subagent_transcript_is_discovered_and_ingested(self) -> None:
        # The whole defect in one assertion: 2 files scanned, both ingested.
        self.assertEqual(self.summary["files_scanned"], 2)
        self.assertEqual(self.summary["files_ingested"], 2)
        self.assertEqual(self.summary["subagent_files_ingested"], 1)

    def test_sidechain_calls_are_flagged_and_counted(self) -> None:
        row = self.q1(
            "SELECT SUM(is_sidechain) side, COUNT(*) n FROM api_calls"
        )
        # 5 main-thread + 2 subagent calls; the 2 subagent calls are sidechain.
        # (the main fixture's own inline isSidechain record is NOT counted as a
        # separate source -- it is one of the 5 and is flagged in place)
        self.assertEqual(row["n"], self.MAIN["n"] + self.SUB["n"])
        self.assertEqual(
            self.q1(
                "SELECT COUNT(*) n FROM api_calls WHERE source_kind = 'subagent'"
            )["n"],
            self.SUB["n"],
        )
        self.assertEqual(
            self.q1(
                "SELECT COUNT(*) n FROM api_calls"
                " WHERE source_kind = 'subagent' AND is_sidechain = 0"
            )["n"],
            0,
        )

    def test_subagent_calls_are_attributed_to_the_PARENT_session(self) -> None:
        # A subagent transcript's own filename is `agent-<id>` -- attributing
        # on `path.stem` would invent a session that never existed and split
        # the session's spend in two.
        rows = self.conn.execute(
            "SELECT DISTINCT session_id FROM api_calls"
        ).fetchall()
        self.assertEqual([r["session_id"] for r in rows], ["session-fixture"])
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM sessions")["n"], 1)

    def test_totals_include_subagent_tokens(self) -> None:
        row = self.q1(
            "SELECT SUM(input_tokens) i, SUM(cache_read) cr,"
            " SUM(cache_write) cw, SUM(output_tokens) o FROM api_calls"
        )
        self.assertEqual(row["i"], self.MAIN["i"] + self.SUB["i"])
        self.assertEqual(row["cw"], self.MAIN["cw"] + self.SUB["cw"])
        self.assertEqual(row["cr"], self.MAIN["cr"] + self.SUB["cr"])
        self.assertEqual(row["o"], self.MAIN["o"] + self.SUB["o"])

    def test_main_and_subagent_totals_stay_separable(self) -> None:
        # The point of the column: a session total and a main-thread figure
        # must be tellable apart AFTER the fix, not merged into one number.
        main = self.q1(
            "SELECT SUM(input_tokens) i, SUM(cache_read) cr, SUM(cache_write) cw,"
            " SUM(output_tokens) o, COUNT(*) n FROM api_calls"
            " WHERE source_kind = 'main'"
        )
        sub = self.q1(
            "SELECT SUM(input_tokens) i, SUM(cache_read) cr, SUM(cache_write) cw,"
            " SUM(output_tokens) o, COUNT(*) n FROM api_calls"
            " WHERE source_kind = 'subagent'"
        )
        self.assertEqual(
            (main["n"], main["i"], main["cw"], main["cr"], main["o"]),
            (self.MAIN["n"], self.MAIN["i"], self.MAIN["cw"], self.MAIN["cr"],
             self.MAIN["o"]),
        )
        self.assertEqual(
            (sub["n"], sub["i"], sub["cw"], sub["cr"], sub["o"]),
            (self.SUB["n"], self.SUB["i"], self.SUB["cw"], self.SUB["cr"],
             self.SUB["o"]),
        )

    def test_subagent_tool_result_roundtrip_is_not_a_turn(self) -> None:
        # The subagent fixture's only user record is a tool_result round-trip;
        # it must not inflate the session's turn count (still the main
        # transcript's 3).
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM turns")["n"], 3)

    def test_reingest_is_idempotent_across_both_sources(self) -> None:
        before = tuple(self.q1("SELECT COUNT(*) n, SUM(cache_read) cr FROM api_calls"))
        summary2 = ingest(self.projects_dir, self.db_path)
        after = tuple(self.q1("SELECT COUNT(*) n, SUM(cache_read) cr FROM api_calls"))
        self.assertEqual(before, after)
        self.assertEqual(summary2["files_skipped"], 2)
        self.assertEqual(summary2["files_ingested"], 0)

    def test_changed_subagent_file_does_not_destroy_main_thread_rows(self) -> None:
        # The delete scope must be the FILE, not the session: both sources
        # share one session_id, so a session-scoped delete would wipe the main
        # thread's rows every time a subagent transcript grew.
        with open(self.subagent_path, "a") as fh:
            fh.write(
                '{"type":"assistant","sessionId":"session-fixture",'
                '"agentId":"agent-atest1","timestamp":"2026-07-28T15:03:00.000Z",'
                '"isSidechain":true,"message":{"model":"claude-sonnet-5-20260115",'
                '"usage":{"input_tokens":7,"output_tokens":9}}}\n'
            )
        summary2 = ingest(self.projects_dir, self.db_path)
        self.assertEqual(summary2["files_ingested"], 1)
        self.assertEqual(
            self.q1(
                "SELECT COUNT(*) n FROM api_calls WHERE source_kind = 'main'"
            )["n"],
            self.MAIN["n"],
        )
        self.assertEqual(
            self.q1(
                "SELECT COUNT(*) n FROM api_calls WHERE source_kind = 'subagent'"
            )["n"],
            self.SUB["n"] + 1,
        )
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM turns")["n"], 3)

    def test_deleted_subagent_file_prunes_only_its_own_rows(self) -> None:
        # The per-FILE scoping this protects is unchanged and still matters:
        # a session has one main transcript plus N subagent transcripts sharing
        # a session_id, so the delete must never widen to the session. Only the
        # trigger moved behind the explicit flag (see the sibling test above).
        self.subagent_path.unlink()
        summary2 = ingest(self.projects_dir, self.db_path, prune_missing=True)
        self.assertEqual(summary2["files_pruned"], 1)
        self.assertEqual(
            self.q1("SELECT COUNT(*) n FROM api_calls")["n"], self.MAIN["n"]
        )
        # The session itself survives -- only the subagent source went away.
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM sessions")["n"], 1)

    def test_session_row_spans_both_sources(self) -> None:
        row = self.q1("SELECT * FROM sessions WHERE id = 'session-fixture'")
        # size_bytes is the session's whole on-disk footprint, both sources.
        self.assertEqual(
            row["size_bytes"],
            (self.projects_dir / "session-fixture.jsonl").stat().st_size
            + self.subagent_path.stat().st_size,
        )
        self.assertGreater(row["last_ts"], row["first_ts"])

    def test_absent_empty_and_populated_subagent_trees_are_three_cases(self) -> None:
        """Rule #12: a real 0 stays distinguishable from "not measured".

        Three cases, and the separable signal asserted at the layer that
        actually carries it. An earlier version of this test claimed to check
        "task-index scanned-ness" while passing no `tasks_dir` at all and
        querying `ingest_state` -- the same subagent-file bookkeeping the
        counters above already assert. A regression collapsing absent and
        empty would have passed it (CodeRabbit). `task_index_sessions` is the
        table that separates them, so that is what is queried, and each case
        now gets a real task index.
        """
        def corpus(name: str, *, subagents_tree: bool, tasks_tree: bool) -> tuple:
            root = self.tmp / f"projects-{name}"
            root.mkdir()
            shutil.copy(FIXTURE, root / "session-fixture.jsonl")
            if subagents_tree:
                (root / "session-fixture" / "subagents").mkdir(parents=True)
            tasks = self.tmp / f"tasks-{name}"
            if tasks_tree:
                (tasks / "session-fixture" / "tasks").mkdir(parents=True)
            else:
                tasks.mkdir()
            db = self.tmp / f"{name}.db"
            return ingest(root, db, tasks_dir=tasks), db

        # (1) ABSENT: no subagents/ tree AND no task index for the session --
        #     nothing was measured, so "no dispatches" is UNKNOWN.
        absent_summary, absent_db = corpus("absent", subagents_tree=False, tasks_tree=False)
        # (2) EMPTY: an empty subagents/ tree AND a scanned (empty) task index
        #     -- measured, and genuinely zero.
        empty_summary, empty_db = corpus("empty", subagents_tree=True, tasks_tree=True)

        # The COUNTERS cannot tell these apart, and that is the point: both
        # report the same shape. Asserted so the next reader does not mistake
        # them for the distinguishing signal.
        for name, summary in (("absent", absent_summary), ("empty", empty_summary)):
            with self.subTest(counters=name):
                self.assertEqual(summary["sessions_with_subagent_transcripts"], 0)
                self.assertEqual(summary["subagent_files_ingested"], 0)

        # (3) POPULATED: the primary fixture, which DID have one.
        self.assertEqual(self.summary["sessions_with_subagent_transcripts"], 1)
        self.assertEqual(self.summary["subagent_files_ingested"], 1)

        # THE SEPARABLE SIGNAL -- `task_index_sessions`, which records the
        # sessions whose tasks/ tree was actually walked. This is the assertion
        # a collapse of absent-vs-empty would break.
        def scanned(db):
            conn = sqlite3.connect(db)
            try:
                return conn.execute(
                    "SELECT COUNT(*) FROM task_index_sessions"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(scanned(absent_db), 0, "no task index was scanned")
        self.assertEqual(scanned(empty_db), 1, "the empty task index WAS scanned")
        self.assertNotEqual(
            scanned(absent_db), scanned(empty_db),
            "absent and empty must remain distinguishable somewhere",
        )


def build_corpus(root: Path) -> tuple[Path, Path]:
    """Lay out a miniature but STRUCTURALLY REAL two-source corpus.

    Mirrors what is actually on disk (verified 2026-08-02):

      projects/<session>.jsonl                        main-thread transcript
      projects/<session>/subagents/agent-<id>.jsonl   canonical subagent store
      projects/<session>/subagents/agent-<id>.meta.json   agentType/description
      tasks/<session>/tasks/<id>.output               per-session task INDEX

    The task index is a set of SYMLINKS into the canonical store, not a second
    copy -- so it supplies dispatch attribution and reaped-transcript
    detection, and must never be ingested as data (that would double-count).

    Returns (projects_dir, tasks_dir).
    """
    projects = root / "projects"
    (projects / "session-fixture" / "subagents").mkdir(parents=True)
    shutil.copy(FIXTURE, projects / "session-fixture.jsonl")

    subs = projects / "session-fixture" / "subagents"
    a1 = subs / "agent-atest1.jsonl"
    shutil.copy(SUBAGENT_FIXTURE, a1)
    (subs / "agent-atest1.meta.json").write_text(json.dumps({
        "agentType": "laravel-expert", "description": "Fix widget",
        "toolUseId": "toolu_XYZ", "spawnDepth": 1,
    }))
    # A SECOND agent, stored under session-fixture but DISPATCHED by
    # other-session (a session resumption -- 749 real cases on this host).
    a2 = subs / "agent-atest2.jsonl"
    a2.write_text(
        '{"type":"assistant","sessionId":"session-fixture","agentId":"agent-atest2",'
        '"timestamp":"2026-07-28T15:04:00.000Z","isSidechain":true,"message":'
        '{"model":"claude-opus-5-20260201","usage":{"input_tokens":5,'
        '"cache_creation_input_tokens":6,"cache_read_input_tokens":7,'
        '"output_tokens":8}}}\n'
    )
    (subs / "agent-atest2.meta.json").write_text(json.dumps({
        "agentType": "architect", "description": "Design the seam",
    }))

    tasks = root / "tasks"
    t1 = tasks / "session-fixture" / "tasks"
    t1.mkdir(parents=True)
    (t1 / "atest1.output").symlink_to(a1)
    # A REAPED transcript: the index still records the dispatch, the file is
    # gone. This must read as UNAVAILABLE, never as zero subagent spend.
    (t1 / "agone.output").symlink_to(subs / "agent-agone.jsonl")
    # A regular-file task output that is NOT a subagent transcript (background
    # bash output shares this directory and carries no usage records).
    (t1 / "b1local.output").write_text('{"type":"progress","text":"working"}\n')

    t2 = tasks / "other-session" / "tasks"
    t2.mkdir(parents=True)
    (t2 / "atest2.output").symlink_to(a2)
    return projects, tasks


class TaskIndexAttributionTest(unittest.TestCase):
    """#4966 elevated scope: per-dispatch attribution + UNAVAILABLE.

    The harness task directory (`/private/tmp/claude-<uid>/<project>/<session>
    /tasks/<agentId>.output`) is a SYMLINK INDEX into the canonical
    `projects/.../subagents/` store -- content is byte-identical (verified
    across three sessions). It is therefore read as an INDEX, never as a data
    source: ingesting it too would double every subagent figure.

    What it uniquely supplies, and neither source alone can:
      * which session DISPATCHED an agent (the storing directory is the
        session that happened to be live when the file was written, and
        differs in 749 of 2834 real cases);
      * that a dispatch happened at all when its transcript has been REAPED
        from /private/tmp -- the difference between "no subagent spend" and
        "subagent spend unmeasured".
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-report-taskindex-test-"))
        self.projects_dir, self.tasks_dir = build_corpus(self.tmp)
        self.db_path = self.tmp / "usage.db"
        self.summary = ingest(self.projects_dir, self.db_path, tasks_dir=self.tasks_dir)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp)

    def q1(self, sql: str, *params) -> sqlite3.Row:
        row = self.conn.execute(sql, params).fetchone()
        self.assertIsNotNone(row, f"query returned no row: {sql}")
        return row

    def test_symlinked_transcript_is_counted_exactly_once(self) -> None:
        # The whole double-count hazard in one assertion: agent-atest1 is
        # reachable from BOTH the canonical store and the task index.
        self.assertEqual(
            self.q1(
                "SELECT COUNT(*) n FROM api_calls WHERE agent_id = 'atest1'"
            )["n"],
            2,  # the subagent fixture's two usage-bearing records
        )

    def test_non_transcript_task_output_contributes_no_calls(self) -> None:
        # b1local.output is a regular file with no usage records; it must not
        # invent an agent or a call.
        self.assertEqual(
            self.q1(
                "SELECT COUNT(*) n FROM subagent_runs WHERE agent_id = 'b1local'"
            )["n"],
            0,
        )

    def test_agent_metadata_comes_from_the_meta_sidecar(self) -> None:
        row = self.q1("SELECT * FROM subagent_runs WHERE agent_id = 'atest1'")
        self.assertEqual(row["agent_type"], "laravel-expert")
        self.assertEqual(row["description"], "Fix widget")
        self.assertEqual(row["status"], "ingested")

    def test_calls_are_attributed_to_the_DISPATCHING_session(self) -> None:
        # atest2 is STORED under session-fixture but DISPATCHED by
        # other-session. Spend follows the dispatcher -- that is the session
        # whose cost the operator is asking about.
        self.assertEqual(
            self.q1("SELECT session_id s FROM api_calls WHERE agent_id = 'atest2'")["s"],
            "other-session",
        )
        self.assertEqual(
            self.q1(
                "SELECT dispatching_session_id d FROM subagent_runs"
                " WHERE agent_id = 'atest2'"
            )["d"],
            "other-session",
        )
        # ...while atest1 has no cross-session redirect and stays put.
        self.assertEqual(
            self.q1("SELECT session_id s FROM api_calls WHERE agent_id = 'atest1'")["s"],
            "session-fixture",
        )

    def test_reaped_transcript_is_recorded_as_UNAVAILABLE_not_zero(self) -> None:
        row = self.q1("SELECT * FROM subagent_runs WHERE agent_id = 'agone'")
        self.assertEqual(row["status"], "unavailable")
        self.assertEqual(row["dispatching_session_id"], "session-fixture")
        # It contributes NO fabricated call rows...
        self.assertEqual(
            self.q1(
                "SELECT COUNT(*) n FROM api_calls WHERE agent_id = 'agone'"
            )["n"],
            0,
        )
        # ...but IS counted, so the gap is visible rather than silently absent.
        self.assertEqual(self.summary["subagent_transcripts_unavailable"], 1)

    def test_index_records_which_sessions_were_scanned(self) -> None:
        # Rule #12: "scanned and found none" and "never scanned" are different
        # facts. Without this a session with no tasks/ dir is indistinguishable
        # from one that genuinely dispatched nothing.
        scanned = {
            r["session_id"]
            for r in self.conn.execute("SELECT session_id FROM task_index_sessions")
        }
        self.assertEqual(scanned, {"session-fixture", "other-session"})

    def test_ingest_without_a_tasks_dir_still_works_and_says_so(self) -> None:
        db2 = self.tmp / "noindex.db"
        summary = ingest(self.projects_dir, db2, tasks_dir=self.tmp / "absent")
        self.assertFalse(summary["task_index_available"])
        self.assertTrue(self.summary["task_index_available"])
        conn = sqlite3.connect(db2)
        try:
            conn.row_factory = sqlite3.Row
            # No index -> fall back to the storing directory for attribution.
            row = conn.execute(
                "SELECT session_id s FROM api_calls WHERE agent_id = 'atest2'"
            ).fetchone()
            self.assertEqual(row["s"], "session-fixture")
            # ...and the reaped dispatch is simply unknown, not invented.
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) n FROM subagent_runs WHERE status = 'unavailable'"
                ).fetchone()["n"],
                0,
            )
        finally:
            conn.close()

    def test_per_agent_rollup_carries_cost_and_model(self) -> None:
        row = self.q1(
            "SELECT agent_id, COUNT(*) calls, SUM(cache_read) cr,"
            " SUM(cost_usd) cost FROM api_calls WHERE agent_id = 'atest1'"
            " GROUP BY agent_id"
        )
        self.assertEqual(row["calls"], 2)
        self.assertEqual(row["cr"], 4090)
        self.assertGreater(row["cost"], 0.0)

    def test_main_thread_calls_have_no_agent_id(self) -> None:
        self.assertEqual(
            self.q1(
                "SELECT COUNT(*) n FROM api_calls"
                " WHERE source_kind = 'main' AND agent_id IS NOT NULL"
            )["n"],
            0,
        )


class SchemaRebuildTest(unittest.TestCase):
    """Every table this tool creates is dropped on a schema-version rebuild.

    Sentry, Qodo and CodeRabbit all raised this independently: the drop loop
    named five tables while the schema creates seven, so a future
    SCHEMA_VERSION bump would leave `subagent_runs` / `task_index_sessions`
    at their stale column shape (`CREATE TABLE IF NOT EXISTS` is a no-op) and
    the next INSERT would fail with an OperationalError.
    """

    def test_every_created_table_is_in_the_rebuild_drop_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "u.db"
            conn = sqlite3.connect(db)
            try:
                ingest_mod._prepare_schema(conn)
                created = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                        " AND name NOT LIKE 'sqlite_%'"
                    )
                }
            finally:
                conn.close()
        # The set the schema actually creates and the set the rebuild drops
        # are ONE set, not two hand-maintained lists that can drift.
        self.assertEqual(created, set(ingest_mod.DERIVED_TABLES))

    def test_version_mismatch_drops_the_new_tables_too(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "u.db"
            conn = sqlite3.connect(db)
            try:
                ingest_mod._prepare_schema(conn)
                conn.execute(
                    "INSERT INTO subagent_runs (agent_id, status) VALUES ('a','ingested')"
                )
                conn.execute(
                    "INSERT INTO task_index_sessions (session_id) VALUES ('s')"
                )
                conn.commit()
                # Simulate an older DB shape.
                conn.execute("PRAGMA user_version = 1")
                self.assertTrue(ingest_mod._prepare_schema(conn))
                for table in ("subagent_runs", "task_index_sessions"):
                    with self.subTest(table=table):
                        self.assertEqual(
                            conn.execute(
                                f"SELECT COUNT(*) FROM {table}"
                            ).fetchone()[0],
                            0,
                            f"{table} survived the rebuild",
                        )
            finally:
                conn.close()


class CarriesApiCallsTriStateTest(unittest.TestCase):
    """A candidate excluded WITHOUT a verdict is not a corroborated absence.

    `carries_api_calls` returned a bare False for three different outcomes, so
    a transcript whose first usage record sits past the scan limit, and one
    that could not be opened at all, were both dropped from `sources` exactly
    as a genuinely-empty file was -- and their spend then read as absent. That
    is the rule-#12 shape re-entering through discovery.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, name: str, lines: list[str]) -> Path:
        p = self.tmp / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_usage_record_found(self) -> None:
        p = self._write("hit.jsonl", [json.dumps({"message": {"usage": {"a": 1}}})])
        self.assertEqual(ingest_mod.carries_api_calls(p), ingest_mod.CARRIES_YES)

    def test_whole_file_scanned_and_empty_is_a_corroborated_no(self) -> None:
        p = self._write("miss.jsonl", [json.dumps({"message": {"content": "x"}})])
        self.assertEqual(ingest_mod.carries_api_calls(p), ingest_mod.CARRIES_NO)

    def test_scan_stopping_early_is_TRUNCATED_not_no(self) -> None:
        # The usage record sits PAST the limit: the honest answer is "we did
        # not look far enough", never "there is nothing here".
        lines = [json.dumps({"message": {"content": "x"}})] * 5
        lines.append(json.dumps({"message": {"usage": {"a": 1}}}))
        p = self._write("trunc.jsonl", lines)
        self.assertEqual(
            ingest_mod.carries_api_calls(p, max_lines=3),
            ingest_mod.CARRIES_TRUNCATED,
        )
        # ...and with the full scan it is a YES, which is what proves the
        # truncated verdict was hiding a real record rather than describing
        # an empty file.
        self.assertEqual(ingest_mod.carries_api_calls(p), ingest_mod.CARRIES_YES)

    def test_unopenable_file_is_UNREADABLE_not_no(self) -> None:
        self.assertEqual(
            ingest_mod.carries_api_calls(self.tmp / "does-not-exist.jsonl"),
            ingest_mod.CARRIES_UNREADABLE,
        )


class PricingTest(unittest.TestCase):
    def test_longest_prefix_wins(self) -> None:
        self.assertEqual(rates_for_model("claude-opus-4-8")["input"], 15.00)
        self.assertEqual(rates_for_model("claude-opus-5-20260201")["input"], 5.00)
        self.assertEqual(rates_for_model("claude-sonnet-5-20260115")["output"], 10.00)
        self.assertEqual(rates_for_model("claude-fable-5")["cache_read"], 1.00)

    def test_unknown_model_returns_none(self) -> None:
        self.assertIsNone(rates_for_model("mystery-model-9"))
        self.assertIsNone(rates_for_model("<synthetic>"))

    def test_prefix_match_requires_boundary(self) -> None:
        # A hypothetical claude-opus-45 must NOT match the claude-opus-4 prefix
        # (CodeRabbit finding: unbounded startswith would silently misprice it).
        self.assertIsNone(rates_for_model("claude-opus-45"))
        # Same family, exact match with no trailing id is still priced.
        self.assertEqual(rates_for_model("claude-opus-4")["input"], 15.00)

    def test_cost_none_for_unpriced_never_zero(self) -> None:
        self.assertIsNone(cost_usd("mystery-model-9", 1000, 1000, 1000, 1000))

    def test_cost_arithmetic(self) -> None:
        # haiku: 1.00 in / 0.10 cache_read / 1.25 cache_write / 5.00 out per MTok
        c = cost_usd("claude-haiku-4-5", 1_000_000, 2_000_000, 3_000_000, 4_000_000)
        self.assertAlmostEqual(c, 1.00 + 2 * 0.10 + 3 * 1.25 + 4 * 5.00, places=9)

    def test_rates_as_of_is_a_dated_iso_string(self) -> None:
        # RATES_AS_OF is what tells a reader whether the hand-maintained
        # PRICES table is fresh or stale (README "Cost figures are an
        # ESTIMATE"). A missing/blank value would silently look current.
        self.assertRegex(RATES_AS_OF, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(RATES_AS_OF, "2026-08-02")


class DefaultProjectsDirTest(unittest.TestCase):
    """The default transcript location must be DERIVED, never an author path.

    Claude Code stores a project's transcripts under
    `~/.claude/projects/<cwd with every '/' replaced by '-'>`. An early revision
    shipped that path hard-coded to one developer's machine, which
    on anyone else's resolves to a directory that does not exist. The
    missing-dir refusal (`ingest.py`, "projects dir not found") means it fails
    loudly rather than reporting an empty run -- so the defect is portability,
    not a silent wrong number. It still makes the tool unusable out of the box
    for every user but one, which is a release blocker for open-sourcing it.
    """

    def test_derives_the_path_from_cwd_using_claude_codes_convention(self) -> None:
        self.assertEqual(
            default_projects_dir(
                cwd=Path("/Users/alice/code/myapp"), home=Path("/Users/alice")
            ),
            Path("/Users/alice/.claude/projects/-Users-alice-code-myapp"),
        )

    def test_a_different_cwd_yields_a_different_path(self) -> None:
        # Pins that cwd is actually READ. A stub returning one constant would
        # satisfy the test above and fail this one.
        home = Path("/home/bob")
        a = default_projects_dir(cwd=Path("/home/bob/one"), home=home)
        b = default_projects_dir(cwd=Path("/home/bob/two"), home=home)
        self.assertNotEqual(a, b)
        self.assertEqual(b.name, "-home-bob-two")

    def test_ships_no_hard_coded_author_path(self) -> None:
        # Structural, and PRECISE about what it bans. The rule is that the
        # CLI default is DERIVED, not a literal -- so the assertion is that
        # `main()` obtains it by calling `default_projects_dir()`.
        #
        # An earlier revision grepped for a machine-specific slug shape
        # instead. That could not distinguish a hard-coded default from the
        # illustrative examples the docstrings legitimately contain
        # (`-Users-alice-code-myapp`), so it failed on correct code. Banning
        # the shape was the wrong rule; requiring the derivation is the right
        # one, and it cannot false-positive on prose.
        src = (Path(__file__).resolve().parent.parent / "ingest.py").read_text()
        self.assertIn(
            "default_projects = default_projects_dir()",
            src,
            "main() must derive --projects-dir, never assign a literal path",
        )

class TranscriptSlugTest(unittest.TestCase):
    """The slug must be RELATIVE, on every platform.

    Extracted as pure string work precisely so this can be tested from a POSIX
    host: `os.path.abspath` here can never return a Windows path, so a test that
    went through `default_projects_dir` could not reach the Windows case at all.
    """

    def test_posix_path_folds_to_claude_codes_convention(self) -> None:
        self.assertEqual(transcript_slug("/Users/alice/code/myapp"), "-Users-alice-code-myapp")

    def test_windows_drive_path_does_not_stay_absolute(self) -> None:
        # The reported bug (CodeRabbit + Qodo). Folding only `/` left
        # `C:\Users\alice\repo` intact, and joining an absolute Windows path
        # DISCARDS the prefix rather than extending it:
        #
        #   PureWindowsPath('C:/Users/alice') / '.claude' / 'projects'
        #       / 'C:\\Users\\alice\\repo'   ->   C:\Users\alice\repo
        #
        # NOTE: no exact-string assertion here, deliberately. Claude Code's real
        # Windows directory encoding is NOT verified -- this host is macOS and
        # the corpus holds no Windows transcript. Asserting a guessed slug would
        # pin a fiction and read as though it had been checked. What IS proven,
        # and all that is asserted, is the property the bug violated: the slug
        # carries no separator and no drive colon, so it cannot be absolute.
        slug = transcript_slug(r"C:\Users\alice\repo")
        for forbidden in ("\\", "/", ":"):
            self.assertNotIn(forbidden, slug)

    def test_documents_the_best_known_windows_encoding(self) -> None:
        """The expected slug, recorded as a BEST-KNOWN value, not a verified one.

        Separate from the property test above on purpose. The property is what
        this code guarantees; this is what we currently believe Claude Code
        produces, and the two deserve different confidence.

        `C--Users-alice-repo` (doubled dash: the drive letter, then one dash
        for the colon and one for the separator) is what CodeRabbit stated on
        PR #4974, consistent with the community reports its earlier search
        surfaced describing the drive letter as carrying "an additional dash".

        NOT verified first-hand: macOS host, no Windows transcript in the
        corpus, and the reports themselves name several competing variants. If
        a Windows user finds this wrong, this is the line to change -- and the
        symptom will have been `ingest()` refusing with "projects dir not
        found" while listing the real directories, not a silent wrong answer.
        """
        self.assertEqual(transcript_slug(r"C:\Users\alice\repo"), "C--Users-alice-repo")

    def test_a_unc_path_is_also_relative(self) -> None:
        self.assertNotIn("\\", transcript_slug(r"\\server\share\repo"))

    def test_the_prefix_can_never_be_overridden(self) -> None:
        # The guarantee, stated directly: whatever the slug, it EXTENDS the
        # projects directory. This is what fails if either separator or the
        # drive colon is ever dropped from the fold.
        home = Path("/home/carol")
        for raw in ("/p/q", r"C:\p\q", r"\\srv\share\q", "relative/q"):
            with self.subTest(raw=raw):
                joined = home / ".claude" / "projects" / transcript_slug(raw)
                self.assertEqual(joined.parent, home / ".claude" / "projects")

    def test_derives_the_PROJECT_root_not_the_tool_subdirectory(self) -> None:
        # Found by a live run, not by review: the README tells you to
        # `cd tools/usage-report && python3 ingest.py`, so the process cwd is
        # the TOOL's directory, and deriving from it asked for a transcript
        # directory named after `.../tools/usage-report` -- which never exists.
        # Claude Code names the directory after the repo you opened it in, so
        # the derivation must climb to the project root.
        root = Path(tempfile.mkdtemp(prefix="pg-root-"))
        (root / ".git").mkdir()
        tool = root / "tools" / "usage-report"
        tool.mkdir(parents=True)
        home = Path("/home/carol")
        self.assertEqual(
            default_projects_dir(cwd=tool, home=home),
            default_projects_dir(cwd=root, home=home),
        )
        shutil.rmtree(root, ignore_errors=True)

    def test_a_git_worktree_uses_its_OWN_root(self) -> None:
        # In a worktree `.git` is a FILE, not a directory, and the worktree is
        # its own project as far as Claude Code is concerned -- its transcripts
        # live under the worktree's own name. So a file must terminate the
        # climb exactly as a directory does; treating only directories as roots
        # would walk past it into the parent repo and read the wrong project.
        root = Path(tempfile.mkdtemp(prefix="pg-wt-"))
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
        nested = root / "tools" / "usage-report"
        nested.mkdir(parents=True)
        home = Path("/home/carol")
        self.assertEqual(
            default_projects_dir(cwd=nested, home=home).name,
            os.path.abspath(root).replace("/", "-"),
        )
        shutil.rmtree(root, ignore_errors=True)

    def test_outside_a_repo_it_falls_back_to_cwd(self) -> None:
        plain = Path(tempfile.mkdtemp(prefix="pg-norepo-"))
        home = Path("/home/carol")
        self.assertEqual(
            default_projects_dir(cwd=plain, home=home).name,
            os.path.abspath(plain).replace("/", "-"),
        )
        shutil.rmtree(plain, ignore_errors=True)


class AvailableProjectsTest(unittest.TestCase):
    """A missing project dir must SAY WHAT EXISTS, not just refuse.

    Refusing is already correct (rule #12 -- never report an empty run). But
    for anyone who is not the author, the first invocation lands on a derived
    path they have never seen, and a bare "not found" gives them nothing to act
    on. Listing the sibling directories turns a dead end into a next step, and
    costs one `iterdir`.
    """

    def test_lists_project_directories_under_the_claude_home(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="pg-home-"))
        projects = home / ".claude" / "projects"
        (projects / "-home-carol-alpha").mkdir(parents=True)
        (projects / "-home-carol-beta").mkdir(parents=True)
        (projects / "not-a-dir.txt").parent.mkdir(parents=True, exist_ok=True)
        (projects / "not-a-dir.txt").write_text("")
        found = [p.name for p in available_projects(home=home)]
        self.assertEqual(found, ["-home-carol-alpha", "-home-carol-beta"])
        shutil.rmtree(home, ignore_errors=True)

    def test_a_missing_claude_home_is_an_empty_list_not_a_crash(self) -> None:
        self.assertEqual(available_projects(home=Path("/nonexistent-xyz")), [])


class ContentBlockTest(unittest.TestCase):
    """What the context is MADE OF -- #4967's composition detectors.

    `api_calls` records how many tokens a call cost but nothing about what
    those tokens were. Every composition figure this epic produced (prose
    20-56%, Bash input 12-33%, Bash output 11-17%) was counted by hand off the
    transcripts; this is the pass that makes them queryable.

    ## Why CHARACTERS, and why that is not a token count

    The tool is stdlib-only, so there is no tokenizer, and there is no
    per-block token figure in the transcript -- `usage` is per-CALL only. Chars
    are therefore the honest measured unit. They are NOT tokens and must never
    be presented as tokens: chars-per-token varies by content (minified JSON
    tool output tokenizes very differently from English prose), so a
    composition stated in chars overstates the dense contributors' share of the
    real context. Any token-denominated composition is an APPORTIONMENT of the
    measured per-call total under a uniform-density assumption, and must be
    labelled as such (the tool already keeps measured tokens and estimated
    dollars visually and semantically distinct; this is the same split).

    ## The fixture pins each contributor to a DIFFERENT length

    ...and gives the two tools OPPOSITE input/output relationships -- Bash
    100 in / 30 out, Read 42 in / 200 out. A detector that assumed one
    direction globally (the hand-measured finding was "Bash input exceeds Bash
    output") would pass on a single-tool fixture and be wrong; here it cannot.
    """

    CONTENT_FIXTURE = (
        Path(__file__).resolve().parent / "fixtures" / "content-blocks.jsonl"
    )
    STRIPPED_FIXTURE = (
        Path(__file__).resolve().parent / "fixtures" / "thinking-stripped.jsonl"
    )

    def setUp(self) -> None:
        self.parsed = parse_file(self.CONTENT_FIXTURE)
        self.by = {}
        for b in self.parsed.content_blocks:
            self.by[(b.block_type, b.tool_name)] = b.chars

    def test_assistant_prose_and_thinking_are_separate_contributors(self) -> None:
        # Collapsing thinking into prose would hide the single largest
        # assistant-side contributor behind the one people already watch.
        self.assertEqual(self.by[("thinking", None)], 70)
        self.assertEqual(self.by[("text", None)], 40)

    def test_a_STRIPPED_thinking_block_is_unmeasurable_NOT_zero(self) -> None:
        """Measured on the real corpus: 0 of 14,918 thinking blocks keep their
        text. Claude Code persists `type` + an empty `thinking` + a
        `signature`, so thinking CONTENT is structurally absent from every
        transcript -- while thinking TOKENS are still billed inside
        `output_tokens`.

        Recording that as `chars=0` would make a composition table report
        "thinking: 0.0%", i.e. thinking costs nothing. It is the exact defect
        this tool exists to catch (rule #12), so a stripped block carries
        `chars=None` -- unmeasurable, and countable as such -- while a block
        that really does carry text is measured normally.
        """
        stripped = parse_file(self.STRIPPED_FIXTURE).content_blocks
        thinking = [b for b in stripped if b.block_type == "thinking"]
        self.assertEqual(len(thinking), 1, "the block must still be COUNTED")
        self.assertIsNone(
            thinking[0].chars,
            "a stripped thinking block must be unmeasurable, never a real 0",
        )

    def test_tool_input_is_attributed_to_the_TOOL_that_carried_it(self) -> None:
        self.assertEqual(self.by[("tool_use", "Bash")], 115)
        self.assertEqual(self.by[("tool_use", "Read")], 42)

    def test_tool_RESULT_is_attributed_to_its_tool_via_tool_use_id(self) -> None:
        # A tool_result names only a tool_use_id; the tool NAME has to be
        # carried forward from the tool_use block that opened it. Without that
        # join every result lands in one anonymous bucket and per-tool output
        # attribution is impossible.
        self.assertEqual(self.by[("tool_result", "Bash")], 30)
        self.assertEqual(self.by[("tool_result", "Read")], 200)

    def test_the_two_tools_run_in_OPPOSITE_directions(self) -> None:
        # The property the fixture exists to protect: input>output for one
        # tool and output>input for the other, so no global assumption passes.
        self.assertGreater(self.by[("tool_use", "Bash")], self.by[("tool_result", "Bash")])
        self.assertLess(self.by[("tool_use", "Read")], self.by[("tool_result", "Read")])

    def test_the_human_prompt_is_its_own_contributor(self) -> None:
        self.assertEqual(self.by[("user_text", None)], 55)

    def test_every_block_in_the_fixture_is_accounted_for(self) -> None:
        # Rule #12 at the aggregate: a silently dropped block would make every
        # share sum to less than the whole while still looking like a total.
        self.assertEqual(len(self.parsed.content_blocks), 7)
        self.assertEqual(sum(b.chars for b in self.parsed.content_blocks), 552)


class ReapedTranscriptRetentionTest(unittest.TestCase):
    """A transcript the OS reaped must not take its measurements with it.

    Two mechanisms combined into silent, unrecoverable loss:

      1. Claude Code deletes transcripts after `cleanupPeriodDays`, default 30.
      2. `ingest()` deleted every row whose source file was no longer on disk.

    So each run destroyed the measurements for every session reaped since the
    last one -- and the DB is the ONLY durable copy, because the source is gone.
    Verified on a real corpus: an ingest measured sessions from 2026-06-06 while
    the oldest surviving transcript was 2026-06-30.

    The prune existed for a real reason (a renamed or deleted transcript should
    not linger in the UI). The defect is that it could not tell two very
    different causes apart:

      user deleted it       -> prune; the data is unwanted
      the OS reaped it      -> RETAIN; the data is irreplaceable

    Collapsing them treats scheduled expiry as a delete request.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-retention-test-"))
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "session-fixture.jsonl"
        shutil.copy(FIXTURE, self.transcript)
        self.db = self.tmp / "usage.db"
        ingest(self.projects, self.db)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def calls(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
        finally:
            conn.close()

    def test_measurements_survive_the_transcript_being_reaped(self) -> None:
        before = self.calls()
        self.assertGreater(before, 0, "fixture must produce rows to begin with")
        self.transcript.unlink()          # the OS reaping it, 30 days on
        ingest(self.projects, self.db)    # the run that used to destroy them
        self.assertEqual(
            self.calls(), before,
            "a reaped transcript must not delete its measurements -- the DB is "
            "the only durable copy once the source is gone",
        )

    def test_the_source_is_marked_archived_rather_than_forgotten(self) -> None:
        self.transcript.unlink()
        ingest(self.projects, self.db)
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT archived_at FROM ingest_state WHERE path = ?",
                (str(self.transcript),),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "the source row itself must be retained")
        self.assertIsNotNone(
            row[0], "a source whose file is gone must be MARKED, so an aggregate "
                    "over it can say the corpus is truncated",
        )

    def test_a_returning_transcript_is_un_archived(self) -> None:
        # A file can come back -- a worktree remounted, a restore. The mark
        # describes the CURRENT state, not a one-way latch.
        shutil.copy(FIXTURE, self.projects / "other.jsonl")
        ingest(self.projects, self.db)
        self.transcript.unlink()
        ingest(self.projects, self.db)
        shutil.copy(FIXTURE, self.transcript)
        ingest(self.projects, self.db)
        conn = sqlite3.connect(self.db)
        try:
            archived = conn.execute(
                "SELECT archived_at FROM ingest_state WHERE path = ?",
                (str(self.transcript),),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(archived)

    def test_explicit_prune_is_the_only_way_to_delete(self) -> None:
        before = self.calls()
        self.transcript.unlink()
        ingest(self.projects, self.db, prune_missing=True)
        self.assertLess(
            self.calls(), before,
            "the destructive behaviour must still be REACHABLE -- it is opt-in, "
            "not removed",
        )

    def test_summary_reports_archived_separately_from_pruned(self) -> None:
        self.transcript.unlink()
        summary = ingest(self.projects, self.db)
        self.assertEqual(summary["files_archived"], 1)
        self.assertEqual(
            summary["files_pruned"], 0,
            "retaining is not pruning; the two counts must stay distinguishable",
        )


class SchemaRebuildSafetyTest(unittest.TestCase):
    """A schema rebuild must not destroy what a re-ingest cannot reproduce.

    `_prepare_schema` drops and rebuilds on a version change, justified by:
    "The DB holds no original data -- it is a derived rendering of the
    transcripts and a full re-ingest reproduces it exactly."

    That premise is FALSE once any transcript has been reaped, and it is the
    same false premise as the prune above: it treats an ephemeral source as a
    durable one. The trap is self-inflicted -- bumping SCHEMA_VERSION to ADD
    the archive column would itself destroy the data the column exists to save.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-rebuild-test-"))
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "session-fixture.jsonl"
        shutil.copy(FIXTURE, self.transcript)
        self.db = self.tmp / "usage.db"
        ingest(self.projects, self.db)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rebuild_refuses_when_a_source_can_no_longer_be_re_read(self) -> None:
        self.transcript.unlink()
        ingest(self.projects, self.db)  # archives it; rows retained

        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA user_version = 1")  # simulate an older shape
        conn.commit()
        conn.close()

        with self.assertRaises(SystemExit) as caught:
            ingest(self.projects, self.db)
        message = str(caught.exception)
        self.assertIn("REFUSING", message)
        # The refusal must NAME the source it is protecting -- an operator who
        # cannot see which file is at risk cannot decide whether to back up.
        self.assertIn(str(self.transcript), message)

    def test_rebuild_still_proceeds_when_every_source_is_re_readable(self) -> None:
        # The original justification holds while nothing has been reaped, so
        # the safety check must not block the ordinary upgrade path.
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        summary = ingest(self.projects, self.db)
        self.assertTrue(summary["schema_rebuilt"])

class StreamedRecordDedupeTest(unittest.TestCase):
    """One API call is ONE row, keyed on `message.id` (#2).

    Claude Code writes one transcript record per streamed content block, and
    every one repeats the SAME `message.usage`. Counting each as a call
    inflated every aggregate 1.9-2.4x. Worse than a scale error: the factor
    differs by scope, so main-vs-subagent comparisons were distorted in SHAPE.

    The resolution rule is not a guess. Across the real corpus `output_tokens`
    is non-decreasing over the records of one id in 4,928 of 4,928 cases, and
    only the last record carries the finished total -- so LAST WINS, and
    first-wins would have undercounted output by ~99%.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-dedupe-test-"))
        self.projects = self.tmp / "projects"
        self.projects.mkdir(parents=True)
        shutil.copy(FIXTURES / "streamed-message.jsonl", self.projects / "s.jsonl")
        self.db = self.tmp / "usage.db"
        ingest(self.tmp / "projects", self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp)

    def test_three_records_of_one_message_are_ONE_call(self) -> None:
        n = self.conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
        self.assertEqual(n, 2, "one row per message.id: msg_stream + msg_second")

    def test_the_surviving_row_carries_the_FINAL_output_count(self) -> None:
        # 450, not 2 (the first record) and not 454 (the sum). Fixture pins
        # these three deliberately unequal so no two can be confused.
        row = self.conn.execute(
            "SELECT output_tokens FROM api_calls WHERE message_id = 'msg_stream'"
        ).fetchone()
        self.assertEqual(row["output_tokens"], 450)

    def test_input_and_cache_are_not_multiplied(self) -> None:
        row = self.conn.execute(
            "SELECT input_tokens, cache_read, cache_write FROM api_calls"
            " WHERE message_id = 'msg_stream'"
        ).fetchone()
        self.assertEqual(
            (row["input_tokens"], row["cache_read"], row["cache_write"]),
            (11, 9000, 700),
            "repeated identical usage must be counted once, not summed",
        )

    def test_a_genuinely_separate_call_survives_as_its_own_row(self) -> None:
        # Dedupe must not collapse distinct calls; the guard against an
        # over-eager fix is a second id in the same file.
        row = self.conn.execute(
            "SELECT output_tokens FROM api_calls WHERE message_id = 'msg_second'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["output_tokens"], 33)

    def test_totals_reflect_calls_not_records(self) -> None:
        total = self.conn.execute(
            "SELECT SUM(output_tokens) FROM api_calls"
        ).fetchone()[0]
        self.assertEqual(total, 483)  # 450 + 33, never 2+2+450+33


class DivergentUsageTest(unittest.TestCase):
    """When records of one id disagree beyond output, pick a WHOLE record.

    Field-wise maxima would fabricate a combination that occurred in no real
    API response -- on the real corpus, the id that diverges has cw=5857 on
    its early records and cw=0 on the last, so a per-field max reports a call
    that both wrote and did not write cache. One real row, and the ambiguity
    COUNTED and surfaced rather than hidden (rule #12).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-divergent-test-"))
        self.projects = self.tmp / "projects"
        self.projects.mkdir(parents=True)
        shutil.copy(FIXTURES / "divergent-usage.jsonl", self.projects / "d.jsonl")
        self.db = self.tmp / "usage.db"
        self.summary = ingest(self.tmp / "projects", self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp)

    def test_the_row_is_one_real_record_not_a_synthesis(self) -> None:
        row = self.conn.execute(
            "SELECT input_tokens, cache_read, cache_write, output_tokens"
            " FROM api_calls WHERE message_id = 'msg_diverge'"
        ).fetchone()
        self.assertEqual(
            (row["input_tokens"], row["cache_write"], row["cache_read"], row["output_tokens"]),
            (26, 0, 238759, 263),
            "must be the last record verbatim, never a per-field maximum",
        )

    def test_the_ambiguity_is_counted_and_surfaced(self) -> None:
        self.assertEqual(self.summary["divergent_message_ids"], 1)

    def test_an_agreeing_duplicate_is_NOT_counted_as_divergent(self) -> None:
        # Otherwise the diagnostic fires on every ordinary streamed message
        # and becomes noise nobody reads.
        other = Path(tempfile.mkdtemp(prefix="cpb-agree-test-"))
        proj = other / "projects"
        proj.mkdir(parents=True)
        shutil.copy(FIXTURES / "streamed-message.jsonl", proj / "s.jsonl")
        try:
            summary = ingest(other / "projects", other / "usage.db")
            self.assertEqual(summary["divergent_message_ids"], 0)
        finally:
            shutil.rmtree(other)


class MissingMessageIdTest(unittest.TestCase):
    """A record with no `message.id` is kept and COUNTED, never dropped.

    Dedupe keys on the id, so the tempting shortcut is to skip records that
    lack one -- which would silently delete real spend. Every such record
    stays its own call, and the count is surfaced so a corpus where this is
    common cannot look like a clean one.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-noid-test-"))
        self.projects = self.tmp / "projects"
        self.projects.mkdir(parents=True)
        shutil.copy(FIXTURES / "no-message-id.jsonl", self.projects / "n.jsonl")
        self.db = self.tmp / "usage.db"
        self.summary = ingest(self.tmp / "projects", self.db)
        self.conn = sqlite3.connect(self.db)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp)

    def test_records_without_an_id_are_still_ingested(self) -> None:
        n = self.conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
        self.assertEqual(n, 2)

    def test_they_are_not_collapsed_into_each_other(self) -> None:
        total = self.conn.execute("SELECT SUM(output_tokens) FROM api_calls").fetchone()[0]
        self.assertEqual(total, 25, "12 + 13 -- a NULL id is not a shared key")

    def test_the_count_is_surfaced(self) -> None:
        self.assertEqual(self.summary["calls_without_message_id"], 2)


if __name__ == "__main__":
    unittest.main()
